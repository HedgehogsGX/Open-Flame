from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from video_download_control.uploads.contracts import BackendResult, UploadError
from video_download_control.uploads.service import UploadService, default_upload_root


class FakeBackend:
    def __init__(self):
        self.uploads = []
        self.accounts = []
        self.result = BackendResult("submitted", "upstream_submitted")
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def inspect(self):
        return {"ready": True, "code": "ready"}

    def login(self, platform, account_id, stop):
        self.accounts.append((platform, account_id))
        return BackendResult("ready", "account_ready")

    check = login

    def upload(self, request, stop):
        self.uploads.append(request)
        self.entered.set()
        while not self.release.wait(0.01):
            if stop.is_set():
                return BackendResult("unknown", "upload_cancelled_unknown")
        return self.result


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("upload operation did not complete")


@pytest.fixture
def service(tmp_path):
    backend = FakeBackend()
    service = UploadService(tmp_path / "uploads", backend)
    service.start()
    yield service
    backend.release.set()
    service.stop()


def account(service, platform="bilibili"):
    result = service.add_account(platform, "测试账号")
    service.account_action(result["id"], "check")
    wait_for(lambda: any(a["id"] == result["id"] and a["auth_state"] == "ready" for a in service.accounts()))
    return result


def source(service, tmp_path):
    path = tmp_path / "original.mp4"
    path.write_bytes(b"test video payload")
    return service.import_source(path, "原始视频.mp4")


def drafts(service, tmp_path, **overrides):
    values = dict(source_id=source(service, tmp_path)["id"], account_ids=[account(service)["id"]],
                  title="测试标题", description="说明", tags=["测试"], category_id=249,
                  idempotency_key="request_12345", copyright=2, source_credit="已获授权的来源")
    values.update(overrides)
    return service.create_jobs(**values)


def test_draft_is_persistent_and_requires_explicit_confirm(service, tmp_path):
    job = drafts(service, tmp_path)[0]
    assert job["state"] == "draft"
    assert service.backend.uploads == []
    second_reader = UploadService(service.root, FakeBackend())
    second_reader.start()
    assert second_reader.status()["worker_running"] is False
    assert second_reader.jobs()[0]["state"] == "draft"
    service.confirm(job["id"])
    service.confirm(job["id"])
    wait_for(lambda: service.jobs()[0]["state"] == "submitted")
    assert len(service.backend.uploads) == 1
    request = service.backend.uploads[0]
    assert request.copyright == 2 and request.source_credit == "已获授权的来源"
    assert request.file_path.read_bytes() == b"test video payload"
    assert request.file_path.parent == service.root / "media"


def test_all_three_platforms_share_one_reviewable_batch(service, tmp_path):
    accounts = [account(service, p) for p in ("bilibili", "douyin", "tencent")]
    values = dict(source_id=source(service, tmp_path)["id"], account_ids=[a["id"] for a in accounts],
                  title="三平台", description="说明", tags=["测试"], category_id=249, copyright=1, idempotency_key="request_multi")
    jobs = service.create_jobs(**values)
    assert {job["platform"] for job in jobs} == {"bilibili", "douyin", "tencent"}
    assert service.create_jobs(**values) == jobs
    with pytest.raises(UploadError, match="idempotency_conflict"):
        service.create_jobs(**{**values, "title": "不同内容"})
    for job in jobs:
        service.confirm(job["id"])
    wait_for(lambda: all(job["state"] == "submitted" for job in service.jobs()))
    assert len(service.backend.uploads) == 3


def test_draft_mode_is_supported_only_by_channels(service, tmp_path):
    channels = account(service, "tencent")
    job = service.create_jobs(source_id=source(service, tmp_path)["id"], account_ids=[channels["id"]],
                              title="草稿", description="说明", tags=[], mode="draft", idempotency_key="request_draft")[0]
    service.backend.result = BackendResult("draft_saved", "upstream_draft_saved")
    service.confirm(job["id"])
    wait_for(lambda: service.jobs()[0]["state"] == "draft_saved")
    assert service.backend.uploads[0].mode == "draft"
    with pytest.raises(UploadError, match="draft_mode_unsupported"):
        drafts(service, tmp_path, mode="draft")


def test_cancel_queued_never_uploads_and_cancel_running_is_unknown(service, tmp_path):
    service.backend.release.clear()
    first = drafts(service, tmp_path)[0]
    service.confirm(first["id"])
    assert service.backend.entered.wait(3)
    second = service.create_jobs(source_id=first["source_id"], account_ids=[first["account_id"]],
                                 title="第二项", description="", tags=["测试"], category_id=249,
                                 copyright=1, idempotency_key="second_request")[0]
    service.confirm(second["id"])
    assert service.cancel(second["id"])["state"] == "canceled"
    service.cancel(first["id"])
    wait_for(lambda: {job["id"]: job["state"] for job in service.jobs()}[first["id"]] == "unknown")
    assert len(service.backend.uploads) == 1
    with pytest.raises(UploadError, match="verify_remote_result_first"):
        service.retry(first["id"])
    retry = service.retry(first["id"], acknowledge_unknown=True)
    assert retry["state"] == "draft" and retry["id"] != first["id"]
    assert service.retry(first["id"], acknowledge_unknown=True)["id"] == retry["id"]


def test_mutated_source_fails_before_platform_is_called(service, tmp_path):
    job = drafts(service, tmp_path)[0]
    path = next((service.root / "media").iterdir())
    path.write_bytes(b"different video!!!")
    service.confirm(job["id"])
    wait_for(lambda: service.jobs()[0]["state"] == "failed")
    assert service.jobs()[0]["code"] == "source_changed"
    assert service.backend.uploads == []


def test_import_checks_registered_hash_and_preserves_original(service, tmp_path):
    path = tmp_path / "registered.mp4"
    payload = b"downloaded original"
    path.write_bytes(payload)
    with pytest.raises(UploadError, match="source_hash_mismatch"):
        service.import_source(path, "video.mp4", "0" * 64)
    assert service.sources() == []
    assert list((service.root / "media").iterdir()) == []
    result = service.import_source(path, "video.mp4", hashlib.sha256(payload).hexdigest())
    assert result["size"] == len(payload)
    assert path.read_bytes() == payload
    assert not any("path" in key or "suffix" in key for key in result)


def test_recovery_requires_review_and_does_not_replay_upload(tmp_path):
    backend = FakeBackend()
    service = UploadService(tmp_path / "uploads", backend)
    # Populate records without starting a real operation.
    acct = service.add_account("douyin", "账号")
    src = source(service, tmp_path)
    jobs = []
    for i in range(2):
        jobs.extend(service.create_jobs(source_id=src["id"], account_ids=[acct["id"]], title="标题",
                                       description="", tags=[], idempotency_key=f"request_{i}"))
    with sqlite3.connect(service.database_path) as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (jobs[0]["id"],))
        db.execute("UPDATE jobs SET state='queued' WHERE id=?", (jobs[1]["id"],))
    service.start()
    try:
        states = {job["id"]: job["state"] for job in service.jobs()}
        assert states[jobs[0]["id"]] == "unknown"
        assert states[jobs[1]["id"]] == "draft"
        assert backend.uploads == []
    finally:
        service.stop()


def test_bilibili_metadata_is_not_invented(service, tmp_path):
    with pytest.raises(UploadError, match="bilibili_category_required"):
        drafts(service, tmp_path, category_id=None)
    with pytest.raises(UploadError, match="source_credit_required"):
        # Reuse existing account/source through a plain draft call.
        service.create_jobs(source_id=service.sources()[0]["id"], account_ids=[service.accounts()[0]["id"]],
                            title="转载", description="", tags=["测试"], category_id=249,
                            copyright=2, source_credit="", idempotency_key="credit_required")
    assert service.jobs() == []


def test_private_upload_root_is_outside_download_backup(tmp_path):
    data = tmp_path / "data"
    assert default_upload_root(data) == tmp_path / "data-uploads"
    assert not default_upload_root(data).is_relative_to(data)


def test_unknown_exception_never_leaks_backend_details(service, tmp_path):
    def failing_backend(request, stop):
        raise RuntimeError("cookie=PRIVATE-CANARY; secret path")
    service.backend.upload = failing_backend
    job = drafts(service, tmp_path)[0]
    service.confirm(job["id"])
    wait_for(lambda: service.jobs()[0]["state"] == "unknown")
    assert service.jobs()[0]["code"] == "backend_result_unknown"
    assert "PRIVATE-CANARY" not in str(service.jobs())


def test_login_cancel_does_not_leave_account_checking(service):
    # Queue without an executor, then cancel deterministically.
    service.stop()
    acct = service.add_account("tencent", "登录测试")
    op = service.account_action(acct["id"], "login")
    result = service.cancel_operation(op["id"])
    assert result["state"] == "canceled"
    assert service.accounts()[0]["auth_state"] == "unchecked"


def test_second_instance_cancel_reaches_active_owner(service, tmp_path):
    service.backend.release.clear()
    job = drafts(service, tmp_path)[0]
    service.confirm(job["id"])
    assert service.backend.entered.wait(3)
    reader = UploadService(service.root, FakeBackend())
    reader.start()
    assert reader.status()["worker_running"] is False
    assert reader.cancel(job["id"])["code"] == "cancellation_requested"
    wait_for(lambda: reader.jobs()[0]["state"] == "unknown")
    assert service.backend.uploads and len(service.backend.uploads) == 1


def test_second_instance_can_cancel_account_login(service):
    entered = threading.Event()

    def waiting_login(platform, account_id, stop):
        entered.set()
        assert stop.wait(4)
        return BackendResult("cancelled", "cancelled")

    service.backend.login = waiting_login
    acct = service.add_account("tencent", "扫码")
    operation = service.account_action(acct["id"], "login")
    assert entered.wait(3)
    reader = UploadService(service.root, FakeBackend())
    reader.cancel_operation(operation["id"])
    wait_for(lambda: reader.operations()[0]["state"] == "canceled")
    assert reader.accounts()[0]["auth_state"] == "unchecked"


def test_malformed_backend_result_does_not_stop_the_queue(service, tmp_path):
    service.backend.result = BackendResult("submitted", None)
    job = drafts(service, tmp_path)[0]
    service.confirm(job["id"])
    wait_for(lambda: service.jobs()[0]["state"] == "unknown")
    assert service.jobs()[0]["code"] == "backend_result_invalid"
    assert service.status()["worker_running"]
    service.backend.result = BackendResult("submitted", "upstream_submitted")
    retry = service.retry(job["id"], acknowledge_unknown=True)
    service.confirm(retry["id"])
    wait_for(lambda: service.jobs()[0]["state"] == "submitted")


def test_failed_account_check_stops_previously_queued_submission(service, tmp_path):
    service.backend.release.clear()
    first = drafts(service, tmp_path)[0]
    service.confirm(first["id"])
    assert service.backend.entered.wait(3)
    second = service.create_jobs(source_id=first["source_id"], account_ids=[first["account_id"]],
                                 title="第二项", description="", tags=["测试"], category_id=249,
                                 copyright=1, idempotency_key="second_queued")[0]
    service.confirm(second["id"])
    service.backend.check = lambda *args: BackendResult("failed", "account_invalid")
    service.account_action(first["account_id"], "check")
    service.backend.release.set()
    wait_for(lambda: {job["id"]: job["state"] for job in service.jobs()}[second["id"]] == "failed")
    assert len(service.backend.uploads) == 1
    assert service.accounts()[0]["auth_state"] == "invalid"


def test_new_login_requires_reconfirming_prior_queued_drafts(service, tmp_path):
    service.backend.release.clear()
    first = drafts(service, tmp_path)[0]
    service.confirm(first["id"])
    assert service.backend.entered.wait(3)
    second = service.create_jobs(source_id=first["source_id"], account_ids=[first["account_id"]],
                                 title="第二项", description="", tags=["测试"], category_id=249,
                                 copyright=1, idempotency_key="reauth_queued")[0]
    service.confirm(second["id"])
    service.account_action(first["account_id"], "login")
    second_now = {job["id"]: job for job in service.jobs()}[second["id"]]
    assert second_now["state"] == "draft"
    assert second_now["code"] == "account_session_changed"
    service.backend.release.set()
    wait_for(lambda: service.operations()[0]["state"] == "ready")
    assert len(service.backend.uploads) == 1


def test_unknown_database_is_not_modified(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    path = root / "uploads.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE valuable(value TEXT)")
        db.execute("INSERT INTO valuable VALUES('keep')")
    before = path.read_bytes()
    with pytest.raises(UploadError, match="upload_schema_unsupported"):
        UploadService(root, FakeBackend())
    assert path.read_bytes() == before


def test_bilibili_requires_explicit_copyright_choice(service, tmp_path):
    with pytest.raises(UploadError, match="bilibili_copyright_required"):
        drafts(service, tmp_path, copyright=None)
    assert service.jobs() == []

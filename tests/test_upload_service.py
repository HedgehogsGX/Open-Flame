from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from video_download_control.uploads.contracts import BackendResult, UploadError
from video_download_control.uploads import service as upload_service_module
from video_download_control.uploads.schema import SCHEMA_DDL, SCHEMA_VERSION
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
    assert service.source(job["source_id"])["media_state"] == "present"

    with pytest.raises(UploadError, match="^source_changed$"):
        service.confirm(job["id"])

    assert service.jobs()[0]["state"] == "draft"
    assert service.source(job["source_id"])["media_state"] == "changed"
    assert service.backend.uploads == []


def test_same_size_source_mutation_is_rejected_before_draft_creation(service, tmp_path):
    imported = source(service, tmp_path)
    acct = account(service, "douyin")
    path = next((service.root / "media").iterdir())
    original = path.read_bytes()
    path.write_bytes(b"x" * len(original))

    with pytest.raises(UploadError, match="^source_changed$"):
        service.create_jobs(
            source_id=imported["id"],
            account_ids=[acct["id"]],
            title="校验内容",
            description="",
            tags=[],
            idempotency_key="same_size_changed",
        )

    assert service.jobs() == []
    assert service.source(imported["id"])["media_state"] == "changed"


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


def test_final_database_write_failure_recovers_without_retransmitting(tmp_path, monkeypatch):
    backend = FakeBackend()
    backend.release.clear()
    service = UploadService(tmp_path / "uploads", backend)
    service.start()
    first = drafts(service, tmp_path)[0]
    second = service.create_jobs(
        source_id=first["source_id"], account_ids=[first["account_id"]], title="第二项",
        description="", tags=["测试"], category_id=249, copyright=1,
        idempotency_key="final_write_second",
    )[0]
    service.confirm(first["id"])
    assert backend.entered.wait(3)
    service.confirm(second["id"])

    original_db = service._db
    failed = threading.Event()

    @contextmanager
    def fail_one_worker_write(*args, **kwargs):
        if (threading.current_thread().name == "open-flame-uploads"
                and backend.release.is_set() and not failed.is_set()):
            failed.set()
            raise sqlite3.OperationalError("synthetic final write failure")
        with original_db(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(service, "_db", fail_one_worker_write)
    backend.release.set()
    try:
        wait_for(lambda: failed.is_set())
        wait_for(lambda: (
            not service.status()["worker_running"]
            or {row["id"]: row["state"] for row in service.jobs()}.get(first["id"]) != "running"
        ))
        current = {row["id"]: row for row in service.jobs()}
        assert service.status()["worker_running"] is True
        assert service.status()["scheduler_state"] == "running"
        assert service.status()["scheduler_code"] == "scheduler_recovered"
        assert current[first["id"]]["state"] == "unknown"
        assert current[first["id"]]["code"] == "interrupted_result_unknown"
        assert current[second["id"]]["state"] == "draft"
        assert current[second["id"]]["code"] == "restart_confirmation_required"
        assert len(backend.uploads) == 1
    finally:
        service.stop()


def test_persistent_final_write_failure_is_visible_and_explicitly_recoverable(tmp_path, monkeypatch):
    backend = FakeBackend()
    backend.release.clear()
    service = UploadService(tmp_path / "uploads", backend)
    service.start()
    first = drafts(service, tmp_path)[0]
    second = service.create_jobs(
        source_id=first["source_id"], account_ids=[first["account_id"]], title="第二项",
        description="", tags=["测试"], category_id=249, copyright=1,
        idempotency_key="persistent_write_second",
    )[0]
    service.confirm(first["id"])
    assert backend.entered.wait(3)
    service.confirm(second["id"])

    original_db = service._db

    @contextmanager
    def fail_worker_database(*args, **kwargs):
        if threading.current_thread().name == "open-flame-uploads" and backend.release.is_set():
            raise sqlite3.OperationalError("synthetic persistent database failure")
        with original_db(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(service, "_db", fail_worker_database)
    backend.release.set()
    wait_for(lambda: service.status()["scheduler_state"] == "faulted")
    assert service.status()["worker_running"] is False
    assert service.status()["scheduler_code"] == "scheduler_database_unavailable"
    assert len(backend.uploads) == 1

    monkeypatch.setattr(service, "_db", original_db)
    service.start()
    try:
        current = {row["id"]: row for row in service.jobs()}
        assert service.status()["worker_running"] is True
        assert service.status()["scheduler_code"] == "scheduler_recovered"
        assert current[first["id"]]["state"] == "unknown"
        assert current[second["id"]]["state"] == "draft"
        assert len(backend.uploads) == 1
    finally:
        service.stop()


def test_database_connection_closes_when_connection_pragma_fails(tmp_path, monkeypatch):
    service = UploadService(tmp_path / "uploads", FakeBackend())
    real = sqlite3.connect(":memory:")
    closed = threading.Event()

    class FailingPragmaConnection:
        row_factory = None

        def execute(self, statement, *args):
            if statement == "PRAGMA journal_mode=WAL":
                raise sqlite3.OperationalError("synthetic pragma failure")
            return real.execute(statement, *args)

        def close(self):
            real.close()
            closed.set()

    monkeypatch.setattr(upload_service_module.sqlite3, "connect", lambda *args, **kwargs: FailingPragmaConnection())
    with pytest.raises(sqlite3.OperationalError, match="synthetic pragma failure"):
        with service._db():
            pass
    assert closed.is_set()


def test_bilibili_metadata_is_not_invented(service, tmp_path):
    with pytest.raises(UploadError, match="bilibili_category_required"):
        drafts(service, tmp_path, category_id=None)
    with pytest.raises(UploadError, match="source_credit_required"):
        # Reuse existing account/source through a plain draft call.
        service.create_jobs(source_id=service.sources()[0]["id"], account_ids=[service.accounts()[0]["id"]],
                            title="转载", description="", tags=["测试"], category_id=249,
                            copyright=2, source_credit="", idempotency_key="credit_required")
    assert service.jobs() == []


@pytest.mark.parametrize("category_id", [True, False, 249.0, "249", 0, 10001])
def test_category_id_requires_an_in_range_plain_integer(service, tmp_path, category_id):
    douyin = account(service, "douyin")
    imported = source(service, tmp_path)

    with pytest.raises(UploadError, match="^invalid_metadata$"):
        service.create_jobs(
            source_id=imported["id"],
            account_ids=[douyin["id"]],
            title="分类校验",
            description="",
            tags=[],
            category_id=category_id,
            idempotency_key="invalid_category",
        )

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
    wait_for(lambda: {row["id"]: row["state"] for row in service.jobs()}[retry["id"]] == "submitted")


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


def test_version_one_incomplete_database_is_rejected_without_any_mutation(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    path = root / "uploads.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(version INTEGER NOT NULL)")
        db.execute("INSERT INTO metadata VALUES(1)")
        db.execute("CREATE TABLE valuable(value TEXT)")
        db.execute("INSERT INTO valuable VALUES('keep')")
    before_bytes = path.read_bytes()
    before_entries = {item.name: item.read_bytes() if item.is_file() else None for item in root.iterdir()}

    with pytest.raises(UploadError, match="upload_schema_unsupported"):
        UploadService(root, FakeBackend())

    assert path.read_bytes() == before_bytes
    assert {item.name: item.read_bytes() if item.is_file() else None for item in root.iterdir()} == before_entries


def test_valid_current_schema_and_records_are_opened_without_mutation(tmp_path):
    root = tmp_path / "uploads"
    original = UploadService(root, FakeBackend())
    acct = original.add_account("douyin", "现有账号")
    src = source(original, tmp_path)
    job = original.create_jobs(
        source_id=src["id"], account_ids=[acct["id"]], title="现有草稿",
        description="", tags=[], idempotency_key="existing_schema_one",
    )[0]
    before = {item.relative_to(root).as_posix(): item.read_bytes()
              for item in root.rglob("*") if item.is_file()}

    reader = UploadService(root, FakeBackend())

    after = {item.relative_to(root).as_posix(): item.read_bytes()
             for item in root.rglob("*") if item.is_file()}
    assert after == before
    assert reader.jobs()[0]["id"] == job["id"]
    assert reader.sources()[0]["id"] == src["id"]


def test_valid_current_schema_wal_is_opened_without_creating_a_shm_sidecar(tmp_path):
    source_path = tmp_path / "wal-source.sqlite3"
    target_root = tmp_path / "uploads"
    target_root.mkdir()
    for name in ("media", "assets", "incoming", "private"):
        (target_root / name).mkdir()
    target_path = target_root / "uploads.sqlite3"

    source = sqlite3.connect(source_path)
    try:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.executescript(
            SCHEMA_DDL + f"\nINSERT INTO metadata VALUES({SCHEMA_VERSION});"
        )
        source.commit()
        source_wal = Path(str(source_path) + "-wal")
        assert source_wal.is_file()
        target_path.write_bytes(source_path.read_bytes())
        Path(str(target_path) + "-wal").write_bytes(source_wal.read_bytes())
    finally:
        source.close()

    before = {
        item.relative_to(target_root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in target_root.rglob("*")
    }

    UploadService(target_root, FakeBackend())

    after = {
        item.relative_to(target_root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in target_root.rglob("*")
    }
    assert after == before


def test_malformed_current_schema_wal_is_rejected_without_mutation(tmp_path):
    root = tmp_path / "uploads"
    service = UploadService(root, FakeBackend())
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute(
            "INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)",
            ("a" * 32, "douyin", "WAL account", "now"),
        )
        db.commit()
        wal = Path(str(service.database_path) + "-wal")
        malformed = bytearray(wal.read_bytes())
        malformed[24] ^= 1
    wal.write_bytes(malformed)
    before = {
        item.relative_to(root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in root.rglob("*")
    }

    with pytest.raises(UploadError, match="upload_schema_unsupported"):
        UploadService(root, FakeBackend())

    after = {
        item.relative_to(root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in root.rglob("*")
    }
    assert after == before


def test_current_schema_wal_reset_tail_is_accepted_without_mutation(tmp_path):
    source_root = tmp_path / "wal-reset-source"
    source_service = UploadService(source_root, FakeBackend())
    source_path = source_service.database_path
    target_root = tmp_path / "uploads"
    target_root.mkdir()
    for name in ("media", "assets", "incoming", "private"):
        (target_root / name).mkdir()
    target_path = target_root / "uploads.sqlite3"

    source = sqlite3.connect(source_path)
    try:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("PRAGMA journal_size_limit=-1")
        for index in range(500):
            source.execute(
                "INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)",
                (f"{index:032x}", "douyin", f"reset-{index}", "now"),
            )
        source.commit()
        assert source.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()[0] == 0
        source.execute("DELETE FROM accounts WHERE id=?", (f"{0:032x}",))
        source.commit()
        source_wal = Path(str(source_path) + "-wal")
        wal_bytes = source_wal.read_bytes()
        page_size = int.from_bytes(wal_bytes[8:12], "big")
        frame_size = 24 + page_size
        header_salt = wal_bytes[16:24]
        frame_salts = [
            wal_bytes[offset + 8:offset + 16]
            for offset in range(32, len(wal_bytes) - frame_size + 1, frame_size)
        ]
        assert frame_salts and any(salt != header_salt for salt in frame_salts)
        target_path.write_bytes(source_path.read_bytes())
        Path(str(target_path) + "-wal").write_bytes(wal_bytes)
    finally:
        source.close()

    before = {
        item.relative_to(target_root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in target_root.rglob("*")
    }
    UploadService(target_root, FakeBackend())
    after = {
        item.relative_to(target_root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in target_root.rglob("*")
    }
    assert after == before


def test_current_schema_wal_hardlink_is_rejected_without_mutation(tmp_path):
    root = tmp_path / "uploads"
    service = UploadService(root, FakeBackend())
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute(
            "INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)",
            ("b" * 32, "douyin", "WAL hardlink", "now"),
        )
        db.commit()
        wal = Path(str(service.database_path) + "-wal")
        wal_bytes = wal.read_bytes()
    wal.write_bytes(wal_bytes)
    os.link(wal, tmp_path / "wal-hardlink")
    before = {
        item.relative_to(root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in root.rglob("*")
    }

    with pytest.raises(UploadError, match="upload_schema_unsupported"):
        UploadService(root, FakeBackend())

    after = {
        item.relative_to(root).as_posix(): (
            item.read_bytes() if item.is_file() else None
        )
        for item in root.rglob("*")
    }
    assert after == before


def test_concurrent_new_database_initialization_publishes_only_complete_schema(tmp_path):
    root = tmp_path / "uploads"
    barrier = threading.Barrier(2)
    services, errors = [], []

    def construct():
        try:
            barrier.wait()
            services.append(UploadService(root, FakeBackend()))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=construct) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not errors
    assert len(services) == 2
    with sqlite3.connect(root / "uploads.sqlite3") as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert db.execute("SELECT version FROM metadata").fetchall() == [(SCHEMA_VERSION,)]
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")} == {
            "metadata", "accounts", "sources", "upload_assets", "jobs", "operations",
            "requests",
        }
    assert not any(".tmp" in item.name or item.name.endswith("-journal") for item in root.iterdir())


@pytest.mark.parametrize("damage", ["extra_table", "extra_index", "second_version", "orphan"])
def test_current_schema_drift_and_broken_foreign_keys_are_rejected_read_only(tmp_path, damage):
    root = tmp_path / "uploads"
    service = UploadService(root, FakeBackend())
    path = service.database_path
    with sqlite3.connect(path) as db:
        if damage == "extra_table":
            db.execute("CREATE TABLE unexpected(value TEXT)")
        elif damage == "extra_index":
            db.execute("CREATE INDEX unexpected_index ON jobs(state)")
        elif damage == "second_version":
            db.execute("INSERT INTO metadata VALUES(1)")
        else:
            db.execute(
                "INSERT INTO operations(id,account_id,action,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("f" * 32, "e" * 32, "check", "now", "now"),
            )
    before = path.read_bytes()

    with pytest.raises(UploadError, match="upload_schema_unsupported"):
        UploadService(root, FakeBackend())

    assert path.read_bytes() == before
    assert not Path(str(path) + "-journal").exists()


def test_actionable_jobs_and_sources_are_prioritized_and_all_history_is_pageable(tmp_path):
    service = UploadService(tmp_path / "uploads", FakeBackend())
    acct = service.add_account("bilibili", "容量测试")
    with service._db() as db:
        for index in range(201):
            source_id = f"{index:032x}"
            job_id = f"{index + 1000:032x}"
            state = "running" if index == 0 else "draft"
            db.execute(
                "INSERT INTO sources(id,name,suffix,size,sha256,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, f"source-{index}.mp4", ".mp4", index + 1, "a" * 64, f"time-{index:03d}"),
            )
            db.execute(
                "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,mode,copyright,source_credit,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, acct["id"], source_id, f"job-{index}", "", "[]", 249,
                 "publish", 1, "", state, f"time-{index:03d}", f"time-{index:03d}"),
            )
    oldest_job_id, oldest_source_id = f"{1000:032x}", f"{0:032x}"

    assert any(row["id"] == oldest_job_id for row in service.jobs())
    assert any(row["id"] == oldest_source_id for row in service.sources())
    assert service.job(oldest_job_id)["state"] == "running"
    assert service.source(oldest_source_id)["name"] == "source-0.mp4"

    seen, cursor = [], None
    while True:
        page = service.job_page(cursor=cursor, limit=37)
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 201
    assert set(seen) == {f"{index + 1000:032x}" for index in range(201)}
    assert service.cancel(oldest_job_id)["code"] == "cancellation_requested"


def test_bilibili_requires_explicit_copyright_choice(service, tmp_path):
    with pytest.raises(UploadError, match="bilibili_copyright_required"):
        drafts(service, tmp_path, copyright=None)
    assert service.jobs() == []

from __future__ import annotations

from upload_contract_fixtures import (
    synthetic_receipt_identity, synthetic_upload_result,
    fail_confirmed_upload, reconcile_synthetic_not_accepted,
)

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_download_control.uploads.contracts import BackendResult, UploadError
from video_download_control.uploads import service as upload_service_module
from video_download_control.uploads.service import UploadService


class LifecycleBackend:
    receipt_identity = staticmethod(synthetic_receipt_identity)

    def __init__(self) -> None:
        self.login_entered = threading.Event()
        self.login_release = threading.Event()
        self.credential_written = threading.Event()
        self.disconnects: list[tuple[str, str]] = []
        self.fail_disconnect = False
        self.root: Path | None = None

    def inspect(self):
        return {"ready": True, "code": "ready"}

    def login(self, platform, account_id, stop):
        return BackendResult("ready", "account_ready")

    check = login

    def login_interactive(self, platform, account_id, stop, on_update):
        self.login_entered.set()
        self.login_release.wait(5)
        # Deliberately ignore cancellation and report a late success.
        if self.root is not None:
            credential = self.root / "private" / "accounts" / platform / f"{account_id}.json"
            credential.parent.mkdir(parents=True, exist_ok=True)
            credential.write_text('{"canary":"late-login-secret"}', encoding="utf-8")
            self.credential_written.set()
        return BackendResult("ready", "account_ready")

    def upload(self, request, stop):
        return BackendResult("submitted", "upstream_submitted")

    def disconnect_local(self, platform, account_id):
        self.disconnects.append((platform, account_id))
        if self.fail_disconnect:
            raise OSError("synthetic credential cleanup failure")
        if self.root is not None:
            credential = self.root / "private" / "accounts" / platform / f"{account_id}.json"
            credential.unlink(missing_ok=True)


def _ready_account(service: UploadService, platform: str = "douyin") -> dict:
    account = service.add_account(platform, "生命周期测试")
    with service._db() as db:
        db.execute(
            "UPDATE accounts SET auth_state='ready',code='account_ready' WHERE id=?",
            (account["id"],),
        )
    return service.accounts()[0]


def _source(service: UploadService, tmp_path: Path, payload: bytes = b"video") -> tuple[dict, Path]:
    original = tmp_path / "original.mp4"
    original.write_bytes(payload)
    return service.import_source(original, "original.mp4"), original


def _draft(service: UploadService, account: dict, source: dict) -> dict:
    return service.create_jobs(
        source_id=source["id"],
        account_ids=[account["id"]],
        title="生命周期草稿",
        description="",
        tags=[],
        mode="publish",
        idempotency_key="lifecycle_request",
    )[0]


def test_disconnect_preserves_account_history_and_revokes_queued_confirmation(tmp_path):
    backend = LifecycleBackend()
    service = UploadService(tmp_path / "uploads", backend)
    account = _ready_account(service)
    source, _ = _source(service, tmp_path)
    job = _draft(service, account, source)
    assert service.confirm(job["id"])["state"] == "queued"

    result = service.disconnect_account(account["id"])
    disconnected = result["account"]

    assert disconnected["id"] == account["id"]
    assert disconnected["name"] == "生命周期测试"
    assert disconnected["auth_state"] == "unchecked"
    assert disconnected["lifecycle_state"] == "disconnected"
    assert disconnected["disconnected_at"]
    assert disconnected["code"] == "account_disconnected"
    assert result["revoked_confirmation_count"] == 1
    assert result["canceled_operation_count"] == 0
    assert result["local_login_removed"] is True
    assert service.job(job["id"])["state"] == "draft"
    assert service.job(job["id"])["code"] == "account_disconnected_confirmation_revoked"
    assert backend.disconnects == [("douyin", account["id"])]
    with pytest.raises(UploadError, match="account_disconnected"):
        service.confirm(job["id"])

    repeated = service.disconnect_account(account["id"])
    assert repeated["account"]["disconnected_at"] == disconnected["disconnected_at"]
    assert repeated["revoked_confirmation_count"] == 0
    assert repeated["canceled_operation_count"] == 0
    assert backend.disconnects == [
        ("douyin", account["id"]),
        ("douyin", account["id"]),
    ]


def test_disconnect_cleanup_failure_keeps_tombstone_and_can_be_retried(tmp_path):
    backend = LifecycleBackend()
    service = UploadService(tmp_path / "uploads", backend)
    backend.root = service.root
    account = _ready_account(service)
    credential = service.root / "private" / "accounts" / "douyin" / f"{account['id']}.json"
    credential.parent.mkdir(parents=True)
    credential.write_text('{"canary":"local-secret"}', encoding="utf-8")
    backend.fail_disconnect = True

    with pytest.raises(UploadError, match="^account_disconnect_cleanup_failed$"):
        service.disconnect_account(account["id"])

    failed = service.accounts()[0]
    assert failed["lifecycle_state"] == "disconnected"
    assert failed["auth_state"] == "unchecked"
    assert failed["code"] == "account_disconnect_cleanup_failed"
    assert credential.exists()

    backend.fail_disconnect = False
    result = service.disconnect_account(account["id"])
    assert result["local_login_removed"] is True
    assert result["account"]["code"] == "account_disconnected"
    assert not credential.exists()


def test_disconnect_fences_late_login_success_and_restart_scrubs_credentials(tmp_path):
    backend = LifecycleBackend()
    service = UploadService(tmp_path / "uploads", backend)
    backend.root = service.root
    service.start()
    account = service.add_account("tencent", "扫码账号")
    operation = service.account_action(account["id"], "login")
    assert backend.login_entered.wait(3)

    result = service.disconnect_account(account["id"])
    disconnected = result["account"]
    assert disconnected["lifecycle_state"] == "disconnected"
    assert service.operations()[0]["id"] == operation["id"]
    assert service.operations()[0]["state"] == "canceled"

    backend.login_release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(backend.disconnects) < 2:
        time.sleep(0.01)
    credential = service.root / "private" / "accounts" / "tencent" / f"{account['id']}.json"
    assert backend.credential_written.is_set()
    assert not credential.exists()
    assert backend.disconnects == [
        ("tencent", account["id"]),
        ("tencent", account["id"]),
    ]
    assert service.accounts()[0]["lifecycle_state"] == "disconnected"
    assert service.accounts()[0]["code"] == "account_disconnected"
    service.stop()

    replacement_backend = LifecycleBackend()
    replacement = UploadService(service.root, replacement_backend)
    replacement_backend.root = replacement.root
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text('{"canary":"restart-secret"}', encoding="utf-8")
    replacement.start()
    try:
        assert replacement.accounts()[0]["lifecycle_state"] == "disconnected"
        assert replacement_backend.disconnects == [("tencent", account["id"])]
        assert not credential.exists()
    finally:
        replacement.stop()


def test_running_upload_must_be_resolved_before_account_disconnect(tmp_path):
    backend = LifecycleBackend()
    service = UploadService(tmp_path / "uploads", backend)
    account = _ready_account(service)
    source, _ = _source(service, tmp_path)
    job = _draft(service, account, source)
    with service._db() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))

    with pytest.raises(UploadError, match="account_upload_active"):
        service.disconnect_account(account["id"])
    assert service.accounts()[0]["auth_state"] == "ready"
    assert backend.disconnects == []


def test_media_delete_is_guarded_and_missing_source_must_be_reimported(tmp_path):
    service = UploadService(tmp_path / "uploads", LifecycleBackend())
    account = _ready_account(service)
    source, original = _source(service, tmp_path, b"managed-video")
    job = _draft(service, account, source)

    current = service.source(source["id"])
    assert current["media_present"] is True
    assert current["media_state"] == "present"
    assert current["active_reference_count"] == 1
    assert current["can_delete"] is False
    with pytest.raises(UploadError, match="source_in_use"):
        service.delete_source_media(source["id"])

    fail_confirmed_upload(service, job["id"])
    deleted = service.delete_source_media(source["id"])
    assert deleted["media_present"] is False
    assert deleted["media_state"] == "deleted"
    assert deleted["can_delete"] is False
    historical = service.job(job["id"])
    assert historical["source_media_present"] is False
    assert historical["source_media_state"] == "deleted"
    with pytest.raises(UploadError, match="source_reimport_required"):
        service.retry(job["id"])

    restored = service.restore_source_media(source["id"], original)
    assert restored["id"] == source["id"]
    assert restored["media_present"] is True
    retry = service.retry(job["id"])
    assert retry["state"] == "draft"
    assert retry["source_id"] == source["id"]


def test_media_delete_restores_file_when_database_update_rolls_back(tmp_path):
    service = UploadService(tmp_path / "uploads", LifecycleBackend())
    source, _ = _source(service, tmp_path, b"rollback-video")
    managed = service.root / "media" / f"{source['id']}.mp4"
    with service._db() as db:
        db.executescript(
            """CREATE TRIGGER reject_media_delete
            BEFORE UPDATE OF media_state ON sources
            BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END;"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic rollback"):
        service.delete_source_media(source["id"])

    assert managed.read_bytes() == b"rollback-video"
    assert service.source(source["id"])["media_state"] == "present"
    assert not any(path.name.endswith(".delete") for path in (service.root / "media").iterdir())


def test_storage_usage_separates_registered_missing_and_orphan_files(tmp_path):
    service = UploadService(tmp_path / "uploads", LifecycleBackend())
    first, _ = _source(service, tmp_path, b"12345")
    orphan = service.root / "media" / "orphan.part"
    orphan.write_bytes(b"123")

    usage = service.storage_usage()
    assert usage["registered_source_count"] == 1
    assert usage["present_source_count"] == 1
    assert usage["missing_source_count"] == 0
    assert usage["managed_bytes"] == 5
    assert usage["orphan_file_count"] == 1
    assert usage["orphan_bytes"] == 3
    assert usage["reserve_bytes"] == 64 * 1024**2
    assert usage["free_bytes"] >= 0
    assert isinstance(usage["low_space"], bool)

    service.delete_source_media(first["id"])
    after = service.storage_usage()
    assert after["present_source_count"] == 0
    assert after["missing_source_count"] == 0
    assert after["deleted_source_count"] == 1
    assert after["managed_bytes"] == 0


def test_import_checks_low_space_before_creating_managed_copy(tmp_path, monkeypatch):
    service = UploadService(tmp_path / "uploads", LifecycleBackend())
    original = tmp_path / "space.mp4"
    original.write_bytes(b"12345")
    reserve = 64 * 1024**2
    monkeypatch.setattr(
        upload_service_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=reserve * 2, used=0, free=reserve + 4),
    )

    with pytest.raises(UploadError, match="upload_storage_full"):
        service.import_source(original, "space.mp4")

    assert service.sources() == []
    assert list((service.root / "media").iterdir()) == []
    assert original.read_bytes() == b"12345"

    monkeypatch.setattr(
        upload_service_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=reserve * 2, used=0, free=reserve + 5),
    )
    assert service.import_source(original, "space.mp4")["media_present"] is True

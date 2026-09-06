from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.uploads import service as upload_service
from video_download_control.uploads.activity_lock import (
    UploadActivityBusy,
    activity_lock_path,
    upload_activity_lock,
)
from video_download_control.uploads.contracts import UploadError
from video_download_control.uploads.service import UploadService, default_upload_root


class IdleBackend:
    def inspect(self) -> dict:
        return {"ready": False, "code": "synthetic"}


_CHILD_LOCK = r"""
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
from video_download_control.uploads.activity_lock import UploadActivityBusy, upload_activity_lock
try:
    with upload_activity_lock(pathlib.Path(sys.argv[2]), exclusive=sys.argv[3] == "exclusive"):
        pass
except UploadActivityBusy:
    raise SystemExit(2)
"""


def _other_process_can_lock(root: Path, *, exclusive: bool) -> bool:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _CHILD_LOCK,
            str(Path(__file__).resolve().parents[1] / "src"),
            str(root),
            "exclusive" if exclusive else "shared",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode in {0, 2}, completed.stderr.decode(errors="replace")
    return completed.returncode == 0


def test_cross_process_shared_and_exclusive_activity_semantics(tmp_path):
    root = tmp_path / "uploads"

    with upload_activity_lock(root, exclusive=False):
        assert _other_process_can_lock(root, exclusive=False)
        assert not _other_process_can_lock(root, exclusive=True)

    with upload_activity_lock(root, exclusive=True):
        assert not _other_process_can_lock(root, exclusive=False)
        assert not _other_process_can_lock(root, exclusive=True)

    assert _other_process_can_lock(root, exclusive=True)


@pytest.mark.skipif(os.name != "nt", reason="NTFS 8.3 aliases are Windows-only")
def test_windows_short_and_long_root_names_share_one_activity_lock(tmp_path):
    root = tmp_path / "Long Upload Activity Directory" / "Long Upload Root Name"
    root.mkdir(parents=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path = kernel.GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short_path.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_short_path(str(root), buffer, len(buffer))
    if not length or length >= len(buffer):
        pytest.skip("the test volume does not expose an NTFS 8.3 alias")
    short_root = Path(buffer.value)
    if os.path.normcase(str(short_root)) == os.path.normcase(str(root.resolve())):
        pytest.skip("the test volume does not generate distinct NTFS 8.3 names")

    assert activity_lock_path(short_root) == activity_lock_path(root)
    with upload_activity_lock(short_root, exclusive=False):
        assert not _other_process_can_lock(root, exclusive=True)
    service = UploadService(short_root, IdleBackend())
    assert service.root == root.resolve(strict=True)


def test_activity_lock_rejects_nonempty_and_hardlinked_files(tmp_path):
    nonempty_root = tmp_path / "nonempty"
    nonempty_lock = activity_lock_path(nonempty_root)
    nonempty_lock.write_bytes(b"unexpected")
    with pytest.raises(UploadActivityBusy):
        with upload_activity_lock(nonempty_root, exclusive=True):
            pytest.fail("unsafe nonempty activity lock was accepted")
    assert nonempty_lock.read_bytes() == b"unexpected"

    hardlink_root = tmp_path / "hardlink"
    unrelated = tmp_path / "unrelated.bin"
    unrelated.write_bytes(b"")
    os.link(unrelated, activity_lock_path(hardlink_root))
    with pytest.raises(UploadActivityBusy):
        with upload_activity_lock(hardlink_root, exclusive=False):
            pytest.fail("hardlinked activity lock was accepted")
    assert unrelated.read_bytes() == b""


def test_constructor_is_short_lived_but_started_and_standby_services_hold_shared(tmp_path):
    root = tmp_path / "uploads"
    first = UploadService(root, IdleBackend())
    second = UploadService(root, IdleBackend())
    assert _other_process_can_lock(root, exclusive=True)

    try:
        first.start()
        assert not _other_process_can_lock(root, exclusive=True)
        second.start()
        assert second.status()["scheduler_state"] == "standby"

        first.stop()
        assert not _other_process_can_lock(root, exclusive=True)
        second.stop()
        assert _other_process_can_lock(root, exclusive=True)
    finally:
        first.stop()
        second.stop()


def test_db_context_and_stopped_service_mutation_take_short_shared_lease(tmp_path):
    root = tmp_path / "uploads"
    service = UploadService(root, IdleBackend())
    service.stop()

    with service._db():
        assert not _other_process_can_lock(root, exclusive=True)
    assert _other_process_can_lock(root, exclusive=True)

    with upload_activity_lock(root, exclusive=True):
        with pytest.raises(UploadError, match="upload_activity_busy"):
            UploadService(root, IdleBackend())
        with pytest.raises(UploadError, match="upload_activity_busy"):
            service.add_account("douyin", "blocked")
    assert service.accounts() == []


def test_file_mutator_holds_shared_from_before_copy_until_database_commit(
    tmp_path, monkeypatch
):
    root = tmp_path / "uploads"
    service = UploadService(root, IdleBackend())
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-video")
    entered = threading.Event()
    release = threading.Event()
    original_disk_usage = upload_service.shutil.disk_usage

    def blocked_disk_usage(path):
        entered.set()
        assert release.wait(5)
        return original_disk_usage(path)

    monkeypatch.setattr(upload_service.shutil, "disk_usage", blocked_disk_usage)
    outcome: list[object] = []

    def import_media() -> None:
        try:
            outcome.append(service.import_source(source, "source.mp4"))
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome.append(exc)

    thread = threading.Thread(target=import_media)
    thread.start()
    assert entered.wait(5)
    try:
        assert not _other_process_can_lock(root, exclusive=True)
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], dict), outcome
    assert _other_process_can_lock(root, exclusive=True)


def test_fastapi_lifespan_holds_shared_without_initializing_upload_root(settings):
    app = create_app(settings)
    root = default_upload_root(settings.data_root)
    app.state.upload_service_factory = lambda _root: pytest.fail(
        "upload service was initialized eagerly"
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
        assert app.state.upload_manager.service is None
        assert not root.exists()
        assert _other_process_can_lock(root, exclusive=False)
        assert not _other_process_can_lock(root, exclusive=True)

    assert app.state.upload_manager.service is None
    assert not root.exists()
    assert _other_process_can_lock(root, exclusive=True)


def test_activity_lease_releases_after_exception(tmp_path):
    root = tmp_path / "uploads"
    with pytest.raises(RuntimeError, match="synthetic"):
        with upload_activity_lock(root, exclusive=False):
            raise RuntimeError("synthetic")
    assert _other_process_can_lock(root, exclusive=True)


def test_failed_scheduler_lock_setup_releases_lifetime_activity_lease(tmp_path):
    root = tmp_path / "uploads"
    service = UploadService(root, IdleBackend())
    (root / ".worker.lock").mkdir()

    with pytest.raises(UploadError, match="unsafe_upload_file"):
        service.start()

    assert service.status()["scheduler_state"] == "faulted"
    assert _other_process_can_lock(root, exclusive=True)

from __future__ import annotations

from collections import Counter
import ctypes
from ctypes import wintypes
import gc
import os
from pathlib import Path
import queue
import threading
import time
import tracemalloc

import pytest

from video_download_control.uploads.contracts import BackendResult
from video_download_control.uploads.service import UploadService


POLL_ITERATIONS = 300
POLL_TIMEOUT_SECONDS = 45
MAX_RETAINED_POLL_BYTES = 4 * 1024**2
MAX_TRANSIENT_POLL_BYTES = 16 * 1024**2


class SequencedBackend:
    """A local backend whose upload calls advance only when the test permits."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._release_by_job: dict[str, threading.Event] = {}
        self.entered: queue.Queue = queue.Queue()
        self.calls = []
        self.active = 0
        self.maximum_active = 0

    def inspect(self) -> dict:
        return {"ready": True, "code": "synthetic_ready"}

    def login(self, platform, account_id, stop):
        del platform, account_id, stop
        return BackendResult("ready", "synthetic_ready")

    check = login

    def upload(self, request, stop):
        with self._guard:
            self.calls.append(request)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            release = self._release_by_job.setdefault(request.job_id, threading.Event())
            self.entered.put(request)
        try:
            if not release.wait(5):
                raise TimeoutError("synthetic upload gate timed out")
            if stop.is_set():
                return BackendResult("unknown", "synthetic_stopped")
            return BackendResult("submitted", "synthetic_submitted")
        finally:
            with self._guard:
                self.active -= 1

    def release(self, job_id: str) -> None:
        with self._guard:
            self._release_by_job[job_id].set()

    def release_all(self) -> None:
        with self._guard:
            for release in self._release_by_job.values():
                release.set()

    def snapshot(self) -> tuple[list, int, int]:
        with self._guard:
            return list(self.calls), self.active, self.maximum_active


class PollingBackend:
    def inspect(self) -> dict:
        return {"ready": True, "code": "synthetic_ready"}

    def login(self, platform, account_id, stop):
        del platform, account_id, stop
        return BackendResult("ready", "synthetic_ready")

    check = login

    def upload(self, request, stop):  # pragma: no cover - drafts stay unconfirmed
        del request, stop
        raise AssertionError("polling must not invoke an upload backend")


def _wait_for(predicate, *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("synthetic upload operation exceeded its bounded timeout")


def _mark_accounts_ready(service: UploadService, account_ids: list[str]) -> None:
    placeholders = ",".join("?" for _ in account_ids)
    with service._db() as database:
        database.execute(
            f"UPDATE accounts SET auth_state='ready',code='synthetic_ready' "
            f"WHERE id IN ({placeholders})",
            account_ids,
        )


def _import_source(service: UploadService, tmp_path: Path, payload: bytes = b"video") -> dict:
    original = tmp_path / "synthetic.mp4"
    original.write_bytes(payload)
    return service.import_source(original, "synthetic.mp4")


def _resource_count() -> tuple[str, int] | None:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(), ctypes.byref(count)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return "handles", int(count.value)

    for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
        if directory.is_dir():
            return "fds", len(os.listdir(directory))
    return None


def _transient_files(root: Path) -> set[str]:
    incoming = root / "incoming"
    result = {
        path.relative_to(root).as_posix()
        for path in incoming.rglob("*")
        if path.is_file()
    }
    media = root / "media"
    result.update(
        path.relative_to(root).as_posix()
        for path in media.rglob("*")
        if path.is_file()
        and (
            path.name.startswith(".")
            or path.name.endswith((".tmp", ".part", ".restore", ".delete"))
        )
    )
    return result


def _poll_local_views(service: UploadService) -> None:
    status = service.status()
    accounts = service.accounts()
    jobs = service.jobs()
    sources = service.sources()
    storage = service.storage_usage()
    assert status["backend"]["ready"] is True
    assert len(accounts) == len(jobs) == len(sources) == 1
    assert storage["registered_source_count"] == 1
    assert storage["present_source_count"] == 1


def test_confirmed_multi_account_uploads_are_strictly_serial_and_service_remains_usable(
    tmp_path,
):
    backend = SequencedBackend()
    service = UploadService(tmp_path / "uploads", backend)
    service.start()
    try:
        accounts = [
            service.add_account(platform, f"synthetic-{platform}")
            for platform in ("bilibili", "douyin", "tencent")
        ]
        _mark_accounts_ready(service, [account["id"] for account in accounts])
        source = _import_source(service, tmp_path, b"serial-video")
        jobs = service.create_jobs(
            source_id=source["id"],
            account_ids=[account["id"] for account in accounts],
            title="串行验证",
            description="synthetic only",
            tags=["synthetic"],
            category_id=249,
            copyright=1,
            mode="publish",
            idempotency_key="resilience_serial_batch",
        )
        for job in jobs:
            assert service.confirm(job["id"])["state"] in {"queued", "running"}

        observed_job_ids: list[str] = []
        for expected_call_count in range(1, len(jobs) + 1):
            request = backend.entered.get(timeout=5)
            observed_job_ids.append(request.job_id)
            calls, active, maximum_active = backend.snapshot()
            assert len(calls) == expected_call_count
            assert active == 1
            assert maximum_active == 1
            backend.release(request.job_id)

        _wait_for(
            lambda: all(
                row["state"] == "submitted"
                for row in service.jobs_by_ids([job["id"] for job in jobs])
            )
        )
        calls, active, maximum_active = backend.snapshot()
        assert active == 0
        assert maximum_active == 1
        assert Counter(request.job_id for request in calls) == Counter(
            {job["id"]: 1 for job in jobs}
        )
        assert Counter(request.account_id for request in calls) == Counter(
            {account["id"]: 1 for account in accounts}
        )
        assert Counter(request.platform for request in calls) == Counter(
            {"bilibili": 1, "douyin": 1, "tencent": 1}
        )
        assert set(observed_job_ids) == {job["id"] for job in jobs}

        local_draft = service.create_jobs(
            source_id=source["id"],
            account_ids=[accounts[1]["id"]],
            title="后续本地草稿",
            description="",
            tags=[],
            mode="publish",
            idempotency_key="resilience_post_upload_draft",
        )[0]
        assert local_draft["state"] == "draft"
        assert service.cancel(local_draft["id"])["state"] == "canceled"
        assert len(backend.snapshot()[0]) == 3
    finally:
        backend.release_all()
        service.stop()


def test_repeated_local_polling_stays_within_resource_and_memory_budgets(tmp_path):
    service = UploadService(tmp_path / "uploads", PollingBackend())
    account = service.add_account("douyin", "synthetic-polling")
    source = _import_source(service, tmp_path, b"polling-video")
    service.create_jobs(
        source_id=source["id"],
        account_ids=[account["id"]],
        title="轮询草稿",
        description="",
        tags=[],
        mode="publish",
        idempotency_key="resilience_polling_draft",
    )

    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start(1)
    try:
        for _ in range(20):
            _poll_local_views(service)
        gc.collect()
        _resource_count()  # Warm up the platform-specific counter itself.
        gc.collect()

        threads_before = {
            (thread.ident, thread.name)
            for thread in threading.enumerate()
            if thread.is_alive()
        }
        upload_threads_before = {
            item for item in threads_before if item[1].startswith("open-flame-upload")
        }
        resources_before = _resource_count()
        transient_before = _transient_files(service.root)
        memory_before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

        started_at = time.monotonic()
        deadline = started_at + POLL_TIMEOUT_SECONDS
        for _ in range(POLL_ITERATIONS):
            if time.monotonic() >= deadline:
                pytest.fail(
                    f"{POLL_ITERATIONS} polling rounds exceeded "
                    f"{POLL_TIMEOUT_SECONDS} seconds"
                )
            _poll_local_views(service)
        elapsed = time.monotonic() - started_at

        gc.collect()
        memory_after, memory_peak = tracemalloc.get_traced_memory()
        resources_after = _resource_count()
        threads_after = {
            (thread.ident, thread.name)
            for thread in threading.enumerate()
            if thread.is_alive()
        }
        upload_threads_after = {
            item for item in threads_after if item[1].startswith("open-flame-upload")
        }
        retained_bytes = max(0, memory_after - memory_before)
        transient_bytes = max(0, memory_peak - memory_before)

        assert elapsed < POLL_TIMEOUT_SECONDS
        assert threads_after == threads_before
        assert upload_threads_after == upload_threads_before
        assert (resources_before is None) == (resources_after is None)
        if resources_before is not None and resources_after is not None:
            assert resources_after[0] == resources_before[0]
            assert resources_after[1] <= resources_before[1]
        assert _transient_files(service.root) == transient_before == set()
        assert retained_bytes <= MAX_RETAINED_POLL_BYTES
        assert transient_bytes <= MAX_TRANSIENT_POLL_BYTES

        if os.environ.get("OPEN_FLAME_RESILIENCE_METRICS") == "1":
            resource_text = (
                "unavailable"
                if resources_before is None or resources_after is None
                else f"{resources_before[0]}={resources_before[1]}->{resources_after[1]}"
            )
            print(
                "upload-poll-metrics "
                f"rounds={POLL_ITERATIONS} elapsed={elapsed:.3f}s "
                f"threads={len(threads_before)}->{len(threads_after)} "
                f"{resource_text} retained={retained_bytes} peak_delta={transient_bytes}"
            )
    finally:
        if started_tracing:
            tracemalloc.stop()


def test_interrupted_media_copy_removes_unregistered_partial(tmp_path, monkeypatch):
    service = UploadService(tmp_path / "uploads", PollingBackend())
    original = tmp_path / "interrupted.mp4"
    original.write_bytes(b"a" * (2 * 1024 * 1024 + 1))
    original_open = Path.open

    class SyntheticCopyInterrupted(OSError):
        pass

    class InterruptingReader:
        def __init__(self, handle) -> None:
            self.handle = handle
            self.read_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self.handle.__exit__(exc_type, exc, traceback)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            self.read_count += 1
            if self.read_count == 2:
                raise SyntheticCopyInterrupted("synthetic interrupted media copy")
            return self.handle.read(size)

    def interrupted_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == original and mode == "rb":
            return InterruptingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", interrupted_open)

    with pytest.raises(SyntheticCopyInterrupted, match="synthetic interrupted"):
        service.import_source(original, "interrupted.mp4")

    assert service.sources() == []
    assert list((service.root / "media").iterdir()) == []
    assert list((service.root / "incoming").iterdir()) == []
    assert _transient_files(service.root) == set()
    assert original.stat().st_size == 2 * 1024 * 1024 + 1

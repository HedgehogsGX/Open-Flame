from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import video_download_control.local_worker_cli as local_worker_module
from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.assets import (
    AssetStore,
    AssetValidationError,
    NonEmptyTestVerifier,
)
from video_download_control.domain import ErrorCode
from video_download_control.local_worker_cli import (
    FEATURE_GATE,
    LocalWorkerConfig,
    build_local_worker,
)
from video_download_control.runtime_logging import RuntimeLogConfig, RuntimeLogger
from video_download_control.subprocess_runner import SecureSubprocessRunner
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


class RecordingAdapter(ScriptedFakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.probe_calls = 0
        self.download_calls = 0

    def probe(self, request, context):
        self.probe_calls += 1
        return super().probe(request, context)

    def download(self, request, context, progress, cancellation):
        self.download_calls += 1
        return super().download(request, context, progress, cancellation)


class RecordingAssetStore(AssetStore):
    def __init__(self, data_root: Path) -> None:
        super().__init__(data_root)
        self.prepare_calls = 0

    def prepare_attempt(self, job_id, attempt_id):
        self.prepare_calls += 1
        return super().prepare_attempt(job_id, attempt_id)


class BlockingInterruptedVerifier(NonEmptyTestVerifier):
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.staged_path: Path | None = None

    def verify(self, path, media_kind):
        self.staged_path = path
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("test verifier was not released")
        # FfprobeVerifier wraps CommandCancelled in AssetValidationError. This
        # reproduces that public verifier seam without invoking media tools.
        raise AssetValidationError("media verifier subprocess was interrupted")


def make_worker(settings, database, *, verifier=None):
    adapter = RecordingAdapter()
    store = RecordingAssetStore(settings.data_root)
    repository = WorkerRepository(database)
    stop_event = Event()
    worker = Worker(
        worker_id="stop-regression-worker",
        repository=repository,
        adapter=adapter,
        asset_store=store,
        verifier=verifier or NonEmptyTestVerifier(),
        clock=lambda: NOW,
        stop_event=stop_event,
    )
    return worker, adapter, store, repository, stop_event


def assert_finalized(repository, job_id, *, canceled=False):
    job = repository.get_job(job_id)
    assert job is not None
    assert job["status"] == ("canceled" if canceled else "failed")
    assert job["final_error_code"] == (None if canceled else "worker_lost")
    for field in ("lease_owner", "lease_token", "heartbeat_at", "lease_expires_at"):
        assert job[field] is None
    assert job["attempt_count"] == 1
    attempts = repository.attempts_for(job_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == ("canceled" if canceled else "failed")
    assert attempts[0]["error_code"] == (None if canceled else "worker_lost")
    assert attempts[0]["finished_at"] is not None


def assert_no_media_artifacts(store, database):
    for directory in (store.temporary_root, store.assets_root):
        assert not directory.exists() or not any(
            path.is_file() for path in directory.rglob("*")
        )
    assert not store.staging_root.exists() or not any(store.staging_root.iterdir())
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM asset_commit_intents").fetchone()[0]
            == 0
        )


def test_claimed_then_stopped_finalizes_without_adapter_or_temporary_work(
    service, settings, database,
) -> None:
    batch = service.create_batch(
        name="claimed stop",
        raw_inputs=[
            "https://www.youtube.com/watch?v=stop-claimed",
            "https://www.youtube.com/watch?v=stop-queued",
        ],
    )
    worker, adapter, store, repository, stop_event = make_worker(settings, database)
    claim = worker.claim_once()
    assert claim is not None
    remaining_job_ids = {job["id"] for job in batch["jobs"]} - {claim.lease.job_id}
    assert len(remaining_job_ids) == 1

    worker.request_stop()
    worker.request_stop()
    result = worker.execute_claimed(claim)

    assert worker.stop_event is stop_event
    assert stop_event.is_set()
    assert result.status == "failed"
    assert result.error_code is ErrorCode.WORKER_LOST
    assert adapter.probe_calls == adapter.download_calls == store.prepare_calls == 0
    assert not store.temporary_root.exists()
    assert_finalized(repository, claim.lease.job_id)
    assert worker.claim_once() is None
    assert worker.run_once() is None
    queued = repository.get_job(remaining_job_ids.pop())
    assert queued["status"] == "queued" and queued["attempt_count"] == 0
    assert_no_media_artifacts(store, database)


@pytest.mark.parametrize("cancel_order", [None, "before_stop", "after_stop"])
def test_stop_during_verification_cleans_artifacts_and_user_cancel_wins(
    service, settings, database, cancel_order,
) -> None:
    batch = service.create_batch(
        name="verification stop",
        raw_inputs=["https://www.youtube.com/watch?v=stop-verifying"],
    )
    job_id = batch["jobs"][0]["id"]
    verifier = BlockingInterruptedVerifier()
    worker, adapter, store, repository, stop_event = make_worker(
        settings, database, verifier=verifier,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.run_once)
        try:
            assert verifier.entered.wait(timeout=5)
            assert verifier.staged_path is not None and verifier.staged_path.is_file()
            assert verifier.staged_path.is_relative_to(store.staging_root)
            assert repository.get_job(job_id)["status"] == "verifying"
            if cancel_order == "before_stop":
                repository.request_cancel(job_id, now=NOW)
            worker.request_stop()
            if cancel_order == "after_stop":
                repository.request_cancel(job_id, now=NOW)
        finally:
            verifier.release.set()
        result = future.result(timeout=5)

    canceled = cancel_order is not None
    assert result is not None
    assert result.status == ("canceled" if canceled else "failed")
    assert result.error_code == (None if canceled else ErrorCode.WORKER_LOST)
    assert adapter.probe_calls == adapter.download_calls == store.prepare_calls == 1
    assert stop_event.is_set()
    assert worker.claim_once() is None
    assert_finalized(repository, job_id, canceled=canceled)
    assert_no_media_artifacts(store, database)


def test_local_builder_shares_actual_runner_stop_event_with_adapter_and_verifier(
    tmp_path, settings, database, monkeypatch,
) -> None:
    monkeypatch.setenv(FEATURE_GATE, "1")
    monkeypatch.setattr(local_worker_module.sys, "platform", "win32")
    tool_root = (tmp_path / "tools").resolve()
    entrypoint = tool_root / "yt-dlp" / "yt-dlp"
    ffmpeg_root = tool_root / "ffmpeg" / "bin"
    entrypoint.parent.mkdir(parents=True)
    ffmpeg_root.mkdir(parents=True)
    entrypoint.write_bytes(b"synthetic tool placeholder")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (ffmpeg_root / name).write_bytes(b"synthetic tool placeholder")
    verified = SimpleNamespace(
        yt_dlp_version="2026.08.19",
        ffmpeg_version="n9-test",
        ffprobe_version="n9-test",
        offline_smoke_passed=True,
    )
    monkeypatch.setattr(
        local_worker_module, "verify_toolchain", lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        local_worker_module,
        "load_toolchain_lock",
        lambda: SimpleNamespace(yt_dlp=SimpleNamespace(entrypoint="yt-dlp/yt-dlp")),
    )
    monkeypatch.setattr(local_worker_module.FfprobeVerifier, "validate_runtime", lambda self: None)
    stop_event = Event()
    runner = SecureSubprocessRunner(
        allowed_executable_roots=(Path(sys.executable).resolve().parent, ffmpeg_root),
        stop_event=stop_event,
    )
    logger = RuntimeLogger(
        component="local-worker",
        config=RuntimeLogConfig(directory=settings.data_root / "logs"),
        instance_id="shared-stop-builder",
    )
    config = LocalWorkerConfig(
        data_root=settings.data_root.resolve(),
        database_path=database.path.resolve(),
        tool_root=tool_root,
        worker_id="shared-stop-builder",
        allow_direct_network=True,
    )

    worker = build_local_worker(config, runner=runner, runtime_logger=logger)

    assert worker.stop_event is runner.stop_event is stop_event
    assert worker.adapter._runner is runner
    assert worker.verifier.runner is runner
    worker.request_stop()
    assert runner.stop_event.is_set()
    assert worker.claim_once() is None

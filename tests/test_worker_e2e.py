from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

from video_download_control.adapters import (
    AdapterFailure,
    DownloadResult,
    ProducedFile,
    ScriptedFakeAdapter,
)
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.domain import ErrorCode
from video_download_control.runtime_logging import RuntimeLogConfig, RuntimeLogger
from video_download_control.service import BatchService
from video_download_control.worker import Worker
from video_download_control.worker_repository import (
    JobLease,
    LostLease,
    WorkerRepository,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class BlockingProbeAdapter(ScriptedFakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def probe(self, request, context):
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test probe was not released")
        return super().probe(request, context)


class SignalingWorkerRepository(WorkerRepository):
    def __init__(self, database, *, signal_at: datetime) -> None:
        super().__init__(database)
        self.signal_at = signal_at
        self.renewed = Event()

    def heartbeat(
        self,
        lease: JobLease,
        *,
        now: datetime,
        lease_seconds: int = 60,
    ) -> bool:
        canceled = super().heartbeat(
            lease, now=now, lease_seconds=lease_seconds
        )
        if now >= self.signal_at:
            self.renewed.set()
        return canceled


class FailingPrepareAssetStore(AssetStore):
    def prepare_attempt(self, job_id: str, attempt_id: str):
        del job_id, attempt_id
        raise OSError(28, "simulated storage exhaustion")


class MissingProducedFileAdapter(ScriptedFakeAdapter):
    def download(self, request, context, progress, cancellation):
        del context, progress, cancellation
        return DownloadResult(
            files=(
                ProducedFile(
                    path=request.output_dir / "missing.fake",
                    media_key="missing-output",
                    media_kind="video",
                    role="original",
                    ordinal=0,
                ),
            )
        )


class RejectingCommitRepository(WorkerRepository):
    def finalize_asset_commit_intents(self, lease, **kwargs):
        del kwargs
        raise LostLease(lease.job_id)


class FailingCommitRepository(WorkerRepository):
    @staticmethod
    def _insert_asset_records_locked(*_args, **_kwargs):
        raise RuntimeError("simulated database commit failure")


class SensitiveHeartbeatFailure(RuntimeError):
    pass


class FailingHeartbeatRepository(WorkerRepository):
    def __init__(self, database) -> None:
        super().__init__(database)
        self.failed = Event()

    def heartbeat(self, lease, *, now, lease_seconds=60):
        del lease, now, lease_seconds
        self.failed.set()
        raise SensitiveHeartbeatFailure("heartbeat-secret-marker")


class FailingCleanupAssetStore(AssetStore):
    def cleanup_attempt(self, attempt) -> None:
        del attempt
        raise PermissionError("cleanup-secret-marker")


class CancelDuringVerification(NonEmptyTestVerifier):
    def __init__(
        self,
        repository: WorkerRepository,
        job_id: str,
        clock: MutableClock,
    ) -> None:
        self.repository = repository
        self.job_id = job_id
        self.clock = clock

    def verify(self, path, media_kind):
        self.repository.request_cancel(self.job_id, now=self.clock())
        return super().verify(path, media_kind)


def build_worker(
    settings,
    database,
    adapter,
    clock: MutableClock,
    *,
    runtime_logger: RuntimeLogger | None = None,
) -> Worker:
    return Worker(
        worker_id="offline-test-worker",
        repository=WorkerRepository(database),
        adapter=adapter,
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=clock,
        runtime_logger=runtime_logger,
    )


def emitted_events(runtime_logger: RuntimeLogger) -> list[dict[str, object]]:
    return [
        event
        for line in runtime_logger.path.read_text("utf-8").splitlines()
        if (event := json.loads(line))["run_id"] == runtime_logger.run_id
    ]


def test_offline_fake_adapter_runs_probe_to_immutable_asset(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="offline e2e",
        raw_inputs=["https://www.youtube.com/watch?v=offline-e2e"],
    )
    job_id = batch["jobs"][0]["id"]
    clock = MutableClock()
    runtime_logger = RuntimeLogger(
        component="offline-worker",
        config=RuntimeLogConfig(directory=settings.data_root / "logs"),
        instance_id="offline-test-worker",
    )
    worker = build_worker(
        settings,
        database,
        ScriptedFakeAdapter(),
        clock,
        runtime_logger=runtime_logger,
    )

    result = worker.run_once()

    assert result and result.status == "ready"
    repository = WorkerRepository(database)
    job = repository.get_job(job_id)
    attempts = repository.attempts_for(job_id)
    assert job["status"] == "ready"
    assert job["progress"] == 1
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["discovered_item_count"] == 1
    assert attempts[0]["discovery_snapshot_hash"]
    refreshed_batch = service.get_batch(batch["id"])
    assert refreshed_batch["status"] == "ready"
    assert refreshed_batch["ready_count"] == 1
    assert refreshed_batch["queued_count"] == 0
    assert refreshed_batch["inputs"][0]["status"] == "ready"

    with database.connect() as connection:
        asset = connection.execute("SELECT * FROM media_assets").fetchone()
        artifacts = connection.execute(
            "SELECT kind, path, sha256 FROM artifacts ORDER BY kind"
        ).fetchall()
        input_record = connection.execute(
            "SELECT expected_item_count FROM input_records"
        ).fetchone()
    assert asset["status"] == "ready"
    assert asset["sha256"]
    assert input_record["expected_item_count"] == 1
    assert {row["kind"] for row in artifacts} == {"original", "manifest"}

    manifest_row = next(row for row in artifacts if row["kind"] == "manifest")
    manifest_path = settings.data_root / manifest_row["path"]
    source_path = manifest_path.parent / "source.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    source = json.loads(source_path.read_text("utf-8"))
    assert manifest["producer"]["adapter"] == "scripted_fake"
    assert "authorization" not in source

    events = emitted_events(runtime_logger)
    assert [event["event"] for event in events] == [
        "worker.job_claimed",
        "worker.job_phase",
        "worker.job_phase",
        "worker.job_phase",
        "worker.job_phase",
        "worker.job_phase",
        "worker.job_finished",
    ]
    assert [
        event["phase"]
        for event in events
        if event["event"] == "worker.job_phase"
    ] == ["preparing", "probing", "downloading", "verifying", "committing"]
    claimed = events[0]
    finished = events[-1]
    assert claimed["job_id"] == finished["job_id"] == job_id
    assert claimed["attempt_id"] == finished["attempt_id"] == result.attempt_id
    assert finished["result_status"] == "ready"
    assert finished["asset_count"] == 1
    serialized_log = runtime_logger.path.read_text("utf-8")
    assert "https://" not in serialized_log
    assert "authorization" not in serialized_log
    assert "offline-e2e" not in serialized_log


def test_transient_fake_failure_respects_backoff_then_succeeds(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="retry",
        raw_inputs=["https://www.youtube.com/watch?v=offline-retry"],
    )
    job_id = batch["jobs"][0]["id"]
    adapter = ScriptedFakeAdapter(
        probe_failures=[
            AdapterFailure(ErrorCode.NETWORK_ERROR, "simulated timeout")
        ]
    )
    clock = MutableClock()
    runtime_logger = RuntimeLogger(
        component="offline-worker",
        config=RuntimeLogConfig(directory=settings.data_root / "logs"),
        instance_id="offline-test-worker",
    )
    worker = build_worker(
        settings,
        database,
        adapter,
        clock,
        runtime_logger=runtime_logger,
    )

    failed = worker.run_once()
    assert failed and failed.status == "queued"
    assert worker.run_once() is None
    clock.advance(5)
    succeeded = worker.run_once()

    assert succeeded and succeeded.status == "ready"
    repository = WorkerRepository(database)
    assert repository.get_job(job_id)["attempt_count"] == 2
    assert [row["status"] for row in repository.attempts_for(job_id)] == [
        "failed",
        "succeeded",
    ]
    events = emitted_events(runtime_logger)
    retry_event = next(
        event for event in events if event["event"] == "worker.retry_decided"
    )
    assert retry_event["error_code"] == "network_error"
    assert retry_event["retry_action"] == "retry"
    assert retry_event["retry_delay_ms"] == 5000
    assert [
        event["result_status"]
        for event in events
        if event["event"] == "worker.job_finished"
    ] == ["queued", "ready"]
    assert "simulated timeout" not in runtime_logger.path.read_text("utf-8")


def test_empty_fake_payload_retries_once_then_fails_validation(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="invalid",
        raw_inputs=["https://www.youtube.com/watch?v=offline-empty"],
    )
    job_id = batch["jobs"][0]["id"]
    clock = MutableClock()
    worker = build_worker(
        settings, database, ScriptedFakeAdapter(payload=b""), clock
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first and first.status == "queued"
    assert second and second.status == "failed"
    job = WorkerRepository(database).get_job(job_id)
    assert job["final_error_code"] == "validation_failed"
    assert job["attempt_count"] == 2
    refreshed_batch = service.get_batch(batch["id"])
    assert refreshed_batch["status"] == "failed"
    assert refreshed_batch["failed_count"] == 1
    assert refreshed_batch["inputs"][0]["error_code"] == "validation_failed"


def test_worker_renews_lease_independently_during_blocking_probe(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="long probe",
        raw_inputs=["https://www.youtube.com/watch?v=long-probe"],
    )
    job_id = batch["jobs"][0]["id"]
    clock = MutableClock()
    repository = SignalingWorkerRepository(
        database, signal_at=clock.value + timedelta(seconds=9)
    )
    adapter = BlockingProbeAdapter()
    worker = Worker(
        worker_id="heartbeat-worker",
        repository=repository,
        adapter=adapter,
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=clock,
        lease_seconds=10,
        heartbeat_interval_seconds=0.01,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.run_once)
        try:
            assert adapter.entered.wait(timeout=2)
            clock.advance(9)
            assert repository.renewed.wait(timeout=2)
            renewed = repository.get_job(job_id)
            assert renewed["heartbeat_at"] == "2026-09-03T01:00:09.000Z"
            assert renewed["lease_expires_at"] == "2026-09-03T01:00:19.000Z"
            clock.advance(2)
            assert (
                WorkerRepository(database).claim_next(
                    worker_id="competitor",
                    adapter="fake",
                    adapter_version="1",
                    now=clock(),
                    lease_seconds=10,
                )
                is None
            )
        finally:
            adapter.release.set()
        result = future.result(timeout=5)

    assert result and result.status == "ready"
    assert repository.get_job(job_id)["attempt_count"] == 1


def test_heartbeat_failure_is_logged_once_without_exception_text(
    service: BatchService, settings, database
) -> None:
    service.create_batch(
        name="heartbeat failure",
        raw_inputs=["https://www.youtube.com/watch?v=heartbeat-failure"],
    )
    clock = MutableClock()
    repository = FailingHeartbeatRepository(database)
    adapter = BlockingProbeAdapter()
    runtime_logger = RuntimeLogger(
        component="offline-worker",
        config=RuntimeLogConfig(directory=settings.data_root / "logs"),
        instance_id="heartbeat-failure-worker",
    )
    worker = Worker(
        worker_id="heartbeat-failure-worker",
        repository=repository,
        adapter=adapter,
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=clock,
        lease_seconds=10,
        heartbeat_interval_seconds=0.01,
        runtime_logger=runtime_logger,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.run_once)
        try:
            assert adapter.entered.wait(timeout=2)
            assert repository.failed.wait(timeout=2)
        finally:
            adapter.release.set()
        result = future.result(timeout=5)

    assert result and result.error_code == ErrorCode.WORKER_INTERNAL
    events = emitted_events(runtime_logger)
    heartbeat_events = [
        event for event in events if event["event"] == "worker.heartbeat_failed"
    ]
    assert len(heartbeat_events) == 1
    assert heartbeat_events[0]["exception_type"] == "RuntimeError"
    serialized_log = runtime_logger.path.read_text("utf-8")
    assert "heartbeat-secret-marker" not in serialized_log
    assert "SensitiveHeartbeatFailure" not in serialized_log


def test_cleanup_failure_logs_operation_and_pauses_queue_without_path_text(
    service: BatchService, settings, database
) -> None:
    service.create_batch(
        name="cleanup failure",
        raw_inputs=["https://www.youtube.com/watch?v=cleanup-failure"],
    )
    repository = WorkerRepository(database)
    runtime_logger = RuntimeLogger(
        component="offline-worker",
        config=RuntimeLogConfig(directory=settings.data_root / "logs"),
        instance_id="cleanup-failure-worker",
    )
    worker = Worker(
        worker_id="cleanup-failure-worker",
        repository=repository,
        adapter=ScriptedFakeAdapter(),
        asset_store=FailingCleanupAssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=MutableClock(),
        runtime_logger=runtime_logger,
    )

    result = worker.run_once()

    assert result and result.status == "ready"
    assert worker.paused is True
    assert repository.get_queue_control()["paused"] is True
    events = emitted_events(runtime_logger)
    cleanup_event = next(
        event for event in events if event["event"] == "worker.cleanup_failed"
    )
    assert cleanup_event["cleanup_operation"] == "attempt_directory"
    assert cleanup_event["failure_site"] == "attempt_directory"
    assert cleanup_event["exception_type"] == "OSError"
    assert any(event["event"] == "worker.queue_paused" for event in events)
    serialized_log = runtime_logger.path.read_text("utf-8")
    assert "cleanup-secret-marker" not in serialized_log
    assert "PermissionError" not in serialized_log


def test_storage_oserror_pauses_worker_before_claiming_another_platform(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="storage fail closed",
        raw_inputs=[
            "https://www.youtube.com/watch?v=storage-first",
            "https://x.com/example/status/900003",
        ],
    )
    repository = WorkerRepository(database)
    clock = MutableClock()
    worker = Worker(
        worker_id="storage-worker",
        repository=repository,
        adapter=ScriptedFakeAdapter(),
        asset_store=FailingPrepareAssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=clock,
    )

    failed = worker.run_once()
    second_run = worker.run_once()

    assert failed and failed.status == "failed"
    assert failed.error_code == ErrorCode.STORAGE_ERROR
    assert worker.paused is True
    assert worker.pause_error_code == ErrorCode.STORAGE_ERROR
    assert second_run is None
    assert repository.get_queue_control()["reason"] == "storage_error"
    restarted_worker = Worker(
        worker_id="restarted-storage-worker",
        repository=WorkerRepository(database),
        adapter=ScriptedFakeAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=clock,
    )
    assert restarted_worker.run_once() is None
    failed_job = repository.get_job(failed.job_id)
    assert failed_job["final_error_code"] == "storage_error"
    remaining_job_id = next(
        job["id"] for job in batch["jobs"] if job["id"] != failed.job_id
    )
    assert repository.get_job(remaining_job_id)["status"] == "queued"
    assert repository.attempts_for(remaining_job_id) == []
    repository.resume_queue(now=clock())
    resumed = restarted_worker.run_once()
    assert resumed and resumed.job_id == remaining_job_id
    assert resumed.status == "ready"


def test_missing_adapter_output_is_validation_failure_without_pausing_queue(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="missing adapter output",
        raw_inputs=["https://www.youtube.com/watch?v=missing-output"],
    )
    repository = WorkerRepository(database)
    worker = Worker(
        worker_id="missing-output-worker",
        repository=repository,
        adapter=MissingProducedFileAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=MutableClock(),
    )

    result = worker.run_once()

    assert result and result.error_code == ErrorCode.VALIDATION_FAILED
    assert repository.get_job(batch["jobs"][0]["id"])["status"] == "queued"
    assert repository.get_queue_control()["paused"] is False
    assert worker.paused is False


def test_lost_lease_after_intent_leaves_staging_for_recovery(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="commit compensation",
        raw_inputs=["https://www.youtube.com/watch?v=commit-rollback"],
    )
    repository = RejectingCommitRepository(database)
    clock = MutableClock()
    worker = Worker(
        worker_id="rollback-worker",
        repository=repository,
        adapter=ScriptedFakeAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=clock,
    )

    result = worker.run_once()

    assert result and result.status == "lost_lease"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 1
    assets_root = settings.data_root / "assets"
    final_assets = (
        [path for path in assets_root.iterdir() if path.name != ".staging"]
        if assets_root.exists()
        else []
    )
    assert final_assets == []
    temporary_root = settings.data_root / "temporary"
    assert not temporary_root.exists() or not any(temporary_root.rglob("*"))
    assert repository.get_job(batch["jobs"][0]["id"])["status"] == "verifying"

    assert repository.recover_asset_commit_intents(
        now=clock() + timedelta(seconds=60),
        remove_pending_asset=worker.asset_store.remove_pending_asset,
    ) == 1
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 0


def test_database_failure_after_publish_leaves_intent_for_recovery(
    service: BatchService, settings, database
) -> None:
    service.create_batch(
        name="database compensation",
        raw_inputs=["https://www.youtube.com/watch?v=database-rollback"],
    )
    repository = FailingCommitRepository(database)
    clock = MutableClock()
    worker = Worker(
        worker_id="database-failure-worker",
        repository=repository,
        adapter=ScriptedFakeAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=clock,
    )

    result = worker.run_once()

    assert result and result.status == "failed"
    assert result.error_code == ErrorCode.WORKER_INTERNAL
    assert repository.get_job(result.job_id)["final_error_code"] == "worker_internal"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 1
    assets_root = settings.data_root / "assets"
    published = [
        path for path in assets_root.iterdir() if path.name != ".staging"
    ]
    assert len(published) == 1
    temporary_root = settings.data_root / "temporary"
    assert not temporary_root.exists() or not any(temporary_root.rglob("*"))

    assert repository.recover_asset_commit_intents(
        now=clock() + timedelta(seconds=60),
        remove_pending_asset=worker.asset_store.remove_pending_asset,
    ) == 1
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 0
    assert not any(path for path in assets_root.iterdir() if path.name != ".staging")


def test_cancel_during_verification_rolls_back_unregistered_asset(
    service: BatchService, settings, database
) -> None:
    batch = service.create_batch(
        name="cancel verification",
        raw_inputs=["https://www.youtube.com/watch?v=cancel-verify"],
    )
    job_id = batch["jobs"][0]["id"]
    repository = WorkerRepository(database)
    clock = MutableClock()
    worker = Worker(
        worker_id="cancel-worker",
        repository=repository,
        adapter=ScriptedFakeAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=CancelDuringVerification(repository, job_id, clock),
        clock=clock,
    )

    result = worker.run_once()

    assert result and result.status == "canceled"
    assert repository.get_job(job_id)["status"] == "canceled"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    assets_root = settings.data_root / "assets"
    assert not assets_root.exists() or not any(
        path for path in assets_root.iterdir() if path.name != ".staging"
    )
    temporary_root = settings.data_root / "temporary"
    assert not temporary_root.exists() or not any(temporary_root.rglob("*"))

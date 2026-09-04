from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread

import pytest

from video_download_control.adapters import AdapterJobKind, AdapterRoute
from video_download_control.assets import CommittedAsset, VerificationResult
from video_download_control.domain import ErrorCode, JobStatus, Platform, SourceType
from video_download_control.service import BatchService
from video_download_control.worker_repository import (
    InvalidTransition,
    JobLease,
    LostLease,
    RetryConflict,
    WorkerRepository,
)


NOW = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
CLAIM_RUN_A = "a" * 32
CLAIM_RUN_B = "b" * 32


def make_job(service: BatchService, url: str) -> str:
    batch = service.create_batch(name=None, raw_inputs=[url])
    return batch["jobs"][0]["id"]


def claim(repository: WorkerRepository, worker_id: str, now: datetime = NOW) -> JobLease | None:
    return repository.claim_next(
        worker_id=worker_id,
        adapter="fake",
        adapter_version="1.0",
        now=now,
    )


@pytest.fixture
def worker_repository(database) -> WorkerRepository:
    return WorkerRepository(database)


def open_claim_gate(
    repository: WorkerRepository,
    *,
    run_id: str = CLAIM_RUN_A,
    worker_id: str = "local-app-worker",
    now: datetime = NOW,
) -> None:
    repository.prepare_claim_gate(run_id=run_id, worker_id=worker_id, now=now)
    repository.activate_claim_gate(run_id=run_id, worker_id=worker_id, now=now)


def test_claim_gate_cannot_activate_before_exact_run_is_prepared(
    worker_repository: WorkerRepository,
) -> None:
    with pytest.raises(RuntimeError, match="not prepared"):
        worker_repository.activate_claim_gate(
            run_id=CLAIM_RUN_A,
            worker_id="local-app-worker",
            now=NOW,
        )


def test_two_workers_cannot_claim_the_same_job(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=lease-test1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(lambda name: claim(worker_repository, name), ["a", "b"]))

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    assert claimed[0].attempt_no == 1
    attempts = worker_repository.attempts_for(claimed[0].job_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "running"


def test_stopped_claim_gate_prevents_a_new_job_or_attempt(
    service: BatchService,
    worker_repository: WorkerRepository,
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=stopped-gate")
    open_claim_gate(worker_repository)
    assert worker_repository.stop_claim_gate(
        run_id=CLAIM_RUN_A,
        now=NOW + timedelta(seconds=1),
    )

    lease = worker_repository.claim_next(
        worker_id="local-app-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW + timedelta(seconds=2),
        claim_gate_run_id=CLAIM_RUN_A,
    )

    assert lease is None
    assert worker_repository.get_job(job_id)["status"] == "queued"
    assert worker_repository.attempts_for(job_id) == []


@pytest.mark.parametrize(
    "malformation_sql",
    [
        "UPDATE worker_claim_gate SET accepting_claims = 2 WHERE id = 1",
        "UPDATE worker_claim_gate SET activated_at = '' WHERE id = 1",
    ],
    ids=["non-boolean-accepting-claims", "empty-activation-timestamp"],
)
def test_malformed_open_claim_gate_cannot_create_a_job_attempt(
    service: BatchService,
    worker_repository: WorkerRepository,
    database,
    malformation_sql: str,
) -> None:
    job_id = make_job(
        service,
        "https://www.youtube.com/watch?v=malformed-claim-gate",
    )
    open_claim_gate(worker_repository)
    with database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(malformation_sql)

    lease = worker_repository.claim_next(
        worker_id="local-app-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW + timedelta(seconds=1),
        claim_gate_run_id=CLAIM_RUN_A,
    )

    assert lease is None
    assert worker_repository.get_job(job_id)["status"] == "queued"
    assert worker_repository.attempts_for(job_id) == []


def test_claim_gate_stop_detects_an_after_update_reopen_trigger(
    service: BatchService,
    worker_repository: WorkerRepository,
    database,
) -> None:
    job_id = make_job(
        service,
        "https://www.youtube.com/watch?v=triggered-claim-gate",
    )
    open_claim_gate(worker_repository)
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reopen_gate_after_stop
            AFTER UPDATE ON worker_claim_gate
            WHEN NEW.accepting_claims = 0
                 AND NEW.stop_requested_at IS NOT NULL
            BEGIN
                UPDATE worker_claim_gate
                SET accepting_claims = 1,
                    activated_at = NEW.stop_requested_at,
                    stop_requested_at = NULL,
                    updated_at = NEW.stop_requested_at
                WHERE id = 1;
            END
            """
        )

    with pytest.raises(RuntimeError, match="stop was not persisted"):
        worker_repository.stop_claim_gate(
            run_id=CLAIM_RUN_A,
            now=NOW + timedelta(seconds=1),
        )

    assert worker_repository.get_job(job_id)["status"] == "queued"
    assert worker_repository.attempts_for(job_id) == []


def test_claim_committed_before_stop_is_retained_but_run_cannot_claim_again(
    service: BatchService,
    worker_repository: WorkerRepository,
) -> None:
    claimed_job = make_job(
        service,
        "https://www.youtube.com/watch?v=claim-before-stop",
    )
    queued_job = make_job(service, "https://x.com/example/status/981001")
    open_claim_gate(worker_repository)

    lease = worker_repository.claim_next(
        worker_id="local-app-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW,
        claim_gate_run_id=CLAIM_RUN_A,
    )
    assert lease is not None
    assert lease.job_id == claimed_job
    assert worker_repository.stop_claim_gate(
        run_id=CLAIM_RUN_A,
        now=NOW + timedelta(seconds=1),
    )

    assert (
        worker_repository.claim_next(
            worker_id="local-app-worker",
            adapter="fake",
            adapter_version="1.0",
            now=NOW + timedelta(seconds=2),
            claim_gate_run_id=CLAIM_RUN_A,
        )
        is None
    )
    assert worker_repository.get_job(claimed_job)["status"] == "probing"
    assert len(worker_repository.attempts_for(claimed_job)) == 1
    assert worker_repository.get_job(queued_job)["status"] == "queued"
    assert worker_repository.attempts_for(queued_job) == []


def test_preparing_and_activating_a_new_run_fences_a_stale_run_id(
    service: BatchService,
    worker_repository: WorkerRepository,
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=stale-run-fenced")
    open_claim_gate(worker_repository)
    worker_repository.prepare_claim_gate(
        run_id=CLAIM_RUN_B,
        worker_id="local-app-worker",
        now=NOW + timedelta(seconds=1),
    )
    worker_repository.activate_claim_gate(
        run_id=CLAIM_RUN_B,
        worker_id="local-app-worker",
        now=NOW + timedelta(seconds=1),
    )

    stale = worker_repository.claim_next(
        worker_id="local-app-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW + timedelta(seconds=2),
        claim_gate_run_id=CLAIM_RUN_A,
    )
    current = worker_repository.claim_next(
        worker_id="local-app-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW + timedelta(seconds=2),
        claim_gate_run_id=CLAIM_RUN_B,
    )

    assert stale is None
    assert current is not None
    assert current.job_id == job_id
    assert len(worker_repository.attempts_for(job_id)) == 1


def test_preparing_a_new_run_prevents_a_stale_supervisor_from_reactivating(
    worker_repository: WorkerRepository,
) -> None:
    worker_repository.prepare_claim_gate(
        run_id=CLAIM_RUN_A,
        worker_id="local-app-worker",
        now=NOW,
    )
    worker_repository.prepare_claim_gate(
        run_id=CLAIM_RUN_B,
        worker_id="local-app-worker",
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(RuntimeError, match="not prepared"):
        worker_repository.activate_claim_gate(
            run_id=CLAIM_RUN_A,
            worker_id="local-app-worker",
            now=NOW + timedelta(seconds=2),
        )

    opened = worker_repository.activate_claim_gate(
        run_id=CLAIM_RUN_B,
        worker_id="local-app-worker",
        now=NOW + timedelta(seconds=2),
    )
    assert opened["accepting_claims"] is True


def test_claim_transaction_linearizes_before_a_concurrent_gate_stop(
    service: BatchService,
    worker_repository: WorkerRepository,
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed_job = make_job(
        service,
        "https://www.youtube.com/watch?v=claim-linearizes-first",
    )
    queued_job = make_job(service, "https://x.com/example/status/981002")
    open_claim_gate(worker_repository)
    claim_holds_writer = Event()
    release_claim = Event()
    stop_entered_begin = Event()
    original_connect = database.connect

    class ConnectionProxy:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, sql, parameters=()):
            normalized = " ".join(sql.split())
            role = current_thread().name
            if role.startswith("stop-after-claim") and normalized == "BEGIN IMMEDIATE":
                stop_entered_begin.set()
            result = self.connection.execute(sql, parameters)
            if (
                role.startswith("claim-before-stop")
                and "FROM worker_claim_gate WHERE id = 1" in normalized
            ):
                claim_holds_writer.set()
                assert release_claim.wait(5.0)
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def controlled_connect():
        with original_connect() as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(database, "connect", controlled_connect)
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="claim-before-stop"
    ) as claim_executor, ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="stop-after-claim"
    ) as stop_executor:
        claim_future = claim_executor.submit(
            worker_repository.claim_next,
            worker_id="local-app-worker",
            adapter="fake",
            adapter_version="1.0",
            now=NOW,
            claim_gate_run_id=CLAIM_RUN_A,
        )
        assert claim_holds_writer.wait(5.0)
        stop_future = stop_executor.submit(
            worker_repository.stop_claim_gate,
            run_id=CLAIM_RUN_A,
            now=NOW + timedelta(seconds=1),
        )
        assert stop_entered_begin.wait(5.0)
        assert not stop_future.done()
        release_claim.set()
        lease = claim_future.result(timeout=5.0)
        assert stop_future.result(timeout=5.0) is True

    assert lease is not None and lease.job_id == claimed_job
    assert len(worker_repository.attempts_for(claimed_job)) == 1
    assert worker_repository.get_job(queued_job)["status"] == "queued"
    assert worker_repository.attempts_for(queued_job) == []


def test_gate_stop_transaction_linearizes_before_a_concurrent_claim(
    service: BatchService,
    worker_repository: WorkerRepository,
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = make_job(
        service,
        "https://www.youtube.com/watch?v=stop-linearizes-first",
    )
    open_claim_gate(worker_repository)
    stop_holds_writer = Event()
    release_stop = Event()
    claim_entered_begin = Event()
    original_connect = database.connect

    class ConnectionProxy:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, sql, parameters=()):
            normalized = " ".join(sql.split())
            role = current_thread().name
            if role.startswith("claim-after-stop") and normalized == "BEGIN IMMEDIATE":
                claim_entered_begin.set()
            result = self.connection.execute(sql, parameters)
            if (
                role.startswith("stop-before-claim")
                and normalized.startswith("SELECT run_id FROM worker_claim_gate")
            ):
                stop_holds_writer.set()
                assert release_stop.wait(5.0)
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def controlled_connect():
        with original_connect() as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(database, "connect", controlled_connect)
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="stop-before-claim"
    ) as stop_executor, ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="claim-after-stop"
    ) as claim_executor:
        stop_future = stop_executor.submit(
            worker_repository.stop_claim_gate,
            run_id=CLAIM_RUN_A,
            now=NOW + timedelta(seconds=1),
        )
        assert stop_holds_writer.wait(5.0)
        claim_future = claim_executor.submit(
            worker_repository.claim_next,
            worker_id="local-app-worker",
            adapter="fake",
            adapter_version="1.0",
            now=NOW + timedelta(seconds=2),
            claim_gate_run_id=CLAIM_RUN_A,
        )
        assert claim_entered_begin.wait(5.0)
        assert not claim_future.done()
        release_stop.set()
        assert stop_future.result(timeout=5.0) is True
        assert claim_future.result(timeout=5.0) is None

    assert worker_repository.get_job(job_id)["status"] == "queued"
    assert worker_repository.attempts_for(job_id) == []


def test_stopped_run_active_attempt_is_recovered_only_after_lease_expiry(
    service: BatchService,
    worker_repository: WorkerRepository,
) -> None:
    interrupted_job = make_job(
        service,
        "https://www.youtube.com/watch?v=active-stop-recovery",
    )
    untouched_job = make_job(service, "https://x.com/example/status/981003")
    open_claim_gate(worker_repository)
    first_lease = worker_repository.claim_next(
        worker_id="local-app-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW,
        lease_seconds=10,
        claim_gate_run_id=CLAIM_RUN_A,
    )
    assert first_lease is not None and first_lease.job_id == interrupted_job
    assert worker_repository.stop_claim_gate(
        run_id=CLAIM_RUN_A,
        now=NOW + timedelta(seconds=1),
    )
    assert (
        worker_repository.claim_next(
            worker_id="local-app-worker",
            adapter="fake",
            adapter_version="1.0",
            now=NOW + timedelta(seconds=2),
            claim_gate_run_id=CLAIM_RUN_A,
        )
        is None
    )

    worker_repository.prepare_claim_gate(
        run_id=CLAIM_RUN_B,
        worker_id="local-app-worker",
        now=NOW + timedelta(seconds=11),
    )
    worker_repository.activate_claim_gate(
        run_id=CLAIM_RUN_B,
        worker_id="local-app-worker",
        now=NOW + timedelta(seconds=11),
    )
    recovered_lease = worker_repository.claim_next(
        worker_id="local-app-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW + timedelta(seconds=11),
        lease_seconds=10,
        claim_gate_run_id=CLAIM_RUN_B,
    )

    assert recovered_lease is not None
    assert recovered_lease.job_id == interrupted_job
    assert recovered_lease.attempt_id != first_lease.attempt_id
    assert recovered_lease.attempt_no == 2
    attempts = worker_repository.attempts_for(interrupted_job)
    assert [attempt["status"] for attempt in attempts] == ["abandoned", "running"]
    assert attempts[0]["error_code"] == ErrorCode.WORKER_LOST.value
    assert worker_repository.get_job(untouched_job)["status"] == "queued"
    assert worker_repository.attempts_for(untouched_job) == []


def test_worker_claim_filters_platform_source_and_job_route(
    service: BatchService,
    worker_repository: WorkerRepository,
    database,
) -> None:
    youtube_job = make_job(
        service, "https://www.youtube.com/watch?v=route-youtube"
    )
    bilibili_job = make_job(
        service, "https://www.bilibili.com/video/BV1xx411c7mD"
    )

    lease = worker_repository.claim_next(
        worker_id="bilibili-only",
        adapter="yt_dlp",
        adapter_version="test",
        now=NOW,
        supported_routes=frozenset(
            {AdapterRoute(Platform.BILIBILI, SourceType.BILIBILI_VIDEO)}
        ),
    )

    assert lease is not None
    assert lease.job_id == bilibili_job
    assert lease.platform is Platform.BILIBILI
    with database.connect() as connection:
        untouched = connection.execute(
            "SELECT status FROM download_jobs WHERE id = ?", (youtube_job,)
        ).fetchone()
    assert untouched is not None
    assert untouched["status"] == "queued"


def test_worker_with_no_supported_routes_claims_nothing(
    service: BatchService,
    worker_repository: WorkerRepository,
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=no-route")

    lease = worker_repository.claim_next(
        worker_id="no-routes",
        adapter="yt_dlp",
        adapter_version="test",
        now=NOW,
        supported_routes=frozenset(),
    )

    assert lease is None
    assert worker_repository.get_job(job_id)["status"] == "queued"


def test_route_filter_skips_same_source_with_wrong_job_kind(
    service: BatchService,
    repository,
    settings,
    worker_repository: WorkerRepository,
) -> None:
    graph_service = BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
        x_graph_v2_enabled=True,
    )
    discover_batch = graph_service.create_batch(
        name="older discover",
        raw_inputs=["https://x.com/example/status/991011"],
    )
    download_batch = service.create_batch(
        name="later download",
        raw_inputs=["https://x.com/example/status/991012"],
    )

    lease = worker_repository.claim_next(
        worker_id="x-download-only",
        adapter="yt_dlp",
        adapter_version="test",
        now=NOW,
        supported_routes=frozenset(
            {
                AdapterRoute(
                    Platform.X,
                    SourceType.X_POST,
                    AdapterJobKind.DOWNLOAD,
                )
            }
        ),
    )

    assert lease is not None
    assert lease.job_id == download_batch["jobs"][0]["id"]
    assert worker_repository.get_job(discover_batch["jobs"][0]["id"])[
        "status"
    ] == "queued"


def test_local_flat_policy_atomically_skips_unsupported_graph_jobs(
    repository,
    settings,
    database,
    worker_repository: WorkerRepository,
) -> None:
    graph_service = BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
        x_graph_v2_enabled=True,
    )
    graph_batch = graph_service.create_batch(
        name="graph-kept-queued",
        raw_inputs=["https://x.com/example/status/991001"],
    )
    flat_batch = graph_service.create_batch(
        name="flat-can-run",
        raw_inputs=["https://www.youtube.com/watch?v=flat-after-graph"],
    )

    lease = worker_repository.claim_next(
        worker_id="local-flat",
        adapter="yt_dlp",
        adapter_version="test",
        now=NOW,
        supports_exact_selector=False,
        skip_unsupported_graph_jobs=True,
    )

    assert lease is not None
    assert lease.job_id == flat_batch["jobs"][0]["id"]
    with database.connect() as connection:
        graph_row = connection.execute(
            "SELECT status FROM download_jobs WHERE id = ?",
            (graph_batch["jobs"][0]["id"],),
        ).fetchone()
    assert graph_row is not None
    assert graph_row["status"] == "queued"


def test_platform_concurrency_is_one(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=platform-one")
    make_job(service, "https://www.youtube.com/watch?v=platform-two")
    make_job(service, "https://x.com/example/status/900001")

    first = claim(worker_repository, "a")
    second = claim(worker_repository, "b")
    third = claim(worker_repository, "c")

    assert first and first.platform == "youtube"
    assert second and second.platform == "x"
    assert third is None


def test_global_concurrency_is_two_across_three_platforms(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=global-limit")
    make_job(service, "https://x.com/example/status/900002")
    make_job(service, "https://www.bilibili.com/video/BV1xx411c7mD")

    first = claim(worker_repository, "a")
    second = claim(worker_repository, "b")
    third = claim(worker_repository, "c")

    assert first is not None
    assert second is not None
    assert first.platform != second.platform
    assert third is None


def test_refill_skips_expired_recovery_and_excludes_live_job_ids(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    expired_job = make_job(service, "https://www.youtube.com/watch?v=live-expired")
    live_job = make_job(service, "https://x.com/example/status/900005")
    available_job = make_job(service, "https://www.bilibili.com/video/BV1xx411c7mD")
    original = claim(worker_repository, "local-worker")
    assert original and original.job_id == expired_job

    refilled = worker_repository.claim_next(
        worker_id="local-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW + timedelta(seconds=61),
        perform_recovery=False,
        excluded_job_ids=frozenset({live_job}),
    )

    assert refilled and refilled.job_id == available_job
    assert worker_repository.get_job(expired_job)["status"] == "probing"
    assert worker_repository.attempts_for(expired_job)[0]["status"] == "running"
    assert len(worker_repository.attempts_for(expired_job)) == 1
    assert worker_repository.attempts_for(live_job) == []


def test_heartbeat_extends_lease_and_reports_cancel(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=heartbeat01")
    lease = claim(worker_repository, "worker")
    assert lease

    assert worker_repository.heartbeat(lease, now=NOW + timedelta(seconds=15)) is False
    assert worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=16)) == JobStatus.PROBING
    assert worker_repository.heartbeat(lease, now=NOW + timedelta(seconds=30)) is True
    worker_repository.finish_canceled(lease, now=NOW + timedelta(seconds=31))
    assert worker_repository.get_job(job_id)["status"] == "canceled"
    assert worker_repository.attempts_for(job_id)[0]["status"] == "canceled"


def test_progress_transition_extends_lease(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=progress-renew")
    lease = claim(worker_repository, "worker")
    assert lease

    worker_repository.transition(
        lease,
        status=JobStatus.DOWNLOADING,
        progress=0.5,
        now=NOW + timedelta(seconds=50),
    )

    job = worker_repository.get_job(job_id)
    assert job["heartbeat_at"] == "2026-09-03T00:00:50.000Z"
    assert job["lease_expires_at"] == "2026-09-03T00:01:50.000Z"
    assert claim(worker_repository, "other", NOW + timedelta(seconds=61)) is None
    assert worker_repository.get_job(job_id)["attempt_count"] == 1


def test_queued_cancel_never_creates_attempt(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-queue")
    assert worker_repository.request_cancel(job_id, now=NOW) == JobStatus.CANCELED
    assert claim(worker_repository, "worker") is None
    assert worker_repository.attempts_for(job_id) == []
    batch = service.get_batch(
        worker_repository.get_job(job_id)["batch_id"]
    )
    assert batch["status"] == "canceled"
    assert batch["canceled_count"] == 1
    assert batch["inputs"][0]["status"] == "canceled"


def test_cancel_requested_while_verifying_wins_over_success(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-success")
    lease = claim(worker_repository, "worker")
    assert lease
    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.transition(
        lease, status=JobStatus.VERIFYING, progress=0.9, now=NOW
    )

    assert (
        worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))
        == JobStatus.VERIFYING
    )
    final_status = worker_repository.finish_success(
        lease, now=NOW + timedelta(seconds=2)
    )

    assert final_status == JobStatus.CANCELED
    assert worker_repository.get_job(job_id)["status"] == "canceled"
    assert worker_repository.attempts_for(job_id)[0]["status"] == "canceled"
    batch = service.get_batch(worker_repository.get_job(job_id)["batch_id"])
    assert batch["status"] == "canceled"
    assert batch["inputs"][0]["status"] == "canceled"


def test_expired_lease_preserves_pending_cancel(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-expired")
    lease = claim(worker_repository, "worker")
    assert lease
    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))

    assert claim(worker_repository, "recovery", NOW + timedelta(seconds=61)) is None

    assert worker_repository.get_job(job_id)["status"] == "canceled"
    assert worker_repository.attempts_for(job_id)[0]["status"] == "canceled"
    batch = service.get_batch(worker_repository.get_job(job_id)["batch_id"])
    assert batch["status"] == "canceled"
    assert batch["inputs"][0]["status"] == "canceled"


def test_cancel_requested_while_downloading_wins_over_failure(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-failure")
    lease = claim(worker_repository, "worker")
    assert lease
    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))

    final_status = worker_repository.finish_failure(
        lease,
        error_code=ErrorCode.NETWORK_ERROR,
        diagnostic="download failed after cancellation",
        now=NOW + timedelta(seconds=2),
        retry_at=NOW + timedelta(seconds=10),
    )

    assert final_status == JobStatus.CANCELED
    assert worker_repository.get_job(job_id)["status"] == "canceled"
    attempt = worker_repository.attempts_for(job_id)[0]
    assert attempt["status"] == "canceled"
    assert attempt["error_code"] is None


def test_state_machine_rejects_regression_and_finishes_success(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=success0001")
    lease = claim(worker_repository, "worker")
    assert lease

    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.2, now=NOW
    )
    with pytest.raises(InvalidTransition):
        worker_repository.transition(
            lease, status=JobStatus.PROBING, progress=0.3, now=NOW
        )
    with pytest.raises(InvalidTransition, match="progress cannot decrease"):
        worker_repository.transition(
            lease, status=JobStatus.DOWNLOADING, progress=0.1, now=NOW
        )
    worker_repository.transition(
        lease, status=JobStatus.VERIFYING, progress=0.9, now=NOW
    )
    worker_repository.finish_success(lease, now=NOW)

    job = worker_repository.get_job(job_id)
    assert job["status"] == "ready"
    assert job["progress"] == 1
    assert worker_repository.attempts_for(job_id)[0]["status"] == "succeeded"
    batch = service.get_batch(job["batch_id"])
    assert batch["status"] == "ready"
    assert batch["ready_count"] == 1
    assert batch["inputs"][0]["status"] == "ready"


def test_lost_lease_cannot_heartbeat_or_finalize(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=lost-lease1")
    lease = claim(worker_repository, "worker")
    assert lease
    forged = replace(lease, lease_token="stale")
    with pytest.raises(LostLease):
        worker_repository.heartbeat(forged, now=NOW)
    with pytest.raises(LostLease):
        worker_repository.finish_success(forged, now=NOW)


@pytest.mark.parametrize(
    "operation",
    [
        "heartbeat",
        "transition",
        "record_probe",
        "finish_success",
        "finish_success_with_asset",
        "finish_failure",
        "finish_canceled",
    ],
)
def test_exact_lease_expiry_fences_every_worker_write(
    operation: str,
    service: BatchService,
    worker_repository: WorkerRepository,
) -> None:
    job_id = make_job(
        service, f"https://www.youtube.com/watch?v=expiry-{operation}"
    )
    lease = claim(worker_repository, "worker")
    assert lease
    expires_at = NOW + timedelta(seconds=60)

    if operation in {"finish_success", "finish_success_with_asset"}:
        worker_repository.transition(
            lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
        )
        worker_repository.transition(
            lease, status=JobStatus.VERIFYING, progress=0.9, now=NOW
        )
    if operation == "finish_canceled":
        worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))

    with pytest.raises(LostLease):
        if operation == "heartbeat":
            worker_repository.heartbeat(lease, now=expires_at)
        elif operation == "transition":
            worker_repository.transition(
                lease,
                status=JobStatus.DOWNLOADING,
                progress=0.5,
                now=expires_at,
            )
        elif operation == "record_probe":
            worker_repository.record_probe(
                lease,
                expected_item_count=1,
                discovery_snapshot_hash="expiry-snapshot",
                sanitized_source={"title": "must not be recorded"},
                now=expires_at,
            )
        elif operation == "finish_success":
            worker_repository.finish_success(lease, now=expires_at)
        elif operation == "finish_success_with_asset":
            worker_repository.finish_success_with_asset(
                lease,
                asset=CommittedAsset(
                    asset_id="44444444-4444-4444-8444-444444444444",
                    media_key="expiry-asset",
                    media_kind="video",
                    role="original",
                    ordinal=0,
                    size_bytes=1,
                    sha256="a" * 64,
                    relative_original_path="assets/expiry/original/source.mp4",
                    relative_manifest_path="assets/expiry/metadata/manifest.json",
                    manifest_sha256="b" * 64,
                    verification=VerificationResult(media_kind="video"),
                ),
                now=expires_at,
            )
        elif operation == "finish_failure":
            worker_repository.finish_failure(
                lease,
                error_code=ErrorCode.NETWORK_ERROR,
                diagnostic="expired worker failure",
                now=expires_at,
            )
        else:
            worker_repository.finish_canceled(lease, now=expires_at)

    assert worker_repository.attempts_for(job_id)[0]["status"] == "running"
    recovered = claim(worker_repository, "recovery", expires_at)
    if operation == "finish_canceled":
        assert recovered is None
        assert worker_repository.get_job(job_id)["status"] == "canceled"
    else:
        assert recovered and recovered.job_id == job_id
        assert recovered.attempt_no == 2


def test_expired_lease_is_abandoned_and_reclaimed(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=recover0001")
    old = claim(worker_repository, "old")
    assert old

    new = claim(worker_repository, "new", NOW + timedelta(seconds=61))
    assert new
    assert new.job_id == job_id
    assert new.attempt_no == 2
    assert new.attempt_id != old.attempt_id
    attempts = worker_repository.attempts_for(job_id)
    assert [attempt["status"] for attempt in attempts] == ["abandoned", "running"]
    assert attempts[0]["error_code"] == "worker_lost"
    with pytest.raises(LostLease):
        worker_repository.heartbeat(old, now=NOW + timedelta(seconds=62))


def test_failure_can_retry_but_hard_stops_at_four_attempts(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=retry-test1")
    current_time = NOW
    for expected_attempt in range(1, 5):
        lease = claim(worker_repository, f"worker-{expected_attempt}", current_time)
        assert lease and lease.attempt_no == expected_attempt
        status = worker_repository.finish_failure(
            lease,
            error_code=ErrorCode.NETWORK_ERROR,
            diagnostic="temporary",
            now=current_time,
            retry_at=current_time + timedelta(seconds=1),
        )
        expected = JobStatus.QUEUED if expected_attempt < 4 else JobStatus.FAILED
        assert status == expected
        current_time += timedelta(seconds=1)

    assert worker_repository.get_job(job_id)["final_error_code"] == "network_error"
    assert claim(worker_repository, "fifth", current_time) is None
    assert len(worker_repository.attempts_for(job_id)) == 4
    batch = service.get_batch(worker_repository.get_job(job_id)["batch_id"])
    assert batch["status"] == "failed"
    assert batch["failed_count"] == 1
    assert batch["inputs"][0]["error_code"] == "network_error"


def test_explicit_retry_starts_a_new_generation_and_preserves_attempt_history(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=manual-retry")
    first = claim(worker_repository, "worker-one")
    assert first is not None
    assert worker_repository.finish_failure(
        first,
        error_code=ErrorCode.NETWORK_ERROR,
        diagnostic="terminal synthetic failure",
        now=NOW,
    ) is JobStatus.FAILED

    retried = worker_repository.request_retry(
        job_id,
        now=NOW + timedelta(seconds=1),
    )

    assert retried == {
        "job_id": job_id,
        "status": "queued",
        "run_generation": 2,
        "platform": "youtube",
        "previous_error_code": "network_error",
    }
    job = worker_repository.get_job(job_id)
    assert job["status"] == "queued"
    assert job["attempt_count"] == 1
    assert job["run_generation"] == 2
    assert job["generation_attempt_count"] == 0
    assert job["final_error_code"] is None
    batch = service.get_batch(job["batch_id"])
    assert batch["status"] == "queued"
    assert batch["queued_count"] == 1
    assert batch["failed_count"] == 0
    assert batch["inputs"][0]["status"] == "queued"
    assert batch["inputs"][0]["active_run_generation"] == 2
    assert batch["inputs"][0]["error_code"] is None

    second = claim(worker_repository, "worker-two", NOW + timedelta(seconds=2))
    assert second is not None
    assert second.job_id == job_id
    assert second.attempt_no == 2
    assert second.run_generation == 2
    attempts = worker_repository.attempts_for(job_id)
    assert [attempt["run_generation"] for attempt in attempts] == [1, 2]
    assert [attempt["generation_attempt_no"] for attempt in attempts] == [1, 1]


def test_explicit_retry_rejects_non_failed_and_missing_jobs(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=retry-conflict")

    with pytest.raises(RetryConflict, match="terminal failed"):
        worker_repository.request_retry(job_id, now=NOW)
    assert worker_repository.request_retry("missing-job", now=NOW) is None


def test_concurrent_explicit_retry_advances_only_one_generation(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=retry-double")
    lease = claim(worker_repository, "worker-one")
    assert lease is not None
    worker_repository.finish_failure(
        lease,
        error_code=ErrorCode.NETWORK_ERROR,
        diagnostic="terminal synthetic failure",
        now=NOW,
    )

    def retry_once() -> str:
        try:
            result = worker_repository.request_retry(
                job_id,
                now=NOW + timedelta(seconds=1),
            )
        except RetryConflict:
            return "conflict"
        assert result is not None
        return "queued"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: retry_once(), range(2)))

    assert sorted(outcomes) == ["conflict", "queued"]
    job = worker_repository.get_job(job_id)
    assert job["run_generation"] == 2
    assert job["generation_attempt_count"] == 0
    assert len(worker_repository.attempts_for(job_id)) == 1


def test_explicit_retry_rejects_when_same_source_has_new_live_work(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    url = "https://www.youtube.com/watch?v=retry-live-conflict"
    failed_job_id = make_job(service, url)
    lease = claim(worker_repository, "worker-one")
    assert lease is not None
    worker_repository.finish_failure(
        lease,
        error_code=ErrorCode.NETWORK_ERROR,
        diagnostic="terminal synthetic failure",
        now=NOW,
    )
    live_job_id = make_job(service, url)
    assert live_job_id != failed_job_id

    with pytest.raises(RetryConflict, match="live or ready work"):
        worker_repository.request_retry(
            failed_job_id,
            now=NOW + timedelta(seconds=1),
        )

    assert worker_repository.get_job(failed_job_id)["status"] == "failed"
    assert worker_repository.get_job(live_job_id)["status"] == "queued"


def test_explicit_retry_rejects_graph_parent_even_when_terminal_failed(
    repository,
    settings,
    worker_repository: WorkerRepository,
) -> None:
    graph_service = BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
        x_graph_v2_enabled=True,
    )
    batch = graph_service.create_batch(
        name="graph retry rejected",
        raw_inputs=["https://x.com/example/status/991099"],
    )
    job_id = batch["jobs"][0]["id"]
    lease = worker_repository.claim_next(
        worker_id="graph-worker",
        adapter="fake",
        adapter_version="1.0",
        now=NOW,
        supports_exact_selector=True,
    )
    assert lease is not None and lease.job_id == job_id
    worker_repository.finish_failure(
        lease,
        error_code=ErrorCode.ADAPTER_UNSUPPORTED,
        diagnostic="terminal graph failure",
        now=NOW,
    )

    with pytest.raises(InvalidTransition, match="flat download"):
        worker_repository.request_retry(
            job_id,
            now=NOW + timedelta(seconds=1),
        )

    assert worker_repository.get_job(job_id)["status"] == "failed"


def test_input_and_batch_wait_for_all_jobs_before_terminal_aggregation(
    service: BatchService, worker_repository: WorkerRepository, database
) -> None:
    first_job_id = make_job(
        service, "https://www.youtube.com/watch?v=aggregate-first"
    )
    first_job = worker_repository.get_job(first_job_id)
    second_source_id = "22222222-2222-4222-8222-222222222222"
    second_job_id = "33333333-3333-4333-8333-333333333333"
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_jobs SET created_at = ? WHERE id = ?",
            ("2026-09-02T23:59:00.000Z", first_job_id),
        )
        connection.execute(
            """
            INSERT INTO source_items(
                id, platform, source_type, source_id, canonical_url,
                created_at, updated_at
            ) VALUES (?, 'youtube', 'youtube_video', ?, ?, ?, ?)
            """,
            (
                second_source_id,
                "aggregate-second",
                "https://www.youtube.com/watch?v=aggregate-second",
                "2026-09-03T00:00:00.000Z",
                "2026-09-03T00:00:00.000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO download_jobs(
                id, batch_id, input_record_id, source_item_id, job_kind,
                status, progress, route_policy_version, available_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'download', 'queued', 0, 'test-v1', ?, ?, ?)
            """,
            (
                second_job_id,
                first_job["batch_id"],
                first_job["input_record_id"],
                second_source_id,
                "2026-09-03T00:00:00.000Z",
                "2026-09-03T00:00:00.000Z",
                "2026-09-03T00:00:00.000Z",
            ),
        )

    first = claim(worker_repository, "first")
    assert first and first.job_id == first_job_id
    worker_repository.transition(
        first, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.transition(
        first, status=JobStatus.VERIFYING, progress=0.9, now=NOW
    )
    assert worker_repository.finish_success(first, now=NOW) == JobStatus.READY

    pending = service.get_batch(first_job["batch_id"])
    assert pending["status"] == "queued"
    assert pending["queued_count"] == 1
    assert pending["ready_count"] == 0
    assert pending["inputs"][0]["status"] == "queued"

    second = claim(worker_repository, "second")
    assert second and second.job_id == second_job_id
    final = worker_repository.finish_failure(
        second,
        error_code=ErrorCode.CONTENT_UNAVAILABLE,
        diagnostic="second child unavailable",
        now=NOW + timedelta(seconds=1),
    )
    assert final == JobStatus.FAILED

    aggregate = service.get_batch(first_job["batch_id"])
    assert aggregate["status"] == "partial_success"
    assert aggregate["queued_count"] == 0
    assert aggregate["inputs"][0]["status"] == "partial_success"
    assert aggregate["inputs"][0]["error_code"] == "content_unavailable"

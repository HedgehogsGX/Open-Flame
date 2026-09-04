from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


def test_real_worker_refills_free_slot_without_waiting_for_other_platform(
    settings, database, service,
):
    from video_download_control.worker_pool import run_concurrent_worker

    urls = (
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1yy411c7mD",
    )
    service.create_batch(name="concurrency", raw_inputs=list(urls))
    entered = {url: Event() for url in urls}
    release = {url: Event() for url in urls}
    state_lock = Lock()
    active = set()
    peaks = []

    class Adapter(ScriptedFakeAdapter):
        def probe(self, request, context):
            with state_lock:
                assert request.platform not in active
                active.add(request.platform)
                peaks.append(len(active))
            entered[request.canonical_url].set()
            assert release[request.canonical_url].wait(10)
            return super().probe(request, context)

        def download(self, request, context, progress, cancellation):
            result = super().download(request, context, progress, cancellation)
            with state_lock:
                active.remove(request.platform)
            return result

    worker = Worker(
        worker_id="pool-test", repository=WorkerRepository(database),
        adapter=Adapter(), asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
    )
    results = []
    with ThreadPoolExecutor(max_workers=1) as coordinator:
        future = coordinator.submit(run_concurrent_worker, worker, on_result=results.append)
        try:
            assert entered[urls[0]].wait(5)
            assert entered[urls[1]].wait(5)
            assert not entered[urls[2]].is_set()
            release[urls[1]].set()
            assert entered[urls[2]].wait(5), "free slot must refill while YouTube is blocked"
            assert not release[urls[0]].is_set()
        finally:
            for event in release.values():
                event.set()
            future.result(timeout=10)
    assert max(peaks) == 2
    assert len([result for result in results if result is not None]) == 3
    assert all(result.status == "ready" for result in results if result is not None)


def test_stop_before_claim_never_touches_worker():
    from video_download_control.worker_pool import run_concurrent_worker

    class WorkerMustNotRun:
        def claim_once(self, **_kwargs):
            pytest.fail("stop was already requested")

    run_concurrent_worker(WorkerMustNotRun(), wait_for_stop=lambda _timeout: True)


def test_submission_failure_executes_claim_at_most_once_and_stops(monkeypatch):
    from video_download_control import worker_pool

    stopped = Event()
    executions = []

    class FakeWorker:
        def claim_once(self, **_kwargs):
            return SimpleNamespace(lease=SimpleNamespace(job_id="job-1"))

        def request_stop(self):
            stopped.set()

        def execute_claimed(self, claim):
            assert stopped.is_set()
            executions.append(claim.lease.job_id)

    class FailingExecutor:
        def __init__(self, **_kwargs):
            self.task = None

        def submit(self, task):
            self.task = task
            raise RuntimeError("synthetic dispatch failure after enqueue")

        def shutdown(self, **_kwargs):
            if self.task:
                self.task()

    monkeypatch.setattr(worker_pool, "ThreadPoolExecutor", FailingExecutor)
    with pytest.raises(RuntimeError, match="synthetic dispatch failure"):
        worker_pool.run_concurrent_worker(FakeWorker())
    assert executions == ["job-1"]


def test_live_identity_remains_excluded_until_final_cleanup_returns():
    from video_download_control.worker_pool import run_concurrent_worker

    in_cleanup = Event()
    finish_cleanup = Event()
    second_entered = Event()
    release_second = Event()
    refilled = Event()
    claims = []

    class LifecycleWorker:
        issued = 0

        def claim_once(self, **kwargs):
            claims.append(kwargs)
            self.issued += 1
            if self.issued == 3:
                assert in_cleanup.is_set() and not finish_cleanup.is_set()
                assert kwargs == {"perform_recovery": False,
                                  "excluded_job_ids": frozenset({"job-1"})}
                refilled.set()
            if self.issued > 3:
                return None
            return SimpleNamespace(lease=SimpleNamespace(job_id=f"job-{self.issued}"))

        def execute_claimed(self, claim):
            job_id = claim.lease.job_id
            if job_id == "job-1":
                in_cleanup.set()  # Terminal DB row, but finally still owns files.
                assert finish_cleanup.wait(5)
            if job_id == "job-2":
                second_entered.set()
                assert release_second.wait(5)
            return SimpleNamespace(job_id=job_id, status="ready")

        def request_stop(self):
            finish_cleanup.set()
            release_second.set()

    worker = LifecycleWorker()
    with ThreadPoolExecutor(max_workers=1) as coordinator:
        future = coordinator.submit(run_concurrent_worker, worker)
        try:
            assert in_cleanup.wait(5) and second_entered.wait(5)
            release_second.set()
            assert refilled.wait(5)
        finally:
            worker.request_stop()
            future.result(timeout=5)
    assert claims[0]["perform_recovery"] is True
    assert claims[-1] == {"perform_recovery": True, "excluded_job_ids": frozenset()}


def test_stop_is_checked_between_individual_claims_and_finishes_claimed_work():
    from video_download_control.worker_pool import run_concurrent_worker

    class StopAfterFirstClaim:
        claims = 0
        finished = False

        def claim_once(self, **_kwargs):
            self.claims += 1
            return SimpleNamespace(lease=SimpleNamespace(job_id="job-1"))

        def execute_claimed(self, _claim):
            self.finished = True
            return SimpleNamespace(status="ready")

        def request_stop(self):
            pytest.fail("graceful stop must not interrupt already claimed work")

    worker = StopAfterFirstClaim()
    run_concurrent_worker(worker, wait_for_stop=lambda _timeout: worker.claims == 1)
    assert worker.claims == 1 and worker.finished


def test_heartbeat_thread_start_failure_finalizes_claim(settings, database, service, monkeypatch):
    import video_download_control.worker as worker_module

    service.create_batch(name="heartbeat failure", raw_inputs=[
        "https://www.youtube.com/watch?v=abcdefghijk",
    ])
    worker = Worker(
        worker_id="pool-heartbeat-failure", repository=WorkerRepository(database),
        adapter=ScriptedFakeAdapter(), asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
    )

    def fail_start(_heartbeat):
        raise RuntimeError("synthetic thread resource failure")

    monkeypatch.setattr(worker_module._LeaseHeartbeat, "start", fail_start)
    result = worker.run_once()
    assert result.status in {"queued", "failed"}
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM job_attempts").fetchone()[0] != "running"


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True])
def test_invalid_poll_interval_rejected_before_claim(interval):
    from video_download_control.worker_pool import run_concurrent_worker

    with pytest.raises(ValueError):
        run_concurrent_worker(object(), poll_interval_seconds=interval)

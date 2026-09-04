"""Two execution slots with single-owner claiming and quiescent recovery.

The database remains the authority for global/per-platform limits. A slot is
occupied until the attempt's entire finally/cleanup has returned, even if its
database row is already terminal. Only the coordinator reads stop channels.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock

from .worker import Worker, WorkerClaim, WorkerRunResult


class _Dispatch:
    """Execute at most once, including ambiguous executor submission failure."""

    def __init__(self, worker: Worker, claim: WorkerClaim) -> None:
        self.worker = worker
        self.claim = claim
        self._lock = Lock()
        self._executed = False
        self._result: WorkerRunResult | None = None
        self._error: BaseException | None = None

    def __call__(self) -> WorkerRunResult:
        with self._lock:
            if not self._executed:
                self._executed = True
                try:
                    self._result = self.worker.execute_claimed(self.claim)
                except BaseException as exc:
                    self._error = exc
            if self._error is not None:
                raise self._error
            return self._result


def run_concurrent_worker(
    worker: Worker,
    *,
    poll_interval_seconds: float | None = None,
    wait_for_stop: Callable[[float], bool] | None = None,
    on_result: Callable[[WorkerRunResult | None], None] | None = None,
) -> None:
    """Drain eligible jobs, or poll persistently, using at most two slots.

    A stop-channel notification prevents further claims and lets active work
    finish. The app supervisor still owns its bounded shutdown/kill policy.
    An exception (including Ctrl+C) also interrupts owned subprocesses before
    joining threads. No claim is left silently waiting in an executor queue.
    """
    if poll_interval_seconds is not None and (
        isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, (int, float))
        or not math.isfinite(poll_interval_seconds)
        or poll_interval_seconds <= 0
    ):
        raise ValueError("poll interval must be finite and positive")

    def wait(timeout: float) -> bool:
        if wait_for_stop is not None:
            return wait_for_stop(timeout)
        if timeout:
            time.sleep(timeout)
        return False

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="download-slot")
    active: dict[Future[WorkerRunResult], WorkerClaim] = {}
    stopping = False
    next_claim_at = 0.0
    try:
        while True:
            completed = [future for future in active if future.done()]
            for future in completed:
                # Future.done includes execute_claimed's final cleanup.
                del active[future]
                result = future.result()
                if on_result is not None:
                    on_result(result)
            if completed:
                next_claim_at = 0.0
            if stopping:
                if not active:
                    return
                time.sleep(0.05)
                continue

            # Check immediately before each individual claim, never only once
            # per pair. The persistent DB claim gate is independently checked.
            if wait(0.0):
                stopping = True
                continue
            if len(active) < 2 and time.monotonic() >= next_claim_at:
                claim = worker.claim_once(
                    perform_recovery=not active,
                    excluded_job_ids=frozenset(c.lease.job_id for c in active.values()),
                )
                if claim is not None:
                    dispatch = _Dispatch(worker, claim)
                    try:
                        future = executor.submit(dispatch)
                    except BaseException:
                        # submit can enqueue and then fail starting a thread.
                        # Stop first, then close this claim exactly once whether
                        # it was queued or not; retain the original exception.
                        worker.request_stop()
                        try:
                            dispatch()
                        except BaseException:
                            pass
                        raise
                    active[future] = claim
                    continue
                if not active:
                    if on_result is not None:
                        on_result(None)
                    if poll_interval_seconds is None:
                        return
                    if wait(poll_interval_seconds):
                        stopping = True
                    continue
                # Drain needs no new claim until a live attempt finishes.
                next_claim_at = (
                    float("inf") if poll_interval_seconds is None
                    else time.monotonic() + poll_interval_seconds
                )
            if wait(0.05):
                stopping = True
    except BaseException:
        worker.request_stop()
        raise
    finally:
        executor.shutdown(wait=True)

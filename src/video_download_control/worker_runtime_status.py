"""Run-bound supervisor observations, separate from tool capability policy.

The supervisor shares a small, bounded snapshot directly with its control child.
No on-disk marker or database claim flag is treated as proof of process liveness.
The shared clock is monotonic on the host. An expired or locked snapshot fails
closed to unknown; it never stops or starts work.
"""
from __future__ import annotations

import math
import re
import time
from multiprocessing import get_context

from .schemas import WorkerRuntimeStatusResponse


HEARTBEAT_TIMEOUT_SECONDS = 3.0
_PHASES = ('starting', 'online', 'stopping', 'stopped', 'check_only')


def unknown_runtime_status(*, detail_code: str = 'external_worker_unobserved') -> WorkerRuntimeStatusResponse:
    return WorkerRuntimeStatusResponse(
        mode='external_unknown', state='unknown', detail_code=detail_code,
        heartbeat_timeout_seconds=HEARTBEAT_TIMEOUT_SECONDS,
    )


class ManagedWorkerRuntimeStatus:
    """Created once by the supervisor and passed only to its own control child."""

    def __init__(self, *, run_id: str, product_identity: str) -> None:
        if not re.fullmatch(r'[0-9a-f]{32}', run_id):
            raise ValueError('invalid runtime run identity')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,95}\+build\.sha256\.[0-9a-f]{64}', product_identity):
            raise ValueError('invalid runtime build identity')
        self.run_id = run_id
        self.product_identity = product_identity
        # Explicit spawn context also works when tested from a POSIX host.
        self._shared = get_context('spawn').Array('d', (0.0, 0.0, 0.0))

    def publish(self, phase: str, *, worker_pid: int | None = None, now: float | None = None) -> bool:
        if phase not in _PHASES or (worker_pid is not None and (type(worker_pid) is not int or worker_pid <= 0)):
            raise ValueError('invalid runtime observation')
        observed = time.monotonic() if now is None else now
        if not math.isfinite(observed) or observed < 0:
            raise ValueError('invalid runtime clock')
        lock = self._shared.get_lock()
        if not lock.acquire(timeout=.02):
            return False
        try:
            self._shared.get_obj()[:] = (observed, float(_PHASES.index(phase)), float(worker_pid or 0))
        finally:
            lock.release()
        return True

    def snapshot(self, *, expected_run_id: str, expected_product_identity: str,
                 queue_paused: bool = False, now: float | None = None) -> WorkerRuntimeStatusResponse:
        if self.run_id != expected_run_id or self.product_identity != expected_product_identity:
            return unknown_runtime_status(detail_code='runtime_identity_mismatch')
        lock = self._shared.get_lock()
        if not lock.acquire(timeout=.02):
            return unknown_runtime_status(detail_code='runtime_snapshot_unavailable')
        try:
            observed, raw_phase, raw_pid = self._shared.get_obj()[:]
        finally:
            lock.release()
        clock = time.monotonic() if now is None else now
        if (not all(math.isfinite(value) for value in (observed, raw_phase, raw_pid, clock))
                or raw_phase != int(raw_phase) or not 0 <= raw_phase < len(_PHASES)
                or raw_pid != int(raw_pid) or raw_pid < 0 or observed <= 0 or clock < observed):
            return unknown_runtime_status(detail_code='runtime_snapshot_unavailable')
        age = clock - observed
        phase = _PHASES[int(raw_phase)]
        # Only online is a liveness assertion. Other values are recorded
        # lifecycle phases; slow preflight/cleanup must not erase their meaning.
        stale = phase == 'online' and age >= HEARTBEAT_TIMEOUT_SECONDS
        online = phase == 'online' and not stale and raw_pid > 0
        state = 'stale' if stale else ('paused' if online and queue_paused else phase)
        if phase == 'online' and not raw_pid:
            return unknown_runtime_status(detail_code='runtime_snapshot_unavailable')
        return WorkerRuntimeStatusResponse(
            mode='managed_direct', state=state,
            run_id=self.run_id, worker_pid=int(raw_pid) if raw_pid else None,
            heartbeat_age_seconds=round(age, 3),
            heartbeat_timeout_seconds=HEARTBEAT_TIMEOUT_SECONDS,
            network_download_enabled=True if online else (None if stale else False),
            queue_paused=queue_paused if online else None,
            detail_code='heartbeat_expired' if stale else ('ok' if online else phase),
        )

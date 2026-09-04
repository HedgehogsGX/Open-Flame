from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

import video_download_control.local_app as local_app_module
import video_download_control.local_worker_cli as local_worker_module
import video_download_control.worker_pool as worker_pool_module
from video_download_control import __version__
from video_download_control.database import Database
from video_download_control.domain import Platform, SourceType
from video_download_control.runtime_logging import read_recent_runtime_events
from video_download_control.worker import WorkerClaim, WorkerRunResult
from video_download_control.worker_repository import JobLease


class _BlockingTwoPlatformWorker:
    """Attempt boundary fake: the first platform cannot finish until released."""

    def __init__(
        self,
        *,
        claim_enabled: Event | None = None,
        block_second: bool = False,
    ) -> None:
        self.first_started = Event()
        self.second_started = Event()
        self.release_first = Event()
        self.release_second = Event()
        self.stop_requested = Event()
        self.all_finished = Event()
        self.claim_enabled = claim_enabled
        self.block_second = block_second
        self._lock = Lock()
        self._finished = 0
        self._claims = deque(
            self._claim(platform)
            for platform in (Platform.YOUTUBE, Platform.BILIBILI)
        )
        self.job_ids = {claim.lease.job_id for claim in self._claims}

    @staticmethod
    def _claim(platform: Platform) -> WorkerClaim:
        return WorkerClaim(
            lease=JobLease(
                job_id=str(uuid4()),
                attempt_id=str(uuid4()),
                attempt_no=1,
                lease_token="synthetic-concurrency-lease",
                worker_id="local-integration-worker",
                lease_expires_at="2099-01-01T00:00:00.000Z",
                source_item_id=str(uuid4()),
                canonical_url="https://example.invalid/synthetic",
                platform=platform,
                source_type=(
                    SourceType.YOUTUBE_VIDEO
                    if platform == Platform.YOUTUBE
                    else SourceType.BILIBILI_VIDEO
                ),
                adapter="yt_dlp",
                adapter_version="synthetic",
            ),
            cycle_started=perf_counter(),
        )

    def claim_once(
        self,
        *,
        perform_recovery: bool = True,
        excluded_job_ids: frozenset[str] = frozenset(),
    ) -> WorkerClaim | None:
        if self.stop_requested.is_set():
            return None
        if self.claim_enabled is not None:
            assert self.claim_enabled.is_set(), "claim preceded supervisor activation"
        with self._lock:
            if not self._claims:
                return None
            claim = self._claims.popleft()
            assert claim.lease.job_id not in excluded_job_ids
            return claim

    def execute_claimed(self, claim: WorkerClaim) -> WorkerRunResult:
        if claim.lease.platform == Platform.YOUTUBE:
            self.first_started.set()
            if not self.release_first.wait(10):
                raise TimeoutError("test did not release its blocked attempt")
        else:
            self.second_started.set()
            if self.block_second and not self.release_second.wait(10):
                raise TimeoutError("test did not release its second blocked attempt")
        with self._lock:
            self._finished += 1
            if self._finished == 2:
                self.all_finished.set()
        return WorkerRunResult(
            job_id=claim.lease.job_id,
            attempt_id=claim.lease.attempt_id,
            status="ready",
        )

    def run_once(self) -> WorkerRunResult | None:
        claim = self.claim_once()
        return None if claim is None else self.execute_claimed(claim)

    def request_stop(self) -> None:
        self.stop_requested.set()
        self.release_first.set()
        self.release_second.set()


def _start_call(callback: Callable[[], None]) -> tuple[Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            callback()
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=invoke, name="concurrent-worker-integration", daemon=True)
    thread.start()
    return thread, errors


def test_local_worker_drain_starts_second_platform_before_first_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = (tmp_path / "data").resolve()
    Database(data_root / "control.sqlite3").initialize()
    monkeypatch.setenv(local_worker_module.FEATURE_GATE, "1")
    worker = _BlockingTwoPlatformWorker()
    monkeypatch.setattr(
        local_worker_module,
        "build_local_worker",
        lambda *_args, **_kwargs: worker,
    )
    thread, errors = _start_call(
        lambda: local_worker_module.main(
            [
                "--data-root",
                str(data_root),
                "--tool-root",
                str((tmp_path / "tools").resolve()),
                "--allow-direct-network",
                "--drain",
            ]
        )
    )
    try:
        first_started = worker.first_started.wait(5)
        overlapped = first_started and worker.second_started.wait(2)
    finally:
        worker.release_first.set()
        thread.join(5)

    assert not thread.is_alive(), "drain did not finish after releasing both attempts"
    assert not errors, errors
    assert first_started, "drain never dispatched the first attempt"
    assert overlapped, "second platform waited for the first attempt to finish"
    assert worker.all_finished.is_set()
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {item["job_id"] for item in output if "job_id" in item} == worker.job_ids


def test_local_worker_poll_starts_second_platform_before_first_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = (tmp_path / "data").resolve()
    Database(data_root / "control.sqlite3").initialize()
    monkeypatch.setenv(local_worker_module.FEATURE_GATE, "1")
    worker = _BlockingTwoPlatformWorker()
    idle_poll_reached = Event()
    cleanup = Event()

    def stop_after_completed_idle_poll(timeout: float) -> None:
        # Stop only at the resident idle wait, after result callbacks have run.
        # Replacing this module's clock leaves logger/lease/application clocks
        # unchanged, and the actual scheduler and CLI exception path still run.
        if (timeout == 0.1 and worker.all_finished.is_set()) or cleanup.is_set():
            idle_poll_reached.set()
            raise KeyboardInterrupt
        cleanup.wait(timeout)

    monkeypatch.setattr(
        worker_pool_module,
        "time",
        SimpleNamespace(monotonic=monotonic, sleep=stop_after_completed_idle_poll),
    )
    monkeypatch.setattr(
        local_worker_module,
        "build_local_worker",
        lambda *_args, **_kwargs: worker,
    )
    thread, errors = _start_call(
        lambda: local_worker_module.main(
            [
                "--data-root",
                str(data_root),
                "--tool-root",
                str((tmp_path / "tools").resolve()),
                "--allow-direct-network",
                "--poll-interval-seconds",
                "0.1",
            ]
        )
    )
    try:
        first_started = worker.first_started.wait(5)
        overlapped = first_started and worker.second_started.wait(2)
        worker.release_first.set()
        idle_after_completion = idle_poll_reached.wait(5)
    finally:
        cleanup.set()
        worker.release_first.set()
        thread.join(5)

    assert not thread.is_alive(), "resident main did not stop at its idle poll"
    assert not errors, errors
    assert first_started
    assert overlapped, "resident main serialized different-platform attempts"
    assert idle_after_completion, "resident main never returned to polling after work"
    assert worker.all_finished.is_set()
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {item["job_id"] for item in output if "job_id" in item} == worker.job_ids
    assert any(item == {"status": "idle"} for item in output)
    events = read_recent_runtime_events(data_root / "logs")
    assert events[-1]["event"] == "worker.stopped"
    assert events[-1]["reason"] == "keyboard_interrupt"


def test_local_worker_ctrl_c_stops_and_joins_both_active_platform_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = (tmp_path / "data").resolve()
    Database(data_root / "control.sqlite3").initialize()
    monkeypatch.setenv(local_worker_module.FEATURE_GATE, "1")
    worker = _BlockingTwoPlatformWorker(block_second=True)
    interrupt_sent = Event()

    def interrupt_with_both_attempts_running(_timeout: float) -> None:
        assert worker.first_started.wait(5), "first attempt never started"
        assert worker.second_started.wait(5), "second attempt never started"
        assert not worker.release_first.is_set()
        assert not worker.release_second.is_set()
        interrupt_sent.set()
        raise KeyboardInterrupt

    monkeypatch.setattr(
        worker_pool_module,
        "time",
        SimpleNamespace(monotonic=monotonic, sleep=interrupt_with_both_attempts_running),
    )
    monkeypatch.setattr(
        local_worker_module,
        "build_local_worker",
        lambda *_args, **_kwargs: worker,
    )
    thread, errors = _start_call(
        lambda: local_worker_module.main(
            [
                "--data-root",
                str(data_root),
                "--tool-root",
                str((tmp_path / "tools").resolve()),
                "--allow-direct-network",
                "--poll-interval-seconds",
                "0.1",
            ]
        )
    )
    try:
        interrupt_observed = interrupt_sent.wait(6)
        stopped_by_scheduler = worker.stop_requested.wait(2)
        thread.join(5)
        joined_by_main = not thread.is_alive()
    finally:
        worker.release_first.set()
        worker.release_second.set()
        thread.join(5)

    assert not thread.is_alive(), "CLI cleanup left active attempt threads running"
    assert not errors, errors
    assert interrupt_observed, "Ctrl+C injection never reached the coordinator wait"
    assert stopped_by_scheduler, "Ctrl+C did not request active attempt interruption"
    assert joined_by_main, "main did not join its interrupted attempts before return"
    assert worker.all_finished.is_set()
    events = read_recent_runtime_events(data_root / "logs")
    assert not any(event["event"] == "worker.cycle_failed" for event in events)
    assert events[-1]["event"] == "worker.stopped"
    assert events[-1]["reason"] == "keyboard_interrupt"


class _ReadyConnection:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False

    def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


class _SupervisorCommands:
    """Strict launch/claim then EOF, without replacing the child command parser."""

    def __init__(self, *, claim_enabled: Event, stop: Event) -> None:
        self.claim_enabled = claim_enabled
        self.stop = stop
        self.closed = False
        self.received: list[bytes] = []
        self._commands = deque(
            (local_app_module._COMMAND_LAUNCH, local_app_module._COMMAND_CLAIM)
        )

    def poll(self, timeout: float = 0.0) -> bool:
        return bool(self._commands) or self.stop.wait(timeout)

    def recv_bytes(self, maxlength: int) -> bytes:
        if not self._commands:
            assert self.stop.is_set(), "child attempted a blocking empty receive"
            raise EOFError
        command = self._commands.popleft()
        assert len(command) <= maxlength
        self.received.append(command)
        if command == local_app_module._COMMAND_CLAIM:
            self.claim_enabled.set()
        return command

    def close(self) -> None:
        self.closed = True


def test_local_app_child_starts_second_platform_before_first_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = local_app_module.LocalAppConfig(
        app_root=(tmp_path / "app").resolve(),
        allow_direct_network=True,
        open_browser=False,
        poll_interval_seconds=0.1,
    )
    assert config.database_path is not None
    assert config.data_root is not None
    Database(config.database_path).initialize()
    ready_connection = _ReadyConnection()
    claim_enabled = Event()
    stop = Event()
    commands = _SupervisorCommands(claim_enabled=claim_enabled, stop=stop)
    worker = _BlockingTwoPlatformWorker(claim_enabled=claim_enabled)
    run_id = uuid4().hex
    product_identity = f"{__version__}+build.sha256." + "a" * 64
    monkeypatch.setattr(
        local_app_module,
        "_apply_child_environment",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        local_app_module,
        "current_product_identity",
        lambda: product_identity,
    )

    def build_worker(*_args, **kwargs):
        assert kwargs["claim_gate_run_id"] == run_id
        return worker

    monkeypatch.setattr(local_app_module, "build_local_worker", build_worker)
    thread, errors = _start_call(
        lambda: local_app_module._worker_child_main(
            config,
            ready_connection,
            commands,
            run_id,
            product_identity,
        )
    )
    try:
        first_started = worker.first_started.wait(5)
        overlapped = first_started and worker.second_started.wait(2)
    finally:
        stop.set()
        worker.release_first.set()
        thread.join(5)

    assert not thread.is_alive(), "child ignored EOF after releasing its attempts"
    assert not errors, errors
    assert first_started, "child never dispatched its first attempt after claim"
    assert overlapped, "supervised child serialized different-platform attempts"
    assert worker.all_finished.is_set()
    assert commands.received == [
        local_app_module._COMMAND_LAUNCH,
        local_app_module._COMMAND_CLAIM,
    ]
    assert [json.loads(frame)["phase"] for frame in ready_connection.sent] == [
        "worker_preflight_ready",
        "worker_ready",
    ]
    assert ready_connection.closed
    assert commands.closed
    events = read_recent_runtime_events(config.data_root / "logs")
    assert events[-1]["event"] == "worker.stopped"
    assert events[-1]["reason"] == "supervisor_shutdown"

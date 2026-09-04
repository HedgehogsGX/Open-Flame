from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import video_download_control.subprocess_runner as subprocess_runner_module
from video_download_control.subprocess_runner import (
    CommandCancelled,
    CommandProcessError,
    CommandSpec,
    SecureSubprocessRunner,
)

# A Windows venv python.exe is a redirector with a different PID from the
# interpreter. Use the base executable so the stdout handshake identifies the
# exact child whose process handle we check after termination.
PYTHON = Path(getattr(sys, "_base_executable", sys.executable)).resolve()


def runner(**kwargs) -> SecureSubprocessRunner:
    return SecureSubprocessRunner(
        allowed_executable_roots=[PYTHON.parent],
        poll_interval_seconds=0.005,
        **kwargs,
    )


def test_runner_default_stop_events_are_independent() -> None:
    first = runner()
    second = runner()

    first.stop_event.set()

    assert first.stop_event.is_set()
    assert not second.stop_event.is_set()


def test_preset_stop_does_not_launch_or_clear_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = threading.Event()
    stop_event.set()
    command_runner = runner(stop_event=stop_event)
    launches = []

    def unexpected_spawn(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("a stopped runner must not launch a child")

    monkeypatch.setattr(subprocess_runner_module.subprocess, "Popen", unexpected_spawn)
    spec = CommandSpec(
        executable=PYTHON,
        arguments=("-c", "pass"),
        cwd=tmp_path.resolve(),
    )

    for _ in range(2):
        with pytest.raises(CommandCancelled):
            command_runner.run(spec)

    assert command_runner.stop_event is stop_event
    assert stop_event.is_set()
    assert launches == []


def test_shared_stop_cancels_two_running_children_and_closes_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = threading.Event()
    command_runner = runner(stop_event=stop_event)
    real_popen = subprocess.Popen
    launched: list[subprocess.Popen[bytes]] = []
    launched_lock = threading.Lock()
    ready = [threading.Event(), threading.Event()]
    observed_pids: list[int | None] = [None, None]

    def record_spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        with launched_lock:
            launched.append(process)
        return process

    monkeypatch.setattr(subprocess_runner_module.subprocess, "Popen", record_spawn)

    def execute(index: int):
        def observe(line: bytes) -> None:
            observed_pids[index] = int(line)
            ready[index].set()

        return command_runner.run(
            CommandSpec(
                executable=PYTHON,
                arguments=(
                    "-c",
                    "import os, threading; print(os.getpid(), flush=True); "
                    "threading.Event().wait(30)",
                ),
                cwd=tmp_path.resolve(),
                timeout_seconds=15,
                stdout_line_observer=observe,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(execute, index) for index in range(2)]
        try:
            # Both real children must announce readiness before cancellation;
            # this cannot accidentally pass by stopping the second pre-spawn.
            assert all(event.wait(10) for event in ready)
            assert len(set(observed_pids)) == 2
            assert all(process.poll() is None for process in launched)
        finally:
            stop_event.set()
        for future in futures:
            with pytest.raises(CommandCancelled):
                future.result(timeout=10)

    children = [process for process in launched if process.pid in observed_pids]
    assert len(children) == 2
    assert all(process.poll() is not None for process in children)
    assert all(process.stdout is not None and process.stdout.closed for process in children)
    assert all(process.stderr is not None and process.stderr.closed for process in children)
    assert stop_event.is_set()


def test_per_call_cancellation_still_works_with_unset_global_stop(
    tmp_path: Path,
) -> None:
    stop_event = threading.Event()
    call_cancelled = threading.Event()
    observed = []

    def observe(line: bytes) -> None:
        observed.append(line)
        call_cancelled.set()

    with pytest.raises(CommandCancelled):
        runner(stop_event=stop_event).run(
            CommandSpec(
                executable=PYTHON,
                arguments=(
                    "-c",
                    "import threading; print('ready', flush=True); "
                    "threading.Event().wait(30)",
                ),
                cwd=tmp_path.resolve(),
                timeout_seconds=10,
                stdout_line_observer=observe,
            ),
            is_cancelled=call_cancelled.is_set,
        )

    assert observed == [b"ready"]
    assert not stop_event.is_set()


@pytest.mark.parametrize("failed_start", [1, 2])
@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_reader_start_failure_terminates_child_and_closes_all_pipe_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_start: int,
    failure_type: type[BaseException],
) -> None:
    failure = failure_type("synthetic reader startup failure")
    real_popen = subprocess.Popen
    real_start = threading.Thread.start
    launched = []
    reader_threads = []

    def record_spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    def fail_reader_start(thread):
        reader_threads.append(thread)
        if len(reader_threads) == failed_start:
            raise failure
        return real_start(thread)

    monkeypatch.setattr(subprocess_runner_module.subprocess, "Popen", record_spawn)
    monkeypatch.setattr(subprocess_runner_module.threading.Thread, "start", fail_reader_start)
    expected_type = CommandProcessError if failure_type is RuntimeError else failure_type

    try:
        with pytest.raises(expected_type) as caught:
            runner().run(
                CommandSpec(
                    executable=PYTHON,
                    arguments=("-c", "import threading; threading.Event().wait(30)"),
                    cwd=tmp_path.resolve(),
                    timeout_seconds=10,
                )
            )
        if failure_type is RuntimeError:
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
        assert launched
        child = launched[0]
        assert child.poll() is not None
        assert child.stdout is not None and child.stdout.closed
        assert child.stderr is not None and child.stderr.closed
        assert all(not thread.is_alive() for thread in reader_threads)
    finally:
        # Keep a broken baseline safe: the regression must not itself orphan a
        # real child when the runner has not yet acquired its startup guard.
        if launched:
            child = launched[0]
            SecureSubprocessRunner._best_effort_shutdown(child)
            for thread in reader_threads:
                if thread.ident is not None:
                    thread.join(timeout=10)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()


@pytest.mark.parametrize("failure_type", [OSError, CommandCancelled])
def test_stop_does_not_replace_observer_exception_identity(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    stop_event = threading.Event()
    failure = failure_type("observer failure identity")

    def fail_observer(_line: bytes) -> None:
        stop_event.set()
        raise failure

    with pytest.raises(failure_type) as caught:
        runner(stop_event=stop_event).run(
            CommandSpec(
                executable=PYTHON,
                arguments=(
                    "-c",
                    "import threading; print('ready', flush=True); "
                    "threading.Event().wait(30)",
                ),
                cwd=tmp_path.resolve(),
                timeout_seconds=10,
                stdout_line_observer=fail_observer,
            )
        )

    assert caught.value is failure


@pytest.mark.parametrize("failure_type", [OSError, CommandCancelled])
def test_shared_stop_preserves_per_call_callback_exception_identity(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    failure = failure_type("cancellation callback failure identity")
    stop_event = threading.Event()

    def fail_cancellation() -> bool:
        raise failure

    with pytest.raises(failure_type) as caught:
        runner(stop_event=stop_event).run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "import threading; threading.Event().wait(30)"),
                cwd=tmp_path.resolve(),
                timeout_seconds=10,
            ),
            is_cancelled=fail_cancellation,
        )

    assert caught.value is failure
    assert not stop_event.is_set()

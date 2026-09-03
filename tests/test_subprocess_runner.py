from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from video_download_control.runtime_logging import RuntimeLogConfig, RuntimeLogger
from video_download_control.subprocess_runner import (
    CommandCancelled,
    CommandOutputLimitExceeded,
    CommandSpec,
    CommandTimedOut,
    SecureSubprocessRunner,
    SubprocessPolicyError,
)

PYTHON = Path(sys.executable).resolve()
RUNTIME_LOG_BASE_FIELDS = {
    "schema_version",
    "timestamp",
    "level",
    "event",
    "component",
    "run_id",
    "event_id",
    "pid",
    "sequence",
    "thread_id",
}


def runner(**kwargs) -> SecureSubprocessRunner:
    return SecureSubprocessRunner(
        allowed_executable_roots=[PYTHON.parent],
        poll_interval_seconds=0.005,
        **kwargs,
    )


def runtime_logger(tmp_path: Path, *, run_id: str) -> RuntimeLogger:
    return RuntimeLogger(
        component="candidate-worker",
        config=RuntimeLogConfig(directory=(tmp_path / "runtime-logs").resolve()),
        run_id=run_id,
    )


def runtime_events(logger: RuntimeLogger) -> dict[str, dict[str, object]]:
    events = logger.recent_events(limit=100)
    assert len({event["event"] for event in events}) == len(events)
    return {str(event["event"]): event for event in events}


def test_runner_executes_absolute_binary_with_clean_allowlisted_environment(
    tmp_path: Path,
) -> None:
    result = runner(allowed_environment_keys=frozenset({"VDC_TEST_SAFE"})).run(
        CommandSpec(
            executable=PYTHON,
            arguments=(
                "-c",
                "import json, os; print(json.dumps({'safe': os.getenv('VDC_TEST_SAFE'), 'proxy': os.getenv('HTTP_PROXY')}))",
            ),
            cwd=tmp_path.resolve(),
            environment={"VDC_TEST_SAFE": "yes"},
        )
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"safe": "yes", "proxy": None}
    assert result.stderr == b""


def test_runner_rejects_relative_executable_nul_and_unapproved_environment(
    tmp_path: Path,
) -> None:
    with pytest.raises(SubprocessPolicyError, match="absolute"):
        runner().run(CommandSpec(executable=Path("python")))
    with pytest.raises(SubprocessPolicyError, match="NUL"):
        runner().run(CommandSpec(executable=PYTHON, arguments=("bad\x00argument",)))
    with pytest.raises(SubprocessPolicyError, match="forbidden"):
        runner(allowed_environment_keys=frozenset({"HTTP_PROXY"})).run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "pass"),
                cwd=tmp_path.resolve(),
                environment={"HTTP_PROXY": "http://secret.example"},
            )
        )

    with pytest.raises(SubprocessPolicyError, match="forbidden"):
        runner(allowed_environment_keys=frozenset({"LD_PRELOAD"})).run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "pass"),
                cwd=tmp_path.resolve(),
                environment={"LD_PRELOAD": "malicious-library"},
            )
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_runner_rejects_non_finite_deadlines(tmp_path: Path, value: float) -> None:
    with pytest.raises(SubprocessPolicyError, match="finite positive"):
        runner().run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "pass"),
                cwd=tmp_path.resolve(),
                timeout_seconds=value,
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stdout_limit_bytes", float("nan")),
        ("stdout_limit_bytes", float("inf")),
        ("stderr_limit_bytes", float("-inf")),
    ],
)
def test_runner_rejects_non_integer_output_limits(
    tmp_path: Path, field: str, value: float
) -> None:
    with pytest.raises(SubprocessPolicyError, match="non-negative integers"):
        runner().run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "pass"),
                cwd=tmp_path.resolve(),
                **{field: value},
            )
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_runner_rejects_non_finite_poll_interval(value: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        SecureSubprocessRunner(poll_interval_seconds=value)


def test_runner_bounds_stdout_and_stderr(tmp_path: Path) -> None:
    with pytest.raises(CommandOutputLimitExceeded):
        runner().run(
            CommandSpec(
                executable=PYTHON,
                arguments=(
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 100000); sys.stdout.flush()",
                ),
                cwd=tmp_path.resolve(),
                stdout_limit_bytes=128,
            )
        )


def test_runner_honors_deadline_and_cancellation(tmp_path: Path) -> None:
    with pytest.raises(CommandTimedOut):
        runner().run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "import time; time.sleep(30)"),
                cwd=tmp_path.resolve(),
                timeout_seconds=0.05,
            )
        )

    with pytest.raises(CommandCancelled):
        runner().run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "import time; time.sleep(30)"),
                cwd=tmp_path.resolve(),
            ),
            is_cancelled=lambda: True,
        )


def test_timeout_terminates_spawned_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child-finished.txt"
    child_code = (
        "import pathlib, time; time.sleep(0.4); "
        f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    with pytest.raises(CommandTimedOut):
        runner().run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", parent_code),
                cwd=tmp_path.resolve(),
                timeout_seconds=0.08,
            )
        )
    time.sleep(0.55)
    assert not marker.exists()


def test_cancellation_callback_error_still_terminates_process(tmp_path: Path) -> None:
    marker = tmp_path / "process-finished.txt"
    code = (
        "import pathlib, time; time.sleep(0.3); "
        f"pathlib.Path({str(marker)!r}).write_text('orphan', encoding='utf-8')"
    )

    def broken_callback() -> bool:
        raise RuntimeError("lost cancellation backend")

    with pytest.raises(RuntimeError, match="lost cancellation backend"):
        runner().run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", code),
                cwd=tmp_path.resolve(),
            ),
            is_cancelled=broken_callback,
        )
    time.sleep(0.4)
    assert not marker.exists()


def test_successful_subprocess_log_contains_only_bounded_execution_metadata(
    tmp_path: Path,
) -> None:
    argument_sentinel = "SENTINEL_ARG_MUST_NOT_ENTER_RUNTIME_LOG"
    environment_sentinel = "SENTINEL_ENV_MUST_NOT_ENTER_RUNTIME_LOG"
    stdout_sentinel = "SENTINEL_STDOUT_MUST_NOT_ENTER_RUNTIME_LOG"
    stderr_sentinel = "SENTINEL_STDERR_MUST_NOT_ENTER_RUNTIME_LOG"
    cwd_sentinel = "SENTINEL_CWD_MUST_NOT_ENTER_RUNTIME_LOG"
    cwd = (tmp_path / cwd_sentinel).resolve()
    cwd.mkdir()
    script = (
        "import os, sys; "
        f"sys.stdout.write({stdout_sentinel!r} + os.environ['VDC_TEST_SAFE']); "
        f"sys.stderr.write({stderr_sentinel!r}); "
        "sys.stdout.flush(); sys.stderr.flush()"
    )
    logger = runtime_logger(tmp_path, run_id="subprocess-success-run")

    result = runner(
        allowed_environment_keys=frozenset({"VDC_TEST_SAFE"}),
        runtime_logger=logger,
    ).run(
        CommandSpec(
            executable=PYTHON,
            arguments=("-c", script, argument_sentinel),
            cwd=cwd,
            environment={"VDC_TEST_SAFE": environment_sentinel},
        )
    )

    events = runtime_events(logger)
    started = events["subprocess.started"]
    completed = events["subprocess.completed"]
    assert result.returncode == 0
    assert result.stdout == (stdout_sentinel + environment_sentinel).encode()
    assert result.stderr == stderr_sentinel.encode()
    assert set(started) == RUNTIME_LOG_BASE_FIELDS | {
        "subprocess_id",
        "executable",
        "argument_count",
        "timeout_seconds",
    }
    assert set(completed) == RUNTIME_LOG_BASE_FIELDS | {
        "subprocess_id",
        "executable",
        "argument_count",
        "return_code",
        "stdout_bytes",
        "stderr_bytes",
        "duration_ms",
    }
    assert started["subprocess_id"] == completed["subprocess_id"]
    assert started["executable"] == completed["executable"] == PYTHON.name.lower()
    assert started["argument_count"] == completed["argument_count"] == 3
    assert completed["return_code"] == result.returncode
    assert completed["stdout_bytes"] == len(result.stdout)
    assert completed["stderr_bytes"] == len(result.stderr)
    assert isinstance(completed["duration_ms"], float)
    assert completed["duration_ms"] >= 0
    raw_log = logger.path.read_text(encoding="utf-8")
    for private_value in (
        argument_sentinel,
        environment_sentinel,
        stdout_sentinel,
        stderr_sentinel,
        cwd_sentinel,
        script,
        "VDC_TEST_SAFE",
        str(PYTHON),
        str(cwd),
    ):
        assert private_value not in raw_log


def test_subprocess_policy_rejection_logs_no_command_or_environment_material(
    tmp_path: Path,
) -> None:
    argument_sentinel = "SENTINEL_REJECTED_ARG"
    environment_sentinel = "SENTINEL_REJECTED_ENV"
    cwd_sentinel = "SENTINEL_REJECTED_CWD"
    cwd = (tmp_path / cwd_sentinel).resolve()
    cwd.mkdir()
    logger = runtime_logger(tmp_path, run_id="subprocess-policy-run")

    with pytest.raises(SubprocessPolicyError, match="forbidden"):
        runner(
            allowed_environment_keys=frozenset({"HTTP_PROXY"}),
            runtime_logger=logger,
        ).run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "pass", argument_sentinel),
                cwd=cwd,
                environment={"HTTP_PROXY": environment_sentinel},
            )
        )

    events = runtime_events(logger)
    failed = events["subprocess.failed"]
    assert set(failed) == RUNTIME_LOG_BASE_FIELDS | {
        "subprocess_id",
        "duration_ms",
        "exception_type",
        "failure_site",
    }
    assert failed["level"] == "ERROR"
    assert failed["exception_type"] == "SubprocessPolicyError"
    assert failed["failure_site"] == "policy_validation"
    assert isinstance(failed["duration_ms"], float)
    assert failed["duration_ms"] >= 0
    raw_log = logger.path.read_text(encoding="utf-8")
    for private_value in (
        argument_sentinel,
        environment_sentinel,
        cwd_sentinel,
        "HTTP_PROXY",
        str(PYTHON),
        str(cwd),
    ):
        assert private_value not in raw_log


def test_timed_out_subprocess_log_omits_argv_cwd_and_captured_output(
    tmp_path: Path,
) -> None:
    output_sentinel = "SENTINEL_TIMEOUT_OUTPUT"
    cwd_sentinel = "SENTINEL_TIMEOUT_CWD"
    cwd = (tmp_path / cwd_sentinel).resolve()
    cwd.mkdir()
    script = (
        "import sys, time; "
        f"sys.stdout.write({output_sentinel!r}); sys.stdout.flush(); "
        "time.sleep(30)"
    )
    logger = runtime_logger(tmp_path, run_id="subprocess-timeout-run")

    with pytest.raises(CommandTimedOut):
        runner(runtime_logger=logger).run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", script),
                cwd=cwd,
                timeout_seconds=0.05,
            )
        )

    events = runtime_events(logger)
    started = events["subprocess.started"]
    failed = events["subprocess.failed"]
    assert set(failed) == RUNTIME_LOG_BASE_FIELDS | {
        "subprocess_id",
        "executable",
        "argument_count",
        "return_code",
        "duration_ms",
        "exception_type",
        "failure_site",
    }
    assert started["subprocess_id"] == failed["subprocess_id"]
    assert failed["level"] == "WARNING"
    assert failed["executable"] == PYTHON.name.lower()
    assert failed["argument_count"] == 2
    assert isinstance(failed["return_code"], int)
    assert isinstance(failed["duration_ms"], float)
    assert failed["duration_ms"] >= 0
    assert failed["exception_type"] == "CommandTimedOut"
    assert failed["failure_site"] == "process_wait"
    raw_log = logger.path.read_text(encoding="utf-8")
    for private_value in (output_sentinel, cwd_sentinel, script, str(PYTHON), str(cwd)):
        assert private_value not in raw_log


def test_output_limit_failure_log_omits_argv_cwd_stdout_and_stderr(
    tmp_path: Path,
) -> None:
    stdout_sentinel = "SENTINEL_LIMIT_STDOUT"
    stderr_sentinel = "SENTINEL_LIMIT_STDERR"
    cwd_sentinel = "SENTINEL_LIMIT_CWD"
    cwd = (tmp_path / cwd_sentinel).resolve()
    cwd.mkdir()
    script = (
        "import sys; "
        f"sys.stdout.write({stdout_sentinel!r} * 100); "
        f"sys.stderr.write({stderr_sentinel!r}); "
        "sys.stdout.flush(); sys.stderr.flush()"
    )
    logger = runtime_logger(tmp_path, run_id="subprocess-limit-run")

    with pytest.raises(CommandOutputLimitExceeded):
        runner(runtime_logger=logger).run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", script),
                cwd=cwd,
                stdout_limit_bytes=32,
                stderr_limit_bytes=32,
            )
        )

    events = runtime_events(logger)
    started = events["subprocess.started"]
    failed = events["subprocess.failed"]
    assert started["subprocess_id"] == failed["subprocess_id"]
    assert failed["level"] == "WARNING"
    assert failed["executable"] == PYTHON.name.lower()
    assert failed["argument_count"] == 2
    assert isinstance(failed["return_code"], int)
    assert isinstance(failed["duration_ms"], float)
    assert failed["duration_ms"] >= 0
    assert failed["exception_type"] == "CommandOutputLimitExceeded"
    assert failed["failure_site"] == "process_wait"
    assert "stdout_bytes" not in failed
    assert "stderr_bytes" not in failed
    raw_log = logger.path.read_text(encoding="utf-8")
    for private_value in (
        stdout_sentinel,
        stderr_sentinel,
        cwd_sentinel,
        script,
        str(PYTHON),
        str(cwd),
    ):
        assert private_value not in raw_log


def test_dynamic_callback_exception_name_and_message_are_collapsed_in_log(
    tmp_path: Path,
) -> None:
    class_sentinel = "SENTINEL_DYNAMIC_EXCEPTION_CLASS"
    message_sentinel = "SENTINEL_DYNAMIC_EXCEPTION_MESSAGE"
    dynamic_failure = type(class_sentinel, (Exception,), {})
    logger = runtime_logger(tmp_path, run_id="subprocess-dynamic-error-run")

    def broken_callback() -> bool:
        raise dynamic_failure(message_sentinel)

    with pytest.raises(dynamic_failure, match=message_sentinel):
        runner(runtime_logger=logger).run(
            CommandSpec(
                executable=PYTHON,
                arguments=("-c", "import time; time.sleep(30)"),
                cwd=tmp_path.resolve(),
            ),
            is_cancelled=broken_callback,
        )

    events = runtime_events(logger)
    failed = events["subprocess.failed"]
    assert failed["level"] == "ERROR"
    assert failed["exception_type"] == "UnhandledException"
    assert failed["failure_site"] == "process_wait"
    raw_log = logger.path.read_text(encoding="utf-8")
    assert class_sentinel not in raw_log
    assert message_sentinel not in raw_log

"""Isolated real-CLI coverage for persistent, private failure diagnostics."""

from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import video_download_control.local_app_cli as cli_module
from video_download_control.local_app import LocalAppError
from video_download_control import startup_diagnostics as diagnostics
from video_download_control.runtime_logging import (
    LOCAL_FAILURE_DIAGNOSTIC_CONTEXT,
    RuntimeLogConfig,
    RuntimeLogger,
    read_recent_runtime_events,
)


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows application")
ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "SYNTHETIC-PRIVATE-DIAGNOSTIC-ARGUMENT"


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    result = tmp_path / "profile"
    result.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(result))
    return result


def _cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(ROOT / "src"),
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return subprocess.run(
        [sys.executable, "-m", "video_download_control.local_app_cli", *arguments],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _records(profile: Path) -> list[dict[str, object]]:
    directory = profile / "Open-Flame" / "diagnostics"
    records = []
    for path in directory.glob("runtime-launch-diagnostics.jsonl*"):
        text = path.read_text(encoding="utf-8")
        assert PRIVATE not in text
        assert str(profile) not in text
        records.extend(json.loads(line) for line in text.splitlines())
    return records


@pytest.mark.parametrize("arguments", ((f"--{PRIVATE}",), ("--port", PRIVATE)))
def test_real_invalid_arguments_create_one_safe_diagnostic(
    profile: Path, arguments: tuple[str, ...]
) -> None:
    result = _cli(*arguments)
    assert result.returncode == 2
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert PRIVATE not in result.stderr
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "invalid_arguments"
    assert payload["failure_site"] == "argument_parsing"
    assert payload["log_status"] == "not_started"
    assert payload["diagnostic_status"] == "saved"
    records = _records(profile)
    assert len(records) == 1
    assert records[0]["diagnostic_code"] == "invalid_arguments"
    assert records[0]["failure_site"] == "argument_parsing"
    assert not list(profile.rglob("*.sqlite3"))


def test_help_and_success_do_not_create_failure_diagnostics(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _cli("--help")
    assert result.returncode == 0
    assert result.stderr == ""
    assert not (profile / "Open-Flame").exists()
    monkeypatch.setattr(cli_module, "run_local_app", lambda _config: 0)
    assert cli_module.main(["--allow-direct-network"]) == 0
    assert not (profile / "Open-Flame").exists()


def test_unknown_runtime_failure_is_persisted_without_guessing_startup(
    profile: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_config) -> int:
        raise LocalAppError(f"already ready; worker failed: {PRIVATE}")

    monkeypatch.setattr(cli_module, "run_local_app", fail)
    assert cli_module.main(["--allow-direct-network"]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["failure_site"] == "unknown"
    assert payload["log_status"] == "unknown"
    assert payload["diagnostic_status"] == "saved"
    records = _records(profile)
    assert len(records) == 1
    assert records[0]["failure_site"] == "unknown"
    assert records[0]["log_status"] == "unknown"


def test_unwritable_diagnostic_directory_preserves_original_exit_and_safe_json(
    profile: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = profile / "Open-Flame"
    blocker.write_text(PRIVATE, encoding="utf-8")

    def fail(_config) -> int:
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(cli_module, "run_local_app", fail)
    assert cli_module.main(["--allow-direct-network"]) == 70
    captured = capsys.readouterr()
    assert PRIVATE not in captured.err
    payload = json.loads(captured.err)
    assert payload["error_code"] == "internal_error"
    assert payload["diagnostic_status"] == "unavailable"
    assert blocker.read_text(encoding="utf-8") == PRIVATE


def test_all_codes_have_one_closed_context_and_private_record(profile: Path) -> None:
    assert {code.value for code in diagnostics.DiagnosticCode} == set(
        LOCAL_FAILURE_DIAGNOSTIC_CONTEXT
    )
    for code in diagnostics.DiagnosticCode:
        payload = diagnostics.build_failure_payload(code)
        assert diagnostics.persist_diagnostic(code)
        assert payload["error_code"] == code.value
    records = _records(profile)
    assert {record["diagnostic_code"] for record in records} == set(
        LOCAL_FAILURE_DIAGNOSTIC_CONTEXT
    )
    assert all(record["event"] == "local_app.failure_reported" for record in records)
    assert all(record["component"] == "launch-diagnostics" for record in records)


@pytest.mark.parametrize("value", (PRIVATE, {"error_code": PRIVATE}, None, 2))
def test_untrusted_values_never_cross_the_record_boundary(profile: Path, value: object) -> None:
    assert diagnostics.persist_diagnostic(value) is False
    with pytest.raises(ValueError, match="^invalid diagnostic code$"):
        diagnostics.build_failure_payload(value)
    assert not (profile / "Open-Flame").exists()


@pytest.mark.parametrize("value", (None, "relative-profile", "C:\\", "\\\\host\\share"))
def test_missing_or_unsafe_profile_never_falls_back_to_cwd(
    profile: Path, value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(profile)
    if value is None:
        monkeypatch.delenv("LOCALAPPDATA")
    else:
        monkeypatch.setenv("LOCALAPPDATA", value)
    assert not diagnostics.persist_diagnostic(diagnostics.DiagnosticCode.INVALID_ARGUMENTS)
    assert not list(profile.iterdir())


def test_rotation_is_size_bounded_and_reuses_a_fixed_file_family(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "_DIAGNOSTIC_MAX_BYTES", 1024)
    monkeypatch.setattr(diagnostics, "_DIAGNOSTIC_BACKUP_COUNT", 2)
    for _ in range(24):
        assert diagnostics.persist_diagnostic(diagnostics.DiagnosticCode.INVALID_ARGUMENTS)
    directory = profile / "Open-Flame" / "diagnostics"
    files = sorted(path.name for path in directory.iterdir())
    assert files == [
        ".launch-diagnostics.lock",
        "runtime-launch-diagnostics.jsonl",
        "runtime-launch-diagnostics.jsonl.1",
        "runtime-launch-diagnostics.jsonl.2",
    ]
    assert all(path.stat().st_size <= 1024 for path in directory.iterdir())
    records = _records(profile)
    assert 0 < len(records) < 24
    assert len({record["event_id"] for record in records}) == len(records)
    assert len(read_recent_runtime_events(directory, backup_count=2)) == len(records)


@pytest.mark.parametrize("rotate", (False, True))
def test_parallel_process_writers_have_complete_noninterleaved_records(
    profile: Path, rotate: bool
) -> None:
    program = (
        "from video_download_control.startup_diagnostics import DiagnosticCode, persist_diagnostic; "
        "assert all(persist_diagnostic(DiagnosticCode.INVALID_ARGUMENTS) for _ in range(12))"
    )
    if rotate:
        program = (
            "import video_download_control.startup_diagnostics as diagnostics; "
            "diagnostics._DIAGNOSTIC_MAX_BYTES = 2048; "
        ) + program
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")

    def run(_: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", program],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(run, range(6)))
    assert all(result.returncode == 0 and result.stderr == "" for result in results)
    records = _records(profile)
    if rotate:
        assert 0 < len(records) < 72
        directory = profile / "Open-Flame" / "diagnostics"
        assert len(list(directory.glob("*.jsonl*"))) == 4
        assert all(path.stat().st_size <= 2048 for path in directory.iterdir())
    else:
        assert len(records) == 72
        assert len({record["pid"] for record in records}) == 6
    assert len({record["event_id"] for record in records}) == len(records)


def test_unavailable_writer_does_not_change_guidance_or_leak_exception(
    profile: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_arguments, **_kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(diagnostics, "RuntimeLogger", fail)
    payload = diagnostics.emit_failure(diagnostics.DiagnosticCode.LOCAL_PORT_UNAVAILABLE)
    assert payload["error_code"] == "local_port_unavailable"
    assert payload["diagnostic_status"] == "unavailable"
    assert payload["log_status"] == "not_started"
    captured = capsys.readouterr()
    assert PRIVATE not in captured.err
    assert json.loads(captured.err) == payload


def test_dependency_failure_can_be_persisted_without_site_packages(profile: Path) -> None:
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-S", "-c",
            "from video_download_control.startup_diagnostics import DiagnosticCode, emit_failure; "
            "emit_failure(DiagnosticCode.LOCAL_DEPENDENCIES_UNAVAILABLE)",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0
    assert json.loads(result.stderr)["diagnostic_status"] == "saved"
    assert _records(profile)[0]["diagnostic_code"] == "local_dependencies_unavailable"


def test_mismatched_code_and_site_is_rejected_by_runtime_record_contract(profile: Path) -> None:
    logger = RuntimeLogger(
        component="launch-diagnostics",
        config=RuntimeLogConfig(directory=profile / "logs"),
    )
    assert not logger.emit(
        "local_app.failure_reported",
        app_version="0.24.3",
        diagnostic_code="local_app_failed",
        failure_site="port_reservation",
        log_status="not_started",
    )
    assert logger.recent_events()[0]["event"] == "runtime_log.event_rejected"


def test_diagnostic_sites_do_not_expand_other_runtime_event_contracts(profile: Path) -> None:
    logger = RuntimeLogger(
        component="local-worker",
        config=RuntimeLogConfig(directory=profile / "logs"),
    )
    assert not logger.emit("worker.cleanup_failed", failure_site="unknown")
    assert logger.recent_events()[0]["event"] == "runtime_log.event_rejected"


def test_busy_cross_process_writer_times_out_without_replacing_error(profile: Path) -> None:
    import msvcrt

    assert diagnostics.persist_diagnostic(diagnostics.DiagnosticCode.LOCAL_APP_FAILED)
    lock = profile / "Open-Flame" / "diagnostics" / ".launch-diagnostics.lock"
    with lock.open("r+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        started = time.monotonic()
        try:
            result = _cli(f"--{PRIVATE}")
        finally:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    assert time.monotonic() - started < 10
    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "invalid_arguments"
    assert payload["diagnostic_status"] == "unavailable"
    assert PRIVATE not in result.stderr
    assert len(_records(profile)) == 1
    assert diagnostics.persist_diagnostic(diagnostics.DiagnosticCode.INVALID_ARGUMENTS)
    assert len(_records(profile)) == 2


def test_shared_lock_file_cannot_overwrite_or_lock_an_unrelated_target(profile: Path) -> None:
    directory = profile / "Open-Flame" / "diagnostics"
    directory.mkdir(parents=True)
    target = profile / "unrelated.txt"
    target.write_text(PRIVATE, encoding="utf-8")
    lock = directory / ".launch-diagnostics.lock"
    os.link(target, lock)
    assert not diagnostics.persist_diagnostic(diagnostics.DiagnosticCode.INVALID_ARGUMENTS)
    assert target.read_text(encoding="utf-8") == PRIVATE
    assert not _records(profile)


def test_descriptor_close_failure_cannot_leave_the_thread_lock_held(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = profile / "Open-Flame" / "diagnostics"
    directory.mkdir(parents=True)
    real_close = os.close

    def failing_close(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError(PRIVATE)

    monkeypatch.setattr(diagnostics.os, "close", failing_close)
    try:
        with pytest.raises(OSError):
            with diagnostics._exclusive_diagnostic_writer(directory):
                pass
        assert not diagnostics._THREAD_LOCK.locked()
    finally:
        # Preserve independent tests even while exercising the red regression.
        if diagnostics._THREAD_LOCK.locked():
            diagnostics._THREAD_LOCK.release()


@pytest.mark.parametrize("encoding", ("cp1252", "utf-8"))
def test_non_unicode_stderr_emits_one_safe_json_without_reclassifying_failure(
    profile: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding=encoding, errors="strict")
    monkeypatch.setattr(diagnostics.sys, "stderr", stream)

    def fail(_config) -> int:
        raise LocalAppError(PRIVATE)

    monkeypatch.setattr(cli_module, "run_local_app", fail)
    try:
        result = cli_module.main(["--allow-direct-network"])
        stream.flush()
        captured = output.getvalue().decode(encoding)
        assert stream.encoding == encoding
        assert result == 2
        assert len(captured.splitlines()) == 1
        assert PRIVATE not in captured
        payload = json.loads(captured)
        assert payload["error_code"] == "local_app_failed"
        assert payload["diagnostic_status"] == "saved"
        assert payload["next_step"] == diagnostics.build_failure_payload(
            diagnostics.DiagnosticCode.LOCAL_APP_FAILED
        )["next_step"]
        records = _records(profile)
        assert len(records) == 1
        assert records[0]["diagnostic_code"] == "local_app_failed"
    finally:
        stream.detach()


def test_all_fixed_codes_survive_strict_legacy_encoding_without_extra_records(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
    monkeypatch.setattr(diagnostics.sys, "stderr", stream)
    try:
        for code in diagnostics.DiagnosticCode:
            diagnostics.emit_failure(code)
        stream.flush()
        payloads = [json.loads(line) for line in output.getvalue().decode("ascii").splitlines()]
        assert [payload["error_code"] for payload in payloads] == [
            code.value for code in diagnostics.DiagnosticCode
        ]
        records = _records(profile)
        assert len(records) == len(payloads) == len(diagnostics.DiagnosticCode)
        assert sorted(record["diagnostic_code"] for record in records) == sorted(
            payload["error_code"] for payload in payloads
        )
    finally:
        stream.detach()


@pytest.mark.parametrize(
    ("name", "site"),
    (
        ("SETUP_PREREQUISITE_UNAVAILABLE", "setup_preflight"),
        ("SETUP_ENVIRONMENT_UNAVAILABLE", "setup_environment"),
        ("SETUP_DEPENDENCIES_FAILED", "setup_dependencies"),
        ("SETUP_TOOLCHAIN_FAILED", "setup_toolchain"),
        ("SETUP_BUSY", "setup_lock"),
        ("SETUP_INTERRUPTED", "setup_install"),
    ),
)
def test_setup_failure_has_fixed_context_guidance_and_one_private_record(
    profile: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    site: str,
) -> None:
    assert name in diagnostics.DiagnosticCode.__members__
    code = diagnostics.DiagnosticCode[name]
    payload = diagnostics.emit_failure(code)
    assert payload["error_code"] == name.lower()
    assert payload["failure_site"] == site
    assert payload["log_status"] == "not_started"
    assert payload["diagnostic_status"] == "saved"
    assert "Setup-Open-Flame.cmd" in payload["next_step"]
    assert "README" in payload["next_step"]
    if name == "SETUP_ENVIRONMENT_UNAVAILABLE":
        assert "不会覆盖未知环境" in payload["next_step"]
    if name == "SETUP_INTERRUPTED":
        assert "已有数据保留" in payload["next_step"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert json.loads(captured.err) == payload
    records = _records(profile)
    assert len(records) == 1
    assert records[0]["diagnostic_code"] == code.value
    assert records[0]["failure_site"] == site
    assert records[0]["log_status"] == "not_started"

    logger = RuntimeLogger(
        component="local-worker", config=RuntimeLogConfig(directory=profile / "logs")
    )
    assert not logger.emit("worker.cleanup_failed", failure_site=site)
    assert not logger.emit(
        "local_app.failure_reported",
        app_version="0.24.3",
        diagnostic_code="local_app_failed",
        failure_site=site,
        log_status="not_started",
    )

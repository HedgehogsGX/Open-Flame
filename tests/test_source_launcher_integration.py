"""Owned, hidden Windows source-launcher checks with no media or browser calls."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from video_download_control.windows_job import WindowsKillOnCloseJob


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows source launcher")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_MARKER = "SYNTHETIC-PRIVATE-SOURCE-LAUNCHER"
DIAGNOSTIC_RELATIVE_PATH = Path("Open-Flame/diagnostics/runtime-launch-diagnostics.jsonl")
_requires_checkout_python = pytest.mark.skipif(
    not os.path.lexists(REPOSITORY_ROOT / ".venv/Scripts/python.exe"),
    reason="real source-launcher integration requires the checkout Windows venv",
)

# The helper cannot start CMD until the parent assigns its held process to a
# kill-on-close Job. There is no pending stdin read when the real app spawns.
# Even a failed or timed-out launcher can therefore leave no unowned test tree.
_OWNED_COMMAND_HELPER = (
    "import subprocess,sys; "
    "ready=sys.stdin.buffer.read(1); "
    "sys.exit(71 if ready != b'G' else subprocess.call("
    "sys.argv[1],stdin=subprocess.DEVNULL,shell=False,"
    "creationflags=subprocess.CREATE_NO_WINDOW))"
)


def _isolated_environment(profile: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("VDC_")
        and key.upper()
        not in {"LOCALAPPDATA", "PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    }
    environment.update(
        LOCALAPPDATA=str(profile),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
    )
    return environment


def _quoted_argument(value: str) -> str:
    # Test inputs are closed synthetic values, not arbitrary shell commands.
    if any(character in value for character in ('"', "%", "\r", "\n")):
        raise AssertionError("invalid synthetic launcher argument")
    return f'"{value}"'


def _run_launcher(
    repository_root: Path,
    *,
    cwd: Path,
    profile: Path,
    arguments: tuple[str, ...],
    timeout: float = 70,
    released_port: int | None = None,
) -> subprocess.CompletedProcess[str]:
    launcher = repository_root / "Start-Open-Flame.cmd"
    if not launcher.is_file():
        pytest.fail("root Windows source launcher is not implemented", pytrace=False)
    command_processor = Path(os.environ["SYSTEMROOT"]) / "System32/cmd.exe"
    command = (
        f'{_quoted_argument(str(command_processor))} /d /v:off /s /c "'
        + " ".join(_quoted_argument(value) for value in (str(launcher), *arguments))
        + '"'
    )
    environment = _isolated_environment(profile)
    with WindowsKillOnCloseJob() as ownership:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _OWNED_COMMAND_HELPER, command],
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            ownership.assign_pid(process.pid)
            try:
                stdout, stderr = process.communicate(input=b"G", timeout=timeout)
            except subprocess.TimeoutExpired:
                ownership.close()
                process.communicate(timeout=10)
                pytest.fail("owned source launcher exceeded its bounded deadline", pytrace=False)
            if released_port is not None:
                # Check natural release before the ownership safety net closes.
                _assert_port_released(released_port)
        finally:
            ownership.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    # Never put the command line or inherited environment in assertion output.
    return subprocess.CompletedProcess(
        "owned-source-launcher",
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _assert_port_released(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))


@pytest.fixture
def isolated_paths(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / f"{PRIVATE_MARKER}-profile"
    profile.mkdir()
    cwd = tmp_path / "外部 工作目录!"
    cwd.mkdir()
    return profile, cwd


@_requires_checkout_python
@pytest.mark.skipif(
    not os.path.lexists(REPOSITORY_ROOT / "runtime-tools/windows-x64"),
    reason="real startup smoke requires the separately installed ignored tool bundle",
)
def test_source_launcher_check_from_other_cwd_uses_local_tools_and_default_app_root(
    isolated_paths: tuple[Path, Path],
) -> None:
    profile, cwd = isolated_paths
    port = _available_port()
    completed = _run_launcher(
        REPOSITORY_ROOT,
        cwd=cwd,
        profile=profile,
        arguments=(
            "--check",
            "--no-open-browser",
            "--port",
            str(port),
            "--startup-timeout-seconds",
            "40",
            "--shutdown-timeout-seconds",
            "10",
        ),
        released_port=port,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    # An informational desktop banner is allowed; the CLI result stays JSON.
    results = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.startswith("{")
    ]
    assert {"status": "checked"} in results
    app_root = profile / "Open-Flame/video-download-control"
    assert (app_root / "data/control.sqlite3").is_file()
    assert (app_root / "data/logs/runtime-local-app.jsonl").is_file()
    app_events = [
        json.loads(line)
        for line in (app_root / "data/logs/runtime-local-app.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(event["event"] == "local_app.stopped" for event in app_events)
    assert not (cwd / "data").exists()
    assert not (profile / DIAGNOSTIC_RELATIVE_PATH).exists()
    _assert_port_released(port)


def _safe_failure(
    completed: subprocess.CompletedProcess[str],
    *,
    profile: Path,
    app_root: Path,
    error_code: str,
) -> None:
    assert completed.returncode == 2
    payloads = [
        json.loads(line)
        for line in completed.stderr.splitlines()
        if line.startswith("{")
    ]
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["status"] == "error"
    assert payload["error_code"] == error_code
    assert payload["diagnostic_status"] == "saved"
    diagnostic_path = profile / DIAGNOSTIC_RELATIVE_PATH
    assert diagnostic_path.is_file()
    diagnostic_text = diagnostic_path.read_text(encoding="utf-8")
    for text in (completed.stderr, diagnostic_text):
        if PRIVATE_MARKER in text or str(profile) in text or "Traceback" in text:
            pytest.fail("a source-launcher diagnostic reflected private details", pytrace=False)
    records = [json.loads(line) for line in diagnostic_text.splitlines()]
    assert len(records) == 1
    assert records[0]["diagnostic_code"] == error_code
    assert not app_root.exists()


@_requires_checkout_python
def test_source_launcher_occupied_port_preserves_other_listener_and_diagnostic(
    isolated_paths: tuple[Path, Path], tmp_path: Path,
) -> None:
    profile, cwd = isolated_paths
    app_root = tmp_path / f"{PRIVATE_MARKER}-unused-app"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        completed = _run_launcher(
            REPOSITORY_ROOT,
            cwd=cwd,
            profile=profile,
            arguments=(
                "--check", "--no-open-browser", "--app-root", str(app_root),
                "--port", str(port),
            ),
            timeout=20,
        )
        # A real connection proves the original socket still accepts traffic.
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _address = occupied.accept()
            connection.close()
            assert client.getpeername() == ("127.0.0.1", port)
        assert occupied.getsockname() == ("127.0.0.1", port)
    _safe_failure(
        completed, profile=profile, app_root=app_root,
        error_code="local_port_unavailable",
    )


@_requires_checkout_python
def test_source_launcher_explicit_missing_tools_override_checkout_bundle(
    isolated_paths: tuple[Path, Path], tmp_path: Path,
) -> None:
    profile, cwd = isolated_paths
    app_root = tmp_path / f"{PRIVATE_MARKER}-unused-app"
    missing_tools = tmp_path / f"{PRIVATE_MARKER}-missing-tools"
    port = _available_port()
    completed = _run_launcher(
        REPOSITORY_ROOT,
        cwd=cwd,
        profile=profile,
        arguments=(
            "--check", "--no-open-browser", "--app-root", str(app_root),
            "--port", str(port), "--tool-root", str(missing_tools),
        ),
        timeout=20,
        released_port=port,
    )
    _safe_failure(
        completed, profile=profile, app_root=app_root,
        error_code="local_toolchain_unavailable",
    )
    assert not missing_tools.exists()


@pytest.mark.parametrize(
    "invalid_arguments",
    (("--port", PRIVATE_MARKER), (f"--{PRIVATE_MARKER}",)),
)
@_requires_checkout_python
def test_source_launcher_invalid_arguments_save_safe_diagnostic(
    isolated_paths: tuple[Path, Path], tmp_path: Path,
    invalid_arguments: tuple[str, ...],
) -> None:
    profile, cwd = isolated_paths
    app_root = tmp_path / f"{PRIVATE_MARKER}-unused-app"
    completed = _run_launcher(
        REPOSITORY_ROOT,
        cwd=cwd,
        profile=profile,
        arguments=(
            "--check", "--no-open-browser", "--app-root", str(app_root),
            *invalid_arguments,
        ),
        timeout=20,
    )
    _safe_failure(
        completed, profile=profile, app_root=app_root,
        error_code="invalid_arguments",
    )


def test_source_launcher_missing_python_reports_no_saved_diagnostic_without_hanging(
    isolated_paths: tuple[Path, Path], tmp_path: Path,
) -> None:
    profile, cwd = isolated_paths
    source_copy = tmp_path / "源码 副本!"
    source_copy.mkdir()
    shutil.copyfile(
        REPOSITORY_ROOT / "Start-Open-Flame.cmd",
        source_copy / "Start-Open-Flame.cmd",
    )
    completed = _run_launcher(
        source_copy, cwd=cwd, profile=profile, arguments=(), timeout=20,
    )
    assert completed.returncode == 2
    assert completed.stderr == ""
    explanation = completed.stdout.casefold()
    assert "python" in explanation
    assert "no diagnostic" in explanation
    assert "saved" in explanation
    assert not (profile / DIAGNOSTIC_RELATIVE_PATH).exists()
    assert not (source_copy / ".venv").exists()
    assert not list(profile.iterdir())

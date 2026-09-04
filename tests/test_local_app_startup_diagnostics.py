"""Real, offline CLI regressions for failures before the runtime logger exists."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import video_download_control.local_app_cli as cli_module
from video_download_control.local_app import LocalAppError


pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="standalone local application requires Windows"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_MARKER = "SYNTHETIC-PRIVATE-STARTUP-DIAGNOSTIC"


@pytest.fixture(autouse=True)
def isolated_local_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(profile))


def _run_cli(app_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("VDC_")
        and key.upper() not in {"PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    }
    environment.update(
        PYTHONPATH=str(REPOSITORY_ROOT / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "video_download_control.local_app_cli",
            "--app-root",
            str(app_root),
            "--allow-direct-network",
            "--no-open-browser",
            "--check",
            *arguments,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _assert_private_early_failure(
    completed: subprocess.CompletedProcess[str], app_root: Path
) -> dict[str, object]:
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert len(completed.stderr.splitlines()) == 1
    assert PRIVATE_MARKER not in completed.stderr
    assert str(app_root.parent) not in completed.stderr
    assert "Traceback" not in completed.stderr
    # These preflight failures must not initialize the DB or runtime log tree.
    assert not app_root.exists()
    payload = json.loads(completed.stderr)
    assert payload["status"] == "error"
    return payload


def _assert_diagnostic(
    payload: dict[str, object], *, code: str, site: str, next_step: str
) -> None:
    assert payload == {
        "status": "error",
        "error_code": code,
        "failure_site": site,
        "log_status": "not_started",
        "next_step": next_step,
        "diagnostic_status": "saved",
    }


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_real_cli_occupied_port_has_actionable_private_startup_diagnostic(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / PRIVATE_MARKER / "app"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        completed = _run_cli(app_root, "--port", str(port))
        # Failure must not close or replace the unrelated original listener.
        assert occupied.getsockname() == ("127.0.0.1", port)

    payload = _assert_private_early_failure(completed, app_root)
    _assert_diagnostic(
        payload,
        code="local_port_unavailable",
        site="port_reservation",
        next_step="请关闭占用端口的程序，或使用 --port 指定其他端口后重试。",
    )


@pytest.mark.parametrize("tool_root_kind", ("missing", "ordinary_file"))
def test_real_cli_unavailable_tool_directory_has_actionable_private_diagnostic(
    tmp_path: Path, tool_root_kind: str
) -> None:
    app_root = tmp_path / PRIVATE_MARKER / "app"
    tool_root = tmp_path / f"{PRIVATE_MARKER}-tools"
    if tool_root_kind == "ordinary_file":
        tool_root.write_text(PRIVATE_MARKER, encoding="utf-8")
    completed = _run_cli(
        app_root, "--port", str(_available_port()), "--tool-root", str(tool_root)
    )

    payload = _assert_private_early_failure(completed, app_root)
    if tool_root_kind == "ordinary_file":
        assert tool_root.read_text(encoding="utf-8") == PRIVATE_MARKER
    else:
        assert not tool_root.exists()
    _assert_diagnostic(
        payload,
        code="local_toolchain_unavailable",
        site="toolchain_directory",
        next_step="请安装锁定工具包，或使用 --tool-root 指定已安装的有效工具目录后重试。",
    )


def test_real_cli_invalid_cookie_config_has_actionable_private_diagnostic(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / PRIVATE_MARKER / "app"
    cookie_config = tmp_path / f"{PRIVATE_MARKER}-config.json"
    # Deliberately malformed schema with synthetic private values only. The
    # configuration is read-only; no Cookie source file is created or opened.
    document = {
        "schema_version": 2,
        "cookie_sources": [
            {
                "platform": "douyin",
                "credential_ref": f"{PRIVATE_MARKER}-REF",
                "path": str(tmp_path / f"{PRIVATE_MARKER}-cookie.txt"),
            }
        ],
        "default_cookie_platforms": [],
        "unexpected_private_field": f"{PRIVATE_MARKER}-COOKIE-VALUE",
    }
    cookie_config.write_text(json.dumps(document), encoding="utf-8")
    before = cookie_config.read_bytes()
    cookie_config.chmod(stat.S_IRUSR)
    try:
        completed = _run_cli(
            app_root,
            "--port",
            str(_available_port()),
            "--cookie-config",
            str(cookie_config),
        )
        assert cookie_config.read_bytes() == before
    finally:
        cookie_config.chmod(stat.S_IRUSR | stat.S_IWUSR)

    payload = _assert_private_early_failure(completed, app_root)
    assert not (tmp_path / f"{PRIVATE_MARKER}-cookie.txt").exists()
    _assert_diagnostic(
        payload,
        code="local_cookie_config_invalid",
        site="cookie_config",
        next_step="请检查 --cookie-config 的只读 JSON 配置；如需匿名启动，请移除该参数后重试。",
    )


@pytest.mark.parametrize(
    ("exception_type", "error_code", "exit_code"),
    (
        (LocalAppError, "local_app_failed", 2),
        (ValueError, "local_app_failed", 2),
        (RuntimeError, "internal_error", 70),
    ),
)
def test_unknown_cli_failure_uses_fixed_fallback_not_private_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exception_type: type[Exception],
    error_code: str,
    exit_code: int,
) -> None:
    def fail(_config) -> int:
        # Even text imitating a known cause must not select a typed failure.
        raise exception_type(
            f"the local control port is unavailable {PRIVATE_MARKER} "
            f"ref={PRIVATE_MARKER}-REF Cookie={PRIVATE_MARKER}-COOKIE "
            f"path={tmp_path}"
        )

    monkeypatch.setattr(cli_module, "run_local_app", fail)
    result = cli_module.main(
        [
            "--app-root",
            str(tmp_path / "app"),
            "--allow-direct-network",
            "--no-open-browser",
            "--check",
        ]
    )

    captured = capsys.readouterr()
    assert result == exit_code
    assert captured.out == ""
    assert PRIVATE_MARKER not in captured.err
    assert str(tmp_path) not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.err) == {
        "status": "error",
        "error_code": error_code,
        "failure_site": "unknown",
        "log_status": "unknown",
        "next_step": "请检查运行环境；若已有运行日志，请保留日志并根据错误码排查。",
        "diagnostic_status": "saved",
    }

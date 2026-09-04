from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import video_download_control.local_app_cli as cli_module
from video_download_control.local_app import LocalAppError


@pytest.fixture(autouse=True)
def isolated_local_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(profile))


def test_cli_passes_one_validated_config_to_the_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = (tmp_path / "app-root").resolve()
    seen: list[object] = []

    def run(config) -> int:
        seen.append(config)
        return 0

    monkeypatch.setattr(cli_module, "run_local_app", run)

    result = cli_module.main(
        [
            "--app-root",
            str(app_root),
            "--allow-direct-network",
            "--no-open-browser",
            "--check",
        ]
    )

    assert result == 0
    assert len(seen) == 1
    config = seen[0]
    assert config.app_root == app_root
    assert config.allow_direct_network is True
    assert config.open_browser is False
    assert config.check_only is True


def test_cli_unknown_supervisor_phase_is_not_labeled_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "PRIVATE-SUPERVISOR-PATH-AND-SECRET"

    def fail(_config) -> int:
        # The supervisor can raise after readiness during normal operation.
        # With no typed boundary evidence the CLI must not guess its phase.
        raise LocalAppError(f"worker exited after ready: {private_marker}")

    monkeypatch.setattr(cli_module, "run_local_app", fail)

    result = cli_module.main(
        [
            "--app-root",
            str((tmp_path / "app-root").resolve()),
            "--allow-direct-network",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_code": "local_app_failed",
        "status": "error",
        "failure_site": "unknown",
        "log_status": "unknown",
        "next_step": "请检查运行环境；若已有运行日志，请保留日志并根据错误码排查。",
        "diagnostic_status": "saved" if os.name == "nt" else "unavailable",
    }
    assert private_marker not in captured.err
    assert "Traceback" not in captured.err


def test_cli_does_not_echo_a_mistaken_raw_cookie_mapping(
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "PRIVATE-COOKIE-REF-AND-PATH"

    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args(
            ["--cookie-source", f"douyin:{private_marker}=C:\\private\\cookie.txt"]
        )

    captured = capsys.readouterr()
    assert private_marker not in captured.err
    assert "C:\\private\\cookie.txt" not in captured.err


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--port", "0"),
        ("--port", "65536"),
        ("--startup-timeout-seconds", "nan"),
        ("--startup-timeout-seconds", "inf"),
        ("--shutdown-timeout-seconds", "0"),
        ("--poll-interval-seconds", "0.09"),
    ),
)
def test_cli_rejects_unbounded_ports_and_deadlines(
    option: str,
    value: str,
) -> None:
    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args([option, value])

from __future__ import annotations

import json
from pathlib import Path

import pytest

import video_download_control.toolchain_cli as cli_module
from video_download_control.toolchain import (
    ToolchainError,
    ToolchainStatus,
    VerifiedToolchain,
)


@pytest.mark.parametrize(
    "exception",
    [
        ToolchainError("bundle_invalid", "secret://raw/path"),
        OSError("secret OS detail"),
    ],
)
def test_cli_failure_emits_only_a_bounded_reason_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exception: BaseException,
) -> None:
    tool_root = (tmp_path / "private-tool-root-marker").resolve()

    def fail(_tool_root: Path):
        raise exception

    monkeypatch.setattr(cli_module, "verify_toolchain", fail)

    result = cli_module.main(["verify", "--tool-root", str(tool_root)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    payload = json.loads(captured.err)
    expected_code = (
        "bundle_invalid"
        if isinstance(exception, ToolchainError)
        else "toolchain_unavailable"
    )
    assert payload == {"detail_code": expected_code, "state": "error"}
    assert "secret" not in captured.err
    assert str(tool_root) not in captured.err


def test_smoke_success_includes_every_fail_closed_capability_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool_root = (tmp_path / "managed-tools").resolve()
    monkeypatch.setattr(
        cli_module,
        "run_offline_smoke",
        lambda _tool_root: VerifiedToolchain(
            bundle_id="fixture-bundle",
            yt_dlp_version="fixture-yt-dlp",
            ffmpeg_version="fixture-ffmpeg",
            ffprobe_version="fixture-ffmpeg",
            offline_smoke_passed=True,
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "inspect_toolchain",
        lambda _tool_root: ToolchainStatus(
            state="ready",
            detail_code="ok",
            yt_dlp_version="fixture-yt-dlp",
            ffmpeg_version="fixture-ffmpeg",
            ffprobe_version="fixture-ffmpeg",
            offline_smoke_passed=True,
            isolated_worker_ready=False,
            platform_download_verified=False,
            redistribution_status=(
                "blocked_pending_third_party_source_and_notice_audit"
            ),
            network_download_enabled=False,
        ),
    )

    result = cli_module.main(["smoke", "--tool-root", str(tool_root)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "bundle_id": "fixture-bundle",
        "detail_code": "ok",
        "ffmpeg_version": "fixture-ffmpeg",
        "ffprobe_version": "fixture-ffmpeg",
        "isolated_worker_ready": False,
        "network_download_enabled": False,
        "offline_smoke_passed": True,
        "platform_download_verified": False,
        "redistribution_status": (
            "blocked_pending_third_party_source_and_notice_audit"
        ),
        "state": "ready",
        "yt_dlp_version": "fixture-yt-dlp",
    }

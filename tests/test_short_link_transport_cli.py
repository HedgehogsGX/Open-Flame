from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

import video_download_control.short_link_transport_cli as cli_module


class _WaitingServer:
    async def serve_forever(self) -> None:
        await asyncio.Event().wait()


class _RecordingService:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def start_unix(self, path: Path, *, mode: int):
        assert path == Path("/private/run/short-link.sock")
        assert mode == 0o600
        self.started = True
        return _WaitingServer()

    async def close(self) -> None:
        self.closed = True


def test_serve_handles_sigterm_and_closes_socket_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RecordingService()
    callbacks: dict[signal.Signals, object] = {}

    class _SignalLoop:
        def add_signal_handler(self, candidate, callback) -> None:
            callbacks[candidate] = callback
            if candidate == signal.SIGTERM:
                callback()

        def remove_signal_handler(self, candidate) -> bool:
            callbacks.pop(candidate, None)
            return True

    monkeypatch.setattr(
        cli_module.asyncio,
        "get_running_loop",
        lambda: _SignalLoop(),
    )

    asyncio.run(
        cli_module._serve(
            service,  # type: ignore[arg-type]
            Path("/private/run/short-link.sock"),
        )
    )

    assert service.started is True
    assert service.closed is True
    assert callbacks == {}


def test_main_redacts_unexpected_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "/private/key-path-marker"

    def fail_without_disclosure(path: Path) -> bytes:
        del path
        raise RuntimeError(marker)

    monkeypatch.setattr(cli_module, "load_shared_key", fail_without_disclosure)

    result = cli_module.main(
        [
            "--unix-socket",
            "/private/run/short-link.sock",
            "--shared-key-file",
            "/private/short-link.key",
            "--replay-directory",
            "/private/replay",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err.strip() == "short-link egress configuration failed"
    assert marker not in captured.err

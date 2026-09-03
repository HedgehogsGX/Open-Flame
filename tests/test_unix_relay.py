from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_download_control.security.unix_relay import (
    RelayAuditEvent,
    UnixRelayConfigurationError,
    UnixRelayLimits,
    UnixSocketRelay,
)


class FakeWriter:
    def __init__(self, peer_host: str = "127.0.0.1") -> None:
        self.peer_host = peer_host
        self.data = bytearray()
        self.closed = False
        self.waited = False
        self.eof = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True
        await asyncio.sleep(0)

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "peername":
            return (self.peer_host, 54321)
        return default

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.eof = True


class FakeServer:
    sockets = ()

    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True

    async def serve_forever(self) -> None:
        await asyncio.Future()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *unused: object) -> None:
        self.close()
        await self.wait_closed()


def stream(data: bytes = b"", *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    if data:
        reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def socket_info(
    *, mode: int = stat.S_IFSOCK | 0o660, device: int = 7, inode: int = 11
):
    return SimpleNamespace(st_mode=mode, st_dev=device, st_ino=inode)


def run(coroutine) -> None:
    asyncio.run(coroutine)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "localhost"])
def test_listener_accepts_only_numeric_loopback(
    tmp_path: Path, host: str
) -> None:
    async def scenario() -> None:
        factory_called = False

        async def factory(callback, *, host: str, port: int, limit: int):
            nonlocal factory_called
            factory_called = True
            return FakeServer()

        relay = UnixSocketRelay(
            tmp_path / "proxy.sock",
            path_stat=lambda path: socket_info(),
            server_factory=factory,
        )
        with pytest.raises(UnixRelayConfigurationError, match="loopback"):
            await relay.start(host)
        assert not factory_called

    run(scenario())


def test_start_validates_upstream_socket_before_binding(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory_called = False

        async def factory(callback, *, host: str, port: int, limit: int):
            nonlocal factory_called
            factory_called = True
            return FakeServer()

        regular_file = UnixSocketRelay(
            tmp_path / "regular.sock",
            path_stat=lambda path: socket_info(mode=stat.S_IFREG | 0o600),
            server_factory=factory,
        )
        with pytest.raises(UnixRelayConfigurationError, match="Unix socket"):
            await regular_file.start()

        world_writable = UnixSocketRelay(
            tmp_path / "writable.sock",
            path_stat=lambda path: socket_info(mode=stat.S_IFSOCK | 0o666),
            server_factory=factory,
        )
        with pytest.raises(UnixRelayConfigurationError, match="world-writable"):
            await world_writable.start()
        assert not factory_called

        with pytest.raises(UnixRelayConfigurationError, match="absolute"):
            UnixSocketRelay("relative.sock")

    run(scenario())


def test_start_and_close_are_offline_and_verify_listener_arguments(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calls: list[tuple[object, str, int, int]] = []
        server = FakeServer()

        async def factory(callback, *, host: str, port: int, limit: int):
            calls.append((callback, host, port, limit))
            return server

        relay = UnixSocketRelay(
            tmp_path / "proxy.sock",
            limits=UnixRelayLimits(io_chunk_bytes=1234),
            path_stat=lambda path: socket_info(),
            server_factory=factory,
        )
        assert await relay.start("::1", 18080) is server
        assert callable(calls[0][0])
        assert calls[0][1:] == ("::1", 18080, 1234)
        await relay.close()
        assert server.closed and server.waited
        assert relay.server is None

    run(scenario())


def test_relays_both_directions_and_emits_no_payload_or_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        events: list[RelayAuditEvent] = []
        upstream_writer = FakeWriter()
        secret_path = tmp_path / "secret-token.sock"

        async def connector(path: str):
            assert path == str(secret_path)
            return stream(b"response-secret"), upstream_writer

        relay = UnixSocketRelay(
            secret_path,
            path_stat=lambda path: socket_info(),
            unix_connector=connector,
            audit=events.append,
        )
        client_writer = FakeWriter()
        await relay.handle_connection(stream(b"request-secret"), client_writer)

        assert bytes(upstream_writer.data) == b"request-secret"
        assert bytes(client_writer.data) == b"response-secret"
        assert upstream_writer.closed and client_writer.closed
        assert events == [RelayAuditEvent("connected"), RelayAuditEvent("completed")]
        audit_text = repr(events)
        assert "request-secret" not in audit_text
        assert "response-secret" not in audit_text
        assert "secret-token" not in audit_text

    run(scenario())


def test_byte_budgets_fail_closed_in_both_directions(tmp_path: Path) -> None:
    async def scenario() -> None:
        upload_events: list[RelayAuditEvent] = []
        upload_writer = FakeWriter()

        async def upload_connector(path: str):
            return stream(), upload_writer

        upload_relay = UnixSocketRelay(
            tmp_path / "upload.sock",
            limits=UnixRelayLimits(max_client_to_upstream_bytes=4),
            path_stat=lambda path: socket_info(),
            unix_connector=upload_connector,
            audit=upload_events.append,
        )
        await upload_relay.handle_connection(
            stream(b"four-extra"), FakeWriter()
        )
        assert bytes(upload_writer.data) == b"four"
        assert upload_events[-1] == RelayAuditEvent("client_to_upstream_limit")

        download_events: list[RelayAuditEvent] = []

        async def download_connector(path: str):
            return stream(b"five-extra"), FakeWriter()

        download_relay = UnixSocketRelay(
            tmp_path / "download.sock",
            limits=UnixRelayLimits(max_upstream_to_client_bytes=4),
            path_stat=lambda path: socket_info(),
            unix_connector=download_connector,
            audit=download_events.append,
        )
        download_client = FakeWriter()
        await download_relay.handle_connection(stream(), download_client)
        assert bytes(download_client.data) == b"five"
        assert download_events[-1] == RelayAuditEvent(
            "upstream_to_client_limit"
        )

    run(scenario())


def test_connect_idle_and_total_deadlines_are_distinct(tmp_path: Path) -> None:
    async def scenario() -> None:
        connect_events: list[RelayAuditEvent] = []

        async def stuck_connector(path: str):
            await asyncio.Future()

        connect_relay = UnixSocketRelay(
            tmp_path / "connect.sock",
            limits=UnixRelayLimits(
                connect_timeout_seconds=0.01,
                idle_timeout_seconds=0.2,
                total_timeout_seconds=0.3,
            ),
            path_stat=lambda path: socket_info(),
            unix_connector=stuck_connector,
            audit=connect_events.append,
        )
        await connect_relay.handle_connection(stream(eof=False), FakeWriter())
        assert connect_events == [RelayAuditEvent("connect_timeout")]

        async def quiet_connector(path: str):
            return stream(eof=False), FakeWriter()

        idle_events: list[RelayAuditEvent] = []
        idle_relay = UnixSocketRelay(
            tmp_path / "idle.sock",
            limits=UnixRelayLimits(
                connect_timeout_seconds=0.1,
                idle_timeout_seconds=0.01,
                total_timeout_seconds=0.2,
            ),
            path_stat=lambda path: socket_info(),
            unix_connector=quiet_connector,
            audit=idle_events.append,
        )
        await idle_relay.handle_connection(stream(eof=False), FakeWriter())
        assert idle_events == [
            RelayAuditEvent("connected"),
            RelayAuditEvent("idle_timeout"),
        ]

        total_events: list[RelayAuditEvent] = []
        total_relay = UnixSocketRelay(
            tmp_path / "total.sock",
            limits=UnixRelayLimits(
                connect_timeout_seconds=0.1,
                idle_timeout_seconds=0.2,
                total_timeout_seconds=0.01,
            ),
            path_stat=lambda path: socket_info(),
            unix_connector=quiet_connector,
            audit=total_events.append,
        )
        await total_relay.handle_connection(stream(eof=False), FakeWriter())
        assert total_events == [
            RelayAuditEvent("connected"),
            RelayAuditEvent("total_timeout"),
        ]

    run(scenario())


def test_path_identity_change_after_connect_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = 0
        events: list[RelayAuditEvent] = []
        upstream_writer = FakeWriter()

        def path_stat(path: Path):
            nonlocal calls
            calls += 1
            return socket_info(inode=1 if calls == 1 else 2)

        async def connector(path: str):
            return stream(b"must-not-relay"), upstream_writer

        relay = UnixSocketRelay(
            tmp_path / "proxy.sock",
            path_stat=path_stat,
            unix_connector=connector,
            audit=events.append,
        )
        client = FakeWriter()
        await relay.handle_connection(stream(b"must-not-relay"), client)
        assert bytes(upstream_writer.data) == b""
        assert bytes(client.data) == b""
        assert events == [RelayAuditEvent("upstream_path_changed")]
        assert upstream_writer.closed and client.closed

    run(scenario())


def test_connection_limit_peer_check_and_close_cancel_active_tasks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        events: list[RelayAuditEvent] = []
        connector_calls = 0
        connected = asyncio.Event()
        upstream_writers: list[FakeWriter] = []

        async def connector(path: str):
            nonlocal connector_calls
            connector_calls += 1
            writer = FakeWriter()
            upstream_writers.append(writer)
            connected.set()
            return stream(eof=False), writer

        relay = UnixSocketRelay(
            tmp_path / "proxy.sock",
            limits=UnixRelayLimits(
                max_connections=1,
                idle_timeout_seconds=1,
                total_timeout_seconds=2,
            ),
            path_stat=lambda path: socket_info(),
            unix_connector=connector,
            audit=events.append,
        )
        first = FakeWriter()
        second = FakeWriter()
        remote = FakeWriter("192.0.2.5")
        relay._client_connected(stream(eof=False), first)
        await asyncio.wait_for(connected.wait(), timeout=0.2)
        relay._client_connected(stream(eof=False), second)
        relay._client_connected(stream(eof=False), remote)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert connector_calls == 1
        assert second.closed and remote.closed
        assert RelayAuditEvent("connection_limit") in events
        assert RelayAuditEvent("non_loopback_peer") in events

        await relay.close()
        assert first.closed and upstream_writers[0].closed
        assert relay.active_connections == 0
        assert not relay._tasks

    run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_connections": 0},
        {"max_client_to_upstream_bytes": True},
        {"max_upstream_to_client_bytes": -1},
        {"io_chunk_bytes": 0},
        {"connect_timeout_seconds": float("inf")},
        {"idle_timeout_seconds": 0},
        {"total_timeout_seconds": float("nan")},
    ],
)
def test_limits_must_be_positive_and_finite(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="limits"):
        UnixRelayLimits(**arguments)

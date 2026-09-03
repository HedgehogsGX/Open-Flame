from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import math
import os
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...

    def get_extra_info(self, name: str, default: object = None) -> object: ...

    def can_write_eof(self) -> bool: ...

    def write_eof(self) -> None: ...


RelayUnixConnector = Callable[
    [str], Awaitable[tuple[asyncio.StreamReader, _Writer]]
]
RelayServerFactory = Callable[..., Awaitable[asyncio.AbstractServer]]
RelayPathStat = Callable[[Path], object]
RelayAuditSink = Callable[["RelayAuditEvent"], None]


class UnixRelayConfigurationError(ValueError):
    """The relay cannot safely use the requested listener or Unix socket."""


@dataclass(frozen=True, slots=True)
class UnixRelayLimits:
    """Hard per-connection limits for the byte-transparent relay."""

    max_connections: int = 16
    max_client_to_upstream_bytes: int = 64 * 1024 * 1024
    max_upstream_to_client_bytes: int = 512 * 1024 * 1024
    io_chunk_bytes: int = 64 * 1024
    connect_timeout_seconds: float = 5.0
    idle_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_connections,
            self.max_client_to_upstream_bytes,
            self.max_upstream_to_client_bytes,
            self.io_chunk_bytes,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in integer_limits
        ):
            raise ValueError("Unix relay integer limits must be positive")
        time_limits = (
            self.connect_timeout_seconds,
            self.idle_timeout_seconds,
            self.total_timeout_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in time_limits
        ):
            raise ValueError("Unix relay time limits must be positive")


@dataclass(frozen=True, slots=True)
class RelayAuditEvent:
    """A deliberately sparse event that cannot contain relayed payloads."""

    reason: str


@dataclass(frozen=True, slots=True)
class _SocketIdentity:
    device: int
    inode: int


@dataclass(slots=True)
class _ByteBudget:
    limit: int
    failure_reason: str
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self, size: int) -> None:
        if size < 0 or size > self.remaining:
            raise _RelayFailure(self.failure_reason)
        self.used += size


@dataclass(slots=True)
class _Activity:
    timeout: float
    last_progress: float
    generation: int = 0
    changed: asyncio.Event = field(init=False)

    def __post_init__(self) -> None:
        self.changed = asyncio.Event()

    def touch(self) -> None:
        self.last_progress = asyncio.get_running_loop().time()
        self.generation += 1
        self.changed.set()

    async def wait_until_idle(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            observed_generation = self.generation
            remaining = self.timeout - (loop.time() - self.last_progress)
            if remaining <= 0:
                raise _RelayFailure("idle_timeout")
            self.changed.clear()
            if observed_generation != self.generation:
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=remaining)
            except TimeoutError:
                if observed_generation == self.generation:
                    raise _RelayFailure("idle_timeout") from None


class _RelayFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class UnixSocketRelay:
    """Bounded loopback TCP to filesystem Unix-domain-socket relay.

    Only numeric loopback listener addresses are accepted. The upstream path
    must be an absolute, non-world-writable Unix socket (not a symlink), and
    its device/inode identity is checked both before and after connecting.

    This component constrains traffic that traverses it. A production worker
    still needs network-namespace or firewall isolation to prevent alternate
    direct egress paths.
    """

    def __init__(
        self,
        upstream_path: str | os.PathLike[str],
        *,
        limits: UnixRelayLimits | None = None,
        audit: RelayAuditSink | None = None,
        unix_connector: RelayUnixConnector | None = None,
        server_factory: RelayServerFactory | None = None,
        path_stat: RelayPathStat | None = None,
    ) -> None:
        self.upstream_path = _absolute_path(upstream_path)
        self.limits = limits or UnixRelayLimits()
        self.audit = audit
        self.unix_connector = unix_connector
        self.server_factory = server_factory
        self.path_stat = path_stat or os.lstat
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_connections = 0
        self._closing = False

    @property
    def server(self) -> asyncio.AbstractServer | None:
        return self._server

    @property
    def active_connections(self) -> int:
        return self._active_connections

    async def start(
        self, host: str = "127.0.0.1", port: int = 0
    ) -> asyncio.AbstractServer:
        """Validate both endpoints and start the TCP listener."""

        if self._server is not None or self._closing:
            raise RuntimeError("Unix relay is already started or closing")
        _validate_loopback_host(host)
        _validate_port(port)
        self._require_safe_upstream_path()
        factory = self.server_factory or asyncio.start_server
        server = await factory(
            self._client_connected,
            host=host,
            port=port,
            limit=self.limits.io_chunk_bytes,
        )
        try:
            _validate_bound_server(server)
        except BaseException:
            await self._close_server(server)
            raise
        self._server = server
        return server

    async def serve_forever(
        self, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        server = await self.start(host, port)
        try:
            async with server:
                await server.serve_forever()
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop accepting, cancel active relays, and close their streams."""

        if self._closing:
            while self._closing:
                await asyncio.sleep(0)
            return
        self._closing = True
        server, self._server = self._server, None
        try:
            if server is not None:
                await self._close_server(server)
            while self._tasks:
                tasks = tuple(self._tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._closing = False

    async def __aenter__(self) -> UnixSocketRelay:
        await self.start()
        return self

    async def __aexit__(self, *unused: object) -> None:
        await self.close()

    def _client_connected(
        self, reader: asyncio.StreamReader, writer: _Writer
    ) -> None:
        if self._closing or not _writer_peer_is_loopback(writer):
            self._schedule(self._reject_connection(writer, "non_loopback_peer"))
            return
        if self._active_connections >= self.limits.max_connections:
            self._schedule(self._reject_connection(writer, "connection_limit"))
            return
        self._active_connections += 1
        self._schedule(self._run_accepted_connection(reader, writer))

    def _schedule(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reject_connection(self, writer: _Writer, reason: str) -> None:
        self._emit(reason)
        await self._close_writer(writer)

    async def _run_accepted_connection(
        self, reader: asyncio.StreamReader, writer: _Writer
    ) -> None:
        try:
            await self.handle_connection(reader, writer)
        finally:
            self._active_connections -= 1

    async def handle_connection(
        self, client_reader: asyncio.StreamReader, client_writer: _Writer
    ) -> None:
        """Handle one accepted stream; public for deterministic offline tests."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.total_timeout_seconds
        upstream_writer: _Writer | None = None
        try:
            async with asyncio.timeout_at(deadline):
                identity = self._safe_upstream_identity()
                upstream_reader, upstream_writer = await self._connect_upstream()
                if self._safe_upstream_identity() != identity:
                    raise _RelayFailure("upstream_path_changed")
                self._emit("connected")
                await self._relay_both_directions(
                    client_reader,
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                )
                self._emit("completed")
        except _RelayFailure as failure:
            self._emit(failure.reason)
        except TimeoutError:
            self._emit("total_timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._emit("relay_failure")
        finally:
            if upstream_writer is not None:
                await self._close_writer(upstream_writer, deadline=deadline)
            await self._close_writer(client_writer, deadline=deadline)

    def _require_safe_upstream_path(self) -> _SocketIdentity:
        try:
            info = self.path_stat(self.upstream_path)
        except OSError as exc:
            raise UnixRelayConfigurationError(
                "upstream Unix socket is unavailable"
            ) from exc
        mode = getattr(info, "st_mode", None)
        if not isinstance(mode, int) or not stat.S_ISSOCK(mode):
            raise UnixRelayConfigurationError(
                "upstream path must be a Unix socket, not a symlink or file"
            )
        if mode & stat.S_IWOTH:
            raise UnixRelayConfigurationError(
                "upstream Unix socket must not be world-writable"
            )
        device = getattr(info, "st_dev", None)
        inode = getattr(info, "st_ino", None)
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            raise UnixRelayConfigurationError(
                "upstream Unix socket identity is unavailable"
            )
        return _SocketIdentity(device, inode)

    def _safe_upstream_identity(self) -> _SocketIdentity:
        try:
            return self._require_safe_upstream_path()
        except UnixRelayConfigurationError:
            raise _RelayFailure("unsafe_upstream_path") from None

    async def _connect_upstream(
        self,
    ) -> tuple[asyncio.StreamReader, _Writer]:
        connector = self.unix_connector or getattr(
            asyncio, "open_unix_connection", None
        )
        if connector is None:
            raise _RelayFailure("unix_sockets_unavailable")
        try:
            return await asyncio.wait_for(
                connector(str(self.upstream_path)),
                timeout=self.limits.connect_timeout_seconds,
            )
        except TimeoutError:
            raise _RelayFailure("connect_timeout") from None
        except asyncio.CancelledError:
            raise
        except OSError:
            raise _RelayFailure("upstream_unavailable") from None

    async def _relay_both_directions(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: _Writer,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: _Writer,
    ) -> None:
        loop = asyncio.get_running_loop()
        activity = _Activity(
            timeout=self.limits.idle_timeout_seconds,
            last_progress=loop.time(),
        )
        pumps: set[asyncio.Task[None]] = {
            asyncio.create_task(
                self._pump(
                    client_reader,
                    upstream_writer,
                    _ByteBudget(
                        self.limits.max_client_to_upstream_bytes,
                        "client_to_upstream_limit",
                    ),
                    activity,
                )
            ),
            asyncio.create_task(
                self._pump(
                    upstream_reader,
                    client_writer,
                    _ByteBudget(
                        self.limits.max_upstream_to_client_bytes,
                        "upstream_to_client_limit",
                    ),
                    activity,
                )
            ),
        }
        watchdog = asyncio.create_task(activity.wait_until_idle())
        all_tasks = pumps | {watchdog}
        try:
            while pumps:
                done, _ = await asyncio.wait(
                    pumps | {watchdog}, return_when=asyncio.FIRST_COMPLETED
                )
                if watchdog in done:
                    await watchdog
                for task in done & pumps:
                    pumps.remove(task)
                    await task
        finally:
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: _Writer,
        budget: _ByteBudget,
        activity: _Activity,
    ) -> None:
        while True:
            read_size = min(self.limits.io_chunk_bytes, max(1, budget.remaining))
            chunk = await reader.read(read_size)
            if not chunk:
                await self._half_close(writer, activity)
                return
            budget.consume(len(chunk))
            writer.write(chunk)
            await writer.drain()
            activity.touch()

    async def _half_close(self, writer: _Writer, activity: _Activity) -> None:
        try:
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
                activity.touch()
        except (AttributeError, OSError, RuntimeError):
            return

    async def _close_writer(
        self, writer: _Writer, *, deadline: float | None = None
    ) -> None:
        with contextlib.suppress(Exception):
            writer.close()
        timeout = self.limits.idle_timeout_seconds
        if deadline is not None:
            timeout = min(
                timeout,
                max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        if timeout <= 0:
            return
        with contextlib.suppress(Exception, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=timeout)

    async def _close_server(self, server: asyncio.AbstractServer) -> None:
        with contextlib.suppress(Exception):
            server.close()
        with contextlib.suppress(Exception, TimeoutError):
            await asyncio.wait_for(
                server.wait_closed(), timeout=self.limits.idle_timeout_seconds
            )

    def _emit(self, reason: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit(RelayAuditEvent(reason=reason))
        except Exception:
            pass


# A concise alias for callers that already use ``UnixRelayNetworkGuard``.
UnixRelay = UnixSocketRelay


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str):
        raise TypeError("upstream Unix socket path must be text")
    if not raw or "\x00" in raw:
        raise UnixRelayConfigurationError(
            "upstream Unix socket path must be a non-empty filesystem path"
        )
    result = Path(raw)
    if not result.is_absolute():
        raise UnixRelayConfigurationError(
            "upstream Unix socket path must be absolute"
        )
    return result


def _validate_loopback_host(host: str) -> None:
    if not isinstance(host, str):
        raise UnixRelayConfigurationError(
            "relay listener must use a numeric loopback address"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise UnixRelayConfigurationError(
            "relay listener must use a numeric loopback address"
        ) from None
    if not address.is_loopback:
        raise UnixRelayConfigurationError(
            "relay listener must bind only to loopback"
        )


def _validate_port(port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise UnixRelayConfigurationError("relay listener port is invalid")


def _validate_bound_server(server: asyncio.AbstractServer) -> None:
    sockets = getattr(server, "sockets", None)
    if not sockets:
        return
    for bound_socket in sockets:
        try:
            socket_name = bound_socket.getsockname()
            host = socket_name[0]
        except (AttributeError, IndexError, TypeError):
            raise UnixRelayConfigurationError(
                "could not verify the relay listener address"
            ) from None
        _validate_loopback_host(host)


def _writer_peer_is_loopback(writer: _Writer) -> bool:
    try:
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or not peer:
            return False
        host = peer[0]
        return isinstance(host, str) and ipaddress.ip_address(host).is_loopback
    except (AttributeError, ValueError):
        return False


__all__ = [
    "RelayAuditEvent",
    "RelayAuditSink",
    "RelayPathStat",
    "RelayServerFactory",
    "RelayUnixConnector",
    "UnixRelay",
    "UnixRelayConfigurationError",
    "UnixRelayLimits",
    "UnixSocketRelay",
]

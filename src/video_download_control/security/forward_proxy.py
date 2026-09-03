from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import math
import os
import re
import socket
import stat
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from .egress import (
    EgressPolicyError,
    ResolvedTarget,
    Resolver,
    _ascii_host,
    assert_connected_peer,
    resolve_public_target,
)


_HEADER_NAME = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HTTP_VERSION = re.compile(rb"^HTTP/1\.[01]$")
_STATUS_LINE = re.compile(rb"^HTTP/1\.[01] ([0-9]{3})(?:[ \t].*)?$")
_ALWAYS_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...

    def get_extra_info(self, name: str, default: object = None) -> object: ...

    def can_write_eof(self) -> bool: ...

    def write_eof(self) -> None: ...


Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, _Writer]]]
AuditSink = Callable[["ProxyAuditEvent"], None]
UnixConnector = Callable[
    [str], Awaitable[tuple[asyncio.StreamReader, _Writer]]
]
UnixServerFactory = Callable[..., Awaitable[asyncio.AbstractServer]]


@dataclass(frozen=True, slots=True)
class ForwardProxyLimits:
    """Hard per-connection resource limits.

    Upload and download limits count bytes transferred over the relevant
    upstream leg. For a CONNECT tunnel that is the raw tunnel payload. For a
    plain HTTP exchange the generated request head and upstream response wire
    bytes (including chunk framing) are counted.
    """

    max_request_header_bytes: int = 32 * 1024
    max_response_header_bytes: int = 32 * 1024
    max_request_line_bytes: int = 8 * 1024
    max_header_count: int = 100
    max_connections: int = 64
    max_upload_bytes: int = 64 * 1024 * 1024
    max_download_bytes: int = 512 * 1024 * 1024
    io_chunk_bytes: int = 64 * 1024
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_request_header_bytes,
            self.max_response_header_bytes,
            self.max_request_line_bytes,
            self.max_header_count,
            self.max_connections,
            self.max_upload_bytes,
            self.max_download_bytes,
            self.io_chunk_bytes,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in integer_limits
        ):
            raise ValueError("forward proxy integer limits must be positive")
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
            raise ValueError("forward proxy time limits must be positive")


@dataclass(frozen=True, slots=True)
class ProxyAuditEvent:
    """Sparse audit event containing only an operator-configured host label."""

    reason: str
    host: str


@dataclass(slots=True)
class _ConnectionState:
    host: str = "-"
    response_started: bool = False


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    target: str
    version: str
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    status_line: bytes
    headers: tuple[tuple[str, str], ...]
    raw_size: int


@dataclass(slots=True)
class _ByteBudget:
    limit: int
    reason: str
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self, size: int, host: str) -> None:
        if size < 0 or size > self.remaining:
            raise _ProxyFailure(self.reason, host, 413)
        self.used += size


class _ProxyFailure(Exception):
    def __init__(self, reason: str, host: str = "-", status: int = 400) -> None:
        self.reason = reason
        self.host = host or "-"
        self.status = status
        super().__init__(f"reason={self.reason} host={self.host}")


class ControlledForwardProxy:
    """A small, fail-closed HTTP forward proxy with DNS pinning.

    The proxy validates every CONNECT or plain HTTP target with
    :func:`resolve_public_target`, connects to one of the returned numeric IP
    addresses, and verifies the actual connected peer. It does not follow
    redirects: a redirect followed by the client becomes a new request and is
    validated again.

    This module constrains traffic that traverses it. It cannot prove that a
    worker has no alternate direct network path. Production enforcement still
    requires container networking or firewall rules that make this proxy the
    worker's only egress route.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
        allowed_hosts: Iterable[str] | None = None,
        limits: ForwardProxyLimits | None = None,
        audit: AuditSink | None = None,
        unix_connector: UnixConnector | None = None,
        unix_server_factory: UnixServerFactory | None = None,
    ) -> None:
        if isinstance(allowed_hosts, (str, bytes)) or allowed_hosts is None:
            raise ValueError("allowed_hosts must contain at least one host")
        configured_hosts = tuple(allowed_hosts)
        if not configured_hosts:
            raise ValueError("allowed_hosts must contain at least one host")
        normalized_hosts = tuple(
            dict.fromkeys(_ascii_host(host) for host in configured_hosts)
        )
        self.resolver = resolver
        self.connector = connector or self._open_numeric_connection
        self.allowed_hosts = normalized_hosts
        self.limits = limits or ForwardProxyLimits()
        self.audit = audit
        self.unix_connector = unix_connector
        self.unix_server_factory = unix_server_factory
        self._server: asyncio.AbstractServer | None = None
        self._unix_path: Path | None = None
        self._unix_identity: tuple[int, int] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_connections = 0

    @property
    def server(self) -> asyncio.AbstractServer | None:
        return self._server

    async def start(
        self, host: str = "127.0.0.1", port: int = 0
    ) -> asyncio.AbstractServer:
        if self._server is not None:
            raise RuntimeError("forward proxy is already started")
        try:
            bind_address = ipaddress.ip_address(host)
        except (TypeError, ValueError):
            raise ValueError(
                "forward proxy TCP host must be a numeric loopback address"
            ) from None
        if not bind_address.is_loopback:
            raise ValueError("forward proxy TCP host must be loopback")
        self._server = await asyncio.start_server(
            self._client_connected,
            host=bind_address.compressed,
            port=port,
            limit=max(
                self.limits.max_request_header_bytes,
                self.limits.max_response_header_bytes,
            )
            + 1,
        )
        return self._server

    async def serve_forever(
        self, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        server = await self.start(host, port)
        try:
            async with server:
                await server.serve_forever()
        finally:
            await self.close()

    async def start_unix(
        self,
        path: str | os.PathLike[str],
        *,
        mode: int = 0o660,
    ) -> asyncio.AbstractServer:
        """Bind a Unix socket, refusing every pre-existing filesystem entry.

        Stale socket cleanup belongs to a trusted supervisor before this proxy
        starts. On close, the path is unlinked only if it is still the socket
        created by this instance. The containing directory must be
        access-controlled separately in production.
        """

        if self._server is not None:
            raise RuntimeError("forward proxy is already started")
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or not 0 <= mode <= 0o777
        ):
            raise ValueError("Unix socket mode must contain permission bits only")
        socket_path = Path(path)
        if not socket_path.is_absolute():
            raise ValueError("Unix socket path must be absolute")
        if not socket_path.parent.is_dir():
            raise FileNotFoundError("Unix socket parent directory does not exist")

        factory = self.unix_server_factory or getattr(
            asyncio, "start_unix_server", None
        )
        if factory is None:
            raise NotImplementedError(
                "Unix domain servers are unavailable on this platform"
            )
        await self._prepare_unix_path(socket_path)
        server = await factory(
            self._client_connected,
            path=str(socket_path),
            limit=max(
                self.limits.max_request_header_bytes,
                self.limits.max_response_header_bytes,
            )
            + 1,
        )
        identity: tuple[int, int] | None = None
        try:
            identity = _unix_socket_identity(socket_path)
            if identity is None:
                raise RuntimeError("Unix server did not create a socket path")
            os.chmod(socket_path, mode, follow_symlinks=False)
        except BaseException:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    server.wait_closed(),
                    timeout=self.limits.idle_timeout_seconds,
                )
            if identity is not None:
                _unlink_same_unix_socket(socket_path, identity)
            raise
        self._server = server
        self._unix_path = socket_path
        self._unix_identity = identity
        return server

    async def serve_unix(
        self,
        path: str | os.PathLike[str],
        *,
        mode: int = 0o660,
    ) -> None:
        server = await self.start_unix(path, mode=mode)
        try:
            async with server:
                await server.serve_forever()
        finally:
            await self.close()

    async def close(self) -> None:
        server, self._server = self._server, None
        unix_path, self._unix_path = self._unix_path, None
        unix_identity, self._unix_identity = self._unix_identity, None
        try:
            try:
                if server is not None:
                    server.close()
                    await server.wait_closed()
            finally:
                tasks = tuple(self._tasks)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if unix_path is not None and unix_identity is not None:
                _unlink_same_unix_socket(unix_path, unix_identity)

    async def __aenter__(self) -> ControlledForwardProxy:
        await self.start()
        return self

    async def __aexit__(self, *unused: object) -> None:
        await self.close()

    def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._active_connections >= self.limits.max_connections:
            task = asyncio.create_task(self._reject_over_capacity(writer))
        else:
            self._active_connections += 1
            task = asyncio.create_task(self._run_accepted_connection(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reject_over_capacity(self, writer: _Writer) -> None:
        self._emit("connection_limit", "-")
        try:
            writer.write(self._error_response(503, "connection_limit", "-"))
            await self._idle(writer.drain(), "-")
        except Exception:
            pass
        finally:
            await self._close_writer(writer)

    async def _run_accepted_connection(
        self, reader: asyncio.StreamReader, writer: _Writer
    ) -> None:
        try:
            await self.handle_connection(reader, writer)
        finally:
            self._active_connections -= 1

    async def _prepare_unix_path(self, path: Path) -> None:
        identity = _unix_socket_identity(path)
        if identity is None:
            return
        raise FileExistsError(
            "Unix socket path already exists; refusing automatic cleanup"
        )

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: _Writer
    ) -> None:
        """Handle one client; public primarily for deterministic offline tests."""

        state = _ConnectionState()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.total_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                try:
                    request = await self._read_request(reader, state.host)
                    if request.method == "CONNECT":
                        await self._handle_connect(request, reader, writer, state)
                    elif request.method in {"GET", "HEAD"}:
                        await self._handle_http(request, writer, state)
                    else:
                        raise _ProxyFailure(
                            "method_not_allowed", state.host, 405
                        )
                except _ProxyFailure as failure:
                    await self._report_failure(failure, writer, state)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._report_failure(
                        _ProxyFailure("proxy_failure", state.host, 502),
                        writer,
                        state,
                    )
        except TimeoutError:
            # The total deadline is hard: do not add another potentially
            # blocking response drain after it has expired.
            self._emit("total_timeout", state.host)
        except asyncio.CancelledError:
            raise
        finally:
            await self._close_writer(
                writer, timeout=max(0.0, deadline - loop.time())
            )

    async def _handle_connect(
        self,
        request: _Request,
        client_reader: asyncio.StreamReader,
        client_writer: _Writer,
        state: _ConnectionState,
    ) -> None:
        authority = _canonical_connect_authority(request.target)
        host_headers = _header_values(request.headers, "host")
        if len(host_headers) != 1:
            raise _ProxyFailure("invalid_connect_host", "-", 400)
        try:
            host_authority = _canonical_connect_authority(host_headers[0])
        except _ProxyFailure:
            raise _ProxyFailure("invalid_connect_host", "-", 400) from None
        if host_authority != authority:
            raise _ProxyFailure("invalid_connect_host", "-", 400)
        url = f"https://{authority}/"
        state.host = _safe_url_host(url)
        target = await self._resolve(url, state.host)
        state.host = target.host
        upstream_reader, upstream_writer = await self._connect(target)
        try:
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await self._idle(client_writer.drain(), state.host)
            state.response_started = True
            self._emit("connected", state.host)
            await self._relay_both_directions(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
                state.host,
            )
            self._emit("completed", state.host)
        finally:
            await self._close_writer(upstream_writer)

    async def _handle_http(
        self,
        request: _Request,
        client_writer: _Writer,
        state: _ConnectionState,
    ) -> None:
        try:
            parts = urlsplit(request.target)
        except ValueError:
            raise _ProxyFailure("invalid_url", "-", 400) from None
        state.host = _safe_url_host(request.target)
        if parts.fragment:
            raise _ProxyFailure("fragment_not_allowed", state.host, 400)
        target = await self._resolve(request.target, state.host)
        state.host = target.host
        if target.scheme != "http":
            raise _ProxyFailure("https_requires_connect", state.host, 400)
        _require_default_port(parts, 80, state.host)
        outbound = self._build_upstream_request(request, parts, target)
        upload = _ByteBudget(self.limits.max_upload_bytes, "upload_limit")
        upload.consume(len(outbound), state.host)
        upstream_reader, upstream_writer = await self._connect(target)
        try:
            upstream_writer.write(outbound)
            await self._idle(upstream_writer.drain(), state.host)
            self._emit("connected", state.host)
            await self._forward_response(
                request.method,
                upstream_reader,
                client_writer,
                state,
            )
            self._emit("completed", state.host)
        finally:
            await self._close_writer(upstream_writer)

    async def _resolve(self, url: str, host: str) -> ResolvedTarget:
        try:
            return await self._idle(
                asyncio.to_thread(
                    resolve_public_target,
                    url,
                    resolver=self.resolver,
                    allowed_hosts=self.allowed_hosts,
                    allow_ip_literal=False,
                ),
                host,
                timeout_reason="resolution_timeout",
            )
        except EgressPolicyError as exc:
            raise _ProxyFailure(exc.reason, host, 403) from None
        except _ProxyFailure:
            raise
        except Exception:
            raise _ProxyFailure("resolution_failure", host, 502) from None

    async def _connect(
        self, target: ResolvedTarget
    ) -> tuple[asyncio.StreamReader, _Writer]:
        last_reason = "connect_failed"
        for address in target.addresses:
            try:
                reader, writer = await asyncio.wait_for(
                    self.connector(address, target.port),
                    timeout=self.limits.connect_timeout_seconds,
                )
            except TimeoutError:
                last_reason = "connect_timeout"
                continue
            except OSError:
                last_reason = "connect_failed"
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                last_reason = "connect_failed"
                continue

            try:
                peer = writer.get_extra_info("peername")
                peer_ip = _peer_ip(peer)
                assert_connected_peer(target, peer_ip)
            except EgressPolicyError as exc:
                await self._close_writer(writer)
                raise _ProxyFailure(exc.reason, target.host, 502) from None
            except Exception:
                await self._close_writer(writer)
                raise _ProxyFailure(
                    "peer_address_unavailable", target.host, 502
                ) from None
            return reader, writer
        status = 504 if last_reason == "connect_timeout" else 502
        raise _ProxyFailure(last_reason, target.host, status)

    async def _open_numeric_connection(
        self, address: str, port: int
    ) -> tuple[asyncio.StreamReader, _Writer]:
        parsed = ipaddress.ip_address(address)
        family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        reader, writer = await asyncio.open_connection(
            host=parsed.compressed,
            port=port,
            family=family,
            flags=socket.AI_NUMERICHOST,
            limit=self.limits.max_response_header_bytes + 1,
        )
        return reader, writer

    async def _read_request(
        self, reader: asyncio.StreamReader, host: str
    ) -> _Request:
        raw = await self._read_head(
            reader,
            self.limits.max_request_header_bytes,
            "request_headers_too_large",
            host,
        )
        lines = raw[:-4].split(b"\r\n")
        request_line = lines[0]
        if len(request_line) > self.limits.max_request_line_bytes:
            raise _ProxyFailure("request_line_too_large", host, 414)
        fields = request_line.split(b" ")
        if len(fields) != 3 or not fields[0] or not fields[1]:
            raise _ProxyFailure("invalid_request_line", host, 400)
        method_raw, target_raw, version_raw = fields
        if not _HEADER_NAME.fullmatch(
            method_raw
        ) or _has_invalid_request_target_byte(target_raw):
            raise _ProxyFailure("invalid_request_line", host, 400)
        if not _HTTP_VERSION.fullmatch(version_raw):
            raise _ProxyFailure("invalid_http_version", host, 400)
        try:
            method = method_raw.decode("ascii").upper()
            target = target_raw.decode("ascii")
            version = version_raw.decode("ascii")
        except UnicodeDecodeError:
            raise _ProxyFailure("invalid_request_line", host, 400) from None
        headers = _parse_headers(lines[1:], self.limits.max_header_count, host)
        _reject_request_body(headers, host)
        return _Request(method, target, version, headers)

    def _build_upstream_request(
        self, request: _Request, parts: SplitResult, target: ResolvedTarget
    ) -> bytes:
        if not parts.scheme or not parts.netloc:
            raise _ProxyFailure("absolute_url_required", target.host, 400)
        connection_names = _connection_tokens(request.headers, target.host)
        kept: list[tuple[str, str]] = []
        for name, value in request.headers:
            lowered = name.lower()
            if (
                lowered == "host"
                or lowered in _ALWAYS_HOP_BY_HOP
                or lowered in connection_names
                or lowered == "expect"
            ):
                continue
            kept.append((name, value))
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        try:
            request_line = f"{request.method} {path} HTTP/1.1\r\n".encode("ascii")
        except UnicodeEncodeError:
            raise _ProxyFailure("invalid_url", target.host, 400) from None
        output = bytearray(request_line)
        output.extend(f"Host: {target.host}\r\n".encode("ascii"))
        for name, value in kept:
            output.extend(name.encode("ascii"))
            output.extend(b": ")
            output.extend(value.encode("latin-1"))
            output.extend(b"\r\n")
        output.extend(b"Connection: close\r\n\r\n")
        return bytes(output)

    async def _forward_response(
        self,
        method: str,
        upstream_reader: asyncio.StreamReader,
        client_writer: _Writer,
        state: _ConnectionState,
    ) -> None:
        budget = _ByteBudget(self.limits.max_download_bytes, "download_limit")
        while True:
            response = await self._read_response(upstream_reader, state.host)
            budget.consume(response.raw_size, state.host)
            if response.status == 101:
                raise _ProxyFailure("upstream_upgrade_blocked", state.host, 502)
            framing, content_length = _response_framing(response, method, state.host)
            if framing == "length" and content_length is not None:
                if content_length > budget.remaining:
                    raise _ProxyFailure("download_limit", state.host, 413)
            outgoing = _build_downstream_response(
                response, framing, content_length, state.host
            )
            client_writer.write(outgoing)
            await self._idle(client_writer.drain(), state.host)
            state.response_started = True
            if 100 <= response.status < 200:
                continue
            if framing == "none":
                return
            if framing == "length":
                assert content_length is not None
                await self._relay_exactly(
                    upstream_reader,
                    client_writer,
                    content_length,
                    budget,
                    state.host,
                )
                return
            if framing == "chunked":
                await self._relay_chunked(
                    upstream_reader, client_writer, budget, state.host
                )
                return
            await self._relay_until_eof(
                upstream_reader, client_writer, budget, state.host
            )
            return

    async def _read_response(
        self, reader: asyncio.StreamReader, host: str
    ) -> _Response:
        raw = await self._read_head(
            reader,
            self.limits.max_response_header_bytes,
            "response_headers_too_large",
            host,
        )
        lines = raw[:-4].split(b"\r\n")
        match = _STATUS_LINE.fullmatch(lines[0])
        if match is None or _has_invalid_control(lines[0]):
            raise _ProxyFailure("invalid_upstream_status", host, 502)
        headers = _parse_headers(
            lines[1:], self.limits.max_header_count, host, failure_status=502
        )
        return _Response(int(match.group(1)), lines[0], headers, len(raw))

    async def _relay_both_directions(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: _Writer,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: _Writer,
        host: str,
    ) -> None:
        upload = _ByteBudget(self.limits.max_upload_bytes, "upload_limit")
        download = _ByteBudget(self.limits.max_download_bytes, "download_limit")
        tasks = {
            asyncio.create_task(
                self._relay_until_eof(
                    client_reader, upstream_writer, upload, host, half_close=True
                )
            ),
            asyncio.create_task(
                self._relay_until_eof(
                    upstream_reader, client_writer, download, host, half_close=True
                )
            ),
        }
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            failure: BaseException | None = None
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    failure = task.exception()
                    break
            if failure is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure
            if pending:
                await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _relay_until_eof(
        self,
        reader: asyncio.StreamReader,
        writer: _Writer,
        budget: _ByteBudget,
        host: str,
        *,
        half_close: bool = False,
    ) -> None:
        while True:
            chunk = await self._idle(
                reader.read(self.limits.io_chunk_bytes), host
            )
            if not chunk:
                if half_close and writer.can_write_eof():
                    with contextlib.suppress(Exception):
                        writer.write_eof()
                        await self._idle(writer.drain(), host)
                return
            budget.consume(len(chunk), host)
            writer.write(chunk)
            await self._idle(writer.drain(), host)

    async def _relay_exactly(
        self,
        reader: asyncio.StreamReader,
        writer: _Writer,
        size: int,
        budget: _ByteBudget,
        host: str,
    ) -> None:
        remaining = size
        while remaining:
            chunk = await self._idle(
                reader.read(min(self.limits.io_chunk_bytes, remaining)), host
            )
            if not chunk:
                raise _ProxyFailure("upstream_body_incomplete", host, 502)
            budget.consume(len(chunk), host)
            writer.write(chunk)
            await self._idle(writer.drain(), host)
            remaining -= len(chunk)

    async def _relay_chunked(
        self,
        reader: asyncio.StreamReader,
        writer: _Writer,
        budget: _ByteBudget,
        host: str,
    ) -> None:
        while True:
            line = await self._read_line(reader, host)
            budget.consume(len(line), host)
            raw_size = line[:-2].split(b";", 1)[0].strip()
            if not raw_size or len(raw_size) > 16:
                raise _ProxyFailure("invalid_chunk", host, 502)
            try:
                size = int(raw_size, 16)
            except ValueError:
                raise _ProxyFailure("invalid_chunk", host, 502) from None
            if size == 0:
                trailer_size = await self._read_trailers(reader, host)
                budget.consume(trailer_size, host)
                return
            if size > budget.remaining:
                raise _ProxyFailure("download_limit", host, 413)
            await self._relay_exactly(reader, writer, size, budget, host)
            ending = await self._idle(reader.readexactly(2), host)
            budget.consume(2, host)
            if ending != b"\r\n":
                raise _ProxyFailure("invalid_chunk", host, 502)

    async def _read_trailers(
        self, reader: asyncio.StreamReader, host: str
    ) -> int:
        lines: list[bytes] = []
        total = 0
        while True:
            line = await self._read_line(reader, host)
            total += len(line)
            if total > self.limits.max_response_header_bytes:
                raise _ProxyFailure("response_headers_too_large", host, 502)
            if line == b"\r\n":
                _parse_headers(
                    lines,
                    self.limits.max_header_count,
                    host,
                    failure_status=502,
                )
                return total
            lines.append(line[:-2])

    async def _read_line(self, reader: asyncio.StreamReader, host: str) -> bytes:
        try:
            line = await self._idle(reader.readline(), host)
        except ValueError:
            raise _ProxyFailure("response_headers_too_large", host, 502) from None
        if not line.endswith(b"\r\n"):
            raise _ProxyFailure("invalid_chunk", host, 502)
        if len(line) > self.limits.max_response_header_bytes:
            raise _ProxyFailure("response_headers_too_large", host, 502)
        return line

    async def _read_head(
        self,
        reader: asyncio.StreamReader,
        limit: int,
        reason: str,
        host: str,
    ) -> bytes:
        try:
            raw = await self._idle(reader.readuntil(b"\r\n\r\n"), host)
        except (asyncio.LimitOverrunError, ValueError):
            raise _ProxyFailure(reason, host, 431 if "request" in reason else 502) from None
        except asyncio.IncompleteReadError:
            raise _ProxyFailure("incomplete_headers", host, 400) from None
        if len(raw) > limit:
            raise _ProxyFailure(reason, host, 431 if "request" in reason else 502)
        return raw

    async def _idle(
        self,
        awaitable: Awaitable[object],
        host: str,
        *,
        timeout_reason: str = "idle_timeout",
    ):
        try:
            return await asyncio.wait_for(
                awaitable, timeout=self.limits.idle_timeout_seconds
            )
        except TimeoutError:
            raise _ProxyFailure(timeout_reason, host, 504) from None

    async def _report_failure(
        self, failure: _ProxyFailure, writer: _Writer, state: _ConnectionState
    ) -> None:
        safe_host = failure.host or state.host or "-"
        self._emit(failure.reason, safe_host)
        if state.response_started:
            return
        try:
            writer.write(self._error_response(failure.status, failure.reason, safe_host))
            await self._idle(writer.drain(), safe_host)
            state.response_started = True
        except Exception:
            pass

    def _emit(self, reason: str, host: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit(
                ProxyAuditEvent(
                    reason=reason,
                    host=self._policy_audit_host(host),
                )
            )
        except Exception:
            pass

    def _policy_audit_host(self, host: str) -> str:
        normalized = _safe_host_text(host)
        if normalized == "-":
            return "-"
        matches = tuple(
            allowed
            for allowed in self.allowed_hosts
            if normalized == allowed or normalized.endswith(f".{allowed}")
        )
        return max(matches, key=len) if matches else "-"

    @staticmethod
    def _error_response(status: int, reason: str, host: str) -> bytes:
        phrases = {
            400: "Bad Request",
            403: "Forbidden",
            405: "Method Not Allowed",
            413: "Payload Too Large",
            414: "URI Too Long",
            431: "Request Header Fields Too Large",
            502: "Bad Gateway",
            503: "Service Unavailable",
            504: "Gateway Timeout",
        }
        phrase = phrases.get(status, "Bad Gateway")
        safe_reason = _safe_token(reason)
        safe_host = _safe_host_text(host)
        body = f"reason={safe_reason}\nhost={safe_host}\n".encode("ascii")
        return (
            f"HTTP/1.1 {status} {phrase}\r\n".encode("ascii")
            + b"Content-Type: text/plain; charset=us-ascii\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )

    async def _close_writer(
        self, writer: _Writer, *, timeout: float | None = None
    ) -> None:
        with contextlib.suppress(Exception):
            writer.close()
        close_timeout = (
            self.limits.idle_timeout_seconds if timeout is None else timeout
        )
        if close_timeout <= 0:
            return
        with contextlib.suppress(Exception, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=close_timeout)


def _parse_headers(
    lines: Sequence[bytes],
    max_count: int,
    host: str,
    *,
    failure_status: int = 400,
) -> tuple[tuple[str, str], ...]:
    if len(lines) > max_count:
        status = 431 if failure_status == 400 else failure_status
        raise _ProxyFailure("too_many_headers", host, status)
    parsed: list[tuple[str, str]] = []
    for line in lines:
        if not line or line[:1] in b" \t" or b":" not in line:
            raise _ProxyFailure("invalid_header", host, failure_status)
        name_raw, value_raw = line.split(b":", 1)
        if not _HEADER_NAME.fullmatch(name_raw):
            raise _ProxyFailure("invalid_header", host, failure_status)
        value_raw = value_raw.strip(b" \t")
        if any(byte < 32 and byte != 9 for byte in value_raw) or 127 in value_raw:
            raise _ProxyFailure("invalid_header", host, failure_status)
        try:
            name = name_raw.decode("ascii")
            value = value_raw.decode("latin-1")
        except UnicodeDecodeError:
            raise _ProxyFailure("invalid_header", host, failure_status) from None
        parsed.append((name, value))
    return tuple(parsed)


def _header_values(
    headers: Sequence[tuple[str, str]], name: str
) -> list[str]:
    lowered = name.lower()
    return [value for key, value in headers if key.lower() == lowered]


def _connection_tokens(
    headers: Sequence[tuple[str, str]],
    host: str,
    *,
    failure_status: int = 400,
) -> frozenset[str]:
    tokens: set[str] = set()
    for value in _header_values(headers, "connection") + _header_values(
        headers, "proxy-connection"
    ):
        for item in value.split(","):
            try:
                raw = item.strip().encode("ascii")
            except UnicodeEncodeError:
                raise _ProxyFailure(
                    "invalid_connection_header", host, failure_status
                ) from None
            if not raw or not _HEADER_NAME.fullmatch(raw):
                raise _ProxyFailure(
                    "invalid_connection_header", host, failure_status
                )
            tokens.add(raw.decode("ascii").lower())
    return frozenset(tokens)


def _reject_request_body(headers: Sequence[tuple[str, str]], host: str) -> None:
    if _header_values(headers, "transfer-encoding"):
        raise _ProxyFailure("request_body_not_allowed", host, 400)
    values = _header_values(headers, "content-length")
    if not values:
        return
    normalized = {value.strip() for value in values}
    if len(normalized) != 1:
        raise _ProxyFailure("invalid_content_length", host, 400)
    value = normalized.pop()
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise _ProxyFailure("invalid_content_length", host, 400)
    if int(value) != 0:
        raise _ProxyFailure("request_body_not_allowed", host, 400)


def _response_framing(
    response: _Response, method: str, host: str
) -> tuple[str, int | None]:
    transfer_values = _header_values(response.headers, "transfer-encoding")
    length_values = _header_values(response.headers, "content-length")
    if method == "HEAD" or response.status == 304:
        return "none", _validated_content_length(length_values, host)
    if response.status == 204 or 100 <= response.status < 200:
        return "none", None
    if transfer_values and length_values:
        raise _ProxyFailure("ambiguous_response_framing", host, 502)
    if transfer_values:
        codings = [
            item.strip().lower()
            for value in transfer_values
            for item in value.split(",")
        ]
        if codings != ["chunked"]:
            raise _ProxyFailure("unsupported_transfer_encoding", host, 502)
        return "chunked", None
    if length_values:
        return "length", _validated_content_length(length_values, host)
    return "close", None


def _build_downstream_response(
    response: _Response,
    framing: str,
    content_length: int | None,
    host: str,
) -> bytes:
    connection_names = _connection_tokens(
        response.headers, host, failure_status=502
    )
    output = bytearray(response.status_line + b"\r\n")
    for name, value in response.headers:
        lowered = name.lower()
        if (
            lowered in _ALWAYS_HOP_BY_HOP
            or lowered in connection_names
            or lowered == "content-length"
        ):
            continue
        output.extend(name.encode("ascii"))
        output.extend(b": ")
        output.extend(value.encode("latin-1"))
        output.extend(b"\r\n")
    if framing in {"length", "none"} and content_length is not None:
        output.extend(f"Content-Length: {content_length}\r\n".encode("ascii"))
    output.extend(b"Connection: close\r\n\r\n")
    return bytes(output)


def _peer_ip(peer: object) -> str:
    if isinstance(peer, tuple) and peer and isinstance(peer[0], str):
        return peer[0]
    if isinstance(peer, str):
        return peer
    raise ValueError("peer address is unavailable")


def _validated_content_length(
    values: Sequence[str], host: str
) -> int | None:
    if not values:
        return None
    normalized = {value.strip() for value in values}
    if len(normalized) != 1:
        raise _ProxyFailure("invalid_content_length", host, 502)
    value = normalized.pop()
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise _ProxyFailure("invalid_content_length", host, 502)
    return int(value)


def _canonical_connect_authority(value: str) -> str:
    if (
        not value
        or not value.isascii()
        or value != value.strip()
        or any(marker in value for marker in ("/", "?", "#"))
    ):
        raise _ProxyFailure("invalid_connect_target", "-", 400)
    if "@" in value:
        raise _ProxyFailure("credentials_in_url", "-", 403)

    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            raise _ProxyFailure("invalid_connect_target", "-", 400)
        raw_host = value[1:closing]
        suffix = value[closing + 1 :]
        if not suffix.startswith(":"):
            raise _ProxyFailure("invalid_port", "-", 403)
        if suffix.count(":") != 1:
            raise _ProxyFailure("invalid_port", "-", 403)
        _require_connect_port(suffix[1:])
        if "%" in raw_host:
            raise _ProxyFailure("zone_identifier_blocked", "-", 403)
        try:
            canonical_host = ipaddress.IPv6Address(raw_host).compressed
        except ValueError:
            raise _ProxyFailure("invalid_connect_target", "-", 400) from None
        if raw_host != canonical_host:
            raise _ProxyFailure("invalid_connect_target", "-", 400)
        return f"[{canonical_host}]:443"

    colon_count = value.count(":")
    if colon_count == 0:
        raise _ProxyFailure("invalid_port", "-", 403)
    if colon_count != 1:
        raise _ProxyFailure("invalid_connect_target", "-", 400)
    raw_host, raw_port = value.rsplit(":", 1)
    _require_connect_port(raw_port)
    try:
        canonical_host = _ascii_host(raw_host)
    except EgressPolicyError:
        raise _ProxyFailure("invalid_connect_target", "-", 400) from None
    if raw_host != canonical_host:
        raise _ProxyFailure("invalid_connect_target", "-", 400)
    return f"{canonical_host}:443"


def _require_connect_port(value: str) -> None:
    if not value or not value.isdigit() or not value.isascii():
        raise _ProxyFailure("invalid_port", "-", 403)
    if value == "443":
        return
    if len(value) > 1 and value.startswith("0"):
        raise _ProxyFailure("invalid_port", "-", 403)
    raise _ProxyFailure("blocked_port", "-", 403)


def _require_default_port(parts: SplitResult, expected: int, host: str) -> None:
    authority = parts.netloc.rsplit("@", 1)[-1]
    if authority.startswith("["):
        closing = authority.find("]")
        suffix = authority[closing + 1 :] if closing >= 0 else authority
        has_port_marker = suffix.startswith(":")
    else:
        has_port_marker = ":" in authority
    if not has_port_marker:
        return
    try:
        explicit = parts.port
    except ValueError:
        raise _ProxyFailure("invalid_port", host, 403) from None
    if explicit is None:
        raise _ProxyFailure("invalid_port", host, 403)
    if explicit != expected:
        raise _ProxyFailure("blocked_port", host, 403)


def _has_invalid_control(value: bytes) -> bool:
    return any(byte < 32 and byte != 9 for byte in value) or 127 in value


def _has_invalid_request_target_byte(value: bytes) -> bool:
    return any(byte <= 32 or byte == 127 for byte in value)


def _safe_url_host(url: str) -> str:
    try:
        return _safe_host_text(urlsplit(url).hostname or "-")
    except (ValueError, UnicodeError):
        return "-"


def _safe_token(value: str) -> str:
    accepted = "".join(
        character
        for character in value.lower()
        if character.isascii() and (character.isalnum() or character == "_")
    )
    return accepted[:64] or "proxy_failure"


def _safe_host_text(value: str) -> str:
    try:
        normalized = value.rstrip(".").lower().encode("idna").decode("ascii")
    except (UnicodeError, AttributeError):
        return "-"
    if not normalized or len(normalized) > 253:
        return "-"
    if any(
        not (character.isalnum() or character in ".-:")
        for character in normalized
    ):
        return "-"
    return normalized


def _unix_socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        information = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(information.st_mode):
        raise FileExistsError("refusing to replace a non-socket filesystem entry")
    return information.st_dev, information.st_ino


def _unlink_same_unix_socket(path: Path, identity: tuple[int, int]) -> bool:
    try:
        current = _unix_socket_identity(path)
    except FileExistsError:
        return False
    if current != identity:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "AuditSink",
    "Connector",
    "ControlledForwardProxy",
    "ForwardProxyLimits",
    "ProxyAuditEvent",
    "UnixConnector",
    "UnixServerFactory",
]

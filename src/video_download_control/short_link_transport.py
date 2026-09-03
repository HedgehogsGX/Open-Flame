"""Authenticated control-plane to egress transport for one short-link hop.

The control plane resolves and approves every DNS answer, then sends the exact
``ResolvedTarget`` over a private Unix socket.  The egress service never
resolves the hostname again: it connects to an approved numeric address while
preserving the validated hostname for TLS SNI and HTTP ``Host``.  Requests and
responses use canonical JSON, domain-separated HMAC-SHA256, a fresh nonce, a
short validity window, and an atomically consumed replay marker.

Only fixed reason codes cross this boundary.  URLs, redirect ``Location``
values, selectors, response bodies, and upstream exception text are never
included in errors or audit events.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import ssl
import stat
import struct
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .security.egress import (
    EgressPolicyError,
    ResolvedTarget,
    assert_connected_peer,
    resolve_public_target,
)
from .short_links import ShortLinkResolutionError, ShortLinkResponse

PROTOCOL_VERSION = 1
MIN_SHARED_KEY_BYTES = 32
MAX_SHARED_KEY_BYTES = 4096
_REQUEST_DOMAIN = b"vdc-short-link-request-v1\0"
_RESPONSE_DOMAIN = b"vdc-short-link-response-v1\0"
_NONCE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HEADER_NAME = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MEDIA_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_ALLOWED_REQUEST_HEADERS = frozenset({"accept", "range", "user-agent"})
_GENERIC_FAILURE_REASON = "transport_failure"
_EGRESS_RESPONSE_REASONS = frozenset(
    {
        "authentication",
        "clock_invalid",
        "connect_failed",
        "connect_timeout",
        "deadline",
        "freshness",
        "headers_invalid",
        "method_invalid",
        "peer_mismatch",
        "replay",
        "replay_store_failure",
        "request_frame_limit",
        "request_header_limit",
        "request_invalid",
        "response_body_framing",
        "response_body_incomplete",
        "response_body_limit",
        "response_header_limit",
        "response_headers_invalid",
        "response_status_invalid",
        "schema_invalid",
        "target_invalid",
        _GENERIC_FAILURE_REASON,
        "upstream_failure",
    }
)
_CLIENT_FAILURE_REASONS = frozenset(
    {
        "clock_invalid",
        "exchange_failed",
        "exchange_incomplete",
        "exchange_timeout",
        "headers_invalid",
        "method_invalid",
        "nonce_invalid",
        "nonce_reused",
        "request_frame_limit",
        "request_header_limit",
        "request_invalid",
        "response_attestation",
        "response_authentication",
        "response_binding",
        "response_frame_limit",
        "response_freshness",
        "response_header_limit",
        "response_schema",
        "schema_invalid",
        "target_invalid",
        "unix_socket_identity",
        "unix_socket_unavailable",
        "unix_socket_unsafe",
    }
)
_AUDIT_REASONS = _EGRESS_RESPONSE_REASONS | frozenset(
    {
        "connection_limit",
        "frame_timeout",
        "incomplete_frame",
        "response_frame_limit",
    }
)
_INTERNAL_FAILURE_REASONS = _CLIENT_FAILURE_REASONS | _AUDIT_REASONS
_PUBLIC_FAILURE_REASONS = frozenset(
    {_GENERIC_FAILURE_REASON}
    | {f"transport_{reason}" for reason in _CLIENT_FAILURE_REASONS}
    | {
        f"egress_{reason}"
        for reason in _EGRESS_RESPONSE_REASONS
        if reason != _GENERIC_FAILURE_REASON
    }
)


def _allowlisted_reason(reason: object, allowed: frozenset[str]) -> str:
    if isinstance(reason, str) and reason in allowed:
        return reason
    return _GENERIC_FAILURE_REASON


def _public_transport_reason(reason: object) -> str:
    safe_reason = _allowlisted_reason(reason, _CLIENT_FAILURE_REASONS)
    if safe_reason == _GENERIC_FAILURE_REASON:
        return _GENERIC_FAILURE_REASON
    return f"transport_{safe_reason}"


def _public_egress_reason(reason: object) -> str:
    safe_reason = _allowlisted_reason(reason, _EGRESS_RESPONSE_REASONS)
    if safe_reason == _GENERIC_FAILURE_REASON:
        return _GENERIC_FAILURE_REASON
    return f"egress_{safe_reason}"


class ShortLinkTransportConfigurationError(ValueError):
    """The signed transport cannot safely use its key or local endpoint."""


class _TransportFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = _allowlisted_reason(reason, _INTERNAL_FAILURE_REASONS)
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class ShortLinkTransportLimits:
    max_connections: int = 16
    max_addresses: int = 16
    max_request_headers: int = 16
    max_response_headers: int = 64
    max_request_header_bytes: int = 8 * 1024
    max_response_header_bytes: int = 32 * 1024
    max_body_bytes: int = 8 * 1024
    max_request_frame_bytes: int = 32 * 1024
    max_response_frame_bytes: int = 64 * 1024
    max_timeout_seconds: float = 10.0
    max_validity_seconds: float = 20.0
    clock_skew_seconds: float = 2.0
    frame_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        integers = (
            self.max_connections,
            self.max_addresses,
            self.max_request_headers,
            self.max_response_headers,
            self.max_request_header_bytes,
            self.max_response_header_bytes,
            self.max_body_bytes,
            self.max_request_frame_bytes,
            self.max_response_frame_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise ValueError("short-link transport integer limits must be positive")
        integer_caps = (
            (self.max_connections, 256),
            (self.max_addresses, 64),
            (self.max_request_headers, 64),
            (self.max_response_headers, 256),
            (self.max_request_header_bytes, 64 * 1024),
            (self.max_response_header_bytes, 256 * 1024),
            (self.max_body_bytes, 64 * 1024),
            (self.max_request_frame_bytes, 256 * 1024),
            (self.max_response_frame_bytes, 512 * 1024),
        )
        if any(value > maximum for value, maximum in integer_caps):
            raise ValueError("short-link transport integer limits exceed hard bounds")
        durations = (
            self.max_timeout_seconds,
            self.max_validity_seconds,
            self.clock_skew_seconds,
            self.frame_timeout_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in durations
        ) or (
            self.max_timeout_seconds == 0
            or self.max_validity_seconds == 0
            or self.frame_timeout_seconds == 0
        ):
            raise ValueError("short-link transport time limits are invalid")
        if (
            self.max_timeout_seconds > 60
            or self.max_validity_seconds > 120
            or self.clock_skew_seconds > 30
            or self.frame_timeout_seconds > 30
            or self.max_validity_seconds < self.max_timeout_seconds + 1
        ):
            raise ValueError("short-link transport time limits exceed hard bounds")
        if self.max_request_frame_bytes < self.max_request_header_bytes:
            raise ValueError("request frame limit must cover request headers")
        if self.max_response_frame_bytes < self.max_response_header_bytes:
            raise ValueError("response frame limit must cover response headers")


@dataclass(frozen=True, slots=True)
class ShortLinkTransportAuditEvent:
    """Sparse by design: upstream material must never enter ordinary logs."""

    reason: str


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...

    def get_extra_info(self, name: str, default: object = None) -> object: ...


class NumericConnector(Protocol):
    def __call__(
        self,
        address: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        server_hostname: str,
        stream_limit: int,
    ) -> Awaitable[tuple[asyncio.StreamReader, _Writer]]: ...


FrameExchange = Callable[[bytes, float], bytes]
AuditSink = Callable[[ShortLinkTransportAuditEvent], None]
NonceFactory = Callable[[], str]
Clock = Callable[[], float]


class ReplayStore(Protocol):
    def consume(self, nonce: str, *, expires_at_ms: int, now_ms: int) -> bool: ...


class MemoryReplayStore:
    """Thread-safe replay store for tests or one non-restarting process."""

    def __init__(self) -> None:
        self._expiries: dict[str, int] = {}
        self._lock = threading.Lock()

    def consume(self, nonce: str, *, expires_at_ms: int, now_ms: int) -> bool:
        if _NONCE.fullmatch(nonce) is None:
            return False
        with self._lock:
            self._expiries = {
                key: expiry
                for key, expiry in self._expiries.items()
                if expiry >= now_ms
            }
            if nonce in self._expiries:
                return False
            self._expiries[nonce] = expires_at_ms
            return True


class FileReplayStore:
    """Crash-durable nonce markers in one private, bounded-GC directory.

    The leaf directory is created with mode ``0700`` when absent, but every
    parent must already exist and pass the no-link path checks.  Garbage
    collection keeps a live directory iterator so each call has hard scan and
    delete caps while successive calls still make progress past unknown files.
    """

    def __init__(
        self,
        root: Path,
        *,
        gc_scan_limit: int = 128,
        gc_delete_limit: int = 32,
    ) -> None:
        root = _normalized_absolute_path(root, reason="replay_path_invalid")
        _require_safe_parent_components(root)
        if (
            isinstance(gc_scan_limit, bool)
            or not isinstance(gc_scan_limit, int)
            or gc_scan_limit <= 0
            or isinstance(gc_delete_limit, bool)
            or not isinstance(gc_delete_limit, int)
            or not 0 < gc_delete_limit <= gc_scan_limit
        ):
            raise ShortLinkTransportConfigurationError(
                "replay garbage-collection limits are invalid"
            )
        try:
            root.mkdir(mode=0o700)
            _sync_directory(root.parent)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ShortLinkTransportConfigurationError(
                "replay directory could not be created"
            ) from exc
        self.root = root
        self.gc_scan_limit = gc_scan_limit
        self.gc_delete_limit = gc_delete_limit
        self._parent_identity = _directory_identity(root.parent)
        self._identity = self._safe_identity()
        self._lock = threading.Lock()
        self._gc_iterator: Iterator[os.DirEntry[str]] | None = None
        self._gc_pending: list[str] = []

    def _safe_identity(self) -> tuple[int, int]:
        try:
            info = self.root.lstat()
        except OSError as exc:
            raise ShortLinkTransportConfigurationError(
                "replay directory is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
            or not _owned_by_effective_user(info)
            or (os.name == "posix" and info.st_mode & 0o077)
        ):
            raise ShortLinkTransportConfigurationError(
                "replay directory must be a private plain directory"
            )
        return int(info.st_dev), int(info.st_ino)

    def consume(self, nonce: str, *, expires_at_ms: int, now_ms: int) -> bool:
        if _NONCE.fullmatch(nonce) is None:
            return False
        with self._lock:
            _require_safe_parent_components(self.root)
            if (
                _directory_identity(self.root.parent) != self._parent_identity
                or self._safe_identity() != self._identity
            ):
                raise ShortLinkTransportConfigurationError(
                    "replay directory identity changed"
                )
            self._garbage_collect(now_ms=now_ms)
            marker = self.root / nonce
            self._discard_expired_marker(marker, now_ms=now_ms)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(marker, flags, 0o600)
            except FileExistsError:
                return False
            except OSError as exc:
                raise ShortLinkTransportConfigurationError(
                    "replay marker could not be created"
                ) from exc
            try:
                payload = f"{expires_at_ms}\n".encode("ascii")
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short replay marker write")
                    view = view[written:]
                os.fsync(descriptor)
            except OSError as exc:
                # Keep an incomplete exclusive marker: fail closed across a crash.
                raise ShortLinkTransportConfigurationError(
                    "replay marker could not be persisted"
                ) from exc
            finally:
                os.close(descriptor)
            _sync_directory(self.root)
            return True

    def _garbage_collect(self, *, now_ms: int) -> None:
        deleted = 0
        examined = 0
        while (
            self._gc_pending
            and deleted < self.gc_delete_limit
            and examined < self.gc_scan_limit
        ):
            name = self._gc_pending.pop()
            examined += 1
            if self._discard_expired_marker(self.root / name, now_ms=now_ms):
                deleted += 1
        if deleted >= self.gc_delete_limit or examined >= self.gc_scan_limit:
            return

        scanned = 0
        candidates: list[str] = []
        while examined + scanned < self.gc_scan_limit:
            if self._gc_iterator is None:
                try:
                    self._gc_iterator = os.scandir(self.root)
                except OSError:
                    return
            try:
                entry = next(self._gc_iterator)
            except StopIteration:
                self._gc_iterator.close()
                self._gc_iterator = None
                # Directory mutation during deletion may cause an iterator to
                # skip an entry.  A fresh pass is still bounded by ``scanned``.
                if scanned == 0:
                    break
                # One new pass catches names skipped by a directory iterator
                # invalidated by the prior call's bounded deletions.
                if candidates:
                    break
                continue
            except OSError:
                self._gc_iterator.close()
                self._gc_iterator = None
                return
            scanned += 1
            if _NONCE.fullmatch(entry.name) is None:
                continue
            candidates.append(entry.name)

        while candidates and deleted < self.gc_delete_limit:
            name = candidates.pop()
            if self._discard_expired_marker(self.root / name, now_ms=now_ms):
                deleted += 1
        self._gc_pending.extend(candidates)

    @staticmethod
    def _discard_expired_marker(marker: Path, *, now_ms: int) -> bool:
        try:
            info = marker.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
            or not _owned_by_effective_user(info)
            or info.st_nlink != 1
        ):
            return False
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags)
        except OSError:
            return False
        try:
            opened = os.fstat(descriptor)
            after_open = marker.lstat()
            if not _same_stable_file(
                info,
                opened,
                after_open,
            ) or not _same_ctime(info, after_open):
                return False
            payload = os.read(descriptor, 33)
            after_read = os.fstat(descriptor)
            after_path = marker.lstat()
            if (
                not _same_stable_file(opened, after_read, after_path)
                or not _same_ctime(opened, after_read)
                or not _same_ctime(after_open, after_path)
            ):
                return False
            if len(payload) > 32 or not payload.endswith(b"\n"):
                return False
            expiry = int(payload[:-1].decode("ascii"))
        except (OSError, UnicodeError, ValueError):
            return False
        finally:
            os.close(descriptor)
        if expiry >= now_ms:
            return False
        try:
            current = marker.lstat()
            if not _same_stable_file(after_path, current) or not _same_ctime(
                after_path,
                current,
            ):
                return False
            marker.unlink()
        except OSError:
            return False
        return True

    def close(self) -> None:
        """Close an in-progress bounded directory scan."""

        with self._lock:
            if self._gc_iterator is not None:
                self._gc_iterator.close()
                self._gc_iterator = None


@dataclass(frozen=True, slots=True)
class _FetchRequest:
    target: ResolvedTarget
    method: str
    headers: tuple[tuple[str, str], ...]
    max_header_bytes: int
    max_body_bytes: int
    timeout_seconds: float


class SignedShortLinkTransport:
    """Synchronous ``ShortLinkTransport`` client for a private egress service."""

    def __init__(
        self,
        *,
        shared_key: bytes,
        exchange: FrameExchange | None = None,
        unix_socket: Path | None = None,
        limits: ShortLinkTransportLimits | None = None,
        clock: Clock | None = None,
        nonce_factory: NonceFactory | None = None,
    ) -> None:
        self.shared_key = _validated_key(shared_key)
        self.limits = limits or ShortLinkTransportLimits()
        self.clock = clock or time.time
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))
        if (exchange is None) == (unix_socket is None):
            raise ShortLinkTransportConfigurationError(
                "configure exactly one frame exchange or Unix socket"
            )
        if unix_socket is not None:
            self.unix_socket = _absolute_socket_path(unix_socket)
            self.exchange = self._unix_exchange
        else:
            self.unix_socket = None
            assert exchange is not None
            self.exchange = exchange
        self._issued_nonces: dict[str, int] = {}
        self._nonce_lock = threading.Lock()

    def request(
        self,
        target: ResolvedTarget,
        *,
        method: str,
        headers: Sequence[tuple[str, str]],
        max_header_bytes: int,
        max_body_bytes: int,
        timeout_seconds: float,
    ) -> ShortLinkResponse:
        try:
            return self._request(
                target,
                method=method,
                headers=headers,
                max_header_bytes=max_header_bytes,
                max_body_bytes=max_body_bytes,
                timeout_seconds=timeout_seconds,
            )
        except ShortLinkResolutionError as failure:
            reason = _allowlisted_reason(
                failure.reason,
                _PUBLIC_FAILURE_REASONS,
            )
        except Exception:  # noqa: BLE001 - public trust boundary redacts all causes
            reason = _GENERIC_FAILURE_REASON
        raise ShortLinkResolutionError(reason, "短链 egress transport 请求失败")

    def _request(
        self,
        target: ResolvedTarget,
        *,
        method: str,
        headers: Sequence[tuple[str, str]],
        max_header_bytes: int,
        max_body_bytes: int,
        timeout_seconds: float,
    ) -> ShortLinkResponse:
        try:
            request = _validated_fetch_request(
                target=target,
                method=method,
                headers=headers,
                max_header_bytes=max_header_bytes,
                max_body_bytes=max_body_bytes,
                timeout_seconds=timeout_seconds,
                limits=self.limits,
            )
            issued_at_ms = _now_ms(self.clock)
            validity_ms = min(
                int(self.limits.max_validity_seconds * 1000),
                max(1000, int(timeout_seconds * 1000) + 1000),
            )
            expires_at_ms = issued_at_ms + validity_ms
            nonce = self._new_nonce(
                now_ms=issued_at_ms,
                expires_at_ms=(
                    expires_at_ms + int(self.limits.clock_skew_seconds * 1000)
                ),
            )
            packet = _encode_request(
                request,
                nonce=nonce,
                issued_at_ms=issued_at_ms,
                expires_at_ms=expires_at_ms,
                shared_key=self.shared_key,
            )
            if len(packet) > self.limits.max_request_frame_bytes:
                raise _TransportFailure("request_frame_limit")
            request_hash = hashlib.sha256(packet).hexdigest()
            response_packet = self.exchange(packet, timeout_seconds)
            return _decode_response(
                response_packet,
                shared_key=self.shared_key,
                expected_nonce=nonce,
                expected_request_hash=request_hash,
                expected_target=request.target,
                request_issued_at_ms=issued_at_ms,
                request_expires_at_ms=expires_at_ms,
                now_ms=_now_ms(self.clock),
                limits=self.limits,
                requested_header_bytes=max_header_bytes,
            )
        except ShortLinkResolutionError:
            raise
        except _TransportFailure as exc:
            raise ShortLinkResolutionError(
                _public_transport_reason(exc.reason),
                "短链 egress transport 请求失败",
            ) from None
        except Exception:  # noqa: BLE001 - redact arbitrary exchange failures
            raise ShortLinkResolutionError(
                _GENERIC_FAILURE_REASON,
                "短链 egress transport 请求失败",
            ) from None

    def _new_nonce(self, *, now_ms: int, expires_at_ms: int) -> str:
        with self._nonce_lock:
            self._issued_nonces = {
                nonce: expiry
                for nonce, expiry in self._issued_nonces.items()
                if expiry >= now_ms
            }
            for _ in range(8):
                nonce = self.nonce_factory()
                if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
                    raise _TransportFailure("nonce_invalid")
                if nonce not in self._issued_nonces:
                    self._issued_nonces[nonce] = expires_at_ms
                    return nonce
        raise _TransportFailure("nonce_reused")

    def _unix_exchange(self, packet: bytes, timeout_seconds: float) -> bytes:
        if self.unix_socket is None or not hasattr(socket, "AF_UNIX"):
            raise _TransportFailure("unix_socket_unavailable")
        frame = struct.pack("!I", len(packet)) + packet
        deadline = time.monotonic() + timeout_seconds
        try:
            parent_identity = _safe_socket_parent_identity(self.unix_socket)
            socket_identity = _require_unix_socket_identity(
                self.unix_socket,
                require_private=True,
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(_socket_time_remaining(deadline))
                client.connect(str(self.unix_socket))
                if (
                    _safe_socket_parent_identity(self.unix_socket) != parent_identity
                    or _require_unix_socket_identity(
                        self.unix_socket,
                        require_private=True,
                    )
                    != socket_identity
                ):
                    raise _TransportFailure("unix_socket_identity")
                client.settimeout(_socket_time_remaining(deadline))
                client.sendall(frame)
                raw_size = _recv_exact(client, 4, deadline=deadline)
                size = struct.unpack("!I", raw_size)[0]
                if not 1 <= size <= self.limits.max_response_frame_bytes:
                    raise _TransportFailure("response_frame_limit")
                return _recv_exact(client, size, deadline=deadline)
        except _TransportFailure:
            raise
        except ShortLinkTransportConfigurationError:
            raise _TransportFailure("unix_socket_unsafe") from None
        except (OSError, TimeoutError):
            raise _TransportFailure("exchange_failed") from None


class UnixAttestedShortLinkTransport(SignedShortLinkTransport):
    """Convenient synchronous UDS transport for control-plane construction."""

    def __init__(
        self,
        *,
        unix_socket: Path,
        shared_key: bytes,
        limits: ShortLinkTransportLimits | None = None,
        clock: Clock | None = None,
        nonce_factory: NonceFactory | None = None,
    ) -> None:
        super().__init__(
            shared_key=shared_key,
            unix_socket=unix_socket,
            limits=limits,
            clock=clock,
            nonce_factory=nonce_factory,
        )


class ShortLinkEgressService:
    """Authenticated, replay-safe, single-hop HTTPS fetch service."""

    def __init__(
        self,
        *,
        shared_key: bytes,
        replay_store: ReplayStore,
        limits: ShortLinkTransportLimits | None = None,
        clock: Clock | None = None,
        connector: NumericConnector | None = None,
        ssl_context: ssl.SSLContext | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.shared_key = _validated_key(shared_key)
        self.replay_store = replay_store
        self.limits = limits or ShortLinkTransportLimits()
        self.clock = clock or time.time
        self.connector = connector or self._open_numeric_connection
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.audit = audit
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_connections = 0
        self._closing = False
        self._unix_path: Path | None = None
        self._unix_identity: tuple[int, int] | None = None
        self._unix_parent_identity: tuple[int, int] | None = None
        self._replay_worker_limit = min(4, self.limits.max_connections)
        self._replay_slots = threading.BoundedSemaphore(self._replay_worker_limit)
        self._replay_executor: ThreadPoolExecutor | None = None

    @property
    def active_connections(self) -> int:
        return self._active_connections

    async def handle_frame(self, packet: bytes) -> bytes:
        """Handle one already framed payload; public for deterministic tests."""

        if (
            not isinstance(packet, bytes)
            or not 1 <= len(packet) <= self.limits.max_request_frame_bytes
        ):
            return self._error_packet(
                nonce="0" * 64,
                request_hash=hashlib.sha256(
                    bytes(packet) if isinstance(packet, bytes) else b""
                ).hexdigest(),
                reason="request_frame_limit",
            )
        request_hash = hashlib.sha256(packet).hexdigest()
        nonce = "0" * 64
        try:
            envelope = _strict_json_mapping(packet)
            raw_nonce = envelope.get("nonce")
            if isinstance(raw_nonce, str) and _NONCE.fullmatch(raw_nonce):
                nonce = raw_nonce
            request, _issued_at_ms, expires_at_ms = _decode_request(
                packet,
                shared_key=self.shared_key,
                now_ms=_now_ms(self.clock),
                limits=self.limits,
            )
            nonce = envelope["nonce"]
            consumed = await self._consume_replay(
                nonce,
                expires_at_ms=(
                    expires_at_ms + int(self.limits.clock_skew_seconds * 1000)
                ),
            )
            if not consumed:
                raise _TransportFailure("replay")
            if _now_ms(self.clock) > (
                expires_at_ms + int(self.limits.clock_skew_seconds * 1000)
            ):
                raise _TransportFailure("freshness")
            response = await self._fetch(request)
            return _encode_response(
                nonce=nonce,
                request_hash=request_hash,
                response=response,
                reason=None,
                issued_at_ms=_now_ms(self.clock),
                shared_key=self.shared_key,
            )
        except _TransportFailure as exc:
            self._emit(exc.reason)
            return self._error_packet(
                nonce=nonce,
                request_hash=request_hash,
                reason=exc.reason,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - trust-boundary catch and redaction
            self._emit("transport_failure")
            return self._error_packet(
                nonce=nonce,
                request_hash=request_hash,
                reason="transport_failure",
            )

    async def _consume_replay(self, nonce: str, *, expires_at_ms: int) -> bool:
        now_ms = _now_ms(self.clock)
        remaining = (expires_at_ms - now_ms) / 1000
        if remaining <= 0:
            raise _TransportFailure("freshness")
        if isinstance(self.replay_store, MemoryReplayStore):
            try:
                return self.replay_store.consume(
                    nonce,
                    expires_at_ms=expires_at_ms,
                    now_ms=now_ms,
                )
            except Exception:  # noqa: BLE001 - replay backends are injected
                raise _TransportFailure("replay_store_failure") from None

        if not self._replay_slots.acquire(blocking=False):
            raise _TransportFailure("replay_store_failure")
        executor = self._replay_executor
        if executor is None:
            try:
                executor = ThreadPoolExecutor(
                    max_workers=self._replay_worker_limit,
                    thread_name_prefix="vdc-short-link-replay",
                )
            except Exception:  # noqa: BLE001 - fixed-code failure boundary
                self._replay_slots.release()
                raise _TransportFailure("replay_store_failure") from None
            self._replay_executor = executor

        def consume() -> bool:
            try:
                return self.replay_store.consume(
                    nonce,
                    expires_at_ms=expires_at_ms,
                    now_ms=now_ms,
                )
            finally:
                self._replay_slots.release()

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(executor, consume)
        except Exception:  # noqa: BLE001 - executor submission is fail-closed
            self._replay_slots.release()
            raise _TransportFailure("replay_store_failure") from None
        try:
            async with asyncio.timeout(
                min(remaining, self.limits.frame_timeout_seconds)
            ):
                # The worker owns the semaphore slot until its non-cancellable
                # filesystem operation really returns.
                return await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - replay failures stay fixed-code only
            raise _TransportFailure("replay_store_failure") from None

    async def _fetch(self, request: _FetchRequest) -> ShortLinkResponse:
        upstream_request = _http_request_bytes(request)
        if len(upstream_request) > self.limits.max_request_header_bytes:
            raise _TransportFailure("request_header_limit")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_seconds
        writer: _Writer | None = None
        try:
            async with asyncio.timeout_at(deadline):
                reader, writer, peer_ip = await self._connect(request.target, deadline)
                writer.write(upstream_request)
                await writer.drain()
                status, headers, body = await _read_http_response(
                    reader,
                    max_header_bytes=request.max_header_bytes,
                    max_body_bytes=request.max_body_bytes,
                    max_headers=self.limits.max_response_headers,
                )
                del body
                disclosed_headers = tuple(
                    header for header in headers if header[0].lower() == "location"
                )
                return ShortLinkResponse(
                    status=status,
                    headers=disclosed_headers,
                    body=b"",
                    peer_ip=peer_ip,
                    effective_url=request.target.url,
                )
        except _TransportFailure:
            raise
        except TimeoutError:
            raise _TransportFailure("deadline") from None
        except asyncio.CancelledError:
            raise
        except (OSError, ssl.SSLError):
            raise _TransportFailure("upstream_failure") from None
        finally:
            if writer is not None:
                await _close_writer(writer, deadline=deadline)

    async def _connect(
        self, target: ResolvedTarget, deadline: float
    ) -> tuple[asyncio.StreamReader, _Writer, str]:
        loop = asyncio.get_running_loop()
        last_reason = "connect_failed"
        for address in target.addresses:
            writer: _Writer | None = None
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _TransportFailure("deadline")
            try:
                reader, writer = await asyncio.wait_for(
                    self.connector(
                        address,
                        target.port,
                        ssl_context=self.ssl_context,
                        server_hostname=target.host,
                        stream_limit=self.limits.max_response_header_bytes + 1,
                    ),
                    timeout=remaining,
                )
                peer_ip = _peer_ip(writer.get_extra_info("peername"))
                assert_connected_peer(target, peer_ip)
                return reader, writer, peer_ip
            except TimeoutError:
                last_reason = "connect_timeout"
            except EgressPolicyError:
                last_reason = "peer_mismatch"
            except asyncio.CancelledError:
                raise
            except (OSError, ssl.SSLError, ValueError):
                last_reason = "connect_failed"
            if writer is not None:
                await _close_writer(writer, deadline=deadline)
        raise _TransportFailure(last_reason)

    @staticmethod
    async def _open_numeric_connection(
        address: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        server_hostname: str,
        stream_limit: int,
    ) -> tuple[asyncio.StreamReader, _Writer]:
        parsed = ipaddress.ip_address(address)
        return await asyncio.open_connection(
            host=parsed.compressed,
            port=port,
            family=socket.AF_INET6 if parsed.version == 6 else socket.AF_INET,
            flags=socket.AI_NUMERICHOST,
            ssl=ssl_context,
            server_hostname=server_hostname,
            limit=stream_limit,
        )

    def _error_packet(self, *, nonce: str, request_hash: str, reason: str) -> bytes:
        return _encode_response(
            nonce=nonce if _NONCE.fullmatch(nonce) else "0" * 64,
            request_hash=(
                request_hash if _DIGEST.fullmatch(request_hash) else "0" * 64
            ),
            response=None,
            reason=reason,
            issued_at_ms=_now_ms(self.clock),
            shared_key=self.shared_key,
        )

    async def start_unix(
        self, path: Path, *, mode: int = 0o600
    ) -> asyncio.AbstractServer:
        if self._server is not None or self._closing:
            raise RuntimeError("short-link egress service is already active")
        socket_path = _absolute_socket_path(path)
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or not 0 <= mode <= 0o777
            or mode & 0o077
        ):
            raise ShortLinkTransportConfigurationError(
                "egress Unix socket mode must be private"
            )
        parent_identity = _safe_socket_parent_identity(socket_path)
        try:
            socket_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ShortLinkTransportConfigurationError(
                "egress Unix socket path is unavailable"
            ) from exc
        else:
            raise ShortLinkTransportConfigurationError(
                "egress Unix socket path already exists"
            )
        if not hasattr(asyncio, "start_unix_server"):
            raise ShortLinkTransportConfigurationError("Unix sockets are unavailable")
        server = await asyncio.start_unix_server(
            self._client_connected,
            path=str(socket_path),
            limit=self.limits.max_request_frame_bytes + 4,
        )
        identity: tuple[int, int] | None = None
        try:
            identity = _require_unix_socket_identity(
                socket_path,
                require_private=False,
            )
            os.chmod(socket_path, mode, follow_symlinks=False)
            if (
                _safe_socket_parent_identity(socket_path) != parent_identity
                or _require_unix_socket_identity(
                    socket_path,
                    require_private=True,
                )
                != identity
            ):
                raise ShortLinkTransportConfigurationError(
                    "egress Unix socket identity changed during startup"
                )
        except BaseException:
            server.close()
            await server.wait_closed()
            if identity is not None:
                _unlink_same_unix_socket(
                    socket_path,
                    identity,
                    parent_identity=parent_identity,
                )
            raise
        self._server = server
        self._unix_path = socket_path
        self._unix_identity = identity
        self._unix_parent_identity = parent_identity
        return server

    async def serve_unix_forever(self, path: Path, *, mode: int = 0o600) -> None:
        server = await self.start_unix(path, mode=mode)
        try:
            async with server:
                await server.serve_forever()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closing:
            while self._closing:
                await asyncio.sleep(0)
            return
        self._closing = True
        server, self._server = self._server, None
        unix_path, self._unix_path = self._unix_path, None
        unix_identity, self._unix_identity = self._unix_identity, None
        unix_parent_identity, self._unix_parent_identity = (
            self._unix_parent_identity,
            None,
        )
        replay_executor, self._replay_executor = self._replay_executor, None
        try:
            if server is not None:
                server.close()
                await server.wait_closed()
            while self._tasks:
                tasks = tuple(self._tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if unix_path is not None and unix_identity is not None:
                _unlink_same_unix_socket(
                    unix_path,
                    unix_identity,
                    parent_identity=unix_parent_identity,
                )
            if replay_executor is not None:
                replay_executor.shutdown(wait=False, cancel_futures=True)
            self._closing = False

    def _client_connected(self, reader: asyncio.StreamReader, writer: _Writer) -> None:
        if self._closing or self._active_connections >= self.limits.max_connections:
            self._emit("connection_limit")
            task = asyncio.create_task(_close_writer(writer))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        self._active_connections += 1
        task = asyncio.create_task(self._handle_connection(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: _Writer
    ) -> None:
        try:
            async with asyncio.timeout(self.limits.frame_timeout_seconds):
                raw_size = await reader.readexactly(4)
                size = struct.unpack("!I", raw_size)[0]
                if not 1 <= size <= self.limits.max_request_frame_bytes:
                    raise _TransportFailure("request_frame_limit")
                packet = await reader.readexactly(size)
            response = await self.handle_frame(packet)
            if len(response) > self.limits.max_response_frame_bytes:
                raise _TransportFailure("response_frame_limit")
            async with asyncio.timeout(self.limits.frame_timeout_seconds):
                writer.write(struct.pack("!I", len(response)) + response)
                await writer.drain()
        except asyncio.IncompleteReadError:
            self._emit("incomplete_frame")
        except TimeoutError:
            self._emit("frame_timeout")
        except _TransportFailure as exc:
            self._emit(exc.reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - connection boundary must not escape
            self._emit("transport_failure")
        finally:
            self._active_connections -= 1
            await _close_writer(writer)

    def _emit(self, reason: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit(
                ShortLinkTransportAuditEvent(
                    reason=_allowlisted_reason(reason, _AUDIT_REASONS)
                )
            )
        except Exception:  # noqa: BLE001,S110 - audit sinks cannot affect service
            pass


def _validated_key(value: bytes) -> bytes:
    if (
        not isinstance(value, bytes)
        or not MIN_SHARED_KEY_BYTES <= len(value) <= MAX_SHARED_KEY_BYTES
    ):
        raise ShortLinkTransportConfigurationError(
            "shared key must be bounded binary secret material"
        )
    return bytes(value)


def load_shared_key(path: Path) -> bytes:
    """Load exact key bytes from one stable, private, no-follow file."""

    key_path = _normalized_absolute_path(path, reason="key_path_invalid")
    _require_safe_parent_components(key_path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        before = key_path.lstat()
        _require_private_key_info(before)
        descriptor = os.open(key_path, flags)
        opened = os.fstat(descriptor)
        after_open = key_path.lstat()
        _require_private_key_info(opened)
        _require_private_key_info(after_open)
        if not _same_stable_file(
            before,
            opened,
            after_open,
        ) or not _same_ctime(before, after_open):
            raise ShortLinkTransportConfigurationError(
                "shared key file changed before reading"
            )
        payload = bytearray()
        while len(payload) <= MAX_SHARED_KEY_BYTES:
            chunk = os.read(descriptor, MAX_SHARED_KEY_BYTES + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        after_read = os.fstat(descriptor)
        after_path = key_path.lstat()
        _require_private_key_info(after_read)
        _require_private_key_info(after_path)
        _require_safe_parent_components(key_path)
        if (
            not _same_stable_file(opened, after_read, after_path)
            or not _same_ctime(opened, after_read)
            or not _same_ctime(after_open, after_path)
        ):
            raise ShortLinkTransportConfigurationError(
                "shared key file changed during reading"
            )
        return _validated_key(bytes(payload))
    except ShortLinkTransportConfigurationError:
        raise
    except OSError as exc:
        raise ShortLinkTransportConfigurationError(
            "shared key file could not be read safely"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_private_key_info(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
        or not _owned_by_effective_user(info)
        or info.st_nlink != 1
        or not MIN_SHARED_KEY_BYTES <= info.st_size <= MAX_SHARED_KEY_BYTES
        or (os.name == "posix" and info.st_mode & 0o077)
    ):
        raise ShortLinkTransportConfigurationError(
            "shared key file must be a bounded private plain file"
        )


def _normalized_absolute_path(path: Path, *, reason: str) -> Path:
    if not isinstance(path, Path):
        raise ShortLinkTransportConfigurationError(reason)
    spelling = str(path)
    windows_parts_ambiguous = os.name == "nt" and any(
        ":" in component or component.endswith((" ", "."))
        for component in path.parts[1:]
    )
    ambiguous_root = (os.name == "posix" and spelling.startswith("//")) or (
        os.name == "nt" and spelling.startswith("\\\\")
    )
    if (
        not path.is_absolute()
        or not spelling
        or "\x00" in spelling
        or ambiguous_root
        or windows_parts_ambiguous
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in spelling
        )
        or os.path.normpath(spelling) != spelling
    ):
        raise ShortLinkTransportConfigurationError(reason)
    return path


def _path_components(directory: Path) -> tuple[Path, ...]:
    parents = tuple(reversed(directory.parents))
    if parents and parents[-1] == directory:
        return parents
    return (*parents, directory)


def _require_safe_parent_components(path: Path) -> None:
    for component in _path_components(path.parent):
        try:
            info = component.lstat()
        except OSError as exc:
            raise ShortLinkTransportConfigurationError(
                "private path parent is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
            or not _owned_by_root_or_effective_user(info)
            or (
                os.name == "posix"
                and info.st_mode & 0o022
                and not info.st_mode & stat.S_ISVTX
            )
        ):
            raise ShortLinkTransportConfigurationError("private path parent is unsafe")


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ShortLinkTransportConfigurationError(
            "private path parent is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
        or not _owned_by_root_or_effective_user(info)
    ):
        raise ShortLinkTransportConfigurationError("private path parent is unsafe")
    return int(info.st_dev), int(info.st_ino)


def _is_reparse(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(reparse and attributes & reparse)


def _effective_uid() -> int | None:
    getter = getattr(os, "geteuid", None)
    return int(getter()) if callable(getter) else None


def _owned_by_effective_user(info: os.stat_result) -> bool:
    effective_uid = _effective_uid()
    return effective_uid is None or int(info.st_uid) == effective_uid


def _owned_by_root_or_effective_user(info: os.stat_result) -> bool:
    effective_uid = _effective_uid()
    return effective_uid is None or int(info.st_uid) in {0, effective_uid}


def _same_stable_file(*values: os.stat_result) -> bool:
    if not values:
        return False
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
    )
    baseline = tuple(getattr(values[0], field, None) for field in fields)
    return all(
        tuple(getattr(value, field, None) for field in fields) == baseline
        for value in values[1:]
    )


def _same_ctime(first: os.stat_result, second: os.stat_result) -> bool:
    return getattr(first, "st_ctime_ns", None) == getattr(
        second,
        "st_ctime_ns",
        None,
    )


def _absolute_socket_path(path: Path) -> Path:
    socket_path = _normalized_absolute_path(path, reason="unix_socket_path_invalid")
    _require_safe_parent_components(socket_path)
    return socket_path


def _safe_socket_parent_identity(path: Path) -> tuple[int, int]:
    _require_safe_parent_components(path)
    try:
        info = path.parent.lstat()
    except OSError as exc:
        raise ShortLinkTransportConfigurationError(
            "egress Unix socket parent is unavailable"
        ) from exc
    if os.name == "posix" and info.st_mode & 0o077:
        raise ShortLinkTransportConfigurationError(
            "egress Unix socket parent must be private"
        )
    return _directory_identity(path.parent)


def _require_unix_socket_identity(
    path: Path,
    *,
    require_private: bool,
) -> tuple[int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ShortLinkTransportConfigurationError(
            "egress Unix socket is unavailable"
        ) from exc
    if (
        not stat.S_ISSOCK(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
        or not _owned_by_effective_user(info)
        or (require_private and os.name == "posix" and info.st_mode & 0o077)
    ):
        raise ShortLinkTransportConfigurationError("egress Unix socket path is unsafe")
    return int(info.st_dev), int(info.st_ino)


def _unlink_same_unix_socket(
    path: Path,
    identity: tuple[int, int],
    *,
    parent_identity: tuple[int, int] | None = None,
) -> bool:
    if parent_identity is not None:
        try:
            if _safe_socket_parent_identity(path) != parent_identity:
                return False
        except ShortLinkTransportConfigurationError:
            return False
    try:
        current = _require_unix_socket_identity(path, require_private=False)
    except ShortLinkTransportConfigurationError:
        return False
    if current != identity:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _now_ms(clock: Clock) -> int:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise _TransportFailure("clock_invalid")
    return int(value * 1000)


def _valid_int(value: object, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise _TransportFailure("schema_invalid")
    return value


def _valid_timeout(value: object, *, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise _TransportFailure("schema_invalid")
    return float(value)


def _validated_fetch_request(
    *,
    target: ResolvedTarget,
    method: object,
    headers: object,
    max_header_bytes: object,
    max_body_bytes: object,
    timeout_seconds: object,
    limits: ShortLinkTransportLimits,
) -> _FetchRequest:
    if not isinstance(target, ResolvedTarget):
        raise _TransportFailure("schema_invalid")
    if method != "GET":
        raise _TransportFailure("method_invalid")
    if isinstance(headers, (str, bytes)) or not isinstance(headers, Sequence):
        raise _TransportFailure("headers_invalid")
    if len(headers) > limits.max_request_headers:
        raise _TransportFailure("request_header_limit")
    normalized_headers: list[tuple[str, str]] = []
    seen: set[str] = set()
    header_bytes = 0
    for header in headers:
        if not isinstance(header, (tuple, list)) or len(header) != 2:
            raise _TransportFailure("headers_invalid")
        name, value = header
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or _MEDIA_HEADER_NAME.fullmatch(name) is None
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in value
            )
        ):
            raise _TransportFailure("headers_invalid")
        lowered = name.lower()
        if lowered in seen or lowered not in _ALLOWED_REQUEST_HEADERS:
            raise _TransportFailure("headers_invalid")
        seen.add(lowered)
        try:
            header_bytes += len(name.encode("ascii")) + len(value.encode("latin-1")) + 4
        except UnicodeEncodeError:
            raise _TransportFailure("headers_invalid") from None
        if header_bytes > limits.max_request_header_bytes:
            raise _TransportFailure("request_header_limit")
        normalized_headers.append((name, value))
    header_limit = _valid_int(
        max_header_bytes,
        minimum=1,
        maximum=limits.max_response_header_bytes,
    )
    body_limit = _valid_int(
        max_body_bytes,
        minimum=0,
        maximum=limits.max_body_bytes,
    )
    timeout = _valid_timeout(timeout_seconds, maximum=limits.max_timeout_seconds)
    target = _validated_target(target, limits=limits)
    request = _FetchRequest(
        target=target,
        method="GET",
        headers=tuple(normalized_headers),
        max_header_bytes=header_limit,
        max_body_bytes=body_limit,
        timeout_seconds=timeout,
    )
    if len(_http_request_bytes(request)) > limits.max_request_header_bytes:
        raise _TransportFailure("request_header_limit")
    return request


def _validated_target(
    target: ResolvedTarget, *, limits: ShortLinkTransportLimits
) -> ResolvedTarget:
    if (
        target.scheme != "https"
        or target.port != 443
        or not isinstance(target.url, str)
        or not isinstance(target.host, str)
        or not isinstance(target.addresses, tuple)
        or not 1 <= len(target.addresses) <= limits.max_addresses
        or len(set(target.addresses)) != len(target.addresses)
        or any(
            not 0x21 <= ord(character) <= 0x7E or character == "\\"
            for character in target.url
        )
    ):
        raise _TransportFailure("target_invalid")
    try:
        parts = urlsplit(target.url)
        if (
            parts.fragment
            or parts.hostname != target.host
            or (parts.path and not parts.path.startswith("/"))
            or parts.path.startswith("//")
        ):
            raise _TransportFailure("target_invalid")
        independently_checked = resolve_public_target(
            target.url,
            resolver=lambda host, port: target.addresses,
            allowed_hosts=(target.host,),
            allow_ip_literal=False,
        )
    except (_TransportFailure, EgressPolicyError, ValueError):
        raise _TransportFailure("target_invalid") from None
    if independently_checked != target:
        raise _TransportFailure("target_invalid")
    return target


def _request_payload(request: _FetchRequest) -> dict[str, object]:
    return {
        "target": {
            "url": request.target.url,
            "scheme": request.target.scheme,
            "host": request.target.host,
            "port": request.target.port,
            "addresses": list(request.target.addresses),
        },
        "method": request.method,
        "headers": [list(header) for header in request.headers],
        "max_header_bytes": request.max_header_bytes,
        "max_body_bytes": request.max_body_bytes,
        "timeout_ms": int(request.timeout_seconds * 1000),
    }


def _encode_request(
    request: _FetchRequest,
    *,
    nonce: str,
    issued_at_ms: int,
    expires_at_ms: int,
    shared_key: bytes,
) -> bytes:
    unsigned = {
        "version": PROTOCOL_VERSION,
        "kind": "request",
        "nonce": nonce,
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": expires_at_ms,
        "payload": _request_payload(request),
    }
    envelope = {
        **unsigned,
        "mac": _mac(shared_key, _REQUEST_DOMAIN, unsigned),
    }
    return _canonical_json(envelope)


def _decode_request(
    packet: bytes,
    *,
    shared_key: bytes,
    now_ms: int,
    limits: ShortLinkTransportLimits,
) -> tuple[_FetchRequest, int, int]:
    envelope = _strict_json_mapping(packet)
    if set(envelope) != {
        "version",
        "kind",
        "nonce",
        "issued_at_ms",
        "expires_at_ms",
        "payload",
        "mac",
    }:
        raise _TransportFailure("schema_invalid")
    if (
        type(envelope["version"]) is not int
        or envelope["version"] != PROTOCOL_VERSION
        or envelope["kind"] != "request"
    ):
        raise _TransportFailure("schema_invalid")
    nonce = envelope["nonce"]
    mac = envelope["mac"]
    if (
        not isinstance(nonce, str)
        or _NONCE.fullmatch(nonce) is None
        or not isinstance(mac, str)
        or _DIGEST.fullmatch(mac) is None
    ):
        raise _TransportFailure("schema_invalid")
    unsigned = {key: envelope[key] for key in envelope if key != "mac"}
    if not hmac.compare_digest(mac, _mac(shared_key, _REQUEST_DOMAIN, unsigned)):
        raise _TransportFailure("authentication")
    issued_at_ms = _valid_int(envelope["issued_at_ms"], minimum=0, maximum=2**63 - 1)
    expires_at_ms = _valid_int(envelope["expires_at_ms"], minimum=0, maximum=2**63 - 1)
    skew_ms = int(limits.clock_skew_seconds * 1000)
    validity_ms = int(limits.max_validity_seconds * 1000)
    if (
        expires_at_ms <= issued_at_ms
        or expires_at_ms - issued_at_ms > validity_ms
        or issued_at_ms > now_ms + skew_ms
        or expires_at_ms < now_ms - skew_ms
    ):
        raise _TransportFailure("freshness")
    payload = envelope["payload"]
    if not isinstance(payload, Mapping) or set(payload) != {
        "target",
        "method",
        "headers",
        "max_header_bytes",
        "max_body_bytes",
        "timeout_ms",
    }:
        raise _TransportFailure("schema_invalid")
    target_payload = payload["target"]
    if not isinstance(target_payload, Mapping) or set(target_payload) != {
        "url",
        "scheme",
        "host",
        "port",
        "addresses",
    }:
        raise _TransportFailure("schema_invalid")
    addresses = target_payload["addresses"]
    if (
        isinstance(addresses, (str, bytes))
        or not isinstance(addresses, Sequence)
        or any(not isinstance(value, str) for value in addresses)
    ):
        raise _TransportFailure("schema_invalid")
    target = ResolvedTarget(
        url=target_payload["url"],
        scheme=target_payload["scheme"],
        host=target_payload["host"],
        port=target_payload["port"],
        addresses=tuple(addresses),
    )
    timeout_ms = _valid_int(
        payload["timeout_ms"],
        minimum=1,
        maximum=int(limits.max_timeout_seconds * 1000),
    )
    request = _validated_fetch_request(
        target=target,
        method=payload["method"],
        headers=payload["headers"],
        max_header_bytes=payload["max_header_bytes"],
        max_body_bytes=payload["max_body_bytes"],
        timeout_seconds=timeout_ms / 1000,
        limits=limits,
    )
    return request, issued_at_ms, expires_at_ms


def _encode_response(
    *,
    nonce: str,
    request_hash: str,
    response: ShortLinkResponse | None,
    reason: str | None,
    issued_at_ms: int,
    shared_key: bytes,
) -> bytes:
    if response is None:
        safe_reason = _allowlisted_reason(reason, _EGRESS_RESPONSE_REASONS)
        payload: dict[str, object] = {"ok": False, "reason": safe_reason}
    else:
        payload = {
            "ok": True,
            "status": response.status,
            "headers": [list(header) for header in response.headers],
            "peer_ip": response.peer_ip,
            "effective_url": response.effective_url,
        }
    unsigned = {
        "version": PROTOCOL_VERSION,
        "kind": "response",
        "nonce": nonce,
        "request_hash": request_hash,
        "issued_at_ms": issued_at_ms,
        "payload": payload,
    }
    return _canonical_json(
        {**unsigned, "mac": _mac(shared_key, _RESPONSE_DOMAIN, unsigned)}
    )


def _decode_response(
    packet: bytes,
    *,
    shared_key: bytes,
    expected_nonce: str,
    expected_request_hash: str,
    expected_target: ResolvedTarget,
    request_issued_at_ms: int,
    request_expires_at_ms: int,
    now_ms: int,
    limits: ShortLinkTransportLimits,
    requested_header_bytes: int,
) -> ShortLinkResponse:
    if (
        not isinstance(packet, bytes)
        or not 1 <= len(packet) <= limits.max_response_frame_bytes
    ):
        raise _TransportFailure("response_frame_limit")
    envelope = _strict_json_mapping(packet)
    if set(envelope) != {
        "version",
        "kind",
        "nonce",
        "request_hash",
        "issued_at_ms",
        "payload",
        "mac",
    }:
        raise _TransportFailure("response_schema")
    if (
        type(envelope["version"]) is not int
        or envelope["version"] != PROTOCOL_VERSION
        or envelope["kind"] != "response"
    ):
        raise _TransportFailure("response_schema")
    mac = envelope["mac"]
    if not isinstance(mac, str) or _DIGEST.fullmatch(mac) is None:
        raise _TransportFailure("response_authentication")
    unsigned = {key: envelope[key] for key in envelope if key != "mac"}
    if not hmac.compare_digest(mac, _mac(shared_key, _RESPONSE_DOMAIN, unsigned)):
        raise _TransportFailure("response_authentication")
    if (
        envelope["nonce"] != expected_nonce
        or envelope["request_hash"] != expected_request_hash
    ):
        raise _TransportFailure("response_binding")
    issued_at_ms = _valid_int(envelope["issued_at_ms"], minimum=0, maximum=2**63 - 1)
    skew_ms = int(limits.clock_skew_seconds * 1000)
    if (
        now_ms > request_expires_at_ms + skew_ms
        or issued_at_ms < request_issued_at_ms - skew_ms
        or issued_at_ms > request_expires_at_ms + skew_ms
        or issued_at_ms > now_ms + skew_ms
    ):
        raise _TransportFailure("response_freshness")
    payload = envelope["payload"]
    if not isinstance(payload, Mapping) or "ok" not in payload:
        raise _TransportFailure("response_schema")
    if payload["ok"] is False:
        if set(payload) != {"ok", "reason"}:
            raise _TransportFailure("response_schema")
        reason = payload["reason"]
        if not isinstance(reason, str):
            raise _TransportFailure("response_schema")
        raise ShortLinkResolutionError(
            _public_egress_reason(reason), "短链 egress transport 请求失败"
        )
    if payload["ok"] is not True or set(payload) != {
        "ok",
        "status",
        "headers",
        "peer_ip",
        "effective_url",
    }:
        raise _TransportFailure("response_schema")
    status = _valid_int(payload["status"], minimum=100, maximum=599)
    raw_headers = payload["headers"]
    if isinstance(raw_headers, (str, bytes)) or not isinstance(raw_headers, Sequence):
        raise _TransportFailure("response_schema")
    headers: list[tuple[str, str]] = []
    header_bytes = 2
    for raw_header in raw_headers:
        if not isinstance(raw_header, (tuple, list)) or len(raw_header) != 2:
            raise _TransportFailure("response_schema")
        name, value = raw_header
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or _MEDIA_HEADER_NAME.fullmatch(name) is None
            or name.lower() != "location"
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in value
            )
        ):
            raise _TransportFailure("response_schema")
        header_bytes += len(name.encode("ascii")) + len(value.encode("utf-8")) + 4
        if header_bytes > requested_header_bytes:
            raise _TransportFailure("response_header_limit")
        headers.append((name, value))
    if len(headers) > limits.max_response_headers:
        raise _TransportFailure("response_header_limit")
    peer_ip = payload["peer_ip"]
    effective_url = payload["effective_url"]
    if not isinstance(peer_ip, str) or effective_url != expected_target.url:
        raise _TransportFailure("response_attestation")
    try:
        assert_connected_peer(expected_target, peer_ip)
    except EgressPolicyError:
        raise _TransportFailure("response_attestation") from None
    return ShortLinkResponse(
        status=status,
        headers=tuple(headers),
        body=b"",
        peer_ip=peer_ip,
        effective_url=effective_url,
    )


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise _TransportFailure("schema_invalid") from None


def _strict_json_mapping(packet: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _TransportFailure("schema_invalid")
            result[key] = value
        return result

    try:
        value = json.loads(
            packet.decode("ascii", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                _TransportFailure("schema_invalid")
            ),
        )
    except _TransportFailure:
        raise
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise _TransportFailure("schema_invalid") from None
    if not isinstance(value, dict) or _canonical_json(value) != packet:
        raise _TransportFailure("schema_invalid")
    return value


def _mac(key: bytes, domain: bytes, unsigned: Mapping[str, object]) -> str:
    return hmac.new(key, domain + _canonical_json(unsigned), hashlib.sha256).hexdigest()


def _http_request_bytes(request: _FetchRequest) -> bytes:
    parts = urlsplit(request.target.url)
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    if (
        not path.startswith("/")
        or path.startswith("//")
        or any(
            not 0x21 <= ord(character) <= 0x7E or character == "\\"
            for character in path
        )
    ):
        raise _TransportFailure("request_invalid")
    try:
        output = bytearray(f"GET {path} HTTP/1.1\r\n".encode("ascii"))
        output.extend(f"Host: {request.target.host}\r\n".encode("ascii"))
        for name, value in request.headers:
            output.extend(f"{name}: {value}\r\n".encode("latin-1"))
        output.extend(b"Connection: close\r\n\r\n")
    except UnicodeEncodeError:
        raise _TransportFailure("request_invalid") from None
    return bytes(output)


async def _read_http_response(
    reader: asyncio.StreamReader,
    *,
    max_header_bytes: int,
    max_body_bytes: int,
    max_headers: int,
) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise _TransportFailure("response_headers_invalid") from None
    if len(raw) > max_header_bytes:
        raise _TransportFailure("response_header_limit")
    lines = raw[:-4].split(b"\r\n")
    if not lines or len(lines) - 1 > max_headers:
        raise _TransportFailure("response_headers_invalid")
    match = re.fullmatch(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [\x20-\x7e]*)?", lines[0])
    if match is None:
        raise _TransportFailure("response_status_invalid")
    status = int(match.group(1))
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if b":" not in line or line[:1] in b" \t":
            raise _TransportFailure("response_headers_invalid")
        name, value = line.split(b":", 1)
        value = value.strip(b" \t")
        if (
            _HEADER_NAME.fullmatch(name) is None
            or any(byte < 0x20 for byte in value)
            or b"\x7f" in value
        ):
            raise _TransportFailure("response_headers_invalid")
        headers.append((name.decode("ascii"), value.decode("latin-1")))
    body = await _read_http_body(
        reader,
        status=status,
        headers=tuple(headers),
        max_body_bytes=max_body_bytes,
        max_header_bytes=max_header_bytes - len(raw),
        max_headers=max_headers - len(headers),
    )
    return status, tuple(headers), body


async def _read_http_body(
    reader: asyncio.StreamReader,
    *,
    status: int,
    headers: Sequence[tuple[str, str]],
    max_body_bytes: int,
    max_header_bytes: int,
    max_headers: int,
) -> bytes:
    if 100 <= status < 200 or status in {204, 304}:
        return b""
    transfer = _header_values(headers, "transfer-encoding")
    lengths = _header_values(headers, "content-length")
    if transfer and lengths:
        raise _TransportFailure("response_body_framing")
    if transfer:
        if len(transfer) != 1 or transfer[0].strip().lower() != "chunked":
            raise _TransportFailure("response_body_framing")
        return await _read_chunked_body(
            reader,
            max_body_bytes=max_body_bytes,
            max_header_bytes=max_header_bytes,
            max_headers=max_headers,
        )
    if lengths:
        if len(set(lengths)) != 1 or not lengths[0].isdigit():
            raise _TransportFailure("response_body_framing")
        length = int(lengths[0])
        if length > max_body_bytes:
            raise _TransportFailure("response_body_limit")
        try:
            return await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            raise _TransportFailure("response_body_incomplete") from None
    body = bytearray()
    while True:
        chunk = await reader.read(min(4096, max_body_bytes - len(body) + 1))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise _TransportFailure("response_body_limit")


async def _read_chunked_body(
    reader: asyncio.StreamReader,
    *,
    max_body_bytes: int,
    max_header_bytes: int,
    max_headers: int,
) -> bytes:
    body = bytearray()
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise _TransportFailure("response_body_framing") from None
        if len(line) > 128 or b";" in line:
            raise _TransportFailure("response_body_framing")
        raw_size = line[:-2]
        if not raw_size or re.fullmatch(rb"[0-9A-Fa-f]+", raw_size) is None:
            raise _TransportFailure("response_body_framing")
        size = int(raw_size, 16)
        if size == 0:
            await _read_trailers(
                reader,
                max_header_bytes=max_header_bytes,
                max_headers=max_headers,
            )
            return bytes(body)
        if size > max_body_bytes - len(body):
            raise _TransportFailure("response_body_limit")
        try:
            chunk = await reader.readexactly(size + 2)
        except asyncio.IncompleteReadError:
            raise _TransportFailure("response_body_incomplete") from None
        if not chunk.endswith(b"\r\n"):
            raise _TransportFailure("response_body_framing")
        body.extend(chunk[:-2])


async def _read_trailers(
    reader: asyncio.StreamReader, *, max_header_bytes: int, max_headers: int
) -> None:
    total = 0
    count = 0
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise _TransportFailure("response_body_framing") from None
        total += len(line)
        if total > max_header_bytes:
            raise _TransportFailure("response_header_limit")
        if line == b"\r\n":
            return
        count += 1
        if count > max_headers or b":" not in line or line[:1] in b" \t":
            raise _TransportFailure("response_body_framing")
        name, value = line[:-2].split(b":", 1)
        value = value.strip(b" \t")
        if (
            _HEADER_NAME.fullmatch(name) is None
            or any(byte < 0x20 for byte in value)
            or b"\x7f" in value
        ):
            raise _TransportFailure("response_body_framing")


def _header_values(headers: Sequence[tuple[str, str]], name: str) -> list[str]:
    lowered = name.lower()
    return [value for key, value in headers if key.lower() == lowered]


def _peer_ip(peer: object) -> str:
    if isinstance(peer, tuple) and peer and isinstance(peer[0], str):
        return peer[0]
    if isinstance(peer, str):
        return peer
    raise ValueError("peer unavailable")


async def _close_writer(writer: _Writer, *, deadline: float | None = None) -> None:
    with contextlib.suppress(Exception):
        writer.close()
    timeout = 1.0
    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline - asyncio.get_running_loop().time()))
    if timeout <= 0:
        return
    with contextlib.suppress(Exception, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout)


def _socket_time_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _TransportFailure("exchange_timeout")
    return remaining


def _recv_exact(client: socket.socket, size: int, *, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        client.settimeout(_socket_time_remaining(deadline))
        chunk = client.recv(size - len(chunks))
        if not chunk:
            raise _TransportFailure("exchange_incomplete")
        chunks.extend(chunk)
    return bytes(chunks)


def _sync_directory(path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "FileReplayStore",
    "MemoryReplayStore",
    "NumericConnector",
    "ReplayStore",
    "ShortLinkEgressService",
    "ShortLinkTransportAuditEvent",
    "ShortLinkTransportConfigurationError",
    "ShortLinkTransportLimits",
    "SignedShortLinkTransport",
    "UnixAttestedShortLinkTransport",
    "load_shared_key",
]

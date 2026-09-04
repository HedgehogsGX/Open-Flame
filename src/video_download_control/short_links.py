"""Candidate short-link resolver with an injected, no-auto-redirect transport.

The control plane instantiates this component only behind its explicit,
default-off feature gate. A deployment must provide a transport that connects
to one of ``ResolvedTarget.addresses``,
preserves the validated hostname for TLS/Host, disables automatic redirects,
and reports the numeric connected peer.  Keeping that boundary explicit
prevents a generic HTTP client from silently re-resolving DNS or following a
redirect outside policy.
"""

from __future__ import annotations

import math
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from .domain import Platform, SourceType
from .normalization import NormalizedURL, URLNormalizationError, normalize_url
from .security.egress import (
    EgressPolicyError,
    ResolvedTarget,
    Resolver,
    assert_connected_peer,
    resolve_public_target,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_ERROR_REASON = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_REQUEST_HEADERS = (
    ("Accept", "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"),
    ("Range", "bytes=0-1023"),
    ("User-Agent", "VDC-ShortLink-Resolver/0.7"),
)


@dataclass(frozen=True, slots=True)
class ShortLinkLimits:
    max_redirects: int = 5
    max_dns_answers: int = 16
    max_header_bytes: int = 16 * 1024
    max_body_bytes: int = 1024
    max_location_chars: int = 4096
    request_timeout_seconds: float = 5.0
    dns_timeout_seconds: float = 3.0
    total_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        integers = (
            self.max_redirects,
            self.max_dns_answers,
            self.max_header_bytes,
            self.max_body_bytes,
            self.max_location_chars,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise ValueError("short-link integer limits must be positive")
        if (
            self.max_redirects > 10
            or self.max_dns_answers > 64
            or self.max_header_bytes > 64 * 1024
            or self.max_body_bytes > 64 * 1024
            or self.max_location_chars > 8 * 1024
        ):
            raise ValueError("short-link integer limits exceed hard bounds")
        durations = (
            self.request_timeout_seconds,
            self.dns_timeout_seconds,
            self.total_timeout_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in durations
        ):
            raise ValueError("short-link time limits must be finite and positive")
        if (
            self.request_timeout_seconds > 30
            or self.dns_timeout_seconds > 10
            or self.total_timeout_seconds > 120
            or self.request_timeout_seconds > self.total_timeout_seconds
            or self.dns_timeout_seconds > self.total_timeout_seconds
        ):
            raise ValueError("short-link time limits exceed hard bounds")


@dataclass(frozen=True, slots=True)
class ShortLinkResponse:
    """One response from a transport that did not follow redirects."""

    status: int
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)
    peer_ip: str
    effective_url: str = field(repr=False)


class ShortLinkTransport(Protocol):
    def request(
        self,
        target: ResolvedTarget,
        *,
        method: str,
        headers: Sequence[tuple[str, str]],
        max_header_bytes: int,
        max_body_bytes: int,
        timeout_seconds: float,
    ) -> ShortLinkResponse: ...


class ShortLinkResolutionError(ValueError):
    """Bounded, secret-free failure safe for persistence or API mapping."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ShortLinkResolution:
    normalized: NormalizedURL = field(repr=False)
    platform: Platform
    redirect_count: int
    policy_hosts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PlatformPolicy:
    start_hosts: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    exact_hosts: bool = False


_POLICIES: Mapping[Platform, _PlatformPolicy] = {
    Platform.X: _PlatformPolicy(
        start_hosts=("t.co",),
        allowed_hosts=("t.co", "x.com", "twitter.com"),
    ),
    Platform.BILIBILI: _PlatformPolicy(
        start_hosts=("b23.tv",),
        allowed_hosts=("b23.tv", "bilibili.com"),
    ),
    Platform.DOUYIN: _PlatformPolicy(
        start_hosts=("v.douyin.com",),
        allowed_hosts=("v.douyin.com", "douyin.com"),
    ),
    Platform.TIKTOK: _PlatformPolicy(
        start_hosts=("vm.tiktok.com", "vt.tiktok.com"),
        allowed_hosts=(
            "vm.tiktok.com",
            "vt.tiktok.com",
            "tiktok.com",
            "www.tiktok.com",
            "m.tiktok.com",
        ),
        exact_hosts=True,
    ),
}


def _system_dns_answers(host: str, port: int) -> tuple[str, ...]:
    answers = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(answer[4][0] for answer in answers)


class _BoundedResolver:
    """Bound blocking DNS without pretending the underlying call is cancellable."""

    def __init__(self, resolver: Resolver, *, max_active: int = 4) -> None:
        self._resolver = resolver
        self._slots = threading.BoundedSemaphore(max_active)

    def resolve(
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float,
        max_answers: int,
    ) -> tuple[str, ...]:
        if not self._slots.acquire(blocking=False):
            raise OSError("bounded DNS capacity unavailable")
        result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                answers = tuple(islice(self._resolver(host, port), max_answers + 1))
                if len(answers) > max_answers:
                    result.put_nowait((False, None))
                else:
                    result.put_nowait((True, answers))
            except Exception:  # noqa: BLE001 - DNS exceptions are redacted
                result.put_nowait((False, None))
            finally:
                self._slots.release()

        thread = threading.Thread(
            target=worker,
            name="vdc-bounded-dns",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            self._slots.release()
            raise OSError("bounded DNS worker unavailable") from None
        try:
            succeeded, payload = result.get(timeout=timeout_seconds)
        except queue.Empty:
            raise OSError("bounded DNS deadline exceeded") from None
        if not succeeded or not isinstance(payload, tuple):
            raise OSError("bounded DNS resolution failed")
        return payload


class ControlledShortLinkResolver:
    """Resolve a known platform short link one manually validated hop at a time."""

    def __init__(
        self,
        *,
        transport: ShortLinkTransport,
        resolver: Resolver | None = None,
        limits: ShortLinkLimits | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.transport = transport
        self.limits = limits or ShortLinkLimits()
        self._resolver = _BoundedResolver(resolver or _system_dns_answers)
        self.monotonic = monotonic or time.monotonic

    def resolve(
        self,
        submitted_url: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ShortLinkResolution:
        try:
            total_timeout_seconds = self.limits.total_timeout_seconds
            if timeout_seconds is not None:
                if (
                    isinstance(timeout_seconds, bool)
                    or not isinstance(timeout_seconds, (int, float))
                    or not math.isfinite(timeout_seconds)
                    or timeout_seconds <= 0
                ):
                    raise ShortLinkResolutionError(
                        "total_timeout", "短链展开超过总时限"
                    )
                total_timeout_seconds = min(
                    total_timeout_seconds, float(timeout_seconds)
                )
            return self._resolve(
                submitted_url,
                total_timeout_seconds=total_timeout_seconds,
            )
        except ShortLinkResolutionError as failure:
            reason = (
                failure.reason
                if isinstance(failure.reason, str)
                and _ERROR_REASON.fullmatch(failure.reason)
                else "resolution_failure"
            )
        except Exception:  # noqa: BLE001 - public trust boundary redacts all causes
            reason = "resolution_failure"
        # Raise after leaving the handler so even ``__context__`` cannot retain
        # a signed Location, host, numeric address, socket path or resolver text.
        raise ShortLinkResolutionError(reason, "短链未通过受控展开策略")

    def _resolve(
        self,
        submitted_url: str,
        *,
        total_timeout_seconds: float,
    ) -> ShortLinkResolution:
        try:
            initial = normalize_url(submitted_url)
        except URLNormalizationError:
            raise ShortLinkResolutionError(
                "invalid_start", "输入不是受支持的平台短链"
            ) from None
        policy = _POLICIES.get(initial.platform)
        if initial.source_type is not SourceType.SHORT_LINK or policy is None:
            raise ShortLinkResolutionError(
                "not_short_link", "输入不是需要展开的受支持平台短链"
            )
        initial_host = urlsplit(initial.canonical_url).hostname
        if initial_host is None or initial_host not in policy.start_hosts:
            raise ShortLinkResolutionError("invalid_start", "短链起点与平台策略不匹配")

        started = self.monotonic()
        current_url = initial.canonical_url
        remaining = total_timeout_seconds - (self.monotonic() - started)
        if remaining <= 0:
            raise ShortLinkResolutionError("total_timeout", "短链展开超过总时限")
        current_target = self._approved_target(
            current_url,
            policy,
            dns_timeout_seconds=min(self.limits.dns_timeout_seconds, remaining),
        )
        seen = {_redirect_identity(current_url)}
        policy_path: list[str] = [initial_host]

        for redirect_count in range(1, self.limits.max_redirects + 1):
            remaining = total_timeout_seconds - (self.monotonic() - started)
            if remaining <= 0:
                raise ShortLinkResolutionError("total_timeout", "短链展开超过总时限")
            try:
                response = self.transport.request(
                    current_target,
                    method="GET",
                    headers=_REQUEST_HEADERS,
                    max_header_bytes=self.limits.max_header_bytes,
                    max_body_bytes=self.limits.max_body_bytes,
                    timeout_seconds=min(self.limits.request_timeout_seconds, remaining),
                )
            except ShortLinkResolutionError:
                raise
            except Exception:  # noqa: BLE001 - transport failures are redacted
                raise ShortLinkResolutionError(
                    "transport_failure", "短链请求失败"
                ) from None
            if self.monotonic() - started > total_timeout_seconds:
                raise ShortLinkResolutionError("total_timeout", "短链展开超过总时限")
            self._validate_response(current_target, response)
            if response.status not in _REDIRECT_STATUSES:
                raise ShortLinkResolutionError(
                    "redirect_expected", "短链服务没有返回可接受的重定向"
                )
            location = _single_location(
                response.headers, max_chars=self.limits.max_location_chars
            )
            next_url = _canonical_redirect(urljoin(current_url, location))
            identity = _redirect_identity(next_url)
            if identity in seen:
                raise ShortLinkResolutionError("redirect_loop", "短链重定向出现环路")
            seen.add(identity)

            remaining = total_timeout_seconds - (self.monotonic() - started)
            if remaining <= 0:
                raise ShortLinkResolutionError("total_timeout", "短链展开超过总时限")
            next_target = self._approved_target(
                next_url,
                policy,
                dns_timeout_seconds=min(self.limits.dns_timeout_seconds, remaining),
            )
            if self.monotonic() - started > total_timeout_seconds:
                raise ShortLinkResolutionError("total_timeout", "短链展开超过总时限")
            if next_target.host not in policy_path:
                policy_path.append(next_target.host)
            try:
                normalized = normalize_url(next_url)
            except URLNormalizationError:
                normalized = None
            if normalized is not None:
                if normalized.platform is not initial.platform:
                    raise ShortLinkResolutionError(
                        "cross_platform", "短链最终目标跨越了平台边界"
                    )
                if normalized.source_type is not SourceType.SHORT_LINK:
                    normalized = NormalizedURL(
                        submitted_url=initial.canonical_url,
                        canonical_url=normalized.canonical_url,
                        platform=normalized.platform,
                        source_type=normalized.source_type,
                        source_id=normalized.source_id,
                    )
                    return ShortLinkResolution(
                        normalized=normalized,
                        platform=initial.platform,
                        redirect_count=redirect_count,
                        policy_hosts=tuple(policy_path),
                    )
            current_url = next_url
            current_target = next_target

        raise ShortLinkResolutionError("redirect_limit", "短链重定向超过跳数限制")

    def _approved_target(
        self,
        url: str,
        policy: _PlatformPolicy,
        *,
        dns_timeout_seconds: float,
    ) -> ResolvedTarget:
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() != "https":
                raise ShortLinkResolutionError("https_required", "短链解析只允许 HTTPS")
            host = parts.hostname
            if policy.exact_hosts and (
                not isinstance(host, str)
                or host.rstrip(".").lower() not in policy.allowed_hosts
            ):
                raise ShortLinkResolutionError(
                    "egress_host_not_allowed",
                    "短链目标域名不在精确允许列表",
                )
            return resolve_public_target(
                url,
                resolver=lambda host, port: self._resolver.resolve(
                    host,
                    port,
                    timeout_seconds=dns_timeout_seconds,
                    max_answers=self.limits.max_dns_answers,
                ),
                allowed_hosts=policy.allowed_hosts,
                allow_ip_literal=False,
            )
        except ShortLinkResolutionError:
            raise
        except EgressPolicyError as exc:
            raise ShortLinkResolutionError(
                f"egress_{exc.reason}", "短链目标未通过受控出站策略"
            ) from None
        except ValueError:
            raise ShortLinkResolutionError(
                "invalid_redirect", "短链重定向地址无效"
            ) from None

    def _validate_response(
        self, target: ResolvedTarget, response: ShortLinkResponse
    ) -> None:
        if (
            isinstance(response.status, bool)
            or not isinstance(response.status, int)
            or not 100 <= response.status <= 599
        ):
            raise ShortLinkResolutionError("invalid_status", "短链响应状态无效")
        if (
            not isinstance(response.effective_url, str)
            or response.effective_url != target.url
        ):
            raise ShortLinkResolutionError(
                "transport_followed_redirect", "短链 transport 不得自动跟随重定向"
            )
        if not isinstance(response.peer_ip, str):
            raise ShortLinkResolutionError(
                "invalid_peer", "短链 transport 返回的连接 peer 无效"
            )
        try:
            assert_connected_peer(target, response.peer_ip)
        except EgressPolicyError as exc:
            raise ShortLinkResolutionError(
                f"egress_{exc.reason}", "短链连接 peer 未通过受控出站策略"
            ) from None
        if not isinstance(response.body, bytes):
            raise ShortLinkResolutionError("invalid_body", "短链响应体类型无效")
        if len(response.body) > self.limits.max_body_bytes:
            raise ShortLinkResolutionError("body_limit", "短链响应体超过限制")
        if isinstance(response.headers, (str, bytes)) or not isinstance(
            response.headers, Sequence
        ):
            raise ShortLinkResolutionError("invalid_header", "短链响应头无效")
        header_bytes = 2
        for header in response.headers:
            if not isinstance(header, tuple) or len(header) != 2:
                raise ShortLinkResolutionError("invalid_header", "短链响应头无效")
            name, value = header
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not _HEADER_NAME.fullmatch(name)
                or any(character in value for character in "\r\n\0")
            ):
                raise ShortLinkResolutionError("invalid_header", "短链响应头无效")
            if len(name) + len(value) + 4 > self.limits.max_header_bytes:
                raise ShortLinkResolutionError("header_limit", "短链响应头超过限制")
            header_bytes += len(name.encode("ascii")) + len(value.encode("utf-8")) + 4
            if header_bytes > self.limits.max_header_bytes:
                raise ShortLinkResolutionError("header_limit", "短链响应头超过限制")


def _single_location(headers: Sequence[tuple[str, str]], *, max_chars: int) -> str:
    values = [value.strip() for name, value in headers if name.lower() == "location"]
    if len(values) != 1 or not values[0]:
        raise ShortLinkResolutionError(
            "invalid_location", "短链重定向必须包含唯一 Location"
        )
    location = values[0]
    if len(location) > max_chars or any(
        ord(character) < 0x20 for character in location
    ):
        raise ShortLinkResolutionError("invalid_location", "短链 Location 无效或过长")
    return location


def _canonical_redirect(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ShortLinkResolutionError(
            "invalid_redirect", "短链重定向地址无效"
        ) from None
    if not parts.scheme or not parts.netloc:
        raise ShortLinkResolutionError("invalid_redirect", "短链重定向必须产生绝对地址")
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path or "/",
            parts.query,
            "",
        )
    )


def _redirect_identity(url: str) -> str:
    # The identity is kept only in memory for loop detection and is never
    # included in errors or audit output because query strings may be signed.
    return _canonical_redirect(url)

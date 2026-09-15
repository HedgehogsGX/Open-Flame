from __future__ import annotations

import asyncio
import ipaddress
import queue
import re
import socket
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import islice
from urllib.parse import urljoin, urlsplit, urlunsplit


Resolver = Callable[[str, int], Iterable[str]]
MAX_EGRESS_URL_LENGTH = 4096
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BLOCKED_TRANSITION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("2001::/32"),
    ipaddress.ip_network("2002::/16"),
)


class EgressPolicyError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    url: str = field(repr=False)
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


def system_dns_answers(host: str, port: int) -> Iterable[str]:
    """Blocking OS lookup; runtime callers use BoundedResolver to bound it."""
    answers = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return (answer[4][0] for answer in answers)


class BoundedResolver:
    """Own bounded blocking DNS for synchronous and asynchronous callers.

    A timeout abandons the observation, not the underlying OS call. Its daemon
    thread keeps the capacity slot until DNS actually returns, so stalled DNS
    cannot grow an executor queue or prevent the event loop from shutting down.
    """

    def __init__(self, resolver: Resolver | None = None, *, max_active: int = 4) -> None:
        self._resolver = resolver or system_dns_answers
        self._slots = threading.BoundedSemaphore(max_active)

    def _start(
        self,
        host: str,
        port: int,
        max_answers: int,
        completed: Callable[[tuple[str, ...] | None], None],
    ) -> None:
        if not self._slots.acquire(blocking=False):
            raise OSError("bounded DNS capacity unavailable")

        def worker() -> None:
            answers = None
            try:
                values = tuple(islice(self._resolver(host, port), max_answers + 1))
                if len(values) <= max_answers:
                    answers = values
            except Exception:  # noqa: BLE001 - resolver errors are redacted
                pass
            finally:
                self._slots.release()
            completed(answers)

        thread = threading.Thread(target=worker, name="vdc-bounded-dns", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._slots.release()
            raise OSError("bounded DNS worker unavailable") from None

    def resolve(
        self, host: str, port: int, *, timeout_seconds: float, max_answers: int
    ) -> tuple[str, ...]:
        result: queue.Queue[tuple[str, ...] | None] = queue.Queue(maxsize=1)
        self._start(host, port, max_answers, result.put_nowait)
        try:
            answers = result.get(timeout=timeout_seconds)
        except queue.Empty:
            raise TimeoutError("bounded DNS deadline exceeded") from None
        if answers is None:
            raise OSError("bounded DNS resolution failed")
        return answers

    async def resolve_async(
        self, host: str, port: int, *, timeout_seconds: float, max_answers: int
    ) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[tuple[str, ...] | None] = loop.create_future()

        def deliver(answers: tuple[str, ...] | None) -> None:
            if not result.done():
                result.set_result(answers)

        def completed(answers: tuple[str, ...] | None) -> None:
            try:
                loop.call_soon_threadsafe(deliver, answers)
            except RuntimeError:
                # DNS can return after its caller and event loop have stopped.
                pass

        self._start(host, port, max_answers, completed)
        answers = await asyncio.wait_for(result, timeout=timeout_seconds)
        if answers is None:
            raise OSError("bounded DNS resolution failed")
        return answers


def _ascii_host(host: str | None) -> str:
    if not host:
        raise EgressPolicyError("missing_host", "URL 缺少目标域名")
    if "%" in host:
        raise EgressPolicyError("zone_identifier_blocked", "目标地址不得包含 IPv6 zone ID")
    normalized = host.rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.compressed
    try:
        ascii_host = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise EgressPolicyError("invalid_host", "目标域名无法进行 IDNA 编码") from exc
    if len(ascii_host) > 253:
        raise EgressPolicyError("invalid_host", "目标域名过长")
    labels = ascii_host.split(".")
    if len(labels) < 2 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise EgressPolicyError("invalid_host", "目标域名标签无效")
    return ascii_host


def _host_allowed(host: str, allowed_hosts: frozenset[str]) -> bool:
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _public_address(raw: str) -> str:
    address_text = raw.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError as exc:
        raise EgressPolicyError("invalid_dns_answer", "DNS 返回了无效 IP 地址") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv6Address) and any(
        address in network for network in _BLOCKED_TRANSITION_NETWORKS
    ):
        raise EgressPolicyError(
            "transition_address_blocked",
            "不接受 NAT64、Teredo 或 6to4 过渡地址",
        )
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or (
            isinstance(address, ipaddress.IPv6Address)
            and address.is_site_local
        )
    ):
        raise EgressPolicyError(
            "non_public_address",
            f"目标解析到非公网地址：{address.compressed}",
        )
    return address.compressed


def _target_authority(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allow_ip_literal: bool = False,
) -> tuple[str, str, int, str | None]:
    if len(url) > MAX_EGRESS_URL_LENGTH:
        raise EgressPolicyError("url_too_long", "出站 URL 过长")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise EgressPolicyError("invalid_url", "目标 URL 格式无效") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise EgressPolicyError("invalid_scheme", "出站目标只允许 HTTP/HTTPS")
    if parts.username is not None or parts.password is not None:
        raise EgressPolicyError("credentials_in_url", "出站 URL 不得包含凭证")
    host = _ascii_host(parts.hostname)
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise EgressPolicyError("invalid_port", "出站 URL 端口无效") from exc
    port = (
        explicit_port
        if explicit_port is not None
        else (443 if scheme == "https" else 80)
    )
    expected_port = 443 if scheme == "https" else 80
    if port != expected_port:
        raise EgressPolicyError(
            "blocked_port", "出站目标端口必须与 HTTP/HTTPS 默认端口一致"
        )

    if allowed_hosts is not None:
        normalized_allowed = frozenset(_ascii_host(item) for item in allowed_hosts)
        if not _host_allowed(host, normalized_allowed):
            raise EgressPolicyError(
                "host_not_allowed", f"目标域名不在允许列表：{host}"
            )

    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not allow_ip_literal:
        raise EgressPolicyError("ip_literal_blocked", "出站 URL 不接受 IP 字面量")
    return scheme, host, port, literal.compressed if literal is not None else None


def _resolved_target(
    url: str, scheme: str, host: str, port: int, raw_answers: tuple[str, ...]
) -> ResolvedTarget:
    if not raw_answers:
        raise EgressPolicyError("dns_no_answers", "目标域名没有 A/AAAA 结果")
    accepted = tuple(dict.fromkeys(_public_address(answer) for answer in raw_answers))
    return ResolvedTarget(url=url, scheme=scheme, host=host, port=port, addresses=accepted)


def resolve_public_target(
    url: str,
    *,
    resolver: Resolver | None = None,
    allowed_hosts: Iterable[str] | None = None,
    allow_ip_literal: bool = False,
) -> ResolvedTarget:
    """Validate a target; callers must connect to its approved numeric addresses.

    Preserve the validated hostname for TLS and HTTP Host. Resolving again in
    an unrelated client would reintroduce DNS rebinding.
    """
    scheme, host, port, literal = _target_authority(
        url, allowed_hosts=allowed_hosts, allow_ip_literal=allow_ip_literal
    )

    if literal is not None:
        raw_answers = (literal,)
    else:
        active_resolver = resolver or system_dns_answers
        try:
            raw_answers = tuple(active_resolver(host, port))
        except OSError as exc:
            raise EgressPolicyError("dns_failure", "目标域名解析失败") from exc
    return _resolved_target(url, scheme, host, port, raw_answers)


async def resolve_public_target_async(
    url: str,
    *,
    resolver: BoundedResolver,
    allowed_hosts: Iterable[str],
    timeout_seconds: float,
    max_answers: int = 64,
) -> ResolvedTarget:
    """Apply the same URL/IP policy with bounded, cancellation-safe DNS."""
    scheme, host, port, _ = _target_authority(
        url, allowed_hosts=allowed_hosts, allow_ip_literal=False
    )
    try:
        answers = await resolver.resolve_async(
            host, port, timeout_seconds=timeout_seconds, max_answers=max_answers
        )
    except TimeoutError:
        raise
    except OSError:
        raise EgressPolicyError("dns_failure", "目标域名解析失败") from None
    return _resolved_target(url, scheme, host, port, answers)


def assert_connected_peer(target: ResolvedTarget, peer_ip: str) -> None:
    """Require the transport's connected peer to match an approved answer."""
    normalized_peer = _public_address(peer_ip)
    if normalized_peer not in target.addresses:
        raise EgressPolicyError(
            "peer_address_mismatch",
            "实际连接地址不在本次批准的 DNS 结果中",
        )


def validate_redirect_chain(
    urls: Sequence[str],
    *,
    resolver: Resolver,
    final_allowed_hosts: Iterable[str],
) -> tuple[ResolvedTarget, ...]:
    """Validate every observed redirect location and constrain the final host.

    The first entry must be absolute. Later entries may be relative Location
    values and are joined to the prior approved URL. Automatic redirect
    following must stay disabled in the transport that supplies this chain.
    """
    if not urls:
        raise EgressPolicyError("empty_redirect_chain", "重定向链不能为空")
    if len(urls) > 11:
        raise EgressPolicyError("redirect_limit", "重定向最多允许 10 跳")
    absolute_urls: list[str] = []
    seen: set[str] = set()
    for index, location in enumerate(urls):
        absolute = location if index == 0 else urljoin(absolute_urls[-1], location)
        parts = urlsplit(absolute)
        if not parts.scheme or not parts.netloc:
            raise EgressPolicyError(
                "invalid_redirect_location", "首个重定向链 URL 必须是绝对地址"
            )
        canonical = urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path or "/",
                parts.query,
                "",
            )
        )
        if canonical in seen:
            raise EgressPolicyError("redirect_loop", "检测到重定向环路")
        if absolute_urls:
            previous_scheme = urlsplit(absolute_urls[-1]).scheme.lower()
            if previous_scheme == "https" and parts.scheme.lower() == "http":
                raise EgressPolicyError(
                    "https_downgrade", "不允许 HTTPS 重定向降级到 HTTP"
                )
        seen.add(canonical)
        absolute_urls.append(canonical)
    targets = tuple(
        resolve_public_target(url, resolver=resolver) for url in absolute_urls
    )
    allowed = frozenset(_ascii_host(host) for host in final_allowed_hosts)
    if not _host_allowed(targets[-1].host, allowed):
        raise EgressPolicyError(
            "final_host_not_allowed",
            f"重定向最终域名不在允许列表：{targets[-1].host}",
        )
    return targets

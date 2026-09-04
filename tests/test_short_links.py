from __future__ import annotations

import socket
import threading
import time
from collections import deque

import pytest

from video_download_control.domain import Platform, SourceType
from video_download_control.security.egress import ResolvedTarget
from video_download_control.short_links import (
    ControlledShortLinkResolver,
    ShortLinkLimits,
    ShortLinkResolutionError,
    ShortLinkResponse,
)

PUBLIC_IP = "93.184.216.34"


class FakeTransport:
    def __init__(self, *responses: ShortLinkResponse | BaseException) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[ResolvedTarget, dict[str, object]]] = []

    def request(self, target: ResolvedTarget, **kwargs) -> ShortLinkResponse:
        self.calls.append((target, kwargs))
        if not self.responses:
            raise AssertionError("unexpected short-link request")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def response(
    url: str,
    location: str,
    *,
    status: int = 302,
    peer_ip: str = PUBLIC_IP,
    extra_headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> ShortLinkResponse:
    return ShortLinkResponse(
        status=status,
        headers=(("Location", location), *extra_headers),
        body=body,
        peer_ip=peer_ip,
        effective_url=url,
    )


def make_resolver(
    transport: FakeTransport,
    *,
    dns_answers: tuple[str, ...] = (PUBLIC_IP,),
    limits: ShortLinkLimits | None = None,
    monotonic=None,
) -> ControlledShortLinkResolver:
    return ControlledShortLinkResolver(
        transport=transport,
        resolver=lambda host, port: dns_answers,
        limits=limits,
        monotonic=monotonic,
    )


@pytest.mark.parametrize(
    ("short_url", "location", "platform", "source_type"),
    [
        (
            "https://t.co/abc",
            "https://x.com/name/status/12345?secret=drop",
            Platform.X,
            SourceType.X_POST,
        ),
        (
            "https://b23.tv/abc",
            "https://www.bilibili.com/video/BV1Ab411c7mD?share=1",
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
        ),
        (
            "https://v.douyin.com/abc",
            "https://www.douyin.com/video/1234567890?token=drop",
            Platform.DOUYIN,
            SourceType.DOUYIN_VIDEO,
        ),
        (
            "https://vm.tiktok.com/ZShort123",
            "https://www.tiktok.com/@Example.User/video/7461234567890123456?token=drop",
            Platform.TIKTOK,
            SourceType.TIKTOK_VIDEO,
        ),
        (
            "https://vt.tiktok.com/ZShort456",
            "https://m.tiktok.com/@Example.User/video/7461234567890123457?token=drop",
            Platform.TIKTOK,
            SourceType.TIKTOK_VIDEO,
        ),
    ],
)
def test_resolves_supported_short_link_without_fetching_final_page(
    short_url: str,
    location: str,
    platform: Platform,
    source_type: SourceType,
) -> None:
    canonical_start = short_url
    transport = FakeTransport(response(canonical_start, location))

    result = make_resolver(transport).resolve(short_url)

    assert result.platform is platform
    assert result.normalized.source_type is source_type
    assert result.redirect_count == 1
    assert len(transport.calls) == 1
    assert transport.calls[0][1]["method"] == "GET"
    assert transport.calls[0][1]["max_body_bytes"] == 1024
    assert "token" not in result.normalized.canonical_url
    assert "secret" not in result.normalized.canonical_url


def test_relative_redirect_is_validated_one_hop_at_a_time() -> None:
    first = "https://t.co/a"
    second = "https://t.co/next"
    transport = FakeTransport(
        response(first, "/next"),
        response(second, "https://x.com/i/status/99"),
    )

    result = make_resolver(transport).resolve(first)

    assert result.normalized.canonical_url == "https://x.com/i/status/99"
    assert result.redirect_count == 2
    assert [call[0].url for call in transport.calls] == [first, second]


def test_tiktok_redirects_between_reviewed_hosts_one_hop_at_a_time() -> None:
    first = "https://vm.tiktok.com/ZStart"
    second = "https://vt.tiktok.com/ZNext"
    transport = FakeTransport(
        response(first, second),
        response(
            second,
            "https://www.tiktok.com/@Example.User/video/7461234567890123456",
        ),
    )
    dns_calls: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str, ...]:
        dns_calls.append((host, port))
        return (PUBLIC_IP,)

    result = ControlledShortLinkResolver(
        transport=transport,
        resolver=resolver,
    ).resolve(first)

    assert result.normalized.canonical_url == (
        "https://www.tiktok.com/@example.user/video/7461234567890123456"
    )
    assert result.policy_hosts == (
        "vm.tiktok.com",
        "vt.tiktok.com",
        "www.tiktok.com",
    )
    assert dns_calls == [
        ("vm.tiktok.com", 443),
        ("vt.tiktok.com", 443),
        ("www.tiktok.com", 443),
    ]
    assert [call[0].url for call in transport.calls] == [first, second]


@pytest.mark.parametrize(
    "location",
    (
        "https://video.tiktok.com/@example/video/7461234567890123456",
        "https://evil.vm.tiktok.com/ZNext",
        "https://tiktok.com.evil.example/@example/video/7461234567890123456",
    ),
)
def test_tiktok_rejects_hosts_outside_exact_reviewed_list(location: str) -> None:
    start = "https://vm.tiktok.com/ZStart"
    transport = FakeTransport(response(start, location))

    with pytest.raises(ShortLinkResolutionError) as caught:
        make_resolver(transport).resolve(start)

    assert caught.value.reason == "egress_host_not_allowed"
    assert location not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(transport.calls) == 1


def test_tiktok_short_link_keeps_https_dns_and_peer_guards() -> None:
    start = "https://vt.tiktok.com/ZGuarded"

    with pytest.raises(ShortLinkResolutionError) as downgrade:
        make_resolver(
            FakeTransport(
                response(
                    start,
                    "http://www.tiktok.com/@example/video/7461234567890123456",
                )
            )
        ).resolve(start)
    with pytest.raises(ShortLinkResolutionError) as private_dns:
        make_resolver(
            FakeTransport(),
            dns_answers=("127.0.0.1",),
        ).resolve(start)
    with pytest.raises(ShortLinkResolutionError) as peer_mismatch:
        make_resolver(
            FakeTransport(
                response(
                    start,
                    "https://www.tiktok.com/@example/video/7461234567890123456",
                    peer_ip="1.1.1.1",
                )
            )
        ).resolve(start)

    assert downgrade.value.reason == "https_required"
    assert private_dns.value.reason == "egress_non_public_address"
    assert peer_mismatch.value.reason == "egress_peer_address_mismatch"


def test_each_redirect_target_is_resolved_exactly_once() -> None:
    first = "https://t.co/a"
    second = "https://t.co/next"
    transport = FakeTransport(
        response(first, "/next"),
        response(second, "https://x.com/i/status/99"),
    )
    dns_calls: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str, ...]:
        dns_calls.append((host, port))
        return (PUBLIC_IP,)

    result = ControlledShortLinkResolver(
        transport=transport,
        resolver=resolver,
    ).resolve(first)

    assert result.normalized.canonical_url == "https://x.com/i/status/99"
    assert dns_calls == [("t.co", 443), ("t.co", 443), ("x.com", 443)]


def test_transport_contract_uses_only_fixed_bounded_request_values() -> None:
    start = "https://t.co/a"
    transport = FakeTransport(response(start, "https://x.com/i/status/1"))

    make_resolver(transport).resolve(start)

    _, arguments = transport.calls[0]
    assert arguments == {
        "method": "GET",
        "headers": (
            ("Accept", "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"),
            ("Range", "bytes=0-1023"),
            ("User-Agent", "VDC-ShortLink-Resolver/0.7"),
        ),
        "max_header_bytes": 16 * 1024,
        "max_body_bytes": 1024,
        "timeout_seconds": 5.0,
    }


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("http://x.com/i/status/1", "https_required"),
        ("https://youtube.com/watch?v=abc", "egress_host_not_allowed"),
        ("https://user:pass@x.com/i/status/1", "egress_credentials_in_url"),
        ("https://x.com:444/i/status/1", "egress_blocked_port"),
        ("https://127.0.0.1/i/status/1", "egress_host_not_allowed"),
    ],
)
def test_rejects_downgrade_cross_policy_credentials_and_ports(
    location: str, reason: str
) -> None:
    start = "https://t.co/a"
    resolver = make_resolver(FakeTransport(response(start, location)))

    with pytest.raises(ShortLinkResolutionError) as caught:
        resolver.resolve(start)

    assert caught.value.reason == reason
    assert location not in str(caught.value)


@pytest.mark.parametrize(
    "answers",
    [
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("10.0.0.5",),
        (PUBLIC_IP, "192.168.1.2"),
        ("::ffff:127.0.0.1",),
    ],
)
def test_rejects_nonpublic_or_mixed_dns_answers(answers: tuple[str, ...]) -> None:
    start = "https://t.co/a"
    transport = FakeTransport(response(start, "https://x.com/i/status/1"))

    with pytest.raises(ShortLinkResolutionError) as caught:
        make_resolver(transport, dns_answers=answers).resolve(start)

    assert caught.value.reason.startswith("egress_")
    assert not transport.calls


def test_rejects_peer_not_in_pinned_dns_answers() -> None:
    start = "https://t.co/a"
    transport = FakeTransport(
        response(start, "https://x.com/i/status/1", peer_ip="1.1.1.1")
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        make_resolver(transport).resolve(start)

    assert caught.value.reason == "egress_peer_address_mismatch"


def test_rejects_transport_that_followed_redirect_automatically() -> None:
    start = "https://t.co/a"
    transport = FakeTransport(
        ShortLinkResponse(
            status=200,
            headers=(),
            body=b"",
            peer_ip=PUBLIC_IP,
            effective_url="https://x.com/i/status/1",
        )
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        make_resolver(transport).resolve(start)

    assert caught.value.reason == "transport_followed_redirect"


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ((), "invalid_location"),
        ((("Location", "/a"), ("Location", "/b")), "invalid_location"),
        ((("Bad Header", "x"), ("Location", "/a")), "invalid_header"),
        ((("X-Test", "value\r\ninjected"), ("Location", "/a")), "invalid_header"),
    ],
)
def test_rejects_missing_duplicate_or_invalid_headers(
    headers: tuple[tuple[str, str], ...], reason: str
) -> None:
    start = "https://t.co/a"
    transport = FakeTransport(ShortLinkResponse(302, headers, b"", PUBLIC_IP, start))

    with pytest.raises(ShortLinkResolutionError) as caught:
        make_resolver(transport).resolve(start)

    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("headers", (("Location",),), "invalid_header"),
        ("headers", "Location: /next", "invalid_header"),
        ("body", "not-bytes", "invalid_body"),
        ("peer_ip", None, "invalid_peer"),
    ],
)
def test_rejects_malformed_transport_response_without_internal_exception(
    field: str, value: object, reason: str
) -> None:
    start = "https://t.co/a"
    values: dict[str, object] = {
        "status": 302,
        "headers": (("Location", "https://x.com/i/status/1"),),
        "body": b"",
        "peer_ip": PUBLIC_IP,
        "effective_url": start,
    }
    values[field] = value
    malformed = ShortLinkResponse(**values)  # type: ignore[arg-type]

    with pytest.raises(ShortLinkResolutionError) as caught:
        make_resolver(FakeTransport(malformed)).resolve(start)

    assert caught.value.reason == reason


def test_rejects_header_and_body_limits() -> None:
    start = "https://t.co/a"
    header_transport = FakeTransport(
        response(start, "/next", extra_headers=(("X-Large", "x" * 100),))
    )
    body_transport = FakeTransport(response(start, "/next", body=b"x" * 5))

    with pytest.raises(ShortLinkResolutionError) as header_error:
        make_resolver(
            header_transport,
            limits=ShortLinkLimits(max_header_bytes=64),
        ).resolve(start)
    with pytest.raises(ShortLinkResolutionError) as body_error:
        make_resolver(
            body_transport,
            limits=ShortLinkLimits(max_body_bytes=4),
        ).resolve(start)

    assert header_error.value.reason == "header_limit"
    assert body_error.value.reason == "body_limit"


def test_rejects_redirect_loop_and_hop_limit() -> None:
    start = "https://t.co/a"
    loop_transport = FakeTransport(response(start, start))
    limit_transport = FakeTransport(
        response(start, "/b"),
        response("https://t.co/b", "/c"),
    )

    with pytest.raises(ShortLinkResolutionError) as loop_error:
        make_resolver(loop_transport).resolve(start)
    with pytest.raises(ShortLinkResolutionError) as limit_error:
        make_resolver(
            limit_transport,
            limits=ShortLinkLimits(max_redirects=2),
        ).resolve(start)

    assert loop_error.value.reason == "redirect_loop"
    assert limit_error.value.reason == "redirect_limit"


def test_total_deadline_is_rechecked_after_transport_returns() -> None:
    start = "https://t.co/a"
    times = iter((0.0, 0.0, 20.0))
    transport = FakeTransport(response(start, "https://x.com/i/status/1"))

    with pytest.raises(ShortLinkResolutionError) as caught:
        make_resolver(
            transport,
            limits=ShortLinkLimits(total_timeout_seconds=10),
            monotonic=lambda: next(times),
        ).resolve(start)

    assert caught.value.reason == "total_timeout"


def test_total_deadline_is_rechecked_after_final_target_dns() -> None:
    start = "https://t.co/a"
    now = [0.0]
    dns_calls = 0

    def resolver(host: str, port: int) -> tuple[str, ...]:
        nonlocal dns_calls
        del host, port
        dns_calls += 1
        if dns_calls == 2:
            now[0] = 20.0
        return (PUBLIC_IP,)

    controlled = ControlledShortLinkResolver(
        transport=FakeTransport(response(start, "https://x.com/i/status/1")),
        resolver=resolver,
        limits=ShortLinkLimits(total_timeout_seconds=10),
        monotonic=lambda: now[0],
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        controlled.resolve(start)

    assert caught.value.reason == "total_timeout"


def test_blocking_system_dns_has_wall_clock_and_concurrency_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocking_getaddrinfo(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        with calls_lock:
            calls += 1
        release.wait(timeout=2)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, 443),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", blocking_getaddrinfo)
    controlled = ControlledShortLinkResolver(
        transport=FakeTransport(),
        limits=ShortLinkLimits(
            request_timeout_seconds=0.1,
            dns_timeout_seconds=0.02,
            total_timeout_seconds=1,
        ),
    )
    started = time.monotonic()
    try:
        for _ in range(5):
            with pytest.raises(ShortLinkResolutionError) as caught:
                controlled.resolve("https://t.co/bounded")
            assert caught.value.reason == "egress_dns_failure"
            assert caught.value.__cause__ is None
    finally:
        release.set()

    assert time.monotonic() - started < 0.5
    assert calls == 4


def test_dns_answer_iterable_is_never_materialized_past_bound() -> None:
    yielded = 0

    def unbounded_answers(host: str, port: int):
        nonlocal yielded
        del host, port
        while True:
            yielded += 1
            yield PUBLIC_IP

    controlled = ControlledShortLinkResolver(
        transport=FakeTransport(),
        resolver=unbounded_answers,
        limits=ShortLinkLimits(max_dns_answers=16),
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        controlled.resolve("https://t.co/bounded-answers")

    assert caught.value.reason == "egress_dns_failure"
    assert yielded == 17


def test_errors_do_not_echo_signed_location_or_transport_secret() -> None:
    start = "https://t.co/a"
    signed = "https://evil.example/path?token=super-secret"
    resolver = make_resolver(FakeTransport(response(start, signed)))

    with pytest.raises(ShortLinkResolutionError) as caught:
        resolver.resolve(start)

    assert "super-secret" not in str(caught.value)
    assert "evil.example" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_success_objects_do_not_retain_or_repr_signed_location() -> None:
    start = "https://t.co/a"
    signed = "https://x.com/i/status/1?token=super-secret"
    hop = response(start, signed)

    result = make_resolver(FakeTransport(hop)).resolve(start)

    assert result.normalized.submitted_url == start
    assert "super-secret" not in repr(result)
    assert "super-secret" not in repr(hop)


def test_rejects_non_short_link_and_unconfigured_youtube_short_domain() -> None:
    transport = FakeTransport()
    resolver = make_resolver(transport)

    for value in (
        "https://x.com/i/status/1",
        "https://youtu.be/abc",
        "https://example.com/a",
    ):
        with pytest.raises(ShortLinkResolutionError):
            resolver.resolve(value)
    assert not transport.calls


@pytest.mark.parametrize(
    "limits",
    [
        {"max_redirects": 0},
        {"max_redirects": 11},
        {"max_dns_answers": 65},
        {"max_body_bytes": 0},
        {"dns_timeout_seconds": 11},
        {"request_timeout_seconds": float("inf")},
        {"total_timeout_seconds": float("nan")},
    ],
)
def test_limits_reject_unbounded_or_nonpositive_values(
    limits: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        ShortLinkLimits(**limits)  # type: ignore[arg-type]

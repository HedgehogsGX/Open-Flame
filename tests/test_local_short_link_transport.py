from __future__ import annotations

import asyncio
import gc
import ssl
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest

from video_download_control.local_short_links import LocalDirectShortLinkTransport
from video_download_control.security.egress import ResolvedTarget
from video_download_control.short_link_transport import (
    ShortLinkTransportConfigurationError,
    ShortLinkTransportLimits,
)
from video_download_control.short_links import ShortLinkResolutionError

PUBLIC_ADDRESS = "93.184.216.34"
DESTINATION = "https://x.com/i/status/123"
REDIRECT = (
    f"HTTP/1.1 302 Found\r\nLocation: {DESTINATION}\r\n"
    "Set-Cookie: private=not-disclosed\r\nContent-Length: 0\r\n\r\n"
).encode("ascii")


class ResponseWriter:
    def __init__(self, peer: str = PUBLIC_ADDRESS) -> None:
        self.peer = peer
        self.request = bytearray()
        self.closed = False

    def write(self, packet: bytes) -> None:
        self.request.extend(packet)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, name: str, default=None):
        return (self.peer, 443) if name == "peername" else default


class ResponseConnector:
    def __init__(self, response: bytes = REDIRECT, peer: str = PUBLIC_ADDRESS):
        self.response = response
        self.peer = peer
        self.calls: list[dict[str, object]] = []
        self.writers: list[ResponseWriter] = []

    async def __call__(
        self, address, port, *, ssl_context, server_hostname, stream_limit
    ):
        self.calls.append(
            dict(address=address, port=port, ssl_context=ssl_context,
                 server_hostname=server_hostname, stream_limit=stream_limit)
        )
        reader = asyncio.StreamReader(limit=stream_limit)
        reader.feed_data(self.response)
        reader.feed_eof()
        writer = ResponseWriter(self.peer)
        self.writers.append(writer)
        return reader, writer


class GatedConnector(ResponseConnector):
    def __init__(self) -> None:
        super().__init__()
        self.condition = threading.Condition()
        self.waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        self.opened = False

    async def __call__(self, *args, **kwargs):
        loop = asyncio.get_running_loop()
        gate = loop.create_future()
        with self.condition:
            opened = self.opened
            if not opened:
                self.waiters.append((loop, gate))
                self.condition.notify_all()
        if not opened:
            await gate
        return await super().__call__(*args, **kwargs)

    def wait_for_requests(self, count: int) -> bool:
        with self.condition:
            return self.condition.wait_for(lambda: len(self.waiters) >= count, timeout=2)

    def release(self) -> None:
        def finish(gate):
            if not gate.done():
                gate.set_result(None)

        with self.condition:
            self.opened = True
            for loop, gate in self.waiters:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(finish, gate)


def fetch(transport, *, timeout_seconds: float = 1.0, **changes):
    arguments = dict(
        method="GET",
        headers=(("Accept", "text/html"), ("Range", "bytes=0-1023")),
        max_header_bytes=16 * 1024,
        max_body_bytes=1024,
        timeout_seconds=timeout_seconds,
    )
    arguments.update(changes)
    return transport.request(
        ResolvedTarget("https://t.co/a", "https", "t.co", 443, (PUBLIC_ADDRESS,)),
        **arguments,
    )


@pytest.mark.parametrize("acknowledged", [False, None, 1, "true"])
def test_direct_short_link_transport_requires_explicit_acknowledgement(
    acknowledged,
) -> None:
    with pytest.raises(ShortLinkTransportConfigurationError):
        LocalDirectShortLinkTransport(acknowledged=acknowledged)


def test_direct_request_reuses_numeric_tls_and_discloses_only_location() -> None:
    connector = ResponseConnector()
    transport = LocalDirectShortLinkTransport(acknowledged=True, connector=connector)

    response = fetch(transport)

    assert response.status == 302
    assert response.headers == (("Location", DESTINATION),)
    assert response.body == b""
    assert response.peer_ip == PUBLIC_ADDRESS
    assert response.effective_url == "https://t.co/a"
    assert len(connector.calls) == 1
    call = connector.calls[0]
    assert call["address"] == PUBLIC_ADDRESS
    assert call["port"] == 443
    assert call["server_hostname"] == "t.co"
    context = call["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert b"Host: t.co\r\n" in connector.writers[0].request
    assert connector.writers[0].closed


def test_simultaneous_requests_are_bounded_without_an_unbounded_wait_queue() -> None:
    connector = GatedConnector()
    transport = LocalDirectShortLinkTransport(
        acknowledged=True, connector=connector, max_concurrent_requests=2
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = [executor.submit(fetch, transport, timeout_seconds=5) for _ in range(2)]
        try:
            assert connector.wait_for_requests(2)
            with pytest.raises(ShortLinkResolutionError):
                fetch(transport, timeout_seconds=0.05)
            assert len(connector.waiters) == 2
        finally:
            connector.release()
        assert [future.result(timeout=2).status for future in active] == [302, 302]

    # Successful completion releases capacity for later requests.
    assert fetch(transport).status == 302


def test_close_latches_new_requests_while_admitted_request_finishes() -> None:
    connector = GatedConnector()
    transport = LocalDirectShortLinkTransport(
        acknowledged=True, connector=connector, max_concurrent_requests=1
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        active = executor.submit(fetch, transport, timeout_seconds=5)
        try:
            assert connector.wait_for_requests(1)
            transport.close()
            transport.close()
            with pytest.raises(ShortLinkResolutionError):
                fetch(transport)
            assert len(connector.waiters) == 1
        finally:
            connector.release()
        assert active.result(timeout=2).status == 302
    with pytest.raises(ShortLinkResolutionError):
        fetch(transport)
    assert len(connector.calls) == 1


def test_event_loop_misuse_fails_without_unawaited_coroutines_or_lost_capacity() -> None:
    connector = ResponseConnector()
    transport = LocalDirectShortLinkTransport(
        acknowledged=True, connector=connector, max_concurrent_requests=1
    )

    async def misuse() -> None:
        with pytest.raises(ShortLinkResolutionError):
            fetch(transport)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        asyncio.run(misuse())
        gc.collect()
    assert not [warning for warning in captured if "never awaited" in str(warning.message)]
    assert connector.calls == []
    assert fetch(transport).status == 302


def test_deadline_closes_upstream_and_releases_request_capacity() -> None:
    connector = ResponseConnector()
    writers: list[ResponseWriter] = []

    async def hang_once(*args, **kwargs):
        if writers:
            return await connector(*args, **kwargs)
        writer = ResponseWriter()
        writers.append(writer)
        return asyncio.StreamReader(), writer

    transport = LocalDirectShortLinkTransport(
        acknowledged=True, connector=hang_once, max_concurrent_requests=1
    )
    started = time.monotonic()
    with pytest.raises(ShortLinkResolutionError) as caught:
        fetch(transport, timeout_seconds=0.02)
    assert time.monotonic() - started < 2
    assert caught.value.reason in {
        "transport_exchange_timeout", "egress_deadline", "egress_connect_timeout"
    }
    assert writers[0].closed
    assert fetch(transport).status == 302


@pytest.mark.parametrize(
    ("response", "peer", "changes", "reason"),
    [
        (REDIRECT, "8.8.8.8", {}, "egress_peer_mismatch"),
        (
            b"HTTP/1.1 302 Found\r\nContent-Length: 1025\r\n\r\n",
            PUBLIC_ADDRESS,
            {},
            "egress_response_body_limit",
        ),
        (
            b"HTTP/1.1 302 Found\r\nX-Filler: " + b"a" * 512 + b"\r\n\r\n",
            PUBLIC_ADDRESS,
            {"max_header_bytes": 128},
            "egress_response_header_limit",
        ),
        (
            REDIRECT,
            PUBLIC_ADDRESS,
            {"headers": (("Cookie", "sensitive-upstream-value"),)},
            "transport_headers_invalid",
        ),
    ],
)
def test_existing_peer_and_byte_policies_cannot_be_bypassed(
    response, peer, changes, reason
) -> None:
    connector = ResponseConnector(response, peer)
    transport = LocalDirectShortLinkTransport(acknowledged=True, connector=connector)
    with pytest.raises(ShortLinkResolutionError) as caught:
        fetch(transport, **changes)
    assert caught.value.reason == reason
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "sensitive-upstream-value" not in str(caught.value)
    assert all(writer.closed for writer in connector.writers)


@pytest.mark.parametrize("limit", [True, 0, -1, 257, 1.5, "4"])
def test_local_request_concurrency_has_hard_integer_bounds(limit) -> None:
    with pytest.raises(ShortLinkTransportConfigurationError):
        LocalDirectShortLinkTransport(acknowledged=True, max_concurrent_requests=limit)


def test_transport_connection_limit_also_caps_local_concurrency() -> None:
    connector = GatedConnector()
    transport = LocalDirectShortLinkTransport(
        acknowledged=True,
        connector=connector,
        max_concurrent_requests=4,
        limits=ShortLinkTransportLimits(max_connections=1),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        active = executor.submit(fetch, transport, timeout_seconds=5)
        try:
            assert connector.wait_for_requests(1)
            with pytest.raises(ShortLinkResolutionError):
                fetch(transport)
            assert len(connector.waiters) == 1
        finally:
            connector.release()
        assert active.result(timeout=2).status == 302


def test_base_exception_does_not_leak_admission_capacity() -> None:
    connector = ResponseConnector()
    attempts = 0

    async def cancelled_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError()
        return await connector(*args, **kwargs)

    transport = LocalDirectShortLinkTransport(
        acknowledged=True, connector=cancelled_once, max_concurrent_requests=1
    )
    with pytest.raises(asyncio.CancelledError):
        fetch(transport)
    assert fetch(transport).status == 302


def test_service_startup_failure_is_redacted_and_releases_capacity(monkeypatch) -> None:
    connector = ResponseConnector()
    transport = LocalDirectShortLinkTransport(
        acknowledged=True, connector=connector, max_concurrent_requests=1
    )

    def fail_context():
        raise OSError("private-path upstream-cookie=secret")

    with monkeypatch.context() as scoped:
        scoped.setattr(ssl, "create_default_context", fail_context)
        with pytest.raises(ShortLinkResolutionError) as caught:
            fetch(transport)
    assert caught.value.reason == "transport_failure"
    assert "private-path" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert connector.calls == []
    assert fetch(transport).status == 302


def test_event_loop_startup_failure_does_not_leak_coroutines_or_capacity(monkeypatch) -> None:
    connector = ResponseConnector()
    transport = LocalDirectShortLinkTransport(
        acknowledged=True, connector=connector, max_concurrent_requests=1
    )

    def fail_loop():
        raise OSError("private-event-loop-details")

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with monkeypatch.context() as scoped:
            scoped.setattr(asyncio.events, "new_event_loop", fail_loop)
            with pytest.raises(ShortLinkResolutionError) as caught:
                fetch(transport)
        gc.collect()
    assert caught.value.reason == "transport_failure"
    assert "private-event-loop-details" not in str(caught.value)
    assert not [warning for warning in captured if "never awaited" in str(warning.message)]
    assert connector.calls == []
    assert fetch(transport).status == 302

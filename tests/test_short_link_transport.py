from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import stat
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_download_control.short_link_transport as transport_module
from video_download_control.security.egress import ResolvedTarget
from video_download_control.short_link_transport import (
    FileReplayStore,
    MemoryReplayStore,
    ShortLinkEgressService,
    ShortLinkTransportAuditEvent,
    ShortLinkTransportConfigurationError,
    ShortLinkTransportLimits,
    SignedShortLinkTransport,
    UnixAttestedShortLinkTransport,
    load_shared_key,
)
from video_download_control.short_links import (
    ControlledShortLinkResolver,
    ShortLinkResolutionError,
)

KEY = b"k" * 32
NOW = 1_800_000_000.0
PUBLIC_V4 = "93.184.216.34"
OTHER_PUBLIC_V4 = "8.8.8.8"
PUBLIC_V6 = "2606:4700:4700::1111"
START_URL = "https://t.co/a"
FIXED_HEADERS = (
    ("Accept", "text/html"),
    ("Range", "bytes=0-1023"),
    ("User-Agent", "transport-test"),
)


class FakeWriter:
    def __init__(self, peer_ip: str) -> None:
        self.peer_ip = peer_ip
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "peername":
            return (self.peer_ip, 443)
        return default


class FakeConnector:
    def __init__(self, *outcomes: tuple[str, bytes] | BaseException) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[dict[str, object]] = []
        self.writers: list[FakeWriter] = []

    async def __call__(
        self,
        address: str,
        port: int,
        *,
        ssl_context,
        server_hostname: str,
        stream_limit: int,
    ):
        self.calls.append(
            {
                "address": address,
                "port": port,
                "ssl_context": ssl_context,
                "server_hostname": server_hostname,
                "stream_limit": stream_limit,
            }
        )
        if not self.outcomes:
            raise AssertionError("unexpected numeric connection")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        peer_ip, raw_response = outcome
        reader = asyncio.StreamReader(limit=128 * 1024)
        reader.feed_data(raw_response)
        reader.feed_eof()
        writer = FakeWriter(peer_ip)
        self.writers.append(writer)
        return reader, writer


class HangingConnector:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, *args, **kwargs):
        del args, kwargs
        self.called = True
        await asyncio.Event().wait()


class LocalExchange:
    def __init__(self, service: ShortLinkEgressService) -> None:
        self.service = service
        self.packets: list[bytes] = []

    def __call__(self, packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        self.packets.append(packet)
        return asyncio.run(self.service.handle_frame(packet))


class FakeUnixServer:
    def __init__(self) -> None:
        self.closed = False

    @property
    def sockets(self) -> tuple[()]:
        return ()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def target(*addresses: str, url: str = START_URL) -> ResolvedTarget:
    return ResolvedTarget(
        url=url,
        scheme="https",
        host="t.co",
        port=443,
        addresses=addresses or (PUBLIC_V4,),
    )


def raw_response(
    location: str = "https://x.com/i/status/123",
    *,
    status: int = 302,
    headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> bytes:
    values = (("Location", location), *headers, ("Content-Length", str(len(body))))
    head = [f"HTTP/1.1 {status} Test"]
    head.extend(f"{name}: {value}" for name, value in values)
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def make_service(
    connector,
    *,
    replay_store=None,
    limits: ShortLinkTransportLimits | None = None,
    audit=None,
) -> ShortLinkEgressService:
    return ShortLinkEgressService(
        shared_key=KEY,
        replay_store=replay_store or MemoryReplayStore(),
        limits=limits,
        clock=lambda: NOW,
        connector=connector,
        audit=audit,
    )


def make_transport(
    exchange,
    *,
    nonce: str = "a" * 64,
    limits: ShortLinkTransportLimits | None = None,
    clock=lambda: NOW,
) -> SignedShortLinkTransport:
    return SignedShortLinkTransport(
        shared_key=KEY,
        exchange=exchange,
        limits=limits,
        clock=clock,
        nonce_factory=lambda: nonce,
    )


def request(
    transport: SignedShortLinkTransport,
    approved: ResolvedTarget | None = None,
    *,
    max_header_bytes: int = 16 * 1024,
    max_body_bytes: int = 1024,
    timeout_seconds: float = 1.0,
):
    return transport.request(
        approved or target(PUBLIC_V4),
        method="GET",
        headers=FIXED_HEADERS,
        max_header_bytes=max_header_bytes,
        max_body_bytes=max_body_bytes,
        timeout_seconds=timeout_seconds,
    )


def resign_response(packet: bytes, mutate) -> bytes:
    envelope = json.loads(packet)
    mutate(envelope)
    unsigned = {key: envelope[key] for key in envelope if key != "mac"}
    envelope["mac"] = transport_module._mac(
        KEY, transport_module._RESPONSE_DOMAIN, unsigned
    )
    return transport_module._canonical_json(envelope)


def test_numeric_connect_preserves_tls_sni_host_and_never_follows_redirect() -> None:
    connector = FakeConnector((PUBLIC_V4, raw_response()))
    exchange = LocalExchange(make_service(connector))
    transport = make_transport(exchange)

    response = request(transport)

    assert response.status == 302
    assert response.effective_url == START_URL
    assert response.peer_ip == PUBLIC_V4
    assert connector.calls == [
        {
            "address": PUBLIC_V4,
            "port": 443,
            "ssl_context": connector.calls[0]["ssl_context"],
            "server_hostname": "t.co",
            "stream_limit": 32 * 1024 + 1,
        }
    ]
    assert len(connector.writers) == 1
    upstream_request = bytes(connector.writers[0].data)
    assert upstream_request.startswith(b"GET /a HTTP/1.1\r\n")
    assert b"Host: t.co\r\n" in upstream_request
    assert b"Connection: close\r\n\r\n" in upstream_request


def test_egress_discloses_only_location_and_never_response_body() -> None:
    body_secret = b"body-sentinel-do-not-cross-control-boundary"
    connector = FakeConnector(
        (
            PUBLIC_V4,
            raw_response(
                headers=(
                    ("Set-Cookie", "session=header-secret"),
                    ("X-Upstream-Secret", "custom-secret"),
                ),
                body=body_secret,
            ),
        )
    )
    service = make_service(connector)
    response_packets: list[bytes] = []

    def exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        response_packet = asyncio.run(service.handle_frame(packet))
        response_packets.append(response_packet)
        return response_packet

    response = request(make_transport(exchange))

    assert response.headers == (("Location", "https://x.com/i/status/123"),)
    assert response.body == b""
    assert len(response_packets) == 1
    assert b"Set-Cookie" not in response_packets[0]
    assert b"header-secret" not in response_packets[0]
    assert b"X-Upstream-Secret" not in response_packets[0]
    assert b"custom-secret" not in response_packets[0]
    assert body_secret not in response_packets[0]
    assert b"body_b64" not in response_packets[0]


@pytest.mark.parametrize(
    "answers",
    [
        ("127.0.0.1",),
        ("169.254.169.254",),
        (PUBLIC_V4, "10.0.0.8"),
        (PUBLIC_V6, "::1"),
        ("::ffff:127.0.0.1", PUBLIC_V4),
    ],
)
def test_private_metadata_and_mixed_dns_sets_fail_before_connect(
    answers: tuple[str, ...],
) -> None:
    connector = FakeConnector()
    transport = make_transport(LocalExchange(make_service(connector)))

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport, target(*answers))

    assert caught.value.reason == "transport_target_invalid"
    assert all(answer not in str(caught.value) for answer in answers)
    assert connector.calls == []


def test_public_ipv4_ipv6_answers_use_numeric_fallback_without_reresolution() -> None:
    connector = FakeConnector(
        OSError("first address unavailable"),
        (PUBLIC_V6, raw_response()),
    )
    transport = make_transport(LocalExchange(make_service(connector)))

    response = request(transport, target(PUBLIC_V4, PUBLIC_V6))

    assert response.peer_ip == PUBLIC_V6
    assert [call["address"] for call in connector.calls] == [PUBLIC_V4, PUBLIC_V6]
    assert {call["server_hostname"] for call in connector.calls} == {"t.co"}


def test_default_connector_forces_numeric_resolution_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    writer = FakeWriter(PUBLIC_V4)
    returned_reader: list[asyncio.StreamReader] = []

    async def fake_open_connection(**kwargs):
        captured.update(kwargs)
        reader = asyncio.StreamReader()
        returned_reader.append(reader)
        return reader, writer

    monkeypatch.setattr(
        transport_module.asyncio, "open_connection", fake_open_connection
    )
    context = transport_module.ssl.create_default_context()

    async def exercise():
        return await ShortLinkEgressService._open_numeric_connection(
            PUBLIC_V4,
            443,
            ssl_context=context,
            server_hostname="t.co",
            stream_limit=4097,
        )

    result = asyncio.run(exercise())

    assert result == (returned_reader[0], writer)
    assert captured == {
        "host": PUBLIC_V4,
        "port": 443,
        "family": socket.AF_INET,
        "flags": socket.AI_NUMERICHOST,
        "ssl": context,
        "server_hostname": "t.co",
        "limit": 4097,
    }


def test_actual_peer_mismatch_is_signed_fixed_failure_without_address_leak() -> None:
    events: list[ShortLinkTransportAuditEvent] = []
    connector = FakeConnector((OTHER_PUBLIC_V4, raw_response()))
    transport = make_transport(
        LocalExchange(make_service(connector, audit=events.append))
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport)

    assert caught.value.reason == "egress_peer_mismatch"
    assert OTHER_PUBLIC_V4 not in str(caught.value)
    assert START_URL not in str(caught.value)
    assert events == [ShortLinkTransportAuditEvent(reason="peer_mismatch")]
    assert OTHER_PUBLIC_V4 not in repr(events)
    assert START_URL not in repr(events)


def test_cross_platform_location_is_rejected_without_logging_location() -> None:
    secret_location = "https://youtube.com/watch?v=do-not-disclose"
    connector = FakeConnector((PUBLIC_V4, raw_response(secret_location)))
    transport = make_transport(LocalExchange(make_service(connector)))
    resolver = ControlledShortLinkResolver(
        transport=transport,
        resolver=lambda host, port: (PUBLIC_V4,),
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        resolver.resolve(START_URL)

    assert caught.value.reason == "egress_host_not_allowed"
    assert secret_location not in str(caught.value)
    assert "do-not-disclose" not in str(caught.value)


def test_control_rejects_authenticated_response_claiming_redirect_was_followed() -> (
    None
):
    service = make_service(FakeConnector((PUBLIC_V4, raw_response())))

    def exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        response = asyncio.run(service.handle_frame(packet))
        return resign_response(
            response,
            lambda envelope: envelope["payload"].__setitem__(
                "effective_url", "https://x.com/secret-followed"
            ),
        )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(exchange))

    assert caught.value.reason == "transport_response_attestation"
    assert "secret-followed" not in str(caught.value)


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (
            b"HTTP/1.1 302 Test\r\nLocation: " + b"x" * 200 + b"\r\n\r\n",
            "egress_response_header_limit",
        ),
        (
            b"HTTP/1.1 302 Test\r\nLocation: /x\r\nContent-Length: 9\r\n\r\n123456789",
            "egress_response_body_limit",
        ),
    ],
)
def test_response_header_and_body_budgets_are_enforced_at_egress(
    response: bytes, expected_reason: str
) -> None:
    connector = FakeConnector((PUBLIC_V4, response))
    transport = make_transport(LocalExchange(make_service(connector)))

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport, max_header_bytes=128, max_body_bytes=8)

    assert caught.value.reason == expected_reason
    assert "Location" not in str(caught.value)


@pytest.mark.parametrize(
    "header",
    [
        ("Host", "metadata.invalid"),
        ("Authorization", "Bearer secret-token"),
        ("Cookie", "session=secret-cookie"),
        ("Proxy-Authorization", "Basic secret-proxy"),
        ("X-Arbitrary", "secret-extension"),
        ("User-Agent", "safe\tcontrol-character"),
    ],
)
def test_request_headers_use_fixed_allowlist_and_cannot_carry_credentials(
    header: tuple[str, str],
) -> None:
    connector = FakeConnector()
    transport = make_transport(LocalExchange(make_service(connector)))

    with pytest.raises(ShortLinkResolutionError) as caught:
        transport.request(
            target(PUBLIC_V4),
            method="GET",
            headers=(header,),
            max_header_bytes=1024,
            max_body_bytes=0,
            timeout_seconds=1,
        )

    assert caught.value.reason == "transport_headers_invalid"
    assert header[1] not in str(caught.value)
    assert connector.calls == []


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://t.co/a b",
        "https://t.co/a\r\nInjected: yes",
        "https://t.co/a\x00b",
        "https://t.co/a\\b",
        "https://t.co//authority-like",
        "https://t.co/\ttrimmed",
        "https://t.co/未编码路径",
    ],
)
def test_http_origin_form_rejects_smuggling_characters_before_connect(
    unsafe_url: str,
) -> None:
    connector = FakeConnector()
    transport = make_transport(LocalExchange(make_service(connector)))

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport, target(PUBLIC_V4, url=unsafe_url))

    assert caught.value.reason == "transport_target_invalid"
    assert unsafe_url not in str(caught.value)
    assert connector.calls == []


def test_deadline_cancels_hanging_numeric_connector() -> None:
    connector = HangingConnector()
    limits = ShortLinkTransportLimits(max_timeout_seconds=0.05)
    transport = make_transport(
        LocalExchange(make_service(connector, limits=limits)),
        limits=limits,
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport, timeout_seconds=0.01)

    assert connector.called is True
    assert caught.value.reason in {"egress_connect_timeout", "egress_deadline"}


def test_public_transport_failure_discards_entire_exception_context() -> None:
    def leaking_exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del packet, timeout_seconds
        raise OSError("/private/run/secret-egress.sock")

    transport = SignedShortLinkTransport(
        shared_key=KEY,
        exchange=leaking_exchange,
        clock=lambda: NOW,
        nonce_factory=lambda: "e" * 64,
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport)

    assert "secret-egress" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_public_request_reason_allowlist_collapses_injected_reason() -> None:
    covert_reason = "leak_" + "ab" * 20

    def leaking_exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del packet, timeout_seconds
        raise ShortLinkResolutionError(covert_reason, "secret diagnostic")

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(leaking_exchange, nonce="6" * 64))

    assert caught.value.reason == "transport_failure"
    assert covert_reason not in str(caught.value)
    assert "secret diagnostic" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_unix_frame_reads_share_one_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowByteClient:
        def __init__(self) -> None:
            self.calls = 0
            self.timeouts: list[float] = []

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def recv(self, size: int) -> bytes:
            del size
            self.calls += 1
            return b"x"

    client = SlowByteClient()
    times = iter((0.0, 0.5, 1.01))
    monkeypatch.setattr(transport_module.time, "monotonic", lambda: next(times))

    with pytest.raises(transport_module._TransportFailure) as caught:
        transport_module._recv_exact(client, 3, deadline=1.0)

    assert caught.value.reason == "exchange_timeout"
    assert client.calls == 2
    assert client.timeouts == [1.0, 0.5]


@pytest.mark.parametrize(
    "override",
    [
        {"max_connections": 10**9},
        {"max_request_frame_bytes": 10**9},
        {"clock_skew_seconds": 31},
        {"frame_timeout_seconds": 0},
        {"max_timeout_seconds": 20, "max_validity_seconds": 20},
    ],
)
def test_transport_limits_reject_unbounded_or_inconsistent_values(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        ShortLinkTransportLimits(**override)  # type: ignore[arg-type]


def test_listener_connection_limit_is_sparse_and_closes_without_reading() -> None:
    events: list[ShortLinkTransportAuditEvent] = []
    limits = ShortLinkTransportLimits(max_connections=1)
    service = make_service(FakeConnector(), limits=limits, audit=events.append)
    writer = FakeWriter(PUBLIC_V4)

    async def exercise() -> None:
        service._active_connections = 1
        service._client_connected(asyncio.StreamReader(), writer)
        await asyncio.gather(*tuple(service._tasks))

    asyncio.run(exercise())

    assert writer.closed is True
    assert events == [ShortLinkTransportAuditEvent(reason="connection_limit")]


def test_unix_convenience_transport_has_control_plane_constructor(
    tmp_path: Path,
) -> None:
    transport = UnixAttestedShortLinkTransport(
        unix_socket=tmp_path / "egress.sock",
        shared_key=KEY,
    )

    assert isinstance(transport, SignedShortLinkTransport)
    assert transport.unix_socket == tmp_path / "egress.sock"


def test_control_client_rejects_socket_identity_change_after_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "egress.sock"
    identities = iter(((10, 20), (10, 21)))

    class ReplacedSocketClient:
        sent = False

        def __enter__(self):
            return self

        def __exit__(self, *unused) -> None:
            del unused

        def settimeout(self, timeout: float) -> None:
            assert 0 < timeout <= 1.0

        def connect(self, path: str) -> None:
            assert path == str(socket_path)

        def sendall(self, data: bytes) -> None:
            del data
            self.sent = True

    client = ReplacedSocketClient()
    monkeypatch.setattr(transport_module.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(
        transport_module.socket,
        "socket",
        lambda family, kind: client,
    )
    monkeypatch.setattr(
        transport_module,
        "_require_unix_socket_identity",
        lambda path, *, require_private: next(identities),
    )
    transport = UnixAttestedShortLinkTransport(
        unix_socket=socket_path,
        shared_key=KEY,
        clock=lambda: NOW,
        nonce_factory=lambda: "6" * 64,
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport)

    assert caught.value.reason == "transport_unix_socket_identity"
    assert client.sent is False


@pytest.mark.parametrize("replacement_kind", ["leaf", "parent"])
def test_server_close_preserves_replaced_socket_or_parent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    socket_parent = tmp_path / "run"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "egress.sock"
    server = FakeUnixServer()

    async def fake_start_unix_server(callback, *, path: str, limit: int):
        del callback, limit
        Path(path).write_bytes(b"pretend socket")
        return server

    original_identity = transport_module._require_unix_socket_identity
    identity_calls = 0

    def startup_identity(path: Path, *, require_private: bool):
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls <= 2:
            info = path.lstat()
            return int(info.st_dev), int(info.st_ino)
        return original_identity(path, require_private=require_private)

    real_chmod = transport_module.os.chmod

    def portable_chmod(path, mode, *, follow_symlinks=True):
        del follow_symlinks
        real_chmod(path, mode)

    monkeypatch.setattr(
        transport_module.asyncio,
        "start_unix_server",
        fake_start_unix_server,
        raising=False,
    )
    monkeypatch.setattr(
        transport_module,
        "_require_unix_socket_identity",
        startup_identity,
    )
    monkeypatch.setattr(transport_module.os, "chmod", portable_chmod)
    service = make_service(FakeConnector())

    async def exercise() -> Path:
        await service.start_unix(socket_path)
        if replacement_kind == "leaf":
            socket_path.unlink()
            replacement = socket_path
        else:
            displaced = tmp_path / "old-run"
            socket_parent.rename(displaced)
            socket_parent.mkdir(mode=0o700)
            replacement = socket_parent / "egress.sock"
        replacement.write_text("replacement", encoding="ascii")
        await service.close()
        return replacement

    replacement = asyncio.run(exercise())

    assert server.closed is True
    assert replacement.read_text(encoding="ascii") == "replacement"


def test_startup_failure_does_not_unlink_replacement_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_parent = tmp_path / "run"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "egress.sock"
    server = FakeUnixServer()

    async def fake_start_unix_server(callback, *, path: str, limit: int):
        del callback, limit
        Path(path).write_bytes(b"pretend socket")
        return server

    original_identity = transport_module._require_unix_socket_identity
    first_identity = True

    def startup_identity(path: Path, *, require_private: bool):
        nonlocal first_identity
        if first_identity:
            first_identity = False
            info = path.lstat()
            return int(info.st_dev), int(info.st_ino)
        return original_identity(path, require_private=require_private)

    def replace_then_fail(path, mode, *, follow_symlinks=True):
        del mode, follow_symlinks
        replacement = Path(path)
        replacement.unlink()
        replacement.write_text("replacement", encoding="ascii")
        raise OSError("simulated startup failure")

    monkeypatch.setattr(
        transport_module.asyncio,
        "start_unix_server",
        fake_start_unix_server,
        raising=False,
    )
    monkeypatch.setattr(
        transport_module,
        "_require_unix_socket_identity",
        startup_identity,
    )
    monkeypatch.setattr(transport_module.os, "chmod", replace_then_fail)
    service = make_service(FakeConnector())

    with pytest.raises(OSError, match="simulated startup failure"):
        asyncio.run(service.start_unix(socket_path))

    assert server.closed is True
    assert socket_path.read_text(encoding="ascii") == "replacement"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or not hasattr(asyncio, "start_unix_server"),
    reason="Unix domain sockets are unavailable",
)
def test_real_unix_framed_roundtrip_and_closed_endpoint_failure(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "egress.sock"
    connector = FakeConnector((PUBLIC_V4, raw_response()))
    service = make_service(connector)
    nonces = iter(("7" * 64, "8" * 64))
    transport = UnixAttestedShortLinkTransport(
        unix_socket=socket_path,
        shared_key=KEY,
        clock=lambda: NOW,
        nonce_factory=lambda: next(nonces),
    )

    async def exercise():
        await service.start_unix(socket_path)
        response = await asyncio.to_thread(request, transport)
        await service.close()
        assert not socket_path.exists()
        with pytest.raises(ShortLinkResolutionError) as caught:
            await asyncio.to_thread(request, transport)
        return response, caught.value

    response, closed_error = asyncio.run(exercise())

    assert response.status == 302
    assert closed_error.reason in {
        "transport_failure",
        "transport_unix_socket_unsafe",
    }


def test_response_signature_tamper_and_extra_schema_field_are_rejected() -> None:
    service = make_service(FakeConnector((PUBLIC_V4, raw_response())))

    def tampered_exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        response = json.loads(asyncio.run(service.handle_frame(packet)))
        response["payload"]["status"] = 301
        return transport_module._canonical_json(response)

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(tampered_exchange))
    assert caught.value.reason == "transport_response_authentication"

    service = make_service(FakeConnector((PUBLIC_V4, raw_response())))

    def extra_field_exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        response = asyncio.run(service.handle_frame(packet))
        return resign_response(
            response,
            lambda envelope: envelope["payload"].__setitem__("extra", True),
        )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(extra_field_exchange, nonce="b" * 64))
    assert caught.value.reason == "transport_response_schema"

    service = make_service(FakeConnector((PUBLIC_V4, raw_response())))

    def boolean_version_exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        response = asyncio.run(service.handle_frame(packet))
        return resign_response(
            response,
            lambda envelope: envelope.__setitem__("version", True),
        )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(boolean_version_exchange, nonce="9" * 64))
    assert caught.value.reason == "transport_response_schema"


def test_authenticated_unknown_response_reason_collapses_to_transport_failure() -> None:
    covert_reason = "leak_" + "cd" * 20
    service = make_service(FakeConnector((PUBLIC_V4, raw_response())))

    def exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        response = asyncio.run(service.handle_frame(packet))
        return resign_response(
            response,
            lambda envelope: envelope.__setitem__(
                "payload", {"ok": False, "reason": covert_reason}
            ),
        )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(exchange, nonce="2" * 64))

    assert caught.value.reason == "transport_failure"
    assert covert_reason not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_response_encoder_and_audit_reason_allowlists_collapse_unknown_reason() -> None:
    covert_reason = "leak_" + "ef" * 20
    packet = transport_module._encode_response(
        nonce="3" * 64,
        request_hash="4" * 64,
        response=None,
        reason=covert_reason,
        issued_at_ms=int(NOW * 1000),
        shared_key=KEY,
    )
    events: list[ShortLinkTransportAuditEvent] = []
    service = make_service(FakeConnector(), audit=events.append)

    service._emit(covert_reason)

    assert json.loads(packet)["payload"] == {
        "ok": False,
        "reason": "transport_failure",
    }
    assert events == [ShortLinkTransportAuditEvent(reason="transport_failure")]
    assert covert_reason not in repr(events)


def test_request_schema_tamper_is_fixed_error_and_never_connects() -> None:
    connector = FakeConnector()
    service = make_service(connector)

    def exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        envelope = json.loads(packet)
        envelope["extra"] = "secret-request-field"
        unsigned = {key: envelope[key] for key in envelope if key != "mac"}
        envelope["mac"] = transport_module._mac(
            KEY, transport_module._REQUEST_DOMAIN, unsigned
        )
        response = asyncio.run(
            service.handle_frame(transport_module._canonical_json(envelope))
        )
        payload = json.loads(response)["payload"]
        assert payload == {"ok": False, "reason": "schema_invalid"}
        return response

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(exchange))

    assert caught.value.reason == "transport_response_binding"
    assert "secret-request-field" not in str(caught.value)
    assert connector.calls == []


@pytest.mark.parametrize(
    ("control_delay_seconds", "response_delay_seconds"),
    [
        pytest.param(86_400, 0, id="response-arrives-after-request-expiry"),
        pytest.param(4, 6, id="response-issued-after-request-expiry"),
    ],
)
def test_authenticated_response_is_bound_to_request_expiry_window(
    control_delay_seconds: float,
    response_delay_seconds: float,
) -> None:
    control_now = [NOW]
    service = make_service(FakeConnector((PUBLIC_V4, raw_response())))

    def exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        response = asyncio.run(service.handle_frame(packet))
        if response_delay_seconds:
            response = resign_response(
                response,
                lambda envelope: envelope.__setitem__(
                    "issued_at_ms",
                    int((NOW + response_delay_seconds) * 1000),
                ),
            )
        control_now[0] = NOW + control_delay_seconds
        return response

    transport = make_transport(
        exchange,
        nonce="5" * 64,
        clock=lambda: control_now[0],
    )

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(transport)

    assert caught.value.reason == "transport_response_freshness"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_nonce_replay_and_freshness_are_authenticated_failures() -> None:
    connector = FakeConnector((PUBLIC_V4, raw_response()))
    service = make_service(connector)
    exchange = LocalExchange(service)
    request(make_transport(exchange, nonce="c" * 64))

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(exchange, nonce="c" * 64))
    assert caught.value.reason == "egress_replay"

    stale_service = ShortLinkEgressService(
        shared_key=KEY,
        replay_store=MemoryReplayStore(),
        clock=lambda: NOW + 60,
        connector=FakeConnector(),
    )
    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(LocalExchange(stale_service), nonce="d" * 64))
    # The control rejects a time-invalid authenticated response before reading
    # its payload, even when that payload itself reports request freshness.
    assert caught.value.reason == "transport_response_freshness"


def test_replay_marker_outlives_request_expiry_through_allowed_clock_skew() -> None:
    service_now = [NOW]
    connector = FakeConnector((PUBLIC_V4, raw_response()))
    service = ShortLinkEgressService(
        shared_key=KEY,
        replay_store=MemoryReplayStore(),
        clock=lambda: service_now[0],
        connector=connector,
    )
    exchange = LocalExchange(service)
    request(make_transport(exchange, nonce="4" * 64))
    packet = exchange.packets[0]
    service_now[0] = NOW + 2.5

    replay_response = asyncio.run(service.handle_frame(packet))

    assert json.loads(replay_response)["payload"] == {
        "ok": False,
        "reason": "replay",
    }
    assert len(connector.calls) == 1


def test_blocking_replay_store_is_offloaded_and_bounded_by_deadline() -> None:
    packets: list[bytes] = []

    def capture(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        packets.append(packet)
        return b"{}"

    with pytest.raises(ShortLinkResolutionError):
        request(make_transport(capture, nonce="6" * 64))
    assert len(packets) == 1

    release = threading.Event()
    finished = threading.Event()
    replay_thread: list[int] = []

    class BlockingReplayStore:
        def consume(self, nonce: str, *, expires_at_ms: int, now_ms: int) -> bool:
            del nonce, expires_at_ms, now_ms
            replay_thread.append(threading.get_ident())
            release.wait(timeout=1)
            finished.set()
            return True

    service = make_service(
        FakeConnector(),
        replay_store=BlockingReplayStore(),
        limits=ShortLinkTransportLimits(frame_timeout_seconds=0.01),
    )

    async def exercise() -> bytes:
        response = await service.handle_frame(packets[0])
        await service.close()
        return response

    started = time.monotonic()
    response = asyncio.run(exercise())
    elapsed = time.monotonic() - started
    release.set()
    assert finished.wait(timeout=1)

    assert elapsed < 0.5
    assert replay_thread and replay_thread[0] != threading.get_ident()
    assert json.loads(response)["payload"] == {
        "ok": False,
        "reason": "replay_store_failure",
    }


def test_file_replay_store_survives_crash_and_fresh_nonce_recovers(
    tmp_path: Path,
) -> None:
    replay_root = tmp_path / "replay"

    class SimulatedCrash(BaseException):
        pass

    connector = FakeConnector(SimulatedCrash())
    crashed_service = make_service(
        connector,
        replay_store=FileReplayStore(replay_root),
    )
    first_packet: list[bytes] = []

    def crash_exchange(packet: bytes, timeout_seconds: float) -> bytes:
        del timeout_seconds
        first_packet.append(packet)
        try:
            return asyncio.run(crashed_service.handle_frame(packet))
        except SimulatedCrash:
            raise OSError("egress process exited") from None

    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(crash_exchange, nonce="e" * 64))
    assert caught.value.reason == "transport_failure"
    assert len(first_packet) == 1

    recovered_connector = FakeConnector((PUBLIC_V4, raw_response()))
    recovered_service = make_service(
        recovered_connector,
        replay_store=FileReplayStore(replay_root),
    )
    recovered_exchange = LocalExchange(recovered_service)
    with pytest.raises(ShortLinkResolutionError) as caught:
        request(make_transport(recovered_exchange, nonce="e" * 64))
    assert caught.value.reason == "egress_replay"

    response = request(make_transport(recovered_exchange, nonce="f" * 64))
    assert response.status == 302
    assert len(recovered_connector.calls) == 1


def test_file_replay_store_gc_is_bounded_and_preserves_unknown_entries(
    tmp_path: Path,
) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir(mode=0o700)
    expired = [f"{index:064x}" for index in range(1, 4)]
    for nonce in expired:
        (replay_root / nonce).write_text("1\n", encoding="ascii")
    malformed = "a" * 64
    (replay_root / malformed).write_text("not-an-expiry\n", encoding="ascii")
    unknown = replay_root / "operator-note"
    unknown.write_text("preserve", encoding="ascii")
    store = FileReplayStore(
        replay_root,
        gc_scan_limit=128,
        gc_delete_limit=2,
    )

    assert store.consume("b" * 64, expires_at_ms=20_000, now_ms=10_000)
    assert sum((replay_root / nonce).exists() for nonce in expired) == 1
    assert unknown.read_text(encoding="ascii") == "preserve"
    assert (replay_root / malformed).read_text(encoding="ascii") == "not-an-expiry\n"

    assert store.consume("c" * 64, expires_at_ms=20_000, now_ms=10_000)
    assert not any((replay_root / nonce).exists() for nonce in expired)
    assert unknown.exists()
    assert (replay_root / malformed).exists()
    store.close()


def test_file_replay_store_gc_never_scans_past_hard_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir(mode=0o700)
    store = FileReplayStore(replay_root, gc_scan_limit=3, gc_delete_limit=3)

    class BoundedIterator:
        def __init__(self) -> None:
            self.index = 0
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            self.index += 1
            if self.index > 10:
                raise StopIteration
            return SimpleNamespace(name=f"{self.index:064x}")

        def close(self) -> None:
            self.closed = True

    iterator = BoundedIterator()
    examined: list[str] = []

    def fake_discard(marker: Path, *, now_ms: int) -> bool:
        del now_ms
        examined.append(marker.name)
        return False

    monkeypatch.setattr(transport_module.os, "scandir", lambda path: iterator)
    monkeypatch.setattr(
        transport_module.FileReplayStore,
        "_discard_expired_marker",
        staticmethod(fake_discard),
    )

    assert store.consume("f" * 64, expires_at_ms=20_000, now_ms=10_000)
    assert set(examined[:3]) == {f"{index:064x}" for index in range(1, 4)}
    assert examined[3:] == ["f" * 64]
    assert iterator.index == 3
    store.close()
    assert iterator.closed is True


def test_file_replay_store_symlink_marker_fails_closed_without_touching_target(
    tmp_path: Path,
) -> None:
    replay_root = tmp_path / "replay"
    replay_root.mkdir(mode=0o700)
    target_file = tmp_path / "outside-marker"
    target_file.write_text("1\n", encoding="ascii")
    marker = replay_root / ("d" * 64)
    try:
        marker.symlink_to(target_file)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    store = FileReplayStore(replay_root)

    assert not store.consume("d" * 64, expires_at_ms=20_000, now_ms=10_000)

    assert target_file.read_text(encoding="ascii") == "1\n"
    assert marker.is_symlink()
    store.close()


def test_key_loader_reads_exact_private_bytes_and_rejects_bad_boundaries(
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "transport.key"
    key_path.write_bytes(KEY)
    key_path.chmod(0o600)

    assert load_shared_key(key_path) == KEY

    with pytest.raises(ShortLinkTransportConfigurationError):
        load_shared_key(Path("relative.key"))
    short = tmp_path / "short.key"
    short.write_bytes(b"too-short")
    short.chmod(0o600)
    with pytest.raises(ShortLinkTransportConfigurationError) as caught:
        load_shared_key(short)
    assert "too-short" not in str(caught.value)

    disguised = Path(f"{tmp_path}{os.sep}missing{os.sep}..{os.sep}transport.key")
    with pytest.raises(ShortLinkTransportConfigurationError):
        load_shared_key(disguised)


def test_private_security_leaves_reject_foreign_effective_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "transport.key"
    key_path.write_bytes(KEY)
    key_path.chmod(0o600)
    replay_root = tmp_path / "replay"
    replay_root.mkdir(mode=0o700)
    actual_uid = int(key_path.stat().st_uid)
    monkeypatch.setattr(transport_module, "_effective_uid", lambda: actual_uid + 1)

    with pytest.raises(ShortLinkTransportConfigurationError):
        load_shared_key(key_path)
    with pytest.raises(ShortLinkTransportConfigurationError):
        FileReplayStore(replay_root)

    socket_info = SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o600,
        st_uid=actual_uid,
        st_dev=1,
        st_ino=2,
        st_file_attributes=0,
    )
    monkeypatch.setattr(Path, "lstat", lambda self: socket_info)
    with pytest.raises(ShortLinkTransportConfigurationError):
        transport_module._require_unix_socket_identity(
            tmp_path / "egress.sock",
            require_private=True,
        )


def test_private_security_path_rejects_foreign_owned_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "transport.key"
    key_path.write_bytes(KEY)
    key_path.chmod(0o600)
    actual_lstat = Path.lstat
    actual_uid = int(tmp_path.stat().st_uid)

    def foreign_ancestor(path: Path):
        info = actual_lstat(path)
        if path == tmp_path:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_uid=actual_uid + 2,
                st_file_attributes=getattr(info, "st_file_attributes", 0),
            )
        return info

    monkeypatch.setattr(transport_module, "_effective_uid", lambda: actual_uid + 1)
    monkeypatch.setattr(Path, "lstat", foreign_ancestor)

    with pytest.raises(ShortLinkTransportConfigurationError):
        load_shared_key(key_path)


def test_key_loader_rejects_symlink_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    key_path = real_parent / "transport.key"
    key_path.write_bytes(KEY)
    key_path.chmod(0o600)
    alias_parent = tmp_path / "alias"
    try:
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ShortLinkTransportConfigurationError):
        load_shared_key(alias_parent / "transport.key")


@pytest.mark.skipif(
    os.name != "posix", reason="open file replacement is POSIX-specific"
)
def test_key_loader_rejects_leaf_swap_to_same_inode_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    key_path = real_parent / "transport.key"
    key_path.write_bytes(KEY)
    key_path.chmod(0o600)

    moved = real_parent / "moved.key"
    real_open = transport_module.os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if dir_fd is None:
            descriptor = real_open(path, flags, mode)
        else:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == key_path and not swapped:
            key_path.rename(moved)
            key_path.symlink_to(moved)
            swapped = True
        return descriptor

    monkeypatch.setattr(transport_module.os, "open", swapping_open)

    with pytest.raises(ShortLinkTransportConfigurationError) as caught:
        load_shared_key(key_path)

    assert "transport.key" not in str(caught.value)
    monkeypatch.undo()
    assert moved.read_bytes() == KEY


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_private_paths_reject_group_writable_parent(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "group-writable"
    unsafe_parent.mkdir(mode=0o770)
    unsafe_parent.chmod(0o770)
    key_path = unsafe_parent / "transport.key"
    key_path.write_bytes(KEY)
    key_path.chmod(0o600)

    with pytest.raises(ShortLinkTransportConfigurationError):
        load_shared_key(key_path)
    with pytest.raises(ShortLinkTransportConfigurationError):
        FileReplayStore(unsafe_parent / "replay")

    readable_socket_parent = tmp_path / "readable-socket-parent"
    readable_socket_parent.mkdir(mode=0o755)
    readable_socket_parent.chmod(0o755)
    with pytest.raises(ShortLinkTransportConfigurationError):
        transport_module._safe_socket_parent_identity(
            readable_socket_parent / "egress.sock"
        )


def test_protocol_packets_are_canonical_and_bind_response_to_request_hash() -> None:
    connector = FakeConnector((PUBLIC_V4, raw_response()))
    exchange = LocalExchange(make_service(connector))

    request(make_transport(exchange, nonce="1" * 64))

    packet = exchange.packets[0]
    envelope = json.loads(packet)
    assert packet == transport_module._canonical_json(envelope)
    assert set(envelope) == {
        "version",
        "kind",
        "nonce",
        "issued_at_ms",
        "expires_at_ms",
        "payload",
        "mac",
    }
    assert envelope["nonce"] == "1" * 64
    assert len(envelope["mac"]) == 64
    assert hashlib.sha256(packet).hexdigest()

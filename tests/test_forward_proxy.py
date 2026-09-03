from __future__ import annotations

import asyncio
import time
from functools import partial

import pytest

import video_download_control.security.forward_proxy as forward_proxy
from video_download_control.security.forward_proxy import (
    ControlledForwardProxy as _ControlledForwardProxy,
    ForwardProxyLimits,
    ProxyAuditEvent,
)


PUBLIC_IP = "8.8.8.8"
DEFAULT_ALLOWED_HOSTS = frozenset(
    {"example.com", "start.example", "secure.example"}
)
ControlledForwardProxy = partial(
    _ControlledForwardProxy,
    allowed_hosts=DEFAULT_ALLOWED_HOSTS,
)


class FakeWriter:
    def __init__(self, peer_ip: str = PUBLIC_IP) -> None:
        self.data = bytearray()
        self.peer_ip = peer_ip
        self.closed = False
        self.eof = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "peername":
            return (self.peer_ip, 443)
        return default

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.eof = True


class FakeServer:
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


def run(coroutine) -> None:
    asyncio.run(coroutine)


def test_http_get_pins_ip_strips_hop_headers_and_dechunks() -> None:
    async def scenario() -> None:
        resolutions: list[tuple[str, int]] = []
        connections: list[tuple[str, int]] = []
        events: list[ProxyAuditEvent] = []
        upstream_writer = FakeWriter()
        upstream_reader = stream(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: X-Upstream-Secret\r\n"
            b"X-Upstream-Secret: hidden\r\n"
            b"Proxy-Authenticate: Basic realm=secret\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )

        def resolver(host: str, port: int):
            resolutions.append((host, port))
            return [PUBLIC_IP]

        async def connector(address: str, port: int):
            connections.append((address, port))
            return upstream_reader, upstream_writer

        proxy = ControlledForwardProxy(
            resolver=resolver,
            connector=connector,
            allowed_hosts={"example.com"},
            audit=events.append,
        )
        client = FakeWriter("127.0.0.1")
        request = stream(
            b"GET http://media.example.com/file?token=do-not-log HTTP/1.1\r\n"
            b"Host: ignored.example\r\n"
            b"Proxy-Authorization: Basic c2VjcmV0\r\n"
            b"Connection: X-Remove\r\n"
            b"X-Remove: private\r\n"
            b"Cookie: session=secret\r\n\r\n"
        )
        await proxy.handle_connection(request, client)

        outbound = bytes(upstream_writer.data)
        response = bytes(client.data)
        assert resolutions == [("media.example.com", 80)]
        assert connections == [(PUBLIC_IP, 80)]
        assert outbound.startswith(
            b"GET /file?token=do-not-log HTTP/1.1\r\n"
            b"Host: media.example.com\r\n"
        )
        assert b"Proxy-Authorization" not in outbound
        assert b"X-Remove" not in outbound
        assert b"Cookie: session=secret" in outbound
        assert b"Transfer-Encoding" not in response
        assert b"Proxy-Authenticate" not in response
        assert b"X-Upstream-Secret" not in response
        assert response.endswith(b"\r\n\r\nhello")
        assert events == [
            ProxyAuditEvent("connected", "example.com"),
            ProxyAuditEvent("completed", "example.com"),
        ]
        assert "token" not in repr(events)
        assert "secret" not in repr(events)

    run(scenario())


def test_audit_uses_only_operator_policy_host_not_untrusted_subdomain() -> None:
    async def scenario() -> None:
        events: list[ProxyAuditEvent] = []

        async def connector(address: str, port: int):
            return (
                stream(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
                FakeWriter(),
            )

        proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP],
            connector=connector,
            allowed_hosts={"allowed.example"},
            audit=events.append,
        )
        await proxy.handle_connection(
            stream(
                b"GET http://topsecret.allowed.example/ HTTP/1.1\r\n\r\n"
            ),
            FakeWriter(),
        )
        assert events == [
            ProxyAuditEvent("connected", "allowed.example"),
            ProxyAuditEvent("completed", "allowed.example"),
        ]
        assert "topsecret" not in repr(events)

    run(scenario())


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")]
)
@pytest.mark.parametrize(
    "field",
    [
        "connect_timeout_seconds",
        "idle_timeout_seconds",
        "total_timeout_seconds",
    ],
)
def test_forward_proxy_rejects_non_finite_time_limits(
    field: str, value: float
) -> None:
    with pytest.raises(ValueError, match="time limits"):
        ForwardProxyLimits(**{field: value})


@pytest.mark.parametrize("allowed_hosts", [None, (), []])
def test_forward_proxy_requires_a_nonempty_host_policy(allowed_hosts) -> None:
    with pytest.raises(ValueError, match="at least one host"):
        _ControlledForwardProxy(allowed_hosts=allowed_hosts)


def test_tcp_start_accepts_only_numeric_loopback_bindings(monkeypatch) -> None:
    async def scenario() -> None:
        calls: list[tuple[str, int]] = []

        async def start_server(callback, *, host: str, port: int, limit: int):
            calls.append((host, port))
            return FakeServer()

        monkeypatch.setattr(forward_proxy.asyncio, "start_server", start_server)

        proxy = ControlledForwardProxy()
        await proxy.start("127.0.0.2", 18080)
        assert calls == [("127.0.0.2", 18080)]
        await proxy.close()

        for host in ("0.0.0.0", "::", PUBLIC_IP, "localhost"):
            rejected = ControlledForwardProxy()
            with pytest.raises(ValueError, match="loopback"):
                await rejected.start(host, 18080)
        assert calls == [("127.0.0.2", 18080)]

    run(scenario())


def test_redirect_is_forwarded_without_following_or_extra_resolution() -> None:
    async def scenario() -> None:
        resolved: list[str] = []
        upstream_reader = stream(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://next.example/path?token=opaque\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        upstream_writer = FakeWriter()

        def resolver(host: str, port: int):
            resolved.append(host)
            return [PUBLIC_IP]

        async def connector(address: str, port: int):
            return upstream_reader, upstream_writer

        proxy = ControlledForwardProxy(resolver=resolver, connector=connector)
        client = FakeWriter()
        await proxy.handle_connection(
            stream(b"GET http://start.example/a HTTP/1.1\r\n\r\n"), client
        )
        assert resolved == ["start.example"]
        assert b"HTTP/1.1 302 Found" in client.data
        assert b"Location: http://next.example/path?token=opaque" in client.data

    run(scenario())


def test_connect_tunnel_uses_approved_ip_and_bounds_both_directions() -> None:
    async def scenario() -> None:
        calls: list[tuple[str, int]] = []
        events: list[ProxyAuditEvent] = []
        upstream_reader = stream(b"server bytes")
        upstream_writer = FakeWriter()

        async def connector(address: str, port: int):
            calls.append((address, port))
            return upstream_reader, upstream_writer

        proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP],
            connector=connector,
            audit=events.append,
        )
        client_writer = FakeWriter()
        client_reader = stream(
            b"CONNECT secure.example:443 HTTP/1.1\r\n"
            b"Host: secure.example:443\r\n"
            b"Proxy-Authorization: Basic secret\r\n\r\n"
            b"client bytes"
        )
        await proxy.handle_connection(client_reader, client_writer)
        assert calls == [(PUBLIC_IP, 443)]
        assert bytes(upstream_writer.data) == b"client bytes"
        assert bytes(client_writer.data) == (
            b"HTTP/1.1 200 Connection Established\r\n\r\nserver bytes"
        )
        assert events[-1] == ProxyAuditEvent("completed", "secure.example")

    run(scenario())


@pytest.mark.parametrize(
    ("authority", "reason"),
    [
        ("secure.example", "invalid_port"),
        ("secure.example:0", "blocked_port"),
        ("secure.example:", "invalid_port"),
        ("secure.example:0443", "invalid_port"),
        ("secure.example:444", "blocked_port"),
        ("user:secret@secure.example:443", "credentials_in_url"),
        ("@secure.example:443", "credentials_in_url"),
        (":@secure.example:443", "credentials_in_url"),
        ("SECURE.example:443", "invalid_connect_target"),
        ("secure.example.:443", "invalid_connect_target"),
    ],
)
def test_connect_accepts_only_default_port_without_credentials(
    authority: str, reason: str
) -> None:
    async def scenario() -> None:
        connector_called = False

        async def connector(address: str, port: int):
            nonlocal connector_called
            connector_called = True
            return stream(), FakeWriter()

        proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP], connector=connector
        )
        client = FakeWriter()
        await proxy.handle_connection(
            stream(
                (
                    f"CONNECT {authority} HTTP/1.1\r\n"
                    "Host: secure.example:443\r\n\r\n"
                ).encode("ascii")
            ),
            client,
        )
        assert f"reason={reason}".encode("ascii") in client.data
        assert not connector_called

    run(scenario())


@pytest.mark.parametrize(
    "host_headers",
    [
        b"",
        b"Host: secure.example:443\r\nHost: secure.example:443\r\n",
        b"Host: secure.example:443\r\nHost: other.example:443\r\n",
        b"Host: other.example:443\r\n",
    ],
)
def test_connect_requires_one_matching_canonical_host_header(
    host_headers: bytes,
) -> None:
    async def scenario() -> None:
        connector_called = False

        async def connector(address: str, port: int):
            nonlocal connector_called
            connector_called = True
            return stream(), FakeWriter()

        proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP], connector=connector
        )
        client = FakeWriter()
        await proxy.handle_connection(
            stream(
                b"CONNECT secure.example:443 HTTP/1.1\r\n"
                + host_headers
                + b"\r\n"
            ),
            client,
        )
        assert b"reason=invalid_connect_host" in client.data
        assert not connector_called

    run(scenario())


def test_connect_accepts_bracketed_canonical_ipv6_authority_before_ip_policy() -> None:
    async def scenario() -> None:
        client = FakeWriter()
        proxy = ControlledForwardProxy(
            allowed_hosts={"2606:4700:4700::1111"},
            connector=lambda address, port: (_ for _ in ()).throw(
                AssertionError("IP literals must not connect")
            )
        )
        await proxy.handle_connection(
            stream(
                b"CONNECT [2606:4700:4700::1111]:443 HTTP/1.1\r\n"
                b"Host: [2606:4700:4700::1111]:443\r\n\r\n"
            ),
            client,
        )
        assert b"reason=ip_literal_blocked" in client.data

    run(scenario())


def test_absolute_https_request_is_validated_but_requires_connect() -> None:
    async def scenario() -> None:
        resolved: list[tuple[str, int]] = []
        connector_called = False

        def resolver(host: str, port: int):
            resolved.append((host, port))
            return [PUBLIC_IP]

        async def connector(address: str, port: int):
            nonlocal connector_called
            connector_called = True
            return stream(), FakeWriter()

        proxy = ControlledForwardProxy(resolver=resolver, connector=connector)
        client = FakeWriter()
        await proxy.handle_connection(
            stream(b"GET https://secure.example/a HTTP/1.1\r\n\r\n"), client
        )
        assert resolved == [("secure.example", 443)]
        assert b"reason=https_requires_connect" in client.data
        assert not connector_called

    run(scenario())


@pytest.mark.parametrize(
    ("target", "answers", "reason"),
    [
        ("http://user:secret@example.com/a", (PUBLIC_IP,), "credentials_in_url"),
        ("http://example.com:8080/a", (PUBLIC_IP,), "blocked_port"),
        ("http://example.com:0/a", (PUBLIC_IP,), "blocked_port"),
        ("http://example.com:/a", (PUBLIC_IP,), "invalid_port"),
        ("http://example.com/a", ("127.0.0.1",), "non_public_address"),
        (
            "http://example.com/a",
            (PUBLIC_IP, "169.254.169.254"),
            "non_public_address",
        ),
    ],
)
def test_http_policy_failures_never_connect_or_leak_request_secrets(
    target: str, answers: tuple[str, ...], reason: str
) -> None:
    async def scenario() -> None:
        events: list[ProxyAuditEvent] = []

        async def connector(address: str, port: int):
            raise AssertionError("a rejected target must not connect")

        proxy = ControlledForwardProxy(
            resolver=lambda host, port: answers,
            connector=connector,
            audit=events.append,
        )
        client = FakeWriter()
        await proxy.handle_connection(
            stream(
                f"GET {target}?query-secret HTTP/1.1\r\n".encode("ascii")
                + b"Cookie: cookie-secret\r\n\r\n"
            ),
            client,
        )
        output = bytes(client.data)
        assert f"reason={reason}".encode() in output
        assert b"query-secret" not in output
        assert b"cookie-secret" not in output
        assert events == [ProxyAuditEvent(reason, "example.com")]

    run(scenario())


def test_connected_peer_must_match_approved_answer() -> None:
    async def scenario() -> None:
        upstream_writer = FakeWriter("1.1.1.1")

        async def connector(address: str, port: int):
            return stream(), upstream_writer

        proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP], connector=connector
        )
        client = FakeWriter()
        await proxy.handle_connection(
            stream(b"HEAD http://example.com/a HTTP/1.1\r\n\r\n"), client
        )
        assert b"reason=peer_address_mismatch" in client.data
        assert upstream_writer.closed

    run(scenario())


def test_head_preserves_valid_content_length_without_reading_a_body() -> None:
    async def scenario() -> None:
        upstream_writer = FakeWriter()

        async def connector(address: str, port: int):
            return stream(
                b"HTTP/1.1 200 OK\r\nContent-Length: 123\r\n\r\n"
            ), upstream_writer

        proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP], connector=connector
        )
        client = FakeWriter()
        await proxy.handle_connection(
            stream(b"HEAD http://example.com/a HTTP/1.1\r\n\r\n"), client
        )
        assert b"Content-Length: 123" in client.data
        assert bytes(client.data).endswith(b"\r\n\r\n")

    run(scenario())


def test_request_response_and_tunnel_byte_limits_fail_closed() -> None:
    async def scenario() -> None:
        header_events: list[ProxyAuditEvent] = []
        header_proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP],
            limits=ForwardProxyLimits(max_request_header_bytes=48),
            audit=header_events.append,
        )
        header_client = FakeWriter()
        await header_proxy.handle_connection(
            stream(
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"X-Large: 012345678901234567890\r\n\r\n"
            ),
            header_client,
        )
        assert header_events == [
            ProxyAuditEvent("request_headers_too_large", "-")
        ]

        response_head = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n"
        response_writer = FakeWriter()

        async def response_connector(address: str, port: int):
            return stream(response_head + b"hello"), response_writer

        download_events: list[ProxyAuditEvent] = []
        response_proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP],
            connector=response_connector,
            limits=ForwardProxyLimits(
                max_download_bytes=len(response_head) + 4
            ),
            audit=download_events.append,
        )
        download_client = FakeWriter()
        await response_proxy.handle_connection(
            stream(b"GET http://example.com/ HTTP/1.1\r\n\r\n"),
            download_client,
        )
        assert b"reason=download_limit" in download_client.data
        assert download_events[-1] == ProxyAuditEvent(
            "download_limit", "example.com"
        )

        tunnel_writer = FakeWriter()

        async def tunnel_connector(address: str, port: int):
            return stream(), tunnel_writer

        upload_events: list[ProxyAuditEvent] = []
        tunnel_proxy = ControlledForwardProxy(
            resolver=lambda host, port: [PUBLIC_IP],
            connector=tunnel_connector,
            limits=ForwardProxyLimits(max_upload_bytes=3),
            audit=upload_events.append,
        )
        tunnel_client = FakeWriter()
        await tunnel_proxy.handle_connection(
            stream(
                b"CONNECT secure.example:443 HTTP/1.1\r\n"
                b"Host: secure.example:443\r\n\r\nmore"
            ),
            tunnel_client,
        )
        assert bytes(tunnel_writer.data) == b""
        assert upload_events[-1] == ProxyAuditEvent(
            "upload_limit", "secure.example"
        )

    run(scenario())


def test_idle_total_and_connection_count_deadlines_are_bounded() -> None:
    async def scenario() -> None:
        idle_events: list[ProxyAuditEvent] = []
        idle_proxy = ControlledForwardProxy(
            limits=ForwardProxyLimits(
                idle_timeout_seconds=0.01, total_timeout_seconds=0.2
            ),
            audit=idle_events.append,
        )
        idle_client = FakeWriter()
        await idle_proxy.handle_connection(stream(eof=False), idle_client)
        assert idle_events == [ProxyAuditEvent("idle_timeout", "-")]
        assert b"reason=idle_timeout" in idle_client.data

        total_events: list[ProxyAuditEvent] = []
        total_proxy = ControlledForwardProxy(
            limits=ForwardProxyLimits(
                idle_timeout_seconds=0.2, total_timeout_seconds=0.01
            ),
            audit=total_events.append,
        )
        total_client = FakeWriter()
        await total_proxy.handle_connection(stream(eof=False), total_client)
        assert total_events == [ProxyAuditEvent("total_timeout", "-")]

        count_events: list[ProxyAuditEvent] = []
        count_proxy = ControlledForwardProxy(
            limits=ForwardProxyLimits(
                max_connections=1,
                idle_timeout_seconds=0.2,
                total_timeout_seconds=0.4,
            ),
            audit=count_events.append,
        )
        first_writer = FakeWriter()
        second_writer = FakeWriter()
        count_proxy._client_connected(stream(eof=False), first_writer)
        count_proxy._client_connected(stream(eof=False), second_writer)
        await asyncio.sleep(0.01)
        assert b"reason=connection_limit" in second_writer.data
        assert ProxyAuditEvent("connection_limit", "-") in count_events
        await count_proxy.close()

    run(scenario())


def test_total_deadline_includes_synchronous_resolution() -> None:
    async def scenario() -> None:
        events: list[ProxyAuditEvent] = []

        def slow_resolver(host: str, port: int):
            time.sleep(0.05)
            return [PUBLIC_IP]

        proxy = ControlledForwardProxy(
            resolver=slow_resolver,
            limits=ForwardProxyLimits(
                idle_timeout_seconds=0.2, total_timeout_seconds=0.01
            ),
            audit=events.append,
        )
        client = FakeWriter()
        await proxy.handle_connection(
            stream(b"HEAD http://example.com/ HTTP/1.1\r\n\r\n"), client
        )
        assert events == [ProxyAuditEvent("total_timeout", "example.com")]

    run(scenario())


def test_unix_socket_start_chmod_and_owned_cleanup_are_offline(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "proxy.sock"
        identities: dict[object, tuple[int, int]] = {}
        unlinked: list[object] = []
        chmod_calls: list[tuple[object, int, bool]] = []
        factory_calls: list[tuple[object, str, int]] = []
        server = FakeServer()

        def identity(path):
            return identities.get(path)

        def unlink(path) -> None:
            unlinked.append(path)
            identities.pop(path, None)

        def chmod(path, mode, *, follow_symlinks) -> None:
            chmod_calls.append((path, mode, follow_symlinks))

        async def factory(callback, *, path: str, limit: int):
            factory_calls.append((callback, path, limit))
            identities[socket_path] = (7, 11)
            return server

        monkeypatch.setattr(forward_proxy, "_unix_socket_identity", identity)
        monkeypatch.setattr(forward_proxy.os, "unlink", unlink)
        monkeypatch.setattr(forward_proxy.os, "chmod", chmod)

        proxy = ControlledForwardProxy(unix_server_factory=factory)
        returned = await proxy.start_unix(socket_path, mode=0o620)
        assert returned is server
        assert factory_calls[0][1] == str(socket_path)
        assert callable(factory_calls[0][0])
        assert chmod_calls == [(socket_path, 0o620, False)]
        await proxy.close()
        assert server.closed and server.waited
        assert unlinked == [socket_path]

        replacement_server = FakeServer()

        async def replacement_factory(callback, *, path: str, limit: int):
            identities[socket_path] = (7, 12)
            return replacement_server

        replacement_proxy = ControlledForwardProxy(
            unix_server_factory=replacement_factory
        )
        await replacement_proxy.start_unix(socket_path)
        identities[socket_path] = (7, 99)
        await replacement_proxy.close()
        assert identities[socket_path] == (7, 99)
        assert unlinked == [socket_path]

    run(scenario())


def test_unix_socket_existing_socket_is_never_probed_or_unlinked(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "proxy.sock"
        identities: dict[object, tuple[int, int]] = {socket_path: (4, 1)}
        unlinked: list[object] = []
        connector_called = False
        factory_called = False

        def identity(path):
            return identities.get(path)

        def unlink(path) -> None:
            unlinked.append(path)
            identities.pop(path, None)

        async def forbidden_connector(path: str):
            nonlocal connector_called
            connector_called = True
            raise AssertionError("existing sockets must not be probed")

        async def forbidden_factory(callback, *, path: str, limit: int):
            nonlocal factory_called
            factory_called = True
            raise AssertionError("existing sockets must not be replaced")

        monkeypatch.setattr(forward_proxy, "_unix_socket_identity", identity)
        monkeypatch.setattr(forward_proxy.os, "unlink", unlink)
        proxy = ControlledForwardProxy(
            unix_connector=forbidden_connector,
            unix_server_factory=forbidden_factory,
        )
        with pytest.raises(FileExistsError, match="already exists"):
            await proxy.start_unix(socket_path)
        assert not connector_called
        assert not factory_called
        assert identities[socket_path] == (4, 1)
        assert unlinked == []

    run(scenario())


def test_unix_socket_never_replaces_regular_file(tmp_path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "proxy.sock"
        socket_path.write_text("do not delete", encoding="utf-8")
        factory_called = False

        async def factory(callback, *, path: str, limit: int):
            nonlocal factory_called
            factory_called = True
            return FakeServer()

        proxy = ControlledForwardProxy(unix_server_factory=factory)
        with pytest.raises(FileExistsError, match="non-socket"):
            await proxy.start_unix(socket_path)
        assert socket_path.read_text(encoding="utf-8") == "do not delete"
        assert not factory_called

    run(scenario())

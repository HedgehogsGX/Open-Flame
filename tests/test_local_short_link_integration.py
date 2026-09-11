from __future__ import annotations

import asyncio
import os
import ssl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

import video_download_control.api as api_module
import video_download_control.short_links as short_links_module
from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.api import create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.config import Settings
from video_download_control.local_app import LocalAppConfig
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository

PUBLIC_IP = "93.184.216.34"
SHORT_LINKS = (
    "https://b23.tv/LocalBili",
    "https://v.douyin.com/LocalDouyin",
    "https://vm.tiktok.com/LocalTikTokVm",
    "https://vt.tiktok.com/LocalTikTokVt",
)
FINAL_URLS = {
    "b23.tv": "https://www.bilibili.com/video/BV1Ab411c7mD",
    "v.douyin.com": "https://www.douyin.com/video/1234567890",
    "vm.tiktok.com": "https://www.tiktok.com/@example/video/7461234567890123456",
    "vt.tiktok.com": "https://www.tiktok.com/@example/video/7461234567890123457",
}
REDIRECT_SENTINEL = "synthetic-redirect-token-never-persist"


class RecordingWriter:
    def __init__(self) -> None:
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

    def get_extra_info(self, name: str, default=None):
        return (PUBLIC_IP, 443) if name == "peername" else default


class RedirectConnector:
    """Offline numeric socket seam; real transport still parses HTTP and TLS policy."""

    def __init__(self) -> None:
        self.calls = []
        self.writers = []
        self.block_connection = False
        self.entered = Event()
        self._gate = None
        self._loop = None

    def release(self) -> None:
        if self._loop is not None and self._gate is not None:
            self._loop.call_soon_threadsafe(self._gate.set)

    async def __call__(
        self, address, port, *, ssl_context, server_hostname, stream_limit,
    ):
        self.calls.append((address, port, ssl_context, server_hostname, stream_limit))
        assert address == PUBLIC_IP
        assert port == 443
        assert ssl_context.check_hostname
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        # A final platform page must never be fetched by short-link expansion.
        assert server_hostname in FINAL_URLS
        if self.block_connection:
            self._loop = asyncio.get_running_loop()
            self._gate = asyncio.Event()
            self.entered.set()
            await self._gate.wait()
        location = FINAL_URLS[server_hostname] + "?signature=" + REDIRECT_SENTINEL
        reader = asyncio.StreamReader(limit=stream_limit)
        reader.feed_data(
            (
                "HTTP/1.1 302 Found\r\n"
                f"Location: {location}\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode("ascii")
        )
        reader.feed_eof()
        writer = RecordingWriter()
        self.writers.append(writer)
        return reader, writer


@pytest.fixture
def network_seam(monkeypatch):
    connector = RedirectConnector()
    transports = []
    dns_calls = []

    def dns_answers(host, port):
        dns_calls.append((host, port))
        return (PUBLIC_IP,)

    def transport_factory(**kwargs):
        # Import lazily so the original wiring gap can be reproduced before the
        # production local transport exists. This factory is never a resolver.
        from video_download_control.local_short_links import LocalDirectShortLinkTransport

        transport = LocalDirectShortLinkTransport(connector=connector, **kwargs)
        transports.append(transport)
        return transport

    monkeypatch.setattr(short_links_module, "_system_dns_answers", dns_answers)
    monkeypatch.setattr(
        api_module, "LocalDirectShortLinkTransport", transport_factory, raising=False,
    )
    return connector, transports, dns_calls


def submit(client, entrypoint, links=SHORT_LINKS):
    if entrypoint == "json":
        return client.post(
            "/api/v1/batches", json={"name": "local short links", "inputs": list(links)},
        )
    filename = f"local-short-links.{entrypoint}"
    content = ("url\n" if entrypoint == "csv" else "") + "\n".join(links) + "\n"
    return client.post(
        "/api/v1/batches/import",
        params={"filename": filename, "name": "local short links"},
        content=content.encode("utf-8"),
        headers={"content-type": "text/csv" if entrypoint == "csv" else "text/plain"},
    )


@pytest.mark.parametrize("entrypoint", ["json", "txt", "csv"])
@pytest.mark.skipif(os.name != "nt", reason="local direct mode requires Windows")
def test_local_app_short_links_queue_and_reach_offline_ready(
    tmp_path: Path, network_seam, entrypoint,
) -> None:
    connector, transports, dns_calls = network_seam
    config = LocalAppConfig(app_root=tmp_path.resolve(), allow_direct_network=True)
    settings = config.control_settings()
    app = create_app(settings)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        response = submit(client, entrypoint)
        assert response.status_code == 201
        payload = response.json()
        assert payload["queued_count"] == 4, [
            item["error_code"] for item in payload["inputs"]
        ]
        assert payload["failed_count"] == 0
        assert settings.short_link_resolution_enabled
        assert settings.local_direct_short_links
        assert settings.short_link_transport_socket is None
        assert settings.short_link_attestation_key_file is None
        assert len(transports) == 1
        assert {job["canonical_url"] for job in payload["jobs"]} == set(FINAL_URLS.values())
        assert {job["platform"] for job in payload["jobs"]} == {"bilibili", "douyin", "tiktok"}
        assert REDIRECT_SENTINEL not in response.text

        worker = Worker(
            worker_id="local-short-link-offline",
            repository=WorkerRepository(app.state.database),
            adapter=ScriptedFakeAdapter(),
            asset_store=AssetStore(settings.data_root),
            verifier=NonEmptyTestVerifier(),
        )
        results = [worker.run_once() for _ in range(4)]
        assert all(result is not None and result.status == "ready" for result in results)
        assert worker.run_once() is None
        refreshed = client.get(f"/api/v1/batches/{payload['id']}").json()
        assert refreshed["status"] == "ready"
        assert refreshed["ready_count"] == 4
        assert REDIRECT_SENTINEL not in str(refreshed)

    assert len(connector.calls) == len(connector.writers) == 4
    assert all(writer.closed for writer in connector.writers)
    assert {call[3] for call in connector.calls} == set(FINAL_URLS)
    assert {host for host, _ in dns_calls} >= set(FINAL_URLS)
    for writer, call in zip(connector.writers, connector.calls, strict=True):
        request_bytes = bytes(writer.data)
        assert f"Host: {call[3]}".encode() in request_bytes
        assert b"Authorization:" not in request_bytes
        assert b"Cookie:" not in request_bytes
    with app.state.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 4
    for path in settings.data_root.rglob("*.json"):
        assert REDIRECT_SENTINEL not in path.read_text(encoding="utf-8")
    for path in (settings.data_root / "logs").glob("*.jsonl"):
        assert REDIRECT_SENTINEL not in path.read_text(encoding="utf-8")


def test_local_app_without_direct_network_does_not_resolve_short_links(
    tmp_path: Path, network_seam,
) -> None:
    connector, transports, dns_calls = network_seam
    config = LocalAppConfig(app_root=tmp_path.resolve(), allow_direct_network=False)
    settings = config.control_settings()

    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        response = submit(client, "json")

    assert response.status_code == 201
    assert not settings.short_link_resolution_enabled
    assert response.json()["queued_count"] == 0
    assert {item["error_code"] for item in response.json()["inputs"]} == {
        "short_link_resolution_required",
    }
    assert transports == connector.calls == dns_calls == []


@pytest.mark.parametrize("entrypoint", ["txt", "csv"])
@pytest.mark.skipif(os.name != "nt", reason="local direct mode requires Windows")
def test_short_link_import_keeps_health_and_job_cancel_responsive(
    tmp_path: Path, network_seam, entrypoint,
) -> None:
    connector, _, _ = network_seam
    connector.block_connection = True
    config = LocalAppConfig(app_root=tmp_path.resolve(), allow_direct_network=True)
    app = create_app(config.control_settings())

    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        queued = submit(
            client, "json", links=("https://www.youtube.com/watch?v=cancel-during-import",),
        )
        assert queued.status_code == 201
        job_id = queued.json()["jobs"][0]["id"]
        with ThreadPoolExecutor(max_workers=3) as executor:
            imported = executor.submit(submit, client, entrypoint, (SHORT_LINKS[0],))
            try:
                assert connector.entered.wait(timeout=3)
                health = executor.submit(client.get, "/health")
                canceled = executor.submit(client.post, f"/api/v1/jobs/{job_id}/cancel")
                assert health.result(timeout=2).status_code == 200
                cancel_response = canceled.result(timeout=2)
                assert cancel_response.status_code == 200
                assert cancel_response.json()["status"] == "canceled"
                # Both controls must finish while expansion is still suspended,
                # not merely after the short-link request times out or completes.
                assert not imported.done()
            finally:
                connector.release()
            import_response = imported.result(timeout=5)

        assert import_response.status_code == 201
        assert import_response.json()["queued_count"] == 1
        assert app.state.worker_repository.get_job(job_id)["status"] == "canceled"


@pytest.mark.parametrize("mode", ["disabled", "socket", "key", "socket_and_key"])
def test_settings_rejects_local_direct_mixed_or_unenabled_configuration(
    tmp_path: Path, mode,
) -> None:
    root = tmp_path.resolve()
    configuration = {
        "data_root": root / "data",
        "database_path": root / "data" / "control.sqlite3",
        "short_link_resolution_enabled": mode != "disabled",
        "local_direct_short_links": True,
    }
    if mode in {"socket", "socket_and_key"}:
        configuration["short_link_transport_socket"] = root / "transport.sock"
    if mode in {"key", "socket_and_key"}:
        configuration["short_link_attestation_key_file"] = root / "transport.key"

    with pytest.raises(ValueError, match="local short-link mode"):
        Settings(**configuration)


@pytest.mark.parametrize("acknowledgement", [1, 0, "true", None])
def test_settings_requires_boolean_local_direct_acknowledgement(
    tmp_path: Path, acknowledgement,
) -> None:
    root = tmp_path.resolve()
    with pytest.raises(ValueError, match="acknowledgement must be boolean"):
        Settings(
            data_root=root / "data",
            database_path=root / "data" / "control.sqlite3",
            local_direct_short_links=acknowledgement,
        )


def test_generic_environment_does_not_implicitly_enable_local_direct_short_links(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path.resolve()
    monkeypatch.setenv("VDC_DATA_ROOT", str(root / "data"))
    monkeypatch.setenv("VDC_DATABASE_PATH", str(root / "data" / "control.sqlite3"))
    monkeypatch.setenv("VDC_HOST", "127.0.0.1")
    monkeypatch.setenv("VDC_ENABLE_SHORT_LINK_RESOLUTION", "0")
    monkeypatch.delenv("VDC_SHORT_LINK_TRANSPORT_SOCKET", raising=False)
    monkeypatch.delenv("VDC_SHORT_LINK_ATTESTATION_KEY_FILE", raising=False)

    settings = Settings.from_env()
    assert not settings.short_link_resolution_enabled
    assert not settings.local_direct_short_links

    # Generic short-link enablement remains the existing attested Unix mode;
    # it cannot silently become direct networking without the standalone opt-in.
    monkeypatch.setenv("VDC_ENABLE_SHORT_LINK_RESOLUTION", "1")
    with pytest.raises(ValueError, match="requires both transport socket"):
        Settings.from_env()

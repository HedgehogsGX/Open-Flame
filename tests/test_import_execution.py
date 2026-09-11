from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.domain import Platform
from video_download_control.normalization import normalize_url
from video_download_control.short_links import ShortLinkResolution


def test_short_link_file_import_runs_blocking_resolution_outside_event_loop(settings):
    observed_running_loop = []

    class Resolver:
        def resolve(self, _url, *, timeout_seconds):
            try:
                asyncio.get_running_loop()
                observed_running_loop.append(True)
            except RuntimeError:
                observed_running_loop.append(False)
            return ShortLinkResolution(
                normalized=normalize_url("https://www.bilibili.com/video/BV1xx411c7mD"),
                platform=Platform.BILIBILI,
                redirect_count=1,
                policy_hosts=("b23.tv", "www.bilibili.com"),
            )

    configured = replace(
        settings, short_link_resolution_enabled=True,
        short_link_transport_socket=settings.data_root / "transport.sock",
        short_link_attestation_key_file=settings.data_root / "key",
    )
    with TestClient(create_app(configured, short_link_resolver=Resolver()), base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        response = client.post(
            "/api/v1/batches/import?filename=links.txt",
            content="https://b23.tv/synthetic",
            headers={"Content-Type": "text/plain"},
        )
    assert response.status_code == 201
    assert response.json()["queued_count"] == 1
    assert observed_running_loop == [False]

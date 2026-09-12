from __future__ import annotations

from local_http_client import download_client

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.api import create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.domain import Platform, SourceType
from video_download_control.normalization import NormalizedURL
from video_download_control.short_links import ShortLinkResolution
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


@pytest.mark.parametrize(
    ("submitted_url", "canonical_url", "platform", "source_type"),
    [
        (
            "https://m.youtube.com/watch?v=VideoRoute01&feature=share",
            "https://www.youtube.com/watch?v=VideoRoute01",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_VIDEO,
        ),
        (
            "https://youtube.com/shorts/ShortRoute02?feature=share",
            "https://www.youtube.com/shorts/ShortRoute02",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_SHORT,
        ),
        (
            "https://m.bilibili.com/video/BV1xx411c7mD?p=1&spm_id_from=333",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
        ),
        (
            "https://www.bilibili.com/video/AV170001?p=1",
            "https://www.bilibili.com/video/av170001",
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
        ),
    ],
    ids=("youtube-video", "youtube-shorts", "bilibili-bv", "bilibili-av"),
)
def test_direct_mvp_route_normalizes_and_reaches_a_ready_asset(
    service,
    settings,
    database,
    submitted_url: str,
    canonical_url: str,
    platform: Platform,
    source_type: SourceType,
) -> None:
    batch = service.create_batch(
        name="direct route e2e",
        raw_inputs=[submitted_url],
    )
    queued_input = batch["inputs"][0]
    queued_job = batch["jobs"][0]

    assert queued_input["canonical_url"] == canonical_url
    assert queued_job["platform"] == platform.value
    assert queued_job["source_type"] == source_type.value

    worker = Worker(
        worker_id="mvp-route-worker",
        repository=WorkerRepository(database),
        adapter=ScriptedFakeAdapter(payload=b"direct route payload\n"),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
    )

    result = worker.run_once()
    refreshed = service.get_batch(batch["id"])
    assets = service.list_ready_assets_for_batch(batch["id"])

    assert result is not None and result.status == "ready"
    assert refreshed is not None and refreshed["status"] == "ready"
    assert refreshed["inputs"][0]["status"] == "ready"
    assert assets is not None and len(assets) == 1
    assert assets[0]["job_id"] == queued_job["id"]
    assert len(assets[0]["sha256"]) == 64


class _ControlledDouyinResolver:
    def resolve(
        self,
        submitted_url: str,
        *,
        timeout_seconds: float,
    ) -> ShortLinkResolution:
        assert submitted_url == "https://v.douyin.com/DouyinShare01"
        assert 0 < timeout_seconds <= 15
        return ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url=(
                    "https://www.douyin.com/video/7123456789012345678"
                    "?signature=must-not-be-persisted"
                ),
                canonical_url=(
                    "https://www.douyin.com/video/7123456789012345678"
                ),
                platform=Platform.DOUYIN,
                source_type=SourceType.DOUYIN_VIDEO,
                source_id="7123456789012345678",
            ),
            platform=Platform.DOUYIN,
            redirect_count=1,
            policy_hosts=("v.douyin.com", "douyin.com"),
        )


def test_douyin_share_link_reaches_a_ready_asset_through_the_api(
    settings,
) -> None:
    enabled_settings = replace(
        settings,
        short_link_resolution_enabled=True,
        short_link_transport_socket=settings.data_root / "unused-short-link.sock",
        short_link_attestation_key_file=settings.data_root / "unused-short-link.key",
    )
    app = create_app(
        enabled_settings,
        short_link_resolver=_ControlledDouyinResolver(),
    )
    media = b"douyin share-link payload\n"

    with download_client(app) as client:
        created_response = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://v.douyin.com/DouyinShare01/?share=1"]},
        )
        created = created_response.json()
        worker = Worker(
            worker_id="douyin-share-link-worker",
            repository=WorkerRepository(app.state.database),
            adapter=ScriptedFakeAdapter(payload=media),
            asset_store=AssetStore(settings.data_root, min_free_bytes=0),
            verifier=NonEmptyTestVerifier(),
        )
        result = worker.run_once()
        fetched = client.get(f"/api/v1/batches/{created['id']}")
        assets_response = client.get(f"/api/v1/batches/{created['id']}/assets")
        assets = assets_response.json()
        downloaded = client.get(assets[0]["download_url"])

    assert created_response.status_code == 201
    assert created["inputs"][0]["submitted_url"] == (
        "https://v.douyin.com/DouyinShare01/?share=1"
    )
    assert created["inputs"][0]["canonical_url"] == (
        "https://www.douyin.com/video/7123456789012345678"
    )
    assert created["jobs"][0]["platform"] == Platform.DOUYIN.value
    assert created["jobs"][0]["source_type"] == SourceType.DOUYIN_VIDEO.value
    assert result is not None and result.status == "ready"
    assert fetched.status_code == 200 and fetched.json()["status"] == "ready"
    assert assets_response.status_code == 200 and len(assets) == 1
    assert downloaded.status_code == 200 and downloaded.content == media
    assert "must-not-be-persisted" not in created_response.text
    assert "must-not-be-persisted" not in fetched.text


@pytest.mark.parametrize(
    (
        "filename",
        "content_type",
        "content",
        "canonical_url",
        "platform",
        "source_type",
    ),
    [
        (
            "urls.txt",
            "text/plain; charset=utf-8",
            b"https://youtu.be/ImportTxt01\n",
            "https://www.youtube.com/watch?v=ImportTxt01",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_VIDEO,
        ),
        (
            "urls.csv",
            "text/csv; charset=utf-8",
            (
                b"title,video_url,note\n"
                b"one,https://www.bilibili.com/video/av170001?p=1,ok\n"
            ),
            "https://www.bilibili.com/video/av170001",
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
        ),
        (
            "urls.csv",
            "text/csv; charset=utf-8",
            (
                b"one,https://youtube.com/shorts/ImportCsv03?feature=share,note\n"
            ),
            "https://www.youtube.com/shorts/ImportCsv03",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_SHORT,
        ),
    ],
    ids=("txt", "headered-csv", "headerless-csv"),
)
def test_file_import_reaches_a_ready_downloadable_asset(
    settings,
    filename: str,
    content_type: str,
    content: bytes,
    canonical_url: str,
    platform: Platform,
    source_type: SourceType,
) -> None:
    app = create_app(settings)
    media = f"{filename}:{source_type.value}\n".encode()

    with download_client(app) as client:
        created_response = client.post(
            "/api/v1/batches/import",
            params={"filename": filename, "name": "import route e2e"},
            content=content,
            headers={"content-type": content_type},
        )
        created = created_response.json()
        worker = Worker(
            worker_id="import-route-worker",
            repository=WorkerRepository(app.state.database),
            adapter=ScriptedFakeAdapter(payload=media),
            asset_store=AssetStore(settings.data_root, min_free_bytes=0),
            verifier=NonEmptyTestVerifier(),
        )
        result = worker.run_once()
        fetched = client.get(f"/api/v1/batches/{created['id']}")
        assets_response = client.get(f"/api/v1/batches/{created['id']}/assets")
        assets = assets_response.json()
        downloaded = client.get(assets[0]["download_url"])

    assert created_response.status_code == 201
    assert created["queued_count"] == 1
    assert created["inputs"][0]["canonical_url"] == canonical_url
    assert created["jobs"][0]["platform"] == platform.value
    assert created["jobs"][0]["source_type"] == source_type.value
    assert result is not None and result.status == "ready"
    assert fetched.status_code == 200 and fetched.json()["status"] == "ready"
    assert assets_response.status_code == 200 and len(assets) == 1
    assert downloaded.status_code == 200 and downloaded.content == media

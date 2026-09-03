from __future__ import annotations

from datetime import UTC, datetime

import pytest

from video_download_control.adapters import AdapterRoute, ScriptedFakeAdapter
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.domain import Platform, SourceType
from video_download_control.service import BatchService
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


NOW = datetime(2026, 9, 3, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "platform", "source_type"),
    [
        (
            "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
        ),
        (
            "https://www.douyin.com/video/7123456789012345678",
            Platform.DOUYIN,
            SourceType.DOUYIN_VIDEO,
        ),
        (
            "https://www.tiktok.com/@example/video/7461234567890123456",
            Platform.TIKTOK,
            SourceType.TIKTOK_VIDEO,
        ),
        (
            "https://www.instagram.com/reel/AbC_def-123/",
            Platform.INSTAGRAM,
            SourceType.INSTAGRAM_REEL,
        ),
    ],
)
def test_declared_platform_route_reaches_ready_asset_offline(
    service: BatchService,
    settings,
    database,
    url: str,
    platform: Platform,
    source_type: SourceType,
) -> None:
    batch = service.create_batch(name="platform route", raw_inputs=[url])
    worker = Worker(
        worker_id="platform-route-worker",
        repository=WorkerRepository(database),
        adapter=ScriptedFakeAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=lambda: NOW,
    )

    result = worker.run_once()
    refreshed = service.get_batch(batch["id"])

    assert result is not None
    assert result.status == "ready"
    assert refreshed is not None
    assert refreshed["status"] == "ready"
    assert refreshed["jobs"][0]["platform"] == platform.value
    assert refreshed["jobs"][0]["source_type"] == source_type.value
    assert len(service.list_ready_assets_for_batch(batch["id"])) == 1


class _BilibiliOnlyFakeAdapter(ScriptedFakeAdapter):
    name = "bilibili_only_fake"
    supported_routes = frozenset(
        {AdapterRoute(Platform.BILIBILI, SourceType.BILIBILI_VIDEO)}
    )


def test_worker_skips_older_job_outside_its_declared_route(
    service: BatchService,
    settings,
    database,
) -> None:
    douyin = service.create_batch(
        name="older unsupported route",
        raw_inputs=["https://www.douyin.com/video/7123456789012345678"],
    )
    bilibili = service.create_batch(
        name="supported route",
        raw_inputs=["https://www.bilibili.com/video/BV1xx411c7mD"],
    )
    worker = Worker(
        worker_id="bilibili-only-worker",
        repository=WorkerRepository(database),
        adapter=_BilibiliOnlyFakeAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=lambda: NOW,
    )

    result = worker.run_once()

    assert result is not None
    assert result.job_id == bilibili["jobs"][0]["id"]
    assert service.get_batch(bilibili["id"])["status"] == "ready"
    assert service.get_batch(douyin["id"])["status"] == "queued"

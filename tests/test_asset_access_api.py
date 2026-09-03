from __future__ import annotations

import os

from fastapi.testclient import TestClient

from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.api import create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.config import Settings
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


def _finish_fake_download(app, settings: Settings, *, payload: bytes) -> None:
    worker = Worker(
        worker_id="asset-api-test-worker",
        repository=WorkerRepository(app.state.database),
        adapter=ScriptedFakeAdapter(payload=payload),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
    )
    result = worker.run_once()
    assert result is not None
    assert result.status == "ready"


def test_ready_batch_lists_original_metadata_and_downloads_registered_file(
    settings: Settings,
) -> None:
    media = b"registered original bytes\n"
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={
                "name": "title-must-not-enter-download-header",
                "inputs": ["https://youtu.be/asset-api-ready"],
            },
        ).json()
        batch_id = created["id"]
        job_id = created["jobs"][0]["id"]

        pending = client.get(f"/api/v1/batches/{batch_id}/assets")
        _finish_fake_download(app, settings, payload=media)
        listed = client.get(f"/api/v1/batches/{batch_id}/assets")

        assert listed.status_code == 200
        assets = listed.json()
        assert len(assets) == 1
        asset = assets[0]
        downloaded = client.get(asset["download_url"])
        log_status = client.get("/api/v1/operations/logs?limit=100").json()

    assert pending.status_code == 200
    assert pending.json() == []
    assert set(asset) == {
        "asset_id",
        "job_id",
        "ordinal",
        "original",
        "download_url",
    }
    assert asset["job_id"] == job_id
    assert asset["ordinal"] == 0
    assert asset["download_url"] == (
        f"/api/v1/assets/{asset['asset_id']}/download"
    )
    assert asset["original"] == {
        "media_kind": "video",
        "duration_seconds": None,
        "container": "fake",
        "codec": None,
        "width": None,
        "height": None,
        "size_bytes": len(media),
        "sha256": asset["original"]["sha256"],
    }
    assert len(asset["original"]["sha256"]) == 64
    assert "path" not in listed.text
    assert "title-must-not-enter-download-header" not in listed.text

    assert downloaded.status_code == 200
    assert downloaded.content == media
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    disposition = downloaded.headers["content-disposition"]
    assert disposition == f'attachment; filename="asset-{asset["asset_id"]}.fake"'
    assert "title-must-not-enter-download-header" not in disposition
    assert log_status["rejected_events"] == 0


def test_asset_download_rejects_unregistered_missing_and_outside_paths(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        missing_batch = client.get("/api/v1/batches/not-found/assets")
        missing_asset = client.get(
            "/api/v1/assets/00000000-0000-0000-0000-000000000000/download"
        )
        invalid_asset = client.get("/api/v1/assets/not-a-uuid/download")

        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-containment"]},
        ).json()
        _finish_fake_download(app, settings, payload=b"inside only\n")
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        outside = settings.data_root.parent / "outside.fake"
        outside.write_bytes(b"must never be served")
        with app.state.database.connect() as connection:
            connection.execute(
                """
                UPDATE artifacts SET path = '../outside.fake'
                WHERE asset_id = ? AND kind = 'original'
                """,
                (asset["asset_id"],),
            )
        escaped = client.get(asset["download_url"])

    assert missing_batch.status_code == 404
    assert missing_asset.status_code == 404
    assert invalid_asset.status_code == 404
    assert escaped.status_code == 409
    assert escaped.json() == {"detail": "成品文件不可用"}
    assert outside.read_bytes() == b"must never be served"


def test_asset_download_rejects_a_hard_linked_original(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-linked"]},
        ).json()
        _finish_fake_download(app, settings, payload=b"single link required\n")
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        with app.state.database.connect() as connection:
            row = connection.execute(
                """
                SELECT path FROM artifacts
                WHERE asset_id = ? AND kind = 'original'
                """,
                (asset["asset_id"],),
            ).fetchone()
        original = settings.data_root / row["path"]
        alias = original.with_name("hard-link-alias.fake")
        os.link(original, alias)

        rejected = client.get(asset["download_url"])

    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "成品文件不可用"}


def test_frontend_fetches_ready_assets_and_builds_links_without_inner_html(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        page = client.get("/")

    assert page.status_code == 200
    assert 'id="asset-links"' in page.text
    assert 'id="asset-list"' in page.text
    assert 'id="recent-batches"' in page.text
    assert "async function openBatch(batchId)" in page.text
    assert "async function loadRecentBatches()" in page.text
    assert "open.addEventListener('click', () => void openBatch(batch.id));" in page.text
    assert "async function loadReadyAssets(payload, generation)" in page.text
    assert "/api/v1/batches/${encodeURIComponent(payload.id)}/assets" in page.text
    assert "const link = document.createElement('a');" in page.text
    assert "link.href = asset.download_url;" in page.text
    assert "link.textContent = `下载成品" in page.text
    assert "innerHTML" not in page.text

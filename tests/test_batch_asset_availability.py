from __future__ import annotations

from local_http_client import download_client

from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.adapters.base import AdapterFailure
from video_download_control.api import create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.config import Settings
from video_download_control.domain import ErrorCode
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


PAYLOAD = b"offline batch asset availability regression\n"


def _worker(
    app, settings: Settings, *, adapter: ScriptedFakeAdapter | None = None
) -> Worker:
    return Worker(
        worker_id="batch-asset-availability-test",
        repository=WorkerRepository(app.state.database),
        adapter=adapter or ScriptedFakeAdapter(payload=PAYLOAD),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
    )


def _create_batch(client: TestClient, inputs: list[str]) -> dict:
    response = client.post("/api/v1/batches", json={"inputs": inputs})
    assert response.status_code == 201
    return response.json()


def test_duplicate_batch_can_download_existing_ready_asset_without_new_job(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with download_client(app) as client:
        original = _create_batch(
            client, ["https://www.youtube.com/watch?v=duplicate-ready-asset"]
        )
        worker = _worker(app, settings)
        result = worker.run_once()
        assert result is not None and result.status == "ready"
        original_before = client.get(f"/api/v1/batches/{original['id']}").json()
        original_assets = client.get(
            f"/api/v1/batches/{original['id']}/assets"
        ).json()
        assert len(original_assets) == 1

        duplicate = _create_batch(
            client, ["https://youtu.be/duplicate-ready-asset"]
        )
        duplicate_assets = client.get(
            f"/api/v1/batches/{duplicate['id']}/assets"
        )

        # Reuse is a read path, not a new download or a rewrite of either Batch.
        assert duplicate["status"] == "duplicate"
        assert duplicate["jobs"] == []
        assert duplicate["inputs"][0]["duplicate_of_input_record_id"] == (
            original["inputs"][0]["id"]
        )
        assert worker.run_once() is None
        assert client.get(f"/api/v1/batches/{original['id']}").json() == (
            original_before
        )
        assert client.get(f"/api/v1/batches/{duplicate['id']}").json() == duplicate
        assert duplicate_assets.status_code == 200
        assert duplicate_assets.json() == original_assets
        downloaded = client.get(duplicate_assets.json()[0]["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content == PAYLOAD


def test_active_batch_assets_api_returns_completed_media_without_promoting_batch(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with download_client(app) as client:
        batch = _create_batch(
            client,
            [
                "https://youtu.be/completed-before-batch",
                "https://www.bilibili.com/video/BV13x41117TL",
            ],
        )
        result = _worker(app, settings).run_once()
        assert result is not None and result.status == "ready"
        before = client.get(f"/api/v1/batches/{batch['id']}").json()
        assert before["status"] == "queued"
        assert sorted(job["status"] for job in before["jobs"]) == ["queued", "ready"]

        listed = client.get(f"/api/v1/batches/{batch['id']}/assets")
        assert listed.status_code == 200
        assets = listed.json()
        assert len(assets) == 1
        assert assets[0]["job_id"] == result.job_id
        downloaded = client.get(assets[0]["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content == PAYLOAD
        assert client.get(f"/api/v1/batches/{batch['id']}").json() == before


def test_duplicate_input_chain_returns_each_original_asset_once(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with download_client(app) as client:
        original = _create_batch(client, ["https://youtu.be/duplicate-chain"])
        worker = _worker(app, settings)
        result = worker.run_once()
        assert result is not None and result.status == "ready"
        original_assets = client.get(
            f"/api/v1/batches/{original['id']}/assets"
        ).json()
        assert len(original_assets) == 1
        middle = _create_batch(client, ["https://youtu.be/duplicate-chain"])
        latest = _create_batch(
            client,
            [
                "https://youtu.be/duplicate-chain",
                "https://www.youtube.com/watch?v=duplicate-chain",
            ],
        )
        assert latest["jobs"] == []
        assert latest["duplicate_count"] == 2
        assert latest["inputs"][1]["duplicate_of_input_record_id"] == (
            latest["inputs"][0]["id"]
        )
        # Model an existing two-level persisted reference, with the second
        # input adding another route to the same immutable original.
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE input_records SET duplicate_of_input_record_id = ? WHERE id = ?",
                (middle["inputs"][0]["id"], latest["inputs"][0]["id"]),
            )
        before = client.get(f"/api/v1/batches/{latest['id']}").json()
        listed = client.get(f"/api/v1/batches/{latest['id']}/assets")
        assert client.get(f"/api/v1/batches/{latest['id']}").json() == before
        assert worker.run_once() is None
        assert listed.status_code == 200
        assert listed.json() == original_assets


@pytest.mark.parametrize("owner_status", ["queued", "failed", "canceled"])
def test_duplicate_of_nonready_owner_has_no_asset_and_does_not_change_status(
    settings: Settings, owner_status: str,
) -> None:
    app = create_app(settings)
    with download_client(app) as client:
        original = _create_batch(client, ["https://youtu.be/nonready-owner"])
        duplicate = _create_batch(client, ["https://youtu.be/nonready-owner"])
        if owner_status == "canceled":
            response = client.post(
                f"/api/v1/jobs/{original['jobs'][0]['id']}/cancel"
            )
            assert response.status_code == 200
        elif owner_status == "failed":
            adapter = ScriptedFakeAdapter(
                probe_failures=[
                    AdapterFailure(ErrorCode.CONTENT_UNAVAILABLE, "offline unavailable")
                ],
            )
            result = _worker(app, settings, adapter=adapter).run_once()
            assert result is not None and result.status == "failed"
        owner_before = client.get(f"/api/v1/batches/{original['id']}").json()
        assert owner_before["jobs"][0]["status"] == owner_status

        listed = client.get(f"/api/v1/batches/{duplicate['id']}/assets")

        assert listed.status_code == 200
        assert listed.json() == []
        assert client.get(f"/api/v1/batches/{original['id']}").json() == owner_before
        assert client.get(f"/api/v1/batches/{duplicate['id']}").json() == duplicate


def test_assets_of_a_nonexistent_batch_remain_not_found(settings: Settings) -> None:
    with download_client(create_app(settings)) as client:
        response = client.get(f"/api/v1/batches/{uuid4()}/assets")
    assert response.status_code == 404
    assert response.json() == {"detail": "批次不存在"}


def test_live_duplicate_exposes_original_when_it_finishes_without_new_download(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with download_client(app) as client:
        original = _create_batch(client, ["https://youtu.be/live-owner-completes"])
        duplicate = _create_batch(client, ["https://youtu.be/live-owner-completes"])
        before = client.get(f"/api/v1/batches/{duplicate['id']}/assets")
        assert before.status_code == 200
        assert before.json() == []

        worker = _worker(app, settings)
        result = worker.run_once()
        assert result is not None and result.status == "ready"
        assert result.job_id == original["jobs"][0]["id"]
        assert worker.run_once() is None

        available = client.get(f"/api/v1/batches/{duplicate['id']}/assets")
        owner_assets = client.get(f"/api/v1/batches/{original['id']}/assets")
        assert available.status_code == owner_assets.status_code == 200
        assert len(available.json()) == 1
        assert available.json() == owner_assets.json()
        assert client.get(f"/api/v1/batches/{duplicate['id']}").json() == duplicate


def _limit_sqlite_work(app, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the cycle fixture even if a future SQL query uses UNION ALL."""

    original_connect = app.state.database.connect

    @contextmanager
    def bounded_connect():
        with original_connect() as connection:
            progress_calls = 0

            def stop_unbounded_query() -> int:
                nonlocal progress_calls
                progress_calls += 1
                return int(progress_calls > 1000)

            connection.set_progress_handler(stop_unbounded_query, 1000)
            try:
                yield connection
            finally:
                connection.set_progress_handler(None, 0)

    monkeypatch.setattr(app.state.database, "connect", bounded_connect)


@pytest.mark.parametrize(
    "invalid_edge", ["cycle", "missing_target", "nonduplicate_input", "wrong_source"]
)
def test_invalid_duplicate_reference_returns_no_asset_without_mutation_or_loop(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, invalid_edge: str,
) -> None:
    app = create_app(settings)
    with download_client(app) as client:
        original = _create_batch(client, ["https://youtu.be/reference-original"])
        worker = _worker(app, settings)
        result = worker.run_once()
        assert result is not None and result.status == "ready"
        duplicate = _create_batch(client, ["https://youtu.be/reference-original"])
        duplicate_input_id = duplicate["inputs"][0]["id"]
        target_id = original["inputs"][0]["id"]
        input_status = "duplicate"
        if invalid_edge == "cycle":
            target_id = duplicate_input_id
        elif invalid_edge == "missing_target":
            target_id = str(uuid4())
        elif invalid_edge == "nonduplicate_input":
            input_status = "failed"
        else:
            unrelated = _create_batch(client, ["https://youtu.be/unrelated-original"])
            result = worker.run_once()
            assert result is not None and result.status == "ready"
            target_id = unrelated["inputs"][0]["id"]
        # Deliberate database-damage fixtures are not normal application writes.
        with app.state.database.connect() as connection:
            if invalid_edge == "missing_target":
                connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """UPDATE input_records
                SET status = ?, duplicate_of_input_record_id = ? WHERE id = ?""",
                (input_status, target_id, duplicate_input_id),
            )
        before = client.get(f"/api/v1/batches/{duplicate['id']}").json()
        owner_before = client.get(f"/api/v1/batches/{original['id']}").json()
        _limit_sqlite_work(app, monkeypatch)

        response = client.get(f"/api/v1/batches/{duplicate['id']}/assets")

        assert response.status_code == 200
        assert response.json() == []
        assert client.get(f"/api/v1/batches/{duplicate['id']}").json() == before
        assert client.get(f"/api/v1/batches/{original['id']}").json() == owner_before


def test_duplicate_keeps_its_original_owner_not_a_later_ready_job_for_same_source(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with download_client(app) as client:
        original = _create_batch(client, ["https://youtu.be/owner-not-source"])
        duplicate = _create_batch(client, ["https://youtu.be/owner-not-source"])
        canceled = client.post(f"/api/v1/jobs/{original['jobs'][0]['id']}/cancel")
        assert canceled.status_code == 200
        replacement = _create_batch(client, ["https://youtu.be/owner-not-source"])
        assert len(replacement["jobs"]) == 1
        assert replacement["jobs"][0]["source_item_id"] == (
            original["jobs"][0]["source_item_id"]
        )
        result = _worker(app, settings).run_once()
        assert result is not None and result.status == "ready"
        replacement_assets = client.get(
            f"/api/v1/batches/{replacement['id']}/assets"
        ).json()
        assert len(replacement_assets) == 1

        response = client.get(f"/api/v1/batches/{duplicate['id']}/assets")

        assert response.status_code == 200
        assert response.json() == []
        assert client.get(f"/api/v1/batches/{duplicate['id']}").json() == duplicate
        owner = client.get(f"/api/v1/batches/{original['id']}").json()
        assert owner["status"] == "canceled"
        assert owner["jobs"][0]["status"] == "canceled"

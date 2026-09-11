from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.config import Settings


def test_batch_listing_is_bounded_recent_first_and_contains_no_urls(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    with TestClient(
        create_app(
            Settings(
                data_root=data_root,
                database_path=data_root / "control.sqlite3",
            )
        ), base_url="http://127.0.0.1"
    ) as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        first = client.post(
            "/api/v1/batches",
            json={"name": "first", "inputs": ["https://youtu.be/first"]},
        ).json()
        second = client.post(
            "/api/v1/batches",
            json={"name": "second", "inputs": ["https://youtu.be/second"]},
        ).json()
        response = client.get("/api/v1/batches", params={"limit": 1})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [second["id"]]
    assert first["id"] != second["id"]
    serialized = response.text.lower()
    assert "canonical_url" not in serialized
    assert "submitted_url" not in serialized
    assert "lease_token" not in serialized


def test_batch_listing_limit_is_validated(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    with TestClient(
        create_app(
            Settings(
                data_root=data_root,
                database_path=data_root / "control.sqlite3",
            )
        ), base_url="http://127.0.0.1"
    ) as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        assert client.get("/api/v1/batches", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/batches", params={"limit": 101}).status_code == 422

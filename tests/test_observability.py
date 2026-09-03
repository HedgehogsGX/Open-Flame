from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.graph import (
    XAttachmentProbeItem,
    XPostIdentity,
    build_x_attachment_discovery,
)

def test_metrics_are_aggregate_only_and_include_disk_and_queue(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    app = create_app(
        Settings(
            data_root=data_root,
            database_path=data_root / "control.sqlite3",
        )
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={
                "inputs": [
                    "https://www.youtube.com/watch?v=metric-sample",
                    "not a url",
                ]
            },
        )
        assert created.status_code == 201
        response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    metrics = response.json()
    assert metrics["queue_depth"] == 1
    assert metrics["active_jobs"] == 0
    assert metrics["disk_total_bytes"] >= metrics["disk_free_bytes"] > 0
    assert metrics["jobs"] == [
        {"platform": "youtube", "status": "queued", "count": 1}
    ]
    assert metrics["platform_outcomes"][0]["success_rate"] is None
    serialized = response.text.lower()
    assert "metric-sample" not in serialized
    assert "canonical_url" not in serialized
    assert "lease_token" not in serialized


def test_empty_metrics_have_null_percentiles(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    with TestClient(
        create_app(
            Settings(
                data_root=data_root,
                database_path=data_root / "control.sqlite3",
            )
        )
    ) as client:
        metrics = client.get("/api/v1/metrics").json()
    assert metrics["queue_depth"] == 0
    assert metrics["average_attempts"] == 0
    assert metrics["attempt_duration_p50_seconds"] is None
    assert metrics["attempt_duration_p95_seconds"] is None


def test_graph_metrics_count_discover_as_work_but_only_children_as_outcomes(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    app = create_app(
        Settings(
            data_root=data_root,
            database_path=data_root / "control.sqlite3",
            x_graph_v2_enabled=True,
        )
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://x.com/example/status/950001"]},
        ).json()
        parent = created["jobs"][0]
        claim_time = datetime.fromisoformat(created["created_at"]) + timedelta(
            seconds=1
        )
        worker_repository = app.state.worker_repository
        lease = worker_repository.claim_next(
            worker_id="metrics-graph-worker",
            adapter="metrics-graph-fake",
            adapter_version="1",
            now=claim_time,
            supports_exact_selector=True,
        )
        assert lease is not None
        items = tuple(
            XAttachmentProbeItem(
                stable_key=f"stable-{index}",
                selector_key=f"private-selector-{index}",
                expected_media_key=f"private-target-{index}",
                media_kind="video",
            )
            for index in range(3)
        )
        discovery = build_x_attachment_discovery(
            parent=XPostIdentity("950001"),
            parent_canonical_url="https://x.com/i/status/950001",
            probe_items=items,
        )
        worker_repository.commit_discovery(
            lease,
            probe_items=items,
            discovery_snapshot_hash=discovery.snapshot_hash,
            sanitized_source={},
            now=claim_time + timedelta(seconds=1),
        )
        with app.state.database.connect() as connection:
            children = connection.execute(
                """
                SELECT id FROM download_jobs
                WHERE input_record_id = ? AND job_kind = 'download'
                ORDER BY id
                """,
                (parent["input_record_id"],),
            ).fetchall()
            connection.execute(
                "UPDATE download_jobs SET status = 'ready' WHERE id = ?",
                (children[0]["id"],),
            )
            connection.execute(
                "UPDATE download_jobs SET status = 'failed' WHERE id = ?",
                (children[1]["id"],),
            )

        response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    metrics = response.json()
    assert metrics["queue_depth"] == 1
    assert metrics["jobs"] == [
        {"platform": "x", "status": "failed", "count": 1},
        {"platform": "x", "status": "queued", "count": 1},
        {"platform": "x", "status": "ready", "count": 2},
    ]
    assert metrics["platform_outcomes"] == [
        {
            "platform": "x",
            "ready": 1,
            "failed": 1,
            "canceled": 0,
            "success_rate": 0.5,
        }
    ]
    serialized = response.text
    for internal_value in (
        "private-selector-0",
        "private-selector-1",
        "private-selector-2",
        "private-target-0",
        "private-target-1",
        "private-target-2",
    ):
        assert internal_value not in serialized

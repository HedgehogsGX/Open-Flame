from __future__ import annotations

import json
from datetime import UTC, datetime

from video_download_control.adapters import ScriptedGraphFakeAdapter
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.domain import ErrorCode
from video_download_control.graph import XAttachmentProbeItem
from video_download_control.service import BatchService
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


NOW = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)


class _SiblingWritingGraphAdapter(ScriptedGraphFakeAdapter):
    def download(self, request, context, progress, cancellation):
        result = super().download(request, context, progress, cancellation)
        (request.output_dir / "unreported-sibling.fake").write_bytes(b"sibling")
        return result


def _graph_service(repository, settings) -> BatchService:
    return BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version="graph-worker-e2e-v2",
        x_graph_v2_enabled=True,
    )


def _graph_worker(settings, database, adapter) -> Worker:
    return Worker(
        worker_id="graph-e2e-worker",
        repository=WorkerRepository(database),
        adapter=adapter,
        asset_store=AssetStore(settings.data_root),
        verifier=NonEmptyTestVerifier(),
        clock=lambda: NOW,
    )


def test_graph_worker_probes_parent_once_then_downloads_each_child_exactly_once(
    repository,
    settings,
    database,
) -> None:
    internal_values = {
        "stable-key-alpha-private",
        "stable-key-beta-private",
        "selector-alpha-private",
        "selector-beta-private",
        "expected-alpha-private",
        "expected-beta-private",
    }
    adapter = ScriptedGraphFakeAdapter(
        (
            XAttachmentProbeItem(
                stable_key="stable-key-alpha-private",
                selector_key="selector-alpha-private",
                expected_media_key="expected-alpha-private",
                media_kind="video",
            ),
            XAttachmentProbeItem(
                stable_key="stable-key-beta-private",
                selector_key="selector-beta-private",
                expected_media_key="expected-beta-private",
                media_kind="image",
            ),
        )
    )
    service = _graph_service(repository, settings)
    batch = service.create_batch(
        name="graph worker e2e",
        raw_inputs=["https://x.com/example/status/930001"],
    )
    parent_job_id = batch["jobs"][0]["id"]
    worker = _graph_worker(settings, database, adapter)

    parent_result = worker.run_once()

    assert parent_result is not None
    assert parent_result.job_id == parent_job_id
    assert parent_result.status == "ready"
    assert adapter.probe_call_count == 1
    assert adapter.download_call_count == 0
    after_discovery = service.get_batch(batch["id"])
    assert after_discovery is not None
    assert after_discovery["inputs"][0]["status"] == "queued"
    assert after_discovery["inputs"][0]["expected_item_count"] == 2
    assert len(after_discovery["jobs"]) == 3

    first_child = worker.run_once()
    second_child = worker.run_once()

    assert first_child is not None and first_child.status == "ready"
    assert second_child is not None and second_child.status == "ready"
    assert first_child.job_id != second_child.job_id
    assert worker.run_once() is None
    assert adapter.probe_call_count == 1
    assert adapter.download_call_count == 2

    refreshed = service.get_batch(batch["id"])
    assert refreshed is not None
    assert refreshed["status"] == "ready"
    assert refreshed["ready_count"] == 1
    assert refreshed["partial_success_count"] == 0
    assert refreshed["inputs"][0]["status"] == "ready"
    assert {job["status"] for job in refreshed["jobs"]} == {"ready"}
    assert {
        job["canonical_url"]
        for job in refreshed["jobs"]
        if job["job_kind"] == "download"
    } == {"https://x.com/i/status/930001"}

    public_payload = json.dumps(refreshed, ensure_ascii=False, sort_keys=True)
    assert "#vdc-media=" not in public_payload
    for internal_value in internal_values:
        assert internal_value not in public_payload

    with database.connect() as connection:
        counts = {
            "discoveries": connection.execute(
                "SELECT COUNT(*) FROM source_discoveries"
            ).fetchone()[0],
            "relations": connection.execute(
                "SELECT COUNT(*) FROM source_relations"
            ).fetchone()[0],
            "attempts": connection.execute(
                "SELECT COUNT(*) FROM job_attempts"
            ).fetchone()[0],
            "assets": connection.execute(
                "SELECT COUNT(*) FROM media_assets"
            ).fetchone()[0],
            "parent_assets": connection.execute(
                "SELECT COUNT(*) FROM job_assets WHERE job_id = ?",
                (parent_job_id,),
            ).fetchone()[0],
        }
        child_original_counts = [
            row["original_count"]
            for row in connection.execute(
                """
                SELECT j.id, COUNT(ja.asset_id) AS original_count
                FROM download_jobs AS j
                LEFT JOIN job_assets AS ja
                  ON ja.job_id = j.id AND ja.role = 'original'
                WHERE j.input_record_id = ? AND j.job_kind = 'download'
                GROUP BY j.id ORDER BY j.id
                """,
                (batch["inputs"][0]["id"],),
            ).fetchall()
        ]
    assert counts == {
        "discoveries": 1,
        "relations": 2,
        "attempts": 3,
        "assets": 2,
        "parent_assets": 0,
    }
    assert child_original_counts == [1, 1]


def test_graph_worker_rejects_an_unreported_sibling_output(
    repository,
    settings,
    database,
) -> None:
    adapter = _SiblingWritingGraphAdapter(
        (
            XAttachmentProbeItem(
                stable_key="one-stable",
                selector_key="one-selector",
                expected_media_key="one-expected",
                media_kind="video",
            ),
        )
    )
    service = _graph_service(repository, settings)
    batch = service.create_batch(
        name="reject sibling",
        raw_inputs=["https://x.com/example/status/930002"],
    )
    worker = _graph_worker(settings, database, adapter)
    assert worker.run_once().status == "ready"

    result = worker.run_once()

    assert result is not None
    assert result.status == "queued"
    assert result.error_code == ErrorCode.VALIDATION_FAILED
    refreshed = service.get_batch(batch["id"])
    assert refreshed is not None
    child = next(
        job for job in refreshed["jobs"] if job["job_kind"] == "download"
    )
    assert child["status"] == "queued"
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_assets"
        ).fetchone()[0] == 0
    temporary_root = settings.data_root / "temporary"
    assert not temporary_root.exists() or not any(temporary_root.rglob("*"))

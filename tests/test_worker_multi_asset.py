from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from video_download_control.adapters.base import (
    DownloadResult,
    ProbeItem,
    ProbeResult,
    ProducedFile,
)
from video_download_control.adapters.fake import ScriptedFakeAdapter
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


class MultiAssetFakeAdapter(ScriptedFakeAdapter):
    def __init__(self, *, omit_second: bool = False) -> None:
        super().__init__()
        self.omit_second = omit_second

    def probe(self, request, context):
        del context
        snapshot = hashlib.sha256(request.canonical_url.encode()).hexdigest()
        items = tuple(
            ProbeItem(
                canonical_url=request.canonical_url,
                media_key=f"attachment-{ordinal}",
                media_kind="video",
            )
            for ordinal in range(2)
        )
        return ProbeResult(
            sanitized_source={
                "platform": request.platform.value,
                "source_type": request.source_type.value,
                "canonical_url": request.canonical_url,
            },
            items=items,
            expected_item_count=2,
            discovery_snapshot_hash=snapshot,
        )

    def download(self, request, context, progress, cancellation):
        del context, progress
        assert request.expected_media_keys == ("attachment-0", "attachment-1")
        if cancellation.is_cancelled():
            return DownloadResult(files=())
        request.output_dir.mkdir(parents=True, exist_ok=True)
        files = []
        count = 1 if self.omit_second else 2
        for ordinal in range(count):
            path = request.output_dir / f"attachment-{ordinal}.fake"
            path.write_bytes(f"payload-{ordinal}".encode())
            files.append(
                ProducedFile(
                    path=path,
                    media_key=f"attachment-{ordinal}",
                    media_kind="video",
                    role="original",
                    ordinal=ordinal,
                )
            )
        return DownloadResult(files=tuple(files))


def build_worker(settings, database, adapter) -> Worker:
    return Worker(
        worker_id="multi-asset-worker",
        repository=WorkerRepository(database),
        adapter=adapter,
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
        clock=lambda: datetime(2026, 9, 3, 4, 0, tzinfo=UTC),
    )


def test_worker_commits_every_attachment_before_marking_job_ready(
    service, settings, database
) -> None:
    batch = service.create_batch(
        name="two attachments",
        raw_inputs=["https://x.com/example/status/9911223344"],
    )
    result = build_worker(settings, database, MultiAssetFakeAdapter()).run_once()

    assert result and result.status == "ready"
    with database.connect() as connection:
        assets = connection.execute(
            "SELECT media_key, status FROM media_assets ORDER BY media_key"
        ).fetchall()
        links = connection.execute(
            "SELECT ordinal FROM job_assets ORDER BY ordinal"
        ).fetchall()
        artifacts = connection.execute(
            "SELECT kind, COUNT(*) AS count FROM artifacts GROUP BY kind"
        ).fetchall()
        intents = connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0]

    assert [(row["media_key"], row["status"]) for row in assets] == [
        ("attachment-0", "ready"),
        ("attachment-1", "ready"),
    ]
    assert [row["ordinal"] for row in links] == [0, 1]
    assert {row["kind"]: row["count"] for row in artifacts} == {
        "manifest": 2,
        "original": 2,
    }
    assert intents == 0
    refreshed = service.get_batch(batch["id"])
    assert refreshed["inputs"][0]["expected_item_count"] == 2
    assert refreshed["status"] == "ready"


def test_worker_never_marks_partial_attachment_set_ready(
    service, settings, database
) -> None:
    batch = service.create_batch(
        name="missing attachment",
        raw_inputs=["https://x.com/example/status/9911223355"],
    )
    worker = build_worker(
        settings,
        database,
        MultiAssetFakeAdapter(omit_second=True),
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first and first.status == "queued"
    assert second and second.status == "failed"
    assert second.error_code == "validation_failed"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 0
    refreshed = service.get_batch(batch["id"])
    assert refreshed["status"] == "failed"
    assert not list((settings.data_root / "assets" / ".staging").glob("*"))

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from video_download_control.adapters.base import (
    DownloadResult,
    ProbeItem,
    ProbeResult,
    ProducedFile,
)
from video_download_control.adapters.fake import ScriptedFakeAdapter
from video_download_control.assets import (
    AssetStore,
    AssetValidationError,
    AuxiliaryFile,
    NonEmptyTestVerifier,
)
from video_download_control.domain import JobStatus
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


NOW = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)


@dataclass(frozen=True)
class SidecarSpec:
    media_key: str
    kind: str
    ordinal: int
    filename: str
    payload: bytes


class AuxiliaryFakeAdapter(ScriptedFakeAdapter):
    def __init__(
        self,
        *,
        media_keys: tuple[str, ...] = ("media-0",),
        sidecars: tuple[SidecarSpec, ...] = (),
    ) -> None:
        super().__init__()
        self.media_keys = media_keys
        self.sidecars = sidecars

    def probe(self, request, context):
        del context
        return ProbeResult(
            sanitized_source={
                "platform": request.platform.value,
                "source_type": request.source_type.value,
                "canonical_url": request.canonical_url,
            },
            items=tuple(
                ProbeItem(
                    canonical_url=request.canonical_url,
                    media_key=media_key,
                    media_kind="video",
                )
                for media_key in self.media_keys
            ),
            expected_item_count=len(self.media_keys),
            discovery_snapshot_hash=hashlib.sha256(
                request.canonical_url.encode()
            ).hexdigest(),
        )

    def download(self, request, context, progress, cancellation):
        del context, progress
        assert request.expected_media_keys == self.media_keys
        if cancellation.is_cancelled():
            return DownloadResult(files=())
        request.output_dir.mkdir(parents=True, exist_ok=True)
        originals = []
        for ordinal, media_key in enumerate(self.media_keys):
            path = request.output_dir / f"{media_key}.mkv"
            path.write_bytes(f"original-{media_key}".encode())
            originals.append(
                ProducedFile(
                    path=path,
                    media_key=media_key,
                    media_kind="video",
                    role="original",
                    ordinal=ordinal,
                )
            )
        thumbnails = []
        captions = []
        for spec in self.sidecars:
            path = request.output_dir / spec.filename
            path.write_bytes(spec.payload)
            produced = ProducedFile(
                path=path,
                media_key=spec.media_key,
                media_kind="image" if spec.kind == "thumbnail" else "text",
                role=spec.kind,
                ordinal=spec.ordinal,
            )
            (thumbnails if spec.kind == "thumbnail" else captions).append(produced)
        return DownloadResult(
            files=tuple(originals),
            thumbnails=tuple(thumbnails),
            captions=tuple(captions),
        )


class RecordingVerifier(NonEmptyTestVerifier):
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def verify(self, path: Path, media_kind: str):
        self.paths.append(path)
        return super().verify(path, media_kind)


def build_worker(settings, database, adapter, *, verifier=None) -> Worker:
    return Worker(
        worker_id="auxiliary-worker",
        repository=WorkerRepository(database),
        adapter=adapter,
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=verifier or NonEmptyTestVerifier(),
        clock=lambda: NOW,
    )


def create_job(service, suffix: str) -> None:
    service.create_batch(
        name="auxiliary artifacts",
        raw_inputs=[f"https://www.youtube.com/watch?v={suffix}"],
    )


def assert_validation_failure(worker: Worker, database) -> None:
    first = worker.run_once()
    second = worker.run_once()
    assert first and first.status == "queued"
    assert second and second.status == "failed"
    assert second.error_code == "validation_failed"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM captions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 0


def test_worker_commits_thumbnail_and_caption_with_manifest_and_caption_semantics(
    service, settings, database
) -> None:
    create_job(service, "aux-success")
    verifier = RecordingVerifier()
    adapter = AuxiliaryFakeAdapter(
        sidecars=(
            SidecarSpec(
                "media-0", "thumbnail", 0, "media-0.thumbnail.webp", b"webp"
            ),
            SidecarSpec(
                "media-0",
                "caption",
                0,
                "media-0.caption.en-US.vtt",
                b"WEBVTT\n",
            ),
        )
    )

    result = build_worker(
        settings, database, adapter, verifier=verifier
    ).run_once()

    assert result and result.status == "ready"
    assert len(verifier.paths) == 1
    assert "original" in verifier.paths[0].parts
    with database.connect() as connection:
        asset_id = connection.execute("SELECT id FROM media_assets").fetchone()[0]
        artifacts = connection.execute(
            """
            SELECT id, kind, path, mime_type, sha256, parent_artifact_id
            FROM artifacts ORDER BY kind
            """
        ).fetchall()
        caption = connection.execute(
            """
            SELECT c.language, c.origin, c.revision, c.status, a.kind
            FROM captions AS c JOIN artifacts AS a ON a.id = c.artifact_id
            """
        ).fetchone()
    by_kind = {row["kind"]: row for row in artifacts}
    assert set(by_kind) == {"original", "manifest", "thumbnail", "caption"}
    assert by_kind["thumbnail"]["mime_type"] == "image/webp"
    assert by_kind["caption"]["mime_type"] == "text/vtt"
    assert by_kind["thumbnail"]["parent_artifact_id"] == by_kind["original"]["id"]
    assert by_kind["caption"]["parent_artifact_id"] == by_kind["original"]["id"]
    assert dict(caption) == {
        "language": "en-US",
        "origin": "platform",
        "revision": 1,
        "status": "ready",
        "kind": "caption",
    }

    asset_root = settings.data_root / "assets" / asset_id
    manifest = json.loads(
        (asset_root / "metadata" / "manifest.json").read_text("utf-8")
    )
    assert manifest["schema_version"] == 1
    assert manifest["artifacts"] == [
        {
            "kind": "thumbnail",
            "language": None,
            "mime_type": "image/webp",
            "ordinal": 0,
            "path": "thumbnails/thumbnail-0000.webp",
            "sha256": by_kind["thumbnail"]["sha256"],
            "size_bytes": len(b"webp"),
        },
        {
            "kind": "caption",
            "language": "en-US",
            "mime_type": "text/vtt",
            "ordinal": 0,
            "path": "captions/caption-0000-en-US.vtt",
            "sha256": by_kind["caption"]["sha256"],
            "size_bytes": len(b"WEBVTT\n"),
        },
    ]
    for kind in ("thumbnail", "caption"):
        stored = settings.data_root / by_kind[kind]["path"]
        assert stored.is_file()
        assert AssetStore.sha256(stored) == by_kind[kind]["sha256"]


def test_worker_assigns_sidecars_to_the_matching_original_in_multi_asset_result(
    service, settings, database
) -> None:
    create_job(service, "aux-multiple")
    adapter = AuxiliaryFakeAdapter(
        media_keys=("attachment-0", "attachment-1"),
        sidecars=(
            SidecarSpec(
                "attachment-0",
                "thumbnail",
                0,
                "attachment-0.thumbnail.jpg",
                b"thumb-0",
            ),
            SidecarSpec(
                "attachment-1",
                "thumbnail",
                1,
                "attachment-1.thumbnail.png",
                b"thumb-1",
            ),
            SidecarSpec(
                "attachment-0",
                "caption",
                0,
                "attachment-0.caption.en.vtt",
                b"caption-0",
            ),
            SidecarSpec(
                "attachment-1",
                "caption",
                1,
                "attachment-1.caption.zh-Hans.srt",
                b"caption-1",
            ),
        ),
    )

    result = build_worker(settings, database, adapter).run_once()

    assert result and result.status == "ready"
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT m.media_key, a.kind, a.path
            FROM media_assets AS m
            JOIN artifacts AS a ON a.asset_id = m.id
            WHERE a.kind IN ('thumbnail', 'caption')
            ORDER BY m.media_key, a.kind
            """
        ).fetchall()
        languages = connection.execute(
            """
            SELECT m.media_key, c.language
            FROM captions AS c
            JOIN artifacts AS a ON a.id = c.artifact_id
            JOIN media_assets AS m ON m.id = a.asset_id
            ORDER BY m.media_key
            """
        ).fetchall()
    assert [(row["media_key"], row["kind"]) for row in rows] == [
        ("attachment-0", "caption"),
        ("attachment-0", "thumbnail"),
        ("attachment-1", "caption"),
        ("attachment-1", "thumbnail"),
    ]
    assert [(row["media_key"], row["language"]) for row in languages] == [
        ("attachment-0", "en"),
        ("attachment-1", "zh-Hans"),
    ]
    for row in rows:
        payload = (settings.data_root / row["path"]).read_bytes()
        expected_suffix = row["media_key"].split("-")[-1].encode()
        assert payload.endswith(expected_suffix)


@pytest.mark.parametrize(
    "sidecars",
    [
        (
            SidecarSpec(
                "missing-owner",
                "thumbnail",
                0,
                "missing.thumbnail.jpg",
                b"orphan",
            ),
        ),
        (
            SidecarSpec(
                "media-0", "thumbnail", 0, "first.thumbnail.jpg", b"one"
            ),
            SidecarSpec(
                "media-0", "thumbnail", 0, "second.thumbnail.jpg", b"two"
            ),
        ),
        (
            SidecarSpec(
                "media-0",
                "caption",
                0,
                "media-0.caption.en.secret.vtt",
                b"unsafe-language",
            ),
        ),
        (
            SidecarSpec(
                "media-0",
                "thumbnail",
                0,
                "media-0.thumbnail.svg",
                b"unsafe-extension",
            ),
        ),
        tuple(
            SidecarSpec(
                "media-0",
                "thumbnail",
                ordinal,
                f"media-0-{ordinal}.thumbnail.jpg",
                f"thumb-{ordinal}".encode(),
            )
            for ordinal in range(9)
        ),
    ],
    ids=[
        "orphan-owner",
        "duplicate-ordinal",
        "unsafe-language",
        "unsafe-extension",
        "per-asset-count-limit",
    ],
)
def test_worker_rejects_invalid_auxiliary_sets_without_ready_or_orphans(
    service, settings, database, sidecars
) -> None:
    create_job(service, f"aux-invalid-{len(sidecars)}-{sidecars[0].filename}")
    worker = build_worker(
        settings,
        database,
        AuxiliaryFakeAdapter(sidecars=sidecars),
    )

    assert_validation_failure(worker, database)

    assets_root = settings.data_root / "assets"
    published = [
        path
        for path in assets_root.glob("*")
        if path.name != ".staging"
    ]
    assert published == []
    assert not list((assets_root / ".staging").glob("*"))


def test_worker_rejects_one_auxiliary_path_claimed_twice(
    service, settings, database
) -> None:
    create_job(service, "aux-duplicate-path")

    class DuplicatePathAdapter(AuxiliaryFakeAdapter):
        def download(self, request, context, progress, cancellation):
            base = super().download(request, context, progress, cancellation)
            path = request.output_dir / "shared.thumbnail.jpg"
            path.write_bytes(b"shared")
            first = ProducedFile(path, "media-0", "image", "thumbnail", 0)
            second = ProducedFile(path, "media-0", "image", "thumbnail", 1)
            return DownloadResult(files=base.files, thumbnails=(first, second))

    assert_validation_failure(
        build_worker(settings, database, DuplicatePathAdapter()), database
    )


def test_worker_rejects_missing_auxiliary_file_without_partial_asset(
    service, settings, database
) -> None:
    create_job(service, "aux-missing-file")

    class MissingSidecarAdapter(AuxiliaryFakeAdapter):
        def download(self, request, context, progress, cancellation):
            base = super().download(request, context, progress, cancellation)
            missing = request.output_dir / "missing.caption.en.vtt"
            return DownloadResult(
                files=base.files,
                captions=(
                    ProducedFile(missing, "media-0", "text", "caption", 0),
                ),
            )

    assert_validation_failure(
        build_worker(settings, database, MissingSidecarAdapter()), database
    )


def prepare_staged_auxiliary_asset(service, settings, database, suffix: str):
    create_job(service, suffix)
    repository = WorkerRepository(database)
    store = AssetStore(settings.data_root, min_free_bytes=0)
    lease = repository.claim_next(
        worker_id="aux-intent-worker",
        adapter="fake",
        adapter_version="1",
        now=NOW,
        remove_pending_asset=store.remove_pending_asset,
    )
    assert lease is not None
    repository.transition(lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW)
    repository.transition(lease, status=JobStatus.VERIFYING, progress=0.9, now=NOW)
    attempt = store.prepare_attempt(lease.job_id, lease.attempt_id)
    original = attempt.output / "media-0.mkv"
    thumbnail = attempt.output / "media-0.thumbnail.webp"
    caption = attempt.output / "media-0.caption.en.vtt"
    original.write_bytes(b"original")
    thumbnail.write_bytes(b"thumbnail")
    caption.write_bytes(b"WEBVTT\n")
    staged = store.stage_file(
        attempt=attempt,
        produced_path=original,
        media_key="media-0",
        media_kind="video",
        role="original",
        ordinal=0,
        sanitized_source={"platform": "youtube"},
        producer={"adapter": "fake", "adapter_version": "1"},
        verifier=NonEmptyTestVerifier(),
        job_id=lease.job_id,
        attempt_id=lease.attempt_id,
        source_item_id=lease.source_item_id,
        auxiliary_files=(
            AssetStore.describe_auxiliary_file(
                path=thumbnail, kind="thumbnail", ordinal=0
            ),
            AssetStore.describe_auxiliary_file(path=caption, kind="caption", ordinal=0),
        ),
    )
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    return repository, store, lease, staged


def test_caption_sql_rollback_keeps_complete_published_directory_for_recovery(
    service, settings, database
) -> None:
    repository, store, lease, staged = prepare_staged_auxiliary_asset(
        service, settings, database, "aux-caption-sql-rollback"
    )
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_caption_insert
            BEFORE INSERT ON captions
            BEGIN
                SELECT RAISE(ABORT, 'simulated caption SQL failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="caption SQL failure"):
        repository.finalize_asset_commit_intent(
            lease,
            asset=staged,
            publish_staged=store.publish_staged,
            remove_pending_asset=store.remove_pending_asset,
            now=NOW,
        )

    final = store.assets_root / staged.asset_id
    assert (final / "original").is_dir()
    assert (final / "thumbnails").is_dir()
    assert (final / "captions").is_dir()
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM captions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 1

    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=60),
        remove_pending_asset=store.remove_pending_asset,
    ) == 1
    assert not final.exists()


def test_crash_after_publishing_auxiliary_asset_is_recovered_as_one_unit(
    service, settings, database
) -> None:
    repository, store, lease, staged = prepare_staged_auxiliary_asset(
        service, settings, database, "aux-crash-recovery"
    )
    committed = store.publish_staged(staged)
    final = store.assets_root / committed.asset_id
    assert (final / "thumbnails" / "thumbnail-0000.webp").is_file()
    assert (final / "captions" / "caption-0000-en.vtt").is_file()

    recovered = repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=60),
        remove_pending_asset=store.remove_pending_asset,
    )

    assert recovered == 1
    assert not final.exists()
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM captions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_commit_intents"
        ).fetchone()[0] == 0


def test_asset_store_rejects_untrusted_auxiliary_metadata_and_escape(
    tmp_path: Path,
) -> None:
    store = AssetStore(tmp_path / "data", min_free_bytes=0)
    job_id = "11111111-1111-1111-1111-111111111111"
    attempt_id = "22222222-2222-2222-2222-222222222222"
    attempt = store.prepare_attempt(job_id, attempt_id)
    original = attempt.output / "source.mkv"
    original.write_bytes(b"original")
    outside = tmp_path / "outside.caption.en.vtt"
    outside.write_bytes(b"outside")

    with pytest.raises(AssetValidationError, match="escapes"):
        store.stage_file(
            attempt=attempt,
            produced_path=original,
            media_key="media-0",
            media_kind="video",
            role="original",
            ordinal=0,
            sanitized_source={},
            producer={},
            verifier=NonEmptyTestVerifier(),
            job_id=job_id,
            attempt_id=attempt_id,
            source_item_id="33333333-3333-3333-3333-333333333333",
            auxiliary_files=(
                AuxiliaryFile(
                    path=outside,
                    kind="caption",
                    ordinal=0,
                    mime_type="text/vtt",
                    language="en",
                ),
            ),
        )

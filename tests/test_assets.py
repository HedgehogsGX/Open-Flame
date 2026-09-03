from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from video_download_control.assets import (
    AssetStorageError,
    AssetStore,
    AssetValidationError,
    NonEmptyTestVerifier,
)


def test_asset_store_hashes_filters_and_atomically_commits(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "data")
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    attempt = store.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "clip.fake"
    produced.write_bytes(b"controlled offline bytes")

    committed = store.commit_file(
        attempt=attempt,
        produced_path=produced,
        media_key="media-1",
        media_kind="video",
        role="original",
        ordinal=0,
        sanitized_source={
            "platform": "youtube",
            "canonical_url": "https://www.youtube.com/watch?v=test",
            "title": "safe",
            "authorization": "secret",
            "http_headers": {"Cookie": "secret"},
        },
        producer={"adapter": "fake", "adapter_version": "1"},
        verifier=NonEmptyTestVerifier(),
        job_id=job_id,
        attempt_id=attempt_id,
        source_item_id=str(uuid4()),
    )

    asset_root = store.data_root / "assets" / committed.asset_id
    original = store.data_root / committed.relative_original_path
    manifest_path = store.data_root / committed.relative_manifest_path
    source = json.loads((asset_root / "metadata" / "source.json").read_text("utf-8"))
    manifest = json.loads(manifest_path.read_text("utf-8"))

    assert original.read_bytes() == b"controlled offline bytes"
    assert committed.sha256 == store.sha256(original)
    assert committed.manifest_sha256 == store.sha256(manifest_path)
    assert source == {
        "canonical_url": "https://www.youtube.com/watch?v=test",
        "platform": "youtube",
        "title": "safe",
    }
    assert "authorization" not in json.dumps(source).lower()
    assert "cookie" not in json.dumps(source).lower()
    assert manifest["original"]["sha256"] == committed.sha256
    assert manifest["created_at"].endswith("+00:00")
    assert not any(store.staging_root.iterdir())

    store.cleanup_attempt(attempt)
    assert not attempt.root.exists()
    store.rollback_committed_asset(committed)
    assert not asset_root.exists()


def test_asset_store_rejects_escape_and_empty_output(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "data")
    attempt = store.prepare_attempt(str(uuid4()), str(uuid4()))
    outside = tmp_path / "outside.fake"
    outside.write_bytes(b"outside")
    empty = attempt.output / "empty.fake"
    empty.touch()

    common = {
        "attempt": attempt,
        "media_key": "media-1",
        "media_kind": "video",
        "role": "original",
        "ordinal": 0,
        "sanitized_source": {},
        "producer": {},
        "verifier": NonEmptyTestVerifier(),
        "job_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "source_item_id": str(uuid4()),
    }
    with pytest.raises(AssetValidationError, match="escapes"):
        store.commit_file(produced_path=outside, **common)
    with pytest.raises(AssetValidationError, match="empty"):
        store.commit_file(produced_path=empty, **common)


def test_asset_store_verifies_the_managed_staged_copy(tmp_path: Path) -> None:
    class RecordingVerifier(NonEmptyTestVerifier):
        verified_path: Path | None = None

        def verify(self, path: Path, media_kind: str):
            self.verified_path = path
            return super().verify(path, media_kind)

    store = AssetStore(tmp_path / "data", min_free_bytes=0)
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    attempt = store.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "changing.fake"
    produced.write_bytes(b"changing")
    verifier = RecordingVerifier()

    staged = store.stage_file(
        attempt=attempt,
        produced_path=produced,
        media_key="media-1",
        media_kind="video",
        role="original",
        ordinal=0,
        sanitized_source={},
        producer={},
        verifier=verifier,
        job_id=job_id,
        attempt_id=attempt_id,
        source_item_id=str(uuid4()),
    )

    assert verifier.verified_path is not None
    assert verifier.verified_path != produced
    assert verifier.verified_path.is_relative_to(store.staging_root)
    assert verifier.verified_path.read_bytes() == produced.read_bytes()
    store.remove_pending_asset(staged.asset_id, attempt_id)


def test_asset_store_classifies_source_read_failure_during_copy_as_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(tmp_path / "data", min_free_bytes=0)
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    attempt = store.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "unreadable.fake"
    produced.write_bytes(b"readable source")
    real_open = Path.open
    source_reads = 0

    def fail_source_read(path: Path, mode: str = "r", *args, **kwargs):
        nonlocal source_reads
        if path == produced and mode == "rb":
            source_reads += 1
            raise PermissionError("simulated source read denial")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_source_read)

    with pytest.raises(AssetValidationError, match="missing or unreadable"):
        store.stage_file(
            attempt=attempt,
            produced_path=produced,
            media_key="media-1",
            media_kind="video",
            role="original",
            ordinal=0,
            sanitized_source={},
            producer={},
            verifier=NonEmptyTestVerifier(),
            job_id=job_id,
            attempt_id=attempt_id,
            source_item_id=str(uuid4()),
        )
    assert source_reads == 1


def test_asset_store_rejects_source_path_swap_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(tmp_path / "data", min_free_bytes=0)
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    attempt = store.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "swapped.fake"
    produced.write_bytes(b"original")
    original_copy = store._copy_and_sync

    def swap_then_copy(
        source: Path,
        target: Path,
        *,
        expected_size: int,
        max_size: int,
        expected_device: int,
        expected_inode: int,
    ) -> None:
        source.unlink()
        source.write_bytes(b"replaced")
        original_copy(
            source,
            target,
            expected_size=expected_size,
            max_size=max_size,
            expected_device=expected_device,
            expected_inode=expected_inode,
        )

    monkeypatch.setattr(store, "_copy_and_sync", swap_then_copy)

    with pytest.raises(AssetValidationError, match="changed before staging"):
        store.stage_file(
            attempt=attempt,
            produced_path=produced,
            media_key="media-1",
            media_kind="video",
            role="original",
            ordinal=0,
            sanitized_source={},
            producer={},
            verifier=NonEmptyTestVerifier(),
            job_id=job_id,
            attempt_id=attempt_id,
            source_item_id=str(uuid4()),
        )
    assert not any(store.staging_root.iterdir())


def test_attempt_paths_reject_non_identifier_components(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "data")
    with pytest.raises(AssetValidationError, match="unsafe identifier"):
        store.prepare_attempt("../escape", str(uuid4()))


def test_asset_store_enforces_size_and_free_space_limits(tmp_path: Path) -> None:
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    size_limited = AssetStore(
        tmp_path / "size-limited", max_file_bytes=4, min_free_bytes=0
    )
    attempt = size_limited.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "too-large.fake"
    produced.write_bytes(b"12345")

    common = {
        "produced_path": produced,
        "media_key": "media-1",
        "media_kind": "video",
        "role": "original",
        "ordinal": 0,
        "sanitized_source": {},
        "producer": {},
        "verifier": NonEmptyTestVerifier(),
        "job_id": job_id,
        "attempt_id": attempt_id,
        "source_item_id": str(uuid4()),
    }
    with pytest.raises(AssetValidationError, match="size limit"):
        size_limited.commit_file(attempt=attempt, **common)

    reserve_limited = AssetStore(
        tmp_path / "reserve-limited",
        max_file_bytes=1024,
        min_free_bytes=10**30,
    )
    reserve_attempt = reserve_limited.prepare_attempt(str(uuid4()), str(uuid4()))
    reserve_file = reserve_attempt.output / "small.fake"
    reserve_file.write_bytes(b"small")
    with pytest.raises(AssetStorageError, match="free-space reserve"):
        reserve_limited.commit_file(
            attempt=reserve_attempt,
            **{**common, "produced_path": reserve_file},
        )


def test_asset_store_omits_nested_allowlisted_values_and_cleans_failed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(tmp_path / "data", min_free_bytes=0)
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    attempt = store.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "clip.fake"
    produced.write_bytes(b"bytes")

    original_write = store._write_json_sync
    writes = 0

    def fail_on_manifest(path: Path, value: dict[str, object]) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated disk failure")
        original_write(path, value)

    monkeypatch.setattr(store, "_write_json_sync", fail_on_manifest)
    with pytest.raises(AssetStorageError, match="staging storage operation failed"):
        store.commit_file(
            attempt=attempt,
            produced_path=produced,
            media_key="media-1",
            media_kind="video",
            role="original",
            ordinal=0,
            sanitized_source={
                "title": {"Authorization": "Bearer secret"},
                "author": "safe author",
            },
            producer={"adapter": "fake", "Cookie": "secret"},
            verifier=NonEmptyTestVerifier(),
            job_id=job_id,
            attempt_id=attempt_id,
            source_item_id=str(uuid4()),
        )
    assert store.staging_root.exists()
    assert not any(store.staging_root.iterdir())


def test_asset_store_fails_closed_when_staging_directory_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(tmp_path / "data", min_free_bytes=0)
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    attempt = store.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "clip.fake"
    produced.write_bytes(b"bytes requiring durable staging")

    def fail_directory_sync(*_args, **_kwargs):
        raise OSError("simulated directory sync failure")

    if os.name == "nt":
        monkeypatch.setattr(
            AssetStore,
            "_sync_windows_directory",
            staticmethod(fail_directory_sync),
        )
    else:
        monkeypatch.setattr(os, "open", fail_directory_sync)

    with pytest.raises(AssetStorageError, match="failed to sync managed directory"):
        store.stage_file(
            attempt=attempt,
            produced_path=produced,
            media_key="media-1",
            media_kind="video",
            role="original",
            ordinal=0,
            sanitized_source={},
            producer={},
            verifier=NonEmptyTestVerifier(),
            job_id=job_id,
            attempt_id=attempt_id,
            source_item_id=str(uuid4()),
        )
    assert store.staging_root.exists()
    assert not any(store.staging_root.iterdir())


def test_asset_store_fails_closed_when_publish_directory_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(tmp_path / "data", min_free_bytes=0)
    job_id = str(uuid4())
    attempt_id = str(uuid4())
    attempt = store.prepare_attempt(job_id, attempt_id)
    produced = attempt.output / "clip.fake"
    produced.write_bytes(b"bytes requiring durable publish")
    staged = store.stage_file(
        attempt=attempt,
        produced_path=produced,
        media_key="media-1",
        media_kind="video",
        role="original",
        ordinal=0,
        sanitized_source={},
        producer={},
        verifier=NonEmptyTestVerifier(),
        job_id=job_id,
        attempt_id=attempt_id,
        source_item_id=str(uuid4()),
    )

    def fail_directory_sync(_path: Path) -> None:
        raise AssetStorageError("simulated publish directory sync failure")

    monkeypatch.setattr(store, "_sync_directory", fail_directory_sync)

    with pytest.raises(AssetStorageError, match="publish directory sync failure"):
        store.publish_staged(staged)
    assert (store.assets_root / staged.asset_id).is_dir()
    assert not staged.staged_directory.exists()

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Protocol, Sequence
from uuid import UUID, uuid4


SOURCE_JSON_KEYS = {
    "platform",
    "source_type",
    "source_id",
    "canonical_url",
    "title",
    "author",
    "published_at",
    "duration_seconds",
}
SOURCE_STRING_LIMITS = {
    "platform": 64,
    "source_type": 64,
    "source_id": 512,
    "canonical_url": 2048,
    "title": 1024,
    "author": 512,
    "published_at": 128,
}
PRODUCER_STRING_LIMITS = {
    "adapter": 128,
    "adapter_version": 128,
    "worker": 128,
    "worker_version": 128,
}
DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_AUXILIARY_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024
DEFAULT_ATTEMPT_RECONCILE_LIMIT = 64
MAX_THUMBNAILS_PER_ASSET = 8
MAX_CAPTIONS_PER_ASSET = 32
MAX_TOTAL_THUMBNAILS_PER_DOWNLOAD = 64
MAX_TOTAL_CAPTIONS_PER_DOWNLOAD = 256

THUMBNAIL_MIME_TYPES = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpe": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
CAPTION_MIME_TYPES = {
    ".ass": "text/x-ssa",
    ".json3": "application/json",
    ".lrc": "text/plain",
    ".smi": "application/smil+xml",
    ".srt": "application/x-subrip",
    ".srv1": "application/xml",
    ".srv2": "application/xml",
    ".srv3": "application/xml",
    ".ssa": "text/x-ssa",
    ".ttml": "application/ttml+xml",
    ".vtt": "text/vtt",
}
_CAPTION_LANGUAGE_RE = re.compile(
    r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*$"
)


@dataclass(frozen=True, slots=True)
class AttemptPaths:
    root: Path
    output: Path
    metadata: Path


@dataclass(frozen=True, slots=True)
class VerificationResult:
    media_kind: str
    container: str | None = None
    codec: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class AuxiliaryFile:
    """One adapter-produced sidecar owned by an original media item."""

    path: Path
    kind: str
    ordinal: int
    mime_type: str
    language: str | None = None


@dataclass(frozen=True, slots=True)
class AssetArtifact:
    """One staged/published auxiliary artifact recorded in manifest and DB."""

    kind: str
    ordinal: int
    size_bytes: int
    sha256: str
    relative_path: str
    mime_type: str
    language: str | None = None


class MediaVerifier(Protocol):
    name: str
    version: str

    def verify(self, path: Path, media_kind: str) -> VerificationResult: ...


@dataclass(frozen=True, slots=True)
class CommittedAsset:
    asset_id: str
    media_key: str
    media_kind: str
    role: str
    ordinal: int
    size_bytes: int
    sha256: str
    relative_original_path: str
    relative_manifest_path: str
    manifest_sha256: str
    verification: VerificationResult
    attempt_id: str = ""
    artifacts: tuple[AssetArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class StagedAsset:
    asset_id: str
    attempt_id: str
    media_key: str
    media_kind: str
    role: str
    ordinal: int
    size_bytes: int
    sha256: str
    relative_original_path: str
    relative_manifest_path: str
    manifest_sha256: str
    verification: VerificationResult
    staged_directory: Path
    artifacts: tuple[AssetArtifact, ...] = ()


class AssetValidationError(ValueError):
    pass


class AssetStorageError(OSError):
    """A storage-capacity or durability failure that must pause the queue."""


class NonEmptyTestVerifier:
    """Offline-only verifier used by the scripted fake adapter tests.

    This is intentionally named as a test verifier so it cannot be mistaken
    for ffprobe-backed production validation.
    """

    name = "non_empty_test_verifier"
    version = "1"

    def verify(self, path: Path, media_kind: str) -> VerificationResult:
        if not path.is_file() or path.stat().st_size <= 0:
            raise AssetValidationError("produced file is empty or missing")
        return VerificationResult(media_kind=media_kind, container="fake")


class AssetStore:
    def __init__(
        self,
        data_root: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    ) -> None:
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must be non-negative")
        self.data_root = data_root.resolve()
        self.temporary_root = self.data_root / "temporary"
        self.assets_root = self.data_root / "assets"
        self.staging_root = self.assets_root / ".staging"
        self.max_file_bytes = max_file_bytes
        self.min_free_bytes = min_free_bytes

    def prepare_attempt(self, job_id: str, attempt_id: str) -> AttemptPaths:
        root = self.temporary_root / self._safe_component(job_id) / self._safe_component(
            attempt_id
        )
        output = root / "output"
        metadata = root / "metadata"
        try:
            output.mkdir(parents=True, exist_ok=False)
            metadata.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise AssetStorageError(
                "failed to prepare managed attempt directory"
            ) from exc
        return AttemptPaths(root=root, output=output, metadata=metadata)

    def stage_file(
        self,
        *,
        attempt: AttemptPaths,
        produced_path: Path,
        media_key: str,
        media_kind: str,
        role: str,
        ordinal: int,
        sanitized_source: Mapping[str, object],
        producer: Mapping[str, object],
        verifier: MediaVerifier,
        job_id: str,
        attempt_id: str,
        source_item_id: str,
        auxiliary_files: Sequence[AuxiliaryFile] = (),
    ) -> StagedAsset:
        """Validate and durably stage one asset without publishing it.

        Publishing is deliberately a separate operation so a database-backed
        commit intent can be made durable before the only filesystem rename.
        """
        source_path = self._validate_produced_path(attempt.output, produced_path)
        try:
            source_info = source_path.stat()
            source_size = source_info.st_size
        except OSError as exc:
            raise AssetValidationError(
                "produced file is missing or unreadable"
            ) from exc
        if source_size > self.max_file_bytes:
            raise AssetValidationError("produced file exceeds configured size limit")
        normalized_auxiliary = self._validate_auxiliary_files(
            attempt.output,
            source_info=source_info,
            files=auxiliary_files,
        )
        total_size = source_size + sum(
            info.st_size for _, _, info in normalized_auxiliary
        )
        try:
            self.assets_root.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(self.assets_root).free
        except OSError as exc:
            raise AssetStorageError("failed to inspect managed asset storage") from exc
        if free_bytes - total_size < self.min_free_bytes:
            raise AssetStorageError("insufficient disk free-space reserve")
        asset_id = str(uuid4())
        stage = self.staging_root / f"{asset_id}-{self._safe_component(attempt_id)}"
        final = self.assets_root / asset_id
        if stage.exists() or final.exists():
            raise AssetValidationError("asset staging collision")

        try:
            original_dir = stage / "original"
            metadata_dir = stage / "metadata"
            original_dir.mkdir(parents=True)
            metadata_dir.mkdir(parents=True)
            suffix = source_path.suffix.lower()[:12]
            target = original_dir / f"source{suffix}"
            self._copy_and_sync(
                source_path,
                target,
                expected_size=source_size,
                max_size=self.max_file_bytes,
                expected_device=source_info.st_dev,
                expected_inode=source_info.st_ino,
            )
            if target.stat().st_size != source_size:
                raise AssetValidationError("staging size mismatch")
            try:
                verification = verifier.verify(target, media_kind)
            except AssetValidationError:
                raise
            except OSError as exc:
                raise AssetStorageError("failed to verify staged asset") from exc
            source_sha = self.sha256(target)

            staged_artifacts: list[AssetArtifact] = []
            auxiliary_directories: set[Path] = set()
            for auxiliary, auxiliary_source, auxiliary_info in normalized_auxiliary:
                auxiliary_dir = stage / (
                    "thumbnails" if auxiliary.kind == "thumbnail" else "captions"
                )
                auxiliary_dir.mkdir(parents=True, exist_ok=True)
                auxiliary_directories.add(auxiliary_dir)
                suffix = auxiliary_source.suffix.lower()
                if auxiliary.kind == "caption":
                    if auxiliary.language is None:
                        raise AssetValidationError("caption language is required")
                    filename = (
                        f"caption-{auxiliary.ordinal:04d}-"
                        f"{auxiliary.language.replace('_', '-')}{suffix}"
                    )
                else:
                    filename = f"thumbnail-{auxiliary.ordinal:04d}{suffix}"
                auxiliary_target = auxiliary_dir / filename
                self._copy_and_sync(
                    auxiliary_source,
                    auxiliary_target,
                    expected_size=auxiliary_info.st_size,
                    max_size=min(
                        self.max_file_bytes,
                        DEFAULT_MAX_AUXILIARY_FILE_BYTES,
                    ),
                    expected_device=auxiliary_info.st_dev,
                    expected_inode=auxiliary_info.st_ino,
                )
                auxiliary_sha = self.sha256(auxiliary_target)
                staged_artifacts.append(
                    AssetArtifact(
                        kind=auxiliary.kind,
                        ordinal=auxiliary.ordinal,
                        size_bytes=auxiliary_info.st_size,
                        sha256=auxiliary_sha,
                        relative_path=(
                            Path("assets")
                            / asset_id
                            / auxiliary_dir.name
                            / filename
                        ).as_posix(),
                        mime_type=auxiliary.mime_type,
                        language=auxiliary.language,
                    )
                )

            source_json = self._sanitize_source(sanitized_source)
            self._write_json_sync(metadata_dir / "source.json", source_json)
            relative_original = (
                Path("assets") / asset_id / "original" / target.name
            ).as_posix()
            relative_manifest = (
                Path("assets") / asset_id / "metadata" / "manifest.json"
            ).as_posix()
            safe_producer = {
                key: value[:limit]
                for key, limit in PRODUCER_STRING_LIMITS.items()
                if isinstance((value := producer.get(key)), str)
            }
            manifest = {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "asset_id": asset_id,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "source_item_id": source_item_id,
                "media_key": str(media_key)[:1024],
                "original": {
                    "path": f"original/{target.name}",
                    "size_bytes": source_size,
                    "sha256": source_sha,
                },
                "media": {
                    "kind": verification.media_kind,
                    "container": verification.container,
                    "codec": verification.codec,
                    "duration_seconds": verification.duration_seconds,
                    "width": verification.width,
                    "height": verification.height,
                },
                "producer": {
                    **safe_producer,
                    "verifier": verifier.name[:128],
                    "verifier_version": verifier.version[:128],
                },
                "artifacts": [
                    {
                        "path": str(
                            Path(artifact.relative_path).relative_to(
                                Path("assets") / asset_id
                            )
                        ).replace("\\", "/"),
                        "kind": artifact.kind,
                        "ordinal": artifact.ordinal,
                        "size_bytes": artifact.size_bytes,
                        "mime_type": artifact.mime_type,
                        "language": artifact.language,
                        "sha256": artifact.sha256,
                    }
                    for artifact in staged_artifacts
                ],
            }
            manifest_path = metadata_dir / "manifest.json"
            self._write_json_sync(manifest_path, manifest)
            manifest_sha = self.sha256(manifest_path)
            for directory in (
                original_dir,
                *sorted(auxiliary_directories),
                metadata_dir,
                stage,
                self.staging_root,
                self.assets_root,
                self.data_root,
            ):
                self._sync_directory(directory)

        except Exception as exc:
            if stage.exists():
                shutil.rmtree(stage)
            if isinstance(exc, OSError) and not isinstance(exc, AssetStorageError):
                raise AssetStorageError("asset staging storage operation failed") from exc
            raise
        return StagedAsset(
            asset_id=asset_id,
            attempt_id=attempt_id,
            media_key=media_key,
            media_kind=media_kind,
            role=role,
            ordinal=ordinal,
            size_bytes=source_size,
            sha256=source_sha,
            relative_original_path=relative_original,
            relative_manifest_path=relative_manifest,
            manifest_sha256=manifest_sha,
            verification=verification,
            staged_directory=stage,
            artifacts=tuple(staged_artifacts),
        )

    def publish_staged(self, staged: StagedAsset) -> CommittedAsset:
        """Atomically publish a staged asset.

        The operation is idempotent for the crash window where the directory
        rename landed but the surrounding database transaction rolled back.
        This method is the sole location allowed to call ``os.replace``.
        """
        asset_id = self._safe_component(staged.asset_id)
        attempt_id = self._safe_component(staged.attempt_id)
        expected_stage = self._validated_managed_directory(
            self.staging_root / f"{asset_id}-{attempt_id}",
            parent=self.staging_root,
            expected_depth=1,
        )
        supplied_stage = self._validated_managed_directory(
            staged.staged_directory,
            parent=self.staging_root,
            expected_depth=1,
        )
        if supplied_stage != expected_stage:
            raise AssetValidationError("staged asset identity mismatch")
        final = self._validated_managed_directory(
            self.assets_root / asset_id,
            parent=self.assets_root,
            expected_depth=1,
        )
        if final.exists():
            if expected_stage.exists():
                raise AssetValidationError("asset publish collision")
            self._validate_published_asset(staged)
        else:
            if not expected_stage.is_dir():
                raise AssetValidationError("staged asset is missing")
            try:
                os.replace(expected_stage, final)
            except OSError as exc:
                raise AssetStorageError("asset publish rename failed") from exc
            self._validate_published_asset(staged)
        self._sync_directory(self.staging_root)
        self._sync_directory(self.assets_root)
        return CommittedAsset(
            asset_id=staged.asset_id,
            media_key=staged.media_key,
            media_kind=staged.media_kind,
            role=staged.role,
            ordinal=staged.ordinal,
            size_bytes=staged.size_bytes,
            sha256=staged.sha256,
            relative_original_path=staged.relative_original_path,
            relative_manifest_path=staged.relative_manifest_path,
            manifest_sha256=staged.manifest_sha256,
            verification=staged.verification,
            attempt_id=staged.attempt_id,
            artifacts=staged.artifacts,
        )

    def commit_file(self, **kwargs: object) -> CommittedAsset:
        """Compatibility wrapper for callers that do not use durable intents.

        Worker code must use ``stage_file`` followed by a database intent and
        ``publish_staged``.  Keeping this wrapper preserves the existing
        in-process compensation API for offline tests and small integrations.
        """
        staged = self.stage_file(**kwargs)  # type: ignore[arg-type]
        try:
            return self.publish_staged(staged)
        except Exception:
            self.remove_pending_asset(staged.asset_id, staged.attempt_id)
            raise

    def cleanup_attempt(self, attempt: AttemptPaths) -> None:
        """Remove one Worker-owned temporary attempt after a terminal outcome."""
        root = self._validated_managed_directory(
            attempt.root, parent=self.temporary_root, expected_depth=2
        )
        if root.exists():
            shutil.rmtree(root)
        job_root = root.parent
        if job_root.exists() and not any(job_root.iterdir()):
            job_root.rmdir()

    def list_managed_attempt_ids(
        self, limit: int = DEFAULT_ATTEMPT_RECONCILE_LIMIT
    ) -> tuple[str, ...]:
        """Return a bounded identity-only view of Worker-owned leftovers."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("attempt reconciliation limit must be positive")
        candidates: set[str] = set()
        try:
            if self.temporary_root.exists():
                for job_entry in os.scandir(self.temporary_root):
                    if not job_entry.is_dir(follow_symlinks=False):
                        continue
                    if not self._is_uuid_component(job_entry.name):
                        continue
                    with os.scandir(job_entry.path) as attempt_entries:
                        for attempt_entry in attempt_entries:
                            if (
                                attempt_entry.is_dir(follow_symlinks=False)
                                and self._is_uuid_component(attempt_entry.name)
                            ):
                                candidates.add(attempt_entry.name.lower())
            if self.staging_root.exists():
                for stage_entry in os.scandir(self.staging_root):
                    if not stage_entry.is_dir(follow_symlinks=False):
                        continue
                    identity = self._stage_identity(stage_entry.name)
                    if identity is not None:
                        candidates.add(identity[1])
        except OSError as exc:
            raise AssetStorageError(
                "failed to enumerate managed attempt leftovers"
            ) from exc
        return tuple(sorted(candidates)[:limit])

    def cleanup_attempt_identity(self, job_id: str, attempt_id: str) -> None:
        """Idempotently remove leftovers for one database-terminal Attempt."""

        safe_job_id = self._safe_component(job_id)
        safe_attempt_id = self._safe_component(attempt_id)
        root = self._validated_managed_directory(
            self.temporary_root / safe_job_id / safe_attempt_id,
            parent=self.temporary_root,
            expected_depth=2,
        )
        try:
            if root.exists():
                self._remove_managed_tree(root)
                self._sync_directory(root.parent)
            job_root = root.parent
            if job_root.exists() and not any(job_root.iterdir()):
                job_root.rmdir()
                self._sync_directory(self.temporary_root)

            if self.staging_root.exists():
                matching: list[Path] = []
                for stage_entry in os.scandir(self.staging_root):
                    identity = self._stage_identity(stage_entry.name)
                    if identity is None or identity[1] != safe_attempt_id.lower():
                        continue
                    if not stage_entry.is_dir(follow_symlinks=False):
                        raise AssetStorageError(
                            "managed staging leftover is not a directory"
                        )
                    matching.append(Path(stage_entry.path))
                for stage in matching:
                    self._remove_managed_tree(stage)
                if matching:
                    self._sync_directory(self.staging_root)
                    self._sync_directory(self.assets_root)
        except AssetStorageError:
            raise
        except OSError as exc:
            raise AssetStorageError(
                "failed to remove managed attempt leftovers"
            ) from exc

    def rollback_committed_asset(self, committed: CommittedAsset) -> None:
        """Compensate a filesystem commit when the database commit did not land."""
        self.remove_pending_asset(
            committed.asset_id,
            committed.attempt_id or None,
        )

    def remove_pending_asset(
        self,
        asset_id: str,
        attempt_id: str | None = None,
    ) -> None:
        """Remove staged and/or published files for an uncommitted asset.

        Recovery supplies both identifiers from ``asset_commit_intents``.
        ``attempt_id`` remains optional only for the legacy committed-asset
        compensation wrapper, whose historical value did not carry it.
        """
        safe_asset_id = self._safe_component(asset_id)
        final = self._validated_managed_directory(
            self.assets_root / safe_asset_id,
            parent=self.assets_root,
            expected_depth=1,
        )
        cleanup_error: OSError | None = None
        if attempt_id is not None:
            safe_attempt_id = self._safe_component(attempt_id)
            stage = self._validated_managed_directory(
                self.staging_root / f"{safe_asset_id}-{safe_attempt_id}",
                parent=self.staging_root,
                expected_depth=1,
            )
            if stage.exists():
                try:
                    shutil.rmtree(stage)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    cleanup_error = exc
        if final.exists():
            try:
                shutil.rmtree(final)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error

    def _validate_published_asset(self, staged: StagedAsset) -> None:
        original = self.data_root / staged.relative_original_path
        manifest = self.data_root / staged.relative_manifest_path
        if not original.is_file() or not manifest.is_file():
            raise AssetValidationError("published asset is incomplete")
        if original.stat().st_size != staged.size_bytes:
            raise AssetValidationError("published asset size mismatch")
        if self.sha256(original) != staged.sha256:
            raise AssetValidationError("published asset hash mismatch")
        if self.sha256(manifest) != staged.manifest_sha256:
            raise AssetValidationError("published manifest hash mismatch")
        for artifact in staged.artifacts:
            path = self.data_root / artifact.relative_path
            if not path.is_file():
                raise AssetValidationError("published auxiliary artifact is incomplete")
            if path.stat().st_size != artifact.size_bytes:
                raise AssetValidationError("published auxiliary artifact size mismatch")
            if self.sha256(path) != artifact.sha256:
                raise AssetValidationError("published auxiliary artifact hash mismatch")

    @staticmethod
    def describe_auxiliary_file(
        *,
        path: Path,
        kind: str,
        ordinal: int,
    ) -> AuxiliaryFile:
        """Derive a bounded MIME/language contract from a sidecar filename."""

        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise AssetValidationError("auxiliary ordinal must be non-negative")
        suffix = path.suffix.lower()
        if kind == "thumbnail":
            mime_type = THUMBNAIL_MIME_TYPES.get(suffix)
            if mime_type is None:
                raise AssetValidationError("thumbnail extension is not allowed")
            if ".thumbnail." not in path.name.lower():
                raise AssetValidationError("thumbnail filename marker is missing")
            return AuxiliaryFile(
                path=path,
                kind=kind,
                ordinal=ordinal,
                mime_type=mime_type,
            )
        if kind == "caption":
            mime_type = CAPTION_MIME_TYPES.get(suffix)
            if mime_type is None:
                raise AssetValidationError("caption extension is not allowed")
            match = re.search(
                rf"\.caption\.([^.]+){re.escape(suffix)}$",
                path.name,
                flags=re.IGNORECASE,
            )
            if match is None or not _CAPTION_LANGUAGE_RE.fullmatch(match.group(1)):
                raise AssetValidationError("caption language is missing or unsafe")
            return AuxiliaryFile(
                path=path,
                kind=kind,
                ordinal=ordinal,
                mime_type=mime_type,
                language=match.group(1).replace("_", "-"),
            )
        raise AssetValidationError("auxiliary artifact kind is not allowed")

    def _validate_auxiliary_files(
        self,
        output_root: Path,
        *,
        source_info: os.stat_result,
        files: Sequence[AuxiliaryFile],
    ) -> tuple[tuple[AuxiliaryFile, Path, os.stat_result], ...]:
        counts = {"thumbnail": 0, "caption": 0}
        seen_ordinals = {"thumbnail": set(), "caption": set()}
        seen_identities = {(source_info.st_dev, source_info.st_ino)}
        normalized: list[tuple[AuxiliaryFile, Path, os.stat_result]] = []
        for auxiliary in files:
            if auxiliary.kind not in counts:
                raise AssetValidationError("auxiliary artifact kind is not allowed")
            described = self.describe_auxiliary_file(
                path=auxiliary.path,
                kind=auxiliary.kind,
                ordinal=auxiliary.ordinal,
            )
            if (
                described.mime_type != auxiliary.mime_type
                or described.language != auxiliary.language
            ):
                raise AssetValidationError("auxiliary metadata does not match filename")
            if auxiliary.ordinal in seen_ordinals[auxiliary.kind]:
                raise AssetValidationError("auxiliary ordinals must be unique")
            seen_ordinals[auxiliary.kind].add(auxiliary.ordinal)
            counts[auxiliary.kind] += 1
            limit = (
                MAX_THUMBNAILS_PER_ASSET
                if auxiliary.kind == "thumbnail"
                else MAX_CAPTIONS_PER_ASSET
            )
            if counts[auxiliary.kind] > limit:
                raise AssetValidationError("auxiliary artifact count exceeds limit")
            path = self._validate_produced_path(output_root, auxiliary.path)
            try:
                info = path.stat()
            except OSError as exc:
                raise AssetValidationError(
                    "auxiliary file is missing or unreadable"
                ) from exc
            identity = (info.st_dev, info.st_ino)
            if identity in seen_identities:
                raise AssetValidationError("auxiliary file path is duplicated")
            seen_identities.add(identity)
            if info.st_size > min(
                self.max_file_bytes,
                DEFAULT_MAX_AUXILIARY_FILE_BYTES,
            ):
                raise AssetValidationError(
                    "auxiliary file exceeds configured size limit"
                )
            normalized.append((described, path, info))
        return tuple(normalized)

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_component(value: str) -> str:
        if not value or any(character not in "0123456789-abcdefABCDEF" for character in value):
            raise AssetValidationError("unsafe identifier component")
        return value

    @staticmethod
    def _is_uuid_component(value: str) -> bool:
        try:
            return str(UUID(value)) == value.lower()
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def _stage_identity(value: str) -> tuple[str, str] | None:
        if len(value) != 73 or value[36] != "-":
            return None
        asset_id = value[:36].lower()
        attempt_id = value[37:].lower()
        if not (
            AssetStore._is_uuid_component(asset_id)
            and AssetStore._is_uuid_component(attempt_id)
        ):
            return None
        return asset_id, attempt_id

    @staticmethod
    def _remove_managed_tree(path: Path) -> None:
        for current_root, directory_names, file_names in os.walk(
            path, topdown=True, followlinks=False
        ):
            for name in [*directory_names, *file_names]:
                candidate = Path(current_root) / name
                info = candidate.lstat()
                attributes = getattr(info, "st_file_attributes", 0)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if candidate.is_symlink() or (reparse and attributes & reparse):
                    raise AssetStorageError(
                        "managed cleanup refuses links or reparse points"
                    )
        shutil.rmtree(path)

    @staticmethod
    def _validate_produced_path(output_root: Path, produced_path: Path) -> Path:
        try:
            root = output_root.resolve(strict=True)
            if produced_path.is_symlink():
                raise AssetValidationError("links and reparse points are not accepted")
            candidate = produced_path.resolve(strict=True)
            if not candidate.is_relative_to(root) or candidate == root:
                raise AssetValidationError("produced path escapes attempt output")
            current = candidate
            while current != root:
                info = current.lstat()
                attributes = getattr(info, "st_file_attributes", 0)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if current.is_symlink() or (reparse and attributes & reparse):
                    raise AssetValidationError(
                        "links and reparse points are not accepted"
                    )
                current = current.parent
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                raise AssetValidationError("produced file is empty or missing")
            if candidate.stat().st_nlink != 1:
                raise AssetValidationError("hard-linked output is not accepted")
            return candidate
        except AssetValidationError:
            raise
        except OSError as exc:
            raise AssetValidationError(
                "produced file is missing or unreadable"
            ) from exc

    @staticmethod
    def _copy_and_sync(
        source: Path,
        target: Path,
        *,
        expected_size: int,
        max_size: int,
        expected_device: int,
        expected_inode: int,
    ) -> None:
        try:
            source_handle = source.open("rb")
        except OSError as exc:
            raise AssetValidationError(
                "produced file is missing or unreadable"
            ) from exc
        try:
            with source_handle:
                opened_info = os.fstat(source_handle.fileno())
                if (
                    not stat.S_ISREG(opened_info.st_mode)
                    or opened_info.st_nlink != 1
                    or opened_info.st_dev != expected_device
                    or opened_info.st_ino != expected_inode
                    or opened_info.st_size != expected_size
                ):
                    raise AssetValidationError(
                        "produced file changed before staging"
                    )
                try:
                    target_handle = target.open("xb")
                except OSError as exc:
                    raise AssetStorageError(
                        "failed to create staged asset"
                    ) from exc
                try:
                    with target_handle:
                        copied = 0
                        while True:
                            try:
                                chunk = source_handle.read(1024 * 1024)
                            except OSError as exc:
                                raise AssetValidationError(
                                    "produced file is missing or unreadable"
                                ) from exc
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > max_size or copied > expected_size:
                                raise AssetValidationError(
                                    "produced file changed or exceeded size limit"
                                )
                            try:
                                target_handle.write(chunk)
                            except OSError as exc:
                                raise AssetStorageError(
                                    "failed to write staged asset"
                                ) from exc
                        if copied != expected_size:
                            raise AssetValidationError(
                                "produced file changed during staging"
                            )
                        final_source_info = os.fstat(source_handle.fileno())
                        if (
                            final_source_info.st_size != expected_size
                            or final_source_info.st_dev != expected_device
                            or final_source_info.st_ino != expected_inode
                        ):
                            raise AssetValidationError(
                                "produced file changed during staging"
                            )
                        try:
                            target_handle.flush()
                            os.fsync(target_handle.fileno())
                        except OSError as exc:
                            raise AssetStorageError(
                                "failed to sync staged asset"
                            ) from exc
                except (AssetValidationError, AssetStorageError):
                    raise
                except OSError as exc:
                    raise AssetStorageError(
                        "failed to close staged asset"
                    ) from exc
        except (AssetValidationError, AssetStorageError):
            raise
        except OSError as exc:
            raise AssetValidationError(
                "produced file is missing or unreadable"
            ) from exc

    @staticmethod
    def _sanitize_source(value: Mapping[str, object]) -> dict[str, object]:
        safe: dict[str, object] = {}
        for key in SOURCE_JSON_KEYS:
            candidate = value.get(key)
            if key == "duration_seconds":
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    safe[key] = candidate
                continue
            if isinstance(candidate, str):
                safe[key] = candidate[: SOURCE_STRING_LIMITS[key]]
        return safe

    @staticmethod
    def _validated_managed_directory(
        path: Path,
        *,
        parent: Path,
        expected_depth: int,
    ) -> Path:
        parent_absolute = parent.absolute()
        path_absolute = path.absolute()
        try:
            relative = path_absolute.relative_to(parent_absolute)
        except ValueError as exc:
            raise AssetValidationError("managed path escapes data root") from exc
        if len(relative.parts) != expected_depth:
            raise AssetValidationError("managed path has unexpected depth")
        return path_absolute

    @staticmethod
    def _sync_directory(path: Path) -> None:
        """Persist directory-entry changes using the strongest local primitive."""

        try:
            if os.name == "nt":
                AssetStore._sync_windows_directory(path)
                return
            flags = os.O_RDONLY
            flags |= getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise AssetStorageError("managed sync target is not a directory")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except AssetStorageError:
            raise
        except OSError as exc:
            raise AssetStorageError(
                f"failed to sync managed directory {path.name!r}"
            ) from exc

    @staticmethod
    def _sync_windows_directory(path: Path) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = [wintypes.HANDLE]
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        generic_write = 0x40000000
        shared_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        backup_semantics = 0x02000000
        invalid_handle = ctypes.c_void_p(-1).value
        handle = create_file(
            str(path),
            generic_write,
            shared_read_write_delete,
            None,
            open_existing,
            backup_semantics,
            None,
        )
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not flush_file_buffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if not close_handle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _write_json_sync(path: Path, value: Mapping[str, object]) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

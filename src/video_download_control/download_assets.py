"""Download-owned registration, stable reads, and cross-domain media references.

HTTP callers own response delivery and error mapping. This module knows no
Editing, Upload, Workflow, ASGI or backend execution contract.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Mapping
from uuid import UUID

from .assets import (
    CAPTION_MIME_TYPES,
    DEFAULT_MAX_AUXILIARY_FILE_BYTES,
    THUMBNAIL_MIME_TYPES,
)
from .managed_files import close_binary_on_error

if TYPE_CHECKING:
    from .service import BatchService

ASSET_READ_CHUNK_BYTES = 64 * 1024
_SPOOL_MEMORY_BYTES = 1024 * 1024


class DownloadAssetReadError(ValueError):
    """A stable Download lookup/availability outcome, independent of HTTP."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DownloadReadCancelled(Exception):
    """Internal cooperative stop between bounded source/spool operations."""


def _raise_if_cancelled(cancellation: threading.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise DownloadReadCancelled


def canonical_download_asset_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("asset id is invalid")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("asset id is invalid") from exc
    if canonical != value:
        raise ValueError("asset id is invalid")
    return canonical


def resolve_registered_original(
    data_root: Path,
    *,
    asset_id: str,
    relative_path: object,
    expected_size: object,
) -> tuple[Path, os.stat_result]:
    """Resolve one immutable AssetStore original without trusting DB path text."""

    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path) > 4096
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("registered original path is invalid")
    stored = PurePosixPath(relative_path)
    if (
        stored.is_absolute()
        or stored.as_posix() != relative_path
        or len(stored.parts) != 4
        or stored.parts[:3] != ("assets", asset_id, "original")
        or any(part in {"", ".", ".."} for part in stored.parts)
    ):
        raise ValueError("registered original path is invalid")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        raise ValueError("registered original size is invalid")

    root = data_root.resolve(strict=True)
    current = root
    final_info: os.stat_result | None = None
    for index, component in enumerate(stored.parts):
        current /= component
        info = current.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or (reparse and attributes & reparse):
            raise ValueError("registered original path contains a link")
        if index < len(stored.parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("registered original parent is not a directory")
        else:
            final_info = info

    assert final_info is not None
    if not stat.S_ISREG(final_info.st_mode) or final_info.st_nlink != 1:
        raise ValueError("registered original is not a plain single-link file")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root) or final_info.st_size != expected_size:
        raise ValueError("registered original does not match its asset record")
    return resolved, final_info


def read_original_snapshot(
    path: Path,
    *,
    expected_info: os.stat_result,
    expected_sha256: object,
    cancellation: threading.Event | None = None,
) -> BinaryIO:
    """Copy one verified original into a stable, bounded-memory spool."""

    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ValueError("registered original hash is invalid")
    _raise_if_cancelled(cancellation)
    spool: BinaryIO = tempfile.SpooledTemporaryFile(
        max_size=_SPOOL_MEMORY_BYTES,
        mode="w+b",
    )
    with close_binary_on_error(spool):
        digest = hashlib.sha256()
        total_bytes = 0
        with path.open("rb") as handle:
            _raise_if_cancelled(cancellation)
            opened_info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_nlink != 1
                or opened_info.st_dev != expected_info.st_dev
                or opened_info.st_ino != expected_info.st_ino
                or opened_info.st_size != expected_info.st_size
            ):
                raise ValueError("registered original changed before reading")
            while True:
                _raise_if_cancelled(cancellation)
                chunk = handle.read(ASSET_READ_CHUNK_BYTES)
                _raise_if_cancelled(cancellation)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > expected_info.st_size:
                    raise ValueError("registered original content is invalid")
                digest.update(chunk)
                spool.write(chunk)
                _raise_if_cancelled(cancellation)
            final_opened_info = os.fstat(handle.fileno())
        if (
            total_bytes != expected_info.st_size
            or final_opened_info.st_dev != opened_info.st_dev
            or final_opened_info.st_ino != opened_info.st_ino
            or final_opened_info.st_size != opened_info.st_size
            or final_opened_info.st_mtime_ns != opened_info.st_mtime_ns
            or final_opened_info.st_ctime_ns != opened_info.st_ctime_ns
            or digest.hexdigest() != expected_sha256
        ):
            raise ValueError("registered original content is invalid")
        _raise_if_cancelled(cancellation)
        spool.flush()
        spool.seek(0)
        _raise_if_cancelled(cancellation)
        return spool


def resolve_registered_auxiliary(
    data_root: Path,
    *,
    asset_id: object,
    kind: object,
    relative_path: object,
    mime_type: object,
    language: object,
    expected_sha256: object,
    maximum_bytes: int = DEFAULT_MAX_AUXILIARY_FILE_BYTES,
) -> tuple[Path, os.stat_result, str, str, str]:
    """Resolve one registered sidecar without trusting its database path fields."""

    canonical_asset_id = canonical_download_asset_id(asset_id)
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes < 1
        or maximum_bytes > DEFAULT_MAX_AUXILIARY_FILE_BYTES
    ):
        raise ValueError("registered artifact size limit is invalid")
    if kind == "thumbnail":
        directory = "thumbnails"
        mime_types = THUMBNAIL_MIME_TYPES
    elif kind == "caption":
        directory = "captions"
        mime_types = CAPTION_MIME_TYPES
    else:
        raise ValueError("registered artifact kind is invalid")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path) > 4096
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("registered artifact path is invalid")
    stored = PurePosixPath(relative_path)
    if (
        stored.is_absolute()
        or stored.as_posix() != relative_path
        or len(stored.parts) != 4
        or stored.parts[:3] != ("assets", canonical_asset_id, directory)
        or any(part in {"", ".", ".."} for part in stored.parts)
    ):
        raise ValueError("registered artifact path is invalid")

    suffix = Path(stored.name).suffix.lower()
    expected_mime = mime_types.get(suffix)
    if (
        expected_mime is None
        or not isinstance(mime_type, str)
        or mime_type != expected_mime
    ):
        raise ValueError("registered artifact MIME is invalid")
    stem = stored.name[: -len(suffix)]
    if kind == "thumbnail":
        match = re.fullmatch(r"thumbnail-([0-9]{4,})", stem)
        if language is not None:
            raise ValueError("registered thumbnail language is invalid")
    else:
        match = re.fullmatch(
            r"caption-([0-9]{4,})-([A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)",
            stem,
        )
        if (
            match is None
            or not isinstance(language, str)
            or match.group(2) != language
        ):
            raise ValueError("registered caption language is invalid")
    if match is None or f"{int(match.group(1)):04d}" != match.group(1):
        raise ValueError("registered artifact ordinal is invalid")
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ValueError("registered artifact hash is invalid")

    root_info = data_root.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    root_attributes = getattr(root_info, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or (reparse and root_attributes & reparse)
    ):
        raise ValueError("asset root is not a plain directory")
    root = data_root.resolve(strict=True)
    current = root
    final_info: os.stat_result | None = None
    for index, component in enumerate(stored.parts):
        current /= component
        info = current.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or (reparse and attributes & reparse):
            raise ValueError("registered artifact path contains a link")
        if index < len(stored.parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("registered artifact parent is not a directory")
        else:
            final_info = info

    assert final_info is not None
    if (
        not stat.S_ISREG(final_info.st_mode)
        or final_info.st_nlink != 1
        or final_info.st_size < 1
        or final_info.st_size > maximum_bytes
    ):
        raise ValueError("registered artifact is not a bounded single-link file")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("registered artifact is outside the asset root")
    return resolved, final_info, expected_mime, suffix, expected_sha256


def read_registered_auxiliary(
    data_root: Path,
    *,
    asset_id: object,
    kind: object,
    relative_path: object,
    mime_type: object,
    language: object,
    expected_sha256: object,
    maximum_bytes: int = DEFAULT_MAX_AUXILIARY_FILE_BYTES,
) -> tuple[BinaryIO, int, str, str]:
    """Verify one sidecar into a bounded spool before any bytes are served."""

    resolved, final_info, expected_mime, suffix, registered_sha256 = (
        resolve_registered_auxiliary(
            data_root,
            asset_id=asset_id,
            kind=kind,
            relative_path=relative_path,
            mime_type=mime_type,
            language=language,
            expected_sha256=expected_sha256,
            maximum_bytes=maximum_bytes,
        )
    )
    spool: BinaryIO = tempfile.SpooledTemporaryFile(
        max_size=_SPOOL_MEMORY_BYTES,
        mode="w+b",
    )
    with close_binary_on_error(spool):
        digest = hashlib.sha256()
        total_bytes = 0
        with resolved.open("rb") as handle:
            opened_info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_nlink != 1
                or opened_info.st_dev != final_info.st_dev
                or opened_info.st_ino != final_info.st_ino
                or opened_info.st_size != final_info.st_size
            ):
                raise ValueError("registered artifact changed before reading")
            while chunk := handle.read(ASSET_READ_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > maximum_bytes:
                    raise ValueError("registered artifact content is invalid")
                digest.update(chunk)
                spool.write(chunk)
            final_opened_info = os.fstat(handle.fileno())
        if (
            total_bytes != opened_info.st_size
            or final_opened_info.st_dev != opened_info.st_dev
            or final_opened_info.st_ino != opened_info.st_ino
            or final_opened_info.st_size != opened_info.st_size
            or digest.hexdigest() != registered_sha256
        ):
            raise ValueError("registered artifact content is invalid")
        spool.seek(0)
        return spool, total_bytes, expected_mime, suffix


class DownloadAssetReader:
    """Resolve registered media using Download's own data and file contracts."""

    def __init__(self, data_root: Path, service: BatchService):
        self._data_root = data_root
        self._service = service

    def resolve_video_original(self, asset_id: str) -> tuple[Path, str]:
        """Return a video path/digest for the consuming domain's verified import."""

        try:
            canonical_id = canonical_download_asset_id(asset_id)
        except ValueError:
            raise DownloadAssetReadError("asset_not_found") from None
        registered = self._service.get_ready_original_asset(canonical_id)
        if registered is None or registered.get("media_kind") != "video":
            raise DownloadAssetReadError("asset_not_found")
        try:
            path, _ = resolve_registered_original(
                self._data_root, asset_id=canonical_id,
                relative_path=registered["original_path"],
                expected_size=registered["size_bytes"],
            )
        except (OSError, ValueError):
            raise DownloadAssetReadError("asset_file_unavailable") from None
        return path, registered["sha256"]

    def resolve_thumbnail(self, artifact_id: str) -> tuple[Path, str, str]:
        """Return a thumbnail reference for verification when it is imported."""

        try:
            canonical_id = canonical_download_asset_id(artifact_id)
        except ValueError:
            raise DownloadAssetReadError("download_thumbnail_not_found") from None
        registered = self._service.get_ready_auxiliary_artifact(canonical_id)
        if (
            registered is None
            or registered.get("artifact_id") != canonical_id
            or registered.get("kind") != "thumbnail"
        ):
            raise DownloadAssetReadError("download_thumbnail_not_found")
        try:
            path, _, _, suffix, expected_sha256 = resolve_registered_auxiliary(
                self._data_root,
                asset_id=registered["asset_id"],
                kind=registered["kind"],
                relative_path=registered["artifact_path"],
                mime_type=registered["mime_type"],
                language=registered["language"],
                expected_sha256=registered["sha256"],
            )
        except (KeyError, OSError, ValueError):
            raise DownloadAssetReadError("download_thumbnail_unavailable") from None
        if suffix not in {".jpe", ".jpg", ".jpeg", ".png", ".webp"}:
            raise DownloadAssetReadError("unsupported_cover_type")
        upload_suffix = ".jpeg" if suffix == ".jpe" else suffix
        return (
            path,
            expected_sha256,
            f"download-cover-{canonical_id}{upload_suffix}",
        )

    def read_caption(self, artifact_id: str, *, maximum_bytes: int) -> dict[str, object]:
        """Return verified caption bytes and their registered source metadata."""

        canonical_id = canonical_download_asset_id(artifact_id)
        registered = self._service.get_ready_auxiliary_artifact(canonical_id)
        if (
            registered is None
            or registered.get("artifact_id") != canonical_id
            or registered.get("kind") != "caption"
        ):
            raise ValueError("download caption is not registered and ready")
        try:
            handle, size_bytes, mime_type, _suffix = read_registered_auxiliary(
                self._data_root,
                asset_id=registered["asset_id"],
                kind=registered["kind"],
                relative_path=registered["artifact_path"],
                mime_type=registered["mime_type"],
                language=registered["language"],
                expected_sha256=registered["sha256"],
                maximum_bytes=maximum_bytes,
            )
            with close_binary_on_error(handle):
                payload = handle.read(size_bytes + 1)
                if len(payload) != size_bytes or handle.read(1):
                    raise ValueError("verified download caption size changed")
        except (KeyError, OSError, ValueError):
            raise ValueError("download caption is unavailable") from None
        handle.close()
        return {
            "artifact_id": canonical_id,
            "asset_id": canonical_download_asset_id(registered["asset_id"]),
            "artifact_path": registered["artifact_path"],
            "mime_type": mime_type,
            "language": registered["language"],
            "sha256": registered["sha256"],
            "origin": registered.get("origin"),
            "tool_name": registered.get("tool_name"),
            "tool_version": registered.get("tool_version"),
            "payload": payload,
        }

    def unique_thumbnail(self, asset_id: str) -> Mapping[str, object] | None:
        """Verify owner, candidates and bytes before returning one thumbnail."""

        try:
            canonical_asset_id = canonical_download_asset_id(asset_id)
            registry_rows = self._service.list_registered_thumbnails_for_asset(
                canonical_asset_id
            )
        except (OSError, ValueError):
            raise DownloadAssetReadError("source_cover_unavailable") from None
        if not registry_rows:
            raise DownloadAssetReadError("source_cover_unavailable")

        def belongs_to_directory(value: object, directory: str) -> bool:
            if (
                not isinstance(value, str)
                or not value
                or "\\" in value
                or "\x00" in value
            ):
                return False
            relative = PurePosixPath(value)
            return (
                not relative.is_absolute()
                and relative.as_posix() == value
                and ".." not in relative.parts
                and relative.parent
                == PurePosixPath("assets") / canonical_asset_id / directory
            )

        registered_candidates: list[
            tuple[Mapping[str, object], Path, str, str]
        ] = []
        try:
            for registered in registry_rows:
                registered_asset_id = canonical_download_asset_id(
                    registered["asset_id"]
                )
                original_artifact_id = canonical_download_asset_id(
                    registered["original_artifact_id"]
                )
                asset_sha256 = registered.get("asset_sha256")
                original_sha256 = registered.get("original_sha256")
                if (
                    registered_asset_id != canonical_asset_id
                    or registered.get("asset_status") != "ready"
                    or registered.get("asset_media_kind") != "video"
                    or registered.get("original_count") != 1
                    or not isinstance(asset_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", asset_sha256) is None
                    or asset_sha256 != original_sha256
                    or registered.get("original_parent_artifact_id") is not None
                    or not belongs_to_directory(
                        registered.get("original_path"), "original"
                    )
                    or registered.get("ready_original_link") != 1
                ):
                    raise ValueError("download cover owner is invalid")

                raw_artifact_id = registered.get("artifact_id")
                if raw_artifact_id is None:
                    if any(
                        registered.get(key) is not None
                        for key in (
                            "artifact_asset_id",
                            "kind",
                            "artifact_path",
                            "mime_type",
                            "sha256",
                            "caption_artifact_id",
                            "parent_artifact_id",
                        )
                    ):
                        raise ValueError("empty download artifact row is invalid")
                    continue

                artifact_id = canonical_download_asset_id(raw_artifact_id)
                artifact_asset_id = canonical_download_asset_id(
                    registered["artifact_asset_id"]
                )
                parent_artifact_id = canonical_download_asset_id(
                    registered["parent_artifact_id"]
                )
                artifact_sha256 = registered.get("sha256")
                if (
                    artifact_asset_id != canonical_asset_id
                    or parent_artifact_id != original_artifact_id
                    or not isinstance(artifact_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
                ):
                    raise ValueError("download artifact ownership is invalid")
                if (
                    registered.get("kind") != "thumbnail"
                    or registered.get("caption_artifact_id") is not None
                ):
                    raise ValueError("download thumbnail registry is invalid")
                path, _, _, suffix, expected_sha256 = (
                    resolve_registered_auxiliary(
                        self._data_root,
                        asset_id=canonical_asset_id,
                        kind=registered["kind"],
                        relative_path=registered["artifact_path"],
                        mime_type=registered["mime_type"],
                        language=registered["language"],
                        expected_sha256=artifact_sha256,
                    )
                )
                handle, _, _, verified_suffix = read_registered_auxiliary(
                    self._data_root,
                    asset_id=canonical_asset_id,
                    kind=registered["kind"],
                    relative_path=registered["artifact_path"],
                    mime_type=registered["mime_type"],
                    language=registered["language"],
                    expected_sha256=artifact_sha256,
                )
                handle.close()
                if (
                    suffix != verified_suffix
                    or suffix not in {".jpe", ".jpg", ".jpeg", ".png", ".webp"}
                ):
                    raise ValueError("download thumbnail type is not supported")
                registered_candidates.append(
                    (registered, path, suffix, expected_sha256)
                )
        except (KeyError, OSError, TypeError, ValueError):
            raise DownloadAssetReadError("source_cover_unavailable") from None

        if not registered_candidates:
            return None
        if len(registered_candidates) != 1:
            raise DownloadAssetReadError("source_cover_ambiguous")
        registered, path, suffix, expected_sha256 = registered_candidates[0]
        artifact_id = canonical_download_asset_id(registered["artifact_id"])
        upload_suffix = ".jpeg" if suffix == ".jpe" else suffix
        return {
            "artifact_id": artifact_id,
            "asset_id": canonical_asset_id,
            "path": path,
            "sha256": expected_sha256,
            "name": f"download-cover-{artifact_id}{upload_suffix}",
        }

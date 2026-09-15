"""Offline, secret-free backup and restore for the upload data root.

An exclusive upload activity lease and the legacy upload worker lock are held
for the whole backup.  The database is copied from a stable byte snapshot, so
reading a WAL database does not create a SHM file or otherwise open the source
database through SQLite.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping
from uuid import uuid4

from .. import __version__
from .. import backup_files
from ..managed_files import fdopen_owned_binary
from . import schema as _schema
from .activity_lock import UploadActivityBusy, UploadActivityLease, activity_lock_path
from .contracts import (
    PLATFORMS,
    UPLOAD_ATTEMPT_STATES,
    UPLOAD_CODE_PATTERN as _SAFE_CODE,
    UPLOAD_EVIDENCE_KINDS,
    UPLOAD_RECONCILIATIONS,
    UPLOAD_RESULT_STATUSES,
    UploadError,
    is_tencent_short_title_output,
    legacy_migrated_platform_options,
    normalize_tencent_short_title,
    upload_adapter_identity_matches,
)
from .identity import upload_job_definition_digest, upload_retry_payload_matches
from .metadata import (
    DOUYIN_DECLARATIONS,
    TENCENT_CONTENT_LABELS,
    TITLE_LIMITS,
    cover_slot_error,
    cover_dimensions_error,
)
from .receipts import validate_upload_attempt_state
from .covers import COVER_MIME_TYPES, MAX_COVER_BYTES, cover_metadata

UPLOAD_BACKUP_FORMAT_VERSION = 3
_UPLOAD_BACKUP_FORMAT_V2 = 2
UPLOAD_BACKUP_METADATA_NAME = "backup-metadata.json"
UPLOAD_BACKUP_MANIFEST_NAME = "backup-manifest.json"
UPLOAD_BACKUP_MANIFEST_HASH_NAME = "backup-manifest.sha256"
UPLOAD_DATABASE_PAYLOAD_PATH = "payload/uploads.sqlite3"
UPLOAD_MEDIA_PAYLOAD_PREFIX = PurePosixPath("payload/media")
UPLOAD_ASSET_PAYLOAD_PREFIX = PurePosixPath("payload/assets")
# Retain the plural spelling as a discoverable public alias for callers.
UPLOAD_ASSETS_PAYLOAD_PREFIX = UPLOAD_ASSET_PAYLOAD_PREFIX
UPLOAD_DATABASE_NAME = "uploads.sqlite3"
UPLOAD_WORKER_LOCK_NAME = ".worker.lock"
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_BACKUP_ENTRIES = 1_000_000
MAX_SOURCE_BYTES = 2 * 1024**3
_CONSISTENCY = (
    "exclusive_upload_activity_and_worker_plus_stable_sqlite_snapshot"
)

UploadBackupError = backup_files.BackupRestoreError

_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRODUCT_IDENTITY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,95}\+build\.sha256\.[0-9a-f]{64}$"
)
_ADAPTER_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_WORKFLOW_UPLOAD_REQUEST = re.compile(
    r"^wf-[0-9a-f]{32}-upload-jobs(?:-[0-9]{3})?$"
)
_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"})
_EXCLUDED_PATHS = [
    ".open-flame-setup.lock",
    ".worker.lock",
    "incoming",
    "private",
    "runtime",
]
_ACCOUNT_FIELDS = [
    "id",
    "platform",
    "name",
    "auth_state",
    "code",
    "created_at",
    "lifecycle_state",
    "disconnected_at",
]
_PRESERVED_TABLES = [
    "accounts",
    "sources",
    "upload_assets",
    "jobs",
    "operations",
    "requests",
    "upload_attempts",
]
_FORMAT2_PRESERVED_TABLES = [
    "accounts",
    "sources",
    "upload_assets",
    "jobs",
    "operations",
    "requests",
]
_LEGACY_PRESERVED_TABLES = ["accounts", "sources", "jobs", "operations", "requests"]
_FORMAT12_RESTORE_POLICY = {
    "ready_or_checking_account": "unchecked_account_missing",
    "queued_job": "draft_restart_confirmation_required",
    "running_job": "unknown_interrupted_result_unknown",
    "queued_or_running_operation": "failed_operation_interrupted",
}
_RESTORE_POLICY = {
    **_FORMAT12_RESTORE_POLICY,
    # Format 3 is the first format that declares the Schema 4 receipt
    # contract. A running row without that receipt must say exactly why it
    # cannot be reconciled; formats 1/2 retain their immutable historical
    # policy strings and legacy recovery codes.
    "running_job": "unknown_attempt_receipt_missing",
    "reserved_upload_attempt": "responded_failed_dispatch_not_started",
    "dispatch_may_have_started_upload_attempt": "unknown_interrupted_result_unknown",
}
_MANIFEST_KEYS = {"algorithm", "entries", "format_version", "metadata_path"}
_METADATA_KEYS = {
    "account_fields",
    "application_version",
    "asset_payload_prefix",
    "asset_scope",
    "consistency",
    "created_at",
    "database_payload_path",
    "excluded_paths",
    "format_version",
    "media_payload_prefix",
    "media_scope",
    "payload_file_count",
    "payload_total_bytes",
    "preserved_tables",
    "restore_policy",
    "schema_version",
    "secret_material_included",
}
_LEGACY_METADATA_KEYS = _METADATA_KEYS - {"asset_payload_prefix", "asset_scope"}


@dataclass(frozen=True, slots=True)
class UploadBackupResult:
    backup_root: Path
    schema_version: int
    file_count: int
    total_bytes: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class UploadRestoreResult:
    restore_root: Path
    database_path: Path
    schema_version: int
    file_count: int
    total_bytes: int
    manifest_sha256: str


@contextmanager
def _exclusive_upload_activity(root: Path) -> Iterator[None]:
    """Map a nonblocking exclusive activity conflict to the backup API."""

    try:
        lease = UploadActivityLease.acquire(root, exclusive=True)
    except UploadActivityBusy:
        raise UploadBackupError("upload application or operation is active") from None
    try:
        yield
    finally:
        lease.release()


def create_upload_backup(
    *,
    source_root: Path,
    backup_target: Path,
) -> UploadBackupResult:
    """Create one atomic backup while all upload mutation is quiescent."""

    root_candidate = backup_files.require_existing_directory(
        source_root,
        label="upload source root",
    )
    target_candidate = backup_files.require_new_target(
        backup_target,
        label="upload backup target",
    )
    if target_candidate == activity_lock_path(root_candidate):
        raise UploadBackupError("upload backup target conflicts with activity lock")
    try:
        with _exclusive_upload_activity(root_candidate):
            root = backup_files.require_existing_directory(
                root_candidate,
                label="upload source root",
            )
            target = backup_files.require_new_target(
                target_candidate,
                label="upload backup target",
            )
            if target == activity_lock_path(root):
                raise UploadBackupError(
                    "upload backup target conflicts with activity lock"
                )
            if backup_files.paths_overlap(root, target):
                raise UploadBackupError("upload backup target overlaps its source")
            with _exclusive_upload_worker(root):
                return _create_upload_backup_locked(
                    source_root=root,
                    backup_target=target,
                )
    except UploadBackupError:
        raise
    except _schema.UploadSchemaError as exc:
        raise UploadBackupError("upload database schema is invalid") from exc
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise UploadBackupError("upload backup operation failed") from exc


def _create_upload_backup_locked(
    *,
    source_root: Path,
    backup_target: Path,
) -> UploadBackupResult:
    """Create after the activity and worker leases have both been acquired."""

    root = backup_files.require_existing_directory(source_root, label="upload source root")
    database = backup_files.require_existing_regular_file(
        root / UPLOAD_DATABASE_NAME,
        label="upload source database",
    )
    media_root = backup_files.require_existing_directory(
        root / "media",
        label="upload media root",
    )
    asset_root = backup_files.require_existing_directory(
        root / "assets",
        label="upload asset root",
    )
    target = backup_files.require_new_target(backup_target, label="upload backup target")
    source_activity_path = activity_lock_path(root)
    if target == source_activity_path:
        raise UploadBackupError("upload backup target conflicts with activity lock")
    if backup_files.paths_overlap(root, target):
        raise UploadBackupError("upload backup target overlaps its source")

    stage = target.parent / f".{target.name}.partial-{uuid4().hex}"
    if stage == source_activity_path:
        raise UploadBackupError("upload backup staging path conflicts with activity lock")
    if os.path.lexists(stage):
        raise UploadBackupError("upload backup staging path collision")
    try:
        stage.mkdir(mode=0o700)
        payload_database = stage.joinpath(
            *PurePosixPath(UPLOAD_DATABASE_PAYLOAD_PATH).parts
        )
        payload_database.parent.mkdir(parents=True)
        payload_media = stage.joinpath(*UPLOAD_MEDIA_PAYLOAD_PREFIX.parts)
        payload_media.mkdir(parents=True)
        payload_assets = stage.joinpath(*UPLOAD_ASSET_PAYLOAD_PREFIX.parts)
        payload_assets.mkdir(parents=True)

        _snapshot_upload_database(database, payload_database)
        payload_entries = [
            backup_files.entry_for_file(
                payload_database,
                relative_path=UPLOAD_DATABASE_PAYLOAD_PATH,
            )
        ]
        payload_entries.extend(
            _copy_registered_media(
                database_path=payload_database,
                source_media_root=media_root,
                destination_media_root=payload_media,
            )
        )
        payload_entries.extend(
            _copy_registered_assets(
                database_path=payload_database,
                source_asset_root=asset_root,
                destination_asset_root=payload_assets,
            )
        )
        _audit_database_and_media(
            database_path=payload_database,
            media_root=payload_media,
            asset_root=payload_assets,
        )

        payload_entries.sort(key=lambda entry: entry.path.casefold())
        payload_total = sum(entry.size_bytes for entry in payload_entries)
        metadata = {
            "format_version": UPLOAD_BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "application_version": __version__,
            "schema_version": _schema.SCHEMA_VERSION,
            "database_payload_path": UPLOAD_DATABASE_PAYLOAD_PATH,
            "media_payload_prefix": UPLOAD_MEDIA_PAYLOAD_PREFIX.as_posix(),
            "asset_payload_prefix": UPLOAD_ASSET_PAYLOAD_PREFIX.as_posix(),
            "payload_file_count": len(payload_entries),
            "payload_total_bytes": payload_total,
            "consistency": _CONSISTENCY,
            "excluded_paths": _EXCLUDED_PATHS,
            "media_scope": "registered_present_media_only",
            "asset_scope": "registered_present_cover_assets_only",
            "secret_material_included": False,
            "account_fields": _ACCOUNT_FIELDS,
            "preserved_tables": _PRESERVED_TABLES,
            "restore_policy": _RESTORE_POLICY,
        }
        metadata_path = stage / UPLOAD_BACKUP_METADATA_NAME
        backup_files.write_json_exclusive(metadata_path, metadata)
        entries = [
            backup_files.entry_for_file(
                metadata_path,
                relative_path=UPLOAD_BACKUP_METADATA_NAME,
            ),
            *payload_entries,
        ]
        entries.sort(key=lambda entry: entry.path.casefold())
        manifest = {
            "format_version": UPLOAD_BACKUP_FORMAT_VERSION,
            "algorithm": "sha256",
            "metadata_path": UPLOAD_BACKUP_METADATA_NAME,
            "entries": [
                {
                    "path": entry.path,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                }
                for entry in entries
            ],
        }
        manifest_path = stage / UPLOAD_BACKUP_MANIFEST_NAME
        backup_files.write_json_exclusive(manifest_path, manifest)
        manifest_sha256 = backup_files.sha256_regular_file(manifest_path)
        backup_files.write_text_exclusive(
            stage / UPLOAD_BACKUP_MANIFEST_HASH_NAME,
            manifest_sha256 + "\n",
        )
        _assert_no_windows_named_streams(stage)
        backup_files.sync_tree(stage)
        if os.path.lexists(target):
            raise UploadBackupError("upload backup target already exists")
        backup_files.publish_directory(stage, target)
        return UploadBackupResult(
            backup_root=target,
            schema_version=_schema.SCHEMA_VERSION,
            file_count=len(entries),
            total_bytes=sum(entry.size_bytes for entry in entries),
            manifest_sha256=manifest_sha256,
        )
    except UploadBackupError:
        raise
    except _schema.UploadSchemaError as exc:
        raise UploadBackupError("upload database schema is invalid") from exc
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise UploadBackupError("upload backup operation failed") from exc
    finally:
        backup_files.cleanup_stage(stage)


def restore_upload_backup(
    *,
    backup_root: Path,
    restore_root: Path,
) -> UploadRestoreResult:
    """Verify and restore while exclusively reserving the new upload root."""

    source_candidate = backup_files.require_existing_directory(
        backup_root,
        label="upload backup root",
    )
    target_candidate = backup_files.require_new_target(
        restore_root,
        label="upload restore target",
    )
    source_activity_path = activity_lock_path(source_candidate)
    target_activity_path = activity_lock_path(target_candidate)
    if backup_files.paths_overlap(source_candidate, target_candidate):
        raise UploadBackupError("upload restore target overlaps its backup")
    if (
        source_candidate == target_activity_path
        or target_candidate == source_activity_path
    ):
        raise UploadBackupError("upload restore path conflicts with activity lock")
    try:
        with _exclusive_upload_activity(target_candidate):
            source = backup_files.require_existing_directory(
                source_candidate,
                label="upload backup root",
            )
            target = backup_files.require_new_target(
                target_candidate,
                label="upload restore target",
            )
            if (
                source == activity_lock_path(target)
                or target == activity_lock_path(source)
            ):
                raise UploadBackupError(
                    "upload restore path conflicts with activity lock"
                )
            if backup_files.paths_overlap(source, target):
                raise UploadBackupError("upload restore target overlaps its backup")
            return _restore_upload_backup_locked(
                backup_root=source,
                restore_root=target,
            )
    except UploadBackupError:
        raise
    except _schema.UploadSchemaError as exc:
        raise UploadBackupError("upload backup database schema is invalid") from exc
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise UploadBackupError("upload restore operation failed") from exc


def _restore_upload_backup_locked(
    *,
    backup_root: Path,
    restore_root: Path,
) -> UploadRestoreResult:
    """Restore after the destination activity lease has been acquired."""

    source = backup_files.require_existing_directory(backup_root, label="upload backup root")
    target = backup_files.require_new_target(restore_root, label="upload restore target")
    source_activity_path = activity_lock_path(source)
    target_activity_path = activity_lock_path(target)
    if source == target_activity_path or target == source_activity_path:
        raise UploadBackupError("upload restore path conflicts with activity lock")
    if backup_files.paths_overlap(source, target):
        raise UploadBackupError("upload restore target overlaps its backup")

    stage = target.parent / f".{target.name}.partial-{uuid4().hex}"
    if stage in {source_activity_path, target_activity_path}:
        raise UploadBackupError("upload restore staging path conflicts with activity lock")
    if os.path.lexists(stage):
        raise UploadBackupError("upload restore staging path collision")
    try:
        manifest_sha256, entries, metadata = _load_and_verify_backup(source)
        format_version = metadata["format_version"]
        payload_entries = [
            entry for entry in entries if entry.path != UPLOAD_BACKUP_METADATA_NAME
        ]
        total_bytes = sum(entry.size_bytes for entry in payload_entries)
        if shutil.disk_usage(target.parent).free < total_bytes:
            raise UploadBackupError("insufficient free space for upload restore")
        stage.mkdir(mode=0o700)
        (stage / "media").mkdir()
        (stage / "assets").mkdir()
        restored_paths: set[str] = set()
        for entry in payload_entries:
            source_file = source.joinpath(*PurePosixPath(entry.path).parts)
            if entry.path == UPLOAD_DATABASE_PAYLOAD_PATH:
                relative = Path(UPLOAD_DATABASE_NAME)
            else:
                pure = PurePosixPath(entry.path)
                try:
                    media_relative = pure.relative_to(UPLOAD_MEDIA_PAYLOAD_PREFIX)
                except ValueError:
                    try:
                        asset_relative = pure.relative_to(UPLOAD_ASSET_PAYLOAD_PREFIX)
                    except ValueError as exc:
                        raise UploadBackupError(
                            "upload backup payload path is unexpected"
                        ) from exc
                    if format_version not in {
                        _UPLOAD_BACKUP_FORMAT_V2,
                        UPLOAD_BACKUP_FORMAT_VERSION,
                    }:
                        raise UploadBackupError("upload backup payload path is unexpected")
                    if len(asset_relative.parts) != 1:
                        raise UploadBackupError("upload backup asset path is invalid")
                    relative = Path("assets", *asset_relative.parts)
                else:
                    if len(media_relative.parts) != 1:
                        raise UploadBackupError("upload backup media path is invalid")
                    relative = Path("media", *media_relative.parts)
            key = _path_key(relative.as_posix())
            if key in restored_paths:
                raise UploadBackupError("upload restore destination path collision")
            restored_paths.add(key)
            destination = stage / relative
            digest, size = backup_files.copy_regular_file(
                source_file,
                destination,
                expected_size=entry.size_bytes,
                expected_sha256=entry.sha256,
            )
            if digest != entry.sha256 or size != entry.size_bytes:
                raise UploadBackupError("upload restored file verification failed")

        restored_database = stage / UPLOAD_DATABASE_NAME
        if format_version in {1, _UPLOAD_BACKUP_FORMAT_V2}:
            source_schema_version = (
                2 if format_version == 1 else _schema._SCHEMA_V3_VERSION
            )
            _audit_database_and_media(
                database_path=restored_database,
                media_root=stage / "media",
                asset_root=(
                    None if format_version == 1 else stage / "assets"
                ),
                schema_version=source_schema_version,
            )
            original_job_states = _job_states(restored_database)
            _schema.ensure_upload_schema(restored_database)
        _audit_database_and_media(
            database_path=restored_database,
            media_root=stage / "media",
            asset_root=stage / "assets",
        )
        if format_version == UPLOAD_BACKUP_FORMAT_VERSION:
            original_job_states = _job_states(restored_database)
        _apply_restore_state_policy(restored_database, original_job_states)
        if format_version == UPLOAD_BACKUP_FORMAT_VERSION:
            _mark_current_missing_receipts(
                restored_database,
                original_job_states,
            )
        _audit_database_and_media(
            database_path=restored_database,
            media_root=stage / "media",
            asset_root=stage / "assets",
        )
        _assert_no_windows_named_streams(stage)
        backup_files.sync_tree(stage)
        if os.path.lexists(target):
            raise UploadBackupError("upload restore target already exists")
        backup_files.publish_directory(stage, target)
        return UploadRestoreResult(
            restore_root=target,
            database_path=target / UPLOAD_DATABASE_NAME,
            schema_version=_schema.SCHEMA_VERSION,
            file_count=len(payload_entries),
            total_bytes=total_bytes,
            manifest_sha256=manifest_sha256,
        )
    except UploadBackupError:
        raise
    except _schema.UploadSchemaError as exc:
        raise UploadBackupError("upload backup database schema is invalid") from exc
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise UploadBackupError("upload restore operation failed") from exc
    finally:
        backup_files.cleanup_stage(stage)


@contextmanager
def _exclusive_upload_worker(root: Path) -> Iterator[None]:
    """Take the same lock byte/flock used by ``UploadService.start``."""

    path = root / UPLOAD_WORKER_LOCK_NAME
    backup_files.assert_existing_ancestors_no_links(path.parent)
    try:
        path = backup_files.require_existing_regular_file(
            path,
            label="upload worker lock",
        )
    except UploadBackupError as exc:
        raise UploadBackupError("upload worker lock is unavailable") from exc
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        handle = fdopen_owned_binary(descriptor, "r+b")
    except OSError as exc:
        raise UploadBackupError("upload worker lock is unavailable") from exc
    locked = False
    try:
        # POSIX flock does not need a lock byte. UploadService creates an empty
        # file there; imported Windows roots retain their one-byte marker.
        markers = (b"0",) if os.name == "nt" else (b"", b"0")
        opened = os.fstat(handle.fileno())
        current = backup_files.safe_lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or backup_files.is_link_or_reparse(path, current)
            or not _same_identity(opened, current)
            or opened.st_size not in {len(marker) for marker in markers}
        ):
            raise UploadBackupError("upload worker lock is unsafe")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UploadBackupError("upload worker is active") from exc
        locked = True
        handle.seek(0)
        if handle.read(2) not in markers:
            raise UploadBackupError("upload worker lock is invalid")
        yield
    finally:
        if locked:
            with suppress(OSError):
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _snapshot_upload_database(source: Path, destination: Path) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="open-flame-upload-backup-") as temporary:
            snapshot = Path(temporary) / UPLOAD_DATABASE_NAME
            for attempt in range(_schema._SNAPSHOT_ATTEMPTS):
                try:
                    _schema._copy_stable_snapshot(source, snapshot)
                    break
                except _schema._SnapshotChanged:
                    snapshot.unlink(missing_ok=True)
                    for suffix in ("-wal", "-shm"):
                        Path(str(snapshot) + suffix).unlink(missing_ok=True)
                    if attempt + 1 == _schema._SNAPSHOT_ATTEMPTS:
                        raise UploadBackupError("upload database changed during snapshot") from None
                    time.sleep(_schema._SNAPSHOT_RETRY_SECONDS)
            _schema._validate_snapshot_wal(snapshot)
            _schema.validate_upload_schema(snapshot)
            backup_files.sqlite_online_backup(snapshot, destination)
        _schema.validate_upload_schema(destination)
    except (_schema.UploadSchemaError, sqlite3.Error) as exc:
        raise UploadBackupError("upload database schema or snapshot is invalid") from exc


def _copy_registered_media(
    *,
    database_path: Path,
    source_media_root: Path,
    destination_media_root: Path,
) -> list[backup_files.BackupFileEntry]:
    entries: list[backup_files.BackupFileEntry] = []
    with _database(database_path) as db:
        rows = list(db.execute(
            "SELECT id,name,suffix,size,sha256,created_at,media_state,deleted_at "
            "FROM sources ORDER BY id"
        ))
    seen: set[str] = set()
    for row in rows:
        name = _validate_source_row(row)
        key = name.casefold()
        if key in seen:
            raise UploadBackupError("upload source media names collide")
        seen.add(key)
        source = source_media_root / name
        if row["media_state"] != "present":
            if os.path.lexists(source):
                raise UploadBackupError("non-present upload media still exists")
            continue
        destination = destination_media_root / name
        digest, size = backup_files.copy_regular_file(
            source,
            destination,
            expected_size=row["size"],
            expected_sha256=row["sha256"],
        )
        entries.append(backup_files.BackupFileEntry(
            path=(UPLOAD_MEDIA_PAYLOAD_PREFIX / name).as_posix(),
            size_bytes=size,
            sha256=digest,
        ))
    return entries


def _copy_registered_assets(
    *,
    database_path: Path,
    source_asset_root: Path,
    destination_asset_root: Path,
) -> list[backup_files.BackupFileEntry]:
    entries: list[backup_files.BackupFileEntry] = []
    with _database(database_path) as db:
        rows = list(db.execute(
            "SELECT id,kind,name,suffix,mime_type,size,sha256,width,height,created_at,"
            "media_state,deleted_at FROM upload_assets ORDER BY id"
        ))
    seen: set[str] = set()
    for row in rows:
        name = _validate_asset_row(row)
        key = name.casefold()
        if key in seen:
            raise UploadBackupError("upload cover asset names collide")
        seen.add(key)
        source = source_asset_root / name
        if row["media_state"] != "present":
            if os.path.lexists(source):
                raise UploadBackupError("non-present upload cover asset still exists")
            continue
        destination = destination_asset_root / name
        digest, size = backup_files.copy_regular_file(
            source,
            destination,
            expected_size=row["size"],
            expected_sha256=row["sha256"],
        )
        _verify_cover_asset_file(destination, row)
        entries.append(backup_files.BackupFileEntry(
            path=(UPLOAD_ASSET_PAYLOAD_PREFIX / name).as_posix(),
            size_bytes=size,
            sha256=digest,
        ))
    return entries


def _audit_database_and_media(
    *,
    database_path: Path,
    media_root: Path,
    asset_root: Path | None = None,
    schema_version: int | None = None,
) -> None:
    version = _schema.SCHEMA_VERSION if schema_version is None else schema_version
    if version == _schema.SCHEMA_VERSION:
        _schema.validate_upload_schema(database_path)
    elif version in {2, _schema._SCHEMA_V3_VERSION}:
        _schema._validated_schema_version(database_path, frozenset({version}))
    else:
        raise UploadBackupError("upload backup database schema is invalid")
    backup_files.require_existing_directory(media_root, label="upload backup media root")
    if version >= _schema._SCHEMA_V3_VERSION:
        if asset_root is None:
            raise UploadBackupError("upload backup asset root is missing")
        backup_files.require_existing_directory(asset_root, label="upload backup asset root")
    with _database(database_path) as db:
        rows = list(db.execute(
            "SELECT id,name,suffix,size,sha256,created_at,media_state,deleted_at "
            "FROM sources ORDER BY id"
        ))
        asset_rows = list(db.execute(
            "SELECT id,kind,name,suffix,mime_type,size,sha256,width,height,created_at,"
            "media_state,deleted_at FROM upload_assets ORDER BY id"
        )) if version >= _schema._SCHEMA_V3_VERSION else []
        if list(db.execute("PRAGMA foreign_key_check")):
            raise UploadBackupError("upload backup foreign keys are invalid")
        for asset_row in asset_rows:
            _validate_asset_row(asset_row)
        _audit_database_rows(db, schema_version=version)
    expected: set[str] = set()
    for row in rows:
        name = _validate_source_row(row)
        path = media_root / name
        if row["media_state"] == "present":
            backup_files.verify_regular_file(
                path,
                expected_size=row["size"],
                expected_sha256=row["sha256"],
            )
            expected.add(name.casefold())
        elif os.path.lexists(path):
            raise UploadBackupError("non-present upload media is included")
    actual: set[str] = set()
    for entry in os.scandir(media_root):
        path = Path(entry.path)
        info = backup_files.safe_lstat(path)
        if backup_files.is_link_or_reparse(path, info) or not stat.S_ISREG(info.st_mode):
            raise UploadBackupError("upload backup media contains an unsafe entry")
        key = entry.name.casefold()
        if key in actual:
            raise UploadBackupError("upload backup media names collide")
        actual.add(key)
    if actual != expected:
        raise UploadBackupError("upload backup media inventory does not match its database")
    if asset_root is None:
        return
    expected_assets: set[str] = set()
    for row in asset_rows:
        name = _validate_asset_row(row)
        path = asset_root / name
        if row["media_state"] == "present":
            backup_files.verify_regular_file(
                path,
                expected_size=row["size"],
                expected_sha256=row["sha256"],
            )
            _verify_cover_asset_file(path, row)
            expected_assets.add(name.casefold())
        elif os.path.lexists(path):
            raise UploadBackupError("non-present upload cover asset is included")
    actual_assets: set[str] = set()
    for entry in os.scandir(asset_root):
        path = Path(entry.path)
        info = backup_files.safe_lstat(path)
        if backup_files.is_link_or_reparse(path, info) or not stat.S_ISREG(info.st_mode):
            raise UploadBackupError("upload backup assets contain an unsafe entry")
        key = entry.name.casefold()
        if key in actual_assets:
            raise UploadBackupError("upload cover asset names collide")
        actual_assets.add(key)
    if actual_assets != expected_assets:
        raise UploadBackupError("upload backup asset inventory does not match its database")


def _validate_source_row(row: sqlite3.Row) -> str:
    source_id = row["id"]
    original_name = row["name"]
    suffix = row["suffix"]
    size = row["size"]
    digest = row["sha256"]
    state = row["media_state"]
    deleted_at = row["deleted_at"]
    if (
        not isinstance(source_id, str)
        or _ID.fullmatch(source_id) is None
        or not _is_normalized_text(original_name, 180, required=True)
        or any(character in original_name for character in "/\\:\x00")
        or original_name in {".", ".."}
        or not isinstance(suffix, str)
        or suffix not in _SUFFIXES
        or Path(original_name).suffix.lower() != suffix
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= MAX_SOURCE_BYTES
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or not _is_timestamp(row["created_at"])
        or state not in {"present", "missing", "deleted"}
        or (state == "deleted" and not _is_timestamp(deleted_at))
        or (state != "deleted" and deleted_at is not None)
    ):
        raise UploadBackupError("upload source metadata is invalid")
    return source_id + suffix


def _validate_asset_row(row: sqlite3.Row) -> str:
    asset_id = row["id"]
    original_name = row["name"]
    suffix = row["suffix"]
    mime_type = row["mime_type"]
    size = row["size"]
    digest = row["sha256"]
    width = row["width"]
    height = row["height"]
    state = row["media_state"]
    deleted_at = row["deleted_at"]
    if (
        not isinstance(asset_id, str)
        or _ID.fullmatch(asset_id) is None
        or row["kind"] != "cover"
        or not _is_normalized_text(original_name, 180, required=True)
        or any(character in original_name for character in "/\\:\x00")
        or original_name in {".", ".."}
        or not isinstance(suffix, str)
        or suffix not in COVER_MIME_TYPES
        or Path(original_name).suffix.lower() != suffix
        or mime_type != COVER_MIME_TYPES[suffix]
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= MAX_COVER_BYTES
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or isinstance(width, bool)
        or not isinstance(width, int)
        or not 1 <= width <= 32768
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 1 <= height <= 32768
        or not _is_timestamp(row["created_at"])
        or state not in {"present", "missing", "deleted"}
        or (state == "deleted" and not _is_timestamp(deleted_at))
        or (state != "deleted" and deleted_at is not None)
    ):
        raise UploadBackupError("upload cover asset metadata is invalid")
    return asset_id + suffix


def _verify_cover_asset_file(path: Path, row: sqlite3.Row) -> None:
    try:
        payload = backup_files.read_bounded_regular_file(path, MAX_COVER_BYTES + 1)
        mime_type, width, height = cover_metadata(payload, row["suffix"])
    except (UploadError, OSError) as exc:
        raise UploadBackupError("upload cover asset content is invalid") from exc
    if (
        len(payload) != row["size"]
        or mime_type != row["mime_type"]
        or width != row["width"]
        or height != row["height"]
    ):
        raise UploadBackupError("upload cover asset content is invalid")


def _job_states(database_path: Path) -> dict[str, str]:
    db = sqlite3.connect(database_path)
    try:
        return dict(db.execute("SELECT id,state FROM jobs"))
    finally:
        db.close()


def _apply_restore_state_policy(
    database_path: Path,
    original_job_states: Mapping[str, str],
) -> None:
    _schema.validate_upload_schema(database_path)
    now = datetime.now(UTC).isoformat()
    db = sqlite3.connect(database_path)
    try:
        try:
            db.row_factory = sqlite3.Row
            mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()
            if mode is None or str(mode[0]).lower() != "delete":
                raise UploadBackupError("restored upload database journal mode is unsafe")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for job_id, original_state in original_job_states.items():
                if original_state not in {"queued", "running"}:
                    continue
                current = db.execute(
                    "SELECT code FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if current is None:
                    raise UploadBackupError("restored upload job is missing")
                legacy = isinstance(current[0], str) and current[0].startswith("legacy_")
                if original_state == "running":
                    attempt = db.execute(
                        "SELECT id,state,result_code,revision FROM upload_attempts "
                        "WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                    if attempt is not None and attempt["state"] == "reserved":
                        code = "upload_dispatch_not_started"
                        changed = db.execute(
                            "UPDATE upload_attempts SET state='responded',"
                            "result_status='failed',result_code=?,evidence_kind=NULL,"
                            "responded_at=?,revision=revision+1 "
                            "WHERE id=? AND state='reserved' AND revision=?",
                            (code, now, attempt["id"], attempt["revision"]),
                        ).rowcount
                        if changed != 1:
                            raise UploadBackupError(
                                "restored upload attempt transition failed"
                            )
                        state = "failed"
                    elif (
                        attempt is not None
                        and attempt["state"] == "dispatch_may_have_started"
                    ):
                        code = "interrupted_result_unknown"
                        changed = db.execute(
                            "UPDATE upload_attempts SET state='unknown',"
                            "result_status='unknown',result_code=?,evidence_kind=NULL,"
                            "revision=revision+1 WHERE id=? "
                            "AND state='dispatch_may_have_started' AND revision=?",
                            (code, attempt["id"], attempt["revision"]),
                        ).rowcount
                        if changed != 1:
                            raise UploadBackupError(
                                "restored upload attempt transition failed"
                            )
                        state = "unknown"
                    else:
                        state = "unknown"
                        code = (
                            attempt["result_code"]
                            if attempt is not None
                            and attempt["state"] == "unknown"
                            and _is_safe_code(attempt["result_code"])
                            else "legacy_metadata_interrupted_result_unknown"
                            if legacy
                            else "interrupted_result_unknown"
                        )
                else:
                    state = "draft"
                    code = (
                        "legacy_metadata_restart_confirmation_required"
                        if legacy else "restart_confirmation_required"
                    )
                db.execute(
                    "UPDATE jobs SET state=?,code=?,updated_at=? WHERE id=?",
                    (state, code, now, job_id),
                )
            db.execute(
                "UPDATE operations SET state='failed',code='operation_interrupted',"
                "updated_at=? WHERE state IN ('queued','running')",
                (now,),
            )
            db.execute(
                "UPDATE accounts SET auth_state='unchecked',code='account_missing' "
                "WHERE lifecycle_state='active' AND auth_state IN ('ready','checking')"
            )
            if list(db.execute("PRAGMA foreign_key_check")):
                raise UploadBackupError("restored upload database foreign keys are invalid")
            db.commit()
        except Exception:
            with suppress(sqlite3.Error):
                db.rollback()
            raise
    finally:
        db.close()
    _schema.validate_upload_schema(database_path)


def _mark_current_missing_receipts(
    database_path: Path,
    original_job_states: Mapping[str, str],
) -> None:
    """Give format-3 running rows the precise missing-receipt reason."""

    _schema.validate_upload_schema(database_path)
    db = sqlite3.connect(database_path)
    try:
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for job_id, original_state in original_job_states.items():
                if original_state != "running":
                    continue
                row = db.execute(
                    "SELECT j.state,j.code,u.id AS attempt_id FROM jobs j "
                    "LEFT JOIN upload_attempts u ON u.job_id=j.id WHERE j.id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise UploadBackupError("restored upload job is missing")
                if row["attempt_id"] is not None:
                    continue
                if row["state"] != "unknown" or row["code"] not in {
                    "interrupted_result_unknown",
                    "legacy_metadata_interrupted_result_unknown",
                    "attempt_receipt_missing",
                }:
                    raise UploadBackupError(
                        "restored upload missing-receipt state is invalid"
                    )
                changed = db.execute(
                    "UPDATE jobs SET code='attempt_receipt_missing' "
                    "WHERE id=? AND state='unknown' AND NOT EXISTS "
                    "(SELECT 1 FROM upload_attempts WHERE job_id=jobs.id)",
                    (job_id,),
                ).rowcount
                if changed != 1:
                    raise UploadBackupError(
                        "restored upload missing-receipt transition failed"
                    )
            if list(db.execute("PRAGMA foreign_key_check")):
                raise UploadBackupError(
                    "restored upload database foreign keys are invalid"
                )
            db.commit()
        except Exception:
            with suppress(sqlite3.Error):
                db.rollback()
            raise
    finally:
        db.close()
    _schema.validate_upload_schema(database_path)


def _load_and_verify_backup(
    root: Path,
) -> tuple[str, tuple[backup_files.BackupFileEntry, ...], Mapping[str, object]]:
    _assert_no_windows_named_streams(root)
    hash_path = root / UPLOAD_BACKUP_MANIFEST_HASH_NAME
    manifest_path = root / UPLOAD_BACKUP_MANIFEST_NAME
    try:
        hash_text = backup_files.read_bounded_regular_file(hash_path, 128).decode(
            "ascii", errors="strict"
        )
    except UnicodeDecodeError as exc:
        raise UploadBackupError("upload manifest hash record is invalid") from exc
    if re.fullmatch(r"[0-9a-f]{64}\n", hash_text) is None:
        raise UploadBackupError("upload manifest hash record is invalid")
    expected_hash = hash_text.strip()
    if backup_files.sha256_regular_file(manifest_path) != expected_hash:
        raise UploadBackupError("upload manifest hash mismatch")
    manifest = backup_files.read_json_mapping(manifest_path, MAX_MANIFEST_BYTES)
    format_version = manifest.get("format_version")
    if (
        set(manifest) != _MANIFEST_KEYS
        or type(format_version) is not int
        or format_version
        not in {1, _UPLOAD_BACKUP_FORMAT_V2, UPLOAD_BACKUP_FORMAT_VERSION}
        or manifest["algorithm"] != "sha256"
        or manifest["metadata_path"] != UPLOAD_BACKUP_METADATA_NAME
    ):
        raise UploadBackupError("upload manifest header is invalid")
    raw_entries = manifest["entries"]
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= MAX_BACKUP_ENTRIES:
        raise UploadBackupError("upload manifest entry count is invalid")
    entries: list[backup_files.BackupFileEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "size_bytes"}:
            raise UploadBackupError("upload manifest entry is invalid")
        relative = backup_files.validated_relative_path(raw["path"])
        _validate_manifest_entry_path(relative, format_version=format_version)
        size = raw["size_bytes"]
        digest = raw["sha256"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise UploadBackupError("upload manifest entry is invalid")
        key = _path_key(relative)
        if key in seen:
            raise UploadBackupError("upload manifest paths collide")
        seen.add(key)
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        backup_files.verify_regular_file(
            candidate,
            expected_size=size,
            expected_sha256=digest,
        )
        entries.append(backup_files.BackupFileEntry(relative, size, digest))
    if entries != sorted(entries, key=lambda entry: entry.path.casefold()):
        raise UploadBackupError("upload manifest entries are not canonical")
    expected_files = {
        *(entry.path.casefold() for entry in entries),
        UPLOAD_BACKUP_MANIFEST_NAME.casefold(),
        UPLOAD_BACKUP_MANIFEST_HASH_NAME.casefold(),
    }
    actual_paths = backup_files.scan_backup_files(root)
    actual_files = {path.casefold() for path in actual_paths}
    if len(actual_files) != len(actual_paths):
        raise UploadBackupError("upload backup paths collide")
    if actual_files != expected_files:
        raise UploadBackupError("upload backup contains untracked or missing files")
    metadata_entry = next(
        (entry for entry in entries if entry.path == UPLOAD_BACKUP_METADATA_NAME),
        None,
    )
    if metadata_entry is None:
        raise UploadBackupError("upload backup metadata is not covered by its manifest")
    metadata = backup_files.read_json_mapping(
        root / UPLOAD_BACKUP_METADATA_NAME,
        MAX_METADATA_BYTES,
    )
    _validate_metadata(metadata, entries, format_version=format_version)
    return expected_hash, tuple(entries), metadata


def _assert_no_windows_named_streams(root: Path) -> None:
    """Reject NTFS alternate data streams anywhere in a backup tree."""

    if os.name != "nt":
        return

    def walk(directory: Path) -> None:
        _assert_windows_path_has_only_default_stream(directory)
        for entry in os.scandir(directory):
            path = Path(entry.path)
            info = backup_files.safe_lstat(path)
            if backup_files.is_link_or_reparse(path, info):
                raise UploadBackupError("upload backup contains a link or reparse point")
            _assert_windows_path_has_only_default_stream(path)
            if stat.S_ISDIR(info.st_mode):
                walk(path)
            elif not stat.S_ISREG(info.st_mode):
                raise UploadBackupError("upload backup contains a special file")

    walk(root)


def _assert_windows_path_has_only_default_stream(path: Path) -> None:
    from ctypes import wintypes

    class _FindStreamData(ctypes.Structure):
        _fields_ = [
            ("stream_size", ctypes.c_longlong),
            ("stream_name", wintypes.WCHAR * 296),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(_FindStreamData),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FindStreamData)]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    absolute = os.path.abspath(path)
    if not absolute.startswith("\\\\?\\"):
        if absolute.startswith("\\\\"):
            absolute = "\\\\?\\UNC\\" + absolute[2:]
        else:
            absolute = "\\\\?\\" + absolute
    data = _FindStreamData()
    handle = find_first(absolute, 0, ctypes.byref(data), 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        if ctypes.get_last_error() == 38:  # ERROR_HANDLE_EOF: no data streams.
            return
        raise UploadBackupError("upload backup alternate stream audit failed")
    try:
        while True:
            if data.stream_name != "::$DATA":
                raise UploadBackupError("upload backup contains an alternate data stream")
            if find_next(handle, ctypes.byref(data)):
                continue
            if ctypes.get_last_error() in {18, 38}:  # NO_MORE_FILES / HANDLE_EOF.
                break
            raise UploadBackupError("upload backup alternate stream audit failed")
    finally:
        find_close(handle)


def _validate_metadata(
    metadata: Mapping[str, object],
    entries: list[backup_files.BackupFileEntry],
    *,
    format_version: int,
) -> None:
    payload = [entry for entry in entries if entry.path != UPLOAD_BACKUP_METADATA_NAME]
    created_at = metadata.get("created_at")
    schema_version = metadata.get("schema_version")
    payload_file_count = metadata.get("payload_file_count")
    payload_total_bytes = metadata.get("payload_total_bytes")
    try:
        parsed = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
    except ValueError:
        parsed = None
    has_assets = format_version >= _UPLOAD_BACKUP_FORMAT_V2
    expected_keys = _METADATA_KEYS if has_assets else _LEGACY_METADATA_KEYS
    expected_schema = {
        1: 2,
        _UPLOAD_BACKUP_FORMAT_V2: _schema._SCHEMA_V3_VERSION,
        UPLOAD_BACKUP_FORMAT_VERSION: _schema.SCHEMA_VERSION,
    }[format_version]
    expected_tables = {
        1: _LEGACY_PRESERVED_TABLES,
        _UPLOAD_BACKUP_FORMAT_V2: _FORMAT2_PRESERVED_TABLES,
        UPLOAD_BACKUP_FORMAT_VERSION: _PRESERVED_TABLES,
    }[format_version]
    expected_restore_policy = (
        _RESTORE_POLICY
        if format_version == UPLOAD_BACKUP_FORMAT_VERSION
        else _FORMAT12_RESTORE_POLICY
    )
    if (
        set(metadata) != expected_keys
        or type(metadata.get("format_version")) is not int
        or metadata.get("format_version") != format_version
        or type(schema_version) is not int
        or schema_version != expected_schema
        or metadata.get("database_payload_path") != UPLOAD_DATABASE_PAYLOAD_PATH
        or metadata.get("media_payload_prefix") != UPLOAD_MEDIA_PAYLOAD_PREFIX.as_posix()
        or (
            has_assets
            and metadata.get("asset_payload_prefix")
            != UPLOAD_ASSET_PAYLOAD_PREFIX.as_posix()
        )
        or type(payload_file_count) is not int
        or payload_file_count != len(payload)
        or type(payload_total_bytes) is not int
        or payload_total_bytes != sum(entry.size_bytes for entry in payload)
        or metadata.get("consistency") != _CONSISTENCY
        or metadata.get("excluded_paths") != _EXCLUDED_PATHS
        or metadata.get("media_scope") != "registered_present_media_only"
        or (
            has_assets
            and metadata.get("asset_scope")
            != "registered_present_cover_assets_only"
        )
        or metadata.get("secret_material_included") is not False
        or metadata.get("account_fields") != _ACCOUNT_FIELDS
        or metadata.get("preserved_tables") != expected_tables
        or metadata.get("restore_policy") != expected_restore_policy
        or not isinstance(metadata.get("application_version"), str)
        or not metadata.get("application_version")
        or parsed is None
        or parsed.tzinfo is None
    ):
        raise UploadBackupError("upload backup metadata is invalid")
    paths = {entry.path for entry in payload}
    if UPLOAD_DATABASE_PAYLOAD_PATH not in paths:
        raise UploadBackupError("upload database payload is missing")
    if any(
        path != UPLOAD_DATABASE_PAYLOAD_PATH
        and not PurePosixPath(path).is_relative_to(UPLOAD_MEDIA_PAYLOAD_PREFIX)
        and not (
            has_assets
            and PurePosixPath(path).is_relative_to(UPLOAD_ASSET_PAYLOAD_PREFIX)
        )
        for path in paths
    ):
        raise UploadBackupError("upload backup payload path is unexpected")


@contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def _audit_database_rows(db: sqlite3.Connection, *, schema_version: int) -> None:
    accounts = list(db.execute(
        "SELECT id,platform,name,auth_state,code,created_at,lifecycle_state,disconnected_at "
        "FROM accounts"
    ))
    account_ids = set()
    account_platforms: dict[str, str] = {}
    account_lifecycles: dict[str, str] = {}
    for row in accounts:
        if (
            not isinstance(row["id"], str)
            or _ID.fullmatch(row["id"]) is None
            or row["id"] in account_ids
            or row["platform"] not in PLATFORMS
            or not _is_normalized_text(row["name"], 60, required=True)
            or row["auth_state"] not in {"unchecked", "checking", "ready", "invalid"}
            or not _is_safe_code(row["code"])
            or not _is_timestamp(row["created_at"])
            or row["lifecycle_state"] not in {"active", "disconnected"}
            or (
                row["lifecycle_state"] == "active"
                and row["disconnected_at"] is not None
            )
            or (
                row["lifecycle_state"] == "disconnected"
                and (
                    not _is_timestamp(row["disconnected_at"])
                    or row["auth_state"] != "unchecked"
                )
            )
        ):
            raise UploadBackupError("upload account metadata is invalid")
        account_ids.add(row["id"])
        account_platforms[row["id"]] = row["platform"]
        account_lifecycles[row["id"]] = row["lifecycle_state"]

    job_columns = (
        "id,account_id,source_id,title,description,tags,category_id,mode,"
        "copyright,source_credit,state,code,created_at,updated_at,retry_of"
    )
    if schema_version >= _schema._SCHEMA_V3_VERSION:
        job_columns += (
            ",cover_landscape_asset_id,cover_portrait_asset_id,publish_at_unix,"
            "publish_timezone_offset_minutes,platform_options"
        )
    jobs = [dict(row) for row in db.execute(f"SELECT {job_columns} FROM jobs")]
    if schema_version == 2:
        for job in jobs:
            job.update(
                cover_landscape_asset_id=None,
                cover_portrait_asset_id=None,
                publish_at_unix=None,
                publish_timezone_offset_minutes=None,
                platform_options="{}",
            )
    source_states = dict(db.execute("SELECT id,media_state FROM sources"))
    source_digests = dict(db.execute("SELECT id,sha256 FROM sources"))
    asset_rows = {
        row["id"]: dict(row)
        for row in db.execute(
            "SELECT id,kind,name,suffix,mime_type,size,sha256,width,height,created_at,"
            "media_state,deleted_at FROM upload_assets"
        )
    } if schema_version >= _schema._SCHEMA_V3_VERSION else {}
    job_ids = {row["id"] for row in jobs}
    if len(job_ids) != len(jobs) or any(
        not isinstance(job_id, str) or _ID.fullmatch(job_id) is None for job_id in job_ids
    ):
        raise UploadBackupError("upload job identity is invalid")
    retry_parent: dict[str, str | None] = {}
    job_tags: dict[str, list[str]] = {}
    job_options: dict[str, dict] = {}
    successors: set[str] = set()
    for row in jobs:
        if not isinstance(row["tags"], str):
            raise UploadBackupError("upload job tags are invalid")
        try:
            tags = json.loads(row["tags"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UploadBackupError("upload job tags are invalid") from exc
        retry_of = row["retry_of"]
        if (
            row["account_id"] not in account_ids
            or not isinstance(row["source_id"], str)
            or _ID.fullmatch(row["source_id"]) is None
            or row["mode"] not in {"publish", "draft"}
            or type(row["copyright"]) is not int
            or row["copyright"] not in {1, 2}
            or row["state"]
            not in {
                "draft",
                "queued",
                "running",
                "submitted",
                "draft_saved",
                "unknown",
                "failed",
                "canceled",
            }
            or not _is_safe_code(row["code"])
            or not _is_timestamp(row["created_at"])
            or not _is_timestamp(row["updated_at"])
            or (retry_of is not None and retry_of not in job_ids)
            or retry_of == row["id"]
            or (retry_of is not None and retry_of in successors)
        ):
            raise UploadBackupError("upload job metadata or retry relation is invalid")
        if not (
            _is_normalized_text(row["title"], 100, required=True)
            and _is_normalized_text(row["description"], 2000)
            and _is_normalized_text(row["source_credit"], 200)
        ):
            raise UploadBackupError("upload job text metadata is invalid")
        if (
            not isinstance(tags, list)
            or len(tags) > 10
            or any(
                not _is_normalized_text(tag, 20, required=True)
                or any(
                    character in tag
                    for character in (
                        ",\n\r\t"
                        if schema_version == 2
                        else ",，#＃\n\r\t"
                    )
                )
                for tag in tags
            )
            or len(tags) != len(set(tags))
        ):
            raise UploadBackupError("upload job tags are invalid")
        platform = account_platforms[row["account_id"]]
        options = _parse_platform_options(
            row["platform_options"],
            platform,
            require_tencent_short_title=schema_version >= _schema._SCHEMA_V3_VERSION,
        )
        category_id = row["category_id"]
        landscape_id = row["cover_landscape_asset_id"]
        portrait_id = row["cover_portrait_asset_id"]
        publish_at = row["publish_at_unix"]
        publish_offset = row["publish_timezone_offset_minutes"]
        if (
            len(row["title"]) > TITLE_LIMITS[platform]
            or row["mode"] == "draft" and platform != "tencent"
            or platform == "bilibili"
            and (
                type(category_id) is not int
                or not 1 <= category_id <= 10000
                or not tags
                or row["copyright"] == 2
                and not row["source_credit"]
            )
            or cover_slot_error(
                platform, landscape=landscape_id is not None, portrait=portrait_id is not None
            ) is not None
            or schema_version >= _schema._SCHEMA_V3_VERSION
            and platform == "bilibili"
            and row["copyright"] == 1
            and bool(row["source_credit"])
        ):
            raise UploadBackupError("upload job platform metadata is invalid")
        if category_id is not None and (
            type(category_id) is not int or not 1 <= category_id <= 10000
        ):
            raise UploadBackupError("upload job category is invalid")
        if (
            row["state"] == "submitted" and row["mode"] != "publish"
            or row["state"] == "draft_saved" and row["mode"] != "draft"
        ):
            raise UploadBackupError("upload job terminal state does not match its mode")
        if not _valid_optional_identifier(landscape_id) or not _valid_optional_identifier(
            portrait_id
        ):
            raise UploadBackupError("upload job cover reference is invalid")
        if landscape_id is not None and landscape_id not in asset_rows:
            raise UploadBackupError("upload job cover reference is invalid")
        if portrait_id is not None and portrait_id not in asset_rows:
            raise UploadBackupError("upload job cover reference is invalid")
        if not _valid_schedule_pair(publish_at, publish_offset):
            raise UploadBackupError("upload job schedule metadata is invalid")
        if row["mode"] == "draft" and publish_at is not None:
            raise UploadBackupError("upload job schedule metadata is invalid")
        if publish_at is not None and (
            publish_at % 60
            or platform == "tencent" and (publish_at + publish_offset * 60) % 3600
        ):
            raise UploadBackupError("upload job schedule metadata is invalid")
        landscape = asset_rows.get(landscape_id)
        portrait = asset_rows.get(portrait_id)
        if cover_dimensions_error(
            platform,
            landscape=(landscape["width"], landscape["height"]) if landscape is not None else None,
            portrait=(portrait["width"], portrait["height"]) if portrait is not None else None,
        ) is not None:
            raise UploadBackupError("upload job cover metadata is invalid")
        if (
            row["state"] in {"draft", "queued", "running"}
            and source_states.get(row["source_id"]) != "present"
        ):
            raise UploadBackupError("active upload job source is not present")
        if row["state"] in {"draft", "queued", "running"} and any(
            asset_rows[asset_id]["media_state"] != "present"
            for asset_id in (landscape_id, portrait_id)
            if asset_id is not None
        ):
            raise UploadBackupError("active upload job cover is not present")
        retry_parent[row["id"]] = retry_of
        job_tags[row["id"]] = tags
        job_options[row["id"]] = options
        if retry_of is not None:
            successors.add(retry_of)
    jobs_by_id = {row["id"]: row for row in jobs}
    for job_id, parent_id in retry_parent.items():
        if parent_id is None:
            continue
        job = jobs_by_id[job_id]
        parent = jobs_by_id[parent_id]
        job_payload = {
            **job,
            "platform": account_platforms[job["account_id"]],
            "tags": job_tags[job_id],
            "platform_options": job_options[job_id],
        }
        parent_payload = {
            **parent,
            "platform": account_platforms[parent["account_id"]],
            "tags": job_tags[parent_id],
            "platform_options": job_options[parent_id],
        }
        if not upload_retry_payload_matches(parent_payload, job_payload):
            raise UploadBackupError("upload retry payload differs from its parent")
        if parent["state"] not in {"failed", "canceled", "unknown"}:
            raise UploadBackupError("upload retry parent state is invalid")
    for job_id in job_ids:
        visited = set()
        current: str | None = job_id
        while current is not None:
            if current in visited:
                raise UploadBackupError("upload retry relation contains a cycle")
            visited.add(current)
            current = retry_parent[current]
    root_job_ids = {
        job_id for job_id, parent_id in retry_parent.items() if parent_id is None
    }

    operations = list(db.execute(
        "SELECT id,account_id,action,state,code,created_at,updated_at FROM operations"
    ))
    active_operations: set[str] = set()
    login_operation_accounts: dict[str, str] = {}
    for row in operations:
        if (
            not isinstance(row["id"], str)
            or _ID.fullmatch(row["id"]) is None
            or row["account_id"] not in account_ids
            or row["action"] not in {"login", "check"}
            or row["state"] not in {"queued", "running", "ready", "failed", "canceled"}
            or not _is_safe_code(row["code"])
            or not _is_timestamp(row["created_at"])
            or not _is_timestamp(row["updated_at"])
        ):
            raise UploadBackupError("upload operation metadata is invalid")
        if row["state"] in {"queued", "running"}:
            if (
                row["account_id"] in active_operations
                or account_lifecycles[row["account_id"]] != "active"
            ):
                raise UploadBackupError("active upload operation relation is invalid")
            active_operations.add(row["account_id"])
        if row["action"] == "login":
            login_operation_accounts[row["id"]] = row["account_id"]

    requested_jobs: set[str] = set()
    request_by_root_job: dict[str, dict[str, object]] = {}
    request_columns = (
        "id,digest,job_ids,digest_version"
        if schema_version >= _schema._SCHEMA_V3_VERSION
        else "id,digest,job_ids,1 AS digest_version"
    )
    for row in db.execute(f"SELECT {request_columns} FROM requests"):
        if not isinstance(row["job_ids"], str):
            raise UploadBackupError("upload request job list is invalid")
        try:
            request_jobs = json.loads(row["job_ids"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UploadBackupError("upload request job list is invalid") from exc
        if request_jobs == []:
            # Whole-workflow cancellation reserves an otherwise absent stable
            # upload request key with either the exact request digest or a v2
            # stable-key sentinel before request metadata is needed.  Preserve
            # that fail-closed tombstone so restore cannot create the fan-out.
            if (
                schema_version < _schema._SCHEMA_V3_VERSION
                or not isinstance(row["id"], str)
                or _WORKFLOW_UPLOAD_REQUEST.fullmatch(row["id"]) is None
                or not isinstance(row["digest"], str)
                or _SHA256.fullmatch(row["digest"]) is None
                or row["digest_version"] != 2
            ):
                raise UploadBackupError("upload request metadata is invalid")
            continue
        if (
            not isinstance(row["id"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", row["id"]) is None
            or not isinstance(row["digest"], str)
            or _SHA256.fullmatch(row["digest"]) is None
            or type(row["digest_version"]) is not int
            or row["digest_version"] not in {1, 2}
            or not isinstance(request_jobs, list)
            or not 1 <= len(request_jobs) <= 20
            or any(not isinstance(job_id, str) for job_id in request_jobs)
            or len(request_jobs) != len(set(request_jobs))
            or any(job_id not in job_ids for job_id in request_jobs)
        ):
            raise UploadBackupError("upload request metadata is invalid")
        request_rows = [jobs_by_id[job_id] for job_id in request_jobs]
        account_ids_for_request = [job["account_id"] for job in request_rows]
        if (
            any(job["retry_of"] is not None for job in request_rows)
            or len(account_ids_for_request) != len(set(account_ids_for_request))
            or any(job_id in requested_jobs for job_id in request_jobs)
        ):
            raise UploadBackupError("upload request job relation is invalid")
        first = request_rows[0]
        if row["digest_version"] == 1:
            shared_fields = (
                "source_id",
                "title",
                "description",
                "category_id",
                "mode",
                "copyright",
                "source_credit",
            )
            if (
                any(
                    job[field] != first[field]
                    for job in request_rows[1:]
                    for field in shared_fields
                )
                or any(
                    job_tags[job["id"]] != job_tags[first["id"]]
                    for job in request_rows[1:]
                )
                or any(
                    job["cover_landscape_asset_id"] is not None
                    or job["cover_portrait_asset_id"] is not None
                    or job["publish_at_unix"] is not None
                    or job["publish_timezone_offset_minutes"] is not None
                    or (
                        job_options[job["id"]]
                        != _legacy_request_platform_options(
                            job,
                            account_platforms[job["account_id"]],
                            schema_version,
                        )
                    )
                    for job in request_rows
                )
            ):
                raise UploadBackupError("upload request job relation is invalid")
            payload = [
                first["source_id"],
                sorted(account_ids_for_request),
                first["title"],
                first["description"],
                job_tags[first["id"]],
                first["category_id"],
                first["mode"],
                first["copyright"],
                first["source_credit"],
            ]
            expected_digests = {_request_digest(payload)}
            request_platforms = {
                account_platforms[account_id] for account_id in account_ids_for_request
            }
            if "bilibili" not in request_platforms and first["copyright"] == 1:
                # Legacy create_jobs hashed an omitted copyright as null, then
                # stored the non-Bilibili default as 1.
                payload_with_defaulted_copyright = [*payload]
                payload_with_defaulted_copyright[7] = None
                expected_digests.add(_request_digest(payload_with_defaulted_copyright))
            if row["digest"] not in expected_digests:
                raise UploadBackupError("upload request digest does not match its jobs")
        else:
            source_ids = {job["source_id"] for job in request_rows}
            if len(source_ids) != 1:
                raise UploadBackupError("upload request job relation is invalid")
            targets = []
            for job in request_rows:
                platform = account_platforms[job["account_id"]]
                if not _is_service_normalized_target(job, platform, job_options[job["id"]]):
                    raise UploadBackupError("upload request platform target is invalid")
                targets.append({
                    "account_id": job["account_id"],
                    "platform": platform,
                    "title": job["title"],
                    "description": job["description"],
                    "tags": job_tags[job["id"]],
                    "category_id": job["category_id"],
                    "mode": job["mode"],
                    "copyright": job["copyright"],
                    "source_credit": job["source_credit"],
                    "cover_landscape_asset_id": job["cover_landscape_asset_id"],
                    "cover_portrait_asset_id": job["cover_portrait_asset_id"],
                    "publish_at_unix": job["publish_at_unix"],
                    "publish_timezone_offset_minutes": job["publish_timezone_offset_minutes"],
                    "platform_options": job_options[job["id"]],
                })
            digest_payload = {
                "source_id": next(iter(source_ids)),
                "targets": sorted(targets, key=lambda item: item["account_id"]),
            }
            if row["digest"] != _request_digest_v2(digest_payload):
                raise UploadBackupError("upload request digest does not match its jobs")
        requested_jobs.update(request_jobs)
        request_record = {
            "id": row["id"],
            "digest": row["digest"],
            "digest_version": row["digest_version"],
        }
        for job_id in request_jobs:
            request_by_root_job[job_id] = request_record
    if requested_jobs != root_job_ids:
        raise UploadBackupError("upload request root job coverage is invalid")
    if schema_version == _schema.SCHEMA_VERSION:
        _audit_upload_attempts(
            db,
            jobs_by_id=jobs_by_id,
            retry_parent=retry_parent,
            job_tags=job_tags,
            job_options=job_options,
            account_platforms=account_platforms,
            source_digests=source_digests,
            asset_rows=asset_rows,
            login_operation_accounts=login_operation_accounts,
            request_by_root_job=request_by_root_job,
        )


def _audit_upload_attempts(
    db: sqlite3.Connection,
    *,
    jobs_by_id: Mapping[str, Mapping[str, object]],
    retry_parent: Mapping[str, str | None],
    job_tags: Mapping[str, list[str]],
    job_options: Mapping[str, dict],
    account_platforms: Mapping[str, str],
    source_digests: Mapping[str, str],
    asset_rows: Mapping[str, Mapping[str, object]],
    login_operation_accounts: Mapping[str, str],
    request_by_root_job: Mapping[str, Mapping[str, object]],
) -> None:
    """Validate immutable dispatch receipts without inventing legacy attempts."""

    attempts = [
        dict(row)
        for row in db.execute(
            "SELECT id,job_id,root_job_id,request_id,request_digest,"
            "request_digest_version,job_digest,job_digest_version,account_id,"
            "platform,session_revision,source_id,source_sha256,"
            "cover_landscape_asset_id,cover_landscape_sha256,"
            "cover_portrait_asset_id,cover_portrait_sha256,product_identity,"
            "adapter_name,adapter_revision,state,result_status,result_code,"
            "evidence_kind,reconciliation,reconciliation_evidence_kind,created_at,"
            "dispatch_started_at,responded_at,reconciled_at,revision "
            "FROM upload_attempts"
        )
    ]
    attempt_ids: set[str] = set()
    attempt_job_ids: set[str] = set()
    for attempt in attempts:
        attempt_id = attempt["id"]
        job_id = attempt["job_id"]
        if (
            not isinstance(attempt_id, str)
            or _ID.fullmatch(attempt_id) is None
            or attempt_id in attempt_ids
            or not isinstance(job_id, str)
            or _ID.fullmatch(job_id) is None
            or job_id in attempt_job_ids
            or job_id not in jobs_by_id
        ):
            raise UploadBackupError("upload attempt identity is invalid")
        attempt_ids.add(attempt_id)
        attempt_job_ids.add(job_id)

        job = jobs_by_id[job_id]
        root_job_id = job_id
        while True:
            parent_id = retry_parent[root_job_id]
            if parent_id is None:
                break
            root_job_id = parent_id
        request = request_by_root_job.get(root_job_id)
        if (
            attempt["root_job_id"] != root_job_id
            or request is None
            or attempt["request_id"] != request["id"]
            or attempt["request_digest"] != request["digest"]
            or attempt["request_digest_version"] != request["digest_version"]
        ):
            raise UploadBackupError("upload attempt request binding is invalid")

        platform = account_platforms[job["account_id"]]
        job_payload = {
            **job,
            "platform": platform,
            "tags": job_tags[job_id],
            "platform_options": job_options[job_id],
        }
        try:
            expected_job_digest = upload_job_definition_digest(job_payload)
        except UploadError as exc:
            raise UploadBackupError("upload attempt job digest is invalid") from exc
        landscape_id = job["cover_landscape_asset_id"]
        portrait_id = job["cover_portrait_asset_id"]
        landscape_sha256 = (
            asset_rows[landscape_id]["sha256"] if landscape_id is not None else None
        )
        portrait_sha256 = (
            asset_rows[portrait_id]["sha256"] if portrait_id is not None else None
        )
        session_revision = attempt["session_revision"]
        if (
            attempt["job_digest_version"] != 1
            or attempt["job_digest"] != expected_job_digest
            or attempt["account_id"] != job["account_id"]
            or attempt["platform"] != platform
            or attempt["source_id"] != job["source_id"]
            or attempt["source_sha256"] != source_digests[job["source_id"]]
            or attempt["cover_landscape_asset_id"] != landscape_id
            or attempt["cover_landscape_sha256"] != landscape_sha256
            or attempt["cover_portrait_asset_id"] != portrait_id
            or attempt["cover_portrait_sha256"] != portrait_sha256
            or not isinstance(session_revision, str)
            or _ID.fullmatch(session_revision) is None
            or (
                session_revision != job["account_id"]
                and login_operation_accounts.get(session_revision) != job["account_id"]
            )
        ):
            raise UploadBackupError("upload attempt frozen identity is invalid")

        state = attempt["state"]
        status = attempt["result_status"]
        code = attempt["result_code"]
        evidence_kind = attempt["evidence_kind"]
        conclusion = attempt["reconciliation"]
        reconciliation_evidence = attempt["reconciliation_evidence_kind"]
        revision = attempt["revision"]
        if (
            not isinstance(attempt["product_identity"], str)
            or _PRODUCT_IDENTITY.fullmatch(attempt["product_identity"]) is None
            or not isinstance(attempt["adapter_name"], str)
            or _ADAPTER_IDENTITY.fullmatch(attempt["adapter_name"]) is None
            or not isinstance(attempt["adapter_revision"], str)
            or _ADAPTER_IDENTITY.fullmatch(attempt["adapter_revision"]) is None
            or not upload_adapter_identity_matches(
                platform, attempt["adapter_name"], attempt["adapter_revision"]
            )
            or state not in UPLOAD_ATTEMPT_STATES
            or status is not None and status not in UPLOAD_RESULT_STATUSES
            or code is not None and not _is_safe_code(code)
            or evidence_kind is not None and evidence_kind not in UPLOAD_EVIDENCE_KINDS
            or conclusion is not None and conclusion not in UPLOAD_RECONCILIATIONS
            or reconciliation_evidence not in {None, "operator_platform_check"}
            or not _is_timestamp(attempt["created_at"])
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision <= 2_147_483_647
        ):
            raise UploadBackupError("upload attempt metadata is invalid")

        dispatch_at = attempt["dispatch_started_at"]
        responded_at = attempt["responded_at"]
        reconciled_at = attempt["reconciled_at"]
        if any(
            value is not None and not _is_timestamp(value)
            for value in (dispatch_at, responded_at, reconciled_at)
        ):
            raise UploadBackupError("upload attempt timestamp is invalid")

        try:
            validate_upload_attempt_state(job_payload, attempt)
        except UploadError:
            raise UploadBackupError("upload attempt state is invalid") from None


def _valid_optional_identifier(value: object) -> bool:
    return value is None or isinstance(value, str) and _ID.fullmatch(value) is not None


def _valid_schedule_pair(publish_at: object, offset: object) -> bool:
    if publish_at is None:
        return offset is None
    return (
        type(publish_at) is int
        and 1_700_000_000 <= publish_at <= 4_102_444_800
        and type(offset) is int
        and -840 <= offset <= 840
    )


def _parse_platform_options(
    value: object,
    platform: str,
    *,
    require_tencent_short_title: bool = False,
) -> dict:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise UploadBackupError("upload job platform options are invalid")
    try:
        options = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise UploadBackupError("upload job platform options are invalid") from exc
    if not isinstance(options, dict):
        raise UploadBackupError("upload job platform options are invalid")
    canonical = json.dumps(
        options,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if value != canonical:
        raise UploadBackupError("upload job platform options are not canonical")
    allowed = {
        "bilibili": {"dynamic", "no_reprint", "close_comments", "close_danmu"},
        "douyin": {"declaration"},
        "tencent": {"short_title", "content_label"},
    }[platform]
    if set(options) - allowed:
        raise UploadBackupError("upload job platform option is unsupported")
    if platform == "bilibili":
        if "dynamic" in options and not _is_normalized_text(options["dynamic"], 250):
            raise UploadBackupError("upload job platform options are invalid")
        if any(
            key in options and type(options[key]) is not bool
            for key in ("no_reprint", "close_comments", "close_danmu")
        ):
            raise UploadBackupError("upload job platform options are invalid")
    elif platform == "douyin":
        declaration = options.get("declaration")
        if declaration is not None and declaration not in DOUYIN_DECLARATIONS:
            raise UploadBackupError("upload job platform options are invalid")
    else:
        short_title = options.get("short_title")
        content_label = options.get("content_label")
        if require_tencent_short_title and (
            not is_tencent_short_title_output(short_title)
        ):
            raise UploadBackupError("upload job platform options are invalid")
        if short_title is not None and not _is_normalized_text(
            short_title, 15, required=True
        ):
            raise UploadBackupError("upload job platform options are invalid")
        if short_title is not None and len(short_title) < 7:
            raise UploadBackupError("upload job platform options are invalid")
        if content_label is not None and content_label not in TENCENT_CONTENT_LABELS:
            raise UploadBackupError("upload job platform options are invalid")
    return options


def _normalized_platform_options(platform: str, options: dict) -> dict:
    if platform == "bilibili":
        return {
            "dynamic": options.get("dynamic", ""),
            "no_reprint": options.get("no_reprint", False),
            "close_comments": options.get("close_comments", False),
            "close_danmu": options.get("close_danmu", False),
        }
    if platform == "douyin":
        return {"declaration": options.get("declaration")}
    return {
        "short_title": options.get("short_title"),
        "content_label": options.get("content_label"),
    }


def _legacy_request_platform_options(
    job: Mapping[str, object], platform: str, schema_version: int
) -> dict:
    if schema_version < _schema._SCHEMA_V3_VERSION:
        return {}
    return legacy_migrated_platform_options(platform, job["title"])


def _is_service_normalized_target(job: Mapping[str, object], platform: str, options: dict) -> bool:
    if options != _normalized_platform_options(platform, options):
        return False
    if platform == "bilibili":
        return (
            type(job["category_id"]) is int
            and 1 <= job["category_id"] <= 10000
            and type(job["copyright"]) is int
            and job["copyright"] in {1, 2}
            and (job["copyright"] == 2 or job["source_credit"] == "")
        )
    return (
        job["category_id"] is None
        and job["copyright"] == 1
        and job["source_credit"] == ""
    )


def _is_normalized_text(
    value: object,
    maximum: int,
    *,
    required: bool = False,
) -> bool:
    """Match the persisted result of ``UploadService._text`` exactly."""

    return (
        isinstance(value, str)
        and len(value) <= maximum
        and not any(
            ord(character) < 32 and character not in "\n\t\r"
            for character in value
        )
        and value == value.strip()
        and (not required or bool(value))
    )


def _is_safe_code(value: object) -> bool:
    return isinstance(value, str) and (not value or _SAFE_CODE.fullmatch(value) is not None)


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _request_digest(payload: list[object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _request_digest_v2(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _validate_manifest_entry_path(relative: str, *, format_version: int) -> None:
    if relative in {UPLOAD_BACKUP_METADATA_NAME, UPLOAD_DATABASE_PAYLOAD_PATH}:
        return
    pure = PurePosixPath(relative)
    try:
        media_relative = pure.relative_to(UPLOAD_MEDIA_PAYLOAD_PREFIX)
    except ValueError:
        try:
            asset_relative = pure.relative_to(UPLOAD_ASSET_PAYLOAD_PREFIX)
        except ValueError as exc:
            raise UploadBackupError("upload backup payload path is unexpected") from exc
        if (
            format_version not in {
                _UPLOAD_BACKUP_FORMAT_V2,
                UPLOAD_BACKUP_FORMAT_VERSION,
            }
            or len(asset_relative.parts) != 1
        ):
            raise UploadBackupError("upload backup asset path is invalid")
        name = asset_relative.name
        suffix = PurePosixPath(name).suffix
        if suffix not in COVER_MIME_TYPES or _ID.fullmatch(name.removesuffix(suffix)) is None:
            raise UploadBackupError("upload backup asset path is invalid")
        return
    if len(media_relative.parts) != 1:
        raise UploadBackupError("upload backup media path is invalid")
    name = media_relative.name
    suffix = PurePosixPath(name).suffix
    if suffix not in _SUFFIXES or _ID.fullmatch(name.removesuffix(suffix)) is None:
        raise UploadBackupError("upload backup media path is invalid")


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_nlink == second.st_nlink
        and first.st_size == second.st_size
    )


def _path_key(value: str) -> str:
    return value.casefold()


# Module-local aliases keep the upload API concise while retaining explicit names.
create_backup = create_upload_backup
restore_backup = restore_upload_backup
BackupResult = UploadBackupResult
RestoreResult = UploadRestoreResult
BackupRestoreError = UploadBackupError

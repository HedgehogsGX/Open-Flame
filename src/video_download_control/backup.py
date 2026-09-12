"""Fail-closed backup and restore for one Video Download Control data root.

The backup consistency boundary is explicit: an SQLite ``BEGIN IMMEDIATE``
write reservation is held while an online database backup and the immutable
published files are copied.  Volatile top-level ``logs`` and ``temporary`` plus
``assets/.staging`` trees are excluded.  Pending asset commit intents are
rejected, because their filesystem/SQL outcome has not yet been reconciled.
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
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator
from urllib.parse import unquote
from uuid import uuid4

from . import __version__
from .database import SCHEMA_VERSION, Database
from .managed_files import close_binary_on_error, discard_created_file
from .graph import (
    GraphValidationError,
    XPostIdentity,
    x_attachment_canonical_url,
    x_attachment_source_id,
)

BACKUP_FORMAT_VERSION = 1
BACKUP_METADATA_NAME = "backup-metadata.json"
BACKUP_MANIFEST_NAME = "backup-manifest.json"
BACKUP_MANIFEST_HASH_NAME = "backup-manifest.sha256"
DATABASE_PAYLOAD_PATH = "payload/database/control.sqlite3"
DATA_PAYLOAD_PREFIX = PurePosixPath("payload/data")
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_ASSET_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_BACKUP_ENTRIES = 1_000_000
COPY_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BackupRestoreError(RuntimeError):
    """A bounded, operator-safe backup/restore diagnostic."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_root: Path
    schema_version: int
    file_count: int
    total_bytes: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restore_root: Path
    database_path: Path
    schema_version: int
    file_count: int
    total_bytes: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    path: str
    size_bytes: int
    sha256: str


def create_backup(
    *,
    source_data_root: Path,
    source_database_path: Path,
    backup_target: Path,
) -> BackupResult:
    """Create one immutable, checksummed backup directory."""

    data_root = _require_existing_directory(
        source_data_root, label="source data root"
    )
    database_path = _require_existing_regular_file(
        source_database_path, label="source database"
    )
    target = _require_new_target(backup_target, label="backup target")
    if _paths_overlap(data_root, target) or _paths_overlap(database_path, target):
        raise BackupRestoreError("backup target overlaps a source path")

    ready, _ = Database(database_path).readiness()
    if not ready:
        raise BackupRestoreError("source database is not ready for backup")

    stage = target.parent / f".{target.name}.partial-{uuid4()}"
    if os.path.lexists(stage):
        raise BackupRestoreError("backup staging path collision")
    try:
        stage.mkdir(mode=0o700)
        payload_database = stage.joinpath(*PurePosixPath(DATABASE_PAYLOAD_PATH).parts)
        payload_database.parent.mkdir(parents=True)
        payload_data_root = stage.joinpath(*DATA_PAYLOAD_PREFIX.parts)
        payload_data_root.mkdir(parents=True)

        payload_entries: list[_ManifestEntry] = []
        lock_connection = sqlite3.connect(str(database_path), timeout=5.0)
        try:
            lock_connection.row_factory = sqlite3.Row
            lock_connection.execute("PRAGMA busy_timeout = 5000")
            lock_connection.execute("BEGIN IMMEDIATE")
            schema_version = _database_schema_version(lock_connection)
            if schema_version != SCHEMA_VERSION:
                raise BackupRestoreError("source database schema is unsupported")
            pending = lock_connection.execute(
                "SELECT COUNT(*) FROM asset_commit_intents"
            ).fetchone()[0]
            if int(pending) != 0:
                raise BackupRestoreError(
                    "source has pending asset commit intents"
                )

            _sqlite_online_backup(database_path, payload_database)
            payload_entries.append(
                _entry_for_file(
                    payload_database,
                    relative_path=DATABASE_PAYLOAD_PATH,
                )
            )
            for source, relative in _iter_managed_source_files(
                data_root,
                database_path=database_path,
            ):
                backup_relative = DATA_PAYLOAD_PREFIX / PurePosixPath(
                    relative.as_posix()
                )
                destination = stage.joinpath(*backup_relative.parts)
                digest, copied = _copy_regular_file(source, destination)
                payload_entries.append(
                    _ManifestEntry(
                        path=backup_relative.as_posix(),
                        size_bytes=copied,
                        sha256=digest,
                    )
                )

            _audit_database_and_assets(
                data_root=payload_data_root,
                database_path=payload_database,
                expected_schema=schema_version,
            )
        finally:
            with suppress(sqlite3.Error):
                lock_connection.rollback()
            lock_connection.close()

        payload_entries.sort(key=lambda entry: entry.path.casefold())
        payload_total = sum(entry.size_bytes for entry in payload_entries)
        metadata = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "application_version": __version__,
            "schema_version": schema_version,
            "database_payload_path": DATABASE_PAYLOAD_PATH,
            "data_payload_prefix": DATA_PAYLOAD_PREFIX.as_posix(),
            "payload_file_count": len(payload_entries),
            "payload_total_bytes": payload_total,
            "consistency": "sqlite_begin_immediate_plus_online_backup",
            "volatile_paths_excluded": [
                "logs",
                "temporary",
                "assets/.staging",
            ],
        }
        metadata_path = stage / BACKUP_METADATA_NAME
        _write_json_exclusive(metadata_path, metadata)
        entries = [
            _entry_for_file(
                metadata_path,
                relative_path=BACKUP_METADATA_NAME,
            ),
            *payload_entries,
        ]
        entries.sort(key=lambda entry: entry.path.casefold())
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "algorithm": "sha256",
            "metadata_path": BACKUP_METADATA_NAME,
            "entries": [
                {
                    "path": entry.path,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                }
                for entry in entries
            ],
        }
        manifest_path = stage / BACKUP_MANIFEST_NAME
        _write_json_exclusive(manifest_path, manifest)
        manifest_sha256 = _sha256_regular_file(manifest_path)
        _write_text_exclusive(
            stage / BACKUP_MANIFEST_HASH_NAME,
            manifest_sha256 + "\n",
        )
        _sync_tree(stage)
        _publish_directory(stage, target)
        return BackupResult(
            backup_root=target,
            schema_version=schema_version,
            file_count=len(entries),
            total_bytes=sum(entry.size_bytes for entry in entries),
            manifest_sha256=manifest_sha256,
        )
    except BackupRestoreError:
        _cleanup_stage(stage)
        raise
    except sqlite3.Error as exc:
        _cleanup_stage(stage)
        raise BackupRestoreError("database backup operation failed") from exc
    except (OSError, ValueError, TypeError) as exc:
        _cleanup_stage(stage)
        raise BackupRestoreError("backup storage operation failed") from exc


def restore_backup(
    *,
    backup_root: Path,
    restore_data_root: Path,
    restore_database_path: Path,
) -> RestoreResult:
    """Verify and restore a backup into one nonexistent independent root."""

    source = _require_existing_directory(backup_root, label="backup root")
    requested_target = _require_absolute_non_root(
        restore_data_root, label="restore target"
    )
    target = _require_new_target(requested_target, label="restore target")
    requested_database_target = _require_absolute_non_root(
        restore_database_path, label="restore database"
    )
    database_relative: Path | None = None
    for candidate_root in (requested_target, target):
        if (
            requested_database_target != candidate_root
            and requested_database_target.is_relative_to(candidate_root)
        ):
            database_relative = requested_database_target.relative_to(candidate_root)
            break
    if database_relative is None:
        raise BackupRestoreError(
            "restore database must be inside the restore target"
        )
    database_target = target / database_relative
    _assert_existing_ancestors_no_links(database_target)
    if _paths_overlap(source, target):
        raise BackupRestoreError("restore target overlaps the backup root")

    manifest_sha256, entries, metadata = _load_and_verify_backup(source)
    schema_version = metadata.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        raise BackupRestoreError("backup schema is unsupported")
    database_payload_path = metadata.get("database_payload_path")
    if database_payload_path != DATABASE_PAYLOAD_PATH:
        raise BackupRestoreError("backup database payload identity is invalid")

    stage = target.parent / f".{target.name}.partial-{uuid4()}"
    if os.path.lexists(stage):
        raise BackupRestoreError("restore staging path collision")
    database_relative = database_target.relative_to(target)
    total_restore_bytes = sum(
        entry.size_bytes
        for entry in entries
        if entry.path != BACKUP_METADATA_NAME
    )
    try:
        if shutil.disk_usage(target.parent).free < total_restore_bytes:
            raise BackupRestoreError("insufficient free space for restore")
        stage.mkdir(mode=0o700)
        restored_paths: set[str] = set()
        restored_file_count = 0
        for entry in entries:
            if entry.path == BACKUP_METADATA_NAME:
                continue
            source_file = source.joinpath(*PurePosixPath(entry.path).parts)
            if entry.path == DATABASE_PAYLOAD_PATH:
                relative_destination = database_relative
            else:
                pure = PurePosixPath(entry.path)
                try:
                    data_relative = pure.relative_to(DATA_PAYLOAD_PREFIX)
                except ValueError as exc:
                    raise BackupRestoreError(
                        "backup contains an unexpected payload path"
                    ) from exc
                relative_destination = Path(*data_relative.parts)
            key = relative_destination.as_posix().casefold()
            if not key or key in restored_paths:
                raise BackupRestoreError("restore destination path collision")
            restored_paths.add(key)
            destination = stage / relative_destination
            digest, copied = _copy_regular_file(
                source_file,
                destination,
                expected_size=entry.size_bytes,
                expected_sha256=entry.sha256,
            )
            if digest != entry.sha256 or copied != entry.size_bytes:
                raise BackupRestoreError("restored file verification failed")
            restored_file_count += 1

        staged_database = stage / database_relative
        _audit_database_and_assets(
            data_root=stage,
            database_path=staged_database,
            expected_schema=SCHEMA_VERSION,
        )
        _sync_tree(stage)
        _publish_directory(stage, target)
        return RestoreResult(
            restore_root=target,
            database_path=target / database_relative,
            schema_version=SCHEMA_VERSION,
            file_count=restored_file_count,
            total_bytes=total_restore_bytes,
            manifest_sha256=manifest_sha256,
        )
    except BackupRestoreError:
        _cleanup_stage(stage)
        raise
    except sqlite3.Error as exc:
        _cleanup_stage(stage)
        raise BackupRestoreError("restored database verification failed") from exc
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _cleanup_stage(stage)
        raise BackupRestoreError("restore storage operation failed") from exc


def _load_and_verify_backup(
    backup_root: Path,
) -> tuple[str, tuple[_ManifestEntry, ...], Mapping[str, Any]]:
    hash_path = backup_root / BACKUP_MANIFEST_HASH_NAME
    manifest_path = backup_root / BACKUP_MANIFEST_NAME
    hash_text = _read_bounded_regular_file(hash_path, 128).decode(
        "ascii", errors="strict"
    )
    if not re.fullmatch(r"[0-9a-f]{64}\n", hash_text):
        raise BackupRestoreError("backup manifest hash record is invalid")
    expected_manifest_hash = hash_text.strip()
    if _sha256_regular_file(manifest_path) != expected_manifest_hash:
        raise BackupRestoreError("backup manifest hash mismatch")
    manifest = _read_json_mapping(manifest_path, MAX_MANIFEST_BYTES)
    if (
        manifest.get("format_version") != BACKUP_FORMAT_VERSION
        or manifest.get("algorithm") != "sha256"
        or manifest.get("metadata_path") != BACKUP_METADATA_NAME
    ):
        raise BackupRestoreError("backup manifest header is invalid")
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= MAX_BACKUP_ENTRIES:
        raise BackupRestoreError("backup manifest entry count is invalid")
    entries: list[_ManifestEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise BackupRestoreError("backup manifest entry is invalid")
        path = _validated_relative_path(raw.get("path"))
        size = raw.get("size_bytes")
        digest = raw.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise BackupRestoreError("backup manifest entry is invalid")
        key = path.casefold()
        if key in seen:
            raise BackupRestoreError("backup manifest has duplicate paths")
        seen.add(key)
        entry = _ManifestEntry(path=path, size_bytes=size, sha256=digest)
        candidate = backup_root.joinpath(*PurePosixPath(path).parts)
        _verify_regular_file(
            candidate,
            expected_size=size,
            expected_sha256=digest,
        )
        entries.append(entry)
    if BACKUP_METADATA_NAME.casefold() not in seen:
        raise BackupRestoreError("backup metadata is not covered by manifest")
    expected_files = {
        *(entry.path.casefold() for entry in entries),
        BACKUP_MANIFEST_NAME.casefold(),
        BACKUP_MANIFEST_HASH_NAME.casefold(),
    }
    actual_files = {
        path.casefold() for path in _scan_backup_files(backup_root)
    }
    if actual_files != expected_files:
        raise BackupRestoreError("backup contains untracked or missing files")
    metadata = _read_json_mapping(
        backup_root / BACKUP_METADATA_NAME,
        MAX_ASSET_MANIFEST_BYTES,
    )
    if (
        metadata.get("format_version") != BACKUP_FORMAT_VERSION
        or metadata.get("data_payload_prefix") != DATA_PAYLOAD_PREFIX.as_posix()
        or metadata.get("database_payload_path") != DATABASE_PAYLOAD_PATH
    ):
        raise BackupRestoreError("backup metadata is invalid")
    payload_entries = [
        entry for entry in entries if entry.path != BACKUP_METADATA_NAME
    ]
    if (
        metadata.get("payload_file_count") != len(payload_entries)
        or metadata.get("payload_total_bytes")
        != sum(entry.size_bytes for entry in payload_entries)
    ):
        raise BackupRestoreError("backup metadata totals are invalid")
    return expected_manifest_hash, tuple(entries), metadata


def _audit_database_and_assets(
    *,
    data_root: Path,
    database_path: Path,
    expected_schema: int,
) -> None:
    if not database_path.is_file():
        raise BackupRestoreError("restored database is missing")
    connection = sqlite3.connect(str(database_path), timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        quick = connection.execute("PRAGMA quick_check").fetchall()
        if len(quick) != 1 or quick[0][0] != "ok":
            raise BackupRestoreError("restored database quick_check failed")
        if _database_schema_version(connection) != expected_schema:
            raise BackupRestoreError("restored database schema mismatch")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupRestoreError("restored database foreign keys are invalid")
        if int(
            connection.execute(
                "SELECT COUNT(*) FROM asset_commit_intents"
            ).fetchone()[0]
        ):
            raise BackupRestoreError("restored database has pending asset intents")
        _audit_graph_state(connection)
        assets = connection.execute(
            """
            SELECT id, size_bytes, sha256, status
            FROM media_assets ORDER BY id
            """
        ).fetchall()
        artifacts = connection.execute(
            """
            SELECT a.id, a.asset_id, a.kind, a.path, a.mime_type, a.sha256,
                   c.language AS caption_language
            FROM artifacts AS a
            LEFT JOIN captions AS c ON c.artifact_id = a.id
            ORDER BY a.asset_id, a.kind, a.path
            """
        ).fetchall()
    finally:
        connection.close()
    ready, _ = Database(database_path).readiness()
    if not ready:
        raise BackupRestoreError("restored database readiness failed")

    artifacts_by_asset: dict[str, list[sqlite3.Row]] = {}
    artifact_sizes: dict[str, int] = {}
    seen_paths: set[str] = set()
    for artifact in artifacts:
        relative = _validated_relative_path(artifact["path"])
        if not relative.startswith(f"assets/{artifact['asset_id']}/"):
            raise BackupRestoreError("artifact path does not match its asset")
        if relative.casefold() in seen_paths:
            raise BackupRestoreError("artifact paths are not unique")
        seen_paths.add(relative.casefold())
        path = data_root.joinpath(*PurePosixPath(relative).parts)
        _verify_regular_file(path, expected_sha256=artifact["sha256"])
        artifact_sizes[artifact["id"]] = path.stat().st_size
        if artifact["kind"] == "caption" and artifact["caption_language"] is None:
            raise BackupRestoreError("caption artifact lacks caption semantics")
        if artifact["kind"] != "caption" and artifact["caption_language"] is not None:
            raise BackupRestoreError("caption semantics point to a non-caption")
        artifacts_by_asset.setdefault(artifact["asset_id"], []).append(artifact)

    asset_ids = {row["id"] for row in assets}
    published_ids = _published_asset_directory_ids(data_root)
    if published_ids != asset_ids:
        raise BackupRestoreError("published asset directories do not match database")
    for asset in assets:
        if asset["status"] != "ready":
            raise BackupRestoreError("database contains a non-ready media asset")
        rows = artifacts_by_asset.get(asset["id"], [])
        originals = [row for row in rows if row["kind"] == "original"]
        manifests = [row for row in rows if row["kind"] == "manifest"]
        if len(originals) != 1 or len(manifests) != 1:
            raise BackupRestoreError("asset lacks one original and one manifest")
        original = originals[0]
        manifest_row = manifests[0]
        original_path = data_root.joinpath(
            *PurePosixPath(original["path"]).parts
        )
        if (
            asset["sha256"] != original["sha256"]
            or asset["size_bytes"] != original_path.stat().st_size
        ):
            raise BackupRestoreError("asset original metadata mismatch")
        manifest_path = data_root.joinpath(
            *PurePosixPath(manifest_row["path"]).parts
        )
        manifest = _read_json_mapping(manifest_path, MAX_ASSET_MANIFEST_BYTES)
        _audit_asset_manifest(
            asset_id=asset["id"],
            asset=asset,
            artifacts=rows,
            artifact_sizes=artifact_sizes,
            manifest=manifest,
        )


def _audit_graph_state(connection: sqlite3.Connection) -> None:
    """Validate graph-v2 cross-table meaning without exposing target material."""

    invalid = "restored database graph state is invalid"
    discoveries = connection.execute(
        """
        SELECT id, parent_source_item_id, relation_type, snapshot_hash,
               members_json, member_count, discovered_by_attempt_id
        FROM source_discoveries ORDER BY id
        """
    ).fetchall()
    relations = connection.execute(
        """
        SELECT relation.id, relation.discovery_id,
               relation.parent_source_item_id,
               relation.child_source_item_id, relation.relation_type,
               relation.ordinal, relation.media_kind,
               relation.discovered_by_attempt_id,
               child.source_id AS child_source_identity,
               child.canonical_url AS child_canonical_url,
               child.platform AS child_platform,
               child.source_type AS child_source_type,
               parent.source_id AS parent_source_identity,
               parent.canonical_url AS parent_canonical_url,
               parent.platform AS parent_platform,
               parent.source_type AS parent_source_type
        FROM source_relations AS relation
        JOIN source_items AS child ON child.id = relation.child_source_item_id
        JOIN source_items AS parent ON parent.id = relation.parent_source_item_id
        ORDER BY relation.discovery_id, relation.ordinal, relation.id
        """
    ).fetchall()
    relations_by_discovery: dict[str, list[sqlite3.Row]] = {}
    for relation in relations:
        relations_by_discovery.setdefault(relation["discovery_id"], []).append(
            relation
        )

    discovery_members: dict[str, tuple[Mapping[str, Any], ...]] = {}
    discovery_rows = {row["id"]: row for row in discoveries}
    relation_members: dict[str, Mapping[str, Any]] = {}
    attempt_owners = {
        row["id"]: row
        for row in connection.execute(
            """
            SELECT attempt.id, job.source_item_id, job.job_kind
            FROM job_attempts AS attempt
            JOIN download_jobs AS job ON job.id = attempt.job_id
            ORDER BY attempt.id
            """
        ).fetchall()
    }
    for discovery in discoveries:
        try:
            raw_members = json.loads(discovery["members_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BackupRestoreError(invalid) from exc
        if not isinstance(raw_members, list) or any(
            not isinstance(member, dict) for member in raw_members
        ):
            raise BackupRestoreError(invalid)
        canonical = json.dumps(
            raw_members,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            canonical != discovery["members_json"]
            or len(raw_members) != discovery["member_count"]
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            != discovery["snapshot_hash"]
        ):
            raise BackupRestoreError(invalid)
        discovery_relations = relations_by_discovery.get(discovery["id"], [])
        if len(discovery_relations) != len(raw_members):
            raise BackupRestoreError(invalid)
        new_shape = all(
            set(member) == {"child_source_id", "media_kind", "selector_key"}
            for member in raw_members
        )
        legacy_shape = all(
            set(member)
            == {
                "child_source_item_id",
                "media_kind",
                "ordinal",
                "selector_key",
            }
            for member in raw_members
        )
        if not (new_shape or legacy_shape):
            raise BackupRestoreError(invalid)
        discovery_attempt = attempt_owners.get(
            discovery["discovered_by_attempt_id"]
        )
        if new_shape and (
            discovery_attempt is None
            or discovery_attempt["job_kind"] != "discover"
            or discovery_attempt["source_item_id"]
            != discovery["parent_source_item_id"]
        ):
            raise BackupRestoreError(invalid)
        if (
            legacy_shape
            and discovery["discovered_by_attempt_id"] is not None
            and (
                discovery_attempt is None
                or discovery_attempt["source_item_id"]
                != discovery["parent_source_item_id"]
            )
        ):
            raise BackupRestoreError(invalid)
        for ordinal, (member, relation) in enumerate(
            zip(raw_members, discovery_relations, strict=True)
        ):
            if (
                relation["parent_source_item_id"]
                != discovery["parent_source_item_id"]
                or relation["relation_type"] != discovery["relation_type"]
                or relation["media_kind"] != member["media_kind"]
            ):
                raise BackupRestoreError(invalid)
            if new_shape:
                if (
                    relation["ordinal"] != ordinal
                    or member["child_source_id"]
                    != relation["child_source_identity"]
                    or relation["discovered_by_attempt_id"]
                    != discovery["discovered_by_attempt_id"]
                    or not _valid_graph_key(member["selector_key"])
                    or not _valid_graph_media_kind(member["media_kind"])
                    or not _valid_graph_member_identity(relation)
                ):
                    raise BackupRestoreError(invalid)
            elif (
                member["child_source_item_id"]
                != relation["child_source_item_id"]
                or member["ordinal"] != ordinal
                or member["selector_key"] is not None
                or (
                    relation["discovered_by_attempt_id"] is not None
                    and (
                        relation["discovered_by_attempt_id"] not in attempt_owners
                        or attempt_owners[
                            relation["discovered_by_attempt_id"]
                        ]["source_item_id"]
                        != discovery["parent_source_item_id"]
                    )
                )
            ):
                raise BackupRestoreError(invalid)
            relation_members[relation["id"]] = member
        discovery_members[discovery["id"]] = tuple(raw_members)

    targets = connection.execute(
        """
        SELECT target.job_id, target.fetch_source_item_id,
               target.selector_key, target.expected_media_key,
               target.expected_media_kind,
               job.input_record_id, job.source_item_id, job.job_kind,
               job.run_generation, job.status AS job_status,
               child.platform AS child_platform,
               child.source_type AS child_source_type,
               fetch.platform AS fetch_platform,
               fetch.source_type AS fetch_source_type
        FROM download_job_targets AS target
        JOIN download_jobs AS job ON job.id = target.job_id
        JOIN source_items AS child ON child.id = job.source_item_id
        JOIN source_items AS fetch ON fetch.id = target.fetch_source_item_id
        ORDER BY target.job_id
        """
    ).fetchall()
    targets_by_job = {row["job_id"]: row for row in targets}
    for target in targets:
        if (
            target["job_kind"] != "download"
            or target["child_platform"] != "x"
            or target["child_source_type"] != "x_attachment"
            or target["fetch_platform"] != "x"
            or target["fetch_source_type"] != "x_post"
            or not _valid_graph_key(target["selector_key"])
            or not _valid_graph_key(target["expected_media_key"])
            or not _valid_graph_media_kind(target["expected_media_kind"])
        ):
            raise BackupRestoreError(invalid)

    target_asset_rows = connection.execute(
        """
        SELECT link.job_id, link.role, link.ordinal,
               asset.source_item_id, asset.media_key,
               asset.media_kind, asset.status
        FROM job_assets AS link
        JOIN media_assets AS asset ON asset.id = link.asset_id
        WHERE EXISTS (
            SELECT 1 FROM download_job_targets AS target
            WHERE target.job_id = link.job_id
        )
        ORDER BY link.job_id, link.ordinal, asset.id
        """
    ).fetchall()
    target_assets: dict[str, list[sqlite3.Row]] = {}
    for asset in target_asset_rows:
        target_assets.setdefault(asset["job_id"], []).append(asset)
    for target in targets:
        assets = target_assets.get(target["job_id"], [])
        if target["job_status"] == "ready":
            if len(assets) != 1:
                raise BackupRestoreError(invalid)
            asset = assets[0]
            if (
                asset["role"] != "original"
                or asset["ordinal"] != 0
                or asset["source_item_id"] != target["source_item_id"]
                or asset["media_key"] != target["expected_media_key"]
                or asset["media_kind"] != target["expected_media_kind"]
                or asset["status"] != "ready"
            ):
                raise BackupRestoreError(invalid)
        elif assets:
            raise BackupRestoreError(invalid)

    links = connection.execute(
        """
        SELECT link.input_record_id, link.discovery_id,
               link.run_generation, link.relation_id, link.job_id,
               input.batch_id AS input_batch_id,
               input.active_run_generation,
               relation.discovery_id AS relation_discovery_id,
               relation.parent_source_item_id,
               relation.child_source_item_id,
               relation.media_kind,
               job.batch_id AS job_batch_id,
               job.input_record_id AS job_input_record_id,
               job.source_item_id AS job_source_item_id,
               job.run_generation AS job_run_generation
        FROM input_relation_jobs AS link
        JOIN input_records AS input ON input.id = link.input_record_id
        JOIN source_relations AS relation
          ON relation.id = link.relation_id
         AND relation.discovery_id = link.discovery_id
        JOIN download_jobs AS job ON job.id = link.job_id
        ORDER BY link.input_record_id, link.run_generation, link.relation_id
        """
    ).fetchall()
    links_by_input_generation: dict[tuple[str, int], list[sqlite3.Row]] = {}
    linked_target_jobs: set[str] = set()
    for link in links:
        member = relation_members.get(link["relation_id"])
        target = targets_by_job.get(link["job_id"])
        if member is None or target is None or member.get("selector_key") is None:
            raise BackupRestoreError(invalid)
        if (
            link["relation_discovery_id"] != link["discovery_id"]
            or link["job_input_record_id"] != link["input_record_id"]
            or link["job_batch_id"] != link["input_batch_id"]
            or link["job_source_item_id"] != link["child_source_item_id"]
            or not link["job_run_generation"]
            <= link["run_generation"]
            <= link["active_run_generation"]
            or target["fetch_source_item_id"]
            != link["parent_source_item_id"]
            or target["selector_key"] != member["selector_key"]
            or target["expected_media_kind"] != link["media_kind"]
        ):
            raise BackupRestoreError(invalid)
        linked_target_jobs.add(link["job_id"])
        links_by_input_generation.setdefault(
            (link["input_record_id"], link["run_generation"]), []
        ).append(link)
    if linked_target_jobs != set(targets_by_job):
        raise BackupRestoreError(invalid)

    inputs = connection.execute(
        """
        SELECT id, expected_item_count, active_discovery_id,
               active_run_generation
        FROM input_records ORDER BY id
        """
    ).fetchall()
    parents = connection.execute(
        """
        SELECT job.id, job.input_record_id, job.source_item_id,
               job.run_generation, source.platform, source.source_type
        FROM download_jobs AS job
        JOIN source_items AS source ON source.id = job.source_item_id
        WHERE job.job_kind = 'discover'
        ORDER BY job.input_record_id, job.created_at, job.id
        """
    ).fetchall()
    parents_by_input: dict[str, list[sqlite3.Row]] = {}
    for parent in parents:
        parents_by_input.setdefault(parent["input_record_id"], []).append(parent)
    for input_row in inputs:
        input_parents = parents_by_input.get(input_row["id"], [])
        current_links = links_by_input_generation.get(
            (input_row["id"], input_row["active_run_generation"]), []
        )
        if not input_parents:
            if input_row["active_discovery_id"] is not None or current_links:
                raise BackupRestoreError(invalid)
            continue
        if len(input_parents) != 1:
            raise BackupRestoreError(invalid)
        parent = input_parents[0]
        if (
            parent["run_generation"] != input_row["active_run_generation"]
            or parent["platform"] != "x"
            or parent["source_type"] != "x_post"
        ):
            raise BackupRestoreError(invalid)
        active_discovery_id = input_row["active_discovery_id"]
        if active_discovery_id is None:
            if input_row["expected_item_count"] is not None or current_links:
                raise BackupRestoreError(invalid)
            continue
        discovery = discovery_rows.get(active_discovery_id)
        members = discovery_members.get(active_discovery_id)
        if discovery is None or members is None:
            raise BackupRestoreError(invalid)
        expected_relation_ids = {
            row["id"]
            for row in relations_by_discovery.get(active_discovery_id, [])
        }
        if (
            discovery["parent_source_item_id"] != parent["source_item_id"]
            or input_row["expected_item_count"] != discovery["member_count"]
            or len(current_links) != len(members)
            or {row["discovery_id"] for row in current_links}
            != {active_discovery_id}
            or {row["relation_id"] for row in current_links}
            != expected_relation_ids
        ):
            raise BackupRestoreError(invalid)

    graph_jobs = connection.execute(
        """
        SELECT job.id, job.input_record_id, job.job_kind,
               job.attempt_count, job.run_generation,
               job.generation_attempt_count,
               input.active_run_generation
        FROM download_jobs AS job
        JOIN input_records AS input ON input.id = job.input_record_id
        WHERE job.job_kind = 'discover'
           OR EXISTS (
                SELECT 1 FROM download_job_targets AS target
                WHERE target.job_id = job.id
           )
        ORDER BY job.id
        """
    ).fetchall()
    attempts = connection.execute(
        """
        SELECT attempt.job_id, attempt.attempt_no,
               attempt.run_generation, attempt.generation_attempt_no
        FROM job_attempts AS attempt
        JOIN download_jobs AS job ON job.id = attempt.job_id
        WHERE job.job_kind = 'discover'
           OR EXISTS (
                SELECT 1 FROM download_job_targets AS target
                WHERE target.job_id = job.id
           )
        ORDER BY attempt.job_id, attempt.attempt_no
        """
    ).fetchall()
    attempts_by_job: dict[str, list[sqlite3.Row]] = {}
    for attempt in attempts:
        attempts_by_job.setdefault(attempt["job_id"], []).append(attempt)
    for job in graph_jobs:
        job_attempts = attempts_by_job.get(job["id"], [])
        current_generation_attempts = [
            attempt["generation_attempt_no"]
            for attempt in job_attempts
            if attempt["run_generation"] == job["run_generation"]
        ]
        if (
            job["run_generation"] > job["active_run_generation"]
            or (
                job["job_kind"] == "discover"
                and job["run_generation"] != job["active_run_generation"]
            )
            or any(
                attempt["run_generation"] > job["run_generation"]
                or attempt["generation_attempt_no"] < 1
                for attempt in job_attempts
            )
            or job["attempt_count"]
            != max((attempt["attempt_no"] for attempt in job_attempts), default=0)
            or job["generation_attempt_count"]
            != max(current_generation_attempts, default=0)
        ):
            raise BackupRestoreError(invalid)

    reused_jobs = connection.execute(
        """
        SELECT reused.id, reused.input_record_id, reused.source_item_id,
               reused.job_kind, reused.status, reused.route_policy_version,
               reused.credential_profile_id,
               donor.id AS donor_id,
               donor.input_record_id AS donor_input_record_id,
               donor.source_item_id AS donor_source_item_id,
               donor.job_kind AS donor_job_kind,
               donor.status AS donor_status,
               donor.route_policy_version AS donor_route_policy_version,
               donor.credential_profile_id AS donor_credential_profile_id,
               target.fetch_source_item_id,
               target.selector_key, target.expected_media_key,
               target.expected_media_kind,
               donor_target.fetch_source_item_id AS donor_fetch_source_item_id,
               donor_target.selector_key AS donor_selector_key,
               donor_target.expected_media_key AS donor_expected_media_key,
               donor_target.expected_media_kind AS donor_expected_media_kind
        FROM download_jobs AS reused
        JOIN download_jobs AS donor ON donor.id = reused.reused_from_job_id
        LEFT JOIN download_job_targets AS target ON target.job_id = reused.id
        LEFT JOIN download_job_targets AS donor_target
          ON donor_target.job_id = donor.id
        WHERE reused.reused_from_job_id IS NOT NULL
        ORDER BY reused.id
        """
    ).fetchall()
    reused_job_ids = {
        identifier
        for row in reused_jobs
        for identifier in (row["id"], row["donor_id"])
    }
    asset_links: dict[str, set[tuple[str, str, int]]] = {}
    if reused_job_ids:
        placeholders = ",".join("?" for _ in reused_job_ids)
        rows = connection.execute(
            f"""
            SELECT job_id, asset_id, role, ordinal
            FROM job_assets WHERE job_id IN ({placeholders})
            ORDER BY job_id, asset_id, role, ordinal
            """,
            sorted(reused_job_ids),
        ).fetchall()
        for row in rows:
            asset_links.setdefault(row["job_id"], set()).add(
                (row["asset_id"], row["role"], row["ordinal"])
            )
    for reused in reused_jobs:
        target_identity = (
            reused["fetch_source_item_id"],
            reused["selector_key"],
            reused["expected_media_key"],
            reused["expected_media_kind"],
        )
        donor_target_identity = (
            reused["donor_fetch_source_item_id"],
            reused["donor_selector_key"],
            reused["donor_expected_media_key"],
            reused["donor_expected_media_kind"],
        )
        reused_assets = asset_links.get(reused["id"], set())
        if (
            reused["job_kind"] != "download"
            or reused["donor_job_kind"] != "download"
            or reused["status"] != "ready"
            or reused["donor_status"] != "ready"
            or reused["input_record_id"] == reused["donor_input_record_id"]
            or reused["source_item_id"] != reused["donor_source_item_id"]
            or reused["route_policy_version"]
            != reused["donor_route_policy_version"]
            or reused["credential_profile_id"]
            != reused["donor_credential_profile_id"]
            or target_identity != donor_target_identity
            or not reused_assets
            or reused_assets != asset_links.get(reused["donor_id"], set())
        ):
            raise BackupRestoreError(invalid)


def _valid_graph_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and not any(
            unicodedata.category(character).startswith("C") for character in value
        )
    )


def _valid_graph_member_identity(relation: Mapping[str, Any]) -> bool:
    if (
        relation["parent_platform"] != "x"
        or relation["parent_source_type"] != "x_post"
        or relation["child_platform"] != "x"
        or relation["child_source_type"] != "x_attachment"
    ):
        return False
    parent_url = relation["parent_canonical_url"]
    child_url = relation["child_canonical_url"]
    if not isinstance(parent_url, str) or not isinstance(child_url, str):
        return False
    prefix = f"{parent_url}#vdc-media="
    if not child_url.startswith(prefix):
        return False
    try:
        parent = XPostIdentity(relation["parent_source_identity"])
        stable_key = unquote(child_url.removeprefix(prefix))
        return (
            parent_url == f"https://x.com/i/status/{parent.source_id}"
            and child_url
            == x_attachment_canonical_url(parent, parent_url, stable_key)
            and relation["child_source_identity"]
            == x_attachment_source_id(parent, stable_key)
        )
    except GraphValidationError:
        return False


def _valid_graph_media_kind(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) is not None


def _audit_asset_manifest(
    *,
    asset_id: str,
    asset: sqlite3.Row,
    artifacts: Sequence[sqlite3.Row],
    artifact_sizes: Mapping[str, int],
    manifest: Mapping[str, Any],
) -> None:
    if manifest.get("asset_id") != asset_id:
        raise BackupRestoreError("asset manifest identity mismatch")
    original = manifest.get("original")
    if not isinstance(original, dict):
        raise BackupRestoreError("asset manifest original is invalid")
    original_rows = [row for row in artifacts if row["kind"] == "original"]
    original_row = original_rows[0]
    asset_prefix = PurePosixPath("assets") / asset_id
    try:
        original_relative = PurePosixPath(original_row["path"]).relative_to(
            asset_prefix
        ).as_posix()
    except ValueError as exc:
        raise BackupRestoreError("asset original path is invalid") from exc
    if (
        original.get("path") != original_relative
        or original.get("sha256") != asset["sha256"]
        or original.get("size_bytes") != asset["size_bytes"]
    ):
        raise BackupRestoreError("asset manifest original mismatch")

    raw_sidecars = manifest.get("artifacts")
    if not isinstance(raw_sidecars, list):
        raise BackupRestoreError("asset manifest artifacts are invalid")
    manifest_sidecars: set[tuple[Any, ...]] = set()
    for raw in raw_sidecars:
        if not isinstance(raw, dict):
            raise BackupRestoreError("asset manifest artifact is invalid")
        path = _validated_relative_path(raw.get("path"))
        size = raw.get("size_bytes")
        ordinal = raw.get("ordinal")
        digest = raw.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise BackupRestoreError("asset manifest artifact is invalid")
        key = (
            raw.get("kind"),
            path,
            raw.get("mime_type"),
            raw.get("language"),
            digest,
            size,
        )
        if key in manifest_sidecars:
            raise BackupRestoreError("asset manifest artifact is duplicated")
        manifest_sidecars.add(key)
    database_sidecars = set()
    for row in artifacts:
        if row["kind"] not in {"thumbnail", "caption"}:
            continue
        relative = PurePosixPath(row["path"]).relative_to(asset_prefix).as_posix()
        database_sidecars.add(
            (
                row["kind"],
                relative,
                row["mime_type"],
                row["caption_language"],
                row["sha256"],
                artifact_sizes[row["id"]],
            )
        )
    if manifest_sidecars != database_sidecars:
        raise BackupRestoreError("asset manifest artifacts mismatch database")


def _iter_managed_source_files(
    data_root: Path,
    *,
    database_path: Path,
) -> Iterable[tuple[Path, Path]]:
    database_exclusions = {
        database_path,
        Path(str(database_path) + "-wal"),
        Path(str(database_path) + "-shm"),
        Path(str(database_path) + "-journal"),
    }

    def walk(directory: Path, relative: Path) -> Iterable[tuple[Path, Path]]:
        _assert_existing_ancestors_no_links(directory)
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise BackupRestoreError("managed data enumeration failed") from exc
        for entry in entries:
            path = Path(entry.path)
            info = _safe_lstat(path)
            if _is_link_or_reparse(path, info):
                raise BackupRestoreError("managed data contains a link or reparse point")
            child_relative = relative / entry.name
            if child_relative == Path("logs"):
                if not stat.S_ISDIR(info.st_mode):
                    raise BackupRestoreError("volatile path has an invalid type")
                continue
            if child_relative == Path("temporary"):
                if not stat.S_ISDIR(info.st_mode):
                    raise BackupRestoreError("volatile path has an invalid type")
                continue
            if child_relative == Path("assets") / ".staging":
                if not stat.S_ISDIR(info.st_mode):
                    raise BackupRestoreError("volatile path has an invalid type")
                continue
            if path in database_exclusions:
                if not stat.S_ISREG(info.st_mode):
                    raise BackupRestoreError("database sidecar has an invalid type")
                continue
            if stat.S_ISDIR(info.st_mode):
                yield from walk(path, child_relative)
            elif stat.S_ISREG(info.st_mode):
                yield path, child_relative
            else:
                raise BackupRestoreError("managed data contains a special file")

    yield from walk(data_root, Path())


def _sqlite_online_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(str(source), timeout=5.0)
    destination_connection = sqlite3.connect(str(destination), timeout=5.0)
    try:
        source_connection.execute("PRAGMA query_only = ON")
        source_connection.backup(destination_connection)
        destination_connection.commit()
        journal_mode = destination_connection.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            raise BackupRestoreError("database snapshot journal mode is unsafe")
        quick = destination_connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or quick[0] != "ok":
            raise BackupRestoreError("database snapshot quick_check failed")
    finally:
        destination_connection.close()
        source_connection.close()
    _sync_file(destination)


def _database_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
    except sqlite3.Error as exc:
        raise BackupRestoreError("database schema marker is unavailable") from exc
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise BackupRestoreError("database schema marker is invalid")
    return row[0]


def _published_asset_directory_ids(data_root: Path) -> set[str]:
    assets_root = data_root / "assets"
    if not assets_root.exists():
        return set()
    _require_existing_directory(assets_root, label="assets root")
    result = set()
    for entry in os.scandir(assets_root):
        path = Path(entry.path)
        info = _safe_lstat(path)
        if _is_link_or_reparse(path, info):
            raise BackupRestoreError("asset storage contains a link")
        if entry.name == ".staging":
            if not stat.S_ISDIR(info.st_mode):
                raise BackupRestoreError("asset staging path is invalid")
            if any(os.scandir(path)):
                raise BackupRestoreError("restored asset staging is not empty")
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise BackupRestoreError("asset storage contains an unexpected entry")
        result.add(entry.name)
    return result


def _scan_backup_files(root: Path) -> tuple[str, ...]:
    files: list[str] = []

    def walk(directory: Path, relative: PurePosixPath) -> None:
        _assert_existing_ancestors_no_links(directory)
        for entry in sorted(os.scandir(directory), key=lambda item: item.name.casefold()):
            path = Path(entry.path)
            info = _safe_lstat(path)
            if _is_link_or_reparse(path, info):
                raise BackupRestoreError("backup contains a link or reparse point")
            child = relative / entry.name
            if stat.S_ISDIR(info.st_mode):
                walk(path, child)
            elif stat.S_ISREG(info.st_mode):
                files.append(child.as_posix())
            else:
                raise BackupRestoreError("backup contains a special file")

    walk(root, PurePosixPath())
    return tuple(files)


@contextmanager
def _owned_binary_reader(descriptor: int) -> Iterator[BinaryIO]:
    """Transfer a raw descriptor once, preserving any earlier failure on close."""

    try:
        handle = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        with suppress(BaseException):
            os.close(descriptor)
        raise
    with close_binary_on_error(handle):
        yield handle
    handle.close()


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> tuple[str, int]:
    _assert_existing_ancestors_no_links(source)
    initial = _safe_lstat(source)
    if _is_link_or_reparse(source, initial) or not stat.S_ISREG(initial.st_mode):
        raise BackupRestoreError("copy source is not a plain regular file")
    if initial.st_nlink != 1:
        raise BackupRestoreError("copy source has multiple filesystem links")
    if expected_size is not None and initial.st_size != expected_size:
        raise BackupRestoreError("copy source size mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    copied = 0
    created: os.stat_result | None = None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise BackupRestoreError("copy source could not be opened safely") from exc
    try:
        with _owned_binary_reader(descriptor) as source_handle:
            opened = os.fstat(source_handle.fileno())
            if not _same_file_identity(initial, opened):
                raise BackupRestoreError("copy source changed before reading")
            try:
                target_handle = destination.open("xb")
            except OSError as exc:
                raise BackupRestoreError("copy destination could not be created") from exc
            with close_binary_on_error(target_handle):
                created = os.fstat(target_handle.fileno())
                while True:
                    chunk = source_handle.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if expected_size is not None and copied > expected_size:
                        raise BackupRestoreError("copy source grew during reading")
                    target_handle.write(chunk)
                    digest.update(chunk)
                final_source = os.fstat(source_handle.fileno())
                if not _same_file_identity(initial, final_source) or copied != initial.st_size:
                    raise BackupRestoreError("copy source changed during reading")
                target_handle.flush()
                os.fsync(target_handle.fileno())
            target_handle.close()
        value = digest.hexdigest()
        if expected_sha256 is not None and value != expected_sha256:
            raise BackupRestoreError("copy source hash mismatch")
        return value, copied
    except BackupRestoreError:
        discard_created_file(destination, created)
        raise
    except OSError as exc:
        discard_created_file(destination, created)
        raise BackupRestoreError("file copy failed") from exc
    except BaseException:
        discard_created_file(destination, created)
        raise


def _entry_for_file(path: Path, *, relative_path: str) -> _ManifestEntry:
    info = _safe_lstat(path)
    return _ManifestEntry(
        path=_validated_relative_path(relative_path),
        size_bytes=info.st_size,
        sha256=_sha256_regular_file(path),
    )


def _verify_regular_file(
    path: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    _assert_existing_ancestors_no_links(path)
    info = _safe_lstat(path)
    if _is_link_or_reparse(path, info) or not stat.S_ISREG(info.st_mode):
        raise BackupRestoreError("verified path is not a plain regular file")
    if info.st_nlink != 1:
        raise BackupRestoreError("verified file has multiple filesystem links")
    if expected_size is not None and info.st_size != expected_size:
        raise BackupRestoreError("verified file size mismatch")
    if expected_sha256 is not None and _sha256_regular_file(path) != expected_sha256:
        raise BackupRestoreError("verified file hash mismatch")


def _sha256_regular_file(path: Path) -> str:
    _assert_existing_ancestors_no_links(path)
    initial = _safe_lstat(path)
    if _is_link_or_reparse(path, initial) or not stat.S_ISREG(initial.st_mode):
        raise BackupRestoreError("hash source is not a plain regular file")
    if initial.st_nlink != 1:
        raise BackupRestoreError("hash source has multiple filesystem links")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        digest = hashlib.sha256()
        size = 0
        descriptor = os.open(path, flags)
        with _owned_binary_reader(descriptor) as handle:
            opened = os.fstat(handle.fileno())
            if not _same_file_identity(initial, opened):
                raise BackupRestoreError("hash source changed before reading")
            for chunk in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
                size += len(chunk)
                digest.update(chunk)
            final = os.fstat(handle.fileno())
            if not _same_file_identity(initial, final) or size != initial.st_size:
                raise BackupRestoreError("hash source changed during reading")
        return digest.hexdigest()
    except BackupRestoreError:
        raise
    except OSError as exc:
        raise BackupRestoreError("hash source could not be read") from exc


def _read_bounded_regular_file(path: Path, max_bytes: int) -> bytes:
    _assert_existing_ancestors_no_links(path)
    info = _safe_lstat(path)
    if (
        _is_link_or_reparse(path, info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > max_bytes
    ):
        raise BackupRestoreError("backup control file is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with _owned_binary_reader(descriptor) as handle:
            opened = os.fstat(handle.fileno())
            if not _same_file_identity(info, opened):
                raise BackupRestoreError(
                    "backup control file changed before reading"
                )
            value = handle.read(max_bytes + 1)
            final = os.fstat(handle.fileno())
            if not _same_file_identity(info, final):
                raise BackupRestoreError(
                    "backup control file changed during reading"
                )
    except OSError as exc:
        raise BackupRestoreError("backup control file could not be read") from exc
    if len(value) != info.st_size or len(value) > max_bytes:
        raise BackupRestoreError("backup control file changed during reading")
    return value


def _read_json_mapping(path: Path, max_bytes: int) -> Mapping[str, Any]:
    raw = _read_bounded_regular_file(path, max_bytes)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupRestoreError("JSON control file is invalid") from exc
    if not isinstance(value, dict):
        raise BackupRestoreError("JSON control file must be an object")
    return value


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")
    _write_bytes_exclusive(path, encoded)


def _write_text_exclusive(path: Path, value: str) -> None:
    _write_bytes_exclusive(path, value.encode("ascii"))


def _write_bytes_exclusive(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BackupRestoreError("backup control file could not be written") from exc


def _sync_file(path: Path) -> None:
    try:
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BackupRestoreError("backup file durability sync failed") from exc


def _sync_tree(root: Path) -> None:
    directories = [Path(current) for current, _, _ in os.walk(root, topdown=False)]
    for directory in directories:
        _sync_directory(directory)


def _sync_directory(path: Path) -> None:
    try:
        if os.name == "nt":
            _sync_windows_directory(path)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise BackupRestoreError("durability target is not a directory")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BackupRestoreError:
        raise
    except OSError as exc:
        raise BackupRestoreError("directory durability sync failed") from exc


def _sync_windows_directory(path: Path) -> None:
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
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush_file_buffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _validated_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BackupRestoreError("relative path is invalid")
    if "\\" in value or "\x00" in value:
        raise BackupRestoreError("relative path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupRestoreError("relative path escapes its root")
    normalized = path.as_posix()
    if normalized != value or ":" in path.parts[0]:
        raise BackupRestoreError("relative path is not canonical")
    return normalized


def _require_existing_directory(path: Path, *, label: str) -> Path:
    absolute = _require_absolute_non_root(path, label=label)
    return _canonical_existing_entry(absolute, label=label, directory=True)


def _require_existing_regular_file(path: Path, *, label: str) -> Path:
    absolute = _require_absolute_non_root(path, label=label)
    return _canonical_existing_entry(absolute, label=label, directory=False)


def _require_new_target(path: Path, *, label: str) -> Path:
    absolute = _require_absolute_non_root(path, label=label)
    if os.path.lexists(absolute):
        raise BackupRestoreError(f"{label} already exists")
    parent = absolute.parent
    if not os.path.lexists(parent):
        raise BackupRestoreError(f"{label} parent does not exist")
    _assert_existing_ancestors_no_links(parent)
    parent_info = _safe_lstat(parent)
    if not stat.S_ISDIR(parent_info.st_mode):
        raise BackupRestoreError(f"{label} parent does not exist")
    canonical_parent = _canonical_existing_entry(
        parent,
        label=f"{label} parent",
        directory=True,
    )
    canonical_target = canonical_parent / absolute.name
    if os.path.lexists(absolute) or os.path.lexists(canonical_target):
        raise BackupRestoreError(f"{label} already exists")
    return canonical_target


def _canonical_existing_entry(
    absolute: Path,
    *,
    label: str,
    directory: bool,
) -> Path:
    _assert_existing_ancestors_no_links(absolute)
    initial = _safe_lstat(absolute)
    _validate_plain_entry(absolute, initial, label=label, directory=directory)
    try:
        canonical = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BackupRestoreError("required filesystem entry is unavailable") from exc

    for candidate in (absolute, canonical):
        _assert_existing_ancestors_no_links(candidate)
        current = _safe_lstat(candidate)
        _validate_plain_entry(candidate, current, label=label, directory=directory)
        if not _same_path_identity(initial, current):
            raise BackupRestoreError(
                "required filesystem entry changed during validation"
            )
    return canonical


def _validate_plain_entry(
    path: Path,
    info: os.stat_result,
    *,
    label: str,
    directory: bool,
) -> None:
    if directory:
        valid = stat.S_ISDIR(info.st_mode)
        kind = "directory"
    else:
        valid = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        kind = "regular file"
    if _is_link_or_reparse(path, info) or not valid:
        raise BackupRestoreError(f"{label} is not a plain {kind}")


def _require_absolute_non_root(path: Path, *, label: str) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise BackupRestoreError(f"{label} must be an explicit absolute path")
    absolute = Path(os.path.abspath(path))
    if absolute == Path(absolute.anchor):
        raise BackupRestoreError(f"{label} must not be a filesystem root")
    return absolute


def _assert_existing_ancestors_no_links(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            continue
        info = _safe_lstat(current)
        if _is_link_or_reparse(current, info):
            raise BackupRestoreError("path contains a link or reparse point")


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _same_path_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise BackupRestoreError("required filesystem entry is unavailable") from exc


def _is_link_or_reparse(path: Path, info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse and attributes & reparse)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(second.st_mode)
        and second.st_nlink == 1
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _cleanup_stage(stage: Path) -> None:
    if not os.path.lexists(stage):
        return
    with suppress(OSError):
        info = stage.lstat()
        if _is_link_or_reparse(stage, info):
            if stat.S_ISDIR(info.st_mode):
                stage.rmdir()
            else:
                stage.unlink()
            return
        shutil.rmtree(stage)


def _publish_directory(stage: Path, target: Path) -> None:
    try:
        os.replace(stage, target)
    except OSError as exc:
        raise BackupRestoreError("atomic directory publish failed") from exc
    try:
        _sync_directory(target.parent)
    except BackupRestoreError:
        _cleanup_stage(target)
        with suppress(BackupRestoreError):
            _sync_directory(target.parent)
        raise

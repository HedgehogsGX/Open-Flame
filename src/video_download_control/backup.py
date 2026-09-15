"""Fail-closed backup and restore for one Video Download Control data root.

The backup consistency boundary is explicit: an SQLite ``BEGIN IMMEDIATE``
write reservation is held while an online database backup and the immutable
published files are copied.  Volatile top-level ``logs`` and ``temporary`` plus
``assets/.staging`` trees are excluded.  Pending asset commit intents are
rejected, because their filesystem/SQL outcome has not yet been reconciled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from . import __version__
from .backup_files import (
    BackupFileEntry,
    BackupRestoreError,
    assert_existing_ancestors_no_links,
    cleanup_stage,
    copy_regular_file,
    entry_for_file,
    is_link_or_reparse,
    paths_overlap,
    publish_directory,
    read_bounded_regular_file,
    read_json_mapping,
    require_absolute_non_root,
    require_existing_directory,
    require_existing_regular_file,
    require_new_target,
    safe_lstat,
    scan_backup_files,
    sha256_regular_file,
    sqlite_online_backup,
    sync_tree,
    validated_relative_path,
    verify_regular_file,
    write_json_exclusive,
    write_text_exclusive,
)
from .database import SCHEMA_VERSION, Database
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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def create_backup(
    *,
    source_data_root: Path,
    source_database_path: Path,
    backup_target: Path,
) -> BackupResult:
    """Create one immutable, checksummed backup directory."""

    data_root = require_existing_directory(
        source_data_root, label="source data root"
    )
    database_path = require_existing_regular_file(
        source_database_path, label="source database"
    )
    target = require_new_target(backup_target, label="backup target")
    if paths_overlap(data_root, target) or paths_overlap(database_path, target):
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

        payload_entries: list[BackupFileEntry] = []
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

            sqlite_online_backup(database_path, payload_database)
            payload_entries.append(
                entry_for_file(
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
                digest, copied = copy_regular_file(source, destination)
                payload_entries.append(
                    BackupFileEntry(
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
        write_json_exclusive(metadata_path, metadata)
        entries = [
            entry_for_file(
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
        write_json_exclusive(manifest_path, manifest)
        manifest_sha256 = sha256_regular_file(manifest_path)
        write_text_exclusive(
            stage / BACKUP_MANIFEST_HASH_NAME,
            manifest_sha256 + "\n",
        )
        sync_tree(stage)
        publish_directory(stage, target)
        return BackupResult(
            backup_root=target,
            schema_version=schema_version,
            file_count=len(entries),
            total_bytes=sum(entry.size_bytes for entry in entries),
            manifest_sha256=manifest_sha256,
        )
    except BackupRestoreError:
        cleanup_stage(stage)
        raise
    except sqlite3.Error as exc:
        cleanup_stage(stage)
        raise BackupRestoreError("database backup operation failed") from exc
    except (OSError, ValueError, TypeError) as exc:
        cleanup_stage(stage)
        raise BackupRestoreError("backup storage operation failed") from exc


def restore_backup(
    *,
    backup_root: Path,
    restore_data_root: Path,
    restore_database_path: Path,
) -> RestoreResult:
    """Verify and restore a backup into one nonexistent independent root."""

    source = require_existing_directory(backup_root, label="backup root")
    requested_target = require_absolute_non_root(
        restore_data_root, label="restore target"
    )
    target = require_new_target(requested_target, label="restore target")
    requested_database_target = require_absolute_non_root(
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
    assert_existing_ancestors_no_links(database_target)
    if paths_overlap(source, target):
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
            digest, copied = copy_regular_file(
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
        sync_tree(stage)
        publish_directory(stage, target)
        return RestoreResult(
            restore_root=target,
            database_path=target / database_relative,
            schema_version=SCHEMA_VERSION,
            file_count=restored_file_count,
            total_bytes=total_restore_bytes,
            manifest_sha256=manifest_sha256,
        )
    except BackupRestoreError:
        cleanup_stage(stage)
        raise
    except sqlite3.Error as exc:
        cleanup_stage(stage)
        raise BackupRestoreError("restored database verification failed") from exc
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        cleanup_stage(stage)
        raise BackupRestoreError("restore storage operation failed") from exc


def _load_and_verify_backup(
    backup_root: Path,
) -> tuple[str, tuple[BackupFileEntry, ...], Mapping[str, Any]]:
    hash_path = backup_root / BACKUP_MANIFEST_HASH_NAME
    manifest_path = backup_root / BACKUP_MANIFEST_NAME
    hash_text = read_bounded_regular_file(hash_path, 128).decode(
        "ascii", errors="strict"
    )
    if not re.fullmatch(r"[0-9a-f]{64}\n", hash_text):
        raise BackupRestoreError("backup manifest hash record is invalid")
    expected_manifest_hash = hash_text.strip()
    if sha256_regular_file(manifest_path) != expected_manifest_hash:
        raise BackupRestoreError("backup manifest hash mismatch")
    manifest = read_json_mapping(manifest_path, MAX_MANIFEST_BYTES)
    if (
        manifest.get("format_version") != BACKUP_FORMAT_VERSION
        or manifest.get("algorithm") != "sha256"
        or manifest.get("metadata_path") != BACKUP_METADATA_NAME
    ):
        raise BackupRestoreError("backup manifest header is invalid")
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= MAX_BACKUP_ENTRIES:
        raise BackupRestoreError("backup manifest entry count is invalid")
    entries: list[BackupFileEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise BackupRestoreError("backup manifest entry is invalid")
        path = validated_relative_path(raw.get("path"))
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
        entry = BackupFileEntry(path=path, size_bytes=size, sha256=digest)
        candidate = backup_root.joinpath(*PurePosixPath(path).parts)
        verify_regular_file(
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
        path.casefold() for path in scan_backup_files(backup_root)
    }
    if actual_files != expected_files:
        raise BackupRestoreError("backup contains untracked or missing files")
    metadata = read_json_mapping(
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
        relative = validated_relative_path(artifact["path"])
        if not relative.startswith(f"assets/{artifact['asset_id']}/"):
            raise BackupRestoreError("artifact path does not match its asset")
        if relative.casefold() in seen_paths:
            raise BackupRestoreError("artifact paths are not unique")
        seen_paths.add(relative.casefold())
        path = data_root.joinpath(*PurePosixPath(relative).parts)
        verify_regular_file(path, expected_sha256=artifact["sha256"])
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
        manifest = read_json_mapping(manifest_path, MAX_ASSET_MANIFEST_BYTES)
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
        path = validated_relative_path(raw.get("path"))
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
        assert_existing_ancestors_no_links(directory)
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise BackupRestoreError("managed data enumeration failed") from exc
        for entry in entries:
            path = Path(entry.path)
            info = safe_lstat(path)
            if is_link_or_reparse(path, info):
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
    require_existing_directory(assets_root, label="assets root")
    result = set()
    for entry in os.scandir(assets_root):
        path = Path(entry.path)
        info = safe_lstat(path)
        if is_link_or_reparse(path, info):
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

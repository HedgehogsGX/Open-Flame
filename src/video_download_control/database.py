from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Iterator


SCHEMA_VERSION = 8
_INITIALIZE_LOCK = Lock()

REQUIRED_TABLES = {
    "schema_migrations",
    "batches",
    "input_records",
    "source_items",
    "source_discoveries",
    "source_relations",
    "download_jobs",
    "job_attempts",
    "input_relation_jobs",
    "download_job_targets",
    "media_assets",
    "job_assets",
    "artifacts",
    "captions",
    "credential_profiles",
    "platform_capabilities",
    "asset_commit_intents",
    "queue_control",
    "platform_circuits",
}

SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    queued_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_records (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    raw_text TEXT NOT NULL,
    submitted_url TEXT,
    canonical_url TEXT,
    platform TEXT,
    source_type TEXT,
    source_id TEXT,
    ordinal INTEGER NOT NULL,
    expected_item_count INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    duplicate_of_input_record_id TEXT REFERENCES input_records(id),
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, ordinal)
);

CREATE TABLE IF NOT EXISTS source_items (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT,
    author TEXT,
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_identity
ON source_items(platform, source_type, source_id)
WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS source_relations (
    id TEXT PRIMARY KEY,
    parent_source_item_id TEXT NOT NULL REFERENCES source_items(id),
    child_source_item_id TEXT NOT NULL REFERENCES source_items(id),
    relation_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    discovered_by_attempt_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(parent_source_item_id, relation_type, ordinal)
);

CREATE TABLE IF NOT EXISTS download_jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    input_record_id TEXT NOT NULL REFERENCES input_records(id) ON DELETE CASCADE,
    source_item_id TEXT NOT NULL REFERENCES source_items(id),
    job_kind TEXT NOT NULL DEFAULT 'download',
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    final_error_code TEXT,
    route_policy_version TEXT NOT NULL,
    credential_profile_id TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(input_record_id, source_item_id, job_kind)
);

CREATE TABLE IF NOT EXISTS job_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES download_jobs(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    adapter TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    worker_image_digest TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    diagnostic TEXT,
    discovered_item_count INTEGER,
    discovery_snapshot_hash TEXT,
    UNIQUE(job_id, attempt_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_job_per_source
ON download_jobs(source_item_id)
WHERE status IN (
    'queued', 'probing', 'downloading', 'postprocessing', 'verifying', 'ready'
);

CREATE TABLE IF NOT EXISTS media_assets (
    id TEXT PRIMARY KEY,
    source_item_id TEXT NOT NULL REFERENCES source_items(id),
    media_key TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    duration_seconds REAL,
    container TEXT,
    codec TEXT,
    width INTEGER,
    height INTEGER,
    size_bytes INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_item_id, media_key)
);

CREATE TABLE IF NOT EXISTS job_assets (
    job_id TEXT NOT NULL REFERENCES download_jobs(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(job_id, asset_id, role)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    mime_type TEXT,
    sha256 TEXT NOT NULL,
    parent_artifact_id TEXT REFERENCES artifacts(id),
    tool_name TEXT,
    tool_version TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id) ON DELETE CASCADE,
    language TEXT,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credential_profiles (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    expires_at TEXT,
    last_verified_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(platform, name)
);

CREATE TABLE IF NOT EXISTS platform_capabilities (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_type TEXT NOT NULL,
    adapter TEXT NOT NULL,
    status TEXT NOT NULL,
    last_verified_at TEXT,
    tested_version TEXT,
    sample_set_version TEXT,
    notes TEXT,
    UNIQUE(platform, source_type, adapter)
);

CREATE INDEX IF NOT EXISTS idx_input_records_batch ON input_records(batch_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_download_jobs_status ON download_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_download_jobs_batch ON download_jobs(batch_id, created_at);
"""

MIGRATION_2_SQL = """
BEGIN IMMEDIATE;

ALTER TABLE download_jobs ADD COLUMN lease_token TEXT;
ALTER TABLE download_jobs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE download_jobs ADD COLUMN available_at TEXT;
ALTER TABLE download_jobs ADD COLUMN cancel_requested_at TEXT;
ALTER TABLE download_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE job_attempts ADD COLUMN lease_token TEXT;
ALTER TABLE job_attempts ADD COLUMN exit_code INTEGER;

ALTER TABLE platform_capabilities RENAME TO platform_capabilities_v1;
CREATE TABLE platform_capabilities (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_type TEXT NOT NULL,
    adapter TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    last_verified_at TEXT,
    tested_version TEXT,
    sample_set_version TEXT,
    notes TEXT
);
INSERT INTO platform_capabilities(
    id, platform, source_type, adapter, adapter_version, environment,
    status, last_verified_at, tested_version, sample_set_version, notes
)
SELECT
    id, platform, source_type, adapter, 'unknown', 'unknown',
    status, last_verified_at, tested_version, sample_set_version, notes
FROM platform_capabilities_v1;
DROP TABLE platform_capabilities_v1;

CREATE UNIQUE INDEX idx_capability_evidence_identity
ON platform_capabilities(
    platform, source_type, adapter, adapter_version, environment
);

UPDATE download_jobs
SET available_at = COALESCE(available_at, created_at);

CREATE TEMP TABLE migration2_duplicate_jobs (
    job_id TEXT PRIMARY KEY,
    input_record_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    kept_input_record_id TEXT NOT NULL
);
INSERT INTO migration2_duplicate_jobs(
    job_id, input_record_id, batch_id, kept_input_record_id
)
SELECT job_id, input_record_id, batch_id, kept_input_record_id
FROM (
    SELECT
        id AS job_id,
        input_record_id,
        batch_id,
        FIRST_VALUE(input_record_id) OVER (
            PARTITION BY source_item_id
            ORDER BY
                CASE WHEN status = 'ready' THEN 0 ELSE 1 END,
                created_at,
                id
        ) AS kept_input_record_id,
        ROW_NUMBER() OVER (
            PARTITION BY source_item_id
            ORDER BY
                CASE WHEN status = 'ready' THEN 0 ELSE 1 END,
                created_at,
                id
        ) AS duplicate_rank
    FROM download_jobs
    WHERE status IN (
        'queued', 'probing', 'downloading', 'postprocessing', 'verifying', 'ready'
    )
)
WHERE duplicate_rank > 1;

UPDATE input_records
SET status = 'duplicate',
    duplicate_of_input_record_id = (
        SELECT kept_input_record_id
        FROM migration2_duplicate_jobs
        WHERE input_record_id = input_records.id
    )
WHERE id IN (SELECT input_record_id FROM migration2_duplicate_jobs);

UPDATE download_jobs
SET status = 'canceled',
    lease_owner = NULL,
    lease_expires_at = NULL,
    lease_token = NULL,
    heartbeat_at = NULL,
    cancel_requested_at = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE id IN (SELECT job_id FROM migration2_duplicate_jobs);

UPDATE batches
SET queued_count = (
        SELECT COUNT(*) FROM input_records
        WHERE input_records.batch_id = batches.id AND status = 'queued'
    ),
    failed_count = (
        SELECT COUNT(*) FROM input_records
        WHERE input_records.batch_id = batches.id AND status = 'failed'
    ),
    duplicate_count = (
        SELECT COUNT(*) FROM input_records
        WHERE input_records.batch_id = batches.id AND status = 'duplicate'
    ),
    status = CASE
        WHEN EXISTS (
            SELECT 1 FROM input_records
            WHERE input_records.batch_id = batches.id AND status = 'queued'
        ) THEN 'queued'
        WHEN EXISTS (
            SELECT 1 FROM input_records
            WHERE input_records.batch_id = batches.id AND status = 'failed'
        ) THEN 'failed'
        WHEN EXISTS (
            SELECT 1 FROM input_records
            WHERE input_records.batch_id = batches.id AND status = 'duplicate'
        ) THEN 'duplicate'
        ELSE status
    END,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

DROP TABLE migration2_duplicate_jobs;

CREATE INDEX IF NOT EXISTS idx_jobs_claim
ON download_jobs(status, available_at, lease_expires_at, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_job_per_source
ON download_jobs(source_item_id)
WHERE status IN (
    'queued', 'probing', 'downloading', 'postprocessing', 'verifying', 'ready'
);

INSERT INTO schema_migrations(version, applied_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""

MIGRATION_3_SQL = """
BEGIN IMMEDIATE;

ALTER TABLE batches ADD COLUMN ready_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN canceled_count INTEGER NOT NULL DEFAULT 0;

UPDATE batches
SET ready_count = (
        SELECT COUNT(*) FROM input_records
        WHERE input_records.batch_id = batches.id AND status = 'ready'
    ),
    canceled_count = (
        SELECT COUNT(*) FROM input_records
        WHERE input_records.batch_id = batches.id AND status = 'canceled'
    );

INSERT INTO schema_migrations(version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""

MIGRATION_4_SQL = """
BEGIN IMMEDIATE;

CREATE TABLE asset_commit_intents (
    asset_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES download_jobs(id),
    attempt_id TEXT NOT NULL REFERENCES job_attempts(id),
    lease_token TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_asset_commit_intents_attempt
ON asset_commit_intents(attempt_id, created_at, asset_id);

CREATE INDEX idx_asset_commit_intents_job
ON asset_commit_intents(job_id, created_at, asset_id);

INSERT INTO schema_migrations(version, applied_at)
VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""

MIGRATION_5_SQL = """
BEGIN IMMEDIATE;

CREATE TABLE queue_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    reason TEXT,
    paused_at TEXT,
    resumed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (paused = 0 OR (reason IS NOT NULL AND paused_at IS NOT NULL))
);

INSERT INTO queue_control(id, paused, reason, paused_at, resumed_at, updated_at)
VALUES (1, 0, NULL, NULL, NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE platform_circuits (
    platform TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('closed', 'open', 'half_open')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    last_error_code TEXT,
    opened_at TEXT,
    cooldown_until TEXT,
    requires_manual_reset INTEGER NOT NULL DEFAULT 0
        CHECK (requires_manual_reset IN (0, 1)),
    probe_job_id TEXT REFERENCES download_jobs(id),
    probe_lease_token TEXT,
    updated_at TEXT NOT NULL,
    CHECK (
        (state = 'half_open' AND probe_job_id IS NOT NULL
            AND probe_lease_token IS NOT NULL)
        OR
        (state <> 'half_open' AND probe_job_id IS NULL
            AND probe_lease_token IS NULL)
    )
);

CREATE INDEX idx_platform_circuits_state
ON platform_circuits(state, requires_manual_reset, cooldown_until);

INSERT INTO schema_migrations(version, applied_at)
VALUES (5, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""

MIGRATION_6_SQL = """
BEGIN IMMEDIATE;

ALTER TABLE asset_commit_intents ADD COLUMN recovery_token TEXT;
ALTER TABLE asset_commit_intents ADD COLUMN recovery_expires_at TEXT;

CREATE INDEX idx_asset_commit_intents_recovery
ON asset_commit_intents(recovery_expires_at, created_at, asset_id);

INSERT INTO schema_migrations(version, applied_at)
VALUES (6, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""

MIGRATION_7_SQL = """
BEGIN IMMEDIATE;

ALTER TABLE credential_profiles ADD COLUMN disabled_at TEXT;

INSERT INTO schema_migrations(version, applied_at)
VALUES (7, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""


MIGRATION_8_CREATE_DISCOVERY_SQL = """
CREATE TABLE source_discoveries (
    id TEXT PRIMARY KEY,
    parent_source_item_id TEXT NOT NULL REFERENCES source_items(id),
    relation_type TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    members_json TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (member_count BETWEEN 1 AND 50),
    discovered_by_attempt_id TEXT REFERENCES job_attempts(id),
    created_at TEXT NOT NULL,
    UNIQUE(parent_source_item_id, relation_type, snapshot_hash)
);

CREATE INDEX idx_source_discoveries_parent
ON source_discoveries(parent_source_item_id, relation_type, created_at, id);

CREATE TRIGGER trg_source_discoveries_no_update
BEFORE UPDATE ON source_discoveries
BEGIN
    SELECT RAISE(ABORT, 'source discoveries are immutable');
END;

CREATE TRIGGER trg_source_discoveries_no_delete
BEFORE DELETE ON source_discoveries
BEGIN
    SELECT RAISE(ABORT, 'source discoveries are immutable');
END;
"""


MIGRATION_8_REBUILD_RELATIONS_SQL = """
ALTER TABLE source_relations RENAME TO source_relations_v7;

CREATE TABLE source_relations (
    id TEXT PRIMARY KEY,
    discovery_id TEXT NOT NULL REFERENCES source_discoveries(id),
    parent_source_item_id TEXT NOT NULL REFERENCES source_items(id),
    child_source_item_id TEXT NOT NULL REFERENCES source_items(id),
    relation_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    media_kind TEXT NOT NULL,
    discovered_by_attempt_id TEXT REFERENCES job_attempts(id),
    created_at TEXT NOT NULL,
    UNIQUE(discovery_id, ordinal),
    UNIQUE(discovery_id, id)
);

CREATE INDEX idx_source_relations_discovery
ON source_relations(discovery_id, ordinal, id);

CREATE TRIGGER trg_source_relations_no_update
BEFORE UPDATE ON source_relations
BEGIN
    SELECT RAISE(ABORT, 'source relations are immutable');
END;

CREATE TRIGGER trg_source_relations_no_delete
BEFORE DELETE ON source_relations
BEGIN
    SELECT RAISE(ABORT, 'source relations are immutable');
END;
"""


MIGRATION_8_REBUILD_JOBS_SQL = """
ALTER TABLE download_jobs RENAME TO download_jobs_v7;

CREATE TABLE download_jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    input_record_id TEXT NOT NULL REFERENCES input_records(id) ON DELETE CASCADE,
    source_item_id TEXT NOT NULL REFERENCES source_items(id),
    job_kind TEXT NOT NULL DEFAULT 'download',
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    final_error_code TEXT,
    route_policy_version TEXT NOT NULL,
    credential_profile_id TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_token TEXT,
    heartbeat_at TEXT,
    available_at TEXT,
    cancel_requested_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    run_generation INTEGER NOT NULL DEFAULT 1 CHECK (run_generation >= 1),
    generation_attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (generation_attempt_count >= 0),
    reused_from_job_id TEXT REFERENCES download_jobs(id)
);

INSERT INTO download_jobs(
    id, batch_id, input_record_id, source_item_id, job_kind,
    status, progress, final_error_code, route_policy_version,
    credential_profile_id, lease_owner, lease_expires_at,
    created_at, updated_at, lease_token, heartbeat_at,
    available_at, cancel_requested_at, attempt_count,
    run_generation, generation_attempt_count, reused_from_job_id
)
SELECT
    id, batch_id, input_record_id, source_item_id, job_kind,
    status, progress, final_error_code, route_policy_version,
    credential_profile_id, lease_owner, lease_expires_at,
    created_at, updated_at, lease_token, heartbeat_at,
    available_at, cancel_requested_at, attempt_count,
    1, attempt_count, NULL
FROM download_jobs_v7;

DROP TABLE download_jobs_v7;

CREATE INDEX idx_download_jobs_status
ON download_jobs(status, created_at);

CREATE INDEX idx_download_jobs_batch
ON download_jobs(batch_id, created_at);

CREATE INDEX idx_jobs_claim
ON download_jobs(status, available_at, lease_expires_at, created_at);

CREATE UNIQUE INDEX idx_job_input_source_kind_generation
ON download_jobs(input_record_id, source_item_id, job_kind, run_generation);
"""


MIGRATION_8_FINISH_SQL = """
ALTER TABLE input_records
ADD COLUMN active_discovery_id TEXT REFERENCES source_discoveries(id);
ALTER TABLE input_records
ADD COLUMN active_run_generation INTEGER NOT NULL DEFAULT 1
    CHECK (active_run_generation >= 1);
ALTER TABLE input_records ADD COLUMN cancel_requested_at TEXT;

ALTER TABLE job_attempts
ADD COLUMN run_generation INTEGER NOT NULL DEFAULT 1
    CHECK (run_generation >= 1);
ALTER TABLE job_attempts
ADD COLUMN generation_attempt_no INTEGER NOT NULL DEFAULT 0
    CHECK (generation_attempt_no >= 0);
UPDATE job_attempts SET generation_attempt_no = attempt_no;

ALTER TABLE batches
ADD COLUMN partial_success_count INTEGER NOT NULL DEFAULT 0;
UPDATE batches
SET partial_success_count = (
    SELECT COUNT(*)
    FROM input_records
    WHERE input_records.batch_id = batches.id
      AND input_records.status = 'partial_success'
);

CREATE TABLE input_relation_jobs (
    input_record_id TEXT NOT NULL REFERENCES input_records(id) ON DELETE CASCADE,
    discovery_id TEXT NOT NULL REFERENCES source_discoveries(id),
    run_generation INTEGER NOT NULL DEFAULT 1 CHECK (run_generation >= 1),
    relation_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES download_jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(input_record_id, run_generation, relation_id),
    FOREIGN KEY(discovery_id, relation_id)
        REFERENCES source_relations(discovery_id, id)
);

CREATE INDEX idx_input_relation_jobs_discovery
ON input_relation_jobs(discovery_id, input_record_id, run_generation, relation_id);

CREATE INDEX idx_input_relation_jobs_job
ON input_relation_jobs(job_id, input_record_id, run_generation);

CREATE TABLE download_job_targets (
    job_id TEXT PRIMARY KEY REFERENCES download_jobs(id) ON DELETE CASCADE,
    fetch_source_item_id TEXT NOT NULL REFERENCES source_items(id),
    selector_key TEXT NOT NULL,
    expected_media_key TEXT NOT NULL,
    expected_media_kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_download_job_targets_fetch_source
ON download_job_targets(fetch_source_item_id, job_id);
"""


def _legacy_discovery_rows(
    connection: sqlite3.Connection,
) -> tuple[
    list[tuple[object, ...]],
    list[tuple[object, ...]],
]:
    """Build deterministic immutable snapshots for pre-graph relations."""

    rows = connection.execute(
        """
        SELECT id, parent_source_item_id, child_source_item_id,
               relation_type, ordinal, discovered_by_attempt_id, created_at
        FROM source_relations_v7
        ORDER BY parent_source_item_id, relation_type, ordinal, id
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["parent_source_item_id"], row["relation_type"])].append(row)

    discoveries: list[tuple[object, ...]] = []
    relations: list[tuple[object, ...]] = []
    for (parent_id, relation_type), members in sorted(grouped.items()):
        membership = [
            {
                "child_source_item_id": row["child_source_item_id"],
                "media_kind": "unknown",
                "ordinal": int(row["ordinal"]),
                "selector_key": None,
            }
            for row in members
        ]
        members_json = json.dumps(
            membership,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_hash = hashlib.sha256(members_json.encode("utf-8")).hexdigest()
        identity = f"{parent_id}\0{relation_type}\0{snapshot_hash}".encode("utf-8")
        discovery_id = "legacy:v1:" + hashlib.sha256(identity).hexdigest()
        created_at = min(str(row["created_at"]) for row in members)
        discovered_by_attempt_id = next(
            (
                row["discovered_by_attempt_id"]
                for row in members
                if row["discovered_by_attempt_id"] is not None
            ),
            None,
        )
        discoveries.append(
            (
                discovery_id,
                parent_id,
                relation_type,
                snapshot_hash,
                members_json,
                len(members),
                discovered_by_attempt_id,
                created_at,
            )
        )
        relations.extend(
            (
                row["id"],
                discovery_id,
                row["parent_source_item_id"],
                row["child_source_item_id"],
                row["relation_type"],
                row["ordinal"],
                "unknown",
                row["discovered_by_attempt_id"],
                row["created_at"],
            )
            for row in members
        )
    return discoveries, relations


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite serializes writers, but a version check followed by
        # ``executescript`` is not itself atomic. Two fresh processes can both
        # observe version 0 and then attempt the same ALTER TABLE migration.
        # Keep migrations through Schema 8 under one IMMEDIATE transaction.
        # Schema 8 below uses its own FK-off transaction because it rebuilds
        # tables; the in-process lock also prevents competing journal-mode
        # setup on a brand-new file.
        with _INITIALIZE_LOCK:
            self._enable_wal()
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()
                current_version = int(row["version"] or 0)
                if current_version == 0:
                    self._execute_in_current_transaction(connection, SCHEMA_V1_SQL)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, applied_at)
                        VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                    current_version = 1
                if current_version < 2:
                    self._execute_in_current_transaction(connection, MIGRATION_2_SQL)
                    current_version = 2
                if current_version < 3:
                    self._execute_in_current_transaction(connection, MIGRATION_3_SQL)
                    current_version = 3
                if current_version < 4:
                    self._execute_in_current_transaction(connection, MIGRATION_4_SQL)
                    current_version = 4
                if current_version < 5:
                    self._execute_in_current_transaction(connection, MIGRATION_5_SQL)
                    current_version = 5
                if current_version < 6:
                    self._execute_in_current_transaction(connection, MIGRATION_6_SQL)
                    current_version = 6
                if current_version < 7:
                    self._execute_in_current_transaction(connection, MIGRATION_7_SQL)
                    current_version = 7
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"数据库 schema {current_version} 高于程序支持的 {SCHEMA_VERSION}"
                    )
            # Migration 8 rebuilds FK-connected tables.  SQLite requires the
            # foreign_keys mode to be changed outside a transaction, so the
            # earlier migrations are deliberately committed before entering
            # its own fail-closed transaction.
            if current_version < 8:
                self._migrate_to_8()

    def _migrate_to_8(self) -> None:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA legacy_alter_table = ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise RuntimeError("schema 8 migration could not disable foreign keys")
            if connection.execute("PRAGMA legacy_alter_table").fetchone()[0] != 1:
                raise RuntimeError(
                    "schema 8 migration could not enable legacy alter table"
                )

            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            current_version = int(row["version"] or 0)
            if current_version == 8:
                connection.commit()
                return
            if current_version != 7:
                raise RuntimeError(
                    f"schema 8 migration requires schema 7, found {current_version}"
                )

            invalid_attempt = connection.execute(
                """
                SELECT relation.id
                FROM source_relations AS relation
                LEFT JOIN job_attempts AS attempt
                  ON attempt.id = relation.discovered_by_attempt_id
                WHERE relation.discovered_by_attempt_id IS NOT NULL
                  AND attempt.id IS NULL
                LIMIT 1
                """
            ).fetchone()
            if invalid_attempt is not None:
                raise RuntimeError(
                    "schema 8 migration found an invalid legacy relation attempt"
                )

            self._execute_in_current_transaction(
                connection, MIGRATION_8_CREATE_DISCOVERY_SQL
            )
            self._execute_in_current_transaction(
                connection, MIGRATION_8_REBUILD_RELATIONS_SQL
            )
            discoveries, relations = _legacy_discovery_rows(connection)
            connection.executemany(
                """
                INSERT INTO source_discoveries(
                    id, parent_source_item_id, relation_type, snapshot_hash,
                    members_json, member_count, discovered_by_attempt_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                discoveries,
            )
            connection.executemany(
                """
                INSERT INTO source_relations(
                    id, discovery_id, parent_source_item_id,
                    child_source_item_id, relation_type, ordinal,
                    media_kind, discovered_by_attempt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                relations,
            )
            connection.execute("DROP TABLE source_relations_v7")

            self._execute_in_current_transaction(
                connection, MIGRATION_8_REBUILD_JOBS_SQL
            )
            self._execute_in_current_transaction(connection, MIGRATION_8_FINISH_SQL)

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                table = str(violations[0][0])
                raise RuntimeError(
                    f"schema 8 migration foreign key check failed for {table}"
                )
            connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (8, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.close()

    @staticmethod
    def _execute_in_current_transaction(
        connection: sqlite3.Connection, script: str
    ) -> None:
        """Execute a migration script without ``executescript`` auto-commits."""

        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            sql = statement.strip()
            statement = ""
            if not sql or sql.upper() in {"BEGIN IMMEDIATE;", "COMMIT;"}:
                continue
            connection.execute(sql)
        if statement.strip():
            raise RuntimeError("incomplete database migration statement")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _enable_wal(self) -> None:
        """Set the persistent journal mode once, with bounded lock retries."""

        for attempt in range(5):
            connection = sqlite3.connect(self.path, timeout=5.0)
            try:
                connection.execute("PRAGMA busy_timeout = 5000")
                row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if row is None or str(row[0]).lower() != "wal":
                    raise RuntimeError("SQLite WAL mode could not be enabled")
                return
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
                if not locked or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
            finally:
                connection.close()

    def ping(self) -> bool:
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT 1 AS ok").fetchone()
            return bool(row and row["ok"] == 1)
        except sqlite3.Error:
            return False

    def readiness(self) -> tuple[bool, str]:
        """Verify the migration marker and tables needed by the control plane."""
        try:
            with self.connect() as connection:
                migration = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                indexes = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
                triggers = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            if not migration or migration["version"] != SCHEMA_VERSION:
                return False, "schema_version_mismatch"
            present = {row["name"] for row in rows}
            if missing := REQUIRED_TABLES - present:
                return False, "missing_tables:" + ",".join(sorted(missing))
            required_columns = {
                "download_jobs": {
                    "lease_token",
                    "heartbeat_at",
                    "available_at",
                    "cancel_requested_at",
                    "attempt_count",
                    "run_generation",
                    "generation_attempt_count",
                    "reused_from_job_id",
                },
                "job_attempts": {
                    "lease_token",
                    "exit_code",
                    "run_generation",
                    "generation_attempt_no",
                },
                "platform_capabilities": {"adapter_version", "environment"},
                "credential_profiles": {"disabled_at"},
                "batches": {
                    "ready_count",
                    "canceled_count",
                    "partial_success_count",
                },
                "input_records": {
                    "active_discovery_id",
                    "active_run_generation",
                    "cancel_requested_at",
                },
                "source_discoveries": {
                    "id",
                    "parent_source_item_id",
                    "relation_type",
                    "snapshot_hash",
                    "members_json",
                    "member_count",
                    "discovered_by_attempt_id",
                    "created_at",
                },
                "source_relations": {
                    "id",
                    "discovery_id",
                    "parent_source_item_id",
                    "child_source_item_id",
                    "relation_type",
                    "ordinal",
                    "media_kind",
                    "discovered_by_attempt_id",
                    "created_at",
                },
                "input_relation_jobs": {
                    "input_record_id",
                    "discovery_id",
                    "run_generation",
                    "relation_id",
                    "job_id",
                    "created_at",
                },
                "download_job_targets": {
                    "job_id",
                    "fetch_source_item_id",
                    "selector_key",
                    "expected_media_key",
                    "expected_media_kind",
                    "created_at",
                },
                "asset_commit_intents": {
                    "asset_id",
                    "job_id",
                    "attempt_id",
                    "lease_token",
                    "created_at",
                    "recovery_token",
                    "recovery_expires_at",
                },
                "queue_control": {
                    "id",
                    "paused",
                    "reason",
                    "paused_at",
                    "resumed_at",
                    "updated_at",
                },
                "platform_circuits": {
                    "platform",
                    "state",
                    "consecutive_failures",
                    "last_error_code",
                    "opened_at",
                    "cooldown_until",
                    "requires_manual_reset",
                    "probe_job_id",
                    "probe_lease_token",
                    "updated_at",
                },
            }
            with self.connect() as connection:
                for table, expected in required_columns.items():
                    actual = {
                        row["name"]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    if missing_columns := expected - actual:
                        return (
                            False,
                            f"missing_columns:{table}:"
                            + ",".join(sorted(missing_columns)),
                        )
            present_indexes = {row["name"] for row in indexes}
            required_indexes = {
                "idx_jobs_claim",
                "idx_capability_evidence_identity",
                "idx_job_input_source_kind_generation",
                "idx_asset_commit_intents_attempt",
                "idx_asset_commit_intents_job",
                "idx_asset_commit_intents_recovery",
                "idx_platform_circuits_state",
                "idx_source_discoveries_parent",
                "idx_source_relations_discovery",
                "idx_input_relation_jobs_discovery",
                "idx_input_relation_jobs_job",
                "idx_download_job_targets_fetch_source",
            }
            if missing_indexes := required_indexes - present_indexes:
                return False, "missing_indexes:" + ",".join(sorted(missing_indexes))
            if "idx_one_live_job_per_source" in present_indexes:
                return False, "forbidden_indexes:idx_one_live_job_per_source"
            required_triggers = {
                "trg_source_discoveries_no_update",
                "trg_source_discoveries_no_delete",
                "trg_source_relations_no_update",
                "trg_source_relations_no_delete",
            }
            present_triggers = {row["name"] for row in triggers}
            if missing_triggers := required_triggers - present_triggers:
                return False, "missing_triggers:" + ",".join(
                    sorted(missing_triggers)
                )
            trigger_sql = {row["name"]: row["sql"] for row in triggers}
            for name, event in {
                "trg_source_discoveries_no_update": (
                    "BEFORE UPDATE ON SOURCE_DISCOVERIES"
                ),
                "trg_source_discoveries_no_delete": (
                    "BEFORE DELETE ON SOURCE_DISCOVERIES"
                ),
                "trg_source_relations_no_update": (
                    "BEFORE UPDATE ON SOURCE_RELATIONS"
                ),
                "trg_source_relations_no_delete": (
                    "BEFORE DELETE ON SOURCE_RELATIONS"
                ),
            }.items():
                normalized = " ".join(str(trigger_sql[name]).upper().split())
                if event not in normalized or "RAISE(ABORT" not in normalized:
                    return False, f"malformed_trigger:{name}"
            with self.connect() as connection:
                queue_control = connection.execute(
                    "SELECT id FROM queue_control WHERE id = 1"
                ).fetchone()
                job_index = next(
                    (
                        row
                        for row in connection.execute(
                            "PRAGMA index_list(download_jobs)"
                        ).fetchall()
                        if row["name"] == "idx_job_input_source_kind_generation"
                    ),
                    None,
                )
                job_index_columns = (
                    tuple(
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA index_info(idx_job_input_source_kind_generation)"
                        ).fetchall()
                    )
                    if job_index is not None
                    else ()
                )
                if (
                    job_index is None
                    or not bool(job_index["unique"])
                    or job_index_columns
                    != (
                        "input_record_id",
                        "source_item_id",
                        "job_kind",
                        "run_generation",
                    )
                ):
                    return (
                        False,
                        "malformed_index:idx_job_input_source_kind_generation",
                    )
                relation_unique_shapes = {
                    tuple(
                        column["name"]
                        for column in connection.execute(
                            f"PRAGMA index_info({row['name']})"
                        ).fetchall()
                    )
                    for row in connection.execute(
                        "PRAGMA index_list(source_relations)"
                    ).fetchall()
                    if bool(row["unique"])
                }
                if ("discovery_id", "ordinal") not in relation_unique_shapes:
                    return False, "missing_unique:source_relations:discovery_id,ordinal"
                required_foreign_keys = {
                    "source_discoveries": {
                        ("parent_source_item_id", "source_items", "id"),
                        ("discovered_by_attempt_id", "job_attempts", "id"),
                    },
                    "source_relations": {
                        ("discovery_id", "source_discoveries", "id"),
                        ("parent_source_item_id", "source_items", "id"),
                        ("child_source_item_id", "source_items", "id"),
                        ("discovered_by_attempt_id", "job_attempts", "id"),
                    },
                    "input_records": {
                        ("active_discovery_id", "source_discoveries", "id"),
                    },
                    "download_jobs": {
                        ("reused_from_job_id", "download_jobs", "id"),
                    },
                    "input_relation_jobs": {
                        ("input_record_id", "input_records", "id"),
                        ("discovery_id", "source_discoveries", "id"),
                        ("discovery_id", "source_relations", "discovery_id"),
                        ("relation_id", "source_relations", "id"),
                        ("job_id", "download_jobs", "id"),
                    },
                    "download_job_targets": {
                        ("job_id", "download_jobs", "id"),
                        ("fetch_source_item_id", "source_items", "id"),
                    },
                }
                for table, expected in required_foreign_keys.items():
                    actual = {
                        (row["from"], row["table"], row["to"])
                        for row in connection.execute(
                            f"PRAGMA foreign_key_list({table})"
                        ).fetchall()
                    }
                    if missing_foreign_keys := expected - actual:
                        return (
                            False,
                            f"missing_foreign_keys:{table}:"
                            + ",".join(
                                sorted(
                                    f"{source}->{target}.{column}"
                                    for source, target, column in missing_foreign_keys
                                )
                            ),
                        )
                violation = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchone()
            if queue_control is None:
                return False, "missing_queue_control_singleton"
            if violation is not None:
                return False, f"foreign_key_violation:{violation['table']}"
            return True, "ok"
        except sqlite3.Error as exc:
            return False, f"database_error:{type(exc).__name__}"

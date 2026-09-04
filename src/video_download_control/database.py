from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Callable, Iterator


SCHEMA_VERSION = 11
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
    "capability_legacy_schema9",
    "capability_evidence",
    "capability_decisions",
    "asset_commit_intents",
    "queue_control",
    "platform_circuits",
    "worker_claim_gate",
}

REQUIRED_VIEWS = {"platform_capabilities"}

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


MIGRATION_9_SQL = """
BEGIN IMMEDIATE;

DROP INDEX idx_capability_evidence_identity;
ALTER TABLE platform_capabilities RENAME TO platform_capabilities_v8;

CREATE TABLE platform_capabilities (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_type TEXT NOT NULL,
    job_kind TEXT NOT NULL CHECK (job_kind IN ('discover', 'download')),
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
    id, platform, source_type, job_kind, adapter, adapter_version,
    environment, status, last_verified_at, tested_version,
    sample_set_version, notes
)
SELECT
    id, platform, source_type, 'download', adapter, adapter_version,
    environment, status, last_verified_at, tested_version,
    sample_set_version, notes
FROM platform_capabilities_v8;

DROP TABLE platform_capabilities_v8;

CREATE UNIQUE INDEX idx_capability_evidence_identity
ON platform_capabilities(
    platform, source_type, job_kind, adapter, adapter_version, environment
);

INSERT INTO schema_migrations(version, applied_at)
VALUES (9, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""


CAPABILITY_DECISIONS_VERIFIED_TRIGGER_SQL = """
CREATE TRIGGER trg_capability_decisions_verified_evidence
BEFORE INSERT ON capability_decisions
WHEN NEW.action = 'approve'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM capability_evidence AS evidence
        WHERE evidence.evidence_id = NEW.evidence_id
          AND evidence.identity_key = NEW.identity_key
          AND evidence.platform = NEW.platform
          AND evidence.source_type = NEW.source_type
          AND evidence.job_kind = NEW.job_kind
          AND evidence.adapter = NEW.adapter
          AND evidence.downloader_version = NEW.downloader_version
          AND evidence.environment = NEW.environment
          AND evidence.product_version = NEW.product_version
          AND evidence.evidence_kind = 'stage0_csv_v3'
          AND evidence.policy_version = 'stage0-v3'
          AND evidence.verdict = 'verified'
    ) THEN RAISE(ABORT,
        'approved capability requires matching verified Stage 0 v3 evidence'
    ) END;
END;
"""


MIGRATION_10_SQL = (
    """
BEGIN IMMEDIATE;

DROP INDEX idx_capability_evidence_identity;
ALTER TABLE platform_capabilities RENAME TO capability_legacy_schema9;

CREATE INDEX idx_capability_legacy_schema9_identity
ON capability_legacy_schema9(
    platform, source_type, job_kind, adapter, adapter_version, environment
);

CREATE TRIGGER trg_capability_legacy_schema9_no_insert
BEFORE INSERT ON capability_legacy_schema9
BEGIN
    SELECT RAISE(ABORT, 'schema9 capability archive is immutable');
END;

CREATE TRIGGER trg_capability_legacy_schema9_no_update
BEFORE UPDATE ON capability_legacy_schema9
BEGIN
    SELECT RAISE(ABORT, 'schema9 capability archive is immutable');
END;

CREATE TRIGGER trg_capability_legacy_schema9_no_delete
BEFORE DELETE ON capability_legacy_schema9
BEGIN
    SELECT RAISE(ABORT, 'schema9 capability archive is immutable');
END;

CREATE TABLE capability_evidence (
    evidence_id TEXT NOT NULL PRIMARY KEY
        CHECK (
            evidence_id = trim(evidence_id)
            AND length(evidence_id) BETWEEN 1 AND 128
        ),
    identity_key TEXT NOT NULL
        CHECK (
            length(identity_key) = 64
            AND identity_key NOT GLOB '*[^0-9a-f]*'
        ),
    platform TEXT NOT NULL
        CHECK (platform = trim(platform) AND length(platform) BETWEEN 1 AND 32),
    source_type TEXT NOT NULL
        CHECK (
            source_type = trim(source_type)
            AND length(source_type) BETWEEN 1 AND 64
        ),
    job_kind TEXT NOT NULL CHECK (job_kind IN ('discover', 'download')),
    adapter TEXT NOT NULL
        CHECK (adapter = trim(adapter) AND length(adapter) BETWEEN 1 AND 128),
    downloader_version TEXT NOT NULL
        CHECK (
            downloader_version = trim(downloader_version)
            AND length(downloader_version) BETWEEN 1 AND 128
            AND lower(downloader_version) NOT IN ('unknown', 'unexecuted', 'n/a')
        ),
    environment TEXT NOT NULL
        CHECK (
            environment = trim(environment)
            AND length(environment) BETWEEN 1 AND 128
            AND lower(environment) NOT IN ('unknown', 'unexecuted', 'n/a')
        ),
    product_version TEXT NOT NULL
        CHECK (
            product_version = trim(product_version)
            AND length(product_version) BETWEEN 1 AND 128
            AND lower(product_version)
                NOT IN ('unknown', 'unexecuted', 'n/a', 'legacy')
        ),
    evidence_kind TEXT NOT NULL CHECK (evidence_kind = 'stage0_csv_v3'),
    policy_version TEXT NOT NULL CHECK (policy_version = 'stage0-v3'),
    manifest_sha256 TEXT NOT NULL
        CHECK (
            length(manifest_sha256) = 64
            AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    results_sha256 TEXT NOT NULL
        CHECK (
            length(results_sha256) = 64
            AND results_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    bundle_sha256 TEXT NOT NULL
        CHECK (
            length(bundle_sha256) = 64
            AND bundle_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    evidence_sha256 TEXT NOT NULL
        CHECK (
            length(evidence_sha256) = 64
            AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    positive_sample_count INTEGER NOT NULL CHECK (positive_sample_count >= 0),
    negative_sample_count INTEGER NOT NULL CHECK (negative_sample_count >= 0),
    complete_run_count INTEGER NOT NULL CHECK (complete_run_count >= 0),
    positive_rates_json TEXT NOT NULL
        CHECK (
            length(positive_rates_json) <= 4096
            AND json_valid(positive_rates_json)
            AND json_type(positive_rates_json) = 'array'
            AND json_array_length(positive_rates_json) BETWEEN 0 AND 3
        ),
    negative_rates_json TEXT NOT NULL
        CHECK (
            length(negative_rates_json) <= 4096
            AND json_valid(negative_rates_json)
            AND json_type(negative_rates_json) = 'array'
            AND json_array_length(negative_rates_json) BETWEEN 0 AND 3
        ),
    verdict TEXT NOT NULL CHECK (verdict IN ('candidate', 'verified')),
    evaluated_at TEXT NOT NULL
        CHECK (
            evaluated_at = trim(evaluated_at)
            AND length(evaluated_at) BETWEEN 20 AND 64
        ),
    imported_at TEXT NOT NULL
        CHECK (
            imported_at = trim(imported_at)
            AND length(imported_at) BETWEEN 20 AND 64
        ),
    CHECK (
        verdict <> 'verified'
        OR (
            positive_sample_count >= 10
            AND negative_sample_count >= 1
            AND complete_run_count >= 3
            AND json_array_length(positive_rates_json) = 3
            AND json_array_length(negative_rates_json) = 3
        )
    )
);

CREATE UNIQUE INDEX idx_capability_evidence_digest
ON capability_evidence(evidence_sha256);

CREATE UNIQUE INDEX idx_capability_evidence_id_identity
ON capability_evidence(
    evidence_id, identity_key, platform, source_type, job_kind, adapter,
    downloader_version, environment, product_version
);

CREATE INDEX idx_capability_evidence_identity_imported
ON capability_evidence(
    platform, source_type, job_kind, adapter, downloader_version, environment,
    product_version, imported_at, evidence_id
);

CREATE TRIGGER trg_capability_evidence_no_replace
BEFORE INSERT ON capability_evidence
WHEN EXISTS (
    SELECT 1
    FROM capability_evidence AS existing
    WHERE existing.rowid = NEW.rowid
       OR existing.evidence_id = NEW.evidence_id
       OR existing.evidence_sha256 = NEW.evidence_sha256
)


BEGIN
    SELECT RAISE(ABORT, 'capability_evidence is append-only');
END;

CREATE TRIGGER trg_capability_evidence_validate_rates
BEFORE INSERT ON capability_evidence
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.positive_rates_json)
        WHERE type NOT IN ('integer', 'real') OR value < 0 OR value > 1
    ) OR EXISTS (
        SELECT 1 FROM json_each(NEW.negative_rates_json)
        WHERE type NOT IN ('integer', 'real') OR value < 0 OR value > 1
    ) THEN RAISE(ABORT, 'capability evidence rates are invalid') END;
    SELECT CASE WHEN NEW.verdict = 'verified' AND (
        EXISTS (
            SELECT 1 FROM json_each(NEW.positive_rates_json)
            WHERE value < 0.9
        )
        OR EXISTS (
            SELECT 1 FROM json_each(NEW.negative_rates_json)
            WHERE value <> 1
        )
    ) THEN RAISE(ABORT, 'verified capability evidence misses policy') END;
END;

CREATE TRIGGER trg_capability_evidence_no_update
BEFORE UPDATE ON capability_evidence
BEGIN
    SELECT RAISE(ABORT, 'capability_evidence is immutable');
END;

CREATE TRIGGER trg_capability_evidence_no_delete
BEFORE DELETE ON capability_evidence
BEGIN
    SELECT RAISE(ABORT, 'capability_evidence is immutable');
END;

CREATE TABLE capability_decisions (
    decision_id TEXT NOT NULL PRIMARY KEY
        CHECK (
            decision_id = trim(decision_id)
            AND length(decision_id) BETWEEN 1 AND 128
        ),
    identity_key TEXT NOT NULL
        CHECK (
            length(identity_key) = 64
            AND identity_key NOT GLOB '*[^0-9a-f]*'
        ),
    platform TEXT NOT NULL
        CHECK (platform = trim(platform) AND length(platform) BETWEEN 1 AND 32),
    source_type TEXT NOT NULL
        CHECK (
            source_type = trim(source_type)
            AND length(source_type) BETWEEN 1 AND 64
        ),
    job_kind TEXT NOT NULL CHECK (job_kind IN ('discover', 'download')),
    adapter TEXT NOT NULL
        CHECK (adapter = trim(adapter) AND length(adapter) BETWEEN 1 AND 128),
    downloader_version TEXT NOT NULL
        CHECK (
            downloader_version = trim(downloader_version)
            AND length(downloader_version) BETWEEN 1 AND 128
            AND lower(downloader_version) NOT IN ('unknown', 'unexecuted', 'n/a')
        ),
    environment TEXT NOT NULL
        CHECK (
            environment = trim(environment)
            AND length(environment) BETWEEN 1 AND 128
            AND lower(environment) NOT IN ('unknown', 'unexecuted', 'n/a')
        ),
    product_version TEXT NOT NULL
        CHECK (
            product_version = trim(product_version)
            AND length(product_version) BETWEEN 1 AND 128
            AND lower(product_version)
                NOT IN ('unknown', 'unexecuted', 'n/a', 'legacy')
        ),
    evidence_id TEXT NOT NULL
        CHECK (
            evidence_id = trim(evidence_id)
            AND length(evidence_id) BETWEEN 1 AND 128
        ),
    action TEXT NOT NULL CHECK (action IN ('approve', 'revoke')),
    status TEXT NOT NULL CHECK (status IN ('verified', 'candidate')),
    supersedes_decision_id TEXT
        CHECK (
            supersedes_decision_id IS NULL
            OR (
                supersedes_decision_id <> decision_id
                AND supersedes_decision_id = trim(supersedes_decision_id)
                AND length(supersedes_decision_id) BETWEEN 1 AND 128
            )
        ),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    reason_code TEXT NOT NULL
        CHECK (
            reason_code = trim(reason_code)
            AND length(reason_code) BETWEEN 1 AND 128
        ),
    decided_at TEXT NOT NULL
        CHECK (
            decided_at = trim(decided_at)
            AND length(decided_at) BETWEEN 20 AND 64
        ),
    CHECK (
        (action = 'approve' AND status = 'verified' AND evidence_id IS NOT NULL)
        OR (action = 'revoke' AND status = 'candidate')
    ),
    CHECK (
        (supersedes_decision_id IS NULL AND revision = 1)
        OR (supersedes_decision_id IS NOT NULL AND revision >= 2)
    ),
    FOREIGN KEY(
        evidence_id, identity_key, platform, source_type, job_kind, adapter,
        downloader_version, environment, product_version
    ) REFERENCES capability_evidence(
        evidence_id, identity_key, platform, source_type, job_kind, adapter,
        downloader_version, environment, product_version
    ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(
        supersedes_decision_id, identity_key, platform, source_type, job_kind,
        adapter, downloader_version, environment, product_version
    ) REFERENCES capability_decisions(
        decision_id, identity_key, platform, source_type, job_kind, adapter,
        downloader_version, environment, product_version
    ) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_capability_decision_id_identity
ON capability_decisions(
    decision_id, identity_key, platform, source_type, job_kind, adapter,
    downloader_version, environment, product_version
);

CREATE UNIQUE INDEX idx_capability_decision_one_root
ON capability_decisions(
    platform, source_type, job_kind, adapter, downloader_version, environment,
    product_version
)


WHERE supersedes_decision_id IS NULL;

CREATE UNIQUE INDEX idx_capability_decision_one_successor
ON capability_decisions(supersedes_decision_id)
WHERE supersedes_decision_id IS NOT NULL;

CREATE UNIQUE INDEX idx_capability_decision_identity_revision
ON capability_decisions(
    platform, source_type, job_kind, adapter, downloader_version, environment,
    product_version, revision
);

CREATE UNIQUE INDEX idx_capability_decision_one_approval_per_evidence
ON capability_decisions(evidence_id)
WHERE action = 'approve';

CREATE TRIGGER trg_capability_decisions_no_replace
BEFORE INSERT ON capability_decisions
WHEN EXISTS (
    SELECT 1
    FROM capability_decisions AS existing
    WHERE existing.rowid = NEW.rowid
       OR existing.decision_id = NEW.decision_id
       OR (
            existing.identity_key = NEW.identity_key
            AND existing.platform = NEW.platform
            AND existing.source_type = NEW.source_type
            AND existing.job_kind = NEW.job_kind
            AND existing.adapter = NEW.adapter
            AND existing.downloader_version = NEW.downloader_version
            AND existing.environment = NEW.environment
            AND existing.product_version = NEW.product_version
            AND existing.revision = NEW.revision
       )
       OR (
            NEW.supersedes_decision_id IS NOT NULL
            AND existing.supersedes_decision_id = NEW.supersedes_decision_id
       )
       OR (
            NEW.supersedes_decision_id IS NULL
            AND existing.supersedes_decision_id IS NULL
            AND existing.identity_key = NEW.identity_key
       )
       OR (
            NEW.action = 'approve'
            AND existing.action = 'approve'
            AND existing.evidence_id = NEW.evidence_id
       )
)
BEGIN
    SELECT RAISE(ABORT, 'capability_decisions is append-only');
END;

CREATE TRIGGER trg_capability_decisions_validate_revision
BEFORE INSERT ON capability_decisions
WHEN NEW.supersedes_decision_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM capability_decisions AS previous
        WHERE previous.decision_id = NEW.supersedes_decision_id
          AND previous.identity_key = NEW.identity_key
          AND previous.platform = NEW.platform
          AND previous.source_type = NEW.source_type
          AND previous.job_kind = NEW.job_kind
          AND previous.adapter = NEW.adapter
          AND previous.downloader_version = NEW.downloader_version
          AND previous.environment = NEW.environment
          AND previous.product_version = NEW.product_version
          AND NEW.revision = previous.revision + 1
    ) THEN RAISE(ABORT, 'capability decision revision conflict') END;
END;

CREATE TRIGGER trg_capability_decisions_validate_transition
BEFORE INSERT ON capability_decisions
WHEN NEW.action = 'revoke'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM capability_decisions AS previous
        WHERE previous.decision_id = NEW.supersedes_decision_id
          AND previous.identity_key = NEW.identity_key
          AND previous.status = 'verified'
          AND previous.evidence_id = NEW.evidence_id
    ) THEN RAISE(ABORT, 'capability decision transition is invalid') END;
END;

"""
    + CAPABILITY_DECISIONS_VERIFIED_TRIGGER_SQL
    + """
CREATE TRIGGER trg_capability_decisions_no_update
BEFORE UPDATE ON capability_decisions
BEGIN
    SELECT RAISE(ABORT, 'capability_decisions is append-only');
END;

CREATE TRIGGER trg_capability_decisions_no_delete
BEFORE DELETE ON capability_decisions
BEGIN
    SELECT RAISE(ABORT, 'capability_decisions is append-only');
END;

CREATE VIEW platform_capabilities AS
SELECT
    decision.decision_id AS current_decision_id,
    decision.identity_key,
    decision.platform,
    decision.source_type,
    decision.job_kind,
    decision.adapter,
    decision.downloader_version,
    decision.environment,
    decision.product_version,
    decision.evidence_id,
    decision.action,
    decision.status,
    decision.supersedes_decision_id,
    decision.revision,
    decision.reason_code,
    decision.decided_at
FROM capability_decisions AS decision
WHERE NOT EXISTS (
    SELECT 1
    FROM capability_decisions AS successor
    WHERE successor.supersedes_decision_id = decision.decision_id
);

INSERT INTO schema_migrations(version, applied_at)
VALUES (10, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
"""
)


WORKER_CLAIM_GATE_TABLE_SQL = """
CREATE TABLE worker_claim_gate (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT,
    worker_id TEXT,
    accepting_claims INTEGER NOT NULL DEFAULT 0
        CHECK (accepting_claims IN (0, 1)),
    activated_at TEXT,
    stop_requested_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (
        (
            run_id IS NULL
            AND worker_id IS NULL
            AND accepting_claims = 0
            AND activated_at IS NULL
            AND stop_requested_at IS NULL
        )
        OR
        (
            run_id IS NOT NULL
            AND run_id = trim(run_id)
            AND length(run_id) = 32
            AND run_id NOT GLOB '*[^0-9a-f]*'
            AND worker_id IS NOT NULL
            AND worker_id = trim(worker_id)
            AND length(worker_id) BETWEEN 1 AND 128
            AND (
                (
                    accepting_claims = 1
                    AND activated_at IS NOT NULL
                    AND stop_requested_at IS NULL
                )
                OR
                (
                    accepting_claims = 0
                    AND (
                        (activated_at IS NULL AND stop_requested_at IS NULL)
                        OR stop_requested_at IS NOT NULL
                    )
                )
            )
        )
    )
)
""".strip()


MIGRATION_11_SQL = f"""
BEGIN IMMEDIATE;

{WORKER_CLAIM_GATE_TABLE_SQL};

INSERT INTO worker_claim_gate(
    id, run_id, worker_id, accepting_claims,
    activated_at, stop_requested_at, updated_at
) VALUES (
    1, NULL, NULL, 0, NULL, NULL,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

INSERT INTO schema_migrations(version, applied_at)
VALUES (11, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
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
    def __init__(
        self,
        path: Path,
        *,
        path_validator: Callable[[Path], None] | None = None,
    ) -> None:
        self.path = path
        self._path_validator = path_validator
        self._readiness_cache_lock = Lock()
        self._readiness_cache: (
            tuple[int, float, tuple[bool, str]] | None
        ) = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite serializes writers, but a version check followed by
        # ``executescript`` is not itself atomic. Two fresh processes can both
        # observe version 0 and then attempt the same ALTER TABLE migration.
        # Keep migrations through Schema 11 under serialized transactions.
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
            self._migrate_to_9()
            self._migrate_to_10()
            self._migrate_to_11()

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
            # Another process may have completed later migrations after
            # this caller observed Schema 7 in ``initialize``.
            if 8 <= current_version <= SCHEMA_VERSION:
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

    def _migrate_to_9(self) -> None:
        """Add the Stage 0 job-kind identity in one forward-only transaction."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            current_version = int(row["version"] or 0)
            if 9 <= current_version <= SCHEMA_VERSION:
                return
            if current_version != 8:
                raise RuntimeError(
                    f"schema 9 migration requires schema 8, found {current_version}"
                )
            self._execute_in_current_transaction(connection, MIGRATION_9_SQL)

    def _migrate_to_10(self) -> None:
        """Replace mutable capability rows with an append-only evidence ledger."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            current_version = int(row["version"] or 0)
            if 10 <= current_version <= SCHEMA_VERSION:
                return
            if current_version != 9:
                raise RuntimeError(
                    f"schema 10 migration requires schema 9, found {current_version}"
                )
            self._execute_in_current_transaction(connection, MIGRATION_10_SQL)

    def _migrate_to_11(self) -> None:
        """Add the run-scoped Worker claim fencing gate."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            current_version = int(row["version"] or 0)
            if current_version == 11:
                return
            if current_version != 10:
                raise RuntimeError(
                    f"schema 11 migration requires schema 10, found {current_version}"
                )
            self._execute_in_current_transaction(connection, MIGRATION_11_SQL)

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
        if self._path_validator is not None:
            self._path_validator(self.path)
        connection = sqlite3.connect(self.path, timeout=5.0)
        if self._path_validator is not None:
            try:
                self._path_validator(self.path)
            except Exception:
                connection.close()
                raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
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

    def _schema_cookie(self) -> int:
        with self.connect() as connection:
            row = connection.execute("PRAGMA schema_version").fetchone()
        if row is None:
            raise sqlite3.DatabaseError("schema cookie is unavailable")
        return int(row[0])

    def cached_readiness(
        self, *, max_age_seconds: float = 5.0
    ) -> tuple[bool, str]:
        """Coalesce expensive probe audits while detecting schema DDL now.

        Queue state is checked separately by the API.  The SQLite schema cookie
        invalidates this cache immediately after table/view/index/trigger DDL;
        otherwise a full integrity and ledger audit runs at least every five
        seconds.  Administrative and release commands continue to call the
        uncached ``readiness`` method.
        """

        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or max_age_seconds <= 0
            or max_age_seconds > 60
        ):
            raise ValueError("readiness cache age is invalid")
        with self._readiness_cache_lock:
            try:
                schema_cookie = self._schema_cookie()
            except (OSError, sqlite3.Error, TypeError, ValueError):
                self._readiness_cache = None
                return self.readiness()
            now = time.monotonic()
            if self._readiness_cache is not None:
                cached_cookie, cached_at, cached_result = self._readiness_cache
                if (
                    cached_cookie == schema_cookie
                    and now - cached_at <= max_age_seconds
                ):
                    return cached_result
            result = self.readiness()
            try:
                final_cookie = self._schema_cookie()
            except (OSError, sqlite3.Error, TypeError, ValueError):
                self._readiness_cache = None
                return False, "schema_version_unavailable"
            if final_cookie == schema_cookie:
                self._readiness_cache = (final_cookie, time.monotonic(), result)
                return result
            self._readiness_cache = None
            return False, "schema_changed_during_readiness"

    def readiness(self) -> tuple[bool, str]:
        """Verify the migration marker and tables needed by the control plane."""
        try:
            with self.connect() as connection:
                quick_check = connection.execute("PRAGMA quick_check").fetchall()
                migration_versions = [
                    int(row["version"])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall()
                ]
                rows = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                views = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'view'"
                ).fetchall()
                indexes = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
                triggers = connection.execute(
                    """
                    SELECT name, tbl_name, sql
                    FROM sqlite_master WHERE type = 'trigger'
                    """
                ).fetchall()
            if len(quick_check) != 1 or quick_check[0][0] != "ok":
                return False, "database_quick_check_failed"
            if migration_versions != list(range(1, SCHEMA_VERSION + 1)):
                return False, "schema_version_mismatch"
            present = {row["name"] for row in rows}
            if missing := REQUIRED_TABLES - present:
                return False, "missing_tables:" + ",".join(sorted(missing))
            present_views = {row["name"] for row in views}
            if missing_views := REQUIRED_VIEWS - present_views:
                return False, "missing_views:" + ",".join(sorted(missing_views))
            if "platform_capabilities" in present:
                return False, "forbidden_tables:platform_capabilities"
            table_sql = {row["name"]: row["sql"] for row in rows}
            normalized_claim_gate_sql = "".join(
                str(table_sql["worker_claim_gate"]).upper().split()
            )
            expected_claim_gate_sql = "".join(
                WORKER_CLAIM_GATE_TABLE_SQL.upper().split()
            )
            if normalized_claim_gate_sql != expected_claim_gate_sql:
                return False, "malformed_table:worker_claim_gate"
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
                "capability_legacy_schema9": {
                    "id",
                    "platform",
                    "source_type",
                    "job_kind",
                    "adapter",
                    "adapter_version",
                    "environment",
                    "status",
                    "last_verified_at",
                    "tested_version",
                    "sample_set_version",
                    "notes",
                },
                "capability_evidence": {
                    "evidence_id",
                    "identity_key",
                    "platform",
                    "source_type",
                    "job_kind",
                    "adapter",
                    "downloader_version",
                    "environment",
                    "product_version",
                    "evidence_kind",
                    "policy_version",
                    "manifest_sha256",
                    "results_sha256",
                    "bundle_sha256",
                    "evidence_sha256",
                    "positive_sample_count",
                    "negative_sample_count",
                    "complete_run_count",
                    "positive_rates_json",
                    "negative_rates_json",
                    "verdict",
                    "evaluated_at",
                    "imported_at",
                },
                "capability_decisions": {
                    "decision_id",
                    "identity_key",
                    "platform",
                    "source_type",
                    "job_kind",
                    "adapter",
                    "downloader_version",
                    "environment",
                    "product_version",
                    "evidence_id",
                    "action",
                    "status",
                    "supersedes_decision_id",
                    "revision",
                    "reason_code",
                    "decided_at",
                },
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
                "worker_claim_gate": {
                    "id",
                    "run_id",
                    "worker_id",
                    "accepting_claims",
                    "activated_at",
                    "stop_requested_at",
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
                "idx_capability_legacy_schema9_identity",
                "idx_capability_evidence_digest",
                "idx_capability_evidence_id_identity",
                "idx_capability_evidence_identity_imported",
                "idx_capability_decision_id_identity",
                "idx_capability_decision_one_root",
                "idx_capability_decision_one_successor",
                "idx_capability_decision_identity_revision",
                "idx_capability_decision_one_approval_per_evidence",
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
                "trg_capability_legacy_schema9_no_insert",
                "trg_capability_legacy_schema9_no_update",
                "trg_capability_legacy_schema9_no_delete",
                "trg_capability_evidence_no_replace",
                "trg_capability_evidence_validate_rates",
                "trg_capability_evidence_no_update",
                "trg_capability_evidence_no_delete",
                "trg_capability_decisions_no_replace",
                "trg_capability_decisions_validate_revision",
                "trg_capability_decisions_validate_transition",
                "trg_capability_decisions_verified_evidence",
                "trg_capability_decisions_no_update",
                "trg_capability_decisions_no_delete",
            }
            present_triggers = {row["name"] for row in triggers}
            if missing_triggers := required_triggers - present_triggers:
                return False, "missing_triggers:" + ",".join(
                    sorted(missing_triggers)
                )
            if any(
                str(row["tbl_name"]).lower() == "worker_claim_gate"
                for row in triggers
            ):
                return False, "forbidden_triggers:worker_claim_gate"
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
                "trg_capability_legacy_schema9_no_insert": (
                    "BEFORE INSERT ON CAPABILITY_LEGACY_SCHEMA9"
                ),
                "trg_capability_legacy_schema9_no_update": (
                    "BEFORE UPDATE ON CAPABILITY_LEGACY_SCHEMA9"
                ),
                "trg_capability_legacy_schema9_no_delete": (
                    "BEFORE DELETE ON CAPABILITY_LEGACY_SCHEMA9"
                ),
                "trg_capability_evidence_no_replace": (
                    "BEFORE INSERT ON CAPABILITY_EVIDENCE"
                ),
                "trg_capability_evidence_validate_rates": (
                    "BEFORE INSERT ON CAPABILITY_EVIDENCE"
                ),
                "trg_capability_evidence_no_update": (
                    "BEFORE UPDATE ON CAPABILITY_EVIDENCE"
                ),
                "trg_capability_evidence_no_delete": (
                    "BEFORE DELETE ON CAPABILITY_EVIDENCE"
                ),
                "trg_capability_decisions_no_replace": (
                    "BEFORE INSERT ON CAPABILITY_DECISIONS"
                ),
                "trg_capability_decisions_validate_revision": (
                    "BEFORE INSERT ON CAPABILITY_DECISIONS"
                ),
                "trg_capability_decisions_validate_transition": (
                    "BEFORE INSERT ON CAPABILITY_DECISIONS"
                ),
                "trg_capability_decisions_verified_evidence": (
                    "BEFORE INSERT ON CAPABILITY_DECISIONS"
                ),
                "trg_capability_decisions_no_update": (
                    "BEFORE UPDATE ON CAPABILITY_DECISIONS"
                ),
                "trg_capability_decisions_no_delete": (
                    "BEFORE DELETE ON CAPABILITY_DECISIONS"
                ),
            }.items():
                normalized = " ".join(str(trigger_sql[name]).upper().split())
                if event not in normalized or "RAISE(ABORT" not in normalized:
                    return False, f"malformed_trigger:{name}"
            evidence_replace_guard_sql = "".join(
                str(trigger_sql["trg_capability_evidence_no_replace"])
                .upper()
                .split()
            )
            if any(
                token not in evidence_replace_guard_sql
                for token in (
                    "EXISTING.ROWID=NEW.ROWID",
                    "EXISTING.EVIDENCE_ID=NEW.EVIDENCE_ID",
                    "EXISTING.EVIDENCE_SHA256=NEW.EVIDENCE_SHA256",
                )
            ):
                return (
                    False,
                    "malformed_trigger:trg_capability_evidence_no_replace",
                )
            decision_replace_guard_sql = "".join(
                str(trigger_sql["trg_capability_decisions_no_replace"])
                .upper()
                .split()
            )
            if any(
                token not in decision_replace_guard_sql
                for token in (
                    "EXISTING.ROWID=NEW.ROWID",
                    "EXISTING.DECISION_ID=NEW.DECISION_ID",
                    "EXISTING.IDENTITY_KEY=NEW.IDENTITY_KEY",
                    "EXISTING.REVISION=NEW.REVISION",
                    "EXISTING.SUPERSEDES_DECISION_ID=NEW.SUPERSEDES_DECISION_ID",
                    "EXISTING.EVIDENCE_ID=NEW.EVIDENCE_ID",
                )
            ):
                return (
                    False,
                    "malformed_trigger:trg_capability_decisions_no_replace",
                )
            verified_guard_sql = "".join(
                str(
                    trigger_sql["trg_capability_decisions_verified_evidence"]
                )
                .upper()
                .split()
            )
            if any(
                token not in verified_guard_sql
                for token in (
                    "NEW.ACTION='APPROVE'",
                    "EVIDENCE.EVIDENCE_KIND='STAGE0_CSV_V3'",
                    "EVIDENCE.POLICY_VERSION='STAGE0-V3'",
                    "EVIDENCE.VERDICT='VERIFIED'",
                    "EVIDENCE.IDENTITY_KEY=NEW.IDENTITY_KEY",
                    "EVIDENCE.PRODUCT_VERSION=NEW.PRODUCT_VERSION",
                )
            ):
                return (
                    False,
                    "malformed_trigger:trg_capability_decisions_verified_evidence",
                )
            revision_guard_sql = "".join(
                str(trigger_sql["trg_capability_decisions_validate_revision"])
                .upper()
                .split()
            )
            if any(
                token not in revision_guard_sql
                for token in (
                    "NEW.SUPERSEDES_DECISION_IDISNOTNULL",
                    "NEW.REVISION=PREVIOUS.REVISION+1",
                    "PREVIOUS.PRODUCT_VERSION=NEW.PRODUCT_VERSION",
                )
            ):
                return (
                    False,
                    "malformed_trigger:trg_capability_decisions_validate_revision",
                )
            transition_guard_sql = "".join(
                str(trigger_sql["trg_capability_decisions_validate_transition"])
                .upper()
                .split()
            )
            if any(
                token not in transition_guard_sql
                for token in (
                    "NEW.ACTION='REVOKE'",
                    "PREVIOUS.STATUS='VERIFIED'",
                    "PREVIOUS.EVIDENCE_ID=NEW.EVIDENCE_ID",
                )
            ):
                return (
                    False,
                    "malformed_trigger:trg_capability_decisions_validate_transition",
                )
            with self.connect() as connection:
                queue_control = connection.execute(
                    "SELECT id FROM queue_control WHERE id = 1"
                ).fetchone()
                worker_claim_gate_rows = connection.execute(
                    """
                    SELECT id, run_id, worker_id, accepting_claims,
                           activated_at, stop_requested_at, updated_at
                    FROM worker_claim_gate ORDER BY id
                    """
                ).fetchall()
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
                capability_index_specs = {
                    "idx_capability_legacy_schema9_identity": (
                        "capability_legacy_schema9",
                        False,
                        (
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "adapter_version",
                            "environment",
                        ),
                        None,
                    ),
                    "idx_capability_evidence_digest": (
                        "capability_evidence",
                        True,
                        ("evidence_sha256",),
                        None,
                    ),
                    "idx_capability_evidence_id_identity": (
                        "capability_evidence",
                        True,
                        (
                            "evidence_id",
                            "identity_key",
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "downloader_version",
                            "environment",
                            "product_version",
                        ),
                        None,
                    ),
                    "idx_capability_evidence_identity_imported": (
                        "capability_evidence",
                        False,
                        (
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "downloader_version",
                            "environment",
                            "product_version",
                            "imported_at",
                            "evidence_id",
                        ),
                        None,
                    ),
                    "idx_capability_decision_id_identity": (
                        "capability_decisions",
                        True,
                        (
                            "decision_id",
                            "identity_key",
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "downloader_version",
                            "environment",
                            "product_version",
                        ),
                        None,
                    ),
                    "idx_capability_decision_one_root": (
                        "capability_decisions",
                        True,
                        (
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "downloader_version",
                            "environment",
                            "product_version",
                        ),
                        "WHERESUPERSEDES_DECISION_IDISNULL",
                    ),
                    "idx_capability_decision_one_successor": (
                        "capability_decisions",
                        True,
                        ("supersedes_decision_id",),
                        "WHERESUPERSEDES_DECISION_IDISNOTNULL",
                    ),
                    "idx_capability_decision_identity_revision": (
                        "capability_decisions",
                        True,
                        (
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "downloader_version",
                            "environment",
                            "product_version",
                            "revision",
                        ),
                        None,
                    ),
                    "idx_capability_decision_one_approval_per_evidence": (
                        "capability_decisions",
                        True,
                        ("evidence_id",),
                        "WHEREACTION='APPROVE'",
                    ),
                }
                for index_name, (
                    table,
                    unique,
                    expected_columns,
                    predicate,
                ) in capability_index_specs.items():
                    index = next(
                        (
                            row
                            for row in connection.execute(
                                f"PRAGMA index_list({table})"
                            ).fetchall()
                            if row["name"] == index_name
                        ),
                        None,
                    )
                    columns = (
                        tuple(
                            row["name"]
                            for row in connection.execute(
                                f"PRAGMA index_info({index_name})"
                            ).fetchall()
                        )
                        if index is not None
                        else ()
                    )
                    index_row = connection.execute(
                        """
                        SELECT sql FROM sqlite_master
                        WHERE type = 'index' AND name = ?
                        """,
                        (index_name,),
                    ).fetchone()
                    normalized_index_sql = "".join(
                        str(index_row["sql"] if index_row else "").upper().split()
                    )
                    if (
                        index is None
                        or bool(index["unique"]) is not unique
                        or columns != expected_columns
                        or (
                            predicate is not None
                            and predicate not in normalized_index_sql
                        )
                    ):
                        return False, f"malformed_index:{index_name}"

                exact_capability_columns = {
                    "capability_evidence": {
                        "evidence_id",
                        "identity_key",
                        "platform",
                        "source_type",
                        "job_kind",
                        "adapter",
                        "downloader_version",
                        "environment",
                        "product_version",
                        "evidence_kind",
                        "policy_version",
                        "manifest_sha256",
                        "results_sha256",
                        "bundle_sha256",
                        "evidence_sha256",
                        "positive_sample_count",
                        "negative_sample_count",
                        "complete_run_count",
                        "positive_rates_json",
                        "negative_rates_json",
                        "verdict",
                        "evaluated_at",
                        "imported_at",
                    },
                    "capability_decisions": {
                        "decision_id",
                        "identity_key",
                        "platform",
                        "source_type",
                        "job_kind",
                        "adapter",
                        "downloader_version",
                        "environment",
                        "product_version",
                        "evidence_id",
                        "action",
                        "status",
                        "supersedes_decision_id",
                        "revision",
                        "reason_code",
                        "decided_at",
                    },
                }
                for table, expected_columns in exact_capability_columns.items():
                    column_rows = connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                    actual_columns = {row["name"] for row in column_rows}
                    if actual_columns != expected_columns:
                        return False, f"malformed_columns:{table}"
                    nullable = {
                        row["name"]
                        for row in column_rows
                        if not bool(row["notnull"])
                    }
                    allowed_nullable = (
                        {"supersedes_decision_id"}
                        if table == "capability_decisions"
                        else set()
                    )
                    if nullable != allowed_nullable:
                        return False, f"malformed_nullability:{table}"

                expected_view_columns = (
                    "current_decision_id",
                    "identity_key",
                    "platform",
                    "source_type",
                    "job_kind",
                    "adapter",
                    "downloader_version",
                    "environment",
                    "product_version",
                    "evidence_id",
                    "action",
                    "status",
                    "supersedes_decision_id",
                    "revision",
                    "reason_code",
                    "decided_at",
                )
                actual_view_columns = tuple(
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(platform_capabilities)"
                    ).fetchall()
                )
                if actual_view_columns != expected_view_columns:
                    return False, "malformed_view:platform_capabilities"

                table_sql = {
                    row["name"]: "".join(str(row["sql"]).upper().split())
                    for row in connection.execute(
                        """
                        SELECT name, sql FROM sqlite_master
                        WHERE type = 'table'
                          AND name IN ('capability_evidence', 'capability_decisions')
                        """
                    ).fetchall()
                }
                evidence_sql = table_sql.get("capability_evidence", "")
                decision_sql = table_sql.get("capability_decisions", "")
                if (
                    "CHECK(JOB_KINDIN('DISCOVER','DOWNLOAD'))" not in evidence_sql
                    or "CHECK(VERDICTIN('CANDIDATE','VERIFIED'))"
                    not in evidence_sql
                    or "CHECK(POLICY_VERSION='STAGE0-V3')" not in evidence_sql
                    or "PRODUCT_VERSION" not in evidence_sql
                ):
                    return False, "malformed_checks:capability_evidence"
                if (
                    "CHECK(JOB_KINDIN('DISCOVER','DOWNLOAD'))" not in decision_sql
                    or "CHECK(ACTIONIN('APPROVE','REVOKE'))" not in decision_sql
                    or "CHECK(STATUSIN('VERIFIED','CANDIDATE'))" not in decision_sql
                    or "REVISION=1" not in decision_sql
                    or "PRODUCT_VERSION" not in decision_sql
                ):
                    return False, "malformed_checks:capability_decisions"

                capability_view = next(
                    (
                        row for row in views if row["name"] == "platform_capabilities"
                    ),
                    None,
                )
                normalized_view_sql = "".join(
                    str(capability_view["sql"] if capability_view else "")
                    .upper()
                    .split()
                )
                if (
                    "FROMCAPABILITY_DECISIONSASDECISION" not in normalized_view_sql
                    or "SUCCESSOR.SUPERSEDES_DECISION_ID=DECISION.DECISION_ID"
                    not in normalized_view_sql
                    or "WHERENOTEXISTS" not in normalized_view_sql
                ):
                    return False, "malformed_view:platform_capabilities"

                forbidden_view_trigger = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'trigger' AND tbl_name = 'platform_capabilities'
                    LIMIT 1
                    """
                ).fetchone()
                if forbidden_view_trigger is not None:
                    return False, "writable_view:platform_capabilities"

                # Import only after ``database`` has finished loading: the
                # governance module itself depends on ``Database``.  These
                # helpers are pure and let readiness verify persisted content,
                # rather than trusting caller-supplied digest columns.
                from .capabilities import (  # noqa: PLC0415
                    DEFAULT_DOWNLOAD_CAPABILITIES,
                )
                from .capability_evidence import (  # noqa: PLC0415
                    capability_identity_key,
                    evidence_sha256_from_payload,
                )
                from .validation import STAGE0_POLICY_VERSION  # noqa: PLC0415

                # Historical evidence remains readable after a route is
                # disabled.  A disabled registry entry is the tombstone that
                # preserves its identity; only import/approve require an
                # active implementation.
                known_routes = {
                    (
                        item.route.platform.value,
                        item.route.source_type.value,
                        item.route.job_kind.value,
                        item.adapter,
                    )
                    for item in DEFAULT_DOWNLOAD_CAPABILITIES.list()
                }
                evidence_rows = connection.execute(
                    "SELECT * FROM capability_evidence ORDER BY evidence_id"
                ).fetchall()
                for evidence in evidence_rows:
                    identity = {
                        name: evidence[name]
                        for name in (
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "downloader_version",
                            "environment",
                            "product_version",
                        )
                    }
                    if evidence["policy_version"] != STAGE0_POLICY_VERSION:
                        return False, "invalid_capability_state:evidence_policy"
                    route = (
                        identity["platform"],
                        identity["source_type"],
                        identity["job_kind"],
                        identity["adapter"],
                    )
                    if route not in known_routes:
                        return False, "invalid_capability_state:unknown_evidence_route"
                    if (
                        not all(isinstance(value, str) for value in identity.values())
                        or capability_identity_key(**identity)
                        != evidence["identity_key"]
                    ):
                        return False, "invalid_capability_state:evidence_identity_key"
                    try:
                        positive_rates = json.loads(evidence["positive_rates_json"])
                        negative_rates = json.loads(evidence["negative_rates_json"])
                    except (TypeError, ValueError):
                        return False, "invalid_capability_state:evidence_payload"
                    if not isinstance(positive_rates, list) or not isinstance(
                        negative_rates, list
                    ):
                        return False, "invalid_capability_state:evidence_payload"
                    public_payload = {
                        **identity,
                        "identity_key": evidence["identity_key"],
                        "evidence_kind": evidence["evidence_kind"],
                        "policy_version": evidence["policy_version"],
                        "bundle_sha256": evidence["bundle_sha256"],
                        "positive_sample_count": evidence["positive_sample_count"],
                        "negative_sample_count": evidence["negative_sample_count"],
                        "complete_run_count": evidence["complete_run_count"],
                        "positive_rates": positive_rates,
                        "negative_rates": negative_rates,
                        "verdict": evidence["verdict"],
                        "evaluated_at": evidence["evaluated_at"],
                    }
                    if (
                        evidence_sha256_from_payload(public_payload)
                        != evidence["evidence_sha256"]
                    ):
                        return False, "invalid_capability_state:evidence_digest"

                decision_rows = connection.execute(
                    "SELECT * FROM capability_decisions ORDER BY decision_id"
                ).fetchall()
                for decision in decision_rows:
                    identity = {
                        name: decision[name]
                        for name in (
                            "platform",
                            "source_type",
                            "job_kind",
                            "adapter",
                            "downloader_version",
                            "environment",
                            "product_version",
                        )
                    }
                    route = (
                        identity["platform"],
                        identity["source_type"],
                        identity["job_kind"],
                        identity["adapter"],
                    )
                    if route not in known_routes:
                        return False, "invalid_capability_state:unknown_decision_route"
                    if (
                        not all(isinstance(value, str) for value in identity.values())
                        or capability_identity_key(**identity)
                        != decision["identity_key"]
                    ):
                        return False, "invalid_capability_state:decision_identity_key"

                verified_without_evidence = connection.execute(
                    """
                    SELECT 1
                    FROM capability_decisions AS decision
                    WHERE decision.status = 'verified'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM capability_evidence AS evidence
                          WHERE evidence.evidence_id = decision.evidence_id
                            AND evidence.identity_key = decision.identity_key
                            AND evidence.platform = decision.platform
                            AND evidence.source_type = decision.source_type
                            AND evidence.job_kind = decision.job_kind
                            AND evidence.adapter = decision.adapter
                            AND evidence.downloader_version =
                                decision.downloader_version
                            AND evidence.environment = decision.environment
                            AND evidence.product_version =
                                decision.product_version
                            AND evidence.evidence_kind = 'stage0_csv_v3'
                            AND evidence.policy_version = 'stage0-v3'
                            AND evidence.verdict = 'verified'
                      )
                    LIMIT 1
                    """
                ).fetchone()
                if verified_without_evidence is not None:
                    return (
                        False,
                        "invalid_capability_state:verified_without_evidence",
                    )

                invalid_revision = connection.execute(
                    """
                    SELECT 1
                    FROM capability_decisions AS decision
                    LEFT JOIN capability_decisions AS previous
                      ON previous.decision_id = decision.supersedes_decision_id
                    WHERE (
                        decision.supersedes_decision_id IS NULL
                        AND decision.revision <> 1
                    ) OR (
                        decision.supersedes_decision_id IS NOT NULL
                        AND (
                            previous.decision_id IS NULL
                            OR decision.revision <> previous.revision + 1
                            OR previous.identity_key <> decision.identity_key
                            OR previous.platform <> decision.platform
                            OR previous.source_type <> decision.source_type
                            OR previous.job_kind <> decision.job_kind
                            OR previous.adapter <> decision.adapter
                            OR previous.downloader_version <>
                                decision.downloader_version
                            OR previous.environment <> decision.environment
                            OR previous.product_version <>
                                decision.product_version
                        )
                    )
                    LIMIT 1
                    """
                ).fetchone()
                if invalid_revision is not None:
                    return False, "invalid_capability_state:revision_chain"
                invalid_transition = connection.execute(
                    """
                    SELECT 1
                    FROM capability_decisions AS decision
                    LEFT JOIN capability_decisions AS previous
                      ON previous.decision_id = decision.supersedes_decision_id
                    WHERE decision.action = 'revoke'
                      AND (
                          previous.decision_id IS NULL
                          OR previous.status <> 'verified'
                          OR previous.evidence_id <> decision.evidence_id
                          OR previous.identity_key <> decision.identity_key
                      )
                    LIMIT 1
                    """
                ).fetchone()
                if invalid_transition is not None:
                    return False, "invalid_capability_state:invalid_transition"
                projection_mismatch = connection.execute(
                    """
                    WITH expected AS (
                        SELECT
                            decision.decision_id AS current_decision_id,
                            decision.identity_key,
                            decision.platform,
                            decision.source_type,
                            decision.job_kind,
                            decision.adapter,
                            decision.downloader_version,
                            decision.environment,
                            decision.product_version,
                            decision.evidence_id,
                            decision.action,
                            decision.status,
                            decision.supersedes_decision_id,
                            decision.revision,
                            decision.reason_code,
                            decision.decided_at
                        FROM capability_decisions AS decision
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM capability_decisions AS successor
                            WHERE successor.supersedes_decision_id =
                                decision.decision_id
                        )
                    ), extra AS (
                        SELECT * FROM platform_capabilities
                        EXCEPT
                        SELECT * FROM expected
                    ), missing AS (
                        SELECT * FROM expected
                        EXCEPT
                        SELECT * FROM platform_capabilities
                    )
                    SELECT 1 FROM extra
                    UNION ALL
                    SELECT 1 FROM missing
                    LIMIT 1
                    """
                ).fetchone()
                if projection_mismatch is not None:
                    return False, "invalid_capability_state:current_projection"
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
                    "capability_decisions": {
                        ("evidence_id", "capability_evidence", "evidence_id"),
                        ("identity_key", "capability_evidence", "identity_key"),
                        ("platform", "capability_evidence", "platform"),
                        ("source_type", "capability_evidence", "source_type"),
                        ("job_kind", "capability_evidence", "job_kind"),
                        ("adapter", "capability_evidence", "adapter"),
                        (
                            "downloader_version",
                            "capability_evidence",
                            "downloader_version",
                        ),
                        ("environment", "capability_evidence", "environment"),
                        (
                            "product_version",
                            "capability_evidence",
                            "product_version",
                        ),
                        (
                            "supersedes_decision_id",
                            "capability_decisions",
                            "decision_id",
                        ),
                        ("identity_key", "capability_decisions", "identity_key"),
                        ("platform", "capability_decisions", "platform"),
                        ("source_type", "capability_decisions", "source_type"),
                        ("job_kind", "capability_decisions", "job_kind"),
                        ("adapter", "capability_decisions", "adapter"),
                        (
                            "downloader_version",
                            "capability_decisions",
                            "downloader_version",
                        ),
                        ("environment", "capability_decisions", "environment"),
                        (
                            "product_version",
                            "capability_decisions",
                            "product_version",
                        ),
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
                capability_foreign_keys = connection.execute(
                    "PRAGMA foreign_key_list(capability_decisions)"
                ).fetchall()
                capability_foreign_key_groups = {
                    row["id"] for row in capability_foreign_keys
                }
                if (
                    len(capability_foreign_keys) != 18
                    or len(capability_foreign_key_groups) != 2
                    or any(
                        row["on_update"] != "RESTRICT"
                        or row["on_delete"] != "RESTRICT"
                        for row in capability_foreign_keys
                    )
                ):
                    return False, "malformed_foreign_keys:capability_decisions"
                violation = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchone()
            if queue_control is None:
                return False, "missing_queue_control_singleton"
            if not worker_claim_gate_rows:
                return False, "missing_worker_claim_gate_singleton"
            if (
                len(worker_claim_gate_rows) != 1
                or worker_claim_gate_rows[0]["id"] != 1
            ):
                return False, "malformed_worker_claim_gate_singleton"
            worker_claim_gate = worker_claim_gate_rows[0]
            run_id = worker_claim_gate["run_id"]
            worker_id = worker_claim_gate["worker_id"]
            accepting_claims = worker_claim_gate["accepting_claims"]
            activated_at = worker_claim_gate["activated_at"]
            stop_requested_at = worker_claim_gate["stop_requested_at"]
            updated_at = worker_claim_gate["updated_at"]
            def valid_timestamp(value: object) -> bool:
                return (
                    isinstance(value, str)
                    and value == value.strip()
                    and bool(value)
                )

            def valid_optional_timestamp(value: object) -> bool:
                return value is None or valid_timestamp(value)
            pristine_gate = (
                run_id is None
                and worker_id is None
                and accepting_claims == 0
                and activated_at is None
                and stop_requested_at is None
            )
            valid_run_id = (
                isinstance(run_id, str)
                and len(run_id) == 32
                and all(character in "0123456789abcdef" for character in run_id)
            )
            valid_worker_id = (
                isinstance(worker_id, str)
                and worker_id == worker_id.strip()
                and 1 <= len(worker_id) <= 128
            )
            prepared_gate = (
                accepting_claims == 0
                and activated_at is None
                and stop_requested_at is None
            )
            active_gate = (
                accepting_claims == 1
                and valid_timestamp(activated_at)
                and stop_requested_at is None
            )
            stopped_gate = (
                accepting_claims == 0
                and valid_optional_timestamp(activated_at)
                and valid_timestamp(stop_requested_at)
            )
            identified_gate = (
                valid_run_id
                and valid_worker_id
                and (prepared_gate or active_gate or stopped_gate)
            )
            if (
                accepting_claims not in (0, 1)
                or not valid_timestamp(updated_at)
                or not (pristine_gate or identified_gate)
            ):
                return False, "malformed_worker_claim_gate_state"
            if violation is not None:
                return False, f"foreign_key_violation:{violation['table']}"
            return True, "ok"
        except sqlite3.Error as exc:
            return False, f"database_error:{type(exc).__name__}"

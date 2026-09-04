from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import video_download_control.database as database_module
from video_download_control.database import (
    MIGRATION_2_SQL,
    MIGRATION_3_SQL,
    MIGRATION_4_SQL,
    MIGRATION_5_SQL,
    MIGRATION_6_SQL,
    MIGRATION_7_SQL,
    SCHEMA_V1_SQL,
    SCHEMA_VERSION,
    Database,
)


_LEGACY_MIGRATIONS = (
    MIGRATION_2_SQL,
    MIGRATION_3_SQL,
    MIGRATION_4_SQL,
    MIGRATION_5_SQL,
    MIGRATION_6_SQL,
    MIGRATION_7_SQL,
)


def _create_legacy_database(path: Path, *, version: int) -> None:
    if not 1 <= version <= 7:
        raise ValueError("legacy fixture version must be between 1 and 7")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1_SQL)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
        )
        connection.commit()
        for migration in _LEGACY_MIGRATIONS[: version - 1]:
            connection.executescript(migration)
    finally:
        connection.close()


def _create_schema_eight_database(path: Path) -> None:
    _create_legacy_database(path, version=7)
    Database(path)._migrate_to_8()


def _create_schema_ten_database(path: Path) -> None:
    _create_schema_eight_database(path)
    database = Database(path)
    database._migrate_to_9()
    database._migrate_to_10()


def _seed_schema_seven_graph_legacy(path: Path) -> None:
    _create_legacy_database(path, version=7)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO batches(
                id, name, status, total_count, queued_count, failed_count,
                duplicate_count, ready_count, canceled_count,
                created_at, updated_at
            ) VALUES (
                'batch-legacy', 'legacy graph', 'partial_success',
                1, 0, 0, 0, 0, 0, '2026-09-01', '2026-09-01'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO input_records(
                id, batch_id, raw_text, submitted_url, canonical_url,
                platform, source_type, source_id, ordinal,
                expected_item_count, status, created_at
            ) VALUES (
                'input-legacy', 'batch-legacy', 'legacy input',
                'https://x.com/example/status/700',
                'https://x.com/i/status/700', 'x', 'x_post', '700',
                1, 2, 'partial_success', '2026-09-01'
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO source_items(
                id, platform, source_type, source_id, canonical_url,
                created_at, updated_at
            ) VALUES (?, 'x', ?, ?, ?, '2026-09-01', '2026-09-01')
            """,
            [
                (
                    'source-parent',
                    'x_post',
                    '700',
                    'https://x.com/i/status/700',
                ),
                (
                    'source-child-a',
                    'x_post',
                    'legacy-child-a',
                    'https://x.com/i/status/700#legacy-a',
                ),
                (
                    'source-child-b',
                    'x_post',
                    'legacy-child-b',
                    'https://x.com/i/status/700#legacy-b',
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO download_jobs(
                id, batch_id, input_record_id, source_item_id, job_kind,
                status, progress, route_policy_version, available_at,
                attempt_count, created_at, updated_at
            ) VALUES (
                'job-flat', 'batch-legacy', 'input-legacy',
                'source-parent', 'download', 'ready', 1,
                'legacy-route', '2026-09-01', 2,
                '2026-09-01', '2026-09-01'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO job_attempts(
                id, job_id, attempt_no, adapter, adapter_version,
                status, started_at, finished_at, discovered_item_count,
                discovery_snapshot_hash, lease_token, exit_code
            ) VALUES (
                'attempt-flat', 'job-flat', 2, 'legacy-adapter', '1',
                'succeeded', '2026-09-01', '2026-09-01', 2,
                'legacy-snapshot', 'legacy-lease', 0
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO source_relations(
                id, parent_source_item_id, child_source_item_id,
                relation_type, ordinal, discovered_by_attempt_id, created_at
            ) VALUES (?, 'source-parent', ?, 'attachment', ?, ?, ?)
            """,
            [
                (
                    'relation-a',
                    'source-child-a',
                    0,
                    None,
                    '2026-09-01T00:00:00Z',
                ),
                (
                    'relation-b',
                    'source-child-b',
                    1,
                    'attempt-flat',
                    '2026-09-01T00:00:01Z',
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO media_assets(
                id, source_item_id, media_key, media_kind, size_bytes,
                sha256, status, created_at
            ) VALUES (?, 'source-parent', ?, 'video', ?, ?, 'ready', '2026-09-01')
            """,
            [
                ('asset-a', 'legacy-media-a', 10, 'a' * 64),
                ('asset-b', 'legacy-media-b', 20, 'b' * 64),
            ],
        )
        connection.executemany(
            """
            INSERT INTO job_assets(job_id, asset_id, role, ordinal)
            VALUES ('job-flat', ?, 'original', ?)
            """,
            [('asset-a', 0), ('asset-b', 1)],
        )
        connection.executemany(
            """
            INSERT INTO artifacts(
                id, asset_id, kind, path, sha256, created_at
            ) VALUES (?, ?, 'original', ?, ?, '2026-09-01')
            """,
            [
                (
                    'artifact-a',
                    'asset-a',
                    'assets/asset-a/original/a.mp4',
                    'a' * 64,
                ),
                (
                    'artifact-b',
                    'asset-b',
                    'assets/asset-b/original/b.mp4',
                    'b' * 64,
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _initialize_in_process(path: str, start, results) -> None:
    start.wait(10)
    try:
        database = Database(Path(path))
        database.initialize()
        results.put(database.readiness())
    except Exception as exc:  # pragma: no cover - returned to parent assertion
        results.put((False, f"{type(exc).__name__}:{exc}"))


def test_schema_one_database_migrates_to_current_without_losing_capability(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V1_SQL)
    # The first on-disk Schema 1 predates the live-source job index. Removing
    # it here freezes that legacy shape so the upgrade test cannot pass merely
    # because the current bootstrap SQL gained a newer index.
    connection.execute("DROP INDEX idx_one_live_job_per_source")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
    )
    connection.execute(
        """
        INSERT INTO batches(
            id, name, status, total_count, queued_count, failed_count,
            duplicate_count, created_at, updated_at
        ) VALUES ('batch-1', 'legacy duplicates', 'queued', 2, 2, 0, 0, '2026-01-01', '2026-01-01')
        """
    )
    connection.executemany(
        """
        INSERT INTO input_records(
            id, batch_id, raw_text, submitted_url, canonical_url,
            platform, source_type, source_id, ordinal, status, created_at
        ) VALUES (?, 'batch-1', ?, ?, 'https://x.com/i/status/123',
                  'x', 'x_post', '123', ?, 'queued', '2026-01-01')
        """,
        [
            ("input-1", "first", "https://x.com/a/status/123", 1),
            ("input-2", "second", "https://x.com/b/status/123", 2),
        ],
    )
    connection.execute(
        """
        INSERT INTO source_items(
            id, platform, source_type, source_id, canonical_url,
            created_at, updated_at
        ) VALUES ('source-1', 'x', 'x_post', '123',
                  'https://x.com/i/status/123', '2026-01-01', '2026-01-01')
        """
    )
    connection.executemany(
        """
        INSERT INTO download_jobs(
            id, batch_id, input_record_id, source_item_id, job_kind,
            status, progress, route_policy_version, created_at, updated_at
        ) VALUES (?, 'batch-1', ?, 'source-1', 'download', 'queued', 0,
                  'legacy-v1', ?, ?)
        """,
        [
            ("job-1", "input-1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ("job-2", "input-2", "2026-01-01T00:00:01Z", "2026-01-01T00:00:01Z"),
        ],
    )
    connection.execute(
        """
        INSERT INTO platform_capabilities(
            id, platform, source_type, adapter, status, notes
        ) VALUES ('cap-1', 'youtube', 'youtube_video', 'yt_dlp', 'candidate', 'keep')
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()

    with database.connect() as migrated:
        versions = [
            row["version"]
            for row in migrated.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        columns = {
            row["name"]
            for row in migrated.execute(
                "PRAGMA table_info(capability_legacy_schema9)"
            ).fetchall()
        }
        capability = migrated.execute(
            "SELECT * FROM capability_legacy_schema9 WHERE id = 'cap-1'"
        ).fetchone()
        current_capability_count = migrated.execute(
            "SELECT COUNT(*) AS count FROM platform_capabilities"
        ).fetchone()["count"]
        job_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(download_jobs)").fetchall()
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        legacy_jobs = migrated.execute(
            "SELECT id, status FROM download_jobs ORDER BY id"
        ).fetchall()
        migrated_inputs = migrated.execute(
            """
            SELECT id, status, duplicate_of_input_record_id
            FROM input_records ORDER BY id
            """
        ).fetchall()
        migrated_batch = migrated.execute(
            "SELECT status, queued_count, duplicate_count FROM batches WHERE id = 'batch-1'"
        ).fetchone()

    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert SCHEMA_VERSION == 11
    assert {"job_kind", "adapter_version", "environment"} <= columns
    assert capability["notes"] == "keep"
    assert capability["job_kind"] == "download"
    assert capability["adapter_version"] == "unknown"
    assert capability["environment"] == "unknown"
    assert current_capability_count == 0
    assert {
        "lease_token",
        "heartbeat_at",
        "available_at",
        "cancel_requested_at",
        "attempt_count",
    } <= job_columns
    assert "idx_one_live_job_per_source" not in indexes
    assert "idx_job_input_source_kind_generation" in indexes
    assert "idx_asset_commit_intents_attempt" in indexes
    assert "idx_asset_commit_intents_job" in indexes
    assert "idx_asset_commit_intents_recovery" in indexes
    assert [(row["id"], row["status"]) for row in legacy_jobs] == [
        ("job-1", "queued"),
        ("job-2", "canceled"),
    ]
    assert [
        (row["id"], row["status"], row["duplicate_of_input_record_id"])
        for row in migrated_inputs
    ] == [
        ("input-1", "queued", None),
        ("input-2", "duplicate", "input-1"),
    ]
    assert dict(migrated_batch) == {
        "status": "queued",
        "queued_count": 1,
        "duplicate_count": 1,
    }
    assert database.readiness() == (True, "ok")


def test_schema_three_database_migrates_explicitly_to_current(tmp_path: Path) -> None:
    path = tmp_path / "schema-three.sqlite3"
    _create_legacy_database(path, version=3)
    database = Database(path)
    database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(asset_commit_intents)"
            ).fetchall()
        }
        foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(asset_commit_intents)"
            ).fetchall()
        }

    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert columns == {
        "asset_id",
        "job_id",
        "attempt_id",
        "lease_token",
        "created_at",
        "recovery_token",
        "recovery_expires_at",
    }
    assert foreign_keys == {
        ("job_id", "download_jobs", "id"),
        ("attempt_id", "job_attempts", "id"),
    }
    assert database.readiness() == (True, "ok")


def test_schema_ten_database_migrates_to_closed_claim_gate(tmp_path: Path) -> None:
    path = tmp_path / "schema-ten.sqlite3"
    _create_schema_ten_database(path)
    database = Database(path)

    database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        gate = connection.execute(
            """
            SELECT id, run_id, worker_id, accepting_claims,
                   activated_at, stop_requested_at
            FROM worker_claim_gate
            """
        ).fetchone()
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert dict(gate) == {
        "id": 1,
        "run_id": None,
        "worker_id": None,
        "accepting_claims": 0,
        "activated_at": None,
        "stop_requested_at": None,
    }
    assert database.readiness() == (True, "ok")


def test_schema_eleven_migration_failure_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-eleven-rollback.sqlite3"
    _create_schema_ten_database(path)
    database = Database(path)
    failing_migration = database_module.MIGRATION_11_SQL.replace(
        "INSERT INTO schema_migrations(version, applied_at)",
        "SELECT missing_column FROM missing_table;\n\n"
        "INSERT INTO schema_migrations(version, applied_at)",
    )
    monkeypatch.setattr(database_module, "MIGRATION_11_SQL", failing_migration)

    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        gate_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'worker_claim_gate'
            """
        ).fetchone()
    assert versions == list(range(1, 11))
    assert gate_table is None


def test_readiness_rejects_missing_worker_claim_gate_singleton(tmp_path: Path) -> None:
    database = Database(tmp_path / "missing-claim-gate.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DELETE FROM worker_claim_gate WHERE id = 1")

    assert database.readiness() == (
        False,
        "missing_worker_claim_gate_singleton",
    )


def test_readiness_rejects_malformed_worker_claim_gate_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "malformed-claim-gate-state.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE worker_claim_gate
            SET run_id = ?, worker_id = ?, accepting_claims = 1,
                activated_at = ''
            WHERE id = 1
            """,
            ("a" * 32, "local-app-worker"),
        )

    assert database.readiness() == (
        False,
        "malformed_worker_claim_gate_state",
    )


def test_readiness_rejects_worker_claim_gate_without_schema_constraints(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "malformed-claim-gate-table.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TABLE worker_claim_gate")
        connection.execute(
            """
            CREATE TABLE worker_claim_gate (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                worker_id TEXT,
                accepting_claims INTEGER NOT NULL DEFAULT 0,
                activated_at TEXT,
                stop_requested_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO worker_claim_gate(
                id, run_id, worker_id, accepting_claims,
                activated_at, stop_requested_at, updated_at
            ) VALUES (1, NULL, NULL, 0, NULL, NULL, '2026-09-03T00:00:00Z')
            """
        )

    assert database.readiness() == (
        False,
        "malformed_table:worker_claim_gate",
    )


def test_readiness_rejects_any_worker_claim_gate_trigger(tmp_path: Path) -> None:
    database = Database(tmp_path / "triggered-claim-gate.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reopen_gate_after_stop
            AFTER UPDATE ON worker_claim_gate
            WHEN NEW.accepting_claims = 0
                 AND NEW.stop_requested_at IS NOT NULL
            BEGIN
                UPDATE worker_claim_gate
                SET accepting_claims = 1,
                    activated_at = NEW.stop_requested_at,
                    stop_requested_at = NULL,
                    updated_at = NEW.stop_requested_at
                WHERE id = 1;
            END
            """
        )

    assert database.readiness() == (
        False,
        "forbidden_triggers:worker_claim_gate",
    )


def test_readiness_detects_missing_schema_index(tmp_path: Path) -> None:
    database = Database(tmp_path / "index.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP INDEX idx_capability_evidence_digest")

    ready, detail = database.readiness()
    assert ready is False
    assert detail == "missing_indexes:idx_capability_evidence_digest"


def test_schema_ten_capability_identity_includes_required_job_kind(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "capability-job-kind.sqlite3")
    database.initialize()

    with database.connect() as connection:
        job_kind_column = next(
            row
            for row in connection.execute(
                "PRAGMA table_info(capability_evidence)"
            ).fetchall()
            if row["name"] == "job_kind"
        )
        index = next(
            row
            for row in connection.execute(
                "PRAGMA index_list(capability_evidence)"
            ).fetchall()
            if row["name"] == "idx_capability_evidence_id_identity"
        )
        index_columns = tuple(
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(idx_capability_evidence_id_identity)"
            ).fetchall()
        )
        object_type = connection.execute(
            """
            SELECT type FROM sqlite_master WHERE name = 'platform_capabilities'
            """
        ).fetchone()["type"]

    assert bool(job_kind_column["notnull"])
    assert job_kind_column["dflt_value"] is None
    assert bool(index["unique"])
    assert index_columns == (
        "evidence_id",
        "identity_key",
        "platform",
        "source_type",
        "job_kind",
        "adapter",
        "downloader_version",
        "environment",
        "product_version",
    )
    assert object_type == "view"
    assert database.readiness() == (True, "ok")


def test_schema_eight_capabilities_migrate_to_download_without_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-eight-capabilities.sqlite3"
    _create_schema_eight_database(path)
    database = Database(path)
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO platform_capabilities(
                id, platform, source_type, adapter, adapter_version,
                environment, status, last_verified_at, tested_version,
                sample_set_version, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "legacy-candidate",
                    "youtube",
                    "youtube_video",
                    "yt_dlp",
                    "2026.08.19",
                    "AU-SA-no-cookie",
                    "candidate",
                    None,
                    "0.10.0",
                    "samples-a",
                    "candidate note",
                ),
                (
                    "legacy-verified",
                    "x",
                    "x_post",
                    "legacy-adapter",
                    "1.2.3",
                    "AU-SA-cookie",
                    "verified",
                    "2026-09-01T00:00:00Z",
                    "0.9.0",
                    "samples-b",
                    "verified note",
                ),
            ),
        )

    database.initialize()

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM capability_legacy_schema9 ORDER BY id"
        ).fetchall()
        current_count = connection.execute(
            "SELECT COUNT(*) AS count FROM platform_capabilities"
        ).fetchone()["count"]
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert [dict(row) for row in rows] == [
        {
            "id": "legacy-candidate",
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "adapter": "yt_dlp",
            "adapter_version": "2026.08.19",
            "environment": "AU-SA-no-cookie",
            "status": "candidate",
            "last_verified_at": None,
            "tested_version": "0.10.0",
            "sample_set_version": "samples-a",
            "notes": "candidate note",
        },
        {
            "id": "legacy-verified",
            "platform": "x",
            "source_type": "x_post",
            "job_kind": "download",
            "adapter": "legacy-adapter",
            "adapter_version": "1.2.3",
            "environment": "AU-SA-cookie",
            "status": "verified",
            "last_verified_at": "2026-09-01T00:00:00Z",
            "tested_version": "0.9.0",
            "sample_set_version": "samples-b",
            "notes": "verified note",
        },
    ]
    assert all(row["job_kind"] != "discover" for row in rows)
    assert current_count == 0
    assert database.readiness() == (True, "ok")


def test_readiness_detects_malformed_capability_identity_index(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "capability-index-shape.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP INDEX idx_capability_evidence_id_identity")
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_capability_evidence_id_identity
            ON capability_evidence(
                evidence_id, platform, source_type, job_kind, adapter,
                downloader_version, environment, product_version
            )
            """
        )

    assert database.readiness() == (
        False,
        "malformed_index:idx_capability_evidence_id_identity",
    )


def test_readiness_detects_missing_capability_rate_trigger(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "capability-check-shape.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TRIGGER trg_capability_evidence_validate_rates")

    assert database.readiness() == (
        False,
        "missing_triggers:trg_capability_evidence_validate_rates",
    )


def test_schema_seven_adds_credential_disable_marker(tmp_path: Path) -> None:
    database = Database(tmp_path / "credential-disable.sqlite3")
    database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(credential_profiles)"
            ).fetchall()
        }

    assert "disabled_at" in columns
    assert database.readiness() == (True, "ok")


def test_schema_eight_migrates_legacy_graph_and_preserves_flat_v1_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-seven-graph.sqlite3"
    _seed_schema_seven_graph_legacy(path)
    database = Database(path)

    database.initialize()

    expected_members = json.dumps(
        [
            {
                "child_source_item_id": "source-child-a",
                "media_kind": "unknown",
                "ordinal": 0,
                "selector_key": None,
            },
            {
                "child_source_item_id": "source-child-b",
                "media_kind": "unknown",
                "ordinal": 1,
                "selector_key": None,
            },
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_snapshot = hashlib.sha256(expected_members.encode("utf-8")).hexdigest()
    expected_identity = hashlib.sha256(
        f"source-parent\0attachment\0{expected_snapshot}".encode("utf-8")
    ).hexdigest()
    expected_discovery_id = f"legacy:v1:{expected_identity}"

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        discovery = connection.execute(
            "SELECT * FROM source_discoveries"
        ).fetchone()
        relations = connection.execute(
            """
            SELECT id, discovery_id, parent_source_item_id,
                   child_source_item_id, relation_type, ordinal, media_kind,
                   discovered_by_attempt_id, created_at
            FROM source_relations ORDER BY ordinal
            """
        ).fetchall()
        job = connection.execute(
            """
            SELECT id, source_item_id, job_kind, attempt_count,
                   run_generation, generation_attempt_count, reused_from_job_id
            FROM download_jobs WHERE id = 'job-flat'
            """
        ).fetchone()
        attempt = connection.execute(
            """
            SELECT attempt_no, run_generation, generation_attempt_no
            FROM job_attempts WHERE id = 'attempt-flat'
            """
        ).fetchone()
        input_record = connection.execute(
            """
            SELECT active_discovery_id, active_run_generation,
                   cancel_requested_at
            FROM input_records WHERE id = 'input-legacy'
            """
        ).fetchone()
        batch = connection.execute(
            "SELECT partial_success_count FROM batches WHERE id = 'batch-legacy'"
        ).fetchone()
        assets = connection.execute(
            """
            SELECT id, source_item_id, media_key, sha256
            FROM media_assets ORDER BY id
            """
        ).fetchall()
        artifacts = connection.execute(
            "SELECT id, asset_id, path, sha256 FROM artifacts ORDER BY id"
        ).fetchall()
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        relation_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(source_relations)"
            ).fetchall()
        }
        discovery_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(source_discoveries)"
            ).fetchall()
        }
        graph_link_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(input_relation_jobs)"
            ).fetchall()
        }
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert dict(discovery) == {
        "id": expected_discovery_id,
        "parent_source_item_id": "source-parent",
        "relation_type": "attachment",
        "snapshot_hash": expected_snapshot,
        "members_json": expected_members,
        "member_count": 2,
        "discovered_by_attempt_id": "attempt-flat",
        "created_at": "2026-09-01T00:00:00Z",
    }
    assert [dict(row) for row in relations] == [
        {
            "id": "relation-a",
            "discovery_id": expected_discovery_id,
            "parent_source_item_id": "source-parent",
            "child_source_item_id": "source-child-a",
            "relation_type": "attachment",
            "ordinal": 0,
            "media_kind": "unknown",
            "discovered_by_attempt_id": None,
            "created_at": "2026-09-01T00:00:00Z",
        },
        {
            "id": "relation-b",
            "discovery_id": expected_discovery_id,
            "parent_source_item_id": "source-parent",
            "child_source_item_id": "source-child-b",
            "relation_type": "attachment",
            "ordinal": 1,
            "media_kind": "unknown",
            "discovered_by_attempt_id": "attempt-flat",
            "created_at": "2026-09-01T00:00:01Z",
        },
    ]
    assert dict(job) == {
        "id": "job-flat",
        "source_item_id": "source-parent",
        "job_kind": "download",
        "attempt_count": 2,
        "run_generation": 1,
        "generation_attempt_count": 2,
        "reused_from_job_id": None,
    }
    assert dict(attempt) == {
        "attempt_no": 2,
        "run_generation": 1,
        "generation_attempt_no": 2,
    }
    assert dict(input_record) == {
        "active_discovery_id": None,
        "active_run_generation": 1,
        "cancel_requested_at": None,
    }
    assert batch["partial_success_count"] == 1
    assert [tuple(row) for row in assets] == [
        ("asset-a", "source-parent", "legacy-media-a", "a" * 64),
        ("asset-b", "source-parent", "legacy-media-b", "b" * 64),
    ]
    assert [tuple(row) for row in artifacts] == [
        (
            "artifact-a",
            "asset-a",
            "assets/asset-a/original/a.mp4",
            "a" * 64,
        ),
        (
            "artifact-b",
            "asset-b",
            "assets/asset-b/original/b.mp4",
            "b" * 64,
        ),
    ]
    assert "idx_one_live_job_per_source" not in indexes
    assert "idx_job_input_source_kind_generation" in indexes
    assert (
        "discovered_by_attempt_id",
        "job_attempts",
        "id",
    ) in relation_foreign_keys
    assert {
        ("parent_source_item_id", "source_items", "id"),
        ("discovered_by_attempt_id", "job_attempts", "id"),
    } <= discovery_foreign_keys
    assert {
        ("discovery_id", "source_discoveries", "id"),
        ("discovery_id", "source_relations", "discovery_id"),
        ("relation_id", "source_relations", "id"),
    } <= graph_link_foreign_keys
    assert foreign_key_violations == []
    assert database.readiness() == (True, "ok")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE source_discoveries SET members_json = '[]' WHERE id = ?",
                (expected_discovery_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "DELETE FROM source_discoveries WHERE id = ?",
                (expected_discovery_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE source_relations SET ordinal = 7 WHERE id = 'relation-a'"
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "DELETE FROM source_relations WHERE id = 'relation-a'"
            )


@pytest.mark.parametrize("member_count", [0, 51])
def test_schema_eight_discovery_member_count_is_bounded(
    tmp_path: Path, member_count: int
) -> None:
    database = Database(tmp_path / f"member-count-{member_count}.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_items(
                id, platform, source_type, source_id, canonical_url,
                created_at, updated_at
            ) VALUES (
                'bounded-parent', 'x', 'x_post', 'bounded-parent',
                'https://x.com/i/status/880', '2026-09-03', '2026-09-03'
            )
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_discoveries(
                    id, parent_source_item_id, relation_type, snapshot_hash,
                    members_json, member_count, created_at
                ) VALUES (?, 'bounded-parent', 'attachment', ?, '[]', ?, '2026-09-03')
                """,
                (f"invalid-{member_count}", f"{member_count:064x}", member_count),
            )


def test_schema_eight_job_identity_is_input_and_generation_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job-identity.sqlite3"
    _seed_schema_seven_graph_legacy(path)
    database = Database(path)
    database.initialize()

    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO input_records(
                id, batch_id, raw_text, ordinal, status, created_at
            ) VALUES (
                'input-second', 'batch-legacy', 'second input', 2,
                'queued', '2026-09-02'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO download_jobs(
                id, batch_id, input_record_id, source_item_id, job_kind,
                status, route_policy_version, run_generation,
                created_at, updated_at
            ) VALUES (
                'job-second-input', 'batch-legacy', 'input-second',
                'source-parent', 'download', 'queued', 'test', 1,
                '2026-09-02', '2026-09-02'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO download_jobs(
                id, batch_id, input_record_id, source_item_id, job_kind,
                status, route_policy_version, run_generation,
                created_at, updated_at
            ) VALUES (
                'job-generation-two', 'batch-legacy', 'input-legacy',
                'source-parent', 'download', 'queued', 'test', 2,
                '2026-09-02', '2026-09-02'
            )
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO download_jobs(
                    id, batch_id, input_record_id, source_item_id, job_kind,
                    status, route_policy_version, run_generation,
                    created_at, updated_at
                ) VALUES (
                    'job-duplicate', 'batch-legacy', 'input-legacy',
                    'source-parent', 'download', 'queued', 'test', 1,
                    '2026-09-02', '2026-09-02'
                )
                """
            )


def test_schema_eight_failure_rolls_back_only_its_independent_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-eight-rollback.sqlite3"
    _create_legacy_database(path, version=6)
    database = Database(path)

    def fail_after_relation_rebuild(_connection):
        raise RuntimeError("simulated schema eight failure")

    monkeypatch.setattr(
        database_module,
        "_legacy_discovery_rows",
        fail_after_relation_rebuild,
    )

    with pytest.raises(RuntimeError, match="schema eight failure"):
        database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        relation_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(source_relations)"
            ).fetchall()
        }
        credential_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(credential_profiles)"
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert versions == list(range(1, 8))
    assert "disabled_at" in credential_columns
    assert "source_discoveries" not in tables
    assert "source_relations_v7" not in tables
    assert "discovery_id" not in relation_columns
    assert "idx_one_live_job_per_source" in indexes
    assert "idx_job_input_source_kind_generation" not in indexes


def test_schema_eight_rejects_orphan_legacy_attempt_without_partial_rebuild(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orphan-relation-attempt.sqlite3"
    _create_legacy_database(path, version=7)
    connection = sqlite3.connect(path)
    try:
        connection.executemany(
            """
            INSERT INTO source_items(
                id, platform, source_type, source_id, canonical_url,
                created_at, updated_at
            ) VALUES (?, 'x', 'x_post', ?, ?, '2026-09-01', '2026-09-01')
            """,
            [
                ('orphan-parent', 'orphan-parent', 'https://x.com/i/status/801'),
                ('orphan-child', 'orphan-child', 'https://x.com/i/status/802'),
            ],
        )
        connection.execute(
            """
            INSERT INTO source_relations(
                id, parent_source_item_id, child_source_item_id,
                relation_type, ordinal, discovered_by_attempt_id, created_at
            ) VALUES (
                'orphan-relation', 'orphan-parent', 'orphan-child',
                'attachment', 0, 'missing-attempt', '2026-09-01'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    with pytest.raises(RuntimeError, match="invalid legacy relation attempt"):
        database.initialize()

    with database.connect() as connection:
        version = connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(source_relations)"
            ).fetchall()
        }
        discovery_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'source_discoveries'
            """
        ).fetchone()
    assert version == 7
    assert "discovery_id" not in columns
    assert discovery_table is None


def test_readiness_detects_missing_schema_eight_trigger(tmp_path: Path) -> None:
    database = Database(tmp_path / "missing-trigger.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TRIGGER trg_source_discoveries_no_delete")

    assert database.readiness() == (
        False,
        "missing_triggers:trg_source_discoveries_no_delete",
    )


def test_readiness_detects_missing_schema_eight_foreign_key(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "missing-graph-fk.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TABLE download_job_targets")
        connection.execute(
            """
            CREATE TABLE download_job_targets (
                job_id TEXT PRIMARY KEY REFERENCES download_jobs(id) ON DELETE CASCADE,
                fetch_source_item_id TEXT NOT NULL,
                selector_key TEXT NOT NULL,
                expected_media_key TEXT NOT NULL,
                expected_media_kind TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_download_job_targets_fetch_source
            ON download_job_targets(fetch_source_item_id, job_id)
            """
        )

    assert database.readiness() == (
        False,
        "missing_foreign_keys:download_job_targets:"
        "fetch_source_item_id->source_items.id",
    )


def test_schema_six_migration_failure_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-six-rollback.sqlite3"
    _create_legacy_database(path, version=5)
    database = Database(path)

    monkeypatch.setattr(
        database_module,
        "MIGRATION_6_SQL",
        """
        BEGIN IMMEDIATE;
        ALTER TABLE asset_commit_intents ADD COLUMN recovery_token TEXT;
        SELECT missing_column FROM missing_table;
        INSERT INTO schema_migrations(version, applied_at)
        VALUES (6, 'must-not-commit');
        COMMIT;
        """,
    )

    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(asset_commit_intents)"
            ).fetchall()
        }
    assert versions == [1, 2, 3, 4, 5]
    assert columns == {
        "asset_id",
        "job_id",
        "attempt_id",
        "lease_token",
        "created_at",
    }


def test_schema_seven_migration_failure_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-seven-rollback.sqlite3"
    _create_legacy_database(path, version=6)
    database = Database(path)

    monkeypatch.setattr(
        database_module,
        "MIGRATION_7_SQL",
        """
        BEGIN IMMEDIATE;
        ALTER TABLE credential_profiles ADD COLUMN disabled_at TEXT;
        SELECT missing_column FROM missing_table;
        INSERT INTO schema_migrations(version, applied_at)
        VALUES (7, 'must-not-commit');
        COMMIT;
        """,
    )

    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(credential_profiles)"
            ).fetchall()
        }
    assert versions == [1, 2, 3, 4, 5, 6]
    assert "disabled_at" not in columns


def test_schema_nine_migration_failure_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-nine-rollback.sqlite3"
    _create_schema_eight_database(path)
    database = Database(path)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO platform_capabilities(
                id, platform, source_type, adapter, adapter_version,
                environment, status, last_verified_at, tested_version,
                sample_set_version, notes
            ) VALUES (
                'cap-before-nine', 'youtube', 'youtube_video', 'yt_dlp',
                'legacy-adapter', 'legacy-environment', 'verified',
                '2026-09-01T00:00:00Z', 'legacy-tested',
                'legacy-samples', 'must survive'
            )
            """
        )

    failing_migration = database_module.MIGRATION_9_SQL.replace(
        "INSERT INTO schema_migrations(version, applied_at)",
        "SELECT missing_column FROM missing_table;\n\n"
        "INSERT INTO schema_migrations(version, applied_at)",
    )
    monkeypatch.setattr(
        database_module,
        "MIGRATION_9_SQL",
        failing_migration,
    )

    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(platform_capabilities)"
            ).fetchall()
        }
        index_columns = tuple(
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(idx_capability_evidence_identity)"
            ).fetchall()
        )
        capability = connection.execute(
            "SELECT * FROM platform_capabilities WHERE id = 'cap-before-nine'"
        ).fetchone()
        temporary_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'platform_capabilities_v8'
            """
        ).fetchone()

    assert versions == list(range(1, 9))
    assert "job_kind" not in columns
    assert index_columns == (
        "platform",
        "source_type",
        "adapter",
        "adapter_version",
        "environment",
    )
    assert dict(capability) == {
        "id": "cap-before-nine",
        "platform": "youtube",
        "source_type": "youtube_video",
        "adapter": "yt_dlp",
        "adapter_version": "legacy-adapter",
        "environment": "legacy-environment",
        "status": "verified",
        "last_verified_at": "2026-09-01T00:00:00Z",
        "tested_version": "legacy-tested",
        "sample_set_version": "legacy-samples",
        "notes": "must survive",
    }
    assert temporary_table is None


def test_concurrent_initialize_of_new_database_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    barrier = Barrier(8)

    def initialize(_: int) -> tuple[bool, str]:
        barrier.wait()
        database = Database(path)
        database.initialize()
        return database.readiness()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(initialize, range(8)))

    assert results == [(True, "ok")] * 8


def test_concurrent_initialize_is_safe_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "multiprocess.sqlite3"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_initialize_in_process,
            args=(str(path), start, results),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)

    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    outcomes = [results.get(timeout=5) for _ in processes]
    assert outcomes == [(True, "ok")] * 4


def test_concurrent_schema_seven_upgrade_is_safe_across_processes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "multiprocess-schema-seven.sqlite3"
    _seed_schema_seven_graph_legacy(path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_initialize_in_process,
            args=(str(path), start, results),
        )
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)

    assert [process.exitcode for process in processes] == [0, 0, 0]
    outcomes = [results.get(timeout=5) for _ in processes]
    assert outcomes == [(True, "ok")] * 3
    database = Database(path)
    with database.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        discovery_count = connection.execute(
            "SELECT COUNT(*) AS count FROM source_discoveries"
        ).fetchone()["count"]
        relation_count = connection.execute(
            "SELECT COUNT(*) AS count FROM source_relations"
        ).fetchone()["count"]
    assert [row["version"] for row in versions] == list(
        range(1, SCHEMA_VERSION + 1)
    )
    assert discovery_count == 1
    assert relation_count == 2

from __future__ import annotations

import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import video_download_control.database as database_module
import video_download_control.capabilities as capabilities_module
from video_download_control.capabilities import (
    CapabilityStatus,
    PlatformCapabilityRegistry,
)
from video_download_control.capability_evidence import (
    capability_identity_key,
    evidence_sha256_from_payload,
)
from video_download_control.database import (
    CAPABILITY_DECISIONS_VERIFIED_TRIGGER_SQL,
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
from video_download_control.validation import STAGE0_POLICY_VERSION


_IDENTITY_FIELDS = {
    "platform": "youtube",
    "source_type": "youtube_video",
    "job_kind": "download",
    "adapter": "yt_dlp",
    "downloader_version": "2026.08.19",
    "environment": "windows-x64-direct-no-cookie",
    "product_version": "0.13.0",
}
_IDENTITY = {
    "identity_key": capability_identity_key(**_IDENTITY_FIELDS),
    **_IDENTITY_FIELDS,
}

_EVIDENCE_NO_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER trg_capability_evidence_no_update
BEFORE UPDATE ON capability_evidence
BEGIN
    SELECT RAISE(ABORT, 'capability_evidence is immutable');
END;
"""

_DECISION_NO_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER trg_capability_decisions_no_update
BEFORE UPDATE ON capability_decisions
BEGIN
    SELECT RAISE(ABORT, 'capability_decisions is append-only');
END;
"""

_DECISION_TRANSITION_TRIGGER_SQL = """
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


def _create_schema_nine_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1_SQL)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
        )
        connection.commit()
        for migration in (
            MIGRATION_2_SQL,
            MIGRATION_3_SQL,
            MIGRATION_4_SQL,
            MIGRATION_5_SQL,
            MIGRATION_6_SQL,
            MIGRATION_7_SQL,
        ):
            connection.executescript(migration)
    finally:
        connection.close()
    database = Database(path)
    database._migrate_to_8()
    database._migrate_to_9()


def _initialize_in_process(path: str, start, results) -> None:
    start.wait(10)
    try:
        database = Database(Path(path))
        database.initialize()
        results.put(database.readiness())
    except Exception as exc:  # pragma: no cover - asserted in the parent process
        results.put((False, f"{type(exc).__name__}:{exc}"))


def _insert_evidence(
    connection: sqlite3.Connection,
    *,
    evidence_id: str,
    verdict: str = "verified",
    identity: dict[str, str] | None = None,
    policy_version: str = STAGE0_POLICY_VERSION,
) -> None:
    values = dict(_IDENTITY if identity is None else identity)
    identity_fields = {
        name: values[name]
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
    values.setdefault("identity_key", capability_identity_key(**identity_fields))
    positive_rates = [0.9, 0.95, 1.0]
    negative_rates = [1.0, 1.0, 1.0]
    bundle_sha256 = "4" * 64
    evaluated_at = "2026-09-03T00:00:00.000Z"
    public_payload = {
        **identity_fields,
        "identity_key": values["identity_key"],
        "evidence_kind": "stage0_csv_v3",
        "policy_version": policy_version,
        "bundle_sha256": bundle_sha256,
        "positive_sample_count": 10,
        "negative_sample_count": 1,
        "complete_run_count": 3,
        "positive_rates": positive_rates,
        "negative_rates": negative_rates,
        "verdict": verdict,
        "evaluated_at": evaluated_at,
    }
    connection.execute(
        """
        INSERT INTO capability_evidence(
            evidence_id, identity_key, platform, source_type, job_kind,
            adapter, downloader_version, environment, product_version,
            evidence_kind, policy_version, manifest_sha256, results_sha256,
            bundle_sha256, evidence_sha256, positive_sample_count,
            negative_sample_count, complete_run_count, positive_rates_json,
            negative_rates_json, verdict, evaluated_at, imported_at
        ) VALUES (
            :evidence_id, :identity_key, :platform, :source_type, :job_kind,
            :adapter, :downloader_version, :environment, :product_version,
            'stage0_csv_v3', :policy_version, :manifest_sha256,
            :results_sha256, :bundle_sha256, :evidence_sha256, 10, 1, 3,
            '[0.9,0.95,1.0]', '[1.0,1.0,1.0]', :verdict,
            :evaluated_at, '2026-09-03T00:01:00.000Z'
        )
        """,
        {
            **values,
            "evidence_id": evidence_id,
            "manifest_sha256": "2" * 64,
            "results_sha256": "3" * 64,
            "bundle_sha256": bundle_sha256,
            "evidence_sha256": evidence_sha256_from_payload(public_payload),
            "policy_version": policy_version,
            "verdict": verdict,
            "evaluated_at": evaluated_at,
        },
    )


def _insert_decision(
    connection: sqlite3.Connection,
    *,
    decision_id: str,
    evidence_id: str | None,
    action: str,
    status: str,
    supersedes_decision_id: str | None,
    revision: int,
    identity: dict[str, str] | None = None,
) -> None:
    values = dict(_IDENTITY if identity is None else identity)
    connection.execute(
        """
        INSERT INTO capability_decisions(
            decision_id, identity_key, platform, source_type, job_kind,
            adapter, downloader_version, environment, product_version,
            evidence_id, action, status, supersedes_decision_id, revision,
            reason_code, decided_at
        ) VALUES (
            :decision_id, :identity_key, :platform, :source_type, :job_kind,
            :adapter, :downloader_version, :environment, :product_version,
            :evidence_id, :action, :status, :supersedes_decision_id, :revision,
            'test-decision', '2026-09-03T00:02:00.000Z'
        )
        """,
        {
            **values,
            "decision_id": decision_id,
            "evidence_id": evidence_id,
            "action": action,
            "status": status,
            "supersedes_decision_id": supersedes_decision_id,
            "revision": revision,
        },
    )


def test_schema_nine_rows_are_archived_without_promoting_legacy_verified(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-nine.sqlite3"
    _create_schema_nine_database(path)
    with Database(path).connect() as connection:
        connection.executemany(
            """
            INSERT INTO platform_capabilities(
                id, platform, source_type, job_kind, adapter, adapter_version,
                environment, status, last_verified_at, tested_version,
                sample_set_version, notes
            ) VALUES (?, 'youtube', 'youtube_video', 'download', 'yt_dlp',
                      '2026.08.19', ?, ?, ?, ?, 'samples', ?)
            """,
            (
                (
                    "legacy-candidate",
                    "windows-candidate",
                    "candidate",
                    None,
                    None,
                    "candidate",
                ),
                (
                    "legacy-verified",
                    "windows-verified",
                    "verified",
                    "2026-09-01T00:00:00Z",
                    "0.12.0",
                    "verified",
                ),
            ),
        )

    database = Database(path)
    database.initialize()

    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        archived = connection.execute(
            "SELECT * FROM capability_legacy_schema9 ORDER BY id"
        ).fetchall()
        counts = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "capability_evidence",
                "capability_decisions",
                "platform_capabilities",
            )
        }

    assert SCHEMA_VERSION == 11
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert [(row["id"], row["status"]) for row in archived] == [
        ("legacy-candidate", "candidate"),
        ("legacy-verified", "verified"),
    ]
    assert counts == {
        "capability_evidence": 0,
        "capability_decisions": 0,
        "platform_capabilities": 0,
    }
    assert database.readiness() == (True, "ok")
    with pytest.raises(sqlite3.IntegrityError, match="archive is immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE capability_legacy_schema9 SET status='candidate'"
            )


def test_schema_ten_ledger_is_immutable_and_current_head_uses_revision_cas(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ledger.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(connection, evidence_id="evidence-a")
        _insert_decision(
            connection,
            decision_id="decision-1",
            evidence_id="evidence-a",
            action="approve",
            status="verified",
            supersedes_decision_id=None,
            revision=1,
        )
        _insert_decision(
            connection,
            decision_id="decision-2",
            evidence_id="evidence-a",
            action="revoke",
            status="candidate",
            supersedes_decision_id="decision-1",
            revision=2,
        )
        current = connection.execute(
            "SELECT * FROM platform_capabilities"
        ).fetchone()

    assert current["current_decision_id"] == "decision-2"
    assert current["revision"] == 2
    assert current["action"] == "revoke"
    assert current["status"] == "candidate"
    assert current["product_version"] == "0.13.0"

    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as connection:
            _insert_decision(
                connection,
                decision_id="stale-successor",
                evidence_id="evidence-a",
                action="revoke",
                status="candidate",
                supersedes_decision_id="decision-1",
                revision=2,
            )
    with pytest.raises(sqlite3.IntegrityError, match="revision"):
        with database.connect() as connection:
            _insert_decision(
                connection,
                decision_id="skipped-revision",
                evidence_id="evidence-a",
                action="approve",
                status="verified",
                supersedes_decision_id="decision-2",
                revision=4,
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE capability_evidence SET verdict='candidate'"
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with database.connect() as connection:
            connection.execute("DELETE FROM capability_decisions")
    with pytest.raises(sqlite3.OperationalError, match="view"):
        with database.connect() as connection:
            connection.execute("DELETE FROM platform_capabilities")
    assert database.readiness() == (True, "ok")


def test_approve_requires_matching_verified_evidence_and_exact_product_version(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "verified-evidence.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(
            connection,
            evidence_id="candidate-evidence",
            verdict="candidate",
        )

    with pytest.raises(sqlite3.IntegrityError, match="verified Stage 0"):
        with database.connect() as connection:
            _insert_decision(
                connection,
                decision_id="invalid-approval",
                evidence_id="candidate-evidence",
                action="approve",
                status="verified",
                supersedes_decision_id=None,
                revision=1,
            )

    different_product = {**_IDENTITY, "product_version": "0.13.1"}
    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as connection:
            _insert_decision(
                connection,
                decision_id="wrong-product",
                evidence_id="candidate-evidence",
                action="revoke",
                status="candidate",
                supersedes_decision_id=None,
                revision=1,
                identity=different_product,
            )


def test_schema_ten_rejects_non_current_evidence_policy(tmp_path: Path) -> None:
    database = Database(tmp_path / "stale-evidence-policy.sqlite3")
    database.initialize()

    with pytest.raises(sqlite3.IntegrityError, match="policy_version"):
        with database.connect() as connection:
            _insert_evidence(
                connection,
                evidence_id="stale-policy-evidence",
                policy_version="stage0-policy-v1",
            )

def test_readiness_detects_recreated_guard_with_forged_verified_decision(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "semantic-tamper.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(
            connection,
            evidence_id="candidate-evidence",
            verdict="candidate",
        )
        connection.execute("DROP TRIGGER trg_capability_decisions_verified_evidence")
        _insert_decision(
            connection,
            decision_id="forged-approval",
            evidence_id="candidate-evidence",
            action="approve",
            status="verified",
            supersedes_decision_id=None,
            revision=1,
        )
        connection.executescript(CAPABILITY_DECISIONS_VERIFIED_TRIGGER_SQL)

    assert database.readiness() == (
        False,
        "invalid_capability_state:verified_without_evidence",
    )


@pytest.mark.parametrize(
    ("column", "replacement", "expected_detail"),
    (
        (
            "identity_key",
            "f" * 64,
            "invalid_capability_state:evidence_identity_key",
        ),
        (
            "evidence_sha256",
            "f" * 64,
            "invalid_capability_state:evidence_digest",
        ),
    ),
)
def test_readiness_recomputes_evidence_identity_and_digest(
    tmp_path: Path,
    column: str,
    replacement: str,
    expected_detail: str,
) -> None:
    database = Database(tmp_path / f"tampered-{column}.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(connection, evidence_id="evidence-tampered")
        connection.execute("DROP TRIGGER trg_capability_evidence_no_update")
        connection.execute(
            f"UPDATE capability_evidence SET {column} = ?",
            (replacement,),
        )
        connection.executescript(_EVIDENCE_NO_UPDATE_TRIGGER_SQL)

    assert database.readiness() == (False, expected_detail)


def test_readiness_rejects_unknown_evidence_route_with_valid_hashes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "unknown-evidence-route.sqlite3")
    database.initialize()
    unknown_fields = {
        **_IDENTITY_FIELDS,
        "platform": "unknown-platform",
        "source_type": "unknown_video",
    }
    unknown_identity = {
        "identity_key": capability_identity_key(**unknown_fields),
        **unknown_fields,
    }
    with database.connect() as connection:
        _insert_evidence(
            connection,
            evidence_id="unknown-route-evidence",
            identity=unknown_identity,
        )

    assert database.readiness() == (
        False,
        "invalid_capability_state:unknown_evidence_route",
    )


def test_readiness_rejects_unknown_decision_route_after_direct_tamper(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "unknown-decision-route.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(connection, evidence_id="registered-evidence")
        _insert_decision(
            connection,
            decision_id="registered-decision",
            evidence_id="registered-evidence",
            action="approve",
            status="verified",
            supersedes_decision_id=None,
            revision=1,
        )

    unknown_fields = {
        **_IDENTITY_FIELDS,
        "platform": "unknown-platform",
        "source_type": "unknown_video",
    }
    unknown_key = capability_identity_key(**unknown_fields)
    connection = sqlite3.connect(database.path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER trg_capability_decisions_no_update")
        connection.execute(
            """
            UPDATE capability_decisions
            SET identity_key = ?, platform = ?, source_type = ?
            WHERE decision_id = 'registered-decision'
            """,
            (unknown_key, unknown_fields["platform"], unknown_fields["source_type"]),
        )
        connection.executescript(_DECISION_NO_UPDATE_TRIGGER_SQL)
        connection.commit()
    finally:
        connection.close()

    assert database.readiness() == (
        False,
        "invalid_capability_state:unknown_decision_route",
    )


def test_schema_ten_migration_failure_rolls_back_to_intact_schema_nine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "schema-ten-rollback.sqlite3"
    _create_schema_nine_database(path)
    with Database(path).connect() as connection:
        connection.execute(
            """
            INSERT INTO platform_capabilities(
                id, platform, source_type, job_kind, adapter,
                adapter_version, environment, status
            ) VALUES (
                'legacy-row', 'x', 'x_post', 'download', 'yt_dlp',
                '2026.08.19', 'windows', 'verified'
            )
            """
        )
    failing = database_module.MIGRATION_10_SQL.replace(
        "INSERT INTO schema_migrations(version, applied_at)",
        "SELECT missing_column FROM missing_table;\n"
        "INSERT INTO schema_migrations(version, applied_at)",
    )
    monkeypatch.setattr(database_module, "MIGRATION_10_SQL", failing)

    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        Database(path).initialize()

    connection = sqlite3.connect(path)
    try:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        objects = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master"
            ).fetchall()
        }
        row = connection.execute(
            "SELECT id, status FROM platform_capabilities"
        ).fetchone()
    finally:
        connection.close()

    assert version == 9
    assert ("table", "platform_capabilities") in objects
    assert ("table", "capability_legacy_schema9") not in objects
    assert ("table", "capability_evidence") not in objects
    assert tuple(row) == ("legacy-row", "verified")


def test_concurrent_schema_nine_upgrade_is_safe_across_processes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "multiprocess-schema-nine.sqlite3"
    _create_schema_nine_database(path)
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
    assert [results.get(timeout=5) for _ in processes] == [(True, "ok")] * 3
    with Database(path).connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in versions] == list(
        range(1, SCHEMA_VERSION + 1)
    )


def test_concurrent_fresh_initialization_remains_safe(tmp_path: Path) -> None:
    path = tmp_path / "threaded-fresh.sqlite3"

    def initialize(_: int) -> tuple[bool, str]:
        database = Database(path)
        database.initialize()
        return database.readiness()

    with ThreadPoolExecutor(max_workers=6) as executor:
        assert list(executor.map(initialize, range(6))) == [(True, "ok")] * 6


def test_database_connections_enable_recursive_triggers(tmp_path: Path) -> None:
    database = Database(tmp_path / "recursive-triggers.sqlite3")
    database.initialize()

    with database.connect() as connection:
        recursive = connection.execute("PRAGMA recursive_triggers").fetchone()[0]

    assert recursive == 1


def test_insert_or_replace_cannot_overwrite_evidence_or_decisions(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "replace-guard.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(connection, evidence_id="evidence-original")
        _insert_decision(
            connection,
            decision_id="decision-original",
            evidence_id="evidence-original",
            action="approve",
            status="verified",
            supersedes_decision_id=None,
            revision=1,
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO capability_evidence
                SELECT * FROM capability_evidence
                WHERE evidence_id = 'evidence-original'
                """
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO capability_decisions
                SELECT * FROM capability_decisions
                WHERE decision_id = 'decision-original'
                """
            )

    with database.connect() as connection:
        evidence = connection.execute(
            "SELECT evidence_id, verdict FROM capability_evidence"
        ).fetchall()
        decisions = connection.execute(
            "SELECT decision_id, action, revision FROM capability_decisions"
        ).fetchall()
    assert [tuple(row) for row in evidence] == [
        ("evidence-original", "verified")
    ]
    assert [tuple(row) for row in decisions] == [
        ("decision-original", "approve", 1)
    ]
    assert database.readiness() == (True, "ok")


@pytest.mark.parametrize(
    "trigger_name",
    (
        "trg_capability_evidence_no_replace",
        "trg_capability_decisions_no_replace",
    ),
)
def test_readiness_requires_insert_or_replace_guards(
    tmp_path: Path,
    trigger_name: str,
) -> None:
    database = Database(tmp_path / f"missing-{trigger_name}.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute(f"DROP TRIGGER {trigger_name}")

    assert database.readiness() == (False, f"missing_triggers:{trigger_name}")


def test_readiness_detects_a_forged_root_revoke(tmp_path: Path) -> None:
    database = Database(tmp_path / "forged-root-revoke.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(connection, evidence_id="revoke-evidence")
        connection.execute(
            "DROP TRIGGER trg_capability_decisions_validate_transition"
        )
        _insert_decision(
            connection,
            decision_id="forged-root-revoke",
            evidence_id="revoke-evidence",
            action="revoke",
            status="candidate",
            supersedes_decision_id=None,
            revision=1,
        )
        connection.executescript(_DECISION_TRANSITION_TRIGGER_SQL)

    assert database.readiness() == (
        False,
        "invalid_capability_state:invalid_transition",
    )


def test_readiness_keeps_disabled_routes_as_historical_tombstones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "disabled-tombstone.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_evidence(connection, evidence_id="historical-evidence")

    disabled_registry = PlatformCapabilityRegistry(
        tuple(
            replace(item, status=CapabilityStatus.DISABLED)
            if item.route.platform.value == "youtube"
            and item.route.source_type.value == "youtube_video"
            else item
            for item in capabilities_module.DEFAULT_DOWNLOAD_CAPABILITIES.list()
        )
    )
    monkeypatch.setattr(
        capabilities_module,
        "DEFAULT_DOWNLOAD_CAPABILITIES",
        disabled_registry,
    )

    assert database.readiness() == (True, "ok")

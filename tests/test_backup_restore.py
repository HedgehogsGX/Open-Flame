from __future__ import annotations

import hashlib
import csv
import io
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from video_download_control.adapters.fake import (
    ScriptedFakeAdapter,
    ScriptedGraphFakeAdapter,
)
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.backup import (
    BACKUP_MANIFEST_HASH_NAME,
    BACKUP_MANIFEST_NAME,
    BACKUP_METADATA_NAME,
    BackupRestoreError,
    create_backup,
    restore_backup,
)
from video_download_control.build_identity import current_product_identity
from video_download_control.backup_cli import main as backup_cli_main
from video_download_control.capability_evidence import CapabilityEvidenceRepository
from video_download_control.database import SCHEMA_VERSION, Database
from video_download_control.graph import XAttachmentProbeItem
from video_download_control.service import BatchService
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def populate_ready_asset(service, settings, database) -> tuple[str, Path]:
    batch = service.create_batch(
        name="backup restore drill",
        raw_inputs=["https://www.youtube.com/watch?v=backup-restore"],
    )
    worker = Worker(
        worker_id="backup-test-worker",
        repository=WorkerRepository(database),
        adapter=ScriptedFakeAdapter(),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
        clock=lambda: NOW,
    )
    result = worker.run_once()
    assert result and result.status == "ready"
    with database.connect() as connection:
        row = connection.execute(
            "SELECT id FROM media_assets"
        ).fetchone()
    assert row is not None
    asset_id = row["id"]
    original = next(
        (settings.data_root / "assets" / asset_id / "original").iterdir()
    )
    return batch["id"], original


def populate_graph_backup_state(repository, settings, database) -> dict[str, str]:
    selector = "selector-private-backup-value"
    expected_media_key = "target-private-backup-value"
    adapter = ScriptedGraphFakeAdapter(
        (
            XAttachmentProbeItem(
                stable_key="stable-private-backup-value",
                selector_key=selector,
                expected_media_key=expected_media_key,
                media_kind="video",
            ),
        )
    )
    service = BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version="graph-backup-v2",
        x_graph_v2_enabled=True,
    )
    clock = [NOW]
    worker_repository = WorkerRepository(database)
    worker = Worker(
        worker_id="graph-backup-worker",
        repository=worker_repository,
        adapter=adapter,
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
        clock=lambda: clock[0],
    )

    first = service.create_batch(
        name="graph backup donor",
        raw_inputs=["https://x.com/example/status/940001"],
    )
    assert worker.run_once().status == "ready"
    assert worker.run_once().status == "ready"

    second = service.create_batch(
        name="graph backup reuse",
        raw_inputs=["https://x.com/example/status/940001"],
    )
    clock[0] = NOW + timedelta(seconds=1)
    assert worker.run_once().status == "ready"
    second_input_id = second["inputs"][0]["id"]
    second_parent_id = second["jobs"][0]["id"]
    assert worker_repository.request_rediscover(
        second_input_id,
        now=NOW + timedelta(seconds=2),
    ) == 2
    clock[0] = NOW + timedelta(seconds=3)
    assert worker.run_once().status == "ready"

    with database.connect() as connection:
        donor_job_id = connection.execute(
            """
            SELECT id FROM download_jobs
            WHERE input_record_id = ? AND job_kind = 'download'
            """,
            (first["inputs"][0]["id"],),
        ).fetchone()["id"]
        reused_job = connection.execute(
            """
            SELECT id, reused_from_job_id FROM download_jobs
            WHERE input_record_id = ? AND job_kind = 'download'
            """,
            (second_input_id,),
        ).fetchone()
        active = connection.execute(
            """
            SELECT active_discovery_id, active_run_generation
            FROM input_records WHERE id = ?
            """,
            (second_input_id,),
        ).fetchone()
    assert reused_job["reused_from_job_id"] == donor_job_id
    assert active["active_discovery_id"] is not None
    assert active["active_run_generation"] == 2
    return {
        "selector": selector,
        "expected_media_key": expected_media_key,
        "input_id": second_input_id,
        "parent_job_id": second_parent_id,
        "donor_job_id": donor_job_id,
        "reused_job_id": reused_job["id"],
    }


def graph_database_snapshot(database_path: Path) -> dict[str, list[tuple]]:
    queries = {
        "source_discoveries": "SELECT * FROM source_discoveries ORDER BY id",
        "source_relations": "SELECT * FROM source_relations ORDER BY id",
        "input_relation_jobs": (
            "SELECT * FROM input_relation_jobs "
            "ORDER BY input_record_id, run_generation, relation_id"
        ),
        "download_job_targets": (
            "SELECT * FROM download_job_targets ORDER BY job_id"
        ),
        "input_graph_fields": (
            "SELECT id, expected_item_count, active_discovery_id, "
            "active_run_generation FROM input_records ORDER BY id"
        ),
        "job_graph_fields": (
            "SELECT id, job_kind, run_generation, generation_attempt_count, "
            "reused_from_job_id FROM download_jobs ORDER BY id"
        ),
        "attempt_graph_fields": (
            "SELECT id, job_id, attempt_no, run_generation, "
            "generation_attempt_no FROM job_attempts ORDER BY id"
        ),
    }
    connection = sqlite3.connect(database_path)
    try:
        return {
            name: [tuple(row) for row in connection.execute(query).fetchall()]
            for name, query in queries.items()
        }
    finally:
        connection.close()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def populate_capability_decision(database: Database) -> tuple[str, str]:
    manifest_rows = [
        {
            "sample_id": f"backup-positive-{index}",
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "url": f"https://www.youtube.com/watch?v=backup{index}",
            "expected_outcome": "ready",
            "expected_output_count": 1,
            "requires_cookie": "false",
            "region": "test-region",
        }
        for index in range(10)
    ]
    manifest_rows.append(
        {
            "sample_id": "backup-negative",
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "url": "https://www.youtube.com/watch?v=backup-unavailable",
            "expected_outcome": "content_unavailable",
            "expected_output_count": 0,
            "requires_cookie": "false",
            "region": "test-region",
        }
    )
    result_rows = [
        {
            "run_id": f"backup-run-{run}",
            "sample_id": sample["sample_id"],
            "job_kind": "download",
            "observed_outcome": sample["expected_outcome"],
            "observed_output_count": sample["expected_output_count"],
            "completed_at": f"2026-09-0{run}T00:00:00Z",
            "adapter": "yt_dlp",
            "downloader_version": "test-1",
            "environment": "test-environment",
            "product_version": current_product_identity(),
        }
        for run in range(1, 4)
        for sample in manifest_rows
    ]

    def encoded(fields: list[str], rows: list[dict[str, object]]) -> bytes:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode("utf-8")

    manifest = encoded(list(manifest_rows[0]), manifest_rows)
    results = encoded(list(result_rows[0]), result_rows)
    repository = CapabilityEvidenceRepository(database)
    imported = repository.import_csv_bundle(
        manifest_content=manifest,
        results_content=results,
        environment="test-environment",
    )
    evidence_id = imported.evidence_ids[0]
    approved = repository.approve(
        evidence_id=evidence_id,
        expected_revision=0,
        reason_code="stage0-reviewed",
    )
    revoked = repository.revoke(
        evidence_id=evidence_id,
        expected_revision=1,
        reason_code="regression",
    )
    assert revoked.revision == 2
    return evidence_id, approved.identity_key


def rewrite_manifest_and_hash(backup_root: Path, manifest: dict) -> None:
    manifest_path = backup_root / BACKUP_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (backup_root / BACKUP_MANIFEST_HASH_NAME).write_text(
        sha256(manifest_path) + "\n",
        encoding="ascii",
        newline="\n",
    )


def update_payload_entry(backup_root: Path, relative_path: str) -> None:
    manifest_path = backup_root / BACKUP_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text("utf-8"))
    payload = backup_root.joinpath(*relative_path.split("/"))
    entry = next(
        item for item in manifest["entries"] if item["path"] == relative_path
    )
    entry["size_bytes"] = payload.stat().st_size
    entry["sha256"] = sha256(payload)
    rewrite_manifest_and_hash(backup_root, manifest)


def test_backup_and_restore_to_independent_root_drills_database_and_assets(
    service, settings, database, tmp_path: Path
) -> None:
    batch_id, source_original = populate_ready_asset(
        service, settings, database
    )
    source_hash = sha256(source_original)
    extra = settings.data_root / "operator-note.txt"
    extra.write_text("non-secret managed note", encoding="utf-8")
    evidence_id, identity_key = populate_capability_decision(database)
    backup_root = tmp_path / "backup-001"

    backup = create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )

    assert backup.backup_root == backup_root
    assert backup.schema_version == SCHEMA_VERSION
    assert backup.file_count >= 4
    assert len(backup.manifest_sha256) == 64
    assert source_original.is_file()
    assert sha256(source_original) == source_hash
    assert not (backup_root / "payload" / "data" / "temporary").exists()
    assert not (
        backup_root / "payload" / "data" / "assets" / ".staging"
    ).exists()

    restore_root = tmp_path / "restore-drill"
    restore_database = restore_root / "database" / "restored.sqlite3"
    restored = restore_backup(
        backup_root=backup_root,
        restore_data_root=restore_root,
        restore_database_path=restore_database,
    )

    assert restored.restore_root == restore_root
    assert restored.database_path == restore_database
    assert restored.schema_version == SCHEMA_VERSION
    assert Database(restore_database).readiness() == (True, "ok")
    assert (restore_root / "operator-note.txt").read_text("utf-8") == (
        "non-secret managed note"
    )
    with Database(restore_database).connect() as connection:
        restored_batch = connection.execute(
            "SELECT status FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        artifact = connection.execute(
            "SELECT path, sha256 FROM artifacts WHERE kind = 'original'"
        ).fetchone()
        capability = connection.execute(
            """
            SELECT evidence_id, identity_key, job_kind, status, revision
            FROM platform_capabilities
            """
        ).fetchone()
        decision_count = connection.execute(
            "SELECT COUNT(*) AS count FROM capability_decisions"
        ).fetchone()["count"]
    assert restored_batch["status"] == "ready"
    assert dict(capability) == {
        "evidence_id": evidence_id,
        "identity_key": identity_key,
        "job_kind": "download",
        "status": "candidate",
        "revision": 2,
    }
    assert decision_count == 2
    restored_original = restore_root.joinpath(
        *artifact["path"].split("/")
    )
    assert restored_original.is_file()
    assert sha256(restored_original) == source_hash == artifact["sha256"]


def test_backup_excludes_only_top_level_runtime_logs_and_records_boundary(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    runtime_logs = settings.data_root / "logs"
    runtime_logs.mkdir()
    (runtime_logs / "runtime-control.jsonl").write_text(
        '{"event":"private-runtime-detail"}\n', encoding="utf-8"
    )
    nested_logs = settings.data_root / "operator-notes" / "logs"
    nested_logs.mkdir(parents=True)
    (nested_logs / "keep.txt").write_text(
        "nested managed file", encoding="utf-8"
    )
    backup_root = tmp_path / "logs-exclusion-backup"

    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )

    assert not (backup_root / "payload" / "data" / "logs").exists()
    assert (
        backup_root / "payload" / "data" / "operator-notes" / "logs" / "keep.txt"
    ).read_text("utf-8") == "nested managed file"
    metadata = json.loads(
        (backup_root / BACKUP_METADATA_NAME).read_text("utf-8")
    )
    assert metadata["volatile_paths_excluded"] == [
        "logs",
        "temporary",
        "assets/.staging",
    ]


def test_backup_rejects_top_level_logs_when_it_is_not_a_directory(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    (settings.data_root / "logs").write_text("not a directory", encoding="utf-8")

    with pytest.raises(BackupRestoreError, match="volatile path has an invalid type"):
        create_backup(
            source_data_root=settings.data_root,
            source_database_path=settings.database_path,
            backup_target=tmp_path / "invalid-logs-backup",
        )


def test_cli_executes_create_and_restore_with_explicit_absolute_paths(
    service, settings, database, tmp_path: Path, capsys
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "cli-backup"
    assert backup_cli_main(
        [
            "create",
            "--source-data-root",
            str(settings.data_root),
            "--source-database",
            str(settings.database_path),
            "--backup-target",
            str(backup_root),
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "ok"
    assert created["operation"] == "create"

    restore_root = tmp_path / "cli-restore"
    restore_database = restore_root / "control.sqlite3"
    assert backup_cli_main(
        [
            "restore",
            "--backup-root",
            str(backup_root),
            "--restore-data-root",
            str(restore_root),
            "--restore-database",
            str(restore_database),
        ]
    ) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["status"] == "ok"
    assert restored["operation"] == "restore"
    assert Database(restore_database).readiness() == (True, "ok")


def test_restore_rejects_payload_tamper_and_leaves_target_absent(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "tamper-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    original = next(
        (backup_root / "payload" / "data" / "assets").glob(
            "*/original/*"
        )
    )
    payload = bytearray(original.read_bytes())
    payload[0] ^= 0x01
    original.write_bytes(payload)
    restore_root = tmp_path / "tampered-restore"

    with pytest.raises(BackupRestoreError, match="hash mismatch"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=restore_root / "control.sqlite3",
        )

    assert not restore_root.exists()


def test_restore_rejects_manifest_tamper_before_copy(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "manifest-tamper-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    manifest_path = backup_root / BACKUP_MANIFEST_NAME
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    restore_root = tmp_path / "manifest-tamper-restore"

    with pytest.raises(BackupRestoreError, match="manifest hash mismatch"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=restore_root / "control.sqlite3",
        )
    assert not restore_root.exists()


def test_restore_rejects_manifest_path_traversal_even_with_rehashed_manifest(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "traversal-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    manifest = json.loads(
        (backup_root / BACKUP_MANIFEST_NAME).read_text("utf-8")
    )
    manifest["entries"].append(
        {
            "path": "payload/data/../../escaped.txt",
            "size_bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    )
    rewrite_manifest_and_hash(backup_root, manifest)
    restore_root = tmp_path / "traversal-restore"

    with pytest.raises(BackupRestoreError, match="escapes"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=restore_root / "control.sqlite3",
        )
    assert not restore_root.exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_restore_inner_audit_rejects_tampered_asset_manifest_after_outer_rehash(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "inner-audit-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    asset_manifest = next(
        (backup_root / "payload" / "data" / "assets").glob(
            "*/metadata/manifest.json"
        )
    )
    value = json.loads(asset_manifest.read_text("utf-8"))
    value["original"]["sha256"] = "0" * 64
    asset_manifest.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    relative = asset_manifest.relative_to(backup_root).as_posix()
    update_payload_entry(backup_root, relative)
    backup_database = backup_root / "payload" / "database" / "control.sqlite3"
    connection = sqlite3.connect(backup_database)
    try:
        connection.execute(
            "UPDATE artifacts SET sha256 = ? WHERE kind = 'manifest'",
            (sha256(asset_manifest),),
        )
        connection.commit()
    finally:
        connection.close()
    update_payload_entry(
        backup_root,
        backup_database.relative_to(backup_root).as_posix(),
    )
    restore_root = tmp_path / "inner-audit-restore"

    with pytest.raises(BackupRestoreError, match="manifest original mismatch"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=restore_root / "control.sqlite3",
        )
    assert not restore_root.exists()


def test_backup_rejects_managed_symlink_without_following_it(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be copied", encoding="utf-8")
    linked = settings.data_root / "assets" / "linked-secret"
    try:
        os.symlink(outside, linked)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")
    backup_root = tmp_path / "link-backup"

    with pytest.raises(BackupRestoreError, match="link or reparse"):
        create_backup(
            source_data_root=settings.data_root,
            source_database_path=settings.database_path,
            backup_target=backup_root,
        )
    assert not backup_root.exists()
    assert outside.read_text("utf-8") == "must not be copied"


def test_restore_rejects_symlinked_backup_payload(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "linked-payload-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    payload = next(
        (backup_root / "payload" / "data" / "assets").glob(
            "*/original/*"
        )
    )
    outside = tmp_path / "replacement.bin"
    outside.write_bytes(payload.read_bytes())
    payload.unlink()
    try:
        os.symlink(outside, payload)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")
    restore_root = tmp_path / "linked-payload-restore"

    with pytest.raises(BackupRestoreError, match="link or reparse"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=restore_root / "control.sqlite3",
        )
    assert not restore_root.exists()


def test_backup_and_restore_reject_existing_targets_and_overlap(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    existing_backup = tmp_path / "existing-backup"
    existing_backup.mkdir()
    with pytest.raises(BackupRestoreError, match="already exists"):
        create_backup(
            source_data_root=settings.data_root,
            source_database_path=settings.database_path,
            backup_target=existing_backup,
        )

    overlapping = settings.data_root / "nested-backup"
    with pytest.raises(BackupRestoreError, match="overlaps"):
        create_backup(
            source_data_root=settings.data_root,
            source_database_path=settings.database_path,
            backup_target=overlapping,
        )

    backup_root = tmp_path / "valid-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    existing_restore = tmp_path / "existing-restore"
    existing_restore.mkdir()
    with pytest.raises(BackupRestoreError, match="already exists"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=existing_restore,
            restore_database_path=existing_restore / "control.sqlite3",
        )
    nested_restore = backup_root / "nested-restore"
    with pytest.raises(BackupRestoreError, match="overlaps"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=nested_restore,
            restore_database_path=nested_restore / "control.sqlite3",
        )


def test_absolute_path_and_restore_database_boundaries_are_fail_closed(
    service, settings, database, tmp_path: Path, capsys
) -> None:
    populate_ready_asset(service, settings, database)
    secret_component = "operator-secret-location"
    relative_target = Path(secret_component) / "backup"
    exit_code = backup_cli_main(
        [
            "create",
            "--source-data-root",
            str(settings.data_root),
            "--source-database",
            str(settings.database_path),
            "--backup-target",
            str(relative_target),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "explicit absolute path" in captured.err
    assert secret_component not in captured.err

    filesystem_root = Path(settings.data_root.anchor)
    with pytest.raises(BackupRestoreError, match="filesystem root"):
        create_backup(
            source_data_root=settings.data_root,
            source_database_path=settings.database_path,
            backup_target=filesystem_root,
        )

    backup_root = tmp_path / "boundary-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    restore_root = tmp_path / "boundary-restore"
    with pytest.raises(BackupRestoreError, match="inside the restore target"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=tmp_path / "outside.sqlite3",
        )


def test_restore_rejects_schema_tamper_after_outer_manifest_is_rehashed(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "schema-tamper-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    backup_database = backup_root / "payload" / "database" / "control.sqlite3"
    connection = sqlite3.connect(backup_database)
    try:
        connection.execute(
            "UPDATE schema_migrations SET version = 999 WHERE version = ?",
            (SCHEMA_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()
    update_payload_entry(
        backup_root,
        backup_database.relative_to(backup_root).as_posix(),
    )
    restore_root = tmp_path / "schema-tamper-restore"

    with pytest.raises(BackupRestoreError, match="schema mismatch"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=restore_root / "control.sqlite3",
        )
    assert not restore_root.exists()


def test_backup_refuses_unreconciled_commit_intent(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    with database.connect() as connection:
        job = connection.execute(
            "SELECT id FROM download_jobs LIMIT 1"
        ).fetchone()["id"]
        attempt = connection.execute(
            "SELECT id, lease_token FROM job_attempts LIMIT 1"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO asset_commit_intents(
                asset_id, job_id, attempt_id, lease_token, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                job,
                attempt["id"],
                attempt["lease_token"],
                "2026-09-03T08:00:00.000Z",
            ),
        )
    backup_root = tmp_path / "pending-intent-backup"

    with pytest.raises(BackupRestoreError, match="pending asset commit intents"):
        create_backup(
            source_data_root=settings.data_root,
            source_database_path=settings.database_path,
            backup_target=backup_root,
        )
    assert not backup_root.exists()


def test_restore_rejects_untracked_backup_file(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "untracked-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    (backup_root / "unexpected.txt").write_text("smuggled", encoding="utf-8")
    restore_root = tmp_path / "untracked-restore"

    with pytest.raises(BackupRestoreError, match="untracked"):
        restore_backup(
            backup_root=backup_root,
            restore_data_root=restore_root,
            restore_database_path=restore_root / "control.sqlite3",
        )
    assert not restore_root.exists()


def test_backup_metadata_is_covered_by_manifest(
    service, settings, database, tmp_path: Path
) -> None:
    populate_ready_asset(service, settings, database)
    backup_root = tmp_path / "metadata-backup"
    result = create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )
    manifest = json.loads(
        (backup_root / BACKUP_MANIFEST_NAME).read_text("utf-8")
    )
    metadata_entry = next(
        entry
        for entry in manifest["entries"]
        if entry["path"] == BACKUP_METADATA_NAME
    )
    metadata_path = backup_root / BACKUP_METADATA_NAME
    assert metadata_entry["sha256"] == sha256(metadata_path)
    assert metadata_entry["size_bytes"] == metadata_path.stat().st_size
    assert (
        backup_root / BACKUP_MANIFEST_HASH_NAME
    ).read_text("ascii").strip() == result.manifest_sha256


def test_graph_v2_backup_restore_preserves_graph_state_and_keeps_targets_private(
    repository,
    settings,
    database,
    tmp_path: Path,
    caplog,
) -> None:
    private = populate_graph_backup_state(repository, settings, database)
    source_snapshot = graph_database_snapshot(settings.database_path)
    backup_root = tmp_path / "graph-backup"

    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )

    public_control_text = "\n".join(
        (backup_root / name).read_text("utf-8")
        for name in (
            BACKUP_METADATA_NAME,
            BACKUP_MANIFEST_NAME,
            BACKUP_MANIFEST_HASH_NAME,
        )
    )
    for internal_value in (
        private["selector"],
        private["expected_media_key"],
        "selector_key",
        "expected_media_key",
    ):
        assert internal_value not in public_control_text
        assert internal_value not in caplog.text

    restore_root = tmp_path / "graph-restore"
    restore_database = restore_root / "control.sqlite3"
    restore_backup(
        backup_root=backup_root,
        restore_data_root=restore_root,
        restore_database_path=restore_database,
    )

    assert graph_database_snapshot(restore_database) == source_snapshot
    connection = sqlite3.connect(restore_database)
    connection.row_factory = sqlite3.Row
    try:
        foreign_keys = {
            table: {
                (row["from"], row["table"], row["to"])
                for row in connection.execute(
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall()
            }
            for table in (
                "source_discoveries",
                "source_relations",
                "input_records",
                "input_relation_jobs",
                "download_jobs",
                "download_job_targets",
            )
        }
    finally:
        connection.close()
    assert (
        "active_discovery_id",
        "source_discoveries",
        "id",
    ) in foreign_keys["input_records"]
    assert (
        "reused_from_job_id",
        "download_jobs",
        "id",
    ) in foreign_keys["download_jobs"]
    assert {
        ("discovery_id", "source_discoveries", "id"),
        ("job_id", "download_jobs", "id"),
    } <= foreign_keys["input_relation_jobs"]
    assert {
        ("job_id", "download_jobs", "id"),
        ("fetch_source_item_id", "source_items", "id"),
    } <= foreign_keys["download_job_targets"]


def test_restore_rejects_rehashed_graph_semantic_tampering_without_echoing_targets(
    repository,
    settings,
    database,
    tmp_path: Path,
) -> None:
    private = populate_graph_backup_state(repository, settings, database)
    cases = (
        (
            "active-discovery",
            "UPDATE input_records SET active_discovery_id = NULL WHERE id = ?",
            (private["input_id"],),
        ),
        (
            "run-generation",
            """
            UPDATE input_records
            SET active_run_generation = active_run_generation + 1
            WHERE id = ?
            """,
            (private["input_id"],),
        ),
        (
            "target",
            """
            UPDATE download_job_targets
            SET selector_key = 'tampered-private-selector-value'
            WHERE job_id = ?
            """,
            (private["reused_job_id"],),
        ),
        (
            "target-asset",
            """
            UPDATE download_job_targets
            SET expected_media_key = 'tampered-private-media-key'
            WHERE job_id IN (?, ?)
            """,
            (private["donor_job_id"], private["reused_job_id"]),
        ),
        (
            "child-identity",
            """
            UPDATE source_items
            SET canonical_url =
                'https://x.com/i/status/940001#vdc-media=tampered-stable-key'
            WHERE id = (
                SELECT source_item_id FROM download_jobs WHERE id = ?
            )
            """,
            (private["reused_job_id"],),
        ),
        (
            "reuse",
            "UPDATE download_jobs SET reused_from_job_id = ? WHERE id = ?",
            (private["parent_job_id"], private["reused_job_id"]),
        ),
    )

    for name, statement, parameters in cases:
        backup_root = tmp_path / f"graph-tamper-{name}"
        create_backup(
            source_data_root=settings.data_root,
            source_database_path=settings.database_path,
            backup_target=backup_root,
        )
        payload_database = (
            backup_root / "payload" / "database" / "control.sqlite3"
        )
        connection = sqlite3.connect(payload_database)
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()
        update_payload_entry(
            backup_root,
            payload_database.relative_to(backup_root).as_posix(),
        )
        restore_root = tmp_path / f"graph-tamper-restore-{name}"

        with pytest.raises(
            BackupRestoreError, match="graph state is invalid"
        ) as caught:
            restore_backup(
                backup_root=backup_root,
                restore_data_root=restore_root,
                restore_database_path=restore_root / "control.sqlite3",
            )

        assert private["selector"] not in str(caught.value)
        assert private["expected_media_key"] not in str(caught.value)
        assert "tampered-private-selector-value" not in str(caught.value)
        assert "tampered-private-media-key" not in str(caught.value)
        assert "tampered-stable-key" not in str(caught.value)
        assert not restore_root.exists()

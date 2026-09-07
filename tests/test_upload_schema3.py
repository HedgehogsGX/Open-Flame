from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from video_download_control.uploads import backup as upload_backup
from video_download_control.uploads import schema
from video_download_control.uploads.schema import (
    LEGACY_SCHEMA_DDL,
    SCHEMA_DDL,
    SCHEMA_V2_DDL,
    SCHEMA_VERSION,
    UploadSchemaError,
    ensure_upload_schema,
    validate_upload_schema,
)
from video_download_control.uploads.contracts import UploadError
from video_download_control.uploads.service import UploadService


ACCOUNT_ID = "a" * 32
SOURCE_ID = "b" * 32
JOB_ID = "c" * 32
ASSET_ID = "d" * 32
REQUEST_ID = "request-key"


def _database_files(path: Path) -> dict[str, bytes]:
    paths = [
        path,
        *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm")),
    ]
    return {
        candidate.name: candidate.read_bytes()
        for candidate in paths
        if os.path.lexists(candidate)
    }


def _insert_v1_rows(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO accounts(id,platform,name,auth_state,code,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (ACCOUNT_ID, "douyin", "account", "ready", "account_ready", "created"),
    )
    db.execute(
        "INSERT INTO sources(id,name,suffix,size,sha256,created_at) VALUES(?,?,?,?,?,?)",
        (SOURCE_ID, "video.mp4", ".mp4", 5, "1" * 64, "created"),
    )
    db.execute(
        "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
        "mode,copyright,source_credit,state,code,created_at,updated_at,retry_of) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            JOB_ID,
            ACCOUNT_ID,
            SOURCE_ID,
            "title",
            "description",
            '["tag"]',
            None,
            "publish",
            1,
            "",
            "draft",
            "",
            "created",
            "updated",
            None,
        ),
    )
    db.execute(
        "INSERT INTO requests(id,digest,job_ids) VALUES(?,?,?)",
        (REQUEST_ID, "2" * 64, f'["{JOB_ID}"]'),
    )


def _create_v1(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA_DDL)
        db.execute("INSERT INTO metadata VALUES(1)")
        _insert_v1_rows(db)


def _create_v2(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA_V2_DDL)
        db.execute("INSERT INTO metadata VALUES(2)")
        _insert_v1_rows(db)


def test_schema3_fresh_database_has_exact_assets_jobs_requests_and_indexes(tmp_path):
    path = tmp_path / "uploads.sqlite3"

    ensure_upload_schema(path)
    validate_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert SCHEMA_VERSION == 3
        assert db.execute("SELECT version FROM metadata").fetchone() == (3,)
        assert {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        } == {
            "metadata",
            "accounts",
            "sources",
            "upload_assets",
            "jobs",
            "operations",
            "requests",
        }
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(upload_assets)")] == [
            "id",
            "kind",
            "name",
            "suffix",
            "mime_type",
            "size",
            "sha256",
            "width",
            "height",
            "created_at",
            "media_state",
            "deleted_at",
        ]
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(jobs)")][-5:] == [
            "cover_landscape_asset_id",
            "cover_portrait_asset_id",
            "publish_at_unix",
            "publish_timezone_offset_minutes",
            "platform_options",
        ]
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(requests)")][-1] == (
            "digest_version"
        )
        assert {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"
            )
        } == {
            "accounts_active_name",
            "jobs_account_state",
            "jobs_source_state",
            "operations_account_state",
            "jobs_cover_landscape_state",
            "jobs_cover_portrait_state",
        }


def test_v1_to_v2_step_keeps_its_frozen_version_marker():
    assert schema._MIGRATION_V1_TO_V2[-1] == "UPDATE metadata SET version=2"


@pytest.mark.parametrize("starting_version", [1, 2])
def test_exact_old_schema_migrates_to_schema3_with_safe_defaults(tmp_path, starting_version):
    path = tmp_path / "uploads.sqlite3"
    (_create_v1 if starting_version == 1 else _create_v2)(path)

    ensure_upload_schema(path)
    validate_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM metadata").fetchone() == (3,)
        assert db.execute("SELECT COUNT(*) FROM upload_assets").fetchone() == (0,)
        assert db.execute(
            "SELECT cover_landscape_asset_id,cover_portrait_asset_id,publish_at_unix,"
            "publish_timezone_offset_minutes,platform_options FROM jobs"
        ).fetchone() == (
            None,
            None,
            None,
            None,
            '{"declaration":"内容由AI生成"}',
        )
        assert db.execute(
            "SELECT digest,digest_version FROM requests"
        ).fetchone() == ("2" * 64, 1)
        assert list(db.execute("PRAGMA foreign_key_check")) == []

    before = _database_files(path)
    ensure_upload_schema(path)
    assert _database_files(path) == before


def test_schema2_legacy_tags_are_normalized_revoked_and_replay_compatible(tmp_path):
    root = tmp_path / "legacy-tags"
    root.mkdir()
    path = root / "uploads.sqlite3"
    _create_v2(path)
    old_tags = ["#topic", "旅＃行", "中文，标签", "#"]
    request_payload = [
        SOURCE_ID, [ACCOUNT_ID], "title", "description", old_tags,
        None, "publish", None, "",
    ]
    old_digest = schema._legacy_request_digest(request_payload)
    assert old_digest is not None
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE jobs SET tags=?,state='queued',code='' WHERE id=?",
            (json.dumps(old_tags, ensure_ascii=False), JOB_ID),
        )
        db.execute(
            "UPDATE requests SET digest=? WHERE id=?", (old_digest, REQUEST_ID)
        )

    ensure_upload_schema(path)

    normalized = ["topic", "旅井行", "中文、标签", "井"]
    expected_digest = schema._legacy_request_digest([
        SOURCE_ID, [ACCOUNT_ID], "title", "description", normalized,
        None, "publish", None, "",
    ])
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT tags,state,code FROM jobs WHERE id=?", (JOB_ID,)
        ).fetchone() == (
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
            "draft",
            "legacy_metadata_restart_confirmation_required",
        )
        assert db.execute(
            "SELECT digest,digest_version FROM requests WHERE id=?", (REQUEST_ID,)
        ).fetchone() == (expected_digest, 1)

    service = UploadService(root, object())
    replay = service.create_jobs(
        source_id=SOURCE_ID,
        account_ids=[ACCOUNT_ID],
        title="title",
        description="description",
        tags=old_tags,
        category_id=None,
        mode="publish",
        copyright=None,
        source_credit="",
        idempotency_key=REQUEST_ID,
    )
    assert replay[0]["id"] == JOB_ID
    assert replay[0]["tags"] == normalized
    with pytest.raises(UploadError, match="^invalid_tags$"):
        service.create_jobs(
            source_id=SOURCE_ID,
            account_ids=[ACCOUNT_ID],
            title="title",
            description="description",
            tags=old_tags,
            idempotency_key="new-legacy-tags",
        )


@pytest.mark.parametrize(
    ("original_state", "expected_state", "expected_code"),
    [
        ("queued", "draft", "legacy_metadata_restart_confirmation_required"),
        ("running", "unknown", "legacy_metadata_interrupted_result_unknown"),
    ],
)
def test_upload_service_direct_schema2_migration_preserves_recovery_reason(
    tmp_path, original_state, expected_state, expected_code
):
    root = tmp_path / f"direct-schema2-{original_state}"
    root.mkdir()
    path = root / "uploads.sqlite3"
    _create_v2(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE jobs SET state=?,code='' WHERE id=?",
            (original_state, JOB_ID),
        )

    UploadService(root, object())

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT state,code,platform_options FROM jobs WHERE id=?",
            (JOB_ID,),
        ).fetchone() == (
            expected_state,
            expected_code,
            '{"declaration":"内容由AI生成"}',
        )


def test_schema2_tencent_short_title_becomes_reviewable_and_revokes_confirmation(
    tmp_path,
):
    path = tmp_path / "uploads.sqlite3"
    _create_v2(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE accounts SET platform='tencent' WHERE id=?", (ACCOUNT_ID,))
        db.execute(
            "UPDATE jobs SET title=?,state='queued',code='' WHERE id=?",
            ("主文案，精彩内容分享", JOB_ID),
        )

    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT platform_options,state,code FROM jobs WHERE id=?", (JOB_ID,)
        ).fetchone()
    assert row == (
        '{"content_label":"含AI生成内容","short_title":"主文案精彩内容分享"}',
        "draft",
        "legacy_metadata_restart_confirmation_required",
    )


def test_schema2_douyin_default_declaration_becomes_reviewable(tmp_path):
    path = tmp_path / "uploads.sqlite3"
    _create_v2(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET state='queued',code='' WHERE id=?", (JOB_ID,))

    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT platform_options,state,code FROM jobs WHERE id=?", (JOB_ID,)
        ).fetchone() == (
            '{"declaration":"内容由AI生成"}',
            "draft",
            "legacy_metadata_restart_confirmation_required",
        )


def test_schema2_bilibili_original_source_is_removed_and_old_replay_is_idempotent(
    tmp_path,
):
    root = tmp_path / "legacy-bilibili-source"
    root.mkdir()
    path = root / "uploads.sqlite3"
    _create_v2(path)
    old_source_credit = "legacy contradictory source"
    old_payload = [
        SOURCE_ID, [ACCOUNT_ID], "title", "description", ["tag"],
        21, "publish", 1, old_source_credit,
    ]
    old_digest = schema._legacy_request_digest(old_payload)
    assert old_digest is not None
    with sqlite3.connect(path) as db:
        db.execute("UPDATE accounts SET platform='bilibili' WHERE id=?", (ACCOUNT_ID,))
        db.execute(
            "UPDATE jobs SET category_id=21,copyright=1,source_credit=?,"
            "state='queued',code='' WHERE id=?",
            (old_source_credit, JOB_ID),
        )
        db.execute("UPDATE requests SET digest=? WHERE id=?", (old_digest, REQUEST_ID))

    ensure_upload_schema(path)

    expected_digest = schema._legacy_request_digest([
        SOURCE_ID, [ACCOUNT_ID], "title", "description", ["tag"],
        21, "publish", 1, "",
    ])
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT source_credit,state,code FROM jobs WHERE id=?", (JOB_ID,)
        ).fetchone() == (
            "", "draft", "legacy_metadata_restart_confirmation_required",
        )
        assert db.execute(
            "SELECT digest,digest_version FROM requests WHERE id=?", (REQUEST_ID,)
        ).fetchone() == (expected_digest, 1)

    replay = UploadService(root, object()).create_jobs(
        source_id=SOURCE_ID,
        account_ids=[ACCOUNT_ID],
        title="title",
        description="description",
        tags=["tag"],
        category_id=21,
        mode="publish",
        copyright=1,
        source_credit=old_source_credit,
        idempotency_key=REQUEST_ID,
    )
    assert replay[0]["id"] == JOB_ID
    assert replay[0]["source_credit"] == ""


def test_schema2_mixed_original_source_normalizes_non_bilibili_retry_lineage(
    tmp_path,
):
    root = tmp_path / "legacy-mixed-source-retry"
    root.mkdir()
    path = root / "uploads.sqlite3"
    _create_v2(path)
    douyin_account = "e" * 32
    douyin_root = "f" * 32
    douyin_retry = "1" * 32
    old_source_credit = "legacy mixed source"
    timestamp = "2026-09-07T00:00:00+00:00"
    account_ids = sorted([ACCOUNT_ID, douyin_account])
    old_payload = [
        SOURCE_ID, account_ids, "title", "description", ["tag"],
        21, "publish", 1, old_source_credit,
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE accounts SET platform='bilibili',created_at=? WHERE id=?",
            (timestamp, ACCOUNT_ID),
        )
        db.execute("UPDATE sources SET created_at=?", (timestamp,))
        db.execute(
            "INSERT INTO accounts(id,platform,name,auth_state,code,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (douyin_account, "douyin", "douyin account", "ready", "account_ready", timestamp),
        )
        db.execute(
            "UPDATE jobs SET category_id=21,copyright=1,source_credit=?,"
            "state='failed',code='failed',created_at=?,updated_at=? WHERE id=?",
            (old_source_credit, timestamp, timestamp, JOB_ID),
        )
        values = (
            douyin_account, SOURCE_ID, "title", "description", '["tag"]', 21,
            "publish", 1, old_source_credit,
        )
        db.execute(
            "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
            "mode,copyright,source_credit,state,code,created_at,updated_at,retry_of) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (douyin_root, *values, "failed", "failed", timestamp, timestamp, None),
        )
        db.execute(
            "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
            "mode,copyright,source_credit,state,code,created_at,updated_at,retry_of) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (douyin_retry, *values, "queued", "", timestamp, timestamp, douyin_root),
        )
        db.execute(
            "UPDATE requests SET digest=?,job_ids=? WHERE id=?",
            (
                schema._legacy_request_digest(old_payload),
                json.dumps([JOB_ID, douyin_root]),
                REQUEST_ID,
            ),
        )

    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = {
            row["id"]: row
            for row in db.execute(
                "SELECT id,source_credit,state,code,platform_options,retry_of FROM jobs"
            )
        }
        assert {row["source_credit"] for row in rows.values()} == {""}
        assert rows[douyin_retry]["state"] == "draft"
        assert rows[douyin_retry]["code"] == "legacy_metadata_restart_confirmation_required"
        assert rows[douyin_retry]["retry_of"] == douyin_root
        assert rows[douyin_retry]["platform_options"] == rows[douyin_root]["platform_options"]
        upload_backup._audit_database_rows(db, schema_version=SCHEMA_VERSION)

    replay = UploadService(root, object()).create_jobs(
        source_id=SOURCE_ID,
        account_ids=list(reversed(account_ids)),
        title="title",
        description="description",
        tags=["tag"],
        category_id=21,
        mode="publish",
        copyright=1,
        source_credit=old_source_credit,
        idempotency_key=REQUEST_ID,
    )
    assert {job["id"] for job in replay} == {JOB_ID, douyin_root}


def test_asset_checks_and_both_job_cover_foreign_keys_are_enforced(tmp_path):
    path = tmp_path / "uploads.sqlite3"
    _create_v2(path)
    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        asset_values = (
            ASSET_ID,
            "cover",
            "cover.png",
            ".png",
            "image/png",
            123,
            "3" * 64,
            1920,
            1080,
            "created",
        )
        db.execute(
            "INSERT INTO upload_assets(id,kind,name,suffix,mime_type,size,sha256,width,"
            "height,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            asset_values,
        )
        db.execute(
            "UPDATE jobs SET cover_landscape_asset_id=?,cover_portrait_asset_id=? WHERE id=?",
            (ASSET_ID, ASSET_ID, JOB_ID),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE jobs SET cover_landscape_asset_id=? WHERE id=?",
                ("e" * 32, JOB_ID),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO upload_assets(id,kind,name,suffix,mime_type,size,sha256,width,"
                "height,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("f" * 32, "thumbnail", *asset_values[2:]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE upload_assets SET media_state='unknown' WHERE id=?",
                (ASSET_ID,),
            )


@pytest.mark.parametrize("starting_version", [1, 2])
def test_schema3_migration_failure_rolls_back_to_exact_starting_schema(
    tmp_path, monkeypatch, starting_version
):
    path = tmp_path / "uploads.sqlite3"
    (_create_v1 if starting_version == 1 else _create_v2)(path)
    before = _database_files(path)
    migration = schema._MIGRATION_V2_TO_V3
    monkeypatch.setattr(
        schema,
        "_MIGRATION_V2_TO_V3",
        (migration[0], "SELECT value FROM synthetic_schema3_failure"),
    )

    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    assert _database_files(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM metadata").fetchone() == (
            starting_version,
        )
        assert "upload_assets" not in {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }


@pytest.mark.parametrize("damage", ["tags_blob", "job_ids_blob", "job_ids_object"])
def test_schema2_malformed_legacy_json_fails_as_schema_error_without_writes(
    tmp_path, damage
):
    path = tmp_path / f"malformed-{damage}.sqlite3"
    _create_v2(path)
    with sqlite3.connect(path) as db:
        if damage == "tags_blob":
            db.execute(
                "UPDATE jobs SET tags=? WHERE id=?",
                (sqlite3.Binary(b"\xff"), JOB_ID),
            )
        else:
            db.execute(
                "UPDATE jobs SET tags=? WHERE id=?",
                (json.dumps(["#topic"]), JOB_ID),
            )
            job_ids = (
                sqlite3.Binary(b"\xff")
                if damage == "job_ids_blob"
                else json.dumps([{}])
            )
            db.execute(
                "UPDATE requests SET job_ids=? WHERE id=?",
                (job_ids, REQUEST_ID),
            )
    before = _database_files(path)

    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    assert _database_files(path) == before


@pytest.mark.parametrize(
    "job_ids",
    [sqlite3.Binary(b"\xff"), "not-json", json.dumps([{}])],
    ids=["blob", "invalid-json", "non-string-id"],
)
def test_schema2_valid_tags_do_not_bypass_request_job_id_validation(
    tmp_path, job_ids
):
    path = tmp_path / "malformed-request-with-valid-tags.sqlite3"
    _create_v2(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE jobs SET tags=? WHERE id=?",
            (json.dumps(["topic"]), JOB_ID),
        )
        db.execute(
            "UPDATE requests SET job_ids=? WHERE id=?",
            (job_ids, REQUEST_ID),
        )
    before = _database_files(path)

    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    assert _database_files(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM metadata").fetchone() == (2,)
        assert "upload_assets" not in {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ddl: ddl.replace("CHECK(kind='cover')", "CHECK(kind IN ('cover','other'))"),
        lambda ddl: ddl.replace(
            "cover_landscape_asset_id TEXT REFERENCES upload_assets(id)",
            "cover_landscape_asset_id TEXT REFERENCES sources(id)",
        ),
        lambda ddl: ddl.replace(
            "CREATE INDEX jobs_cover_landscape_state ON jobs(cover_landscape_asset_id,state);",
            "",
        ),
    ],
)
def test_schema3_exact_validation_rejects_changed_ddl_fk_or_index_without_writes(
    tmp_path, mutate
):
    path = tmp_path / "uploads.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(mutate(SCHEMA_DDL))
        db.execute("INSERT INTO metadata VALUES(3)")
    before = _database_files(path)

    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    assert _database_files(path) == before


def test_schema2_is_validated_exactly_before_migration(tmp_path):
    path = tmp_path / "uploads.sqlite3"
    changed_v2 = SCHEMA_V2_DDL.replace(
        "'present','missing','deleted'",
        "'PRESENT','MISSING','DELETED'",
    )
    with sqlite3.connect(path) as db:
        db.executescript(changed_v2)
        db.execute("INSERT INTO metadata VALUES(2)")
    before = _database_files(path)

    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    assert _database_files(path) == before

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from video_download_control.uploads import schema
from video_download_control.uploads.schema import (
    LEGACY_SCHEMA_DDL,
    SCHEMA_DDL,
    SCHEMA_VERSION,
    UploadSchemaError,
    ensure_upload_schema,
    validate_upload_schema,
)


ACCOUNT_ID = "a" * 32
SOURCE_ID = "b" * 32
JOB_ID = "c" * 32
OPERATION_ID = "d" * 32


def _create_v1(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA_DDL)
        db.execute("INSERT INTO metadata VALUES(1)")
        db.execute(
            "INSERT INTO accounts(id,platform,name,auth_state,code,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (ACCOUNT_ID, "douyin", "same note", "ready", "account_ready", "2026-09-07T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO sources(id,name,suffix,size,sha256,created_at) VALUES(?,?,?,?,?,?)",
            (SOURCE_ID, "source.mp4", ".mp4", 5, "1" * 64, "2026-09-07T00:00:00+00:00"),
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
                "",
                "[]",
                None,
                "publish",
                1,
                "",
                "draft",
                "",
                "2026-09-07T00:00:00+00:00",
                "2026-09-07T00:00:00+00:00",
                None,
            ),
        )
        db.execute(
            "INSERT INTO operations(id,account_id,action,state,code,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                OPERATION_ID,
                ACCOUNT_ID,
                "check",
                "ready",
                "account_ready",
                "2026-09-07T00:00:00+00:00",
                "2026-09-07T00:00:00+00:00",
            ),
        )
        db.execute(
            "INSERT INTO requests(id,digest,job_ids) VALUES(?,?,?)",
            ("request_key", "2" * 64, f'["{JOB_ID}"]'),
        )


def _database_files(path: Path) -> dict[str, bytes]:
    paths = [path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))]
    return {candidate.name: candidate.read_bytes() for candidate in paths if os.path.lexists(candidate)}


def _request_digest(category_id, *, copyright_value=None) -> str:
    payload = [
        SOURCE_ID,
        [ACCOUNT_ID],
        "title",
        "",
        [],
        category_id,
        "publish",
        copyright_value,
        "",
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _set_v1_category_history(
    path: Path,
    category_id,
    *,
    copyright_value=None,
) -> str:
    digest = _request_digest(category_id, copyright_value=copyright_value)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET category_id=?", (category_id,))
        db.execute("UPDATE requests SET digest=?", (digest,))
    return digest


def test_new_schema_is_exact_and_active_account_names_are_unique(tmp_path):
    path = tmp_path / "uploads.sqlite3"

    ensure_upload_schema(path)
    validate_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM metadata").fetchone() == (SCHEMA_VERSION,)
        assert SCHEMA_VERSION == 4
        account_columns = [row[1] for row in db.execute("PRAGMA table_xinfo(accounts)")]
        source_columns = [row[1] for row in db.execute("PRAGMA table_xinfo(sources)")]
        assert account_columns[-2:] == ["lifecycle_state", "disconnected_at"]
        assert source_columns[-2:] == ["media_state", "deleted_at"]
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"
            )
        }
        assert indexes == {
            "accounts_active_name",
            "jobs_account_state",
            "jobs_source_state",
            "jobs_cover_landscape_state",
            "jobs_cover_portrait_state",
            "operations_account_state", "upload_attempts_request_state",
        }
        db.execute(
            "INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)",
            (ACCOUNT_ID, "douyin", "same note", "now"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)",
                ("e" * 32, "douyin", "same note", "now"),
            )
        db.execute(
            "UPDATE accounts SET lifecycle_state='disconnected',disconnected_at='later' WHERE id=?",
            (ACCOUNT_ID,),
        )
        db.execute(
            "INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)",
            ("f" * 32, "douyin", "same note", "later"),
        )


def test_exact_v1_migrates_atomically_and_preserves_relationships(tmp_path):
    path = tmp_path / "uploads.sqlite3"
    _create_v1(path)
    with pytest.raises(UploadSchemaError):
        validate_upload_schema(path)

    ensure_upload_schema(path)
    validate_upload_schema(path)

    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        account = dict(db.execute("SELECT * FROM accounts").fetchone())
        source = dict(db.execute("SELECT * FROM sources").fetchone())
        job = dict(db.execute("SELECT * FROM jobs").fetchone())
        operation = dict(db.execute("SELECT * FROM operations").fetchone())
        assert account == {
            "id": ACCOUNT_ID,
            "platform": "douyin",
            "name": "same note",
            "auth_state": "ready",
            "code": "account_ready",
            "created_at": "2026-09-07T00:00:00+00:00",
            "lifecycle_state": "active",
            "disconnected_at": None,
        }
        assert source["id"] == SOURCE_ID
        assert source["media_state"] == "present"
        assert source["deleted_at"] is None
        assert job["account_id"] == ACCOUNT_ID and job["source_id"] == SOURCE_ID
        assert operation["account_id"] == ACCOUNT_ID
        assert list(db.execute("PRAGMA foreign_key_check")) == []

    before = _database_files(path)
    ensure_upload_schema(path)
    assert _database_files(path) == before


@pytest.mark.parametrize(
    ("legacy_category", "stored_category", "copyright_value"),
    [(True, 1, None), (249.0, 249, 1), ("249", 249, None)],
)
def test_v1_migration_normalizes_proven_legacy_category_digest(
    tmp_path, legacy_category, stored_category, copyright_value
):
    path = tmp_path / "uploads.sqlite3"
    _create_v1(path)
    legacy_digest = _set_v1_category_history(
        path,
        legacy_category,
        copyright_value=copyright_value,
    )

    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        category = db.execute(
            "SELECT category_id,typeof(category_id) FROM jobs"
        ).fetchone()
        migrated_digest = db.execute("SELECT digest FROM requests").fetchone()[0]
    assert category == (stored_category, "integer")
    assert migrated_digest == _request_digest(
        stored_category,
        copyright_value=copyright_value,
    )
    if copyright_value is None:
        assert migrated_digest != _request_digest(
            stored_category,
            copyright_value=1,
        )
    assert migrated_digest != legacy_digest


@pytest.mark.parametrize("category_id", [None, 249])
def test_v1_migration_leaves_canonical_category_digest_unchanged(tmp_path, category_id):
    path = tmp_path / "uploads.sqlite3"
    _create_v1(path)
    original_digest = _set_v1_category_history(path, category_id)

    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT category_id FROM jobs").fetchone() == (category_id,)
        assert db.execute("SELECT digest FROM requests").fetchone() == (original_digest,)


@pytest.mark.parametrize(
    ("legacy_category", "stored_category", "stored_type"),
    [
        (False, 0, "integer"),
        (10001.0, 10001, "integer"),
        ("0249", 249, "integer"),
        (249.5, 249.5, "real"),
        ("category", "category", "text"),
    ],
)
def test_v1_migration_does_not_guess_unsupported_legacy_category(
    tmp_path, legacy_category, stored_category, stored_type
):
    path = tmp_path / "uploads.sqlite3"
    _create_v1(path)
    original_digest = _set_v1_category_history(path, legacy_category)

    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT category_id,typeof(category_id) FROM jobs"
        ).fetchone() == (stored_category, stored_type)
        assert db.execute("SELECT digest FROM requests").fetchone() == (original_digest,)


def test_v1_migration_does_not_normalize_bilibili_category_history(tmp_path):
    path = tmp_path / "uploads.sqlite3"
    _create_v1(path)
    original_digest = _set_v1_category_history(path, True)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE accounts SET platform='bilibili'")

    ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT category_id FROM jobs").fetchone() == (1,)
        assert db.execute("SELECT digest FROM requests").fetchone() == (original_digest,)


@pytest.mark.parametrize(
    "kind", ["incomplete", "extra", "too_new", "bad_version", "changed_check"]
)
def test_unknown_malformed_and_too_new_databases_are_not_modified(tmp_path, kind):
    path = tmp_path / "uploads.sqlite3"
    with sqlite3.connect(path) as db:
        if kind in {"too_new", "bad_version"}:
            db.executescript(SCHEMA_DDL)
            value = SCHEMA_VERSION + 1 if kind == "too_new" else "not-an-integer"
            db.execute("INSERT INTO metadata VALUES(?)", (value,))
        elif kind == "changed_check":
            db.executescript(SCHEMA_DDL.replace(
                "'active','disconnected'", "'ACTIVE','DISCONNECTED'"
            ))
            db.execute("INSERT INTO metadata VALUES(?)", (SCHEMA_VERSION,))
        else:
            db.execute("CREATE TABLE metadata(version INTEGER NOT NULL)")
            db.execute("INSERT INTO metadata VALUES(1)")
            if kind == "extra":
                db.execute("CREATE TABLE unrelated(value TEXT)")
    before = _database_files(path)

    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    assert _database_files(path) == before


def test_failed_migration_rolls_back_every_schema_change(tmp_path, monkeypatch):
    path = tmp_path / "uploads.sqlite3"
    _create_v1(path)
    original = schema._MIGRATION_V1_TO_V2
    monkeypatch.setattr(
        schema,
        "_MIGRATION_V1_TO_V2",
        (original[0], "SELECT value FROM migration_failure"),
    )

    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM metadata").fetchone() == (1,)
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")} == {
            "metadata",
            "accounts",
            "sources",
            "jobs",
            "operations",
            "requests",
        }
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(accounts)")][-1] == "created_at"
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(sources)")][-1] == "created_at"
        assert list(db.execute("PRAGMA foreign_key_check")) == []

    monkeypatch.setattr(schema, "_MIGRATION_V1_TO_V2", original)
    ensure_upload_schema(path)
    validate_upload_schema(path)


def test_failure_after_category_normalization_rolls_back_digest_and_schema(
    tmp_path, monkeypatch
):
    path = tmp_path / "uploads.sqlite3"
    _create_v1(path)
    original_digest = _set_v1_category_history(path, True)
    original_normalize = schema._normalize_v1_legacy_category_digests

    def fail_after_normalization(db):
        original_normalize(db)
        raise sqlite3.OperationalError("synthetic post-normalization failure")

    monkeypatch.setattr(
        schema,
        "_normalize_v1_legacy_category_digests",
        fail_after_normalization,
    )
    with pytest.raises(UploadSchemaError):
        ensure_upload_schema(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM metadata").fetchone() == (1,)
        assert db.execute("SELECT digest FROM requests").fetchone() == (original_digest,)
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(accounts)")][-1] == "created_at"
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(sources)")][-1] == "created_at"


def test_transient_wal_disappearance_is_treated_as_a_retryable_snapshot_change(
    tmp_path, monkeypatch
):
    path = tmp_path / "uploads.sqlite3"
    ensure_upload_schema(path)
    wal = Path(str(path) + "-wal")
    wal.write_bytes(b"transient")
    original = schema._require_plain_database

    def disappear(candidate: Path):
        if candidate == wal:
            wal.unlink()
            raise FileNotFoundError(candidate)
        return original(candidate)

    monkeypatch.setattr(schema, "_require_plain_database", disappear)

    with pytest.raises(schema._SnapshotChanged):
        schema._copy_stable_snapshot(path, tmp_path / "snapshot.sqlite3")


@pytest.mark.parametrize("existing_v1", [False, True])
def test_concurrent_create_or_migration_publishes_one_complete_schema(tmp_path, existing_v1):
    path = tmp_path / "uploads.sqlite3"
    if existing_v1:
        _create_v1(path)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def ensure() -> None:
        try:
            barrier.wait(timeout=5)
            ensure_upload_schema(path)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=ensure) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    validate_upload_schema(path)
    assert not list(tmp_path.glob(".uploads.sqlite3.*.tmp*"))


@pytest.mark.parametrize("existing_v1", [False, True])
def test_independent_processes_concurrently_ensure_one_complete_schema(tmp_path, existing_v1):
    path = tmp_path / "uploads.sqlite3"
    if existing_v1:
        _create_v1(path)
    child = (
        "import sys; from pathlib import Path; "
        "from video_download_control.uploads.schema import ensure_upload_schema; "
        "ensure_upload_schema(Path(sys.argv[1]))"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert results == [(0, "", "")] * len(processes)
    validate_upload_schema(path)
    assert not list(tmp_path.glob(".uploads.sqlite3.*.tmp*"))

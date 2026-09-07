from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import struct
import threading
import zlib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.uploads import backup as upload_backup
from video_download_control.uploads.activity_lock import (
    UploadActivityBusy,
    activity_lock_path,
    upload_activity_lock,
)
from video_download_control.uploads.backup import (
    UPLOAD_ASSET_PAYLOAD_PREFIX,
    UPLOAD_BACKUP_FORMAT_VERSION,
    UPLOAD_BACKUP_MANIFEST_HASH_NAME,
    UPLOAD_BACKUP_MANIFEST_NAME,
    UPLOAD_BACKUP_METADATA_NAME,
    UPLOAD_DATABASE_PAYLOAD_PATH,
    UPLOAD_MEDIA_PAYLOAD_PREFIX,
    UploadBackupError,
    create_upload_backup,
    restore_upload_backup,
)
from video_download_control.uploads.contracts import UploadError
from video_download_control.uploads.schema import (
    LEGACY_SCHEMA_DDL,
    SCHEMA_V2_DDL,
    ensure_upload_schema,
    validate_upload_schema,
)
from video_download_control.uploads.service import UploadService, default_upload_root


ACCOUNT_ACTIVE = "a" * 32
ACCOUNT_DISCONNECTED = "b" * 32
ACCOUNT_READY = "f" * 32
SOURCE_PRESENT = "c" * 32
SOURCE_MISSING = "d" * 32
SOURCE_DELETED = "e" * 32
PRESENT_BYTES = b"registered managed upload media\x00"
ASSET_ID = "9" * 32


class IdleBackend:
    def inspect(self) -> dict:
        return {"ready": False, "code": "synthetic"}


def _windows_short_path(path: Path) -> Path:
    if os.name != "nt":
        pytest.skip("Windows 8.3 short paths are Windows-only")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path_name = kernel32.GetShortPathNameW
    get_short_path_name.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_short_path_name.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_short_path_name(str(path), buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        pytest.skip("Windows 8.3 short names are unavailable on this volume")
    short_path = Path(buffer.value)
    if os.path.normcase(str(short_path)) == os.path.normcase(str(path)):
        pytest.skip("Windows 8.3 short names are disabled for this directory")
    return short_path


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    scanlines = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk("IHDR".encode(), struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _make_upload_root(tmp_path: Path, *, root: Path | None = None) -> Path:
    root = root or tmp_path / "upload-source"
    for relative in (
        "media",
        "assets",
        "incoming",
        "runtime",
        "private/accounts/douyin",
        "private/operations",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    ensure_upload_schema(root / "uploads.sqlite3")
    (root / ".worker.lock").write_bytes(b"0")
    (root / "incoming" / "partial.tmp").write_bytes(b"incoming-excluded")
    (root / "runtime" / "tool.exe").write_bytes(b"runtime-excluded")
    (root / "private" / "accounts" / "douyin" / "secret.json").write_bytes(
        b'{"cookie":"SECRET-CREDENTIAL-CANARY"}'
    )
    (root / "private" / "operations" / "login.tmp").write_bytes(
        b"operation-private-excluded"
    )
    (root / "media" / f"{SOURCE_PRESENT}.mp4").write_bytes(PRESENT_BYTES)
    (root / "media" / "orphan-unregistered.mp4").write_bytes(
        b"ORPHAN-MEDIA-CANARY"
    )
    now = "2026-09-07T00:00:00+00:00"
    with sqlite3.connect(root / "uploads.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executemany(
            "INSERT INTO accounts(id,platform,name,auth_state,code,created_at,"
            "lifecycle_state,disconnected_at) VALUES(?,?,?,?,?,?,?,?)",
            [
                (ACCOUNT_ACTIVE, "douyin", "active account", "checking", "", now, "active", None),
                (
                    ACCOUNT_DISCONNECTED,
                    "bilibili",
                    "historical account",
                    "unchecked",
                    "account_disconnected",
                    now,
                    "disconnected",
                    now,
                ),
                (
                    ACCOUNT_READY,
                    "tencent",
                    "ready account",
                    "checking",
                    "",
                    now,
                    "active",
                    None,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO sources(id,name,suffix,size,sha256,created_at,media_state,deleted_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    SOURCE_PRESENT,
                    "present.mp4",
                    ".mp4",
                    len(PRESENT_BYTES),
                    _sha256(PRESENT_BYTES),
                    now,
                    "present",
                    None,
                ),
                (SOURCE_MISSING, "missing.mov", ".mov", 9, "1" * 64, now, "missing", None),
                (SOURCE_DELETED, "deleted.mkv", ".mkv", 11, "2" * 64, now, "deleted", now),
            ],
        )
        jobs = [
            ("1" * 32, ACCOUNT_ACTIVE, SOURCE_PRESENT, "batch", "running", "", None),
            ("2" * 32, ACCOUNT_READY, SOURCE_PRESENT, "batch", "queued", "", None),
            ("3" * 32, ACCOUNT_ACTIVE, SOURCE_MISSING, "retry-base", "failed", "failed", None),
            (
                "4" * 32,
                ACCOUNT_ACTIVE,
                SOURCE_MISSING,
                "retry-base",
                "canceled",
                "canceled",
                "3" * 32,
            ),
            (
                "5" * 32,
                ACCOUNT_DISCONNECTED,
                SOURCE_DELETED,
                "historical",
                "submitted",
                "submitted",
                None,
            ),
        ]
        for job_id, account_id, source_id, title, state, code, retry_of in jobs:
            bilibili = account_id == ACCOUNT_DISCONNECTED
            db.execute(
                "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
                "mode,copyright,source_credit,state,code,created_at,updated_at,retry_of) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    account_id,
                    source_id,
                    title,
                    "description",
                    json.dumps(["archive"]) if bilibili else "[]",
                    249 if bilibili else None,
                    "publish",
                    1,
                    "",
                    state,
                    code,
                    now,
                    now,
                    retry_of,
                ),
            )
        # This fixture represents a Schema-2 database after the 2→3 migration:
        # pinned upstream defaults are now explicit and reviewable.
        db.execute(
            "UPDATE jobs SET platform_options=? WHERE account_id=?",
            ('{"declaration":"内容由AI生成"}', ACCOUNT_ACTIVE),
        )
        db.execute(
            "UPDATE jobs SET platform_options=? WHERE id=?",
            ('{"content_label":"含AI生成内容","short_title":"batch，精"}', "2" * 32),
        )
        db.executemany(
            "INSERT INTO operations(id,account_id,action,state,code,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                ("6" * 32, ACCOUNT_ACTIVE, "login", "queued", "", now, now),
                ("7" * 32, ACCOUNT_READY, "check", "running", "", now, now),
                ("8" * 32, ACCOUNT_DISCONNECTED, "check", "ready", "account_ready", now, now),
            ],
        )
        request_payload = [
            SOURCE_PRESENT,
            sorted([ACCOUNT_ACTIVE, ACCOUNT_READY]),
            "batch",
            "description",
            [],
            None,
            "publish",
            None,
            "",
        ]
        request_digest = hashlib.sha256(
            json.dumps(
                request_payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        db.execute(
            "INSERT INTO requests(id,digest,job_ids) VALUES(?,?,?)",
            (
                "request-key",
                request_digest,
                json.dumps(["1" * 32, "2" * 32]),
            ),
        )
        for request_id, job_id, payload in (
            (
                "request-retry-base",
                "3" * 32,
                [
                    SOURCE_MISSING,
                    [ACCOUNT_ACTIVE],
                    "retry-base",
                    "description",
                    [],
                    None,
                    "publish",
                    None,
                    "",
                ],
            ),
            (
                "request-historical",
                "5" * 32,
                [
                    SOURCE_DELETED,
                    [ACCOUNT_DISCONNECTED],
                    "historical",
                    "description",
                    ["archive"],
                    249,
                    "publish",
                    1,
                    "",
                ],
            ),
        ):
            digest = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            db.execute(
                "INSERT INTO requests(id,digest,job_ids) VALUES(?,?,?)",
                (request_id, digest, json.dumps([job_id])),
            )
    return root


def _v2_request_digest(db: sqlite3.Connection, job_ids: list[str]) -> str:
    db.row_factory = sqlite3.Row
    rows = list(db.execute(
        "SELECT j.*,a.platform FROM jobs j JOIN accounts a ON a.id=j.account_id "
        f"WHERE j.id IN ({','.join('?' for _ in job_ids)})",
        job_ids,
    ))
    by_id = {row["id"]: row for row in rows}
    targets = []
    for job_id in job_ids:
        row = by_id[job_id]
        targets.append({
            "account_id": row["account_id"],
            "platform": row["platform"],
            "title": row["title"],
            "description": row["description"],
            "tags": json.loads(row["tags"]),
            "category_id": row["category_id"],
            "mode": row["mode"],
            "copyright": row["copyright"],
            "source_credit": row["source_credit"],
            "cover_landscape_asset_id": row["cover_landscape_asset_id"],
            "cover_portrait_asset_id": row["cover_portrait_asset_id"],
            "publish_at_unix": row["publish_at_unix"],
            "publish_timezone_offset_minutes": row[
                "publish_timezone_offset_minutes"
            ],
            "platform_options": json.loads(row["platform_options"]),
        })
    payload = {
        "source_id": rows[0]["source_id"],
        "targets": sorted(targets, key=lambda target: target["account_id"]),
    }
    return _sha256(json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode())


def _add_cover_and_v2_request(root: Path) -> bytes:
    cover = _png(4, 3)
    (root / "assets" / f"{ASSET_ID}.png").write_bytes(cover)
    with sqlite3.connect(root / "uploads.sqlite3") as db:
        db.execute(
            "INSERT INTO upload_assets(id,kind,name,suffix,mime_type,size,sha256,width,"
            "height,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                ASSET_ID,
                "cover",
                "cover.png",
                ".png",
                "image/png",
                len(cover),
                _sha256(cover),
                4,
                3,
                "2026-09-07T00:00:00+00:00",
            ),
        )
        db.execute(
            "UPDATE jobs SET cover_landscape_asset_id=?,publish_at_unix=?,"
            "publish_timezone_offset_minutes=?,platform_options=? WHERE id=?",
            (
                ASSET_ID,
                1_900_002_600,
                570,
                '{"declaration":"内容由AI生成"}',
                "1" * 32,
            ),
        )
        db.execute(
            "UPDATE jobs SET platform_options=? WHERE id=?",
            ('{"content_label":null,"short_title":"batch，精"}', "2" * 32),
        )
        digest = _v2_request_digest(db, ["1" * 32, "2" * 32])
        db.execute(
            "UPDATE requests SET digest=?,digest_version=2 WHERE id='request-key'",
            (digest,),
        )
    return cover


def _manifest(backup_root: Path) -> dict:
    return json.loads((backup_root / UPLOAD_BACKUP_MANIFEST_NAME).read_text("utf-8"))


def _rewrite_manifest(backup_root: Path, manifest: dict) -> None:
    path = backup_root / UPLOAD_BACKUP_MANIFEST_NAME
    path.write_bytes(
        (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )
    )
    (backup_root / UPLOAD_BACKUP_MANIFEST_HASH_NAME).write_bytes(
        (_sha256(path.read_bytes()) + "\n").encode("ascii")
    )


def _update_entry(backup_root: Path, relative: str) -> None:
    manifest = _manifest(backup_root)
    path = backup_root.joinpath(*Path(relative).parts)
    entry = next(item for item in manifest["entries"] if item["path"] == relative)
    entry["size_bytes"] = path.stat().st_size
    entry["sha256"] = _sha256(path.read_bytes())
    _rewrite_manifest(backup_root, manifest)


def _write_format1_schema2_backup(
    root: Path,
    tags: list[str] | None = None,
    *,
    platform: str = "douyin",
    job_state: str = "draft",
) -> None:
    payload = root / "payload"
    media = payload / "media"
    media.mkdir(parents=True)
    database = payload / "uploads.sqlite3"
    source_id = "0" * 32
    account_id = "6" * 32
    job_id = "7" * 32
    media_bytes = b"legacy format one media"
    (media / f"{source_id}.mp4").write_bytes(media_bytes)
    now = "2026-09-07T00:00:00+00:00"
    tags = [] if tags is None else tags
    request_payload = [
        source_id,
        [account_id],
        "legacy format",
        "",
        tags,
        None,
        "publish",
        None,
        "",
    ]
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA_V2_DDL)
        db.execute("INSERT INTO metadata VALUES(2)")
        db.execute(
            "INSERT INTO accounts(id,platform,name,auth_state,code,created_at,"
            "lifecycle_state) VALUES(?,?,?,?,?,?,?)",
            (account_id, platform, "legacy account", "checking", "", now, "active"),
        )
        db.execute(
            "INSERT INTO sources(id,name,suffix,size,sha256,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                source_id,
                "legacy.mp4",
                ".mp4",
                len(media_bytes),
                _sha256(media_bytes),
                now,
            ),
        )
        db.execute(
            "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
            "mode,copyright,source_credit,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                account_id,
                source_id,
                "legacy format",
                "",
                json.dumps(tags, ensure_ascii=False),
                None,
                "publish",
                1,
                "",
                now,
                now,
            ),
        )
        db.execute("UPDATE jobs SET state=? WHERE id=?", (job_state, job_id))
        db.execute(
            "INSERT INTO requests(id,digest,job_ids) VALUES(?,?,?)",
            (
                "legacy-format-request",
                _sha256(json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()),
                json.dumps([job_id]),
            ),
        )
    payload_entries = [
        {
            "path": UPLOAD_DATABASE_PAYLOAD_PATH,
            "size_bytes": database.stat().st_size,
            "sha256": _sha256(database.read_bytes()),
        },
        {
            "path": f"payload/media/{source_id}.mp4",
            "size_bytes": len(media_bytes),
            "sha256": _sha256(media_bytes),
        },
    ]
    metadata = {
        "format_version": 1,
        "created_at": now,
        "application_version": "0.25.0",
        "schema_version": 2,
        "database_payload_path": UPLOAD_DATABASE_PAYLOAD_PATH,
        "media_payload_prefix": "payload/media",
        "payload_file_count": len(payload_entries),
        "payload_total_bytes": sum(entry["size_bytes"] for entry in payload_entries),
        "consistency": "exclusive_upload_activity_and_worker_plus_stable_sqlite_snapshot",
        "excluded_paths": [
            ".open-flame-setup.lock",
            ".worker.lock",
            "incoming",
            "private",
            "runtime",
        ],
        "media_scope": "registered_present_media_only",
        "secret_material_included": False,
        "account_fields": [
            "id",
            "platform",
            "name",
            "auth_state",
            "code",
            "created_at",
            "lifecycle_state",
            "disconnected_at",
        ],
        "preserved_tables": ["accounts", "sources", "jobs", "operations", "requests"],
        "restore_policy": {
            "ready_or_checking_account": "unchecked_account_missing",
            "queued_job": "draft_restart_confirmation_required",
            "running_job": "unknown_interrupted_result_unknown",
            "queued_or_running_operation": "failed_operation_interrupted",
        },
    }
    metadata_path = root / UPLOAD_BACKUP_METADATA_NAME
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    entries = [
        {
            "path": UPLOAD_BACKUP_METADATA_NAME,
            "size_bytes": metadata_path.stat().st_size,
            "sha256": _sha256(metadata_path.read_bytes()),
        },
        *payload_entries,
    ]
    entries.sort(key=lambda entry: entry["path"].casefold())
    _rewrite_manifest(root, {
        "format_version": 1,
        "algorithm": "sha256",
        "metadata_path": UPLOAD_BACKUP_METADATA_NAME,
        "entries": entries,
    })


def _write_windows_ads_or_skip(path: Path, payload: bytes) -> Path:
    stream = Path(str(path) + ":open_flame_test")
    try:
        stream.write_bytes(payload)
    except OSError as exc:
        pytest.skip(f"filesystem does not support NTFS alternate streams: {exc}")
    return stream


def test_backup_restore_round_trip_is_secret_free_and_applies_recovery_policy(tmp_path):
    source = _make_upload_root(tmp_path)
    source_before = _tree_bytes(source)
    backup_root = tmp_path / "upload-backup"

    backup = create_upload_backup(source_root=source, backup_target=backup_root)

    assert _tree_bytes(source) == source_before
    assert backup.schema_version == 3
    assert UPLOAD_BACKUP_FORMAT_VERSION == 2
    payload_media = backup_root.joinpath(*UPLOAD_MEDIA_PAYLOAD_PREFIX.parts)
    assert {path.name for path in payload_media.iterdir()} == {f"{SOURCE_PRESENT}.mp4"}
    all_backup_bytes = b"".join(
        path.read_bytes() for path in backup_root.rglob("*") if path.is_file()
    )
    assert b"SECRET-CREDENTIAL-CANARY" not in all_backup_bytes
    assert b"ORPHAN-MEDIA-CANARY" not in all_backup_bytes
    metadata = json.loads((backup_root / UPLOAD_BACKUP_METADATA_NAME).read_text("utf-8"))
    assert metadata["secret_material_included"] is False
    assert metadata["consistency"] == (
        "exclusive_upload_activity_and_worker_plus_stable_sqlite_snapshot"
    )
    assert metadata["media_scope"] == "registered_present_media_only"
    assert metadata["asset_scope"] == "registered_present_cover_assets_only"
    assert metadata["asset_payload_prefix"] == UPLOAD_ASSET_PAYLOAD_PREFIX.as_posix()
    assert "upload_assets" in metadata["preserved_tables"]
    assert metadata["excluded_paths"] == [
        ".open-flame-setup.lock",
        ".worker.lock",
        "incoming",
        "private",
        "runtime",
    ]

    restored_root = tmp_path / "upload-restored"
    restored = restore_upload_backup(backup_root=backup_root, restore_root=restored_root)

    assert restored.database_path == restored_root / "uploads.sqlite3"
    validate_upload_schema(restored.database_path)
    assert {path.relative_to(restored_root).as_posix() for path in restored_root.rglob("*")} == {
        "media",
        "assets",
        f"media/{SOURCE_PRESENT}.mp4",
        "uploads.sqlite3",
    }
    with sqlite3.connect(restored.database_path) as db:
        db.row_factory = sqlite3.Row
        accounts = {row["id"]: dict(row) for row in db.execute("SELECT * FROM accounts")}
        jobs = {row["id"]: dict(row) for row in db.execute("SELECT * FROM jobs")}
        operations = {row["id"]: dict(row) for row in db.execute("SELECT * FROM operations")}
        sources = {row["id"]: dict(row) for row in db.execute("SELECT * FROM sources")}
        assert accounts[ACCOUNT_ACTIVE]["auth_state"] == "unchecked"
        assert accounts[ACCOUNT_ACTIVE]["code"] == "account_missing"
        assert accounts[ACCOUNT_READY]["auth_state"] == "unchecked"
        assert accounts[ACCOUNT_READY]["code"] == "account_missing"
        assert accounts[ACCOUNT_DISCONNECTED]["lifecycle_state"] == "disconnected"
        assert jobs["1" * 32]["state"] == "unknown"
        assert jobs["1" * 32]["code"] == "interrupted_result_unknown"
        assert jobs["2" * 32]["state"] == "draft"
        assert jobs["2" * 32]["code"] == "restart_confirmation_required"
        assert jobs["4" * 32]["retry_of"] == "3" * 32
        assert operations["6" * 32]["state"] == "failed"
        assert operations["7" * 32]["state"] == "failed"
        assert operations["8" * 32]["state"] == "ready"
        assert sources[SOURCE_MISSING]["media_state"] == "missing"
        assert sources[SOURCE_DELETED]["media_state"] == "deleted"
        assert list(db.execute("PRAGMA foreign_key_check")) == []


def test_format2_round_trip_preserves_cover_schedule_options_and_v2_digest(tmp_path):
    source = _make_upload_root(tmp_path)
    cover = _add_cover_and_v2_request(source)
    before = _tree_bytes(source)
    backup_root = tmp_path / "format2-cover-backup"

    create_upload_backup(source_root=source, backup_target=backup_root)

    assert _tree_bytes(source) == before
    payload_assets = backup_root.joinpath(*UPLOAD_ASSET_PAYLOAD_PREFIX.parts)
    assert (payload_assets / f"{ASSET_ID}.png").read_bytes() == cover
    assert any(
        entry["path"] == f"payload/assets/{ASSET_ID}.png"
        for entry in _manifest(backup_root)["entries"]
    )

    restored = tmp_path / "format2-cover-restored"
    restore_upload_backup(backup_root=backup_root, restore_root=restored)

    assert (restored / "assets" / f"{ASSET_ID}.png").read_bytes() == cover
    with sqlite3.connect(restored / "uploads.sqlite3") as db:
        assert db.execute(
            "SELECT mime_type,width,height,media_state FROM upload_assets WHERE id=?",
            (ASSET_ID,),
        ).fetchone() == ("image/png", 4, 3, "present")
        assert db.execute(
            "SELECT cover_landscape_asset_id,publish_at_unix,"
            "publish_timezone_offset_minutes,platform_options FROM jobs WHERE id=?",
            ("1" * 32,),
        ).fetchone() == (
            ASSET_ID,
                1_900_002_600,
            570,
            '{"declaration":"内容由AI生成"}',
        )
        assert db.execute(
            "SELECT digest_version FROM requests WHERE id='request-key'"
        ).fetchone() == (2,)


@pytest.mark.parametrize(
    ("title", "short_title", "expected"),
    [
        ("主标题测试", "abc,def", "abc def"),
        ("abc", None, "abc，精彩内"),
        ("一二三四，五六七", None, "一二三四五六七"),
    ],
)
def test_service_reachable_tencent_short_titles_can_be_backed_up(
    tmp_path, title, short_title, expected
):
    root = tmp_path / "reachable-short-title"
    service = UploadService(root, IdleBackend())
    service.start()
    try:
        account = service.add_account("tencent", "backup account")
        video = tmp_path / "reachable.mp4"
        video.write_bytes(b"reachable tencent short title")
        source = service.import_source(video, video.name)
        options = {} if short_title is None else {"short_title": short_title}
        job = service.create_jobs(
            source_id=source["id"],
            account_ids=[account["id"]],
            title=title,
            description="",
            tags=[],
            idempotency_key="reachable-short-title",
            target_overrides=[{
                "account_id": account["id"],
                "platform_options": options,
            }],
        )[0]
        assert job["platform_options"]["short_title"] == expected
    finally:
        service.stop()

    result = create_upload_backup(
        source_root=root,
        backup_target=tmp_path / "reachable-short-title-backup",
    )
    assert result.schema_version == 3


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("asset_mime", "upload cover asset metadata is invalid"),
        ("options_noncanonical", "upload job platform options are not canonical"),
        ("options_unknown", "upload job platform option is unsupported"),
        ("tencent_short_title_null", "upload job platform options are invalid"),
        ("tencent_short_title_noncanonical", "upload job platform options are invalid"),
        ("schedule_unpaired", "upload job schedule metadata is invalid"),
        ("douyin_cover_orientation", "upload job cover metadata is invalid"),
        ("v2_digest", "upload request digest does not match its jobs"),
        ("retry_cover", "upload retry payload differs from its parent"),
    ],
)
def test_format2_rejects_rehashed_cover_and_platform_contract_damage(
    tmp_path, damage, expected
):
    source = _make_upload_root(tmp_path)
    _add_cover_and_v2_request(source)
    backup_root = tmp_path / f"format2-{damage}-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    database = backup_root.joinpath(*Path(UPLOAD_DATABASE_PAYLOAD_PATH).parts)
    with sqlite3.connect(database) as db:
        if damage == "asset_mime":
            db.execute(
                "UPDATE upload_assets SET mime_type='image/jpeg' WHERE id=?",
                (ASSET_ID,),
            )
        elif damage == "options_noncanonical":
            db.execute(
                "UPDATE jobs SET platform_options=? WHERE id=?",
                ('{"declaration": "内容由AI生成"}', "1" * 32),
            )
        elif damage == "options_unknown":
            db.execute(
                "UPDATE jobs SET platform_options=? WHERE id=?",
                ('{"declaration":"内容由AI生成","product":"x"}', "1" * 32),
            )
        elif damage == "tencent_short_title_null":
            db.execute(
                "UPDATE jobs SET platform_options=? WHERE id=?",
                ('{"content_label":null,"short_title":null}', "2" * 32),
            )
        elif damage == "tencent_short_title_noncanonical":
            db.execute(
                "UPDATE jobs SET platform_options=? WHERE id=?",
                ('{"content_label":null,"short_title":"invalid_title"}', "2" * 32),
            )
        elif damage == "schedule_unpaired":
            db.execute(
                "UPDATE jobs SET publish_timezone_offset_minutes=NULL WHERE id=?",
                ("1" * 32,),
            )
        elif damage == "douyin_cover_orientation":
            db.execute(
                "UPDATE jobs SET cover_landscape_asset_id=NULL,"
                "cover_portrait_asset_id=? WHERE id=?",
                (ASSET_ID, "1" * 32),
            )
        elif damage == "v2_digest":
            db.execute(
                "UPDATE requests SET digest=? WHERE id='request-key'",
                ("4" * 64,),
            )
        else:
            db.execute(
                "UPDATE jobs SET cover_landscape_asset_id=? WHERE id=?",
                (ASSET_ID, "4" * 32),
            )
    _update_entry(backup_root, UPLOAD_DATABASE_PAYLOAD_PATH)

    target = tmp_path / f"format2-{damage}-restored"
    with pytest.raises(UploadBackupError, match=expected):
        restore_upload_backup(backup_root=backup_root, restore_root=target)
    assert not target.exists()


def test_format1_schema2_restores_via_staged_migration_without_changing_backup(tmp_path):
    backup_root = tmp_path / "legacy-format1-backup"
    _write_format1_schema2_backup(backup_root)
    before = _tree_bytes(backup_root)
    restored = tmp_path / "legacy-format1-restored"

    result = restore_upload_backup(backup_root=backup_root, restore_root=restored)

    assert _tree_bytes(backup_root) == before
    assert result.schema_version == 3
    assert (restored / "assets").is_dir()
    assert list((restored / "assets").iterdir()) == []
    validate_upload_schema(restored / "uploads.sqlite3")
    with sqlite3.connect(restored / "uploads.sqlite3") as db:
        assert db.execute("SELECT version FROM metadata").fetchone() == (3,)
        assert db.execute("SELECT COUNT(*) FROM upload_assets").fetchone() == (0,)
        assert db.execute(
            "SELECT platform_options,cover_landscape_asset_id,publish_at_unix FROM jobs"
        ).fetchone() == ('{"declaration":"内容由AI生成"}', None, None)
        assert db.execute(
            "SELECT digest_version FROM requests"
        ).fetchone() == (1,)


def test_format1_schema2_legacy_tags_migrate_before_strict_schema3_audit(tmp_path):
    backup_root = tmp_path / "legacy-tags-format1"
    _write_format1_schema2_backup(backup_root, ["#topic", "中文，标签"])
    restored = tmp_path / "legacy-tags-restored"

    restore_upload_backup(backup_root=backup_root, restore_root=restored)

    with sqlite3.connect(restored / "uploads.sqlite3") as db:
        assert json.loads(db.execute("SELECT tags FROM jobs").fetchone()[0]) == [
            "topic", "中文、标签",
        ]
        assert db.execute("SELECT digest_version FROM requests").fetchone() == (1,)
    (restored / ".worker.lock").write_bytes(b"0")
    create_upload_backup(
        source_root=restored,
        backup_target=tmp_path / "post-migration-format2",
    )


def test_format1_schema2_tencent_short_title_restores_and_rebacks_up(tmp_path):
    backup_root = tmp_path / "legacy-tencent-format1"
    _write_format1_schema2_backup(backup_root, platform="tencent")
    restored = tmp_path / "legacy-tencent-restored"

    restore_upload_backup(backup_root=backup_root, restore_root=restored)

    with sqlite3.connect(restored / "uploads.sqlite3") as db:
        assert db.execute(
            "SELECT platform_options,state,code FROM jobs"
        ).fetchone() == (
            '{"content_label":"含AI生成内容","short_title":"legacyformat"}',
            "draft",
            "legacy_platform_options_review_required",
        )
    (restored / ".worker.lock").write_bytes(b"0")
    result = create_upload_backup(
        source_root=restored,
        backup_target=tmp_path / "legacy-tencent-format2",
    )
    assert result.schema_version == 3


@pytest.mark.parametrize(
    ("original_state", "expected_state", "expected_code"),
    [
        ("queued", "draft", "legacy_metadata_restart_confirmation_required"),
        ("running", "unknown", "legacy_metadata_interrupted_result_unknown"),
    ],
)
def test_format1_schema2_restore_preserves_legacy_review_and_interruption_reason(
    tmp_path, original_state, expected_state, expected_code
):
    backup_root = tmp_path / f"legacy-{original_state}-format1"
    _write_format1_schema2_backup(
        backup_root,
        platform="tencent",
        job_state=original_state,
    )
    restored = tmp_path / f"legacy-{original_state}-restored"

    restore_upload_backup(backup_root=backup_root, restore_root=restored)

    with sqlite3.connect(restored / "uploads.sqlite3") as db:
        assert db.execute("SELECT state,code FROM jobs").fetchone() == (
            expected_state,
            expected_code,
        )


def test_schema_one_is_rejected_without_mutating_source_or_publishing_target(tmp_path):
    root = tmp_path / "legacy-upload"
    (root / "media").mkdir(parents=True)
    (root / ".worker.lock").write_bytes(b"0")
    with sqlite3.connect(root / "uploads.sqlite3") as db:
        db.executescript(LEGACY_SCHEMA_DDL)
        db.execute("INSERT INTO metadata VALUES(1)")
    before = _tree_bytes(root)
    target = tmp_path / "legacy-backup"

    with pytest.raises(UploadBackupError):
        create_upload_backup(source_root=root, backup_target=target)

    assert _tree_bytes(root) == before
    assert not target.exists()
    assert not list(tmp_path.glob(".legacy-backup.partial-*"))


@contextmanager
def _held_worker_lock(path: Path):
    handle = path.open("r+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def test_active_worker_lock_rejects_backup_and_cleans_staging(tmp_path):
    root = _make_upload_root(tmp_path)
    target = tmp_path / "locked-backup"

    with _held_worker_lock(root / ".worker.lock"):
        with pytest.raises(UploadBackupError, match="worker is active"):
            create_upload_backup(source_root=root, backup_target=target)

    assert not target.exists()
    assert not list(tmp_path.glob(".locked-backup.partial-*"))


def test_missing_worker_lock_is_rejected_without_changing_source(tmp_path):
    root = _make_upload_root(tmp_path)
    (root / ".worker.lock").unlink()
    before = _tree_bytes(root)
    target = tmp_path / "no-worker-lock-backup"

    with pytest.raises(UploadBackupError, match="worker lock"):
        create_upload_backup(source_root=root, backup_target=target)

    assert _tree_bytes(root) == before
    assert not target.exists()
    assert not list(tmp_path.glob(".no-worker-lock-backup.partial-*"))


@pytest.mark.parametrize("damage", ["missing", "size", "hash", "hardlink", "missing_exists"])
def test_backup_rejects_media_that_disagrees_with_registered_state(tmp_path, damage):
    root = _make_upload_root(tmp_path)
    present = root / "media" / f"{SOURCE_PRESENT}.mp4"
    if damage == "missing":
        present.unlink()
    elif damage == "size":
        present.write_bytes(PRESENT_BYTES + b"x")
    elif damage == "hash":
        present.write_bytes(b"X" + PRESENT_BYTES[1:])
    elif damage == "hardlink":
        other = tmp_path / "other-media.mp4"
        os.link(present, other)
    else:
        (root / "media" / f"{SOURCE_MISSING}.mov").write_bytes(b"unexpected")
    target = tmp_path / f"backup-{damage}"

    with pytest.raises(UploadBackupError):
        create_upload_backup(source_root=root, backup_target=target)

    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.partial-*"))


@pytest.mark.parametrize(
    "damage",
    [
        "payload",
        "manifest",
        "manifest_bool",
        "metadata_bool",
        "missing",
        "extra",
        "escape",
        "hardlink",
    ],
)
def test_restore_rejects_tamper_escape_links_and_inventory_drift(tmp_path, damage):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / f"backup-{damage}"
    create_upload_backup(source_root=source, backup_target=backup_root)
    media_relative = f"{UPLOAD_MEDIA_PAYLOAD_PREFIX.as_posix()}/{SOURCE_PRESENT}.mp4"
    media = backup_root.joinpath(*PurePosixPath(media_relative).parts)
    if damage == "payload":
        media.write_bytes(b"tampered")
    elif damage == "manifest":
        (backup_root / UPLOAD_BACKUP_MANIFEST_NAME).write_bytes(
            (backup_root / UPLOAD_BACKUP_MANIFEST_NAME).read_bytes() + b" "
        )
    elif damage == "manifest_bool":
        manifest = _manifest(backup_root)
        manifest["format_version"] = True
        _rewrite_manifest(backup_root, manifest)
    elif damage == "metadata_bool":
        metadata_path = backup_root / UPLOAD_BACKUP_METADATA_NAME
        metadata = json.loads(metadata_path.read_text("utf-8"))
        metadata["format_version"] = True
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            "utf-8",
        )
        _update_entry(backup_root, UPLOAD_BACKUP_METADATA_NAME)
    elif damage == "missing":
        media.unlink()
    elif damage == "extra":
        (backup_root / "untracked.bin").write_bytes(b"extra")
    elif damage == "escape":
        manifest = _manifest(backup_root)
        manifest["entries"].append(
            {"path": "../escape", "size_bytes": 0, "sha256": _sha256(b"")}
        )
        _rewrite_manifest(backup_root, manifest)
    else:
        os.link(media, tmp_path / "linked-media.mp4")
    target = tmp_path / f"restore-{damage}"

    with pytest.raises(UploadBackupError):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.partial-*"))


def test_restore_rejects_casefold_colliding_scanned_paths(tmp_path, monkeypatch):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "casefold-collision-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    original_scan = upload_backup._common._scan_backup_files

    def scan_with_collision(root):
        return (*original_scan(root), "PAYLOAD/uploads.sqlite3")

    monkeypatch.setattr(upload_backup._common, "_scan_backup_files", scan_with_collision)
    target = tmp_path / "casefold-collision-restore"

    with pytest.raises(UploadBackupError, match="upload backup paths collide"):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate streams are Windows-only")
@pytest.mark.parametrize("subject", ["root", "directory", "file"])
def test_restore_rejects_named_stream_on_any_backup_tree_entry(tmp_path, subject):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / f"ads-{subject}-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    subjects = {
        "root": backup_root,
        "directory": backup_root / "payload",
        "file": backup_root / UPLOAD_BACKUP_METADATA_NAME,
    }
    _write_windows_ads_or_skip(subjects[subject], b"untracked-secret")
    target = tmp_path / f"ads-{subject}-restore"

    with pytest.raises(
        UploadBackupError, match="upload backup contains an alternate data stream"
    ):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate streams are Windows-only")
def test_backup_preserves_but_does_not_copy_source_media_named_stream(tmp_path):
    source = _make_upload_root(tmp_path)
    source_media = source / "media" / f"{SOURCE_PRESENT}.mp4"
    source_stream = _write_windows_ads_or_skip(source_media, b"source-only-secret")
    backup_root = tmp_path / "source-ads-backup"

    create_upload_backup(source_root=source, backup_target=backup_root)

    assert source_stream.read_bytes() == b"source-only-secret"
    backup_media = backup_root.joinpath(
        *UPLOAD_MEDIA_PAYLOAD_PREFIX.parts, f"{SOURCE_PRESENT}.mp4"
    )
    with pytest.raises(OSError):
        Path(str(backup_media) + ":open_flame_test").read_bytes()
    restored_root = tmp_path / "source-ads-restored"
    restore_upload_backup(backup_root=backup_root, restore_root=restored_root)


def test_rehashed_database_schema_tamper_is_still_rejected(tmp_path):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "schema-tamper-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    database = backup_root.joinpath(*Path(relative).parts)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE metadata SET version=4")
    _update_entry(backup_root, relative)
    target = tmp_path / "schema-tamper-restore"

    with pytest.raises(
        UploadBackupError, match="upload backup database schema is invalid"
    ):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("job_state", "upload job metadata or retry relation is invalid"),
        ("retry_cycle", "upload retry relation contains a cycle"),
        ("retry_payload", "upload retry payload differs from its parent"),
        ("request_json", "upload request job list is invalid"),
        ("request_blob", "upload request job list is invalid"),
        ("request_digest", "upload request digest does not match its jobs"),
        ("request_missing_root", "upload request root job coverage is invalid"),
        ("foreign_key", "upload backup database schema is invalid"),
    ],
)
def test_rehashed_database_semantic_tamper_is_still_rejected(
    tmp_path, damage, expected
):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / f"semantic-{damage}-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    database = backup_root.joinpath(*Path(relative).parts)
    with sqlite3.connect(database) as db:
        if damage == "job_state":
            db.execute("UPDATE jobs SET state='banana' WHERE id=?", ("1" * 32,))
        elif damage == "retry_cycle":
            db.execute("UPDATE jobs SET retry_of=? WHERE id=?", ("4" * 32, "3" * 32))
        elif damage == "retry_payload":
            db.execute("UPDATE jobs SET retry_of=? WHERE id=?", ("1" * 32, "4" * 32))
        elif damage == "request_json":
            db.execute("UPDATE requests SET job_ids='not-json'")
        elif damage == "request_blob":
            db.execute(
                "UPDATE requests SET job_ids=?",
                (sqlite3.Binary(json.dumps(["1" * 32, "2" * 32]).encode()),),
            )
        elif damage == "request_digest":
            db.execute("UPDATE requests SET digest=?", ("4" * 64,))
        elif damage == "request_missing_root":
            db.execute("DELETE FROM requests WHERE id='request-retry-base'")
        else:
            db.execute("UPDATE jobs SET account_id=? WHERE id=?", ("9" * 32, "1" * 32))
    _update_entry(backup_root, relative)
    target = tmp_path / f"semantic-{damage}-restore"

    with pytest.raises(UploadBackupError, match=expected):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("title_type", "upload job text metadata is invalid"),
        ("title_control", "upload job text metadata is invalid"),
        ("title_length", "upload job text metadata is invalid"),
        ("title_trim", "upload job text metadata is invalid"),
        ("description_control", "upload job text metadata is invalid"),
        ("description_length", "upload job text metadata is invalid"),
        ("description_trim", "upload job text metadata is invalid"),
        ("source_credit_type", "upload job text metadata is invalid"),
        ("source_credit_length", "upload job text metadata is invalid"),
        ("source_credit_trim", "upload job text metadata is invalid"),
        ("tags_not_list", "upload job tags are invalid"),
        ("tags_blob", "upload job tags are invalid"),
        ("tag_type", "upload job tags are invalid"),
        ("tag_empty", "upload job tags are invalid"),
        ("tag_length", "upload job tags are invalid"),
        ("tag_comma", "upload job tags are invalid"),
        ("tag_newline", "upload job tags are invalid"),
        ("tag_carriage_return", "upload job tags are invalid"),
        ("tag_tab", "upload job tags are invalid"),
        ("tag_control", "upload job tags are invalid"),
        ("tag_trim", "upload job tags are invalid"),
        ("tag_duplicate", "upload job tags are invalid"),
        ("tag_count", "upload job tags are invalid"),
        ("douyin_title_limit", "upload job platform metadata is invalid"),
        ("bilibili_title_limit", "upload job platform metadata is invalid"),
        ("douyin_draft", "upload job platform metadata is invalid"),
        ("bilibili_draft", "upload job platform metadata is invalid"),
        ("bilibili_category_missing", "upload job platform metadata is invalid"),
        ("bilibili_category_zero", "upload job platform metadata is invalid"),
        ("bilibili_category_high", "upload job platform metadata is invalid"),
        ("bilibili_category_type", "upload job platform metadata is invalid"),
        ("bilibili_tags_missing", "upload job platform metadata is invalid"),
        ("bilibili_copyright", "upload job metadata or retry relation is invalid"),
        ("bilibili_source_credit", "upload job platform metadata is invalid"),
        ("submitted_draft", "upload job terminal state does not match its mode"),
        ("draft_saved_publish", "upload job terminal state does not match its mode"),
    ],
)
def test_rehashed_database_job_contract_tamper_is_rejected(
    tmp_path, damage, expected
):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / f"job-contract-{damage}-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    database = backup_root.joinpath(*Path(relative).parts)
    douyin_job = "3" * 32
    tencent_job = "2" * 32
    bilibili_job = "5" * 32
    with sqlite3.connect(database) as db:
        if damage == "title_type":
            db.execute(
                "UPDATE jobs SET title=? WHERE id=?",
                (sqlite3.Binary(b"title"), douyin_job),
            )
        elif damage == "title_control":
            db.execute("UPDATE jobs SET title=? WHERE id=?", ("bad\x01title", douyin_job))
        elif damage == "title_length":
            db.execute("UPDATE jobs SET title=? WHERE id=?", ("x" * 101, douyin_job))
        elif damage == "title_trim":
            db.execute("UPDATE jobs SET title=? WHERE id=?", (" padded", douyin_job))
        elif damage == "description_control":
            db.execute(
                "UPDATE jobs SET description=? WHERE id=?",
                ("bad\x01description", douyin_job),
            )
        elif damage == "description_length":
            db.execute(
                "UPDATE jobs SET description=? WHERE id=?",
                ("x" * 2001, douyin_job),
            )
        elif damage == "description_trim":
            db.execute(
                "UPDATE jobs SET description=? WHERE id=?",
                ("description ", douyin_job),
            )
        elif damage == "source_credit_type":
            db.execute(
                "UPDATE jobs SET source_credit=? WHERE id=?",
                (sqlite3.Binary(b"credit"), douyin_job),
            )
        elif damage == "source_credit_length":
            db.execute(
                "UPDATE jobs SET source_credit=? WHERE id=?",
                ("x" * 201, douyin_job),
            )
        elif damage == "source_credit_trim":
            db.execute(
                "UPDATE jobs SET source_credit=? WHERE id=?",
                (" credit", douyin_job),
            )
        elif damage == "tags_not_list":
            db.execute("UPDATE jobs SET tags='{}' WHERE id=?", (douyin_job,))
        elif damage == "tags_blob":
            db.execute(
                "UPDATE jobs SET tags=? WHERE id=?",
                (sqlite3.Binary(b"[]"), douyin_job),
            )
        elif damage == "tag_type":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps([7]), douyin_job))
        elif damage == "tag_empty":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps([""]), douyin_job))
        elif damage == "tag_length":
            db.execute(
                "UPDATE jobs SET tags=? WHERE id=?",
                (json.dumps(["x" * 21]), douyin_job),
            )
        elif damage == "tag_comma":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps(["a,b"]), douyin_job))
        elif damage == "tag_newline":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps(["a\nb"]), douyin_job))
        elif damage == "tag_carriage_return":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps(["a\rb"]), douyin_job))
        elif damage == "tag_tab":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps(["a\tb"]), douyin_job))
        elif damage == "tag_control":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps(["a\x01b"]), douyin_job))
        elif damage == "tag_trim":
            db.execute("UPDATE jobs SET tags=? WHERE id=?", (json.dumps([" tag"]), douyin_job))
        elif damage == "tag_duplicate":
            db.execute(
                "UPDATE jobs SET tags=? WHERE id=?",
                (json.dumps(["tag", "tag"]), douyin_job),
            )
        elif damage == "tag_count":
            db.execute(
                "UPDATE jobs SET tags=? WHERE id=?",
                (json.dumps([str(index) for index in range(11)]), douyin_job),
            )
        elif damage == "douyin_title_limit":
            db.execute("UPDATE jobs SET title=? WHERE id=?", ("x" * 31, douyin_job))
        elif damage == "bilibili_title_limit":
            db.execute("UPDATE jobs SET title=? WHERE id=?", ("x" * 81, bilibili_job))
        elif damage == "douyin_draft":
            db.execute("UPDATE jobs SET mode='draft' WHERE id=?", (douyin_job,))
        elif damage == "bilibili_draft":
            db.execute("UPDATE jobs SET mode='draft' WHERE id=?", (bilibili_job,))
        elif damage == "bilibili_category_missing":
            db.execute("UPDATE jobs SET category_id=NULL WHERE id=?", (bilibili_job,))
        elif damage == "bilibili_category_zero":
            db.execute("UPDATE jobs SET category_id=0 WHERE id=?", (bilibili_job,))
        elif damage == "bilibili_category_high":
            db.execute("UPDATE jobs SET category_id=10001 WHERE id=?", (bilibili_job,))
        elif damage == "bilibili_category_type":
            db.execute("UPDATE jobs SET category_id='category' WHERE id=?", (bilibili_job,))
        elif damage == "bilibili_tags_missing":
            db.execute("UPDATE jobs SET tags='[]' WHERE id=?", (bilibili_job,))
        elif damage == "bilibili_copyright":
            db.execute("UPDATE jobs SET copyright=3 WHERE id=?", (bilibili_job,))
        elif damage == "bilibili_source_credit":
            db.execute(
                "UPDATE jobs SET copyright=2,source_credit='' WHERE id=?",
                (bilibili_job,),
            )
        elif damage == "submitted_draft":
            db.execute(
                "UPDATE jobs SET mode='draft',state='submitted' WHERE id=?",
                (tencent_job,),
            )
        else:
            db.execute("UPDATE jobs SET state='draft_saved' WHERE id=?", (tencent_job,))
    _update_entry(backup_root, relative)
    assert b"\r\n" not in (backup_root / UPLOAD_BACKUP_MANIFEST_HASH_NAME).read_bytes()
    target = tmp_path / f"job-contract-{damage}-restore"

    with pytest.raises(UploadBackupError, match=expected):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()


def test_non_bilibili_fields_and_defaulted_copyright_history_restore(tmp_path):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "non-bilibili-fields-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    database = backup_root.joinpath(*Path(relative).parts)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE jobs SET category_id=249,copyright=2,source_credit='credited source' "
            "WHERE id IN (?,?)",
            ("3" * 32, "4" * 32),
        )
        request_payload = [
            SOURCE_MISSING,
            [ACCOUNT_ACTIVE],
            "retry-base",
            "description",
            [],
            249,
            "publish",
            2,
            "credited source",
        ]
        db.execute(
            "UPDATE requests SET digest=? WHERE id='request-retry-base'",
            (
                hashlib.sha256(
                    json.dumps(
                        request_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            ),
        )
    _update_entry(backup_root, relative)
    target = tmp_path / "non-bilibili-fields-restore"

    restore_upload_backup(backup_root=backup_root, restore_root=target)

    with sqlite3.connect(target / "uploads.sqlite3") as db:
        assert db.execute(
            "SELECT category_id,copyright,source_credit FROM jobs WHERE id=?",
            ("3" * 32,),
        ).fetchone() == (249, 2, "credited source")


@pytest.mark.parametrize("category_id", [0, 10001, 249.5, "category"])
def test_rehashed_non_bilibili_category_outside_contract_is_rejected(
    tmp_path, category_id
):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "non-bilibili-category-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    database = backup_root.joinpath(*Path(relative).parts)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE jobs SET category_id=? WHERE id IN (?,?)",
            (category_id, "1" * 32, "2" * 32),
        )
        request_payload = [
            SOURCE_PRESENT,
            sorted([ACCOUNT_ACTIVE, ACCOUNT_READY]),
            "batch",
            "description",
            [],
            category_id,
            "publish",
            None,
            "",
        ]
        db.execute(
            "UPDATE requests SET digest=? WHERE id='request-key'",
            (
                hashlib.sha256(
                    json.dumps(
                        request_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            ),
        )
    _update_entry(backup_root, relative)
    target = tmp_path / "non-bilibili-category-restore"

    with pytest.raises(UploadBackupError, match="upload job category is invalid"):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("account_name_type", "upload account metadata is invalid"),
        ("account_name_trim", "upload account metadata is invalid"),
        ("account_name_control", "upload account metadata is invalid"),
        ("account_name_length", "upload account metadata is invalid"),
        ("account_code", "upload account metadata is invalid"),
        ("account_created_at", "upload account metadata is invalid"),
        ("account_disconnected_at", "upload account metadata is invalid"),
        ("source_name_type", "upload source metadata is invalid"),
        ("source_name_trim", "upload source metadata is invalid"),
        ("source_name_control", "upload source metadata is invalid"),
        ("source_name_length", "upload source metadata is invalid"),
        ("source_name_slash", "upload source metadata is invalid"),
        ("source_name_backslash", "upload source metadata is invalid"),
        ("source_name_colon", "upload source metadata is invalid"),
        ("source_name_nul", "upload source metadata is invalid"),
        ("source_name_dot", "upload source metadata is invalid"),
        ("source_name_dotdot", "upload source metadata is invalid"),
        ("source_suffix_mismatch", "upload source metadata is invalid"),
        ("source_created_at", "upload source metadata is invalid"),
        ("source_deleted_at", "upload source metadata is invalid"),
        ("job_code", "upload job metadata or retry relation is invalid"),
        ("job_created_at", "upload job metadata or retry relation is invalid"),
        ("job_updated_at", "upload job metadata or retry relation is invalid"),
        ("operation_code", "upload operation metadata is invalid"),
        ("operation_created_at", "upload operation metadata is invalid"),
        ("operation_updated_at", "upload operation metadata is invalid"),
        ("duplicate_active_operation", "active upload operation relation is invalid"),
        ("inactive_account_operation", "active upload operation relation is invalid"),
        ("retry_parent_state", "upload retry parent state is invalid"),
        ("active_job_missing_source", "active upload job source is not present"),
    ],
)
def test_rehashed_database_identity_and_relation_tamper_is_rejected(
    tmp_path, damage, expected
):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / f"identity-{damage}-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    database = backup_root.joinpath(*Path(relative).parts)
    with sqlite3.connect(database) as db:
        if damage == "account_name_type":
            db.execute(
                "UPDATE accounts SET name=? WHERE id=?",
                (sqlite3.Binary(b"account"), ACCOUNT_ACTIVE),
            )
        elif damage == "account_name_trim":
            db.execute("UPDATE accounts SET name=' account' WHERE id=?", (ACCOUNT_ACTIVE,))
        elif damage == "account_name_control":
            db.execute("UPDATE accounts SET name=? WHERE id=?", ("a\x01b", ACCOUNT_ACTIVE))
        elif damage == "account_name_length":
            db.execute("UPDATE accounts SET name=? WHERE id=?", ("x" * 61, ACCOUNT_ACTIVE))
        elif damage == "account_code":
            db.execute("UPDATE accounts SET code='bad-code' WHERE id=?", (ACCOUNT_ACTIVE,))
        elif damage == "account_created_at":
            db.execute(
                "UPDATE accounts SET created_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), ACCOUNT_ACTIVE),
            )
        elif damage == "account_disconnected_at":
            db.execute(
                "UPDATE accounts SET disconnected_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), ACCOUNT_DISCONNECTED),
            )
        elif damage == "source_name_type":
            db.execute(
                "UPDATE sources SET name=? WHERE id=?",
                (sqlite3.Binary(b"present.mp4"), SOURCE_PRESENT),
            )
        elif damage == "source_name_trim":
            db.execute("UPDATE sources SET name=' present.mp4' WHERE id=?", (SOURCE_PRESENT,))
        elif damage == "source_name_control":
            db.execute("UPDATE sources SET name=? WHERE id=?", ("a\x01.mp4", SOURCE_PRESENT))
        elif damage == "source_name_length":
            db.execute(
                "UPDATE sources SET name=? WHERE id=?",
                ("x" * 177 + ".mp4", SOURCE_PRESENT),
            )
        elif damage == "source_name_slash":
            db.execute("UPDATE sources SET name='bad/name.mp4' WHERE id=?", (SOURCE_PRESENT,))
        elif damage == "source_name_backslash":
            db.execute(
                "UPDATE sources SET name='bad\\name.mp4' WHERE id=?", (SOURCE_PRESENT,)
            )
        elif damage == "source_name_colon":
            db.execute("UPDATE sources SET name='bad:name.mp4' WHERE id=?", (SOURCE_PRESENT,))
        elif damage == "source_name_nul":
            db.execute("UPDATE sources SET name=? WHERE id=?", ("bad\x00.mp4", SOURCE_PRESENT))
        elif damage == "source_name_dot":
            db.execute("UPDATE sources SET name='.' WHERE id=?", (SOURCE_PRESENT,))
        elif damage == "source_name_dotdot":
            db.execute("UPDATE sources SET name='..' WHERE id=?", (SOURCE_PRESENT,))
        elif damage == "source_suffix_mismatch":
            db.execute("UPDATE sources SET name='present.mov' WHERE id=?", (SOURCE_PRESENT,))
        elif damage == "source_created_at":
            db.execute(
                "UPDATE sources SET created_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), SOURCE_PRESENT),
            )
        elif damage == "source_deleted_at":
            db.execute(
                "UPDATE sources SET deleted_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), SOURCE_DELETED),
            )
        elif damage == "job_code":
            db.execute("UPDATE jobs SET code='bad-code' WHERE id=?", ("3" * 32,))
        elif damage == "job_created_at":
            db.execute(
                "UPDATE jobs SET created_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), "3" * 32),
            )
        elif damage == "job_updated_at":
            db.execute(
                "UPDATE jobs SET updated_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), "3" * 32),
            )
        elif damage == "operation_code":
            db.execute("UPDATE operations SET code='bad-code' WHERE id=?", ("8" * 32,))
        elif damage == "operation_created_at":
            db.execute(
                "UPDATE operations SET created_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), "8" * 32),
            )
        elif damage == "operation_updated_at":
            db.execute(
                "UPDATE operations SET updated_at=? WHERE id=?",
                (sqlite3.Binary(b"time"), "8" * 32),
            )
        elif damage == "duplicate_active_operation":
            db.execute(
                "UPDATE operations SET account_id=? WHERE id=?",
                (ACCOUNT_ACTIVE, "7" * 32),
            )
        elif damage == "inactive_account_operation":
            db.execute(
                "UPDATE operations SET account_id=? WHERE id=?",
                (ACCOUNT_DISCONNECTED, "6" * 32),
            )
        elif damage == "retry_parent_state":
            db.execute("UPDATE jobs SET state='submitted' WHERE id=?", ("3" * 32,))
        else:
            db.execute("UPDATE jobs SET state='draft' WHERE id=?", ("3" * 32,))
    _update_entry(backup_root, relative)
    assert (backup_root / UPLOAD_BACKUP_MANIFEST_HASH_NAME).read_bytes().endswith(b"\n")
    assert b"\r\n" not in (backup_root / UPLOAD_BACKUP_MANIFEST_HASH_NAME).read_bytes()
    target = tmp_path / f"identity-{damage}-restore"

    with pytest.raises(UploadBackupError, match=expected):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert not target.exists()


def test_existing_backup_and_restore_targets_are_never_replaced(tmp_path):
    source = _make_upload_root(tmp_path)
    existing_backup = tmp_path / "existing-backup"
    existing_backup.mkdir()
    (existing_backup / "keep").write_bytes(b"keep")
    with pytest.raises(UploadBackupError, match="already exists"):
        create_upload_backup(source_root=source, backup_target=existing_backup)
    assert (existing_backup / "keep").read_bytes() == b"keep"

    backup_root = tmp_path / "valid-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    existing_restore = tmp_path / "existing-restore"
    existing_restore.mkdir()
    (existing_restore / "keep").write_bytes(b"keep")
    with pytest.raises(UploadBackupError, match="already exists"):
        restore_upload_backup(backup_root=backup_root, restore_root=existing_restore)
    assert (existing_restore / "keep").read_bytes() == b"keep"


def test_backup_failure_and_restore_failure_remove_private_staging(tmp_path, monkeypatch):
    source = _make_upload_root(tmp_path)
    backup_target = tmp_path / "failed-backup"
    original_copy = upload_backup._copy_regular_file

    def fail_media(source_path, destination, **kwargs):
        if source_path.parent.name == "media":
            raise UploadBackupError("injected media copy failure")
        return original_copy(source_path, destination, **kwargs)

    monkeypatch.setattr(upload_backup, "_copy_regular_file", fail_media)
    with pytest.raises(UploadBackupError, match="injected"):
        create_upload_backup(source_root=source, backup_target=backup_target)
    assert not backup_target.exists()
    assert not list(tmp_path.glob(".failed-backup.partial-*"))

    monkeypatch.setattr(upload_backup, "_copy_regular_file", original_copy)
    valid_backup = tmp_path / "valid-for-failed-restore"
    create_upload_backup(source_root=source, backup_target=valid_backup)
    restore_target = tmp_path / "failed-restore"

    def fail_policy(_path, _original_job_states):
        raise UploadBackupError("injected restore policy failure")

    monkeypatch.setattr(upload_backup, "_apply_restore_state_policy", fail_policy)
    with pytest.raises(UploadBackupError, match="injected"):
        restore_upload_backup(backup_root=valid_backup, restore_root=restore_target)
    assert not restore_target.exists()
    assert not list(tmp_path.glob(".failed-restore.partial-*"))


def test_interruption_removes_private_staging(tmp_path, monkeypatch):
    source = _make_upload_root(tmp_path)
    backup_target = tmp_path / "interrupted-backup"

    def interrupt_backup(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(upload_backup, "_snapshot_upload_database", interrupt_backup)
    with pytest.raises(KeyboardInterrupt):
        create_upload_backup(source_root=source, backup_target=backup_target)
    assert not backup_target.exists()
    assert not list(tmp_path.glob(".interrupted-backup.partial-*"))

    monkeypatch.undo()
    valid_backup = tmp_path / "valid-for-interrupted-restore"
    create_upload_backup(source_root=source, backup_target=valid_backup)
    restore_target = tmp_path / "interrupted-restore"
    monkeypatch.setattr(upload_backup, "_apply_restore_state_policy", interrupt_backup)
    with pytest.raises(KeyboardInterrupt):
        restore_upload_backup(backup_root=valid_backup, restore_root=restore_target)
    assert not restore_target.exists()
    assert not list(tmp_path.glob(".interrupted-restore.partial-*"))


def test_wal_snapshot_does_not_change_any_source_file_or_create_source_sidecars(tmp_path):
    root = _make_upload_root(tmp_path)
    database = root / "uploads.sqlite3"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE accounts SET code='wal_visible' WHERE id=?", (ACCOUNT_ACTIVE,))
        writer.commit()
        before = _tree_bytes(root)

        backup_root = tmp_path / "wal-backup"
        create_upload_backup(source_root=root, backup_target=backup_root)

        assert _tree_bytes(root) == before
        payload_database = backup_root.joinpath(*Path(UPLOAD_DATABASE_PAYLOAD_PATH).parts)
        with sqlite3.connect(payload_database) as reader:
            assert reader.execute(
                "SELECT code FROM accounts WHERE id=?", (ACCOUNT_ACTIVE,)
            ).fetchone() == ("wal_visible",)
    finally:
        writer.close()


def test_wal_only_snapshot_does_not_create_source_shm(tmp_path):
    root = _make_upload_root(tmp_path)
    database = root / "uploads.sqlite3"
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE accounts SET code='wal_only_visible' WHERE id=?", (ACCOUNT_ACTIVE,))
        writer.commit()
        main_bytes = database.read_bytes()
        wal_bytes = wal.read_bytes()
    finally:
        writer.close()

    database.write_bytes(main_bytes)
    wal.write_bytes(wal_bytes)
    shm.unlink(missing_ok=True)
    assert wal.exists()
    assert not shm.exists()
    before = _tree_bytes(root)

    backup_root = tmp_path / "wal-only-backup"
    create_upload_backup(source_root=root, backup_target=backup_root)

    assert _tree_bytes(root) == before
    assert not shm.exists()
    payload_database = backup_root.joinpath(*Path(UPLOAD_DATABASE_PAYLOAD_PATH).parts)
    with sqlite3.connect(payload_database) as reader:
        assert reader.execute(
            "SELECT code FROM accounts WHERE id=?", (ACCOUNT_ACTIVE,)
        ).fetchone() == ("wal_only_visible",)


def test_live_fastapi_lifespan_blocks_create_until_shutdown(tmp_path, settings):
    source = _make_upload_root(
        tmp_path,
        root=default_upload_root(settings.data_root),
    )
    target = tmp_path / "live-app-backup"
    app = create_app(settings)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
        assert app.state.upload_manager.service is None
        with pytest.raises(
            UploadBackupError,
            match="upload application or operation is active",
        ):
            create_upload_backup(source_root=source, backup_target=target)
        assert not target.exists()
        assert not list(tmp_path.glob(".live-app-backup.partial-*"))

    create_upload_backup(source_root=source, backup_target=target)
    assert target.is_dir()


def test_service_category_id_round_trips_through_backup_and_restore(tmp_path):
    source_root = tmp_path / "service-category-source"
    service = UploadService(source_root, IdleBackend())
    media = tmp_path / "category-source.mp4"
    media.write_bytes(b"service category payload")
    account = service.add_account("bilibili", "category account")
    imported = service.import_source(media, media.name)
    job = service.create_jobs(
        source_id=imported["id"],
        account_ids=[account["id"]],
        title="category round trip",
        description="",
        tags=["category"],
        category_id=249,
        copyright=1,
        idempotency_key="category_round_trip",
    )[0]
    assert job["category_id"] == 249

    service.start()
    service.stop()
    backup_root = tmp_path / "service-category-backup"
    create_upload_backup(source_root=source_root, backup_target=backup_root)
    restored_root = tmp_path / "service-category-restored"
    restore_upload_backup(backup_root=backup_root, restore_root=restored_root)

    with sqlite3.connect(restored_root / "uploads.sqlite3") as db:
        assert db.execute(
            "SELECT category_id, typeof(category_id) FROM jobs WHERE id=?",
            (job["id"],),
        ).fetchone() == (249, "integer")


def test_proven_v1_bool_category_history_migrates_and_backs_up(tmp_path):
    source_root = tmp_path / "legacy-category-source"
    (source_root / "media").mkdir(parents=True)
    database = source_root / "uploads.sqlite3"
    media_bytes = b"legacy bool category"
    source_id = "9" * 32
    account_id = "a" * 32
    job_id = "b" * 32
    now = "2026-09-07T00:00:00+00:00"
    (source_root / "media" / f"{source_id}.mp4").write_bytes(media_bytes)
    request_payload = [
        source_id,
        [account_id],
        "legacy category",
        "",
        [],
        True,
        "publish",
        None,
        "",
    ]
    request_digest = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with sqlite3.connect(database) as db:
        db.executescript(LEGACY_SCHEMA_DDL)
        db.execute("INSERT INTO metadata VALUES(1)")
        db.execute(
            "INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)",
            (account_id, "douyin", "legacy account", now),
        )
        db.execute(
            "INSERT INTO sources(id,name,suffix,size,sha256,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                source_id,
                "legacy.mp4",
                ".mp4",
                len(media_bytes),
                _sha256(media_bytes),
                now,
            ),
        )
        db.execute(
            "INSERT INTO jobs(id,account_id,source_id,title,description,tags,"
            "category_id,mode,copyright,source_credit,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                account_id,
                source_id,
                "legacy category",
                "",
                "[]",
                True,
                "publish",
                1,
                "",
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO requests(id,digest,job_ids) VALUES(?,?,?)",
            ("legacy-category", request_digest, json.dumps([job_id])),
        )

    service = UploadService(source_root, IdleBackend())
    service.start()
    service.stop()
    backup_root = tmp_path / "legacy-category-backup"
    create_upload_backup(source_root=source_root, backup_target=backup_root)
    restored_root = tmp_path / "legacy-category-restored"
    restore_upload_backup(backup_root=backup_root, restore_root=restored_root)

    with sqlite3.connect(restored_root / "uploads.sqlite3") as db:
        assert db.execute(
            "SELECT category_id,typeof(category_id) FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone() == (1, "integer")
        canonical_payload = [*request_payload]
        canonical_payload[5] = 1
        canonical_digest = hashlib.sha256(
            json.dumps(
                canonical_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert db.execute(
            "SELECT digest FROM requests WHERE id='legacy-category'"
        ).fetchone() == (canonical_digest,)


def test_active_and_standby_services_both_block_create_until_all_stop(tmp_path):
    source = tmp_path / "service-upload-root"
    first = UploadService(source, IdleBackend())
    second = UploadService(source, IdleBackend())
    target = tmp_path / "services-stopped-backup"

    try:
        first.start()
        second.start()
        assert first.status()["scheduler_state"] == "running"
        assert second.status()["scheduler_state"] == "standby"
        with pytest.raises(
            UploadBackupError,
            match="upload application or operation is active",
        ):
            create_upload_backup(source_root=source, backup_target=target)

        first.stop()
        with pytest.raises(
            UploadBackupError,
            match="upload application or operation is active",
        ):
            create_upload_backup(source_root=source, backup_target=target)
        assert not target.exists()
        assert not list(tmp_path.glob(".services-stopped-backup.partial-*"))

        second.stop()
        create_upload_backup(source_root=source, backup_target=target)
        assert target.is_dir()
    finally:
        first.stop()
        second.stop()


def test_restore_activity_conflict_leaves_no_target_or_staging(tmp_path):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "restore-lock-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    target = tmp_path / "restore-lock-target"

    with upload_activity_lock(target, exclusive=False):
        with pytest.raises(
            UploadBackupError,
            match="upload application or operation is active",
        ):
            restore_upload_backup(backup_root=backup_root, restore_root=target)
        assert not target.exists()
        assert not list(tmp_path.glob(".restore-lock-target.partial-*"))

    restore_upload_backup(backup_root=backup_root, restore_root=target)
    assert target.is_dir()


def test_restore_reserves_target_until_policy_and_publish_finish(tmp_path, monkeypatch):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "restore-race-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    target = tmp_path / "restore-race-target"
    entered = threading.Event()
    release = threading.Event()
    original_policy = upload_backup._apply_restore_state_policy
    outcome: list[object] = []

    def paused_policy(database_path: Path, original_job_states) -> None:
        entered.set()
        assert release.wait(10)
        original_policy(database_path, original_job_states)

    def run_restore() -> None:
        try:
            outcome.append(
                restore_upload_backup(
                    backup_root=backup_root,
                    restore_root=target,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome.append(exc)

    monkeypatch.setattr(upload_backup, "_apply_restore_state_policy", paused_policy)
    thread = threading.Thread(target=run_restore)
    thread.start()
    assert entered.wait(10)
    try:
        assert not target.exists()
        with pytest.raises(UploadError, match="upload_activity_busy"):
            UploadService(target, IdleBackend())
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(outcome) == 1 and not isinstance(outcome[0], BaseException), outcome
    assert target.is_dir()
    assert not list(tmp_path.glob(".restore-race-target.partial-*"))
    UploadService(target, IdleBackend())


def test_activity_lock_paths_cannot_be_used_as_backup_or_restore_roots(tmp_path):
    source = _make_upload_root(tmp_path)
    source_lock = activity_lock_path(source)
    with pytest.raises(UploadBackupError, match="conflicts with activity lock"):
        create_upload_backup(source_root=source, backup_target=source_lock)
    assert not source_lock.exists()

    ordinary_backup = tmp_path / "ordinary-backup"
    create_upload_backup(source_root=source, backup_target=ordinary_backup)
    restore_at_source_lock = activity_lock_path(ordinary_backup)
    with pytest.raises(UploadBackupError, match="conflicts with activity lock"):
        restore_upload_backup(
            backup_root=ordinary_backup,
            restore_root=restore_at_source_lock,
        )
    assert not restore_at_source_lock.exists()

    restore_target = tmp_path / "reserved-restore-target"
    backup_at_target_lock = activity_lock_path(restore_target)
    create_upload_backup(source_root=source, backup_target=backup_at_target_lock)
    with pytest.raises(UploadBackupError, match="conflicts with activity lock"):
        restore_upload_backup(
            backup_root=backup_at_target_lock,
            restore_root=restore_target,
        )
    assert not restore_target.exists()


def test_canonical_parent_alias_cannot_hide_backup_inside_source(tmp_path):
    source = _make_upload_root(tmp_path)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.mkdir()
    source_alias = alias_parent / ".." / source.name
    hidden_target = source_alias / "hidden-backup"

    assert activity_lock_path(source_alias) == activity_lock_path(source)
    with pytest.raises(UploadBackupError, match="overlaps its source"):
        create_upload_backup(
            source_root=source_alias,
            backup_target=hidden_target,
        )
    assert not (source / "hidden-backup").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 paths are Windows-only")
def test_windows_short_paths_cannot_hide_upload_backup_or_restore_overlap(tmp_path):
    source = _make_upload_root(
        tmp_path,
        root=tmp_path / "Canonical Upload Source Directory",
    )
    short_source = _windows_short_path(source)

    assert activity_lock_path(short_source) == activity_lock_path(source)
    with pytest.raises(UploadBackupError, match="overlaps its source"):
        create_upload_backup(
            source_root=source,
            backup_target=short_source / "nested-backup",
        )
    assert not (source / "nested-backup").exists()

    backup_root = tmp_path / "Canonical Upload Backup Directory"
    create_upload_backup(source_root=source, backup_target=backup_root)
    short_backup = _windows_short_path(backup_root)
    hidden_restore = short_backup / "nested-restore"
    with pytest.raises(UploadBackupError, match="overlaps its backup"):
        restore_upload_backup(
            backup_root=backup_root,
            restore_root=hidden_restore,
        )
    assert not hidden_restore.exists()


def test_restore_of_rehashed_wal_database_never_opens_or_changes_backup_source(
    tmp_path,
    monkeypatch,
):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "wal-mode-restore-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    payload_database = backup_root.joinpath(*Path(relative).parts)
    db = sqlite3.connect(payload_database)
    try:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    finally:
        db.close()
    Path(str(payload_database) + "-wal").unlink(missing_ok=True)
    Path(str(payload_database) + "-shm").unlink(missing_ok=True)
    assert payload_database.read_bytes()[18:20] == b"\x02\x02"
    _update_entry(backup_root, relative)
    source_before = _tree_bytes(backup_root)
    original_connect = upload_backup.sqlite3.connect
    original_audit = upload_backup._audit_database_and_media
    audited: list[Path] = []

    def guarded_connect(path, *args, **kwargs):
        if Path(path) == payload_database:
            pytest.fail("restore opened the immutable backup database through SQLite")
        return original_connect(path, *args, **kwargs)

    def observing_audit(
        *,
        database_path: Path,
        media_root: Path,
        asset_root: Path | None = None,
        schema_version: int | None = None,
    ) -> None:
        assert not database_path.is_relative_to(backup_root)
        assert _tree_bytes(backup_root) == source_before
        assert not Path(str(payload_database) + "-wal").exists()
        assert not Path(str(payload_database) + "-shm").exists()
        audited.append(database_path)
        original_audit(
            database_path=database_path,
            media_root=media_root,
            asset_root=asset_root,
            schema_version=schema_version,
        )

    monkeypatch.setattr(upload_backup.sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(upload_backup, "_audit_database_and_media", observing_audit)
    target = tmp_path / "wal-mode-restore-target"
    restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert len(audited) == 2
    assert _tree_bytes(backup_root) == source_before
    assert not Path(str(payload_database) + "-wal").exists()
    assert not Path(str(payload_database) + "-shm").exists()
    assert not Path(str(target / "uploads.sqlite3") + "-wal").exists()
    assert not Path(str(target / "uploads.sqlite3") + "-shm").exists()


def test_semantic_damage_is_rejected_in_staging_before_restore_policy(
    tmp_path,
    monkeypatch,
):
    source = _make_upload_root(tmp_path)
    backup_root = tmp_path / "pre-policy-audit-backup"
    create_upload_backup(source_root=source, backup_target=backup_root)
    relative = UPLOAD_DATABASE_PAYLOAD_PATH
    payload_database = backup_root.joinpath(*Path(relative).parts)
    db = sqlite3.connect(payload_database)
    try:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("DELETE FROM requests WHERE id='request-retry-base'")
        db.commit()
    finally:
        db.close()
    Path(str(payload_database) + "-wal").unlink(missing_ok=True)
    Path(str(payload_database) + "-shm").unlink(missing_ok=True)
    _update_entry(backup_root, relative)
    source_before = _tree_bytes(backup_root)
    policy_called = False

    def unexpected_policy(_database_path: Path, _original_job_states) -> None:
        nonlocal policy_called
        policy_called = True
        pytest.fail("restore policy ran before the raw staged database audit")

    monkeypatch.setattr(upload_backup, "_apply_restore_state_policy", unexpected_policy)
    target = tmp_path / "pre-policy-audit-target"
    with pytest.raises(
        UploadBackupError,
        match="upload request root job coverage is invalid",
    ):
        restore_upload_backup(backup_root=backup_root, restore_root=target)

    assert policy_called is False
    assert _tree_bytes(backup_root) == source_before
    assert not target.exists()
    assert not list(tmp_path.glob(".pre-policy-audit-target.partial-*"))

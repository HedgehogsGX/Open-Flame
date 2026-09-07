"""Exact validation plus atomic creation and migration for the upload schema."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .contracts import normalize_tencent_short_title

SCHEMA_VERSION = 3
_SCHEMA_V2_VERSION = 2

LEGACY_SCHEMA_STATEMENTS = (
    "CREATE TABLE metadata(version INTEGER NOT NULL)",
    """CREATE TABLE accounts(
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL,
 auth_state TEXT NOT NULL DEFAULT 'unchecked', code TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, UNIQUE(platform,name))""",
    """CREATE TABLE sources(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, suffix TEXT NOT NULL,
 size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE jobs(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 source_id TEXT NOT NULL REFERENCES sources(id), title TEXT NOT NULL,
 description TEXT NOT NULL, tags TEXT NOT NULL, category_id INTEGER,
 mode TEXT NOT NULL, copyright INTEGER NOT NULL, source_credit TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', code TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 retry_of TEXT REFERENCES jobs(id))""",
    """CREATE TABLE operations(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 action TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE requests(
 id TEXT PRIMARY KEY, digest TEXT NOT NULL, job_ids TEXT NOT NULL)""",
)
LEGACY_SCHEMA_DDL = ";\n".join(LEGACY_SCHEMA_STATEMENTS) + ";"

SCHEMA_V2_TABLE_STATEMENTS = (
    "CREATE TABLE metadata(version INTEGER NOT NULL)",
    """CREATE TABLE \"accounts\"(
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL,
 auth_state TEXT NOT NULL DEFAULT 'unchecked', code TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, lifecycle_state TEXT NOT NULL DEFAULT 'active'
 CHECK(lifecycle_state IN ('active','disconnected')), disconnected_at TEXT)""",
    """CREATE TABLE sources(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, suffix TEXT NOT NULL,
 size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 media_state TEXT NOT NULL DEFAULT 'present'
 CHECK(media_state IN ('present','missing','deleted')), deleted_at TEXT)""",
    """CREATE TABLE jobs(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 source_id TEXT NOT NULL REFERENCES sources(id), title TEXT NOT NULL,
 description TEXT NOT NULL, tags TEXT NOT NULL, category_id INTEGER,
 mode TEXT NOT NULL, copyright INTEGER NOT NULL, source_credit TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', code TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 retry_of TEXT REFERENCES jobs(id))""",
    """CREATE TABLE operations(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 action TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE requests(
 id TEXT PRIMARY KEY, digest TEXT NOT NULL, job_ids TEXT NOT NULL)""",
)
SCHEMA_V2_INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX accounts_active_name ON accounts(platform,name) WHERE lifecycle_state='active'",
    "CREATE INDEX jobs_account_state ON jobs(account_id,state)",
    "CREATE INDEX jobs_source_state ON jobs(source_id,state)",
    "CREATE INDEX operations_account_state ON operations(account_id,state)",
)
SCHEMA_V2_STATEMENTS = SCHEMA_V2_TABLE_STATEMENTS + SCHEMA_V2_INDEX_STATEMENTS
SCHEMA_V2_DDL = ";\n".join(SCHEMA_V2_STATEMENTS) + ";"

SCHEMA_TABLE_STATEMENTS = (
    SCHEMA_V2_TABLE_STATEMENTS[0],
    SCHEMA_V2_TABLE_STATEMENTS[1],
    SCHEMA_V2_TABLE_STATEMENTS[2],
    """CREATE TABLE upload_assets(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind='cover'),
 name TEXT NOT NULL, suffix TEXT NOT NULL, mime_type TEXT NOT NULL,
 size INTEGER NOT NULL, sha256 TEXT NOT NULL, width INTEGER NOT NULL,
 height INTEGER NOT NULL, created_at TEXT NOT NULL,
 media_state TEXT NOT NULL DEFAULT 'present'
 CHECK(media_state IN ('present','missing','deleted')), deleted_at TEXT)""",
    """CREATE TABLE jobs(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 source_id TEXT NOT NULL REFERENCES sources(id), title TEXT NOT NULL,
 description TEXT NOT NULL, tags TEXT NOT NULL, category_id INTEGER,
 mode TEXT NOT NULL, copyright INTEGER NOT NULL, source_credit TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', code TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 retry_of TEXT REFERENCES jobs(id),
 cover_landscape_asset_id TEXT REFERENCES upload_assets(id),
 cover_portrait_asset_id TEXT REFERENCES upload_assets(id),
 publish_at_unix INTEGER, publish_timezone_offset_minutes INTEGER,
 platform_options TEXT NOT NULL DEFAULT '{}')""",
    SCHEMA_V2_TABLE_STATEMENTS[4],
    """CREATE TABLE requests(
 id TEXT PRIMARY KEY, digest TEXT NOT NULL, job_ids TEXT NOT NULL,
 digest_version INTEGER NOT NULL DEFAULT 1)""",
)
SCHEMA_INDEX_STATEMENTS = SCHEMA_V2_INDEX_STATEMENTS + (
    "CREATE INDEX jobs_cover_landscape_state ON jobs(cover_landscape_asset_id,state)",
    "CREATE INDEX jobs_cover_portrait_state ON jobs(cover_portrait_asset_id,state)",
)
SCHEMA_STATEMENTS = SCHEMA_TABLE_STATEMENTS + SCHEMA_INDEX_STATEMENTS
SCHEMA_DDL = ";\n".join(SCHEMA_STATEMENTS) + ";"


class UploadSchemaError(Exception):
    pass


_LEGACY_COLUMNS = {
    "metadata": ((0, "version", "INTEGER", 1, None, 0, 0),),
    "accounts": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "platform", "TEXT", 1, None, 0, 0),
        (2, "name", "TEXT", 1, None, 0, 0),
        (3, "auth_state", "TEXT", 1, "'unchecked'", 0, 0),
        (4, "code", "TEXT", 1, "''", 0, 0),
        (5, "created_at", "TEXT", 1, None, 0, 0),
    ),
    "sources": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "name", "TEXT", 1, None, 0, 0),
        (2, "suffix", "TEXT", 1, None, 0, 0),
        (3, "size", "INTEGER", 1, None, 0, 0),
        (4, "sha256", "TEXT", 1, None, 0, 0),
        (5, "created_at", "TEXT", 1, None, 0, 0),
    ),
    "jobs": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "account_id", "TEXT", 1, None, 0, 0),
        (2, "source_id", "TEXT", 1, None, 0, 0),
        (3, "title", "TEXT", 1, None, 0, 0),
        (4, "description", "TEXT", 1, None, 0, 0),
        (5, "tags", "TEXT", 1, None, 0, 0),
        (6, "category_id", "INTEGER", 0, None, 0, 0),
        (7, "mode", "TEXT", 1, None, 0, 0),
        (8, "copyright", "INTEGER", 1, None, 0, 0),
        (9, "source_credit", "TEXT", 1, None, 0, 0),
        (10, "state", "TEXT", 1, "'draft'", 0, 0),
        (11, "code", "TEXT", 1, "''", 0, 0),
        (12, "created_at", "TEXT", 1, None, 0, 0),
        (13, "updated_at", "TEXT", 1, None, 0, 0),
        (14, "retry_of", "TEXT", 0, None, 0, 0),
    ),
    "operations": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "account_id", "TEXT", 1, None, 0, 0),
        (2, "action", "TEXT", 1, None, 0, 0),
        (3, "state", "TEXT", 1, "'queued'", 0, 0),
        (4, "code", "TEXT", 1, "''", 0, 0),
        (5, "created_at", "TEXT", 1, None, 0, 0),
        (6, "updated_at", "TEXT", 1, None, 0, 0),
    ),
    "requests": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "digest", "TEXT", 1, None, 0, 0),
        (2, "job_ids", "TEXT", 1, None, 0, 0),
    ),
}
_V2_COLUMNS = dict(_LEGACY_COLUMNS)
_V2_COLUMNS["accounts"] = _LEGACY_COLUMNS["accounts"] + (
    (6, "lifecycle_state", "TEXT", 1, "'active'", 0, 0),
    (7, "disconnected_at", "TEXT", 0, None, 0, 0),
)
_V2_COLUMNS["sources"] = _LEGACY_COLUMNS["sources"] + (
    (6, "media_state", "TEXT", 1, "'present'", 0, 0),
    (7, "deleted_at", "TEXT", 0, None, 0, 0),
)
_COLUMNS = {
    "metadata": _V2_COLUMNS["metadata"],
    "accounts": _V2_COLUMNS["accounts"],
    "sources": _V2_COLUMNS["sources"],
    "upload_assets": (
        (0, "id", "TEXT", 0, None, 1, 0),
        (1, "kind", "TEXT", 1, None, 0, 0),
        (2, "name", "TEXT", 1, None, 0, 0),
        (3, "suffix", "TEXT", 1, None, 0, 0),
        (4, "mime_type", "TEXT", 1, None, 0, 0),
        (5, "size", "INTEGER", 1, None, 0, 0),
        (6, "sha256", "TEXT", 1, None, 0, 0),
        (7, "width", "INTEGER", 1, None, 0, 0),
        (8, "height", "INTEGER", 1, None, 0, 0),
        (9, "created_at", "TEXT", 1, None, 0, 0),
        (10, "media_state", "TEXT", 1, "'present'", 0, 0),
        (11, "deleted_at", "TEXT", 0, None, 0, 0),
    ),
    "jobs": _V2_COLUMNS["jobs"] + (
        (15, "cover_landscape_asset_id", "TEXT", 0, None, 0, 0),
        (16, "cover_portrait_asset_id", "TEXT", 0, None, 0, 0),
        (17, "publish_at_unix", "INTEGER", 0, None, 0, 0),
        (18, "publish_timezone_offset_minutes", "INTEGER", 0, None, 0, 0),
        (19, "platform_options", "TEXT", 1, "'{}'", 0, 0),
    ),
    "operations": _V2_COLUMNS["operations"],
    "requests": _V2_COLUMNS["requests"] + (
        (3, "digest_version", "INTEGER", 1, "1", 0, 0),
    ),
}
_COLUMNS_BY_VERSION = {
    1: _LEGACY_COLUMNS,
    _SCHEMA_V2_VERSION: _V2_COLUMNS,
    SCHEMA_VERSION: _COLUMNS,
}

_LEGACY_INDEXES = {
    "metadata": set(),
    "accounts": {
        ("pk", 1, 0, (("id", 0, "BINARY"),)),
        ("u", 1, 0, (("platform", 0, "BINARY"), ("name", 0, "BINARY"))),
    },
    "sources": {("pk", 1, 0, (("id", 0, "BINARY"),))},
    "jobs": {("pk", 1, 0, (("id", 0, "BINARY"),))},
    "operations": {("pk", 1, 0, (("id", 0, "BINARY"),))},
    "requests": {("pk", 1, 0, (("id", 0, "BINARY"),))},
}
_V2_INDEXES = {
    "metadata": set(),
    "accounts": {
        ("pk", 1, 0, (("id", 0, "BINARY"),)),
        ("c", 1, 1, (("platform", 0, "BINARY"), ("name", 0, "BINARY"))),
    },
    "sources": {("pk", 1, 0, (("id", 0, "BINARY"),))},
    "jobs": {
        ("pk", 1, 0, (("id", 0, "BINARY"),)),
        ("c", 0, 0, (("account_id", 0, "BINARY"), ("state", 0, "BINARY"))),
        ("c", 0, 0, (("source_id", 0, "BINARY"), ("state", 0, "BINARY"))),
    },
    "operations": {
        ("pk", 1, 0, (("id", 0, "BINARY"),)),
        ("c", 0, 0, (("account_id", 0, "BINARY"), ("state", 0, "BINARY"))),
    },
    "requests": {("pk", 1, 0, (("id", 0, "BINARY"),))},
}
_INDEXES = dict(_V2_INDEXES)
_INDEXES["upload_assets"] = {("pk", 1, 0, (("id", 0, "BINARY"),))}
_INDEXES["jobs"] = _V2_INDEXES["jobs"] | {
    ("c", 0, 0, (("cover_landscape_asset_id", 0, "BINARY"), ("state", 0, "BINARY"))),
    ("c", 0, 0, (("cover_portrait_asset_id", 0, "BINARY"), ("state", 0, "BINARY"))),
}
_INDEXES_BY_VERSION = {
    1: _LEGACY_INDEXES,
    _SCHEMA_V2_VERSION: _V2_INDEXES,
    SCHEMA_VERSION: _INDEXES,
}
_V2_FOREIGN_KEYS = {
    "metadata": set(),
    "accounts": set(),
    "sources": set(),
    "jobs": {
        ("accounts", "account_id", "id", "NO ACTION", "NO ACTION", "NONE"),
        ("sources", "source_id", "id", "NO ACTION", "NO ACTION", "NONE"),
        ("jobs", "retry_of", "id", "NO ACTION", "NO ACTION", "NONE"),
    },
    "operations": {
        ("accounts", "account_id", "id", "NO ACTION", "NO ACTION", "NONE"),
    },
    "requests": set(),
}
_FOREIGN_KEYS = dict(_V2_FOREIGN_KEYS)
_FOREIGN_KEYS["upload_assets"] = set()
_FOREIGN_KEYS["jobs"] = _V2_FOREIGN_KEYS["jobs"] | {
    ("upload_assets", "cover_landscape_asset_id", "id", "NO ACTION", "NO ACTION", "NONE"),
    ("upload_assets", "cover_portrait_asset_id", "id", "NO ACTION", "NO ACTION", "NONE"),
}
_FOREIGN_KEYS_BY_VERSION = {
    1: _V2_FOREIGN_KEYS,
    _SCHEMA_V2_VERSION: _V2_FOREIGN_KEYS,
    SCHEMA_VERSION: _FOREIGN_KEYS,
}


def _normalized_sql(statement: str) -> str:
    normalized: list[str] = []
    in_literal = False
    offset = 0
    while offset < len(statement):
        char = statement[offset]
        if char == "'":
            normalized.append(char)
            if in_literal and offset + 1 < len(statement) and statement[offset + 1] == "'":
                normalized.append("'")
                offset += 2
                continue
            in_literal = not in_literal
        elif in_literal:
            normalized.append(char)
        elif not char.isspace():
            normalized.append(char.lower())
        offset += 1
    return "".join(normalized)


_LEGACY_CANONICAL_SQL = {
    name: _normalized_sql(statement)
    for name, statement in zip(_LEGACY_COLUMNS, LEGACY_SCHEMA_STATEMENTS, strict=True)
}
_V2_CANONICAL_SQL = {
    name: _normalized_sql(statement)
    for name, statement in zip(_V2_COLUMNS, SCHEMA_V2_TABLE_STATEMENTS, strict=True)
}
_CANONICAL_SQL = {
    name: _normalized_sql(statement)
    for name, statement in zip(_COLUMNS, SCHEMA_TABLE_STATEMENTS, strict=True)
}
_CANONICAL_SQL_BY_VERSION = {
    1: _LEGACY_CANONICAL_SQL,
    _SCHEMA_V2_VERSION: _V2_CANONICAL_SQL,
    SCHEMA_VERSION: _CANONICAL_SQL,
}
_EXPLICIT_INDEX_SQL_BY_VERSION = {
    1: {},
    _SCHEMA_V2_VERSION: {
        name: (owner, _normalized_sql(statement))
        for name, owner, statement in (
            ("accounts_active_name", "accounts", SCHEMA_V2_INDEX_STATEMENTS[0]),
            ("jobs_account_state", "jobs", SCHEMA_V2_INDEX_STATEMENTS[1]),
            ("jobs_source_state", "jobs", SCHEMA_V2_INDEX_STATEMENTS[2]),
            ("operations_account_state", "operations", SCHEMA_V2_INDEX_STATEMENTS[3]),
        )
    },
    SCHEMA_VERSION: {
        name: (owner, _normalized_sql(statement))
        for name, owner, statement in (
            ("accounts_active_name", "accounts", SCHEMA_INDEX_STATEMENTS[0]),
            ("jobs_account_state", "jobs", SCHEMA_INDEX_STATEMENTS[1]),
            ("jobs_source_state", "jobs", SCHEMA_INDEX_STATEMENTS[2]),
            ("operations_account_state", "operations", SCHEMA_INDEX_STATEMENTS[3]),
            ("jobs_cover_landscape_state", "jobs", SCHEMA_INDEX_STATEMENTS[4]),
            ("jobs_cover_portrait_state", "jobs", SCHEMA_INDEX_STATEMENTS[5]),
        )
    },
}
_INITIALIZE_LOCK = threading.Lock()
_SNAPSHOT_ATTEMPTS = 5
_SNAPSHOT_RETRY_SECONDS = 0.01
_SNAPSHOT_CHUNK_BYTES = 1024 * 1024
_WAL_MAGIC_LITTLE_CHECKSUM = 0x377F0682
_WAL_MAGIC_BIG_CHECKSUM = 0x377F0683
_WAL_FORMAT_VERSION = 3_007_000
_UINT32_MASK = (1 << 32) - 1
_SCHEMA_LOCK_TIMEOUT_SECONDS = 30.0
_SCHEMA_LOCK_POLL_SECONDS = 0.025
_SCHEMA_LOCK_OFFSET = (1 << 63) - 4096
_PUBLISH_LINK_TIMEOUT_SECONDS = 2.0
_LEGACY_CATEGORY_NON_BILIBILI_PLATFORMS = frozenset({"douyin", "tencent"})
_MIGRATION_V1_TO_V2 = (
    """CREATE TABLE accounts_v2(
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL,
 auth_state TEXT NOT NULL DEFAULT 'unchecked', code TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, lifecycle_state TEXT NOT NULL DEFAULT 'active'
 CHECK(lifecycle_state IN ('active','disconnected')), disconnected_at TEXT)""",
    """INSERT INTO accounts_v2(id,platform,name,auth_state,code,created_at)
 SELECT id,platform,name,auth_state,code,created_at FROM accounts""",
    "DROP TABLE accounts",
    "ALTER TABLE accounts_v2 RENAME TO accounts",
    SCHEMA_V2_INDEX_STATEMENTS[0],
    """ALTER TABLE sources ADD COLUMN media_state TEXT NOT NULL DEFAULT 'present'
 CHECK(media_state IN ('present','missing','deleted'))""",
    "ALTER TABLE sources ADD COLUMN deleted_at TEXT",
    SCHEMA_V2_INDEX_STATEMENTS[1],
    SCHEMA_V2_INDEX_STATEMENTS[2],
    SCHEMA_V2_INDEX_STATEMENTS[3],
    "UPDATE metadata SET version=2",
)
_MIGRATION_V2_TO_V3 = (
    SCHEMA_TABLE_STATEMENTS[3],
    "ALTER TABLE jobs ADD COLUMN cover_landscape_asset_id TEXT REFERENCES upload_assets(id)",
    "ALTER TABLE jobs ADD COLUMN cover_portrait_asset_id TEXT REFERENCES upload_assets(id)",
    "ALTER TABLE jobs ADD COLUMN publish_at_unix INTEGER",
    "ALTER TABLE jobs ADD COLUMN publish_timezone_offset_minutes INTEGER",
    "ALTER TABLE jobs ADD COLUMN platform_options TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE requests ADD COLUMN digest_version INTEGER NOT NULL DEFAULT 1",
    SCHEMA_INDEX_STATEMENTS[4],
    SCHEMA_INDEX_STATEMENTS[5],
    "UPDATE metadata SET version=3",
)


class _SnapshotChanged(Exception):
    pass


def _try_lock_schema_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        # Windows byte-range locks are mandatory. Keep the coordination byte
        # outside both database content and SQLite's own 1 GiB lock range.
        handle.seek(_SCHEMA_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_schema_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(_SCHEMA_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_schema_access(path: Path):
    """Serialize validation plus migration without writing a sidecar lock file."""
    handle = None
    acquired = False
    try:
        deadline = time.monotonic() + _PUBLISH_LINK_TIMEOUT_SECONDS
        while True:
            try:
                before = _require_plain_database(path)
                break
            except UploadSchemaError:
                info = path.lstat()
                publisher = False
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    for candidate in path.parent.glob(f".{path.name}.*.tmp"):
                        try:
                            linked = candidate.lstat()
                        except OSError:
                            continue
                        if (linked.st_dev, linked.st_ino) == (info.st_dev, info.st_ino):
                            publisher = True
                            break
                    if not publisher:
                        try:
                            before = _require_plain_database(path)
                        except UploadSchemaError:
                            pass
                        else:
                            break
                if not publisher or time.monotonic() >= deadline:
                    raise
                time.sleep(_SCHEMA_LOCK_POLL_SECONDS)
        handle = path.open("rb")
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise UploadSchemaError
        deadline = time.monotonic() + _SCHEMA_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _try_lock_schema_file(handle)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise UploadSchemaError from None
                time.sleep(_SCHEMA_LOCK_POLL_SECONDS)
        current = _require_plain_database(path)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise UploadSchemaError
    except (OSError, UploadSchemaError):
        if acquired and handle is not None:
            try:
                _unlock_schema_file(handle)
            except OSError:
                pass
        if handle is not None:
            handle.close()
        raise UploadSchemaError from None
    try:
        yield
    finally:
        try:
            _unlock_schema_file(handle)
        finally:
            handle.close()


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _require_plain_database(path: Path) -> os.stat_result:
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse or info.st_nlink != 1):
        raise UploadSchemaError
    return info


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _fingerprint_file(
    path: Path,
    copy_to: Path | None = None,
) -> tuple[tuple[int, ...], bytes]:
    before = _require_plain_database(path)
    digest = sha256()
    total = 0
    destination = copy_to.open("xb") if copy_to is not None else None
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if _file_identity(opened) != _file_identity(before):
                raise _SnapshotChanged
            remaining = before.st_size
            while remaining:
                chunk = source.read(min(_SNAPSHOT_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _SnapshotChanged
                total += len(chunk)
                remaining -= len(chunk)
                digest.update(chunk)
                if destination is not None:
                    destination.write(chunk)
            if source.read(1):
                raise _SnapshotChanged
            after = os.fstat(source.fileno())
        if total != before.st_size or _file_identity(after) != _file_identity(before):
            raise _SnapshotChanged
        current = _require_plain_database(path)
        if _file_identity(current) != _file_identity(before):
            raise _SnapshotChanged
    finally:
        if destination is not None:
            destination.close()
    return _file_identity(before), digest.digest()


def _sidecar(path: Path, suffix: str) -> Path:
    return Path(str(path) + suffix)


def _present(path: Path) -> bool:
    return os.path.lexists(path)


def _copy_stable_snapshot(path: Path, destination: Path) -> None:
    wal = _sidecar(path, "-wal")
    shm = _sidecar(path, "-shm")
    journal = _sidecar(path, "-journal")
    wal_present = _present(wal)
    shm_present = _present(shm)
    if _present(journal):
        raise UploadSchemaError
    if shm_present and not wal_present:
        raise _SnapshotChanged
    try:
        if wal_present:
            _require_plain_database(wal)
        if shm_present:
            _require_plain_database(shm)
    except FileNotFoundError:
        raise _SnapshotChanged from None

    expected = {path: _fingerprint_file(path, destination)}
    try:
        if wal_present:
            expected[wal] = _fingerprint_file(wal, _sidecar(destination, "-wal"))
        if shm_present:
            expected[shm] = _fingerprint_file(shm)
    except FileNotFoundError:
        raise _SnapshotChanged from None

    if (_present(wal), _present(shm), _present(journal)) != (
        wal_present,
        shm_present,
        False,
    ):
        raise _SnapshotChanged
    for source, fingerprint in expected.items():
        try:
            current = _fingerprint_file(source)
        except FileNotFoundError:
            if source != path:
                raise _SnapshotChanged from None
            raise
        if current != fingerprint:
            raise _SnapshotChanged
    if (_present(wal), _present(shm), _present(journal)) != (
        wal_present,
        shm_present,
        False,
    ):
        raise _SnapshotChanged


def _wal_checksum(
    payload: bytes,
    *,
    byteorder: str,
    initial: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    if len(payload) % 8:
        raise UploadSchemaError
    first, second = initial
    for offset in range(0, len(payload), 8):
        left = int.from_bytes(payload[offset:offset + 4], byteorder)
        right = int.from_bytes(payload[offset + 4:offset + 8], byteorder)
        first = (first + left + second) & _UINT32_MASK
        second = (second + right + first) & _UINT32_MASK
    return first, second


def _validate_snapshot_wal(database: Path) -> None:
    # SQLite WAL header, frame checksum, reset-tail and commit-prefix semantics:
    # https://sqlite.org/fileformat2.html#walformat
    wal = _sidecar(database, "-wal")
    if not wal.exists():
        return
    with database.open("rb") as handle:
        database_header = handle.read(100)
    if len(database_header) != 100 or database_header[:16] != b"SQLite format 3\x00":
        raise UploadSchemaError
    raw_page_size = int.from_bytes(database_header[16:18], "big")
    database_page_size = 65536 if raw_page_size == 1 else raw_page_size
    if database_header[18:20] != b"\x02\x02":
        raise UploadSchemaError

    wal_size = wal.stat().st_size
    if wal_size == 0:
        return
    if wal_size < 32:
        raise UploadSchemaError
    with wal.open("rb") as handle:
        header = handle.read(32)
        magic = int.from_bytes(header[:4], "big")
        if magic == _WAL_MAGIC_LITTLE_CHECKSUM:
            checksum_byteorder = "little"
        elif magic == _WAL_MAGIC_BIG_CHECKSUM:
            checksum_byteorder = "big"
        else:
            raise UploadSchemaError
        if (int.from_bytes(header[4:8], "big") != _WAL_FORMAT_VERSION
                or int.from_bytes(header[8:12], "big") != database_page_size
                or database_page_size < 512
                or database_page_size > 65536
                or database_page_size & (database_page_size - 1)):
            raise UploadSchemaError
        checksum = _wal_checksum(header[:24], byteorder=checksum_byteorder)
        stored_header_checksum = (
            int.from_bytes(header[24:28], "big"),
            int.from_bytes(header[28:32], "big"),
        )
        if checksum != stored_header_checksum:
            raise UploadSchemaError

        salt = header[16:24]
        complete_frames = (wal_size - 32) // (24 + database_page_size)
        for _ in range(complete_frames):
            frame_header = handle.read(24)
            page = handle.read(database_page_size)
            if len(frame_header) != 24 or len(page) != database_page_size:
                raise UploadSchemaError
            frame_checksum = _wal_checksum(
                frame_header[:8] + page,
                byteorder=checksum_byteorder,
                initial=checksum,
            )
            stored_frame_checksum = (
                int.from_bytes(frame_header[16:20], "big"),
                int.from_bytes(frame_header[20:24], "big"),
            )
            # SQLite reads only the valid prefix through its last commit frame.
            # Salt/checksum mismatches and partial bytes can be a normal reset or
            # interrupted tail, so the SQLite reader below decides what is visible.
            if (frame_header[8:16] != salt
                    or int.from_bytes(frame_header[:4], "big") == 0
                    or frame_checksum != stored_frame_checksum):
                break
            checksum = frame_checksum


def _metadata_version(db: sqlite3.Connection) -> int:
    versions = [tuple(row) for row in db.execute(
        "SELECT version,typeof(version) FROM metadata"
    )]
    if len(versions) != 1 or versions[0][1] != "integer":
        raise UploadSchemaError
    return int(versions[0][0])


def _validate_connection(db: sqlite3.Connection, version: int) -> None:
    if version not in _COLUMNS_BY_VERSION:
        raise UploadSchemaError
    expected_columns_by_table = _COLUMNS_BY_VERSION[version]
    expected_indexes_by_table = _INDEXES_BY_VERSION[version]
    expected_foreign_keys = _FOREIGN_KEYS_BY_VERSION[version]
    expected_table_sql = _CANONICAL_SQL_BY_VERSION[version]
    expected_explicit_indexes = _EXPLICIT_INDEX_SQL_BY_VERSION[version]
    if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
        raise UploadSchemaError
    objects = [tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
    )]
    tables = {name: sql for kind, name, _owner, sql in objects if kind == "table"}
    if set(tables) != set(expected_columns_by_table):
        raise UploadSchemaError
    if any(kind not in {"table", "index"} for kind, *_ in objects):
        raise UploadSchemaError
    explicit_indexes = {}
    for kind, name, owner, sql in objects:
        if kind != "index":
            continue
        if sql is None:
            if not name.startswith("sqlite_autoindex_"):
                raise UploadSchemaError
        else:
            explicit_indexes[name] = (owner, _normalized_sql(sql))
    if explicit_indexes != expected_explicit_indexes:
        raise UploadSchemaError
    for table, expected_columns in expected_columns_by_table.items():
        sql = tables[table]
        if not isinstance(sql, str) or _normalized_sql(sql) != expected_table_sql[table]:
            raise UploadSchemaError
        table_list = [tuple(row) for row in db.execute("PRAGMA table_list")
                      if row[0] == "main" and row[1] == table]
        if table_list != [("main", table, "table", len(expected_columns), 0, 0)]:
            raise UploadSchemaError
        columns = [tuple(row) for row in db.execute(f"PRAGMA table_xinfo({_quoted(table)})")]
        if columns != list(expected_columns):
            raise UploadSchemaError
        indexes = set()
        for index in db.execute(f"PRAGMA index_list({_quoted(table)})"):
            _seq, name, unique, origin, partial = tuple(index)
            key_columns = tuple((row[2], row[3], row[4]) for row in
                                db.execute(f"PRAGMA index_xinfo({_quoted(name)})") if row[5] == 1)
            indexes.add((origin, unique, partial, key_columns))
        if indexes != expected_indexes_by_table[table]:
            raise UploadSchemaError
        foreign_keys = {
            (row[2], row[3], row[4], row[5], row[6], row[7])
            for row in db.execute(f"PRAGMA foreign_key_list({_quoted(table)})")
        }
        if foreign_keys != expected_foreign_keys[table]:
            raise UploadSchemaError
    if _metadata_version(db) != version:
        raise UploadSchemaError
    if list(db.execute("PRAGMA foreign_key_check")):
        raise UploadSchemaError


def _validated_schema_version(path: Path, allowed_versions: frozenset[int]) -> int:
    """Return an exact known version without opening the source database."""
    try:
        resolved = Path(os.path.abspath(path))
        for attempt in range(_SNAPSHOT_ATTEMPTS):
            try:
                with tempfile.TemporaryDirectory(prefix="open-flame-upload-schema-") as temporary:
                    snapshot = Path(temporary) / "uploads.sqlite3"
                    _copy_stable_snapshot(resolved, snapshot)
                    _validate_snapshot_wal(snapshot)
                    db = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True, timeout=1)
                    try:
                        db.execute("PRAGMA query_only=ON")
                        db.execute("BEGIN")
                        version = _metadata_version(db)
                        if version not in allowed_versions:
                            raise UploadSchemaError
                        _validate_connection(db, version)
                    finally:
                        db.close()
                return version
            except _SnapshotChanged:
                if attempt + 1 == _SNAPSHOT_ATTEMPTS:
                    raise UploadSchemaError from None
                time.sleep(_SNAPSHOT_RETRY_SECONDS)
    except (OSError, sqlite3.Error, UploadSchemaError):
        raise UploadSchemaError from None
    raise UploadSchemaError


def validate_upload_schema(path: Path) -> None:
    """Validate an existing latest-version database without touching its files."""
    _validated_schema_version(path, frozenset({SCHEMA_VERSION}))


def _legacy_request_digest(payload: list[object]) -> str | None:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError):
        return None
    return sha256(encoded).hexdigest()


def _normalize_v1_legacy_category_digests(db: sqlite3.Connection) -> None:
    """Canonicalize only provable pre-Schema-2 category affinity histories.

    Schema 1 accepted direct-service ``bool``, integer-valued ``float``, and
    canonical decimal ``str`` category values for Douyin and Tencent.  SQLite
    stored those values as integers while the request digest retained their
    JSON type.  Only valid-range integer storage and those three exact legacy
    serializations are eligible here.  ``False`` (zero), out-of-range values,
    non-integral floats, and non-canonical strings are deliberately untouched.
    """

    jobs = {
        row["id"]: row
        for row in db.execute(
            "SELECT id,account_id,source_id,title,description,tags,category_id,"
            "mode,copyright,source_credit,retry_of FROM jobs"
        )
    }
    account_platforms = dict(db.execute("SELECT id,platform FROM accounts"))
    requests = list(db.execute("SELECT id,digest,job_ids FROM requests"))
    decoded_requests: list[tuple[sqlite3.Row, list[str]]] = []
    job_request_counts: dict[str, int] = {}
    for request in requests:
        if not isinstance(request["job_ids"], str):
            continue
        try:
            job_ids = json.loads(request["job_ids"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(job_ids, list)
            or not 1 <= len(job_ids) <= 20
            or any(not isinstance(job_id, str) for job_id in job_ids)
            or len(job_ids) != len(set(job_ids))
        ):
            continue
        decoded_requests.append((request, job_ids))
        for job_id in job_ids:
            job_request_counts[job_id] = job_request_counts.get(job_id, 0) + 1

    updates: list[tuple[str, object, object]] = []
    shared_fields = (
        "source_id",
        "title",
        "description",
        "category_id",
        "mode",
        "copyright",
        "source_credit",
    )
    for request, job_ids in decoded_requests:
        if (
            not isinstance(request["id"], str)
            or not isinstance(request["digest"], str)
            or any(job_request_counts[job_id] != 1 for job_id in job_ids)
            or any(job_id not in jobs for job_id in job_ids)
        ):
            continue
        request_jobs = [jobs[job_id] for job_id in job_ids]
        first = request_jobs[0]
        account_ids = [job["account_id"] for job in request_jobs]
        if (
            any(job["retry_of"] is not None for job in request_jobs)
            or any(not isinstance(account_id, str) for account_id in account_ids)
            or len(account_ids) != len(set(account_ids))
            or any(account_id not in account_platforms for account_id in account_ids)
            or not {
                account_platforms[account_id] for account_id in account_ids
            }.issubset(_LEGACY_CATEGORY_NON_BILIBILI_PLATFORMS)
            or any(
                job[field] != first[field]
                for job in request_jobs[1:]
                for field in shared_fields
            )
        ):
            continue
        try:
            if any(not isinstance(job["tags"], str) for job in request_jobs):
                continue
            tags = [json.loads(job["tags"]) for job in request_jobs]
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(tags[0], list)
            or any(value != tags[0] for value in tags[1:])
        ):
            continue
        category_id = first["category_id"]
        copyright_value = first["copyright"]
        if (
            type(category_id) is not int
            or not 1 <= category_id <= 10000
            or type(copyright_value) is not int
            or copyright_value not in {1, 2}
        ):
            continue
        copyright_inputs: list[int | None] = [copyright_value]
        if copyright_value == 1:
            copyright_inputs.append(None)
        base_payload = [
            first["source_id"],
            sorted(account_ids),
            first["title"],
            first["description"],
            tags[0],
            category_id,
            first["mode"],
            copyright_value,
            first["source_credit"],
        ]
        canonical_digests: dict[int | None, str] = {}
        for copyright_input in copyright_inputs:
            payload = [*base_payload]
            payload[7] = copyright_input
            digest = _legacy_request_digest(payload)
            if digest is not None:
                canonical_digests[copyright_input] = digest
        if request["digest"] in canonical_digests.values():
            continue

        legacy_categories: list[object] = [float(category_id), str(category_id)]
        if category_id == 1:
            legacy_categories.append(True)
        matches: list[str] = []
        for copyright_input, canonical_digest in canonical_digests.items():
            for legacy_category in legacy_categories:
                payload = [*base_payload]
                payload[5] = legacy_category
                payload[7] = copyright_input
                if _legacy_request_digest(payload) == request["digest"]:
                    matches.append(canonical_digest)
        if len(matches) == 1:
            updates.append((matches[0], request["id"], request["digest"]))

    for digest, request_id, previous_digest in updates:
        cursor = db.execute(
            "UPDATE requests SET digest=? WHERE id=? AND digest=?",
            (digest, request_id, previous_digest),
        )
        if cursor.rowcount != 1:
            raise UploadSchemaError


def _v3_tag_text(value: object) -> str:
    if not isinstance(value, str):
        raise UploadSchemaError
    normalized = value.strip()
    while normalized[:1] in {"#", "＃"}:
        normalized = normalized[1:].lstrip()
    normalized = normalized.replace("#", "井").replace("＃", "井").replace("，", "、")
    # Schema 2 accepted a tag consisting only of a hash marker. Keep a visible,
    # inert representation rather than turning it into an empty Bilibili tag.
    return normalized or "井"


def _normalize_v2_legacy_tags(db: sqlite3.Connection) -> None:
    """Make Schema-2 tags safe for adapters that add their own hash marker.

    Schema 2 allowed ``#``, ``＃`` and ``，`` inside persisted tags. Schema 3
    treats those characters as syntax. Normalize only during the exact 2→3
    transaction, revoke queued work, and update affected v1 request digests so
    backup and idempotent replay continue to describe the stored jobs.
    """

    jobs = list(db.execute(
        "SELECT id,account_id,source_id,title,description,tags,category_id,mode,"
        "copyright,source_credit,state FROM jobs"
    ))
    old_tags: dict[str, list[str]] = {}
    new_tags: dict[str, list[str]] = {}
    changed: set[str] = set()
    for job in jobs:
        raw_tags = job["tags"]
        if not isinstance(raw_tags, str):
            raise UploadSchemaError
        try:
            decoded = json.loads(raw_tags)
        except (TypeError, json.JSONDecodeError) as exc:
            raise UploadSchemaError from exc
        if (
            not isinstance(decoded, list)
            or len(decoded) > 10
            or any(not isinstance(tag, str) for tag in decoded)
        ):
            raise UploadSchemaError
        normalized: list[str] = []
        for tag in decoded:
            candidate = _v3_tag_text(tag)
            if len(candidate) > 20:
                raise UploadSchemaError
            if candidate not in normalized:
                normalized.append(candidate)
        old_tags[job["id"]] = decoded
        new_tags[job["id"]] = normalized
        if normalized != decoded:
            changed.add(job["id"])

    jobs_by_id = {job["id"]: job for job in jobs}
    decoded_requests: list[tuple[sqlite3.Row, list[str], set[str]]] = []
    for request in db.execute("SELECT id,digest,job_ids FROM requests"):
        raw_job_ids = request["job_ids"]
        if not isinstance(raw_job_ids, str):
            raise UploadSchemaError
        try:
            job_ids = json.loads(raw_job_ids)
        except (TypeError, json.JSONDecodeError) as exc:
            raise UploadSchemaError from exc
        if (
            not isinstance(job_ids, list)
            or not 1 <= len(job_ids) <= 20
            or any(not isinstance(job_id, str) for job_id in job_ids)
        ):
            raise UploadSchemaError
        job_id_set = set(job_ids)
        if len(job_ids) != len(job_id_set) or any(
            job_id not in jobs_by_id for job_id in job_ids
        ):
            raise UploadSchemaError
        decoded_requests.append((request, job_ids, job_id_set))

    if not changed:
        return

    account_platforms = dict(db.execute("SELECT id,platform FROM accounts"))
    for request, job_ids, job_id_set in decoded_requests:
        if not job_id_set & changed:
            continue
        request_jobs = [jobs_by_id[job_id] for job_id in job_ids]
        first = request_jobs[0]
        shared = (
            "source_id", "title", "description", "category_id", "mode",
            "copyright", "source_credit",
        )
        if (
            any(job[field] != first[field] for job in request_jobs[1:] for field in shared)
            or any(old_tags[job["id"]] != old_tags[first["id"]] for job in request_jobs[1:])
            or any(new_tags[job["id"]] != new_tags[first["id"]] for job in request_jobs[1:])
        ):
            raise UploadSchemaError
        account_ids = [job["account_id"] for job in request_jobs]
        if (
            len(account_ids) != len(set(account_ids))
            or any(account_id not in account_platforms for account_id in account_ids)
        ):
            raise UploadSchemaError
        copyright_inputs: list[int | None] = [first["copyright"]]
        if (
            first["copyright"] == 1
            and "bilibili" not in {account_platforms[account_id] for account_id in account_ids}
        ):
            copyright_inputs.append(None)
        matched: list[int | None] = []
        for copyright_input in copyright_inputs:
            payload = [
                first["source_id"], sorted(account_ids), first["title"],
                first["description"], old_tags[first["id"]], first["category_id"],
                first["mode"], copyright_input, first["source_credit"],
            ]
            if _legacy_request_digest(payload) == request["digest"]:
                matched.append(copyright_input)
        if len(matched) != 1:
            raise UploadSchemaError
        payload = [
            first["source_id"], sorted(account_ids), first["title"],
            first["description"], new_tags[first["id"]], first["category_id"],
            first["mode"], matched[0], first["source_credit"],
        ]
        digest = _legacy_request_digest(payload)
        if digest is None:
            raise UploadSchemaError
        db.execute("UPDATE requests SET digest=? WHERE id=?", (digest, request["id"]))

    for job in jobs:
        if job["id"] not in changed:
            continue
        state = job["state"]
        next_state = "unknown" if state == "running" else "draft" if state == "queued" else state
        code = (
            "legacy_tags_normalized_review_required"
            if state in {"draft", "queued", "running"}
            else None
        )
        if code is None:
            db.execute(
                "UPDATE jobs SET tags=? WHERE id=?",
                (json.dumps(new_tags[job["id"]], ensure_ascii=False, separators=(",", ":")), job["id"]),
            )
        else:
            db.execute(
                "UPDATE jobs SET tags=?,state=?,code=? WHERE id=?",
                (
                    json.dumps(new_tags[job["id"]], ensure_ascii=False, separators=(",", ":")),
                    next_state,
                    code,
                    job["id"],
                ),
            )


def _normalize_v2_bilibili_original_sources(db: sqlite3.Connection) -> None:
    """Remove a legacy repost source from Bilibili original submissions.

    Schema 2 accepted this contradictory pair and included it in the v1 request
    digest. Schema 3 rejects it, so update each complete root request as one
    unit and revoke any confirmation that could otherwise execute unseen data.
    """

    account_platforms = dict(db.execute("SELECT id,platform FROM accounts"))
    jobs = {
        row["id"]: row
        for row in db.execute(
            "SELECT id,account_id,source_id,title,description,tags,category_id,"
            "mode,copyright,source_credit,state,retry_of FROM jobs"
        )
    }
    affected = {
        job_id
        for job_id, job in jobs.items()
        if account_platforms.get(job["account_id"]) == "bilibili"
        and job["copyright"] == 1
        and isinstance(job["source_credit"], str)
        and bool(job["source_credit"])
    }
    if not affected:
        return

    normalized_ids: set[str] = set()
    for request in db.execute("SELECT id,digest,job_ids FROM requests"):
        try:
            job_ids = json.loads(request["job_ids"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UploadSchemaError from exc
        if (
            not isinstance(job_ids, list)
            or not 1 <= len(job_ids) <= 20
            or any(not isinstance(job_id, str) for job_id in job_ids)
        ):
            raise UploadSchemaError
        if not set(job_ids) & affected:
            continue
        if (
            any(job_id not in jobs for job_id in job_ids)
            or len(job_ids) != len(set(job_ids))
        ):
            raise UploadSchemaError
        request_jobs = [jobs[job_id] for job_id in job_ids]
        first = request_jobs[0]
        shared = (
            "source_id", "title", "description", "tags", "category_id",
            "mode", "copyright", "source_credit",
        )
        if any(
            job[field] != first[field]
            for job in request_jobs[1:]
            for field in shared
        ):
            raise UploadSchemaError
        try:
            tags = json.loads(first["tags"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UploadSchemaError from exc
        old_payload = [
            first["source_id"], sorted(job["account_id"] for job in request_jobs),
            first["title"], first["description"], tags, first["category_id"],
            first["mode"], first["copyright"], first["source_credit"],
        ]
        if _legacy_request_digest(old_payload) != request["digest"]:
            raise UploadSchemaError
        new_payload = [*old_payload]
        new_payload[8] = ""
        new_digest = _legacy_request_digest(new_payload)
        if new_digest is None:
            raise UploadSchemaError
        db.execute("UPDATE requests SET digest=? WHERE id=?", (new_digest, request["id"]))
        db.executemany(
            "UPDATE jobs SET source_credit='' WHERE id=?",
            ((job_id,) for job_id in job_ids),
        )
        normalized_ids.update(job_ids)

    # Retries are intentionally outside requests.  A request stores one shared
    # v1 payload, so changing that payload changed every root job in the
    # request.  Carry the same normalization through every retry descendant,
    # including descendants for non-Bilibili targets in a mixed request, so
    # retry snapshots remain identical to their parents.
    normalized_lineage = set(normalized_ids)
    while True:
        descendants = {
            job_id
            for job_id, job in jobs.items()
            if job["retry_of"] in normalized_lineage
        }
        expanded = normalized_lineage | descendants
        if expanded == normalized_lineage:
            break
        normalized_lineage = expanded
    if not affected <= normalized_lineage:
        raise UploadSchemaError
    for job_id in normalized_lineage:
        job = jobs[job_id]
        db.execute("UPDATE jobs SET source_credit='' WHERE id=?", (job_id,))
        if job["state"] in {"draft", "queued", "running"}:
            next_state = (
                "unknown" if job["state"] == "running"
                else "draft" if job["state"] == "queued"
                else job["state"]
            )
            db.execute(
                "UPDATE jobs SET state=?,code=? WHERE id=?",
                (next_state, "legacy_bilibili_source_credit_removed", job_id),
            )


def _populate_v3_legacy_platform_options(db: sqlite3.Connection) -> None:
    """Persist pinned upstream implicit defaults as reviewable Schema-3 data."""

    rows = list(db.execute(
        "SELECT j.id,j.title,j.state,a.platform FROM jobs j "
        "JOIN accounts a ON a.id=j.account_id"
    ))
    for row in rows:
        if row["platform"] == "douyin":
            values = {"declaration": "内容由AI生成"}
        elif row["platform"] == "tencent":
            if not isinstance(row["title"], str):
                raise UploadSchemaError
            values = {
                "content_label": "含AI生成内容",
                "short_title": normalize_tencent_short_title(row["title"]),
            }
        else:
            continue
        options = json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        state = row["state"]
        next_state = "unknown" if state == "running" else "draft" if state == "queued" else state
        if state in {"draft", "queued", "running"}:
            db.execute(
                "UPDATE jobs SET platform_options=?,state=?,code=? WHERE id=?",
                (options, next_state, "legacy_platform_options_review_required", row["id"]),
            )
        else:
            db.execute(
                "UPDATE jobs SET platform_options=? WHERE id=?",
                (options, row["id"]),
            )


def _preserve_v3_legacy_review_state_reasons(
    db: sqlite3.Connection,
    original_job_states: dict[object, object],
) -> None:
    """Keep restart/interruption meaning after a legacy metadata migration."""

    review_codes = {
        "legacy_tags_normalized_review_required",
        "legacy_platform_options_review_required",
        "legacy_bilibili_source_credit_removed",
    }
    for job_id, original_state in original_job_states.items():
        if original_state not in {"queued", "running"}:
            continue
        current = db.execute(
            "SELECT code FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if current is None or current["code"] not in review_codes:
            continue
        if original_state == "running":
            state = "unknown"
            code = "legacy_metadata_interrupted_result_unknown"
        else:
            state = "draft"
            code = "legacy_metadata_restart_confirmation_required"
        db.execute(
            "UPDATE jobs SET state=?,code=? WHERE id=?",
            (state, code, job_id),
        )


def _migrate_to_latest(path: Path) -> None:
    before = _require_plain_database(path)
    db = None
    committed = False
    try:
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        current = _require_plain_database(path)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise UploadSchemaError
        version = _metadata_version(db)
        if version == 1:
            _validate_connection(db, 1)
            for statement in _MIGRATION_V1_TO_V2:
                db.execute(statement)
            _normalize_v1_legacy_category_digests(db)
            _validate_connection(db, _SCHEMA_V2_VERSION)
            version = _SCHEMA_V2_VERSION
        if version == _SCHEMA_V2_VERSION:
            _validate_connection(db, _SCHEMA_V2_VERSION)
            original_job_states = dict(db.execute("SELECT id,state FROM jobs"))
            _normalize_v2_legacy_tags(db)
            _normalize_v2_bilibili_original_sources(db)
            for statement in _MIGRATION_V2_TO_V3:
                db.execute(statement)
            _populate_v3_legacy_platform_options(db)
            _preserve_v3_legacy_review_state_reasons(db, original_job_states)
            _validate_connection(db, SCHEMA_VERSION)
            version = SCHEMA_VERSION
        if version == SCHEMA_VERSION:
            _validate_connection(db, SCHEMA_VERSION)
        else:
            raise UploadSchemaError
        db.commit()
        committed = True
    except (OSError, sqlite3.Error, UploadSchemaError):
        if db is not None and not committed:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
        raise UploadSchemaError from None
    finally:
        if db is not None:
            db.close()


def _publish_upload_schema(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    sidecars = [Path(str(temporary) + suffix) for suffix in ("-journal", "-wal", "-shm")]
    try:
        db = sqlite3.connect(temporary)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_DDL
                             + f"\nINSERT INTO metadata VALUES({SCHEMA_VERSION});\nCOMMIT;")
        finally:
            db.close()
        os.chmod(temporary, 0o600)
        validate_upload_schema(temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        published = False
        try:
            os.link(temporary, path)
            published = True
        except FileExistsError:
            pass
        if published:
            temporary.unlink()
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)
    except (OSError, sqlite3.Error, UploadSchemaError):
        raise UploadSchemaError from None
    finally:
        temporary.unlink(missing_ok=True)
        for sidecar in sidecars:
            sidecar.unlink(missing_ok=True)


def initialize_upload_schema(path: Path) -> None:
    """Publish or validate only the latest schema; never migrate implicitly."""
    with _INITIALIZE_LOCK:
        if path.exists():
            validate_upload_schema(path)
            return
        _publish_upload_schema(path)
        validate_upload_schema(path)


def ensure_upload_schema(path: Path) -> None:
    """Atomically create Schema 3 or migrate one exact Schema 1/2 database."""
    with _INITIALIZE_LOCK:
        if not path.exists():
            _publish_upload_schema(path)
        with _exclusive_schema_access(path):
            version = _validated_schema_version(
                path,
                frozenset({1, _SCHEMA_V2_VERSION, SCHEMA_VERSION}),
            )
            if version != SCHEMA_VERSION:
                _migrate_to_latest(path)
            validate_upload_schema(path)

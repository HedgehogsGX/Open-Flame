"""Exact, read-only validation and atomic creation for upload Schema 1."""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import tempfile
import threading
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 1
SCHEMA_STATEMENTS = (
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
SCHEMA_DDL = ";\n".join(SCHEMA_STATEMENTS) + ";"


class UploadSchemaError(Exception):
    pass


_COLUMNS = {
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
_INDEXES = {
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
_FOREIGN_KEYS = {
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
_CANONICAL_SQL = {
    statement.split("(", 1)[0].split()[-1]: re.sub(r"\s+", "", statement).lower()
    for statement in SCHEMA_STATEMENTS
}
_INITIALIZE_LOCK = threading.Lock()
_SNAPSHOT_ATTEMPTS = 2
_SNAPSHOT_CHUNK_BYTES = 1024 * 1024
_WAL_MAGIC_LITTLE_CHECKSUM = 0x377F0682
_WAL_MAGIC_BIG_CHECKSUM = 0x377F0683
_WAL_FORMAT_VERSION = 3_007_000
_UINT32_MASK = (1 << 32) - 1


class _SnapshotChanged(Exception):
    pass


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
    if _present(journal) or (shm_present and not wal_present):
        raise UploadSchemaError
    if wal_present:
        _require_plain_database(wal)
    if shm_present:
        _require_plain_database(shm)

    expected = {path: _fingerprint_file(path, destination)}
    if wal_present:
        expected[wal] = _fingerprint_file(wal, _sidecar(destination, "-wal"))
    if shm_present:
        expected[shm] = _fingerprint_file(shm)

    if (_present(wal), _present(shm), _present(journal)) != (
        wal_present,
        shm_present,
        False,
    ):
        raise _SnapshotChanged
    for source, fingerprint in expected.items():
        if _fingerprint_file(source) != fingerprint:
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


def _validate_connection(db: sqlite3.Connection) -> None:
    if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
        raise UploadSchemaError
    objects = [tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
    )]
    tables = {name: sql for kind, name, _owner, sql in objects if kind == "table"}
    if set(tables) != set(_COLUMNS):
        raise UploadSchemaError
    if any(kind not in {"table", "index"} for kind, *_ in objects):
        raise UploadSchemaError
    if any(kind == "index" and (sql is not None or not name.startswith("sqlite_autoindex_"))
           for kind, name, _owner, sql in objects):
        raise UploadSchemaError
    for table, expected_columns in _COLUMNS.items():
        sql = tables[table]
        if not isinstance(sql, str) or re.sub(r"\s+", "", sql).lower() != _CANONICAL_SQL[table]:
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
        if indexes != _INDEXES[table]:
            raise UploadSchemaError
        foreign_keys = {
            (row[2], row[3], row[4], row[5], row[6], row[7])
            for row in db.execute(f"PRAGMA foreign_key_list({_quoted(table)})")
        }
        if foreign_keys != _FOREIGN_KEYS[table]:
            raise UploadSchemaError
    versions = [tuple(row) for row in db.execute(
        "SELECT version,typeof(version) FROM metadata"
    )]
    if versions != [(SCHEMA_VERSION, "integer")]:
        raise UploadSchemaError
    if list(db.execute("PRAGMA foreign_key_check")):
        raise UploadSchemaError


def validate_upload_schema(path: Path) -> None:
    """Validate an existing database through a read-only SQLite connection."""
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
                        _validate_connection(db)
                    finally:
                        db.close()
                return
            except _SnapshotChanged:
                if attempt + 1 == _SNAPSHOT_ATTEMPTS:
                    raise UploadSchemaError from None
    except (OSError, sqlite3.Error, UploadSchemaError):
        raise UploadSchemaError from None


def initialize_upload_schema(path: Path) -> None:
    """Publish a complete database without ever exposing a partially built target."""
    with _INITIALIZE_LOCK:
        if path.exists():
            validate_upload_schema(path)
            return
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
            try:
                os.link(temporary, path)
            except FileExistsError:
                validate_upload_schema(path)
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

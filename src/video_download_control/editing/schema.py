"""Exact Schema 1 creation and validation for the isolated editing store."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path


SCHEMA_VERSION = 1

TABLE_STATEMENTS = (
    "CREATE TABLE metadata(version INTEGER NOT NULL)",
    """CREATE TABLE sources(
 id TEXT PRIMARY KEY, source_asset_id TEXT, name TEXT NOT NULL,
 suffix TEXT NOT NULL, size INTEGER NOT NULL CHECK(size > 0),
 sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE projects(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 name TEXT NOT NULL, current_version INTEGER NOT NULL CHECK(current_version > 0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE drafts(
 project_id TEXT NOT NULL REFERENCES projects(id),
 version INTEGER NOT NULL CHECK(version > 0), recipe TEXT NOT NULL,
 recipe_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(project_id,version))""",
    """CREATE TABLE render_plans(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 draft_version INTEGER NOT NULL, recipe TEXT NOT NULL,
 recipe_sha256 TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'review'
 CHECK(state IN ('review','queued','running','canceling','ready','failed','canceled')),
 code TEXT NOT NULL DEFAULT '', claim_token TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 confirmed_at TEXT, started_at TEXT, finished_at TEXT,
 retry_of TEXT REFERENCES render_plans(id),
 FOREIGN KEY(project_id,draft_version) REFERENCES drafts(project_id,version))""",
    """CREATE TABLE assets(
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES render_plans(id),
 kind TEXT NOT NULL CHECK(kind IN ('segment','cover','caption','audio','dubbed_video')),
 name TEXT NOT NULL, suffix TEXT NOT NULL, mime_type TEXT NOT NULL,
 size INTEGER NOT NULL CHECK(size > 0), sha256 TEXT NOT NULL,
 ordinal INTEGER NOT NULL DEFAULT 0 CHECK(ordinal >= 0),
 duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
 width INTEGER CHECK(width IS NULL OR width > 0),
 height INTEGER CHECK(height IS NULL OR height > 0),
 container TEXT NOT NULL DEFAULT '', video_codec TEXT, audio_codec TEXT,
 created_at TEXT NOT NULL, UNIQUE(plan_id,name))""",
    """CREATE TABLE requests(
 id TEXT PRIMARY KEY, operation TEXT NOT NULL,
 digest TEXT NOT NULL, result_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
)

INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX sources_download_asset ON sources(source_asset_id) WHERE source_asset_id IS NOT NULL",
    "CREATE INDEX projects_source ON projects(source_id)",
    "CREATE INDEX plans_state ON render_plans(state,created_at)",
    "CREATE INDEX plans_project ON render_plans(project_id,created_at)",
    "CREATE INDEX assets_plan ON assets(plan_id,created_at)",
)

TRIGGER_STATEMENTS = (
    """CREATE TRIGGER render_plan_definition_immutable
 BEFORE UPDATE OF project_id,draft_version,recipe,recipe_sha256,created_at,retry_of ON render_plans
 BEGIN SELECT RAISE(ABORT,'immutable render plan'); END""",
    """CREATE TRIGGER draft_definition_immutable
 BEFORE UPDATE ON drafts BEGIN SELECT RAISE(ABORT,'immutable draft'); END""",
    """CREATE TRIGGER draft_delete_forbidden
 BEFORE DELETE ON drafts BEGIN SELECT RAISE(ABORT,'immutable draft'); END""",
)

SCHEMA_DDL = ";\n".join(TABLE_STATEMENTS + INDEX_STATEMENTS + TRIGGER_STATEMENTS) + ";"
_TABLE_NAMES = tuple(("metadata", "sources", "projects", "drafts", "render_plans", "assets", "requests"))
_INDEX_NAMES = tuple(("sources_download_asset", "projects_source", "plans_state", "plans_project", "assets_plan"))
_TRIGGER_NAMES = (
    "render_plan_definition_immutable", "draft_definition_immutable", "draft_delete_forbidden",
)
_INITIALIZE_LOCK = threading.Lock()


class EditingSchemaError(Exception):
    """The editing database is unsafe, corrupt, or not exactly Schema 1."""


def _normalized_sql(value: str) -> str:
    output: list[str] = []
    quoted = False
    index = 0
    while index < len(value):
        character = value[index]
        if character == "'":
            output.append(character)
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                output.append("'")
                index += 2
                continue
            quoted = not quoted
        elif quoted or not character.isspace():
            output.append(character if quoted else character.lower())
        index += 1
    return "".join(output)


_EXPECTED_TABLE_SQL = {
    name: _normalized_sql(statement)
    for name, statement in zip(_TABLE_NAMES, TABLE_STATEMENTS, strict=True)
}
_EXPECTED_INDEX_SQL = {
    name: _normalized_sql(statement)
    for name, statement in zip(_INDEX_NAMES, INDEX_STATEMENTS, strict=True)
}
_EXPECTED_TRIGGER_SQL = {
    name: _normalized_sql(statement)
    for name, statement in zip(_TRIGGER_NAMES, TRIGGER_STATEMENTS, strict=True)
}


def _plain_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EditingSchemaError from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse or info.st_nlink != 1):
        raise EditingSchemaError
    return info


def _plain_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EditingSchemaError from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse):
        raise EditingSchemaError
    return info


def validate_editing_schema(path: Path) -> None:
    path = Path(path)
    before = _plain_file(path)
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True)
    except sqlite3.Error as exc:
        raise EditingSchemaError from exc
    # SQLite owns its descriptor internally. Re-check the name before and after
    # validation to detect replacement races around the read-only inspection.
    try:
        connection.row_factory = sqlite3.Row
        if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
            raise EditingSchemaError
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise EditingSchemaError
        rows = connection.execute(
            "SELECT type,name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        table_sql = {
            row["name"]: _normalized_sql(row["sql"] or "")
            for row in rows if row["type"] == "table"
        }
        index_sql = {
            row["name"]: _normalized_sql(row["sql"] or "")
            for row in rows if row["type"] == "index"
        }
        trigger_sql = {
            row["name"]: _normalized_sql(row["sql"] or "")
            for row in rows if row["type"] == "trigger"
        }
        if (table_sql != _EXPECTED_TABLE_SQL or index_sql != _EXPECTED_INDEX_SQL
                or trigger_sql != _EXPECTED_TRIGGER_SQL):
            raise EditingSchemaError
        metadata = connection.execute("SELECT version FROM metadata").fetchall()
        if len(metadata) != 1 or metadata[0][0] != SCHEMA_VERSION:
            raise EditingSchemaError
        if connection.execute("PRAGMA application_id").fetchone()[0] != 0x4F464544:
            raise EditingSchemaError
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise EditingSchemaError
    except (sqlite3.Error, EditingSchemaError) as exc:
        raise EditingSchemaError from exc
    finally:
        connection.close()
    after = _plain_file(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise EditingSchemaError


def _create_database(path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(SCHEMA_DDL)
            connection.execute("INSERT INTO metadata(version) VALUES(?)", (SCHEMA_VERSION,))
            connection.execute("PRAGMA application_id=0x4F464544")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        finally:
            connection.close()
        validate_editing_schema(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError as exc:
            # Do not replace an existing database. os.link supplies atomic
            # create-if-absent semantics on both supported platforms.
            if path.exists():
                pass
            else:
                raise EditingSchemaError from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_editing_schema(path: Path) -> None:
    """Create Schema 1 atomically or validate an existing exact database."""

    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise EditingSchemaError
    with _INITIALIZE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _plain_directory(path.parent)
            if not path.exists():
                _create_database(path)
            validate_editing_schema(path)
        except (OSError, sqlite3.Error, EditingSchemaError) as exc:
            raise EditingSchemaError from exc

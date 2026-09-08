"""Exact Schema 2 creation and forward migration for the editing store."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path


SCHEMA_VERSION = 2
_SCHEMA_V1_VERSION = 1

_SCHEMA_V1_TABLE_STATEMENTS = (
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

_SCHEMA_V1_INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX sources_download_asset ON sources(source_asset_id) WHERE source_asset_id IS NOT NULL",
    "CREATE INDEX projects_source ON projects(source_id)",
    "CREATE INDEX plans_state ON render_plans(state,created_at)",
    "CREATE INDEX plans_project ON render_plans(project_id,created_at)",
    "CREATE INDEX assets_plan ON assets(plan_id,created_at)",
)

_SCHEMA_V1_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER render_plan_definition_immutable
 BEFORE UPDATE OF project_id,draft_version,recipe,recipe_sha256,created_at,retry_of ON render_plans
 BEGIN SELECT RAISE(ABORT,'immutable render plan'); END""",
    """CREATE TRIGGER draft_definition_immutable
 BEFORE UPDATE ON drafts BEGIN SELECT RAISE(ABORT,'immutable draft'); END""",
    """CREATE TRIGGER draft_delete_forbidden
 BEFORE DELETE ON drafts BEGIN SELECT RAISE(ABORT,'immutable draft'); END""",
)

_SCHEMA_V2_TABLE_STATEMENTS = (
    """CREATE TABLE timeline_revisions(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 parent_id TEXT REFERENCES timeline_revisions(id),
 kind TEXT NOT NULL CHECK(kind IN ('transcription','translation')),
 language TEXT NOT NULL, cues TEXT NOT NULL, cues_sha256 TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'review'
 CHECK(state IN ('review','approved','rejected')),
 review_version INTEGER NOT NULL DEFAULT 0 CHECK(review_version IN (0,1)),
 code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, reviewed_at TEXT,
 CHECK((kind='transcription' AND parent_id IS NULL) OR
       (kind='translation' AND parent_id IS NOT NULL)),
 CHECK((state='review' AND review_version=0 AND reviewed_at IS NULL) OR
       (state IN ('approved','rejected') AND review_version=1 AND reviewed_at IS NOT NULL)))""",
    """CREATE TABLE ai_tasks(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 operation TEXT NOT NULL CHECK(operation IN ('transcribe','translate')),
 source_revision_id TEXT REFERENCES timeline_revisions(id),
 target_language TEXT, request TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'review'
 CHECK(state IN ('review','queued','running','canceling','succeeded','failed','canceled')),
 code TEXT NOT NULL DEFAULT '', claim_token TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 confirmed_at TEXT, started_at TEXT, finished_at TEXT,
 retry_of TEXT REFERENCES ai_tasks(id),
 result_revision_id TEXT REFERENCES timeline_revisions(id),
 CHECK((operation='transcribe' AND source_revision_id IS NULL AND target_language IS NULL) OR
       (operation='translate' AND source_revision_id IS NOT NULL AND target_language IS NOT NULL)),
 CHECK((state IN ('running','canceling') AND claim_token IS NOT NULL) OR
       (state NOT IN ('running','canceling') AND claim_token IS NULL)),
 CHECK((state='succeeded' AND result_revision_id IS NOT NULL) OR
       (state!='succeeded' AND result_revision_id IS NULL)))""",
)

_SCHEMA_V2_INDEX_STATEMENTS = (
    "CREATE INDEX timelines_project ON timeline_revisions(project_id,created_at)",
    "CREATE INDEX timelines_parent ON timeline_revisions(parent_id,created_at)",
    "CREATE INDEX ai_tasks_state ON ai_tasks(state,created_at)",
    "CREATE INDEX ai_tasks_project ON ai_tasks(project_id,created_at)",
    "CREATE INDEX ai_tasks_source ON ai_tasks(source_revision_id,created_at)",
    "CREATE UNIQUE INDEX ai_tasks_retry ON ai_tasks(retry_of) WHERE retry_of IS NOT NULL",
    "CREATE UNIQUE INDEX ai_tasks_result ON ai_tasks(result_revision_id) WHERE result_revision_id IS NOT NULL",
)

_SCHEMA_V2_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER timeline_definition_immutable
 BEFORE UPDATE OF project_id,parent_id,kind,language,cues,cues_sha256,provider,model,created_at
 ON timeline_revisions BEGIN SELECT RAISE(ABORT,'immutable timeline revision'); END""",
    """CREATE TRIGGER timeline_review_once
 BEFORE UPDATE OF state,review_version,code,updated_at,reviewed_at ON timeline_revisions
 WHEN NOT (OLD.state='review' AND OLD.review_version=0 AND OLD.reviewed_at IS NULL AND
           NEW.state IN ('approved','rejected') AND NEW.review_version=1 AND
           NEW.reviewed_at IS NOT NULL)
 BEGIN SELECT RAISE(ABORT,'invalid timeline review transition'); END""",
    """CREATE TRIGGER timeline_delete_forbidden
 BEFORE DELETE ON timeline_revisions BEGIN SELECT RAISE(ABORT,'immutable timeline revision'); END""",
    """CREATE TRIGGER ai_task_definition_immutable
 BEFORE UPDATE OF project_id,operation,source_revision_id,target_language,request,
 request_sha256,provider,model,created_at,retry_of ON ai_tasks
 BEGIN SELECT RAISE(ABORT,'immutable ai task'); END""",
    """CREATE TRIGGER ai_task_delete_forbidden
 BEFORE DELETE ON ai_tasks BEGIN SELECT RAISE(ABORT,'immutable ai task'); END""",
)

TABLE_STATEMENTS = _SCHEMA_V1_TABLE_STATEMENTS + _SCHEMA_V2_TABLE_STATEMENTS
INDEX_STATEMENTS = _SCHEMA_V1_INDEX_STATEMENTS + _SCHEMA_V2_INDEX_STATEMENTS
TRIGGER_STATEMENTS = _SCHEMA_V1_TRIGGER_STATEMENTS + _SCHEMA_V2_TRIGGER_STATEMENTS

SCHEMA_DDL = ";\n".join(TABLE_STATEMENTS + INDEX_STATEMENTS + TRIGGER_STATEMENTS) + ";"
_SCHEMA_V1_TABLE_NAMES = (
    "metadata", "sources", "projects", "drafts", "render_plans", "assets", "requests",
)
_SCHEMA_V1_INDEX_NAMES = (
    "sources_download_asset", "projects_source", "plans_state", "plans_project", "assets_plan",
)
_SCHEMA_V1_TRIGGER_NAMES = (
    "render_plan_definition_immutable", "draft_definition_immutable", "draft_delete_forbidden",
)
_TABLE_NAMES = _SCHEMA_V1_TABLE_NAMES + ("timeline_revisions", "ai_tasks")
_INDEX_NAMES = _SCHEMA_V1_INDEX_NAMES + (
    "timelines_project", "timelines_parent", "ai_tasks_state", "ai_tasks_project",
    "ai_tasks_source", "ai_tasks_retry", "ai_tasks_result",
)
_TRIGGER_NAMES = _SCHEMA_V1_TRIGGER_NAMES + (
    "timeline_definition_immutable", "timeline_review_once", "timeline_delete_forbidden",
    "ai_task_definition_immutable", "ai_task_delete_forbidden",
)
_INITIALIZE_LOCK = threading.Lock()


class EditingSchemaError(Exception):
    """The editing database is unsafe, corrupt, or not an exact known schema."""


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


def _expected_sql(names: tuple[str, ...], statements: tuple[str, ...]) -> dict[str, str]:
    return {
        name: _normalized_sql(statement)
        for name, statement in zip(names, statements, strict=True)
    }


_EXPECTED_TABLE_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(
        _SCHEMA_V1_TABLE_NAMES, _SCHEMA_V1_TABLE_STATEMENTS
    ),
    SCHEMA_VERSION: _expected_sql(_TABLE_NAMES, TABLE_STATEMENTS),
}
_EXPECTED_INDEX_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(
        _SCHEMA_V1_INDEX_NAMES, _SCHEMA_V1_INDEX_STATEMENTS
    ),
    SCHEMA_VERSION: _expected_sql(_INDEX_NAMES, INDEX_STATEMENTS),
}
_EXPECTED_TRIGGER_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(
        _SCHEMA_V1_TRIGGER_NAMES, _SCHEMA_V1_TRIGGER_STATEMENTS
    ),
    SCHEMA_VERSION: _expected_sql(_TRIGGER_NAMES, TRIGGER_STATEMENTS),
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


def _metadata_version(connection: sqlite3.Connection) -> int:
    rows = connection.execute("SELECT version FROM metadata").fetchall()
    if (
        len(rows) != 1
        or isinstance(rows[0][0], bool)
        or not isinstance(rows[0][0], int)
    ):
        raise EditingSchemaError
    return rows[0][0]


def _validate_connection(connection: sqlite3.Connection, version: int) -> None:
    if version not in _EXPECTED_TABLE_SQL:
        raise EditingSchemaError
    if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
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
    if (
        table_sql != _EXPECTED_TABLE_SQL[version]
        or index_sql != _EXPECTED_INDEX_SQL[version]
        or trigger_sql != _EXPECTED_TRIGGER_SQL[version]
        or _metadata_version(connection) != version
        or connection.execute("PRAGMA application_id").fetchone()[0] != 0x4F464544
        or connection.execute("PRAGMA user_version").fetchone()[0] != version
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise EditingSchemaError


def _validated_schema_version(path: Path, allowed: frozenset[int]) -> int:
    path = Path(path)
    before = _plain_file(path)
    connection: sqlite3.Connection | None = None
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        version = _metadata_version(connection)
        if version not in allowed:
            raise EditingSchemaError
        _validate_connection(connection, version)
    except (OSError, sqlite3.Error, EditingSchemaError) as exc:
        raise EditingSchemaError from exc
    finally:
        if connection is not None:
            connection.close()
    after = _plain_file(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise EditingSchemaError
    return version


def validate_editing_schema(path: Path) -> None:
    _validated_schema_version(Path(path), frozenset({SCHEMA_VERSION}))


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


def _migrate_v1_to_v2(path: Path) -> None:
    before = _plain_file(path)
    connection: sqlite3.Connection | None = None
    committed = False
    try:
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        current = _plain_file(path)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise EditingSchemaError
        version = _metadata_version(connection)
        if version == _SCHEMA_V1_VERSION:
            _validate_connection(connection, _SCHEMA_V1_VERSION)
            for statement in (
                _SCHEMA_V2_TABLE_STATEMENTS
                + _SCHEMA_V2_INDEX_STATEMENTS
                + _SCHEMA_V2_TRIGGER_STATEMENTS
            ):
                connection.execute(statement)
            connection.execute(
                "UPDATE metadata SET version=?", (SCHEMA_VERSION,)
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _validate_connection(connection, SCHEMA_VERSION)
        elif version == SCHEMA_VERSION:
            _validate_connection(connection, SCHEMA_VERSION)
        else:
            raise EditingSchemaError
        connection.commit()
        committed = True
    except (OSError, sqlite3.Error, EditingSchemaError) as exc:
        if connection is not None and not committed:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        raise EditingSchemaError from exc
    finally:
        if connection is not None:
            connection.close()


def ensure_editing_schema(path: Path) -> None:
    """Create Schema 2 or migrate one exact Schema 1 database in place."""

    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise EditingSchemaError
    with _INITIALIZE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _plain_directory(path.parent)
            if not path.exists():
                _create_database(path)
            version = _validated_schema_version(
                path, frozenset({_SCHEMA_V1_VERSION, SCHEMA_VERSION})
            )
            if version == _SCHEMA_V1_VERSION:
                _migrate_v1_to_v2(path)
            validate_editing_schema(path)
        except (OSError, sqlite3.Error, EditingSchemaError) as exc:
            raise EditingSchemaError from exc

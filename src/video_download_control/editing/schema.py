"""Exact Schema 4 creation and forward migration for the editing store."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path


SCHEMA_VERSION = 4
_SCHEMA_V1_VERSION = 1
_SCHEMA_V2_VERSION = 2
_SCHEMA_V3_VERSION = 3

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

_SCHEMA_V3_TABLE_STATEMENTS = (
    """CREATE TABLE plan_timeline_bindings(
 plan_id TEXT PRIMARY KEY REFERENCES render_plans(id),
 revision_id TEXT NOT NULL REFERENCES timeline_revisions(id),
 cues_sha256 TEXT NOT NULL, parent_id TEXT NOT NULL REFERENCES timeline_revisions(id),
 parent_cues_sha256 TEXT NOT NULL, source_language TEXT NOT NULL,
 target_language TEXT NOT NULL, created_at TEXT NOT NULL)""",
)

_SCHEMA_V3_INDEX_STATEMENTS = (
    "CREATE INDEX plan_timelines_revision ON plan_timeline_bindings(revision_id,plan_id)",
)

_SCHEMA_V3_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER plan_timeline_binding_immutable
 BEFORE UPDATE ON plan_timeline_bindings
 BEGIN SELECT RAISE(ABORT,'immutable plan timeline binding'); END""",
    """CREATE TRIGGER plan_timeline_binding_delete_forbidden
 BEFORE DELETE ON plan_timeline_bindings
 BEGIN SELECT RAISE(ABORT,'immutable plan timeline binding'); END""",
)

_SCHEMA_V4_TABLE_STATEMENTS = (
    """CREATE TABLE ai_invocations(
 id TEXT PRIMARY KEY CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
 ai_task_id TEXT REFERENCES ai_tasks(id),
 render_plan_id TEXT REFERENCES render_plans(id),
 operation TEXT NOT NULL CHECK(operation IN ('transcribe','translate','synthesize')),
 ordinal INTEGER NOT NULL CHECK(ordinal >= 0 AND ordinal <= 999999),
 attempt INTEGER NOT NULL CHECK(attempt >= 0 AND attempt <= 999999),
 request_units INTEGER NOT NULL CHECK(request_units > 0 AND request_units <= 600),
 authorization_sha256 TEXT,
 owner_definition_sha256 TEXT NOT NULL
 CHECK(length(owner_definition_sha256)=64 AND owner_definition_sha256 NOT GLOB '*[^0-9a-f]*'),
 request_fingerprint TEXT NOT NULL
 CHECK(length(request_fingerprint)=64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
 state TEXT NOT NULL DEFAULT 'reserved'
 CHECK(state IN ('reserved','dispatched','responded','released','unknown','reconciled')),
 resolution TEXT NOT NULL DEFAULT ''
 CHECK((state='reconciled' AND resolution IN
        ('not_accepted','accepted_without_result','abandoned')) OR
       (state!='reconciled' AND resolution='')),
 reason_code TEXT NOT NULL DEFAULT ''
 CHECK(length(reason_code) <= 80 AND
       reason_code NOT GLOB '*[^a-z0-9_]*' AND
       ((state='reserved' AND reason_code='') OR
        (state!='reserved' AND reason_code GLOB '[a-z]*'))),
 revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
 legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 dispatched_at TEXT, responded_at TEXT, released_at TEXT,
 unknown_at TEXT, reconciled_at TEXT,
 CHECK((ai_task_id IS NOT NULL AND render_plan_id IS NULL AND
        operation IN ('transcribe','translate')) OR
       (ai_task_id IS NULL AND render_plan_id IS NOT NULL AND
        operation='synthesize')),
 CHECK(operation='synthesize' OR ordinal=0),
 CHECK(legacy=1 OR
       (operation='transcribe' AND request_units=1) OR
       (operation='translate' AND request_units <= 20) OR
       (operation='synthesize' AND request_units=1)),
 CHECK((legacy=0 AND attempt >= 1 AND authorization_sha256 IS NOT NULL AND
        length(authorization_sha256)=64 AND
        authorization_sha256 NOT GLOB '*[^0-9a-f]*') OR
       (legacy=1 AND attempt=0 AND state IN ('unknown','reconciled') AND
        (authorization_sha256 IS NULL OR
         (length(authorization_sha256)=64 AND
          authorization_sha256 NOT GLOB '*[^0-9a-f]*')))),
 CHECK((state='reserved' AND dispatched_at IS NULL AND responded_at IS NULL AND
        released_at IS NULL AND unknown_at IS NULL AND reconciled_at IS NULL) OR
       (state='dispatched' AND dispatched_at IS NOT NULL AND responded_at IS NULL AND
        released_at IS NULL AND unknown_at IS NULL AND reconciled_at IS NULL) OR
       (state='responded' AND dispatched_at IS NOT NULL AND responded_at IS NOT NULL AND
        released_at IS NULL AND unknown_at IS NULL AND reconciled_at IS NULL) OR
       (state='released' AND dispatched_at IS NULL AND responded_at IS NULL AND
        released_at IS NOT NULL AND
        unknown_at IS NULL AND reconciled_at IS NULL) OR
       (state='unknown' AND dispatched_at IS NOT NULL AND responded_at IS NULL AND
        released_at IS NULL AND unknown_at IS NOT NULL AND reconciled_at IS NULL) OR
       (state='reconciled' AND dispatched_at IS NOT NULL AND responded_at IS NULL AND
        released_at IS NULL AND unknown_at IS NOT NULL AND reconciled_at IS NOT NULL)))""",
)

_SCHEMA_V4_INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX ai_invocations_task_unit ON ai_invocations(ai_task_id,operation,ordinal,attempt) WHERE ai_task_id IS NOT NULL",
    "CREATE UNIQUE INDEX ai_invocations_plan_unit ON ai_invocations(render_plan_id,operation,ordinal,attempt) WHERE render_plan_id IS NOT NULL",
    "CREATE INDEX ai_invocations_unresolved ON ai_invocations(state,created_at,id)",
)

_SCHEMA_V4_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER ai_invocation_insert_guard
 BEFORE INSERT ON ai_invocations
 WHEN NOT (
  (NEW.legacy=0 AND NEW.state='reserved' AND NEW.resolution='' AND
   NEW.reason_code='' AND NEW.revision=0 AND NEW.created_at IS NEW.updated_at AND
   NEW.dispatched_at IS NULL AND NEW.responded_at IS NULL AND
   NEW.released_at IS NULL AND NEW.unknown_at IS NULL AND
   NEW.reconciled_at IS NULL) OR
  (NEW.legacy=1 AND NEW.state='unknown' AND NEW.resolution='' AND
   NEW.reason_code IN ('legacy_remote_outcome_unknown',
                       'legacy_remote_execution_unknown') AND
   NEW.revision=0 AND NEW.created_at IS NEW.updated_at AND
   NEW.dispatched_at IS NEW.created_at AND NEW.unknown_at IS NEW.created_at AND
   NEW.responded_at IS NULL AND NEW.released_at IS NULL AND
   NEW.reconciled_at IS NULL)
 )
 BEGIN SELECT RAISE(ABORT,'invalid ai invocation insert'); END""",
    """CREATE TRIGGER ai_invocation_definition_immutable
 BEFORE UPDATE OF id,ai_task_id,render_plan_id,operation,ordinal,attempt,request_units,
 authorization_sha256,owner_definition_sha256,request_fingerprint,legacy,created_at
 ON ai_invocations BEGIN SELECT RAISE(ABORT,'immutable ai invocation'); END""",
    """CREATE TRIGGER ai_invocation_transition_guard
 BEFORE UPDATE OF state,resolution,reason_code,revision,updated_at,dispatched_at,
 responded_at,released_at,unknown_at,reconciled_at ON ai_invocations
 WHEN NOT (
  NEW.revision=OLD.revision+1 AND (
   (OLD.state='reserved' AND NEW.state='dispatched' AND
    NEW.dispatched_at IS NEW.updated_at AND
    NEW.responded_at IS OLD.responded_at AND
    NEW.released_at IS OLD.released_at AND NEW.unknown_at IS OLD.unknown_at AND
    NEW.reconciled_at IS OLD.reconciled_at) OR
   (OLD.state='reserved' AND NEW.state='released' AND
    NEW.dispatched_at IS OLD.dispatched_at AND
    NEW.responded_at IS OLD.responded_at AND
    NEW.released_at IS NEW.updated_at AND NEW.unknown_at IS OLD.unknown_at AND
    NEW.reconciled_at IS OLD.reconciled_at) OR
   (OLD.state='dispatched' AND NEW.state='responded' AND
    NEW.dispatched_at IS OLD.dispatched_at AND
    NEW.responded_at IS NEW.updated_at AND
    NEW.released_at IS OLD.released_at AND NEW.unknown_at IS OLD.unknown_at AND
    NEW.reconciled_at IS OLD.reconciled_at) OR
   (OLD.state='dispatched' AND NEW.state='unknown' AND
    NEW.dispatched_at IS OLD.dispatched_at AND
    NEW.responded_at IS OLD.responded_at AND
    NEW.released_at IS OLD.released_at AND NEW.unknown_at IS NEW.updated_at AND
    NEW.reconciled_at IS OLD.reconciled_at) OR
   (OLD.state='unknown' AND NEW.state='reconciled' AND
    NEW.dispatched_at IS OLD.dispatched_at AND
    NEW.responded_at IS OLD.responded_at AND
    NEW.released_at IS OLD.released_at AND NEW.unknown_at IS OLD.unknown_at AND
    NEW.reconciled_at IS NEW.updated_at)
  )
 )
 BEGIN SELECT RAISE(ABORT,'invalid ai invocation transition'); END""",
    """CREATE TRIGGER ai_invocation_delete_forbidden
 BEFORE DELETE ON ai_invocations
 BEGIN SELECT RAISE(ABORT,'immutable ai invocation'); END""",
)

TABLE_STATEMENTS = (
    _SCHEMA_V1_TABLE_STATEMENTS
    + _SCHEMA_V2_TABLE_STATEMENTS
    + _SCHEMA_V3_TABLE_STATEMENTS
    + _SCHEMA_V4_TABLE_STATEMENTS
)
INDEX_STATEMENTS = (
    _SCHEMA_V1_INDEX_STATEMENTS
    + _SCHEMA_V2_INDEX_STATEMENTS
    + _SCHEMA_V3_INDEX_STATEMENTS
    + _SCHEMA_V4_INDEX_STATEMENTS
)
TRIGGER_STATEMENTS = (
    _SCHEMA_V1_TRIGGER_STATEMENTS
    + _SCHEMA_V2_TRIGGER_STATEMENTS
    + _SCHEMA_V3_TRIGGER_STATEMENTS
    + _SCHEMA_V4_TRIGGER_STATEMENTS
)

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
_SCHEMA_V2_TABLE_NAMES = _SCHEMA_V1_TABLE_NAMES + (
    "timeline_revisions",
    "ai_tasks",
)
_SCHEMA_V2_INDEX_NAMES = _SCHEMA_V1_INDEX_NAMES + (
    "timelines_project", "timelines_parent", "ai_tasks_state", "ai_tasks_project",
    "ai_tasks_source", "ai_tasks_retry", "ai_tasks_result",
)
_SCHEMA_V2_TRIGGER_NAMES = _SCHEMA_V1_TRIGGER_NAMES + (
    "timeline_definition_immutable", "timeline_review_once", "timeline_delete_forbidden",
    "ai_task_definition_immutable", "ai_task_delete_forbidden",
)
_SCHEMA_V3_TABLE_NAMES = _SCHEMA_V2_TABLE_NAMES + ("plan_timeline_bindings",)
_SCHEMA_V3_INDEX_NAMES = _SCHEMA_V2_INDEX_NAMES + ("plan_timelines_revision",)
_SCHEMA_V3_TRIGGER_NAMES = _SCHEMA_V2_TRIGGER_NAMES + (
    "plan_timeline_binding_immutable",
    "plan_timeline_binding_delete_forbidden",
)
_TABLE_NAMES = _SCHEMA_V3_TABLE_NAMES + ("ai_invocations",)
_INDEX_NAMES = _SCHEMA_V3_INDEX_NAMES + (
    "ai_invocations_task_unit",
    "ai_invocations_plan_unit",
    "ai_invocations_unresolved",
)
_TRIGGER_NAMES = _SCHEMA_V3_TRIGGER_NAMES + (
    "ai_invocation_insert_guard",
    "ai_invocation_definition_immutable",
    "ai_invocation_transition_guard",
    "ai_invocation_delete_forbidden",
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
    _SCHEMA_V2_VERSION: _expected_sql(
        _SCHEMA_V2_TABLE_NAMES,
        _SCHEMA_V1_TABLE_STATEMENTS + _SCHEMA_V2_TABLE_STATEMENTS,
    ),
    _SCHEMA_V3_VERSION: _expected_sql(
        _SCHEMA_V3_TABLE_NAMES,
        _SCHEMA_V1_TABLE_STATEMENTS
        + _SCHEMA_V2_TABLE_STATEMENTS
        + _SCHEMA_V3_TABLE_STATEMENTS,
    ),
    SCHEMA_VERSION: _expected_sql(_TABLE_NAMES, TABLE_STATEMENTS),
}
_EXPECTED_INDEX_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(
        _SCHEMA_V1_INDEX_NAMES, _SCHEMA_V1_INDEX_STATEMENTS
    ),
    _SCHEMA_V2_VERSION: _expected_sql(
        _SCHEMA_V2_INDEX_NAMES,
        _SCHEMA_V1_INDEX_STATEMENTS + _SCHEMA_V2_INDEX_STATEMENTS,
    ),
    _SCHEMA_V3_VERSION: _expected_sql(
        _SCHEMA_V3_INDEX_NAMES,
        _SCHEMA_V1_INDEX_STATEMENTS
        + _SCHEMA_V2_INDEX_STATEMENTS
        + _SCHEMA_V3_INDEX_STATEMENTS,
    ),
    SCHEMA_VERSION: _expected_sql(_INDEX_NAMES, INDEX_STATEMENTS),
}
_EXPECTED_TRIGGER_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(
        _SCHEMA_V1_TRIGGER_NAMES, _SCHEMA_V1_TRIGGER_STATEMENTS
    ),
    _SCHEMA_V2_VERSION: _expected_sql(
        _SCHEMA_V2_TRIGGER_NAMES,
        _SCHEMA_V1_TRIGGER_STATEMENTS + _SCHEMA_V2_TRIGGER_STATEMENTS,
    ),
    _SCHEMA_V3_VERSION: _expected_sql(
        _SCHEMA_V3_TRIGGER_NAMES,
        _SCHEMA_V1_TRIGGER_STATEMENTS
        + _SCHEMA_V2_TRIGGER_STATEMENTS
        + _SCHEMA_V3_TRIGGER_STATEMENTS,
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


_LEGACY_REMOTE_UNKNOWN_CODES = frozenset(
    {
        "ai_task_interrupted",
        "ai_operation_timeout",
        "ai_remote_result_unknown",
        "ai_authorization_changed",
        "ai_bridge_failed",
        "ai_execution_failed",
        "ai_progress_callback_failed",
        "ai_progress_invalid",
        "ai_progress_limit_exceeded",
        "ai_runtime_changed",
        "ai_source_changed",
        "ai_source_unavailable",
        "ai_worker_output_limit",
        "ai_worker_failed",
        "ai_worker_unavailable",
        "ai_worker_result_invalid",
        "ai_worker_result_mismatch",
        "ai_http_request_failed",
        "ai_http_response_invalid",
        "ai_provider_output_invalid",
        "ai_protocol_invalid",
        "ai_translation_alignment_invalid",
        "ai_audio_output_changed",
        "ai_audio_output_invalid",
        "ai_audio_output_unavailable",
        "ai_audio_destination_exists",
        "ai_audio_destination_invalid",
        "ai_audio_destination_unavailable",
        "ai_speech_failed",
        "ai_render_failed",
        "processor_failed",
        "render_interrupted",
    }
)
_LEGACY_PROVEN_PRE_DISPATCH_CODES = frozenset(
    {
        # These failures are raised while resolving the credential or before a
        # media worker can be created.  Keep this list deliberately narrow:
        # runtime/source/output failures can also be raised by the post-run
        # validation path after a remote request was accepted.
        "ai_provider_auth_environment_invalid",
        "ai_provider_auth_missing",
        "ai_provider_auth_unavailable",
        "ai_work_root_invalid",
        "processor_not_configured",
    }
)
_LEGACY_MAX_REQUEST_UNITS = {
    "transcribe": 1,
    "translate": 20,
    "synthesize": 600,
}


def _legacy_outcome_may_be_unknown(
    state: object,
    code: object,
    started_at: object = None,
) -> bool:
    if state in {"running", "canceling"}:
        return True
    if state == "canceled" and started_at is not None:
        return True
    if state != "failed":
        return False
    if (
        started_at is not None
        and code not in _LEGACY_PROVEN_PRE_DISPATCH_CODES
    ):
        # Schema 2/3 stored only the final local failure code, and early rows
        # may have no parseable authorization binding.  Once an explicitly
        # local authorization has been excluded by the caller, a started owner
        # remains remote-eligible and response/provider codes do not prove that
        # no request was accepted.  Preserve it unless the code is one of the
        # few boundaries that necessarily precedes any dispatch.
        return True
    return isinstance(code, str) and (
        code in _LEGACY_REMOTE_UNKNOWN_CODES
        or "timeout" in code
        or "interrupted" in code
    )


def _legacy_digest(value: object) -> str:
    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def _legacy_remote_authorization(
    value: object, operation: str
) -> tuple[bool, str | None, int]:
    """Classify old authorization without trusting a partial mapping.

    An absent or invalid binding cannot prove local execution, so an otherwise
    eligible historical AI owner remains a possible remote owner.
    """

    if value is not None:
        try:
            from .ai_authorization import parse_operation_authorization

            authorization = parse_operation_authorization(value)
        except (ImportError, TypeError, ValueError):
            authorization = None
        if authorization is not None and authorization.operation == operation:
            if authorization.execution == "local":
                return False, None, 0
            return (
                True,
                authorization.sha256,
                int(authorization.limits["max_requests"]),
            )
    return True, None, _LEGACY_MAX_REQUEST_UNITS[operation]


def _insert_legacy_invocation(
    connection: sqlite3.Connection,
    *,
    owner_column: str,
    owner_id: str,
    operation: str,
    authorization_sha256: str | None,
    owner_definition_sha256: str,
    request_units: int,
    reason_code: str,
    timestamp: str,
) -> None:
    key = bytearray(b"open-flame-editing-ai-invocation-legacy-v1")
    for component in (owner_column, owner_id, operation):
        encoded = str(component).encode("utf-8", errors="surrogatepass")
        key.extend(len(encoded).to_bytes(8, "big"))
        key.extend(encoded)
    invocation_id = hashlib.sha256(key).hexdigest()[:32]
    ai_task_id = owner_id if owner_column == "ai_task_id" else None
    render_plan_id = owner_id if owner_column == "render_plan_id" else None
    connection.execute(
        "INSERT OR IGNORE INTO ai_invocations("
        "id,ai_task_id,render_plan_id,operation,ordinal,attempt,request_units,"
        "authorization_sha256,owner_definition_sha256,request_fingerprint,state,"
        "resolution,reason_code,revision,legacy,created_at,updated_at,dispatched_at,"
        "unknown_at) VALUES(?,?,?,?,0,0,?,?,?,?, 'unknown','',?,0,1,?,?,?,?)",
        (
            invocation_id,
            ai_task_id,
            render_plan_id,
            operation,
            request_units,
            authorization_sha256,
            owner_definition_sha256,
            owner_definition_sha256,
            reason_code,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    row = connection.execute(
        "SELECT ai_task_id,render_plan_id,operation,ordinal,attempt,request_units,"
        "authorization_sha256,owner_definition_sha256,request_fingerprint,state,legacy "
        "FROM ai_invocations WHERE id=?",
        (invocation_id,),
    ).fetchone()
    expected = (
        ai_task_id,
        render_plan_id,
        operation,
        0,
        0,
        request_units,
        authorization_sha256,
        owner_definition_sha256,
        owner_definition_sha256,
        "unknown",
        1,
    )
    if row is None or tuple(row) != expected:
        raise EditingSchemaError


def _migrate_legacy_remote_invocations(connection: sqlite3.Connection) -> None:
    """Create idempotent unknown sentinels only for plausibly dispatched work."""

    timestamp = datetime.now(UTC).isoformat()
    for row in connection.execute(
        "SELECT id,operation,request,state,code,started_at FROM ai_tasks "
        "ORDER BY created_at,id"
    ):
        operation = row["operation"]
        if operation not in {"transcribe", "translate"}:
            continue
        raw_authorization = None
        try:
            request = json.loads(row["request"])
            if isinstance(request, dict):
                raw_authorization = request.get("authorization")
        except (TypeError, json.JSONDecodeError):
            pass
        may_be_remote, authorization_sha256, request_units = (
            _legacy_remote_authorization(raw_authorization, operation)
        )
        if not may_be_remote or not _legacy_outcome_may_be_unknown(
            row["state"],
            row["code"],
            row["started_at"],
        ):
            continue
        owner_digest = _legacy_digest(row["request"])
        _insert_legacy_invocation(
            connection,
            owner_column="ai_task_id",
            owner_id=row["id"],
            operation=operation,
            authorization_sha256=authorization_sha256,
            owner_definition_sha256=owner_digest,
            request_units=request_units,
            reason_code=(
                "legacy_remote_outcome_unknown"
                if authorization_sha256 is not None
                else "legacy_remote_execution_unknown"
            ),
            timestamp=timestamp,
        )

    for row in connection.execute(
        "SELECT id,recipe,state,code,started_at FROM render_plans "
        "ORDER BY created_at,id"
    ):
        try:
            recipe = json.loads(row["recipe"])
            dubbing = recipe.get("dubbing") if isinstance(recipe, dict) else None
        except (TypeError, json.JSONDecodeError):
            dubbing = None
        if not isinstance(dubbing, dict) or dubbing.get("enabled") is not True:
            continue
        may_be_remote, authorization_sha256, request_units = (
            _legacy_remote_authorization(dubbing.get("authorization"), "synthesize")
        )
        if not may_be_remote or not _legacy_outcome_may_be_unknown(
            row["state"],
            row["code"],
            row["started_at"],
        ):
            continue
        owner_digest = _legacy_digest(row["recipe"])
        _insert_legacy_invocation(
            connection,
            owner_column="render_plan_id",
            owner_id=row["id"],
            operation="synthesize",
            authorization_sha256=authorization_sha256,
            owner_definition_sha256=owner_digest,
            request_units=request_units,
            reason_code=(
                "legacy_remote_outcome_unknown"
                if authorization_sha256 is not None
                else "legacy_remote_execution_unknown"
            ),
            timestamp=timestamp,
        )


def _migrate_to_current(path: Path) -> None:
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
                "UPDATE metadata SET version=?", (_SCHEMA_V2_VERSION,)
            )
            connection.execute(f"PRAGMA user_version={_SCHEMA_V2_VERSION}")
            _validate_connection(connection, _SCHEMA_V2_VERSION)
            version = _SCHEMA_V2_VERSION
        if version == _SCHEMA_V2_VERSION:
            _validate_connection(connection, _SCHEMA_V2_VERSION)
            for statement in (
                _SCHEMA_V3_TABLE_STATEMENTS
                + _SCHEMA_V3_INDEX_STATEMENTS
                + _SCHEMA_V3_TRIGGER_STATEMENTS
            ):
                connection.execute(statement)
            connection.execute(
                "UPDATE metadata SET version=?", (_SCHEMA_V3_VERSION,)
            )
            connection.execute(f"PRAGMA user_version={_SCHEMA_V3_VERSION}")
            _validate_connection(connection, _SCHEMA_V3_VERSION)
            version = _SCHEMA_V3_VERSION
        if version == _SCHEMA_V3_VERSION:
            _validate_connection(connection, _SCHEMA_V3_VERSION)
            for statement in (
                _SCHEMA_V4_TABLE_STATEMENTS
                + _SCHEMA_V4_INDEX_STATEMENTS
                + _SCHEMA_V4_TRIGGER_STATEMENTS
            ):
                connection.execute(statement)
            _migrate_legacy_remote_invocations(connection)
            connection.execute("UPDATE metadata SET version=?", (SCHEMA_VERSION,))
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _validate_connection(connection, SCHEMA_VERSION)
            version = SCHEMA_VERSION
        if version != SCHEMA_VERSION:
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
    """Create Schema 4 or migrate one exact Schema 1/2/3 database in place."""

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
                path,
                frozenset(
                    {
                        _SCHEMA_V1_VERSION,
                        _SCHEMA_V2_VERSION,
                        _SCHEMA_V3_VERSION,
                        SCHEMA_VERSION,
                    }
                ),
            )
            if version != SCHEMA_VERSION:
                _migrate_to_current(path)
            validate_editing_schema(path)
        except (OSError, sqlite3.Error, EditingSchemaError) as exc:
            raise EditingSchemaError from exc

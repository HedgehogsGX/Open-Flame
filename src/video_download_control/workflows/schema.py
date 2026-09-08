"""Exact Schema 1 for the lightweight workflow store."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path


SCHEMA_VERSION = 1
APPLICATION_ID = 0x4F465746

TABLE_STATEMENTS = (
    "CREATE TABLE metadata(version INTEGER NOT NULL)",
    """CREATE TABLE workflows(
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
 request_digest TEXT NOT NULL, source_url TEXT NOT NULL,
 name TEXT NOT NULL, profile_json TEXT NOT NULL, profile_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN (
  'created','downloading','preparing_edit','awaiting_ai_review',
  'awaiting_edit_confirmation','rendering','preparing_upload',
  'awaiting_upload_confirmation','uploading','completed',
  'attention_required','canceled')),
 code TEXT NOT NULL DEFAULT '', batch_name TEXT NOT NULL UNIQUE,
 batch_id TEXT, download_asset_id TEXT, edit_project_id TEXT,
 edit_draft_version INTEGER CHECK(edit_draft_version IS NULL OR edit_draft_version > 0),
 edit_plan_id TEXT, edit_output_id TEXT, edit_cover_id TEXT,
 upload_source_id TEXT, upload_cover_id TEXT,
 upload_job_ids_json TEXT NOT NULL DEFAULT '[]',
 auto_confirm_edit INTEGER NOT NULL CHECK(auto_confirm_edit IN (0,1)),
 auto_confirm_upload INTEGER NOT NULL CHECK(auto_confirm_upload IN (0,1)),
 revision INTEGER NOT NULL CHECK(revision > 0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT)""",
    """CREATE TABLE workflow_events(
 workflow_id TEXT NOT NULL REFERENCES workflows(id),
 sequence INTEGER NOT NULL CHECK(sequence > 0),
 from_state TEXT, to_state TEXT NOT NULL, code TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, PRIMARY KEY(workflow_id,sequence))""",
)

INDEX_STATEMENTS = (
    "CREATE INDEX workflows_state ON workflows(state,updated_at,id)",
    "CREATE INDEX workflow_events_created ON workflow_events(created_at,workflow_id,sequence)",
)

TRIGGER_STATEMENTS = (
    """CREATE TRIGGER workflow_intent_immutable
 BEFORE UPDATE OF id,request_key,request_digest,source_url,name,profile_json,
 profile_sha256,batch_name,auto_confirm_edit,auto_confirm_upload,created_at ON workflows
 BEGIN SELECT RAISE(ABORT,'immutable workflow intent'); END""",
    """CREATE TRIGGER workflow_event_immutable
 BEFORE UPDATE ON workflow_events BEGIN SELECT RAISE(ABORT,'immutable workflow event'); END""",
    """CREATE TRIGGER workflow_event_delete_forbidden
 BEFORE DELETE ON workflow_events BEGIN SELECT RAISE(ABORT,'immutable workflow event'); END""",
)

SCHEMA_DDL = ";\n".join(TABLE_STATEMENTS + INDEX_STATEMENTS + TRIGGER_STATEMENTS) + ";"
_TABLE_NAMES = ("metadata", "workflows", "workflow_events")
_INDEX_NAMES = ("workflows_state", "workflow_events_created")
_TRIGGER_NAMES = (
    "workflow_intent_immutable",
    "workflow_event_immutable",
    "workflow_event_delete_forbidden",
)
_INITIALIZE_LOCK = threading.Lock()


class WorkflowSchemaError(Exception):
    """The workflow database is unsafe, corrupt, or has an unknown schema."""


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
        raise WorkflowSchemaError from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & reparse
        or info.st_nlink != 1
    ):
        raise WorkflowSchemaError
    return info


def _plain_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkflowSchemaError from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & reparse
    ):
        raise WorkflowSchemaError
    return info


def validate_workflow_schema(path: Path) -> None:
    path = Path(path)
    before = _plain_file(path)
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True)
    except sqlite3.Error as exc:
        raise WorkflowSchemaError from exc
    try:
        connection.row_factory = sqlite3.Row
        if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
            raise WorkflowSchemaError
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise WorkflowSchemaError
        rows = connection.execute(
            "SELECT type,name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        table_sql = {
            row["name"]: _normalized_sql(row["sql"] or "")
            for row in rows
            if row["type"] == "table"
        }
        index_sql = {
            row["name"]: _normalized_sql(row["sql"] or "")
            for row in rows
            if row["type"] == "index"
        }
        trigger_sql = {
            row["name"]: _normalized_sql(row["sql"] or "")
            for row in rows
            if row["type"] == "trigger"
        }
        metadata = connection.execute("SELECT version FROM metadata").fetchall()
        if (
            table_sql != _EXPECTED_TABLE_SQL
            or index_sql != _EXPECTED_INDEX_SQL
            or trigger_sql != _EXPECTED_TRIGGER_SQL
            or len(metadata) != 1
            or metadata[0][0] != SCHEMA_VERSION
            or connection.execute("PRAGMA application_id").fetchone()[0]
            != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != SCHEMA_VERSION
        ):
            raise WorkflowSchemaError
    except (sqlite3.Error, WorkflowSchemaError) as exc:
        raise WorkflowSchemaError from exc
    finally:
        connection.close()
    after = _plain_file(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise WorkflowSchemaError


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
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        finally:
            connection.close()
        validate_workflow_schema(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError as exc:
            if not path.exists():
                raise WorkflowSchemaError from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_workflow_schema(path: Path) -> None:
    """Create Schema 1 atomically or validate an existing exact database."""

    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise WorkflowSchemaError
    with _INITIALIZE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _plain_directory(path.parent)
            if not path.exists():
                _create_database(path)
            validate_workflow_schema(path)
        except (OSError, sqlite3.Error, WorkflowSchemaError) as exc:
            raise WorkflowSchemaError from exc

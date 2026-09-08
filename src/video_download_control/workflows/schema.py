"""Exact, forward-migrating schema for the lightweight workflow store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from .contracts import MAX_WORKFLOW_ACCOUNTS, workflow_outputs_match_state


SCHEMA_VERSION = 2
APPLICATION_ID = 0x4F465746

_SCHEMA_V1_VERSION = 1
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_PLATFORMS = frozenset({"bilibili", "douyin", "tencent"})

_SCHEMA_V1_TABLE_STATEMENTS = (
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

_SCHEMA_V1_INDEX_STATEMENTS = (
    "CREATE INDEX workflows_state ON workflows(state,updated_at,id)",
    "CREATE INDEX workflow_events_created ON workflow_events(created_at,workflow_id,sequence)",
)

_SCHEMA_V1_TRIGGER_STATEMENTS = (
    """CREATE TRIGGER workflow_intent_immutable
 BEFORE UPDATE OF id,request_key,request_digest,source_url,name,profile_json,
 profile_sha256,batch_name,auto_confirm_edit,auto_confirm_upload,created_at ON workflows
 BEGIN SELECT RAISE(ABORT,'immutable workflow intent'); END""",
    """CREATE TRIGGER workflow_event_immutable
 BEFORE UPDATE ON workflow_events BEGIN SELECT RAISE(ABORT,'immutable workflow event'); END""",
    """CREATE TRIGGER workflow_event_delete_forbidden
 BEFORE DELETE ON workflow_events BEGIN SELECT RAISE(ABORT,'immutable workflow event'); END""",
)

TABLE_STATEMENTS = (
    _SCHEMA_V1_TABLE_STATEMENTS[0],
    _SCHEMA_V1_TABLE_STATEMENTS[1][:-1]
    + ", outputs_json TEXT NOT NULL DEFAULT '[]')",
    _SCHEMA_V1_TABLE_STATEMENTS[2],
)
INDEX_STATEMENTS = _SCHEMA_V1_INDEX_STATEMENTS
TRIGGER_STATEMENTS = _SCHEMA_V1_TRIGGER_STATEMENTS

SCHEMA_DDL = ";\n".join(TABLE_STATEMENTS + INDEX_STATEMENTS + TRIGGER_STATEMENTS) + ";"
_TABLE_NAMES = ("metadata", "workflows", "workflow_events")
_INDEX_NAMES = ("workflows_state", "workflow_events_created")
_TRIGGER_NAMES = (
    "workflow_intent_immutable",
    "workflow_event_immutable",
    "workflow_event_delete_forbidden",
)
_INITIALIZE_LOCK = threading.Lock()
LegacyProfileValidator = Callable[[Mapping[str, object]], bool]


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


def _expected_sql(names: tuple[str, ...], statements: tuple[str, ...]) -> dict[str, str]:
    return {
        name: _normalized_sql(statement)
        for name, statement in zip(names, statements, strict=True)
    }


_EXPECTED_TABLE_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(_TABLE_NAMES, _SCHEMA_V1_TABLE_STATEMENTS),
    SCHEMA_VERSION: _expected_sql(_TABLE_NAMES, TABLE_STATEMENTS),
}
_EXPECTED_INDEX_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(_INDEX_NAMES, _SCHEMA_V1_INDEX_STATEMENTS),
    SCHEMA_VERSION: _expected_sql(_INDEX_NAMES, INDEX_STATEMENTS),
}
_EXPECTED_TRIGGER_SQL = {
    _SCHEMA_V1_VERSION: _expected_sql(
        _TRIGGER_NAMES, _SCHEMA_V1_TRIGGER_STATEMENTS
    ),
    SCHEMA_VERSION: _expected_sql(_TRIGGER_NAMES, TRIGGER_STATEMENTS),
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


def _metadata_version(connection: sqlite3.Connection) -> int:
    rows = connection.execute("SELECT version FROM metadata").fetchall()
    if (
        len(rows) != 1
        or isinstance(rows[0][0], bool)
        or not isinstance(rows[0][0], int)
    ):
        raise WorkflowSchemaError
    return rows[0][0]


def _validate_connection(connection: sqlite3.Connection, version: int) -> None:
    if version not in _EXPECTED_TABLE_SQL:
        raise WorkflowSchemaError
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
    if (
        table_sql != _EXPECTED_TABLE_SQL[version]
        or index_sql != _EXPECTED_INDEX_SQL[version]
        or trigger_sql != _EXPECTED_TRIGGER_SQL[version]
        or _metadata_version(connection) != version
        or connection.execute("PRAGMA application_id").fetchone()[0]
        != APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0] != version
    ):
        raise WorkflowSchemaError


def _validated_schema_version(path: Path, allowed: frozenset[int]) -> int:
    path = Path(path)
    before = _plain_file(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        version = _metadata_version(connection)
        if version not in allowed:
            raise WorkflowSchemaError
        _validate_connection(connection, version)
    except (OSError, sqlite3.Error, WorkflowSchemaError) as exc:
        raise WorkflowSchemaError from exc
    finally:
        if connection is not None:
            connection.close()
    after = _plain_file(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise WorkflowSchemaError
    return version


def validate_workflow_schema(path: Path) -> None:
    _validated_schema_version(Path(path), frozenset({SCHEMA_VERSION}))


def _legacy_outputs(
    row: sqlite3.Row,
    profile_validator: LegacyProfileValidator,
) -> str:
    profile_json = row["profile_json"]
    profile_sha256 = row["profile_sha256"]
    if (
        not isinstance(profile_json, str)
        or len(profile_json.encode("utf-8")) > 128 * 1024
        or not isinstance(profile_sha256, str)
    ):
        raise WorkflowSchemaError
    try:
        profile = json.loads(profile_json)
        job_ids = json.loads(row["upload_job_ids_json"])
    except (TypeError, ValueError):
        raise WorkflowSchemaError from None
    if not isinstance(profile, dict) or not isinstance(job_ids, list):
        raise WorkflowSchemaError
    try:
        canonical_profile = json.dumps(
            profile,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise WorkflowSchemaError from None
    if (
        canonical_profile != profile_json
        or hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
        != profile_sha256
        or profile_validator(profile) is not True
    ):
        raise WorkflowSchemaError
    upload = profile.get("upload")
    edit_recipe = profile.get("edit_recipe")
    if not isinstance(upload, dict) or not isinstance(edit_recipe, dict):
        raise WorkflowSchemaError
    segments = edit_recipe.get("segments")
    dubbing = edit_recipe.get("dubbing")
    if (
        not isinstance(segments, list)
        or len(segments) > 1
        or (
            not segments
            and (
                not isinstance(dubbing, dict)
                or dubbing.get("enabled") is not True
            )
        )
    ):
        # Schema 1 could only persist one rendered output.  Reject a tampered
        # profile that claims several outputs rather than silently mapping the
        # legacy scalar reference to only the first one.
        raise WorkflowSchemaError
    account_ids = upload.get("account_ids")
    bindings = upload.get("account_bindings")
    if (
        not isinstance(account_ids, list)
        or not 1 <= len(account_ids) <= MAX_WORKFLOW_ACCOUNTS
        or any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in account_ids)
        or len(set(account_ids)) != len(account_ids)
        or not isinstance(bindings, list)
        or len(bindings) != len(account_ids)
    ):
        raise WorkflowSchemaError
    binding_platforms: dict[str, str] = {}
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {
            "account_id",
            "platform",
            "session_revision",
        }:
            raise WorkflowSchemaError
        account_id = binding.get("account_id")
        platform = binding.get("platform")
        revision = binding.get("session_revision")
        if (
            not isinstance(account_id, str)
            or account_id not in account_ids
            or account_id in binding_platforms
            or platform not in _PLATFORMS
            or not isinstance(revision, str)
            or not _IDENTIFIER.fullmatch(revision)
        ):
            raise WorkflowSchemaError
        binding_platforms[account_id] = platform
    if set(binding_platforms) != set(account_ids):
        raise WorkflowSchemaError
    if (
        len(job_ids) not in {0, len(account_ids)}
        or any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in job_ids)
        or len(set(job_ids)) != len(job_ids)
    ):
        raise WorkflowSchemaError

    edit_output_id = row["edit_output_id"]
    upload_source_id = row["upload_source_id"]
    if edit_output_id is None:
        if upload_source_id is not None or job_ids:
            raise WorkflowSchemaError
        outputs: list[dict[str, object]] = []
    else:
        if not isinstance(edit_output_id, str) or not _IDENTIFIER.fullmatch(edit_output_id):
            raise WorkflowSchemaError
        if (upload_source_id is None) != (not job_ids):
            raise WorkflowSchemaError
        if upload_source_id is not None and (
            not isinstance(upload_source_id, str)
            or not _IDENTIFIER.fullmatch(upload_source_id)
        ):
            raise WorkflowSchemaError
        targets = [
            {
                "account_id": account_id,
                "platform": binding_platforms[account_id],
                "job_id": job_id,
            }
            for account_id, job_id in zip(account_ids, job_ids, strict=True)
        ]
        outputs = [
            {
                "segment_ordinal": 1,
                "edit_output_id": edit_output_id,
                "upload_source_id": upload_source_id,
                "targets": targets,
            }
        ]
    if not workflow_outputs_match_state(row["state"], outputs, 1):
        raise WorkflowSchemaError
    return json.dumps(outputs, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _migrate_to_current(
    path: Path,
    legacy_profile_validator: LegacyProfileValidator | None,
) -> None:
    version = _validated_schema_version(path, frozenset({_SCHEMA_V1_VERSION, SCHEMA_VERSION}))
    if version == SCHEMA_VERSION:
        return
    if legacy_profile_validator is None:
        raise WorkflowSchemaError
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        _validate_connection(connection, _SCHEMA_V1_VERSION)
        rows = connection.execute(
            "SELECT profile_json,profile_sha256,state,edit_output_id,"
            "upload_source_id,upload_job_ids_json "
            "FROM workflows ORDER BY id"
        ).fetchall()
        encoded_outputs = [
            _legacy_outputs(row, legacy_profile_validator) for row in rows
        ]
        identifiers = [
            row[0]
            for row in connection.execute("SELECT id FROM workflows ORDER BY id").fetchall()
        ]
        connection.execute(
            "ALTER TABLE workflows ADD COLUMN outputs_json TEXT NOT NULL DEFAULT '[]'"
        )
        for workflow_id, outputs_json in zip(identifiers, encoded_outputs, strict=True):
            connection.execute(
                "UPDATE workflows SET outputs_json=?,edit_output_id=NULL,"
                "upload_source_id=NULL,upload_job_ids_json='[]' WHERE id=?",
                (outputs_json, workflow_id),
            )
        connection.execute("UPDATE metadata SET version=?", (SCHEMA_VERSION,))
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        _validate_connection(connection, SCHEMA_VERSION)
        connection.commit()
    except (OSError, sqlite3.Error, WorkflowSchemaError) as exc:
        if connection is not None:
            connection.rollback()
        raise WorkflowSchemaError from exc
    finally:
        if connection is not None:
            connection.close()


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


def ensure_workflow_schema(
    path: Path,
    *,
    legacy_profile_validator: LegacyProfileValidator | None = None,
) -> None:
    """Create Schema 2 atomically or migrate an exact Schema 1 database."""

    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise WorkflowSchemaError
    with _INITIALIZE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _plain_directory(path.parent)
            if not path.exists():
                _create_database(path)
            else:
                _migrate_to_current(path, legacy_profile_validator)
            validate_workflow_schema(path)
        except (OSError, sqlite3.Error, WorkflowSchemaError) as exc:
            raise WorkflowSchemaError from exc

"""Privacy-bounded ledger for remote AI invocation envelopes.

The ledger deliberately stores only identifiers, counters, and SHA-256
digests.  Request bodies, media paths, credentials, endpoints, and provider
responses never enter this module or its table.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .ai_authorization import (
    AiAuthorizationError,
    AiOperationAuthorization,
    parse_operation_authorization,
)


_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_OPERATIONS = frozenset({"transcribe", "translate", "synthesize"})
_STATES = frozenset(
    {"reserved", "dispatched", "responded", "released", "unknown", "reconciled"}
)
_RESOLUTIONS = frozenset(
    {"not_accepted", "accepted_without_result", "abandoned"}
)


class AiInvocationLedgerError(ValueError):
    """A stable ledger failure that does not expose stored or provider data."""

    _CODES = frozenset(
        {
            "ai_invocation_invalid",
            "ai_invocation_not_remote",
            "ai_invocation_owner_not_found",
            "ai_invocation_owner_inactive",
            "ai_invocation_owner_changed",
            "ai_invocation_conflict",
            "ai_invocation_budget_exceeded",
            "ai_invocation_not_found",
            "ai_invocation_state_conflict",
            "ai_invocation_database_unavailable",
        }
    )

    def __init__(self, code: str):
        self.code = code if code in self._CODES else "ai_invocation_invalid"
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AiInvocationLedgerError("ai_invocation_invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AiInvocationLedgerError("ai_invocation_invalid")
    return value


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise AiInvocationLedgerError("ai_invocation_invalid")
    return value


def _safe_code(value: object, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        raise AiInvocationLedgerError("ai_invocation_invalid")
    return value


def _authorization(value: object, operation: str) -> AiOperationAuthorization:
    try:
        result = parse_operation_authorization(value)
    except (AiAuthorizationError, TypeError, ValueError) as exc:
        raise AiInvocationLedgerError("ai_invocation_invalid") from exc
    if result is None or result.operation != operation:
        raise AiInvocationLedgerError("ai_invocation_invalid")
    if result.execution != "remote":
        raise AiInvocationLedgerError("ai_invocation_not_remote")
    return result


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ai_task_id": row["ai_task_id"],
        "render_plan_id": row["render_plan_id"],
        "operation": row["operation"],
        "ordinal": row["ordinal"],
        "attempt": row["attempt"],
        "request_units": row["request_units"],
        "authorization_sha256": row["authorization_sha256"],
        "owner_definition_sha256": row["owner_definition_sha256"],
        "request_fingerprint": row["request_fingerprint"],
        "state": row["state"],
        "resolution": row["resolution"],
        "reason_code": row["reason_code"],
        "revision": row["revision"],
        "legacy": bool(row["legacy"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "dispatched_at": row["dispatched_at"],
        "responded_at": row["responded_at"],
        "released_at": row["released_at"],
        "unknown_at": row["unknown_at"],
        "reconciled_at": row["reconciled_at"],
    }


class AiInvocationLedger:
    """Small SQLite API for one Editing Schema 4 invocation ledger."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.database_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
            connection.commit()
        except AiInvocationLedgerError:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise AiInvocationLedgerError(
                "ai_invocation_database_unavailable"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _owner(
        ai_task_id: object, render_plan_id: object
    ) -> tuple[str, str, str]:
        if (ai_task_id is None) == (render_plan_id is None):
            raise AiInvocationLedgerError("ai_invocation_invalid")
        if ai_task_id is not None:
            return "ai_task_id", _identifier(ai_task_id), "ai_tasks"
        return "render_plan_id", _identifier(render_plan_id), "render_plans"

    @staticmethod
    def _owner_authorization(
        db: sqlite3.Connection,
        *,
        owner_column: str,
        owner_id: str,
        operation: str,
        claim_token: str,
    ) -> tuple[AiOperationAuthorization, str]:
        if owner_column == "ai_task_id":
            row = db.execute(
                "SELECT operation,request,request_sha256,provider,model,state,claim_token "
                "FROM ai_tasks WHERE id=?",
                (owner_id,),
            ).fetchone()
            if row is None:
                raise AiInvocationLedgerError("ai_invocation_owner_not_found")
            if row["state"] != "running":
                raise AiInvocationLedgerError("ai_invocation_owner_inactive")
            if not hmac.compare_digest(str(row["claim_token"] or ""), claim_token):
                raise AiInvocationLedgerError("ai_invocation_owner_changed")
            if row["operation"] != operation or operation not in {
                "transcribe",
                "translate",
            }:
                raise AiInvocationLedgerError("ai_invocation_owner_changed")
            try:
                request = json.loads(row["request"])
                raw_authorization = request["authorization"]
            except (KeyError, TypeError, json.JSONDecodeError):
                raise AiInvocationLedgerError("ai_invocation_owner_changed") from None
        else:
            row = db.execute(
                "SELECT recipe,recipe_sha256,state,claim_token "
                "FROM render_plans WHERE id=?",
                (owner_id,),
            ).fetchone()
            if row is None:
                raise AiInvocationLedgerError("ai_invocation_owner_not_found")
            if row["state"] != "running":
                raise AiInvocationLedgerError("ai_invocation_owner_inactive")
            if not hmac.compare_digest(str(row["claim_token"] or ""), claim_token):
                raise AiInvocationLedgerError("ai_invocation_owner_changed")
            if operation != "synthesize":
                raise AiInvocationLedgerError("ai_invocation_owner_changed")
            try:
                recipe = json.loads(row["recipe"])
                dubbing = recipe["dubbing"]
                if not isinstance(dubbing, dict) or dubbing.get("enabled") is not True:
                    raise AiInvocationLedgerError("ai_invocation_owner_changed")
                raw_authorization = dubbing["authorization"]
            except (KeyError, TypeError, json.JSONDecodeError):
                raise AiInvocationLedgerError("ai_invocation_owner_changed") from None
        definition_field = (
            "request_sha256" if owner_column == "ai_task_id" else "recipe_sha256"
        )
        try:
            definition_sha256 = _digest(row[definition_field])
        except AiInvocationLedgerError as exc:
            raise AiInvocationLedgerError("ai_invocation_owner_changed") from exc
        raw_definition = row["request"] if owner_column == "ai_task_id" else row["recipe"]
        try:
            definition_matches = isinstance(raw_definition, str) and hmac.compare_digest(
                hashlib.sha256(raw_definition.encode("utf-8")).hexdigest(),
                definition_sha256,
            )
        except UnicodeError:
            definition_matches = False
        if not definition_matches:
            raise AiInvocationLedgerError("ai_invocation_owner_changed")
        approved = _authorization(raw_authorization, operation)
        if owner_column == "ai_task_id":
            if (
                request.get("provider_id") != approved.provider_id
                or request.get("model_id") != approved.model_id
                or row["provider"] != approved.provider_id
                or row["model"] != approved.model_id
            ):
                raise AiInvocationLedgerError("ai_invocation_owner_changed")
        elif (
            dubbing.get("provider") != approved.provider_id
            or dubbing.get("model") != approved.model_id
        ):
            raise AiInvocationLedgerError("ai_invocation_owner_changed")
        return approved, definition_sha256

    @staticmethod
    def _assert_active_owner(
        db: sqlite3.Connection, row: sqlite3.Row, claim_token: str, *, dispatch: bool
    ) -> None:
        table = "ai_tasks" if row["ai_task_id"] is not None else "render_plans"
        owner_id = row["ai_task_id"] or row["render_plan_id"]
        owner = db.execute(
            f"SELECT state,claim_token FROM {table} WHERE id=?", (owner_id,)
        ).fetchone()
        if owner is None:
            raise AiInvocationLedgerError("ai_invocation_owner_not_found")
        allowed = {"running"} if dispatch else {"running", "canceling"}
        if owner["state"] not in allowed:
            raise AiInvocationLedgerError("ai_invocation_owner_inactive")
        if not hmac.compare_digest(str(owner["claim_token"] or ""), claim_token):
            raise AiInvocationLedgerError("ai_invocation_owner_changed")

    def reserve(
        self,
        *,
        operation: str,
        ordinal: int,
        attempt: int,
        request_units: int,
        request_fingerprint: str,
        authorization: AiOperationAuthorization | Mapping[str, Any],
        owner_claim_token: str,
        ai_task_id: str | None = None,
        render_plan_id: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently reserve one exact remote invocation envelope."""

        if operation not in _OPERATIONS:
            raise AiInvocationLedgerError("ai_invocation_invalid")
        owner_column, owner_id, _ = self._owner(ai_task_id, render_plan_id)
        ordinal = _integer(ordinal, minimum=0, maximum=999_999)
        attempt = _integer(attempt, minimum=1, maximum=999_999)
        request_units = _integer(request_units, minimum=1, maximum=600)
        if operation != "synthesize" and ordinal != 0:
            raise AiInvocationLedgerError("ai_invocation_invalid")
        if operation in {"transcribe", "synthesize"} and request_units != 1:
            raise AiInvocationLedgerError("ai_invocation_invalid")
        if operation == "translate" and request_units > 20:
            raise AiInvocationLedgerError("ai_invocation_invalid")
        request_fingerprint = _digest(request_fingerprint)
        claim_token = _identifier(owner_claim_token)
        approved = _authorization(authorization, operation)
        maximum_units = int(approved.limits["max_requests"])
        if request_units > maximum_units:
            raise AiInvocationLedgerError("ai_invocation_budget_exceeded")

        owner_values = {
            "ai_task_id": owner_id if owner_column == "ai_task_id" else None,
            "render_plan_id": owner_id if owner_column == "render_plan_id" else None,
        }
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            stored_authorization, owner_definition_sha256 = self._owner_authorization(
                db,
                owner_column=owner_column,
                owner_id=owner_id,
                operation=operation,
                claim_token=claim_token,
            )
            if not hmac.compare_digest(stored_authorization.sha256, approved.sha256):
                raise AiInvocationLedgerError("ai_invocation_owner_changed")
            existing = db.execute(
                f"SELECT * FROM ai_invocations WHERE {owner_column}=? "
                "AND operation=? AND ordinal=? AND attempt=?",
                (owner_id, operation, ordinal, attempt),
            ).fetchone()
            if existing is not None:
                if (
                    existing["legacy"] != 0
                    or existing["request_units"] != request_units
                    or existing["owner_definition_sha256"]
                    != owner_definition_sha256
                    or existing["request_fingerprint"] != request_fingerprint
                    or not hmac.compare_digest(
                        str(existing["authorization_sha256"] or ""), approved.sha256
                    )
                ):
                    raise AiInvocationLedgerError("ai_invocation_conflict")
                return _record(existing)
            previous_attempt = db.execute(
                f"SELECT COALESCE(MAX(attempt),0) FROM ai_invocations "
                f"WHERE {owner_column}=? AND operation=? AND ordinal=?",
                (owner_id, operation, ordinal),
            ).fetchone()[0]
            if not isinstance(previous_attempt, int) or attempt != previous_attempt + 1:
                raise AiInvocationLedgerError("ai_invocation_conflict")
            if previous_attempt:
                predecessor = db.execute(
                    f"SELECT state,resolution FROM ai_invocations "
                    f"WHERE {owner_column}=? AND operation=? AND ordinal=? "
                    "AND attempt=?",
                    (owner_id, operation, ordinal, previous_attempt),
                ).fetchone()
                if predecessor is None or not (
                    predecessor["state"] == "released"
                    or (
                        predecessor["state"] == "reconciled"
                        and predecessor["resolution"] == "not_accepted"
                    )
                ):
                    raise AiInvocationLedgerError("ai_invocation_state_conflict")
            consumed = db.execute(
                f"SELECT COALESCE(SUM(request_units),0) FROM ai_invocations "
                f"WHERE {owner_column}=? AND operation=? AND "
                "NOT (state='released' OR "
                "(state='reconciled' AND resolution='not_accepted'))",
                (owner_id, operation),
            ).fetchone()[0]
            if not isinstance(consumed, int) or consumed + request_units > maximum_units:
                raise AiInvocationLedgerError("ai_invocation_budget_exceeded")
            invocation_id, now = uuid4().hex, _now()
            db.execute(
                "INSERT INTO ai_invocations("
                "id,ai_task_id,render_plan_id,operation,ordinal,attempt,request_units,"
                "authorization_sha256,owner_definition_sha256,request_fingerprint,"
                "state,resolution,reason_code,"
                "revision,legacy,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'reserved','', '',0,0,?,?)",
                (
                    invocation_id,
                    owner_values["ai_task_id"],
                    owner_values["render_plan_id"],
                    operation,
                    ordinal,
                    attempt,
                    request_units,
                    approved.sha256,
                    owner_definition_sha256,
                    request_fingerprint,
                    now,
                    now,
                ),
            )
            return _record(
                db.execute(
                    "SELECT * FROM ai_invocations WHERE id=?", (invocation_id,)
                ).fetchone()
            )

    def _transition(
        self,
        invocation_id: object,
        expected_revision: object,
        owner_claim_token: object,
        *,
        source_states: tuple[str, ...],
        target_state: str,
        reason_code: str,
        timestamp_column: str,
        resolution: str = "",
        dispatch: bool = False,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        invocation_id = _identifier(invocation_id)
        revision = _integer(expected_revision, minimum=0, maximum=999_999)
        claim_token = _identifier(owner_claim_token)
        reason_code = _safe_code(
            reason_code,
            {
                "dispatched": "remote_dispatch_started",
                "responded": "runtime_response_validated",
                "released": "remote_dispatch_not_started",
                "unknown": "remote_outcome_unknown",
            }[target_state],
        )
        now = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM ai_invocations WHERE id=?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise AiInvocationLedgerError("ai_invocation_not_found")
            self._assert_active_owner(db, row, claim_token, dispatch=dispatch)
            if idempotent and (
                row["state"] == target_state
                and row["revision"] == revision + 1
                and row["resolution"] == resolution
                and row["reason_code"] == reason_code
            ):
                return _record(row)
            if row["state"] not in source_states or row["revision"] != revision:
                raise AiInvocationLedgerError("ai_invocation_state_conflict")
            changed = db.execute(
                f"UPDATE ai_invocations SET state=?,resolution=?,reason_code=?,"
                f"revision=revision+1,updated_at=?,{timestamp_column}=? "
                "WHERE id=? AND state=? AND revision=?",
                (
                    target_state,
                    resolution,
                    reason_code,
                    now,
                    now,
                    invocation_id,
                    row["state"],
                    revision,
                ),
            ).rowcount
            if changed != 1:
                raise AiInvocationLedgerError("ai_invocation_state_conflict")
            return _record(
                db.execute(
                    "SELECT * FROM ai_invocations WHERE id=?", (invocation_id,)
                ).fetchone()
            )

    def dispatch(
        self, invocation_id: str, expected_revision: int, *, owner_claim_token: str
    ) -> dict[str, Any]:
        """Mark the last durable boundary immediately before remote dispatch."""

        return self._transition(
            invocation_id,
            expected_revision,
            owner_claim_token,
            source_states=("reserved",),
            target_state="dispatched",
            reason_code="remote_dispatch_started",
            timestamp_column="dispatched_at",
            dispatch=True,
            idempotent=False,
        )

    def respond(
        self,
        invocation_id: str,
        expected_revision: int,
        *,
        owner_claim_token: str,
        reason_code: str = "runtime_response_validated",
    ) -> dict[str, Any]:
        """Record only that the runtime returned a structurally valid response."""

        return self._transition(
            invocation_id,
            expected_revision,
            owner_claim_token,
            source_states=("dispatched",),
            target_state="responded",
            reason_code=reason_code,
            timestamp_column="responded_at",
        )

    def release(
        self,
        invocation_id: str,
        expected_revision: int,
        *,
        owner_claim_token: str,
        reason_code: str = "remote_dispatch_not_started",
    ) -> dict[str, Any]:
        """Close a reservation only while remote dispatch is still impossible."""

        return self._transition(
            invocation_id,
            expected_revision,
            owner_claim_token,
            source_states=("reserved",),
            target_state="released",
            reason_code=reason_code,
            timestamp_column="released_at",
        )

    def unknown(
        self,
        invocation_id: str,
        expected_revision: int,
        *,
        owner_claim_token: str,
        reason_code: str = "remote_outcome_unknown",
    ) -> dict[str, Any]:
        """Preserve a dispatched request whose runtime result is not trusted."""

        return self._transition(
            invocation_id,
            expected_revision,
            owner_claim_token,
            source_states=("dispatched",),
            target_state="unknown",
            reason_code=reason_code,
            timestamp_column="unknown_at",
        )

    def reconcile(
        self,
        invocation_id: str,
        expected_revision: int,
        *,
        resolution: str,
        reason_code: str = "remote_outcome_reconciled",
    ) -> dict[str, Any]:
        """Resolve unknown without storing a provider response or free-form note."""

        invocation_id = _identifier(invocation_id)
        revision = _integer(expected_revision, minimum=0, maximum=999_999)
        if resolution not in _RESOLUTIONS:
            raise AiInvocationLedgerError("ai_invocation_invalid")
        reason_code = _safe_code(reason_code, "remote_outcome_reconciled")
        now = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM ai_invocations WHERE id=?", (invocation_id,)
            ).fetchone()
            if current is None:
                raise AiInvocationLedgerError("ai_invocation_not_found")
            owner_table = (
                "ai_tasks" if current["ai_task_id"] is not None else "render_plans"
            )
            owner_id = current["ai_task_id"] or current["render_plan_id"]
            owner = db.execute(
                f"SELECT state FROM {owner_table} WHERE id=?", (owner_id,)
            ).fetchone()
            if owner is None:
                raise AiInvocationLedgerError("ai_invocation_owner_not_found")
            if owner["state"] not in {"failed", "canceled"}:
                raise AiInvocationLedgerError("ai_invocation_state_conflict")
            if (
                current["state"] == "reconciled"
                and current["revision"] == revision + 1
                and current["resolution"] == resolution
                and current["reason_code"] == reason_code
            ):
                return _record(current)
            changed = db.execute(
                "UPDATE ai_invocations SET state='reconciled',resolution=?,"
                "reason_code=?,revision=revision+1,updated_at=?,reconciled_at=? "
                "WHERE id=? AND state='unknown' AND revision=?",
                (resolution, reason_code, now, now, invocation_id, revision),
            ).rowcount
            if changed != 1:
                raise AiInvocationLedgerError("ai_invocation_state_conflict")
            return _record(
                db.execute(
                    "SELECT * FROM ai_invocations WHERE id=?", (invocation_id,)
                ).fetchone()
            )

    def list(
        self,
        *,
        ai_task_id: str | None = None,
        render_plan_id: str | None = None,
        project_id: str | None = None,
        state: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List redacted ledger rows, newest first."""

        clauses: list[str] = []
        values: list[object] = []
        if ai_task_id is not None and render_plan_id is not None:
            raise AiInvocationLedgerError("ai_invocation_invalid")
        if project_id is not None and (
            ai_task_id is not None or render_plan_id is not None
        ):
            raise AiInvocationLedgerError("ai_invocation_invalid")
        if ai_task_id is not None:
            clauses.append("ai_task_id=?")
            values.append(_identifier(ai_task_id))
        if render_plan_id is not None:
            clauses.append("render_plan_id=?")
            values.append(_identifier(render_plan_id))
        if project_id is not None:
            clauses.append(
                "(ai_task_id IN (SELECT id FROM ai_tasks WHERE project_id=?) OR "
                "render_plan_id IN (SELECT id FROM render_plans WHERE project_id=?))"
            )
            normalized_project_id = _identifier(project_id)
            values.extend((normalized_project_id, normalized_project_id))
        if state is not None:
            if state not in _STATES:
                raise AiInvocationLedgerError("ai_invocation_invalid")
            clauses.append("state=?")
            values.append(state)
        limit = _integer(limit, minimum=1, maximum=1_000)
        offset = _integer(offset, minimum=0, maximum=1_000_000)
        query = "SELECT * FROM ai_invocations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?"
        values.extend((limit, offset))
        with self._db() as db:
            return [_record(row) for row in db.execute(query, values).fetchall()]

    def unresolved(
        self,
        *,
        ai_task_id: str | None = None,
        render_plan_id: str | None = None,
        project_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List reservations, in-flight calls, and unknown outcomes."""

        if ai_task_id is not None and render_plan_id is not None:
            raise AiInvocationLedgerError("ai_invocation_invalid")
        if project_id is not None and (
            ai_task_id is not None or render_plan_id is not None
        ):
            raise AiInvocationLedgerError("ai_invocation_invalid")
        clauses = ["state IN ('reserved','dispatched','unknown')"]
        values: list[object] = []
        if ai_task_id is not None:
            clauses.append("ai_task_id=?")
            values.append(_identifier(ai_task_id))
        if render_plan_id is not None:
            clauses.append("render_plan_id=?")
            values.append(_identifier(render_plan_id))
        if project_id is not None:
            clauses.append(
                "(ai_task_id IN (SELECT id FROM ai_tasks WHERE project_id=?) OR "
                "render_plan_id IN (SELECT id FROM render_plans WHERE project_id=?))"
            )
            normalized_project_id = _identifier(project_id)
            values.extend((normalized_project_id, normalized_project_id))
        limit = _integer(limit, minimum=1, maximum=1_000)
        values.append(limit)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM ai_invocations WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at,id LIMIT ?",
                values,
            ).fetchall()
            return [_record(row) for row in rows]

    def retry_allowed(
        self,
        *,
        ai_task_id: str | None = None,
        render_plan_id: str | None = None,
    ) -> bool:
        """Return the conservative ledger-only retry decision for one owner.

        An empty ledger, a released reservation, or a validated response is
        safe for the owner's existing explicit retry rules to consider.  An
        unknown permits retry only after reconciliation established
        ``not_accepted``; pending, accepted-without-result, and abandoned rows
        block replay.
        """

        owner_column, owner_id, _ = self._owner(ai_task_id, render_plan_id)
        with self._db() as db:
            owner_table = (
                "ai_tasks" if owner_column == "ai_task_id" else "render_plans"
            )
            if db.execute(
                f"SELECT 1 FROM {owner_table} WHERE id=?", (owner_id,)
            ).fetchone() is None:
                raise AiInvocationLedgerError("ai_invocation_owner_not_found")
            rows = db.execute(
                f"SELECT state,resolution FROM ai_invocations "
                f"WHERE {owner_column}=? ORDER BY created_at,id",
                (owner_id,),
            ).fetchall()
        return all(
            row["state"] in {"released", "responded"}
            or (
                row["state"] == "reconciled"
                and row["resolution"] == "not_accepted"
            )
            for row in rows
        )

    def recover(self) -> dict[str, int]:
        """Release never-dispatched rows and preserve dispatched rows as unknown."""

        totals = {"released": 0, "unknown": 0}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,state,revision FROM ai_invocations "
                "WHERE state IN ('reserved','dispatched') ORDER BY created_at,id"
            ).fetchall()
            for row in rows:
                now = _now()
                if row["state"] == "reserved":
                    changed = db.execute(
                        "UPDATE ai_invocations SET state='released',"
                        "reason_code='process_restarted_before_dispatch',"
                        "revision=revision+1,updated_at=?,released_at=? "
                        "WHERE id=? AND state='reserved' AND revision=?",
                        (now, now, row["id"], row["revision"]),
                    ).rowcount
                    totals["released"] += changed
                else:
                    changed = db.execute(
                        "UPDATE ai_invocations SET state='unknown',"
                        "reason_code='process_restarted_after_dispatch',"
                        "revision=revision+1,updated_at=?,unknown_at=? "
                        "WHERE id=? AND state='dispatched' AND revision=?",
                        (now, now, row["id"], row["revision"]),
                    ).rowcount
                    totals["unknown"] += changed
        return totals


__all__ = ["AiInvocationLedger", "AiInvocationLedgerError"]

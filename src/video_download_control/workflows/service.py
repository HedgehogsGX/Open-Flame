"""Durable, restart-safe orchestration across download, edit and upload domains."""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Literal
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from .contracts import (
    AiSnapshot,
    CancellationSnapshot,
    EditSnapshot,
    MAX_WORKFLOW_ACCOUNTS,
    MAX_WORKFLOW_SEGMENTS,
    MAX_WORKFLOW_UPLOAD_JOBS,
    UploadSnapshot,
    WorkflowDomainAdapter,
    WorkflowError,
    workflow_outputs_match_state,
    workflow_upload_request_key,
)
from .profile import (
    canonical_workflow_mapping,
    normalize_resolved_upload,
    normalize_workflow_outputs,
    normalize_workflow_profile,
    normalize_workflow_text,
)
from .schema import SCHEMA_VERSION, WorkflowSchemaError, ensure_workflow_schema
from .snapshots import (
    AiSnapshotDisposition,
    EditSnapshotClassification,
    EditSnapshotDisposition,
    UploadSnapshotClassification,
    classify_ai_snapshot,
    classify_edit_snapshot,
    classify_upload_snapshot,
)


_REQUEST_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HEX_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_PROGRESS_LIMIT = MAX_WORKFLOW_SEGMENTS * 2 + 12
_CANCELLATION_REQUESTED = "workflow_cancellation_requested"
_SOURCE_CAPTION_REVIEW_CODE = "ai_source_caption_review_required"
_SERVICE_LOCKS_GUARD = RLock()
_SERVICE_LOCKS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_ACTIVE_STATES = {
    "created",
    "downloading",
    "preparing_edit",
    "awaiting_ai_review",
    "awaiting_edit_confirmation",
    "rendering",
    "preparing_upload",
    "awaiting_upload_confirmation",
    "uploading",
}
_CANCELLABLE_STATES = _ACTIVE_STATES | {"attention_required"}
_AI_LEDGER_REVIEW_CODES = frozenset(
    {
        "ai_remote_result_unknown",
        "ai_remote_retry_blocked",
        "ai_remote_reconciliation_required",
        "ai_remote_accepted_without_result",
        "ai_remote_abandoned",
    }
)
_AUTO_AI_REVIEW_CODES = frozenset(
    {
        "",
        "ai_transcription_confirmation_required",
        "ai_transcription_review_required",
        "ai_translation_confirmation_required",
        "ai_translation_review_required",
        "ai_restart_confirmation_required",
    }
)
_AUTO_EDIT_REVIEW_CODES = frozenset(
    {
        "",
        "edit_confirmation_required",
        "edit_review_confirmation_required",
        "explicit_confirmation_required",
        "restart_confirmation_required",
        "edit_restart_confirmation_required",
    }
)


def _edit_confirmation_code(code: str) -> str:
    if code == "restart_confirmation_required":
        return "edit_restart_confirmation_required"
    return code


_AUTO_UPLOAD_REVIEW_CODES = frozenset({"", "upload_restart_confirmation_required"})
_UPLOAD_RETRY_REVIEW_CODES = frozenset(
    {
        "upload_retry_confirmation_required",
        "upload_retry_mixed_confirmation_required",
    }
)
_SOURCE_METADATA_RETRY_CODE = "workflow_source_metadata_unavailable"
_PREFLIGHT_RETRY_CODES = frozenset(
    {
        "download_worker_unobserved",
        "download_runtime_unavailable",
        "download_queue_paused",
        "download_worker_stale",
        "download_worker_not_ready",
        "download_network_disabled",
        "processor_not_configured",
        "ai_runtime_missing",
        "ai_runtime_invalid",
        "ai_runtime_changed",
        "ai_runtime_unsupported",
        "ai_provider_not_found",
        "ai_provider_operation_unsupported",
        "ai_model_not_found",
        "ai_model_operation_unsupported",
        "ai_provider_auth_missing",
        "ai_provider_auth_environment_invalid",
        "ai_authorization_binding_required",
        "ai_authorization_changed",
        "ai_authorization_invalid",
        "ai_voice_not_allowed",
        "runtime_missing",
        "runtime_invalid",
        "runtime_busy",
        "runtime_upgrade_required",
        "runtime_unavailable",
        "unsupported_platform",
        "scheduler_owned_by_other_instance",
        "scheduler_database_unavailable",
        "scheduler_failed",
        "upload_scheduler_not_ready",
        "uploader_stopped",
        "upload_activity_busy",
        "account_not_found",
        "account_disconnected",
        "account_not_ready",
        "account_session_changed",
    }
)
_PREFLIGHT_WAIT_CODES = frozenset(
    {
        "download_worker_unobserved",
        "download_runtime_unavailable",
        "download_queue_paused",
        "download_worker_stale",
        "download_worker_not_ready",
        "download_network_disabled",
        "runtime_busy",
        "upload_activity_busy",
        "account_not_ready",
    }
)


def default_workflow_root(data_root: Path) -> Path:
    data_root = Path(data_root)
    return data_root.with_name(data_root.name + "-workflows")


def _download_identifier(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _migration_profile_is_valid(value: Mapping[str, object]) -> bool:
    """Apply the exact legacy profile contract before a Schema 1/2 migration."""

    upload = value.get("upload") if isinstance(value, Mapping) else None
    ai = value.get("ai") if isinstance(value, Mapping) else None
    if (
        not isinstance(upload, Mapping)
        or "title_mode" in upload
        or (
            isinstance(ai, Mapping)
            and "transcription_mode" in ai
        )
    ):
        # Schema 1 and 2 predate source-derived titles and source-caption
        # selection. A legacy database that already contains either future key
        # is forged or corrupt and must not be reinterpreted under the newer
        # contract.
        return False
    try:
        normalize_workflow_profile(value, bound_accounts=True)
    except WorkflowError:
        return False
    return True


def _output_ids_from_snapshot(snapshot: object) -> tuple[str, ...]:
    raw = getattr(snapshot, "output_ids", ())
    primary = getattr(snapshot, "output_id", None)
    if not raw:
        raw = () if primary is None else (primary,)
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or not 1 <= len(raw) <= MAX_WORKFLOW_SEGMENTS
        or any(not isinstance(item, str) or not _HEX_IDENTIFIER.fullmatch(item) for item in raw)
        or len(set(raw)) != len(raw)
        or (primary is not None and (len(raw) != 1 or primary != raw[0]))
    ):
        raise WorkflowError("workflow_domain_data_invalid")
    return tuple(raw)


def _unprepared_outputs(output_ids: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {
            "segment_ordinal": ordinal,
            "edit_output_id": output_id,
            "upload_source_id": None,
            "targets": [],
        }
        for ordinal, output_id in enumerate(output_ids, start=1)
    ]


def _service_lock(database_path: Path) -> Any:
    """Share one in-process mutation lock for every view of a workflow store."""

    try:
        key = os.path.normcase(str(database_path.resolve(strict=False)))
    except (OSError, RuntimeError):
        raise WorkflowError("workflow_database_unavailable") from None
    with _SERVICE_LOCKS_GUARD:
        lock = _SERVICE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _SERVICE_LOCKS[key] = lock
        return lock


class WorkflowService:
    """Own one small state machine; domain media and secrets stay in adapters."""

    def __init__(self, root: Path, adapter: WorkflowDomainAdapter) -> None:
        self.root = Path(root)
        self.database_path = self.root / "workflows.sqlite3"
        self.adapter = adapter
        self._lock = _service_lock(self.database_path)
        with self._lock:
            try:
                ensure_workflow_schema(
                    self.database_path,
                    legacy_profile_validator=_migration_profile_is_valid,
                )
            except WorkflowSchemaError:
                raise WorkflowError("workflow_database_unavailable") from None

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _preflight(
        self,
        profile: Mapping[str, Any],
        *,
        expected_account_bindings: Sequence[Mapping[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        recipe = profile["edit_recipe"]
        cover = recipe.get("cover")
        upload = dict(profile["upload"])
        upload.pop("account_bindings", None)
        raw_bindings = self.adapter.preflight(
            recipe,
            profile["ai"],
            upload,
            cover_aspect_ratio=(
                None if cover is None else cover.get("aspect_ratio")
            ),
            expected_account_bindings=expected_account_bindings,
        )
        if (
            not isinstance(raw_bindings, Sequence)
            or isinstance(raw_bindings, (str, bytes))
            or any(not isinstance(item, Mapping) for item in raw_bindings)
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        bindings = [dict(item) for item in raw_bindings]
        if expected_account_bindings is not None and bindings != [
            dict(item) for item in expected_account_bindings
        ]:
            raise WorkflowError("account_session_changed")
        return bindings

    def create(
        self,
        *,
        source_url: str,
        name: str,
        profile: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not _REQUEST_KEY.fullmatch(
            idempotency_key
        ):
            raise WorkflowError("invalid_idempotency_key")
        source_url = normalize_workflow_text(source_url, 4096, required=True)
        if not source_url.startswith(("https://", "http://")):
            raise WorkflowError("invalid_source_url")
        name = normalize_workflow_text(name, 160, required=True)
        normalized, _encoded, intent_profile_digest = normalize_workflow_profile(
            profile, require_ai_authorization=True
        )
        request_payload = {
            "source_url": source_url,
            "name": name,
            "profile_sha256": intent_profile_digest,
        }
        _, request_digest = canonical_workflow_mapping(request_payload)
        with self._lock:
            with self._db() as db:
                prior = db.execute(
                    "SELECT * FROM workflows WHERE request_key=?", (idempotency_key,)
                ).fetchone()
                if prior is not None:
                    if prior["request_digest"] != request_digest:
                        raise WorkflowError("idempotency_conflict")
                    return self._public(prior)
        # Runtime integrity may require a cold scan. It has no workflow-domain
        # side effects, so do not hold the global mutation lock while it runs.
        # The request key is checked again in the insertion transaction below.
        normalized["upload"]["account_bindings"] = self._preflight(normalized)
        normalized, encoded, profile_digest = normalize_workflow_profile(
            normalized,
            bound_accounts=True,
            require_ai_authorization=True,
        )
        with self._lock:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute(
                    "SELECT * FROM workflows WHERE request_key=?", (idempotency_key,)
                ).fetchone()
                if prior is not None:
                    if prior["request_digest"] != request_digest:
                        raise WorkflowError("idempotency_conflict")
                    return self._public(prior)
                workflow_id = uuid4().hex
                now = _now()
                db.execute(
                """INSERT INTO workflows(
 id,request_key,request_digest,source_url,name,profile_json,profile_sha256,
 state,code,batch_name,upload_job_ids_json,auto_confirm_edit,
 auto_confirm_upload,revision,created_at,updated_at)
 VALUES(?,?,?,?,?,?,?,'created','',?,'[]',?,?,1,?,?)""",
                (
                    workflow_id,
                    idempotency_key,
                    request_digest,
                    source_url,
                    name,
                    encoded,
                    profile_digest,
                    f"Open-Flame workflow {workflow_id}",
                    int(normalized["auto_confirm_edit"]),
                    int(normalized["auto_confirm_upload"]),
                    now,
                    now,
                ),
                )
                self._event(db, workflow_id, None, "created", "", now)
                return self._by_id(db, workflow_id)

    def get(self, workflow_id: str) -> dict[str, Any]:
        workflow_id = self._identifier(workflow_id)
        with self._db() as db:
            return self._by_id(db, workflow_id)

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WorkflowError("invalid_limit")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM workflows ORDER BY created_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def active_page(
        self,
        after: tuple[str, str] | None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one cursor page so every durable active workflow is reconciled."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WorkflowError("invalid_limit")
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 2
            or not all(isinstance(item, str) and item for item in after)
        ):
            raise WorkflowError("workflow_data_invalid")
        states = tuple(sorted(_ACTIVE_STATES))
        placeholders = ",".join("?" for _ in states)

        def fetch(cursor: tuple[str, str] | None) -> list[sqlite3.Row]:
            query = (
                f"SELECT * FROM workflows WHERE (state IN ({placeholders}) OR "
                "(state='attention_required' AND code=?))"
            )
            values: list[object] = [*states, _CANCELLATION_REQUESTED]
            if cursor is not None:
                query += " AND (created_at>? OR (created_at=? AND id>?))"
                values.extend((cursor[0], cursor[0], cursor[1]))
            query += " ORDER BY created_at,id LIMIT ?"
            values.append(limit)
            with self._db() as db:
                return db.execute(query, values).fetchall()

        rows = fetch(after)
        if not rows and after is not None:
            rows = fetch(None)
        return [self._public(row) for row in rows]

    def events(self, workflow_id: str) -> list[dict[str, Any]]:
        workflow_id = self._identifier(workflow_id)
        with self._db() as db:
            if db.execute("SELECT 1 FROM workflows WHERE id=?", (workflow_id,)).fetchone() is None:
                raise WorkflowError("workflow_not_found")
            rows = db.execute(
                "SELECT sequence,from_state,to_state,code,created_at "
                "FROM workflow_events WHERE workflow_id=? ORDER BY sequence",
                (workflow_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def advance(self, workflow_id: str) -> dict[str, Any]:
        """Advance synchronous seams until the workflow must wait for a domain."""

        workflow_id = self._identifier(workflow_id)
        with self._lock:
            for _ in range(_WORKFLOW_PROGRESS_LIMIT):
                record = self.get(workflow_id)
                state = record["state"]
                if record["code"] == _CANCELLATION_REQUESTED:
                    progressed = self._advance_once(record)
                    if not progressed:
                        return self.get(workflow_id)
                    continue
                if (
                    state == "attention_required"
                    and record["outputs"]
                    and any(
                        item["upload_source_id"] is None
                        for item in record["outputs"]
                    )
                ):
                    # Upload preparation only creates local drafts. An explicit
                    # advance may safely replay the first unfinished segment by
                    # its stable idempotency key; already checkpointed segments
                    # stay drafts until the complete fan-out is confirmed.
                    self._transition(
                        workflow_id, "preparing_upload", "", expected=record
                    )
                    continue
                if state == "attention_required" and record["upload_job_ids"]:
                    snapshot, classification, _ = self._observe_upload(record)
                    if classification.disposition == "ready":
                        self._record_upload_outcome(record, snapshot)
                        return self.get(workflow_id)
                    if classification.disposition in {
                        "waiting_confirmation",
                        "waiting_active",
                    }:
                        needs_confirmation = (
                            classification.disposition == "waiting_confirmation"
                        )
                        self._transition(
                            workflow_id,
                            (
                                "awaiting_upload_confirmation"
                                if needs_confirmation
                                else "uploading"
                            ),
                            classification.code if needs_confirmation else "",
                            expected=record,
                        )
                        continue
                    if classification.disposition == "invalid":
                        return self._attention(
                            workflow_id,
                            "workflow_domain_data_invalid",
                            expected=record,
                        )
                    return self.get(workflow_id)
                if (
                    state == "attention_required"
                    and record["batch_id"] is None
                    and record["code"] in _PREFLIGHT_RETRY_CODES
                ):
                    # Environment setup, credentials, or a selected account may
                    # have been repaired after the zero-side-effect preflight.
                    # Only an explicit advance reaches inactive attention rows.
                    self._transition(workflow_id, "created", "", expected=record)
                    continue
                if (
                    state == "attention_required"
                    and record["code"] == _SOURCE_METADATA_RETRY_CODE
                    and record["batch_id"] is not None
                    and record["download_asset_id"] is not None
                    and record["resolved_upload"] is None
                    and record["profile"]["upload"].get("title_mode") == "source"
                ):
                    # Metadata and capability reads are local and replayable.
                    # An explicit reconcile can retry them without repeating
                    # the completed download or creating edit/upload effects.
                    self._transition(workflow_id, "downloading", "", expected=record)
                    continue
                if (
                    state == "attention_required"
                    and record["code"] in _AI_LEDGER_REVIEW_CODES
                    and record["edit_project_id"]
                ):
                    if record["edit_plan_id"]:
                        classification, disposition = self._observe_edit(
                            record, context="reconcile"
                        )
                        if disposition in {"failed", "attention", "invalid"}:
                            return self.get(workflow_id)
                        if disposition == "ready":
                            continue
                        self._transition(
                            workflow_id,
                            (
                                "awaiting_edit_confirmation"
                                if disposition == "waiting_confirmation"
                                else "rendering"
                            ),
                            (
                                classification.code
                                if disposition == "waiting_confirmation"
                                else ""
                            ),
                            expected=record,
                        )
                        continue
                    if record["profile"]["ai"] is None:
                        return self._attention(
                            workflow_id,
                            "workflow_domain_data_invalid",
                            expected=record,
                        )
                    snapshot = self.adapter.advance_ai(
                        workflow_id,
                        record["edit_project_id"],
                        record["profile"]["edit_recipe"],
                        record["profile"]["ai"],
                        authorize=False,
                        explicit=False,
                    )
                    disposition = self._apply_ai_snapshot(
                        record, snapshot, context="reconcile"
                    )
                    if disposition == "attention":
                        return self.get(workflow_id)
                    continue
                if state not in _ACTIVE_STATES:
                    return record
                progressed = self._advance_once(record)
                if not progressed:
                    return self.get(workflow_id)
            record = self.get(workflow_id)
            if record["code"] == _CANCELLATION_REQUESTED:
                return record
            return self._attention(
                workflow_id, "workflow_progress_limit", expected=record
            )

    def cancel(self, workflow_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Persist cancellation intent, then reconcile the furthest domain seam."""

        workflow_id = self._identifier(workflow_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise WorkflowError("invalid_revision")
        with self._lock:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM workflows WHERE id=?", (workflow_id,)
                ).fetchone()
                if row is None:
                    raise WorkflowError("workflow_not_found")
                record = self._public(row)
                if record["state"] == "canceled":
                    return record
                if record["state"] == "completed":
                    raise WorkflowError("workflow_state_conflict")
                if record["code"] != _CANCELLATION_REQUESTED:
                    if record["revision"] != expected_revision:
                        raise WorkflowError("workflow_revision_conflict")
                    if record["state"] not in _CANCELLABLE_STATES:
                        raise WorkflowError("workflow_state_conflict")
                    now = _now()
                    changed = db.execute(
                        "UPDATE workflows SET code=?,revision=revision+1,updated_at=?,"
                        "finished_at=? WHERE id=? AND revision=? AND state=? AND code=?",
                        (
                            _CANCELLATION_REQUESTED,
                            now,
                            now if record["state"] == "attention_required" else None,
                            workflow_id,
                            record["revision"],
                            record["state"],
                            record["code"],
                        ),
                    ).rowcount
                    if changed != 1:
                        raise WorkflowError("workflow_revision_conflict")
                    self._event(
                        db,
                        workflow_id,
                        record["state"],
                        record["state"],
                        _CANCELLATION_REQUESTED,
                        now,
                    )
                elif record["state"] not in _CANCELLABLE_STATES:
                    raise WorkflowError("workflow_state_conflict")
            return self.advance(workflow_id)

    def confirm_edit(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        expected_profile_sha256: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._expected(
                workflow_id, expected_revision, expected_profile_sha256
            )
            if record["profile"]["ai"] is not None and expected_profile_sha256 is None:
                raise WorkflowError("workflow_profile_confirmation_required")
            if record["state"] != "awaiting_edit_confirmation" or not record["edit_plan_id"]:
                raise WorkflowError("workflow_state_conflict")
            classification, disposition = self._observe_edit(
                record, context="observe"
            )
            if disposition in {"failed", "attention", "invalid"}:
                return self.get(workflow_id)
            if disposition == "ready":
                return self.advance(workflow_id)
            if disposition == "waiting_active":
                self._transition(workflow_id, "rendering", "", expected=record)
                return self.advance(workflow_id)
            observed_code = _edit_confirmation_code(classification.code)
            persisted_code = _edit_confirmation_code(record["code"])
            if observed_code and observed_code != persisted_code:
                return self._transition(
                    workflow_id,
                    "awaiting_edit_confirmation",
                    observed_code,
                    expected=record,
                )
            try:
                self.adapter.confirm_edit(record["edit_plan_id"])
            except WorkflowError as error:
                if error.code in _AI_LEDGER_REVIEW_CODES:
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            self._transition(workflow_id, "rendering", "", expected=record)
            return self.advance(workflow_id)

    def confirm_ai(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        expected_profile_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Authorize exactly the AI action or result review currently waiting."""

        with self._lock:
            record = self._expected(
                workflow_id, expected_revision, expected_profile_sha256
            )
            if expected_profile_sha256 is None:
                raise WorkflowError("workflow_profile_confirmation_required")
            if record["state"] != "awaiting_ai_review" or not record["edit_project_id"]:
                raise WorkflowError("workflow_state_conflict")
            try:
                snapshot = self.adapter.advance_ai(
                    workflow_id,
                    record["edit_project_id"],
                    record["profile"]["edit_recipe"],
                    record["profile"]["ai"],
                    authorize=True,
                    explicit=True,
                )
            except WorkflowError as error:
                if error.code in _AI_LEDGER_REVIEW_CODES:
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            disposition = self._apply_ai_snapshot(
                record, snapshot, context="explicit"
            )
            if disposition == "attention":
                return self.get(workflow_id)
            return self.advance(workflow_id)

    def confirm_upload(self, workflow_id: str, *, expected_revision: int) -> dict[str, Any]:
        with self._lock:
            record = self._expected(workflow_id, expected_revision)
            if record["state"] != "awaiting_upload_confirmation":
                raise WorkflowError("workflow_state_conflict")
            snapshot, classification, job_ids_changed = self._observe_upload(record)
            if job_ids_changed:
                return self.get(workflow_id)
            if classification.disposition == "attention":
                return self._attention(
                    workflow_id,
                    classification.code,
                    expected=record,
                )
            if classification.disposition == "ready":
                self._record_upload_outcome(record, snapshot)
                return self.get(workflow_id)
            if classification.disposition == "invalid":
                return self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
            if classification.disposition == "waiting_active":
                self._transition(workflow_id, "uploading", "", expected=record)
                return self.advance(workflow_id)
            try:
                self.adapter.confirm_uploads(
                    record["upload_job_ids"],
                    record["profile"]["upload"]["account_bindings"],
                )
            except WorkflowError as error:
                if error.code == "account_session_changed":
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            self._transition(workflow_id, "uploading", "", expected=record)
            return self.advance(workflow_id)

    def retry(self, workflow_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Create explicitly reviewable successors for a failed workflow step."""

        with self._lock:
            record = self._expected(workflow_id, expected_revision)
            if record["state"] != "attention_required":
                raise WorkflowError("workflow_retry_not_available")
            if record["code"] in _AI_LEDGER_REVIEW_CODES:
                raise WorkflowError("workflow_retry_not_available")
            if record["upload_job_ids"]:
                return self._retry_upload(record)
            if not record["edit_project_id"]:
                raise WorkflowError("workflow_retry_not_available")
            if record["edit_plan_id"] is not None:
                if record["edit_output_ids"] or record["upload_job_ids"]:
                    raise WorkflowError("workflow_retry_not_available")
                classification, disposition = self._observe_edit(
                    record, context="retry"
                )
                if disposition in {"ready", "attention", "invalid"}:
                    return self.get(workflow_id)
                if disposition == "waiting_active":
                    self._transition(
                        workflow_id, "rendering", "", expected=record
                    )
                    return self.get(workflow_id)
                if disposition == "waiting_confirmation":
                    review_code = classification.code
                    if review_code in _AUTO_EDIT_REVIEW_CODES:
                        review_code = "render_retry_confirmation_required"
                    self._transition(
                        workflow_id,
                        "awaiting_edit_confirmation",
                        review_code,
                        expected=record,
                    )
                    return self.get(workflow_id)
                if disposition != "failed":
                    raise WorkflowError("workflow_retry_not_available")
                if classification.code != record["code"]:
                    return self._attention(
                        workflow_id, classification.code, expected=record
                    )
                try:
                    plan_id = self.adapter.retry_edit(
                        workflow_id, record["edit_plan_id"]
                    )
                except WorkflowError as error:
                    if error.code in _AI_LEDGER_REVIEW_CODES:
                        return self._attention(
                            workflow_id, error.code, expected=record
                        )
                    raise
                record = self._set_refs(
                    workflow_id, expected=record, edit_plan_id=plan_id
                )
                self._transition(
                    workflow_id,
                    "awaiting_edit_confirmation",
                    "render_retry_confirmation_required",
                    expected=record,
                )
                return self.get(workflow_id)
            if record["profile"]["ai"] is None:
                raise WorkflowError("workflow_retry_not_available")
            try:
                self.adapter.retry_ai(
                    workflow_id,
                    record["edit_project_id"],
                    record["profile"]["edit_recipe"],
                    record["profile"]["ai"],
                )
            except WorkflowError as error:
                if error.code in _AI_LEDGER_REVIEW_CODES:
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            self._transition(
                workflow_id,
                "awaiting_ai_review",
                "ai_retry_confirmation_required",
                expected=record,
            )
            return self.get(workflow_id)

    def _retry_upload(self, record: dict[str, Any]) -> dict[str, Any]:
        """Retry a complete failed upload fan-out behind a fresh confirmation."""

        workflow_id = record["id"]
        if record["code"] != "upload_job_failed":
            raise WorkflowError("workflow_retry_not_available")
        outputs = record["outputs"]
        account_ids = record["profile"]["upload"]["account_ids"]
        expected_job_count = len(outputs) * len(account_ids)
        if (
            not outputs
            or not account_ids
            or any(output["upload_source_id"] is None for output in outputs)
            or any(len(output["targets"]) != len(account_ids) for output in outputs)
            or len(record["upload_job_ids"]) != expected_job_count
        ):
            return self._attention(
                workflow_id,
                "workflow_domain_data_invalid",
                expected=record,
            )

        snapshot, classification, _job_ids_changed = self._observe_upload(record)
        applied = self._apply_upload_retry_observation(record, snapshot, classification)
        if applied is not None:
            return applied
        if classification.disposition == "invalid":
            return self._attention(
                workflow_id,
                "workflow_domain_data_invalid",
                expected=record,
            )
        if classification.code != "upload_job_failed":
            return self._attention(
                workflow_id, classification.code, expected=record
            )

        retry_snapshot = self.adapter.retry_uploads(
            record["upload_job_ids"],
            expected_targets=self._upload_targets(record),
            account_bindings=record["profile"]["upload"]["account_bindings"],
            expected_request_keys=self._upload_request_keys(record),
            expected_upload=self._upload_for_execution(record),
            expected_upload_cover_id=record["upload_cover_id"],
        )
        self._sync_upload_job_ids(record, retry_snapshot)
        retry_classification = classify_upload_snapshot(retry_snapshot)
        applied = self._apply_upload_retry_observation(
            record, retry_snapshot, retry_classification
        )
        if applied is not None:
            return applied
        return self._attention(
            workflow_id,
            (
                retry_classification.code
                if retry_classification.disposition == "attention"
                else "workflow_domain_data_invalid"
            ),
            expected=record,
        )

    def _apply_upload_retry_observation(
        self,
        record: dict[str, Any],
        snapshot: UploadSnapshot,
        classification: UploadSnapshotClassification,
    ) -> dict[str, Any] | None:
        """Apply a checkpointed success/waiting result without authorizing a retry."""

        workflow_id = record["id"]
        if classification.disposition == "ready":
            self._record_upload_outcome(record, snapshot)
            return self.get(workflow_id)
        if classification.disposition == "waiting_active":
            self._transition(
                workflow_id,
                "uploading",
                classification.code,
                expected=record,
            )
            return self.get(workflow_id)
        if classification.disposition == "waiting_confirmation":
            if classification.code not in _UPLOAD_RETRY_REVIEW_CODES:
                return self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
            self._transition(
                workflow_id,
                "awaiting_upload_confirmation",
                classification.code,
                expected=record,
            )
            return self.get(workflow_id)
        return None

    def require_attention(
        self,
        workflow_id: str,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            code = "workflow_failed"
        with self._lock:
            record = (
                self.get(self._identifier(workflow_id))
                if expected_revision is None
                else self._expected(workflow_id, expected_revision)
            )
            if record["code"] == _CANCELLATION_REQUESTED:
                return record
            if record["state"] not in _ACTIVE_STATES:
                return record
            return self._attention(record["id"], code, expected=record)

    def _advance_once(self, record: dict[str, Any]) -> bool:
        workflow_id, state = record["id"], record["state"]
        if record["code"] == _CANCELLATION_REQUESTED:
            try:
                snapshot = self._cancel_furthest_domain(record)
            except WorkflowError as error:
                self._attention(
                    workflow_id,
                    error.code or "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            if snapshot is None or snapshot.status == "stopped":
                self._transition(
                    workflow_id,
                    "canceled",
                    "workflow_canceled",
                    expected=record,
                )
                return True
            if snapshot.status == "waiting":
                return False
            if snapshot.status == "attention":
                self._attention(
                    workflow_id,
                    snapshot.code or "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            if snapshot.status == "upload_completed":
                return self._record_upload_outcome(
                    record,
                    UploadSnapshot("ready", outcome=snapshot.outcome),
                )
            self._attention(
                workflow_id, "workflow_domain_data_invalid", expected=record
            )
            return False
        if state == "created":
            try:
                self._preflight(
                    record["profile"],
                    expected_account_bindings=record["profile"]["upload"][
                        "account_bindings"
                    ],
                )
            except WorkflowError as error:
                if error.code in _PREFLIGHT_WAIT_CODES:
                    self._transition(
                        workflow_id, "created", error.code, expected=record
                    )
                else:
                    self._attention(workflow_id, error.code, expected=record)
                return False
            batch_id = self.adapter.create_download(
                workflow_id,
                record["source_url"],
                record["profile"]["download_credential_mode"],
            )
            record = self._set_refs(
                workflow_id, expected=record, batch_id=batch_id
            )
            self._transition(workflow_id, "downloading", "", expected=record)
            return True
        if state == "downloading":
            if not record["batch_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            snapshot = self.adapter.inspect_download(record["batch_id"])
            if snapshot.status == "waiting":
                return False
            if snapshot.status != "ready" or not snapshot.asset_id:
                self._attention(
                    workflow_id,
                    snapshot.code or "download_attention_required",
                    expected=record,
                )
                return False
            if (
                record["download_asset_id"] is not None
                and record["download_asset_id"] != snapshot.asset_id
            ):
                # Once a ready asset has been observed, retries may fill in
                # metadata but must never retarget the workflow to a different
                # media object.
                self._attention(
                    workflow_id,
                    "workflow_data_invalid",
                    expected=record,
                )
                return False
            refs: dict[str, object] = {"download_asset_id": snapshot.asset_id}
            upload_profile = record["profile"]["upload"]
            if upload_profile.get("title_mode") == "source":
                if record["resolved_upload"] is not None:
                    if record["download_asset_id"] != snapshot.asset_id:
                        self._attention(
                            workflow_id,
                            "workflow_data_invalid",
                            expected=record,
                        )
                        return False
                    # The snapshot and asset id were committed together before
                    # the state transition.  Reuse them after a crash instead
                    # of resolving against metadata or capabilities that may
                    # have changed in the meantime.
                    self._transition(
                        workflow_id,
                        "preparing_edit",
                        "",
                        expected=record,
                    )
                    return True
                resolver = getattr(self.adapter, "resolve_upload", None)
                if not callable(resolver):
                    record = self._set_refs(
                        workflow_id,
                        expected=record,
                        download_asset_id=snapshot.asset_id,
                    )
                    self._attention(
                        workflow_id,
                        "workflow_source_metadata_unavailable",
                        expected=record,
                    )
                    return False
                try:
                    candidate = resolver(upload_profile, snapshot)
                    resolved_upload, _ = normalize_resolved_upload(
                        candidate,
                        record["profile"],
                    )
                except WorkflowError as error:
                    record = self._set_refs(
                        workflow_id,
                        expected=record,
                        download_asset_id=snapshot.asset_id,
                    )
                    self._attention(workflow_id, error.code, expected=record)
                    return False
                refs["resolved_upload"] = resolved_upload
            record = self._set_refs(workflow_id, expected=record, **refs)
            self._transition(
                workflow_id, "preparing_edit", "", expected=record
            )
            return True
        if state == "preparing_edit":
            if not record["download_asset_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            prepared = self.adapter.prepare_edit(
                workflow_id,
                record["download_asset_id"],
                record["profile"]["edit_recipe"],
            )
            record = self._set_refs(
                workflow_id,
                expected=record,
                edit_project_id=prepared.project_id,
                edit_draft_version=prepared.draft_version,
                edit_plan_id=prepared.plan_id,
            )
            next_state = (
                "awaiting_ai_review"
                if prepared.awaiting_ai_review
                else "awaiting_edit_confirmation"
            )
            self._transition(workflow_id, next_state, "", expected=record)
            return True
        if state == "awaiting_ai_review":
            if not record["edit_project_id"] or record["profile"]["ai"] is None:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            observing_source_caption = (
                record["code"] == _SOURCE_CAPTION_REVIEW_CODE
            )
            if record["code"] and not observing_source_caption and (
                not record["auto_confirm_edit"]
                or record["code"] not in _AUTO_AI_REVIEW_CODES
            ):
                return False
            snapshot = self.adapter.advance_ai(
                workflow_id,
                record["edit_project_id"],
                record["profile"]["edit_recipe"],
                record["profile"]["ai"],
                authorize=(
                    record["auto_confirm_edit"]
                    and not observing_source_caption
                ),
                explicit=False,
            )
            disposition = self._apply_ai_snapshot(
                record, snapshot, context="automatic"
            )
            return disposition == "ready"
        if state == "awaiting_edit_confirmation":
            if not record["edit_plan_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            classification, disposition = self._observe_edit(
                record, context="observe"
            )
            if disposition in {"failed", "attention", "invalid"}:
                return False
            if disposition == "ready":
                return True
            if disposition == "waiting_active":
                self._transition(workflow_id, "rendering", "", expected=record)
                return True
            if record["code"] not in _AUTO_EDIT_REVIEW_CODES:
                return False
            code = _edit_confirmation_code(
                classification.code or record["code"]
            )
            if code and code != record["code"]:
                record = self._transition(
                    workflow_id, state, code, expected=record
                )
            if (
                not record["auto_confirm_edit"]
                or code not in _AUTO_EDIT_REVIEW_CODES
            ):
                return False
            self.adapter.confirm_edit(record["edit_plan_id"])
            self._transition(workflow_id, "rendering", "", expected=record)
            return True
        if state == "rendering":
            if not record["edit_plan_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            classification, disposition = self._observe_edit(
                record, context="observe"
            )
            if disposition in {"failed", "attention", "invalid"}:
                return False
            if disposition == "ready":
                return True
            if disposition == "waiting_active" or not classification.code:
                return False
            self._transition(
                workflow_id,
                "awaiting_edit_confirmation",
                _edit_confirmation_code(classification.code),
                expected=record,
            )
            return False
        if state == "preparing_upload":
            outputs = record["outputs"]
            if not outputs:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            pending = next(
                (item for item in outputs if item["upload_source_id"] is None), None
            )
            if pending is None:
                self._transition(
                    workflow_id,
                    "awaiting_upload_confirmation",
                    "",
                    expected=record,
                )
                return True
            try:
                upload = self._upload_for_execution(record)
                upload_kwargs: dict[str, object] = {
                    "segment_ordinal": pending["segment_ordinal"]
                }
                if upload.get("prefer_download_cover") is True:
                    if not record["download_asset_id"]:
                        raise WorkflowError("workflow_data_invalid")
                    if record["upload_cover_id"] is None:
                        selected_cover_id = self.adapter.select_upload_cover(
                            workflow_id,
                            pending["edit_output_id"],
                            record["edit_cover_id"],
                            upload,
                            segment_ordinal=pending["segment_ordinal"],
                            download_asset_id=record["download_asset_id"],
                        )
                        self._set_refs(
                            workflow_id,
                            expected=record,
                            upload_cover_id=selected_cover_id,
                        )
                        return True
                    upload_kwargs.update(
                        download_asset_id=record["download_asset_id"],
                        expected_upload_cover_id=record["upload_cover_id"],
                    )
                prepared = self.adapter.prepare_upload(
                    workflow_id,
                    pending["edit_output_id"],
                    record["edit_cover_id"],
                    upload,
                    **upload_kwargs,
                )
            except WorkflowError as error:
                self._attention(workflow_id, error.code, expected=record)
                return False
            job_ids = tuple(prepared.job_ids)
            account_ids = record["profile"]["upload"]["account_ids"]
            bindings = record["profile"]["upload"]["account_bindings"]
            platforms = {item["account_id"]: item["platform"] for item in bindings}
            if len(job_ids) != len(account_ids):
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            replacement = [dict(item) for item in outputs]
            index = pending["segment_ordinal"] - 1
            replacement[index] = {
                **pending,
                "upload_source_id": prepared.source_id,
                "targets": [
                    {
                        "account_id": account_id,
                        "platform": platforms[account_id],
                        "job_id": job_id,
                    }
                    for account_id, job_id in zip(account_ids, job_ids, strict=True)
                ],
            }
            if record["upload_cover_id"] not in {None, prepared.cover_id}:
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            self._set_refs(
                workflow_id,
                expected=record,
                outputs=replacement,
                upload_cover_id=prepared.cover_id,
            )
            return True
        if state == "awaiting_upload_confirmation":
            snapshot, classification, _ = self._observe_upload(record)
            if classification.disposition == "attention":
                self._attention(
                    workflow_id,
                    classification.code,
                    expected=record,
                )
                return False
            if classification.disposition == "ready":
                return self._record_upload_outcome(record, snapshot)
            if classification.disposition == "invalid":
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            if classification.disposition == "waiting_active":
                self._transition(workflow_id, "uploading", "", expected=record)
                return True
            if record["code"] not in _AUTO_UPLOAD_REVIEW_CODES:
                return False
            if classification.code and classification.code != record["code"]:
                record = self._transition(
                    workflow_id, state, classification.code, expected=record
                )
            if (
                not record["auto_confirm_upload"]
                or classification.code not in _AUTO_UPLOAD_REVIEW_CODES
            ):
                return False
            self.adapter.confirm_uploads(
                record["upload_job_ids"],
                record["profile"]["upload"]["account_bindings"],
            )
            self._transition(workflow_id, "uploading", "", expected=record)
            return True
        if state == "uploading":
            snapshot, classification, _ = self._observe_upload(record)
            if classification.disposition == "waiting_confirmation":
                self._transition(
                    workflow_id,
                    "awaiting_upload_confirmation",
                    classification.code,
                    expected=record,
                )
                return False
            if classification.disposition == "waiting_active":
                return False
            if classification.disposition == "attention":
                self._attention(
                    workflow_id,
                    classification.code,
                    expected=record,
                )
                return False
            if classification.disposition == "invalid":
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            return self._record_upload_outcome(record, snapshot)
        return False

    def _cancel_furthest_domain(
        self, record: Mapping[str, Any]
    ) -> CancellationSnapshot | None:
        pending_upload = next(
            (
                output
                for output in record["outputs"]
                if output["upload_source_id"] is None
            ),
            None,
        )
        if pending_upload is not None:
            expected_upload = self._upload_for_execution(record)
            discovery_kwargs: dict[str, object] = {
                "expected_targets": self._upload_targets(record),
                "expected_account_ids": record["profile"]["upload"]["account_ids"],
                "expected_account_bindings": record["profile"]["upload"][
                    "account_bindings"
                ],
                "expected_upload": expected_upload,
                "expected_cover_id": record["edit_cover_id"],
            }
            if expected_upload.get("prefer_download_cover") is True:
                discovery_kwargs.update(
                    expected_download_asset_id=record["download_asset_id"],
                    expected_upload_cover_id=record["upload_cover_id"],
                )
            snapshot = self._cancel_via_discovery(
                "cancel_uploads_for_workflow",
                "upload_discovery_unavailable",
                record["id"],
                pending_upload["edit_output_id"],
                pending_upload["segment_ordinal"],
                record["upload_job_ids"],
                **discovery_kwargs,
            )
            if snapshot.status == "upload_completed":
                return CancellationSnapshot(
                    "attention", code="upload_partially_completed"
                )
            return snapshot
        if record["upload_job_ids"]:
            return self.adapter.cancel_uploads(
                record["upload_job_ids"],
                expected_targets=self._upload_targets(record),
                expected_account_bindings=record["profile"]["upload"][
                    "account_bindings"
                ],
            )
        if record["edit_plan_id"]:
            if not record["edit_project_id"] or not record["download_asset_id"]:
                return CancellationSnapshot(
                    "attention", code="workflow_domain_data_invalid"
                )
            return self.adapter.cancel_edit(
                record["edit_plan_id"],
                expected_project_id=record["edit_project_id"],
                expected_name=f"Open-Flame workflow {record['id']}",
                expected_source_asset_id=record["download_asset_id"],
            )
        if record["edit_project_id"]:
            if not record["download_asset_id"]:
                return CancellationSnapshot(
                    "attention", code="workflow_domain_data_invalid"
                )
            return self._cancel_via_discovery(
                "cancel_edit_for_workflow",
                "edit_discovery_unavailable",
                record["id"],
                expected_project_id=record["edit_project_id"],
                expected_name=f"Open-Flame workflow {record['id']}",
                expected_source_asset_id=record["download_asset_id"],
                expected_recipe=record["profile"]["edit_recipe"],
            )
        if record["download_asset_id"]:
            return self._cancel_via_discovery(
                "cancel_edit_for_workflow",
                "edit_discovery_unavailable",
                record["id"],
                expected_project_id=None,
                expected_name=f"Open-Flame workflow {record['id']}",
                expected_source_asset_id=record["download_asset_id"],
                expected_recipe=record["profile"]["edit_recipe"],
            )
        if record["batch_id"]:
            return self.adapter.cancel_download(
                record["batch_id"],
                expected_name=record["batch_name"],
                expected_source_url=record["source_url"],
            )
        if not record["download_asset_id"]:
            return self._cancel_via_discovery(
                "cancel_download_for_workflow",
                "download_discovery_unavailable",
                record["id"],
                expected_name=record["batch_name"],
                expected_source_url=record["source_url"],
            )
        return CancellationSnapshot(
            "attention", code="workflow_domain_data_invalid"
        )

    @staticmethod
    def _upload_for_execution(record: Mapping[str, Any]) -> Mapping[str, Any]:
        profile = record.get("profile")
        if not isinstance(profile, Mapping):
            raise WorkflowError("workflow_data_invalid")
        upload = profile.get("upload")
        if not isinstance(upload, Mapping):
            raise WorkflowError("workflow_data_invalid")
        if upload.get("title_mode") != "source":
            if record.get("resolved_upload") is not None:
                raise WorkflowError("workflow_data_invalid")
            return upload
        resolved = record.get("resolved_upload")
        if not isinstance(resolved, Mapping):
            raise WorkflowError("workflow_source_metadata_unavailable")
        return resolved

    def _cancel_via_discovery(
        self,
        method_name: str,
        unavailable_code: str,
        *args: Any,
        **kwargs: Any,
    ) -> CancellationSnapshot:
        method = getattr(self.adapter, method_name, None)
        if not callable(method):
            return CancellationSnapshot("attention", code=unavailable_code)
        return method(*args, **kwargs)

    def _apply_ai_snapshot(
        self,
        record: dict[str, Any],
        snapshot: AiSnapshot,
        *,
        context: Literal["automatic", "explicit", "reconcile"],
    ) -> AiSnapshotDisposition:
        """Apply observed AI facts while the caller retains action authority."""

        classification = classify_ai_snapshot(snapshot)
        if classification.disposition == "invalid":
            code = (
                snapshot.code or "ai_review_required"
                if context == "automatic"
                else "workflow_domain_data_invalid"
            )
            self._attention(record["id"], code, expected=record)
            return "attention"
        if classification.disposition == "attention":
            if (
                record["state"] != "attention_required"
                or record["code"] != classification.code
            ):
                self._attention(
                    record["id"], classification.code, expected=record
                )
            return "attention"
        if classification.disposition == "waiting":
            if (
                context != "automatic"
                or record["state"] != "awaiting_ai_review"
                or record["code"] != classification.code
            ):
                self._transition(
                    record["id"],
                    "awaiting_ai_review",
                    classification.code,
                    expected=record,
                )
            return "waiting"
        if not snapshot.plan_id or snapshot.draft_version is None:
            code = (
                "workflow_domain_data_invalid"
                if context == "reconcile"
                else "workflow_data_invalid"
            )
            self._attention(record["id"], code, expected=record)
            return "attention"
        updated = self._set_refs(
            record["id"],
            expected=record,
            edit_draft_version=snapshot.draft_version,
            edit_plan_id=snapshot.plan_id,
        )
        self._transition(
            record["id"],
            "awaiting_edit_confirmation",
            "",
            expected=updated,
        )
        return "ready"

    def _apply_edit_snapshot(
        self,
        record: dict[str, Any],
        snapshot: EditSnapshot,
        classification: EditSnapshotClassification,
        *,
        context: Literal["observe", "reconcile", "retry"],
    ) -> EditSnapshotDisposition:
        """Apply safe edit facts while the caller retains mutation authority."""

        disposition = classification.disposition
        if disposition == "invalid":
            self._attention(
                record["id"], "workflow_domain_data_invalid", expected=record
            )
            return disposition
        if classification.code in _AI_LEDGER_REVIEW_CODES:
            if (
                record["state"] != "attention_required"
                or record["code"] != classification.code
            ):
                self._attention(record["id"], classification.code, expected=record)
            return "attention"
        if disposition in {"failed", "attention"}:
            if context == "retry" and disposition == "failed":
                return disposition
            if (
                record["state"] != "attention_required"
                or record["code"] != classification.code
            ):
                self._attention(record["id"], classification.code, expected=record)
            return disposition
        if disposition == "ready":
            if not self._record_edit_outputs(record, snapshot):
                return "invalid"
            self._transition(
                record["id"], "preparing_upload", "", expected=record
            )
        return disposition

    def _observe_edit(
        self,
        record: dict[str, Any],
        *,
        context: Literal["observe", "reconcile", "retry"],
    ) -> tuple[EditSnapshotClassification, EditSnapshotDisposition]:
        """Inspect and apply one canonical editing-domain observation."""

        try:
            snapshot = self.adapter.inspect_edit(record["edit_plan_id"])
        except WorkflowError as error:
            if error.code not in _AI_LEDGER_REVIEW_CODES:
                raise
            snapshot = EditSnapshot("attention", code=error.code)
        classification = classify_edit_snapshot(snapshot)
        disposition = self._apply_edit_snapshot(
            record, snapshot, classification, context=context
        )
        return classification, disposition

    def _record_upload_outcome(
        self, record: Mapping[str, Any], snapshot: UploadSnapshot
    ) -> bool:
        """Finish with the exact acknowledgement observed by the upload domain."""

        codes = {
            "submitted": "submission_acknowledged",
            "draft_saved": "platform_draft_saved",
            "mixed": "submission_and_draft_acknowledged",
        }
        code = codes.get(snapshot.outcome or "")
        if code is None:
            self._attention(
                record["id"], "upload_outcome_missing", expected=record
            )
            return False
        self._transition(record["id"], "completed", code, expected=record)
        return True

    def _record_edit_outputs(
        self, record: dict[str, Any], snapshot: object
    ) -> bool:
        try:
            output_ids = _output_ids_from_snapshot(snapshot)
            updated = self._set_refs(
                record["id"],
                expected=record,
                outputs=_unprepared_outputs(output_ids),
                edit_cover_id=getattr(snapshot, "cover_id", None),
            )
        except WorkflowError:
            self._attention(
                record["id"],
                "workflow_domain_data_invalid",
                expected=record,
            )
            return False
        record.clear()
        record.update(updated)
        return True

    @staticmethod
    def _upload_targets(record: Mapping[str, Any]) -> list[dict[str, str]]:
        return [
            {
                "job_id": target["job_id"],
                "source_id": output["upload_source_id"],
                "account_id": target["account_id"],
                "platform": target["platform"],
            }
            for output in record["outputs"]
            for target in output["targets"]
        ]

    @staticmethod
    def _upload_request_keys(record: Mapping[str, Any]) -> list[str]:
        """Return each upload slot's immutable per-segment request key."""

        return [
            workflow_upload_request_key(
                record["id"], output["segment_ordinal"]
            )
            for output in record["outputs"]
            for _target in output["targets"]
        ]

    def _inspect_upload(self, record: Mapping[str, Any]) -> UploadSnapshot:
        expected_targets = self._upload_targets(record)
        if len(expected_targets) != len(record["upload_job_ids"]):
            raise WorkflowError("workflow_data_invalid")
        return self.adapter.inspect_upload(
            record["upload_job_ids"], expected_targets=expected_targets
        )

    def _observe_upload(
        self, record: dict[str, Any]
    ) -> tuple[UploadSnapshot, UploadSnapshotClassification, bool]:
        """Inspect upload facts and checkpoint current retry-leaf identities."""

        original_job_ids = tuple(record["upload_job_ids"])
        snapshot = self._inspect_upload(record)
        self._sync_upload_job_ids(record, snapshot)
        classification = classify_upload_snapshot(snapshot)
        job_ids_changed = (
            snapshot.job_ids is not None
            and snapshot.job_ids != original_job_ids
        )
        return snapshot, classification, job_ids_changed

    def _sync_upload_job_ids(
        self, record: dict[str, Any], snapshot: UploadSnapshot
    ) -> None:
        if snapshot.job_ids is None:
            return
        current = tuple(record["upload_job_ids"])
        if snapshot.job_ids != current:
            if (
                not isinstance(snapshot.job_ids, Sequence)
                or isinstance(snapshot.job_ids, (str, bytes))
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            replacement = tuple(snapshot.job_ids)
            if (
                len(replacement) != len(current)
                or any(
                    not isinstance(item, str)
                    or not _HEX_IDENTIFIER.fullmatch(item)
                    for item in replacement
                )
                or len(set(replacement)) != len(replacement)
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            iterator = iter(replacement)
            outputs: list[dict[str, Any]] = []
            for output in record["outputs"]:
                targets = [
                    {**target, "job_id": next(iterator)}
                    for target in output["targets"]
                ]
                outputs.append({**output, "targets": targets})
            try:
                next(iterator)
            except StopIteration:
                pass
            else:
                raise WorkflowError("workflow_domain_data_invalid")
            updated = self._set_refs(
                record["id"], expected=record, outputs=outputs
            )
            record.clear()
            record.update(updated)

    def _identifier(self, value: str) -> str:
        if not isinstance(value, str) or not _HEX_IDENTIFIER.fullmatch(value):
            raise WorkflowError("workflow_not_found")
        return value

    def _expected(
        self,
        workflow_id: str,
        revision: int,
        expected_profile_sha256: str | None = None,
    ) -> dict[str, Any]:
        workflow_id = self._identifier(workflow_id)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise WorkflowError("invalid_revision")
        record = self.get(workflow_id)
        if record["code"] == _CANCELLATION_REQUESTED:
            raise WorkflowError("workflow_state_conflict")
        if record["revision"] != revision:
            raise WorkflowError("workflow_revision_conflict")
        if expected_profile_sha256 is not None:
            if (
                not isinstance(expected_profile_sha256, str)
                or not _SHA256.fullmatch(expected_profile_sha256)
            ):
                raise WorkflowError("invalid_workflow_profile_confirmation")
            if not hmac.compare_digest(
                record["profile_sha256"], expected_profile_sha256
            ):
                raise WorkflowError("workflow_profile_changed")
        return record

    @staticmethod
    def _mutation_identity(
        workflow_id: str, expected: Mapping[str, Any]
    ) -> tuple[int, str, str]:
        try:
            expected_id = expected["id"]
            revision = expected["revision"]
            state = expected["state"]
            code = expected["code"]
        except (KeyError, TypeError):
            raise WorkflowError("workflow_data_invalid") from None
        if (
            expected_id != workflow_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(state, str)
            or not isinstance(code, str)
        ):
            raise WorkflowError("workflow_data_invalid")
        return revision, state, code

    def _set_refs(
        self,
        workflow_id: str,
        *,
        expected: Mapping[str, Any] | None = None,
        **values: object,
    ) -> dict[str, Any]:
        allowed = {
            "batch_id",
            "download_asset_id",
            "edit_project_id",
            "edit_draft_version",
            "edit_plan_id",
            "edit_cover_id",
            "upload_cover_id",
            "outputs",
            "resolved_upload",
        }
        if not values or set(values) - allowed:
            raise WorkflowError("workflow_data_invalid")
        download_identifier_fields = {
            "batch_id",
            "download_asset_id",
        }
        identifier_fields = {
            "edit_project_id",
            "edit_plan_id",
            "edit_cover_id",
            "upload_cover_id",
        }
        for name in download_identifier_fields & values.keys():
            value = values[name]
            if value is not None and not _download_identifier(value):
                raise WorkflowError("workflow_data_invalid")
        for name in identifier_fields & values.keys():
            value = values[name]
            if value is not None and (
                not isinstance(value, str) or not _HEX_IDENTIFIER.fullmatch(value)
            ):
                raise WorkflowError("workflow_data_invalid")
        if "edit_draft_version" in values:
            version = values["edit_draft_version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise WorkflowError("workflow_data_invalid")
        assignments: list[str] = []
        parameters: list[object] = []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError("workflow_not_found")
            identity = (
                (row["revision"], row["state"], row["code"])
                if expected is None
                else self._mutation_identity(workflow_id, expected)
            )
            if (row["revision"], row["state"], row["code"]) != identity:
                raise WorkflowError("workflow_revision_conflict")
            normalized_profile: dict[str, Any] | None = None
            if "outputs" in values or "resolved_upload" in values:
                try:
                    profile = json.loads(row["profile_json"])
                except (TypeError, ValueError):
                    raise WorkflowError("workflow_data_invalid") from None
                normalized_profile, _, _ = normalize_workflow_profile(profile, bound_accounts=True)
            if "outputs" in values:
                assert normalized_profile is not None
                values["outputs"] = normalize_workflow_outputs(
                    values["outputs"], normalized_profile
                )
            if "resolved_upload" in values:
                assert normalized_profile is not None
                _, encoded_resolved = normalize_resolved_upload(
                    values["resolved_upload"], normalized_profile
                )
                if normalized_profile["upload"].get("title_mode") != "source":
                    raise WorkflowError("workflow_data_invalid")
                values["resolved_upload"] = encoded_resolved
            for name, value in values.items():
                column = {
                    "outputs": "outputs_json",
                    "resolved_upload": "resolved_upload_json",
                }.get(name, name)
                if name == "outputs":
                    value = json.dumps(
                        value,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                assignments.append(f"{column}=?")
                parameters.append(value)
            assignments.extend(("revision=revision+1", "updated_at=?"))
            parameters.extend((_now(), workflow_id, *identity))
            changed = db.execute(
                f"UPDATE workflows SET {','.join(assignments)} "
                "WHERE id=? AND revision=? AND state=? AND code=?",
                parameters,
            ).rowcount
            if changed != 1:
                raise WorkflowError("workflow_revision_conflict")
            try:
                return self._by_id(db, workflow_id)
            except WorkflowError:
                # Private fixture/setup callers may intentionally assemble a
                # valid state across two writes. Production callers always
                # supply ``expected`` and may not observe an invalid midpoint.
                if expected is not None:
                    raise
                return {
                    "id": workflow_id,
                    "revision": identity[0] + 1,
                    "state": identity[1],
                    "code": identity[2],
                }

    def _transition(
        self,
        workflow_id: str,
        state: str,
        code: str,
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError("workflow_not_found")
            identity = (
                (row["revision"], row["state"], row["code"])
                if expected is None
                else self._mutation_identity(workflow_id, expected)
            )
            if (row["revision"], row["state"], row["code"]) != identity:
                raise WorkflowError("workflow_revision_conflict")
            previous = row["state"]
            if previous == state and row["code"] == code:
                return self._public(row)
            finished = now if state in {"completed", "attention_required", "canceled"} else None
            changed = db.execute(
                "UPDATE workflows SET state=?,code=?,revision=revision+1,updated_at=?,"
                "finished_at=? WHERE id=? AND revision=? AND state=? AND code=?",
                (state, code, now, finished, workflow_id, *identity),
            ).rowcount
            if changed != 1:
                raise WorkflowError("workflow_revision_conflict")
            self._event(db, workflow_id, previous, state, code, now)
            return self._by_id(db, workflow_id)

    def _attention(
        self,
        workflow_id: str,
        code: str,
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            workflow_id,
            "attention_required",
            code,
            expected=expected,
        )

    def _event(
        self,
        db: sqlite3.Connection,
        workflow_id: str,
        previous: str | None,
        state: str,
        code: str,
        now: str,
    ) -> None:
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM workflow_events WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO workflow_events(workflow_id,sequence,from_state,to_state,code,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (workflow_id, sequence, previous, state, code, now),
        )

    def _by_id(self, db: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise WorkflowError("workflow_not_found")
        return self._public(row)

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            profile = json.loads(row["profile_json"])
            legacy_job_ids = json.loads(row["upload_job_ids_json"])
            raw_outputs = json.loads(row["outputs_json"])
        except (TypeError, ValueError):
            raise WorkflowError("workflow_data_invalid") from None
        if not isinstance(profile, dict) or legacy_job_ids != []:
            raise WorkflowError("workflow_data_invalid")
        normalized_profile, encoded, profile_digest = normalize_workflow_profile(
            profile, bound_accounts=True
        )
        if (
            encoded != row["profile_json"]
            or profile_digest != row["profile_sha256"]
            or row["auto_confirm_edit"]
            != int(normalized_profile["auto_confirm_edit"])
            or row["auto_confirm_upload"]
            != int(normalized_profile["auto_confirm_upload"])
        ):
            raise WorkflowError("workflow_data_invalid")
        resolved_upload: dict[str, Any] | None = None
        raw_resolved_upload = row["resolved_upload_json"]
        if raw_resolved_upload is not None:
            if row["download_asset_id"] is None:
                raise WorkflowError("workflow_data_invalid")
            if (
                not isinstance(raw_resolved_upload, str)
                or len(raw_resolved_upload.encode("utf-8")) > 128 * 1024
            ):
                raise WorkflowError("workflow_data_invalid")
            try:
                candidate = json.loads(raw_resolved_upload)
            except (TypeError, ValueError):
                raise WorkflowError("workflow_data_invalid") from None
            resolved_upload, encoded_resolved = normalize_resolved_upload(
                candidate,
                normalized_profile,
            )
            if (
                encoded_resolved != raw_resolved_upload
                or normalized_profile["upload"].get("title_mode") != "source"
            ):
                raise WorkflowError("workflow_data_invalid")
        elif (
            normalized_profile["upload"].get("title_mode") == "source"
            and row["download_asset_id"] is not None
            and row["state"] not in {
                "downloading",
                "attention_required",
                "canceled",
            }
        ):
            raise WorkflowError("workflow_data_invalid")
        outputs = normalize_workflow_outputs(raw_outputs, normalized_profile)
        outputs_json = json.dumps(
            outputs,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if (
            outputs_json != row["outputs_json"]
            or row["edit_output_id"] is not None
            or row["upload_source_id"] is not None
        ):
            raise WorkflowError("workflow_data_invalid")
        expected_output_count = len(normalized_profile["edit_recipe"]["segments"]) or 1
        if not workflow_outputs_match_state(
            row["state"], outputs, expected_output_count
        ):
            raise WorkflowError("workflow_data_invalid")
        for field in ("batch_id", "download_asset_id"):
            if row[field] is not None and not _download_identifier(row[field]):
                raise WorkflowError("workflow_data_invalid")
        for field in (
            "edit_project_id",
            "edit_plan_id",
            "edit_cover_id",
            "upload_cover_id",
        ):
            value = row[field]
            if value is not None and (
                not isinstance(value, str) or not _HEX_IDENTIFIER.fullmatch(value)
            ):
                raise WorkflowError("workflow_data_invalid")
        public = dict(row)
        public.pop("request_key")
        public.pop("request_digest")
        public.pop("profile_json")
        public.pop("upload_job_ids_json")
        public.pop("outputs_json")
        public.pop("resolved_upload_json")
        edit_output_ids = [item["edit_output_id"] for item in outputs]
        upload_source_ids = [
            item["upload_source_id"]
            for item in outputs
            if item["upload_source_id"] is not None
        ]
        upload_job_ids = [
            target["job_id"]
            for item in outputs
            for target in item["targets"]
        ]
        public["profile"] = normalized_profile
        public["resolved_upload"] = resolved_upload
        public["outputs"] = outputs
        public["edit_output_ids"] = edit_output_ids
        public["upload_source_ids"] = upload_source_ids
        public["upload_job_ids"] = upload_job_ids
        public["edit_output_id"] = (
            edit_output_ids[0] if len(edit_output_ids) == 1 else None
        )
        public["upload_source_id"] = (
            upload_source_ids[0] if len(upload_source_ids) == 1 else None
        )
        public["edit_output_count"] = len(edit_output_ids)
        public["upload_job_count"] = len(upload_job_ids)
        public["auto_confirm_edit"] = normalized_profile["auto_confirm_edit"]
        public["auto_confirm_upload"] = normalized_profile["auto_confirm_upload"]
        public["schema_version"] = SCHEMA_VERSION
        return public

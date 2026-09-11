"""Transactional service for reviewable, non-destructive edit renders."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, BinaryIO, Iterator, Mapping, Sequence
from uuid import UUID, uuid4

from ..managed_files import (
    ManagedFileChanged,
    ManagedFileSizeExceeded,
    UnsafeManagedPath,
    file_signature,
    hash_open_binary,
    lstat_plain,
    open_matching_binary,
    require_matching_fstat,
)

from .contracts import (
    EditRecipe,
    EditingError,
    MediaProcessor,
    RenderAsset,
    RenderResult,
    recipe_from_mapping,
)
from .ai import TranslationRevision
from .ai_authorization import AiOperationAuthorization
from .ai_render import MAX_CUE_WAV_BYTES, SpeechCheckpointBinding
from .ai_ledger import AiInvocationLedger, AiInvocationLedgerError
from .ai_pipeline import (
    AiPipelineError,
    CanonicalAiRequest,
    CanonicalTimeline,
    MAX_TIMELINE_MILLISECONDS,
    canonical_ai_request,
    canonical_timeline,
    decode_ai_request,
    decode_timeline,
    translation_timeline,
    validate_translation_timeline,
)
from .schema import SCHEMA_VERSION, EditingSchemaError, ensure_editing_schema
from .timeline import TimelineCue, TimelineError, parse_subtitles


MAX_SOURCE_BYTES = 16 * 1024**3
MAX_OUTPUT_BYTES = 8 * 1024**3
EDITING_RESERVE_BYTES = 64 * 1024**2
VERIFIED_MEDIA_CHUNK_BYTES = 1024 * 1024
_VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".webm", ".m4v"})
_ASSET_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".webm", ".m4v", ".png", ".jpg", ".jpeg", ".ass", ".json", ".srt", ".vtt", ".wav", ".m4a", ".aac"})
_ASSET_KINDS = frozenset({"segment", "cover", "caption", "audio", "dubbed_video"})
_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_AI_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_SOURCE_CAPTION_KINDS = {
    "application/x-subrip": "srt",
    "text/vtt": "vtt",
}
_SOURCE_CAPTION_MARKUP = re.compile(r"<[^>\n]{1,512}>|\{\\[^}\n]{1,512}\}")
_SPEECH_CHECKPOINT_OPERATION = "speech_checkpoint_v1"
_SPEECH_CHECKPOINT_RESULT = re.compile(r"^([0-9a-f]{32}):([0-9a-f]{64})$")
_EMPTY_RECIPE = EditRecipe().to_dict()
_AI_CANCELLATION_EVIDENCE_CODES = frozenset({
    "ai_remote_result_unknown",
    "ai_remote_retry_blocked",
    "ai_remote_reconciliation_required",
    "ai_remote_accepted_without_result",
    "ai_remote_abandoned",
})


def default_editing_root(data_root: Path) -> Path:
    """Keep derived media and its database beside the download data root."""

    data_root = Path(data_root)
    return data_root.with_name(data_root.name + "-edits")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise EditingError("invalid_identifier")
    return value


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise EditingError("invalid_idempotency_key")
    return value


def _source_asset_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise EditingError("invalid_source_asset_id")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise EditingError("invalid_source_asset_id") from exc
    if str(parsed) != value:
        raise EditingError("invalid_source_asset_id")
    return value


def _source_artifact_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise EditingError("invalid_source_artifact_id")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise EditingError("invalid_source_artifact_id") from exc
    if str(parsed) != value:
        raise EditingError("invalid_source_artifact_id")
    return value


def _text(value: object, maximum: int, *, required: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(character) < 32 and character not in "\n\t\r" for character in value)):
        raise EditingError("invalid_metadata")
    result = value.strip()
    if required and not result:
        raise EditingError("invalid_metadata")
    return result


def _canonical_recipe(recipe: EditRecipe | Mapping[str, Any]) -> tuple[EditRecipe, str, str]:
    normalized = recipe_from_mapping(recipe)
    encoded = json.dumps(
        normalized.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return normalized, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_ordinary_plan(
    processor: MediaProcessor,
    source: Path,
    output_dir: Path,
    recipe: EditRecipe | Mapping[str, Any],
    *,
    cancel_event: Event | None = None,
    expected_source_size: int | None = None,
    expected_source_sha256: str | None = None,
) -> RenderResult:
    """Render a non-AI plan using the canonical whole-video convention."""

    normalized = recipe_from_mapping(recipe)
    if normalized.segments:
        return processor.render(
            source,
            output_dir,
            normalized,
            cancel_event=cancel_event,
            expected_source_size=expected_source_size,
            expected_source_sha256=expected_source_sha256,
        )
    # AI rendering owns its separate whole-video dubbed output and never enters
    # this ordinary-plan helper. For ordinary plans, zero segments means one
    # deterministic full-length video, with a cover as an optional extra asset.
    return processor.render(
        source,
        output_dir,
        normalized,
        cancel_event=cancel_event,
        expected_source_size=expected_source_size,
        expected_source_sha256=expected_source_sha256,
        full_video_output=True,
    )


def _digest(operation: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plain(path: Path, *, directory: bool = False) -> os.stat_result:
    try:
        return lstat_plain(path, directory=directory)
    except UnsafeManagedPath:
        raise EditingError("unsafe_editing_file") from None
    except OSError as exc:
        raise EditingError("editing_media_unavailable") from exc


def _hash_plain_file(path: Path, *, maximum: int) -> tuple[int, str]:
    before = _plain(path)
    if not 0 < before.st_size <= maximum:
        raise EditingError("editing_media_size_invalid")
    expected = file_signature(before)
    try:
        opened = open_matching_binary(path, expected=expected)
        with opened.handle as handle:
            hashed = hash_open_binary(handle, maximum=maximum)
            require_matching_fstat(handle, expected=expected)
    except (ManagedFileChanged, ManagedFileSizeExceeded):
        raise EditingError("editing_media_changed") from None
    except OSError as exc:
        raise EditingError("editing_media_unavailable") from exc
    after = _plain(path)
    if hashed.size != before.st_size or file_signature(after) != expected:
        raise EditingError("editing_media_changed")
    return hashed.size, hashed.hexdigest


def _open_verified_file(
    path: Path,
    *,
    maximum: int,
    expected_size: int,
    expected_sha256: str,
    error_code: str,
) -> tuple[BinaryIO, os.stat_result, tuple[bytes, ...]]:
    """Verify one managed file and retain that exact open handle for serving."""

    handle: BinaryIO | None = None
    try:
        before = _plain(path)
        if not 0 < before.st_size <= maximum or before.st_size != expected_size:
            raise EditingError(error_code)
        expected = file_signature(before)
        opened = open_matching_binary(path, expected=expected)
        handle = opened.handle
        hashed = hash_open_binary(
            handle,
            maximum=expected_size,
            chunk_size=VERIFIED_MEDIA_CHUNK_BYTES,
            collect_chunk_digests=True,
        )
        require_matching_fstat(handle, expected=expected)
        after = _plain(path)
        if (
            hashed.size != expected_size
            or hashed.hexdigest != expected_sha256
            or file_signature(after) != expected
        ):
            raise EditingError(error_code)
        handle.seek(0)
        return handle, opened.info, hashed.chunk_digests
    except EditingError:
        if handle is not None:
            handle.close()
        raise
    except (ManagedFileChanged, ManagedFileSizeExceeded):
        if handle is not None:
            handle.close()
        raise EditingError(error_code) from None
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise EditingError(error_code) from exc
    except BaseException:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        raise


class EditingService:
    """Owns Schema 4 records and immutable source/output copies.

    The application owns the single render worker. Claims are fenced with a
    random token so a stale worker cannot complete another worker's plan.
    """

    def __init__(
        self,
        root: Path,
        processor: MediaProcessor | None = None,
        *,
        recover_interrupted: bool = True,
    ):
        self.root = Path(root)
        self.processor = processor
        self.database_path = self.root / "editing.sqlite3"
        self.source_root = self.root / "sources"
        self.asset_root = self.root / "assets"
        self.staging_root = self.root / "staging"
        self._prepare_root()
        try:
            ensure_editing_schema(self.database_path)
        except EditingSchemaError as exc:
            raise EditingError("editing_schema_invalid") from exc
        self._ai_invocations = AiInvocationLedger(self.database_path)
        if recover_interrupted:
            self.recover_interrupted()

    def _prepare_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _plain(self.root, directory=True)
            for path in (self.source_root, self.asset_root, self.staging_root):
                path.mkdir(exist_ok=True)
                _plain(path, directory=True)
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.database_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except (UnboundLocalError, sqlite3.Error):
                pass
            raise EditingError("editing_database_unavailable") from exc
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass

    def recover_interrupted(self, *, cleanup_orphans: bool = False) -> None:
        """Fail local work safely and revoke queued confirmation after restart."""

        try:
            self._ai_invocations.recover()
        except AiInvocationLedgerError as exc:
            raise EditingError(exc.code) from exc
        interrupted: list[tuple[str, str]] = []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            interrupted = [
                (row["id"], row["claim_token"])
                for row in db.execute(
                    "SELECT id,claim_token FROM render_plans "
                    "WHERE state IN ('running','canceling')"
                )
                if isinstance(row["id"], str)
                and isinstance(row["claim_token"], str)
                and _ID.fullmatch(row["id"])
                and _ID.fullmatch(row["claim_token"])
            ]
            db.execute(
                "UPDATE render_plans SET state='failed',code=CASE WHEN EXISTS("
                "SELECT 1 FROM ai_invocations i WHERE i.render_plan_id=render_plans.id "
                "AND i.state='unknown') THEN 'ai_remote_result_unknown' "
                "ELSE 'render_interrupted' END,"
                "claim_token=NULL,updated_at=?,finished_at=? "
                "WHERE state IN ('running','canceling')",
                (now, now),
            )
            db.execute(
                "UPDATE render_plans SET state='review',code='restart_confirmation_required',"
                "updated_at=?,confirmed_at=NULL WHERE state='queued'",
                (now,),
            )
            db.execute(
                "UPDATE ai_tasks SET state='failed',code=CASE WHEN EXISTS("
                "SELECT 1 FROM ai_invocations i WHERE i.ai_task_id=ai_tasks.id "
                "AND i.state='unknown') THEN 'ai_remote_result_unknown' "
                "ELSE 'ai_task_interrupted' END,"
                "claim_token=NULL,updated_at=?,finished_at=? "
                "WHERE state IN ('running','canceling')",
                (now, now),
            )
            db.execute(
                "UPDATE ai_tasks SET state='review',code='restart_confirmation_required',"
                "updated_at=?,confirmed_at=NULL WHERE state='queued'",
                (now,),
            )
            for table, owner_column in (
                ("render_plans", "render_plan_id"),
                ("ai_tasks", "ai_task_id"),
            ):
                db.execute(
                    f"UPDATE {table} SET state='failed',"
                    "code='ai_remote_result_unknown',updated_at=? "
                    "WHERE state IN ('failed','canceled') AND EXISTS("
                    "SELECT 1 FROM ai_invocations i "
                    f"WHERE i.{owner_column}={table}.id AND i.state='unknown')",
                    (now,),
                )
                resolved = db.execute(
                    f"SELECT id FROM {table} WHERE state IN ('failed','canceled') "
                    "AND code='ai_remote_result_unknown' AND NOT EXISTS("
                    "SELECT 1 FROM ai_invocations i "
                    f"WHERE i.{owner_column}={table}.id AND i.state='unknown')"
                ).fetchall()
                for owner in resolved:
                    code = self._reconciled_ai_invocation_code(
                        db, owner_column, owner["id"]
                    )
                    if code is not None:
                        db.execute(
                            f"UPDATE {table} SET code=?,updated_at=? WHERE id=?",
                            (code, now, owner["id"]),
                        )
        for plan_id, claim_token in interrupted:
            self._remove_output_dir(self.staging_root / plan_id / claim_token)
        if cleanup_orphans:
            self._cleanup_orphan_media()

    def status(self) -> dict[str, Any]:
        with self._db() as db:
            counts = {
                row["state"]: row["amount"]
                for row in db.execute(
                    "SELECT state,COUNT(*) amount FROM render_plans GROUP BY state"
                )
            }
            project_count = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            ai_counts = {
                row["state"]: row["amount"]
                for row in db.execute(
                    "SELECT state,COUNT(*) amount FROM ai_tasks GROUP BY state"
                )
            }
            timeline_counts = {
                row["state"]: row["amount"]
                for row in db.execute(
                    "SELECT state,COUNT(*) amount FROM timeline_revisions GROUP BY state"
                )
            }
            invocation_counts = {
                row["state"]: row["amount"]
                for row in db.execute(
                    "SELECT state,COUNT(*) amount FROM ai_invocations GROUP BY state"
                )
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "processor_configured": self.processor is not None,
            "project_count": project_count,
            "plan_counts": counts,
            "ai_task_counts": ai_counts,
            "timeline_counts": timeline_counts,
            "ai_invocation_counts": invocation_counts,
        }

    @staticmethod
    def _assert_no_unresolved_ai_invocations(
        db: sqlite3.Connection, owner_column: str, owner_id: str
    ) -> None:
        if owner_column not in {"ai_task_id", "render_plan_id"}:
            raise EditingError("editing_data_invalid")
        pending = db.execute(
            f"SELECT 1 FROM ai_invocations WHERE {owner_column}=? "
            "AND state IN ('reserved','dispatched','unknown') LIMIT 1",
            (owner_id,),
        ).fetchone()
        if pending is not None:
            raise EditingError("ai_remote_reconciliation_required")

    @staticmethod
    def _reconciled_ai_invocation_code(
        db: sqlite3.Connection, owner_column: str, owner_id: str
    ) -> str | None:
        """Summarize all fixed reconciliation outcomes conservatively."""

        if owner_column not in {"ai_task_id", "render_plan_id"}:
            raise EditingError("editing_data_invalid")
        resolutions = {
            row["resolution"]
            for row in db.execute(
                f"SELECT resolution FROM ai_invocations WHERE {owner_column}=? "
                "AND state='reconciled'",
                (owner_id,),
            )
        }
        if "accepted_without_result" in resolutions:
            return "ai_remote_accepted_without_result"
        if "abandoned" in resolutions:
            return "ai_remote_abandoned"
        if "not_accepted" in resolutions:
            return "ai_remote_not_accepted"
        return None

    @staticmethod
    def _assert_ai_invocation_retry_allowed(
        db: sqlite3.Connection, owner_column: str, owner_id: str
    ) -> None:
        owner_table = {
            "ai_task_id": "ai_tasks",
            "render_plan_id": "render_plans",
        }.get(owner_column)
        if owner_table is None:
            raise EditingError("editing_data_invalid")
        blocked = db.execute(
            "WITH RECURSIVE retry_ancestry(id,retry_of) AS ("
            f"SELECT id,retry_of FROM {owner_table} WHERE id=? UNION "
            f"SELECT parent.id,parent.retry_of FROM {owner_table} parent "
            "JOIN retry_ancestry child ON parent.id=child.retry_of) "
            "SELECT 1 FROM ai_invocations invocation "
            f"JOIN retry_ancestry owner ON invocation.{owner_column}=owner.id "
            "WHERE invocation.state IN ('reserved','dispatched','unknown') OR "
            "(invocation.state='reconciled' AND invocation.resolution IN "
            "('accepted_without_result','abandoned')) LIMIT 1",
            (owner_id,),
        ).fetchone()
        if blocked is not None:
            raise EditingError("ai_remote_retry_blocked")

    @staticmethod
    def _assert_ai_invocation_cancellation_safe(
        db: sqlite3.Connection,
        owner_column: str,
        owner_id: str,
        *,
        owner_state: str,
        owner_code: str,
    ) -> None:
        """Preserve durable remote evidence while allowing an in-flight stop."""

        owner_table = {
            "ai_task_id": "ai_tasks",
            "render_plan_id": "render_plans",
        }.get(owner_column)
        if owner_table is None:
            raise EditingError("editing_data_invalid")
        if owner_code in _AI_CANCELLATION_EVIDENCE_CODES:
            raise EditingError(owner_code)
        blocked = db.execute(
            "WITH RECURSIVE retry_ancestry(id,retry_of) AS ("
            f"SELECT id,retry_of FROM {owner_table} WHERE id=? UNION "
            f"SELECT parent.id,parent.retry_of FROM {owner_table} parent "
            "JOIN retry_ancestry child ON parent.id=child.retry_of) "
            "SELECT 1 FROM ai_invocations invocation "
            f"JOIN retry_ancestry owner ON invocation.{owner_column}=owner.id "
            "WHERE invocation.state='unknown' OR "
            "(invocation.state='reconciled' AND invocation.resolution IN "
            "('accepted_without_result','abandoned')) OR "
            "(invocation.state IN ('reserved','dispatched') AND NOT "
            "(owner.id=? AND ? IN ('running','canceling'))) LIMIT 1",
            (owner_id, owner_id, owner_state),
        ).fetchone()
        if blocked is not None:
            raise EditingError("ai_remote_retry_blocked")

    @staticmethod
    def _finalize_ai_invocations_on_owner_failure(
        db: sqlite3.Connection,
        owner_column: str,
        owner_id: str,
        now: str,
    ) -> None:
        """Close any bridge transition left incomplete before failing its owner."""

        if owner_column not in {"ai_task_id", "render_plan_id"}:
            raise EditingError("editing_data_invalid")
        db.execute(
            "UPDATE ai_invocations SET state='released',"
            "reason_code='owner_failed_before_dispatch',revision=revision+1,"
            "updated_at=?,released_at=? "
            f"WHERE {owner_column}=? AND state='reserved'",
            (now, now, owner_id),
        )
        db.execute(
            "UPDATE ai_invocations SET state='unknown',"
            "reason_code='owner_failed_after_dispatch',revision=revision+1,"
            "updated_at=?,unknown_at=? "
            f"WHERE {owner_column}=? AND state='dispatched'",
            (now, now, owner_id),
        )

    def ai_invocations(
        self,
        project_id: str | None = None,
        *,
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return one redacted page plus full project safety totals."""

        normalized_project_id = (
            None if project_id is None else _identifier(project_id)
        )
        try:
            items = self._ai_invocations.list(
                project_id=normalized_project_id,
                limit=limit,
                offset=offset,
            )
        except AiInvocationLedgerError as exc:
            raise EditingError(exc.code) from exc
        counts = {
            state: 0
            for state in (
                "reserved",
                "dispatched",
                "responded",
                "released",
                "unknown",
                "reconciled",
            )
        }
        predicate = ""
        values: tuple[object, ...] = ()
        if normalized_project_id is not None:
            predicate = (
                " WHERE (ai_task_id IN (SELECT id FROM ai_tasks WHERE project_id=?) "
                "OR render_plan_id IN (SELECT id FROM render_plans WHERE project_id=?))"
            )
            values = (normalized_project_id, normalized_project_id)
        retry_blocking = (
            "(state IN ('reserved','dispatched','unknown') OR "
            "(state='reconciled' AND resolution IN "
            "('accepted_without_result','abandoned')))"
        )

        def blocked_owner_query(owner_column: str, owner_table: str) -> str:
            separator = " AND " if predicate else " WHERE "
            return (
                "WITH RECURSIVE blocked_owner(id) AS ("
                f"SELECT DISTINCT {owner_column} FROM ai_invocations"
                + predicate
                + separator
                + f"{owner_column} IS NOT NULL AND {retry_blocking} UNION "
                f"SELECT child.id FROM {owner_table} child "
                "JOIN blocked_owner parent ON child.retry_of=parent.id) "
                "SELECT id FROM blocked_owner ORDER BY id"
            )

        with self._db() as db:
            grouped = db.execute(
                "SELECT state,COUNT(*) amount,COALESCE(SUM(request_units),0) units "
                "FROM ai_invocations"
                + predicate
                + " GROUP BY state",
                values,
            ).fetchall()
            blocked_task_ids = [
                row["id"]
                for row in db.execute(
                    blocked_owner_query("ai_task_id", "ai_tasks"), values
                )
            ]
            blocked_plan_ids = [
                row["id"]
                for row in db.execute(
                    blocked_owner_query("render_plan_id", "render_plans"), values
                )
            ]
        request_units = 0
        for row in grouped:
            if row["state"] in counts:
                counts[row["state"]] = row["amount"]
                request_units += row["units"]
        total = sum(counts.values())
        return {
            "items": items,
            "summary": {
                "counts": counts,
                "total": total,
                "request_units": request_units,
                "unresolved": counts["reserved"]
                + counts["dispatched"]
                + counts["unknown"],
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(items) < total,
                "retry_blocked_ai_task_ids": blocked_task_ids,
                "retry_blocked_render_plan_ids": blocked_plan_ids,
            },
        }

    def reconcile_ai_invocation(
        self,
        invocation_id: str,
        expected_revision: int,
        resolution: str,
        *,
        acknowledge: bool,
    ) -> dict[str, Any]:
        """Apply one fixed, revision-fenced decision to an unknown result."""

        if acknowledge is not True:
            raise EditingError("ai_remote_reconciliation_acknowledgement_required")
        try:
            record = self._ai_invocations.reconcile(
                invocation_id,
                expected_revision,
                resolution=resolution,
            )
        except AiInvocationLedgerError as exc:
            raise EditingError(exc.code) from exc
        owner_column = (
            "ai_task_id" if record.get("ai_task_id") is not None else "render_plan_id"
        )
        owner_id = record.get(owner_column)
        if not isinstance(owner_id, str) or not _ID.fullmatch(owner_id):
            raise EditingError("editing_data_invalid")
        table = "ai_tasks" if owner_column == "ai_task_id" else "render_plans"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            remaining = db.execute(
                f"SELECT 1 FROM ai_invocations WHERE {owner_column}=? "
                "AND state='unknown' LIMIT 1",
                (owner_id,),
            ).fetchone()
            if remaining is None:
                resolved_code = self._reconciled_ai_invocation_code(
                    db, owner_column, owner_id
                )
                if resolved_code is None:
                    raise EditingError("editing_data_invalid")
                db.execute(
                    f"UPDATE {table} SET code=?,updated_at=? WHERE id=? "
                    "AND state IN ('failed','canceled') "
                    "AND code='ai_remote_result_unknown'",
                    (resolved_code, _now(), owner_id),
                )
        return record

    def import_source(
        self,
        path: Path,
        name: str,
        expected_sha256: str,
        source_asset_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        path = Path(path)
        name = self._media_name(name, _VIDEO_SUFFIXES)
        suffix = path.suffix.lower()
        if suffix not in _VIDEO_SUFFIXES or Path(name).suffix.lower() != suffix:
            raise EditingError("invalid_source_type")
        if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
            raise EditingError("invalid_source_hash")
        if source_asset_id is not None:
            source_asset_id = _source_asset_identifier(source_asset_id)
        key = _request_id(idempotency_key) if idempotency_key is not None else None
        request_digest = _digest("import_source", {
            "name": name,
            "sha256": expected_sha256,
            "source_asset_id": source_asset_id,
        })
        if key is not None:
            existing = self._request_result(key, "import_source", request_digest)
            if existing is not None:
                result = self.source(existing)
                self.source_path(existing)
                return result
        if source_asset_id is not None:
            with self._db() as db:
                existing_row = db.execute(
                    "SELECT * FROM sources WHERE source_asset_id=?", (source_asset_id,)
                ).fetchone()
            if existing_row is not None:
                public = self._source_public(existing_row)
                if public["sha256"] != expected_sha256 or public["name"] != name:
                    raise EditingError("source_asset_conflict")
                self._verified_source_row(existing_row)
                if key is not None:
                    self._record_request(key, "import_source", request_digest, public["id"])
                return public

        source_id = uuid4().hex
        destination = self.source_root / f"{source_id}{suffix}"
        size, actual = self._copy_and_hash(path, destination, MAX_SOURCE_BYTES)
        if actual != expected_sha256:
            self._discard_unregistered_file(destination)
            raise EditingError("source_hash_mismatch")
        now = _now()
        converged_source_id: str | None = None
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if key is not None:
                    replay = self._request_result_in(db, key, "import_source", request_digest)
                    if replay is not None:
                        result = self._source_by_id(db, replay)
                        converged_source_id = replay
                if converged_source_id is None and source_asset_id is not None:
                    existing_row = db.execute(
                        "SELECT * FROM sources WHERE source_asset_id=?",
                        (source_asset_id,),
                    ).fetchone()
                    if existing_row is not None:
                        public = self._source_public(existing_row)
                        if (
                            public["sha256"] != expected_sha256
                            or public["name"] != name
                        ):
                            raise EditingError("source_asset_conflict")
                        if key is not None:
                            self._insert_request(
                                db,
                                key,
                                "import_source",
                                request_digest,
                                public["id"],
                                now,
                            )
                        result = public
                        converged_source_id = public["id"]
                if converged_source_id is None:
                    db.execute(
                        "INSERT INTO sources(id,source_asset_id,name,suffix,size,sha256,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (source_id, source_asset_id, name, suffix, size, actual, now),
                    )
                    if key is not None:
                        self._insert_request(
                            db, key, "import_source", request_digest, source_id, now
                        )
                    result = self._source_by_id(db, source_id)
        except Exception:
            self._discard_unregistered_file(destination)
            raise
        if converged_source_id is not None:
            self._discard_unregistered_file(destination)
            # Hash the durable source only after releasing the write transaction.
            # This branch can process a 16 GiB source and must not block all
            # editing database readers while it verifies the winning copy.
            self.source_path(converged_source_id)
        return result

    def sources(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM sources ORDER BY created_at,id").fetchall()
        return [self._source_public(row) for row in rows]

    def source(self, source_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._source_by_id(db, _identifier(source_id))

    def source_path(self, source_id: str) -> Path:
        """Return a verified internal source path for preview/render code."""

        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (_identifier(source_id),)).fetchone()
        if row is None:
            raise EditingError("source_not_found")
        return self._verified_source_row(row)

    def project_source_identity(self, project_id: str) -> tuple[Path, int, str]:
        """Return the verified project source and its immutable database identity."""

        with self._db() as db:
            row = db.execute(
                "SELECT s.* FROM sources s JOIN projects p ON p.source_id=s.id WHERE p.id=?",
                (_identifier(project_id),),
            ).fetchone()
        if row is None:
            raise EditingError("project_not_found")
        return self._verified_source_row(row), row["size"], row["sha256"]

    def project_source_path(self, project_id: str) -> Path:
        return self.project_source_identity(project_id)[0]

    def open_project_source(
        self, project_id: str
    ) -> tuple[BinaryIO, os.stat_result, tuple[bytes, ...], dict[str, Any]]:
        with self._db() as db:
            row = db.execute(
                "SELECT s.* FROM sources s JOIN projects p ON p.source_id=s.id "
                "WHERE p.id=?",
                (_identifier(project_id),),
            ).fetchone()
        if row is None:
            raise EditingError("project_not_found")
        _identifier(row["id"])
        if row["suffix"] not in _VIDEO_SUFFIXES:
            raise EditingError("editing_data_invalid")
        handle, info, chunk_digests = _open_verified_file(
            self.source_root / f"{row['id']}{row['suffix']}",
            maximum=MAX_SOURCE_BYTES,
            expected_size=row["size"],
            expected_sha256=row["sha256"],
            error_code="source_changed",
        )
        return handle, info, chunk_digests, self._source_public(row)

    def create_project(
        self,
        source_id: str,
        name: str,
        idempotency_key: str,
        recipe: EditRecipe | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_id = _identifier(source_id)
        name = _text(name, 160, required=True)
        key = _request_id(idempotency_key)
        ready_translation = False
        if recipe is None:
            recipe_value = _EMPTY_RECIPE
            encoded = json.dumps(recipe_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            recipe_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        else:
            normalized, encoded, recipe_hash = _canonical_recipe(recipe)
            ready_translation = (
                normalized.translation.state == "ready"
                or normalized.translation.revision_id is not None
            )
        request_digest = _digest("create_project", {
            "source_id": source_id, "name": name, "recipe_sha256": recipe_hash
        })
        existing = self._request_result(key, "create_project", request_digest)
        if existing is not None:
            return self.project(existing)
        if ready_translation:
            raise EditingError("ai_timeline_revision_required")
        with self._db() as db:
            source_row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source_row is None:
            raise EditingError("source_not_found")
        self._verified_source_row(source_row)
        project_id, now = uuid4().hex, _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._request_result_in(db, key, "create_project", request_digest)
            if replay is not None:
                return self._project_by_id(db, replay)
            db.execute(
                "INSERT INTO projects(id,source_id,name,current_version,created_at,updated_at) "
                "VALUES(?,?,?,1,?,?)", (project_id, source_id, name, now, now),
            )
            db.execute(
                "INSERT INTO drafts(project_id,version,recipe,recipe_sha256,created_at) "
                "VALUES(?,1,?,?,?)", (project_id, encoded, recipe_hash, now),
            )
            self._insert_request(db, key, "create_project", request_digest, project_id, now)
            return self._project_by_id(db, project_id)

    def update_draft(
        self,
        project_id: str,
        expected_version: int,
        recipe: EditRecipe | Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        project_id = _identifier(project_id)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise EditingError("invalid_version")
        key = _request_id(idempotency_key)
        normalized, encoded, recipe_hash = _canonical_recipe(recipe)
        request_digest = _digest("update_draft", {
            "project_id": project_id,
            "expected_version": expected_version,
            "recipe_sha256": recipe_hash,
        })
        existing = self._request_result(key, "update_draft", request_digest)
        if existing is not None:
            saved_project, saved_version = self._draft_result_id(existing)
            return self.draft(saved_project, saved_version)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._request_result_in(db, key, "update_draft", request_digest)
            if replay is not None:
                saved_project, saved_version = self._draft_result_id(replay)
                return self._draft_by_version(db, saved_project, saved_version)
            project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise EditingError("project_not_found")
            if project["current_version"] != expected_version:
                raise EditingError("draft_version_conflict")
            if normalized.translation.state == "ready":
                revision_id = normalized.translation.revision_id
                if revision_id is None:
                    raise EditingError("ai_timeline_revision_required")
                self._approved_translation_binding(
                    db,
                    project_id=project_id,
                    recipe=normalized,
                    revision_id=revision_id,
                )
            next_version = expected_version + 1
            now = _now()
            db.execute(
                "INSERT INTO drafts(project_id,version,recipe,recipe_sha256,created_at) "
                "VALUES(?,?,?,?,?)", (project_id, next_version, encoded, recipe_hash, now),
            )
            db.execute(
                "UPDATE projects SET current_version=?,updated_at=? WHERE id=? AND current_version=?",
                (next_version, now, project_id, expected_version),
            )
            self._insert_request(
                db, key, "update_draft", request_digest,
                f"{project_id}:{next_version}", now,
            )
            return self._draft_by_version(db, project_id, next_version)

    def draft(self, project_id: str, version: int | None = None) -> dict[str, Any]:
        project_id = _identifier(project_id)
        with self._db() as db:
            if version is None:
                row = db.execute(
                    "SELECT current_version FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if row is None:
                    raise EditingError("project_not_found")
                version = row["current_version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise EditingError("invalid_version")
            return self._draft_by_version(db, project_id, version)

    def projects(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY updated_at DESC,id").fetchall()
            return [self._project_public(db, row) for row in rows]

    def project(self, project_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._project_by_id(db, _identifier(project_id))

    def workflow_artifacts_for_requests(
        self,
        project_request_key: str,
        plan_request_key: str,
        *,
        expected_name: str | None = None,
        expected_source_asset_id: str | None = None,
        expected_recipe: EditRecipe | Mapping[str, Any] | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        """Discover workflow-owned editing artifacts without creating replacements."""

        project_key = _request_id(project_request_key)
        plan_key = _request_id(plan_request_key)
        if project_key == plan_key:
            raise EditingError("editing_request_invalid")
        expected_recipe_value = None
        expected_recipe_sha256 = None
        if expected_recipe is not None:
            expected_recipe_value, _encoded, expected_recipe_sha256 = (
                _canonical_recipe(expected_recipe)
            )
        if expected_name is not None:
            expected_name = _text(expected_name, 160, required=True)
        if expected_source_asset_id is not None:
            expected_source_asset_id = _source_asset_identifier(
                expected_source_asset_id
            )
        with self._db() as db:
            return self._workflow_artifacts_for_requests_in(
                db,
                project_key,
                plan_key,
                expected_name=expected_name,
                expected_source_asset_id=expected_source_asset_id,
                expected_recipe=expected_recipe_value,
                expected_recipe_sha256=expected_recipe_sha256,
            )

    def _workflow_artifacts_for_requests_in(
        self,
        db: sqlite3.Connection,
        project_key: str,
        plan_key: str,
        *,
        expected_name: str | None = None,
        expected_source_asset_id: str | None = None,
        expected_recipe: EditRecipe | None = None,
        expected_recipe_sha256: str | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        project_request = db.execute(
            "SELECT operation,digest,result_id FROM requests WHERE id=?", (project_key,)
        ).fetchone()
        plan_request = db.execute(
            "SELECT operation,digest,result_id FROM requests WHERE id=?", (plan_key,)
        ).fetchone()
        if project_request is None:
            if plan_request is not None:
                raise EditingError("editing_request_invalid")
            return {"project": None, "plan": None}
        if project_request["operation"] == "cancel_workflow_project":
            if (
                expected_name is None
                or expected_source_asset_id is None
                or expected_recipe_sha256 is None
                or not isinstance(project_request["digest"], str)
                or _SHA256.fullmatch(project_request["digest"]) is None
                or not hmac.compare_digest(
                    project_request["digest"],
                    _digest(
                        "cancel_workflow_project",
                        {
                            "name": expected_name,
                            "source_asset_id": expected_source_asset_id,
                            "recipe_sha256": expected_recipe_sha256,
                        },
                    ),
                )
                or plan_request is None
                or plan_request["operation"] != "cancel_workflow_plan"
                or not isinstance(plan_request["digest"], str)
                or _SHA256.fullmatch(plan_request["digest"]) is None
                or not hmac.compare_digest(
                    plan_request["digest"],
                    _digest(
                        "cancel_workflow_plan",
                        {
                            "project_id": None,
                            "project_request_key": project_key,
                        },
                    ),
                )
            ):
                raise EditingError("editing_request_invalid")
            return {"project": None, "plan": None}
        if project_request["operation"] != "create_project":
            raise EditingError("editing_request_invalid")
        try:
            project = self._project_by_id(
                db, _identifier(project_request["result_id"])
            )
        except EditingError as error:
            if error.code in {"invalid_identifier", "project_not_found"}:
                raise EditingError("editing_request_invalid") from None
            raise
        if (
            expected_name is not None
            and project.get("name") != expected_name
            or expected_source_asset_id is not None
            and project.get("source_asset_id") != expected_source_asset_id
        ):
            raise EditingError("edit_project_mismatch")
        if expected_recipe_sha256 is not None:
            expected_project_digest = _digest(
                "create_project",
                {
                    "source_id": project.get("source_id"),
                    "name": project.get("name"),
                    "recipe_sha256": expected_recipe_sha256,
                },
            )
            if (
                not isinstance(project_request["digest"], str)
                or _SHA256.fullmatch(project_request["digest"]) is None
                or not hmac.compare_digest(
                    project_request["digest"], expected_project_digest
                )
            ):
                raise EditingError("editing_request_invalid")
        plan = None
        if plan_request is not None:
            if plan_request["operation"] == "cancel_workflow_plan":
                expected_tombstone_digest = _digest(
                    "cancel_workflow_plan",
                    {
                        "project_id": project.get("id"),
                        "project_request_key": project_key,
                    },
                )
                if (
                    not isinstance(plan_request["digest"], str)
                    or _SHA256.fullmatch(plan_request["digest"]) is None
                    or not hmac.compare_digest(
                        plan_request["digest"], expected_tombstone_digest
                    )
                ):
                    raise EditingError("editing_request_invalid")
                return {"project": project, "plan": None}
            if plan_request["operation"] != "create_plan":
                raise EditingError("editing_request_invalid")
            try:
                plan = self._plan_by_id(
                    db, _identifier(plan_request["result_id"])
                )
            except EditingError as error:
                if error.code in {"invalid_identifier", "plan_not_found"}:
                    raise EditingError("editing_request_invalid") from None
                raise
            if plan.get("project_id") != project.get("id"):
                raise EditingError("editing_request_invalid")
            expected_plan_digest = _digest(
                "create_plan",
                {
                    "project_id": plan.get("project_id"),
                    "expected_version": plan.get("draft_version"),
                    "timeline_revision_id": plan.get("timeline_revision_id"),
                },
            )
            if (
                not isinstance(plan_request["digest"], str)
                or _SHA256.fullmatch(plan_request["digest"]) is None
                or not hmac.compare_digest(plan_request["digest"], expected_plan_digest)
                or expected_recipe is not None
                and not self._workflow_plan_recipe_matches(
                    expected_recipe,
                    plan.get("recipe"),
                    plan.get("timeline_revision_id"),
                )
            ):
                raise EditingError("editing_request_invalid")
        return {"project": project, "plan": plan}

    @staticmethod
    def _workflow_plan_recipe_matches(
        expected: EditRecipe,
        actual: object,
        timeline_revision_id: object,
    ) -> bool:
        """Allow only the deterministic AI-ready evolution of a workflow recipe."""

        try:
            actual_recipe = recipe_from_mapping(actual).to_dict()
        except EditingError:
            return False
        expected_recipe = expected.to_dict()
        if not (expected.translation.enabled or expected.dubbing.enabled):
            return timeline_revision_id is None and actual_recipe == expected_recipe
        translation = actual_recipe.get("translation")
        expected_translation = expected_recipe.get("translation")
        dubbing = actual_recipe.get("dubbing")
        expected_dubbing = expected_recipe.get("dubbing")
        if (
            not isinstance(translation, dict)
            or not isinstance(expected_translation, dict)
            or not isinstance(dubbing, dict)
            or not isinstance(expected_dubbing, dict)
            or not isinstance(timeline_revision_id, str)
            or _ID.fullmatch(timeline_revision_id) is None
            or translation.get("state") != "ready"
            or expected.dubbing.enabled and dubbing.get("state") != "ready"
        ):
            return False
        actual_revision_id = translation.get("revision_id")
        if actual_revision_id is not None and actual_revision_id != timeline_revision_id:
            return False
        if expected_translation.get("revision_id") is None:
            translation.pop("revision_id", None)
        translation["source_language"] = expected_translation["source_language"]
        translation["state"] = expected_translation["state"]
        if expected.dubbing.enabled:
            dubbing["state"] = expected_dubbing["state"]
        return actual_recipe == expected_recipe

    def import_transcription_timeline(
        self,
        project_id: str,
        payload: bytes,
        language: str,
        mime_type: str,
        expected_sha256: str,
        source_asset_id: str,
        source_artifact_id: str,
        idempotency_key: str,
        *,
        clip_start_ms: int | None = None,
        clip_end_ms: int | None = None,
        segment_boundaries_ms: Sequence[int] = (),
    ) -> dict[str, Any]:
        """Import one verified download caption as a reviewable transcription.

        The caller still owns verification that the registered sidecar belongs
        to ``source_artifact_id``. This boundary additionally binds that
        artifact to the download asset used by the editing project, verifies
        its exact payload, and records durable provenance on the immutable
        timeline revision.
        """

        project_id = _identifier(project_id)
        source_asset_id = _source_asset_identifier(source_asset_id)
        source_artifact_id = _source_artifact_identifier(source_artifact_id)
        key = _request_id(idempotency_key)
        if not isinstance(payload, bytes):
            raise EditingError("source_caption_conflict")
        if (
            not isinstance(expected_sha256, str)
            or not _SHA256.fullmatch(expected_sha256)
            or not hmac.compare_digest(
                hashlib.sha256(payload).hexdigest(), expected_sha256
            )
        ):
            raise EditingError("source_caption_conflict")
        if not isinstance(mime_type, str):
            raise EditingError("source_caption_conflict")
        subtitle_kind = _SOURCE_CAPTION_KINDS.get(mime_type)
        if subtitle_kind is None:
            raise EditingError("source_caption_conflict")
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise EditingError("source_caption_conflict")

        for boundary in (clip_start_ms, clip_end_ms):
            if boundary is not None and (
                isinstance(boundary, bool)
                or not isinstance(boundary, int)
                or boundary < 0
                or boundary > MAX_TIMELINE_MILLISECONDS
            ):
                raise EditingError("source_caption_conflict")
        if (
            clip_start_ms is not None
            and clip_end_ms is not None
            and clip_end_ms <= clip_start_ms
        ):
            raise EditingError("source_caption_conflict")
        if (
            not isinstance(segment_boundaries_ms, Sequence)
            or isinstance(segment_boundaries_ms, (str, bytes))
        ):
            raise EditingError("source_caption_conflict")
        boundaries = tuple(segment_boundaries_ms)
        lower = 0 if clip_start_ms is None else clip_start_ms
        if (
            any(
                isinstance(boundary, bool)
                or not isinstance(boundary, int)
                or boundary <= lower
                or boundary > MAX_TIMELINE_MILLISECONDS
                or (clip_end_ms is not None and boundary >= clip_end_ms)
                for boundary in boundaries
            )
            or tuple(sorted(set(boundaries))) != boundaries
        ):
            raise EditingError("source_caption_conflict")

        try:
            parsed = parse_subtitles(payload, subtitle_kind, language=language)
            if any(_SOURCE_CAPTION_MARKUP.search(cue.source_text) for cue in parsed):
                raise EditingError("source_caption_not_usable")
            clipped: list[TimelineCue] = []
            for cue in parsed:
                overlaps = cue.end_ms > lower and (
                    clip_end_ms is None or cue.start_ms < clip_end_ms
                )
                contained = cue.start_ms >= lower and (
                    clip_end_ms is None or cue.end_ms <= clip_end_ms
                )
                if overlaps and not contained:
                    raise EditingError("source_caption_not_usable")
                if any(
                    cue.start_ms < boundary < cue.end_ms
                    for boundary in boundaries
                ):
                    raise EditingError("source_caption_not_usable")
                if contained:
                    clipped.append(
                        TimelineCue(
                            id=cue.id,
                            order=len(clipped),
                            start_ms=cue.start_ms,
                            end_ms=cue.end_ms,
                            source_text=cue.source_text,
                            source_language=cue.source_language,
                            speaker_id=cue.speaker_id,
                        )
                    )
            timeline = canonical_timeline(clipped, language=language)
        except EditingError:
            raise
        except (AiPipelineError, TimelineError, TypeError, ValueError):
            raise EditingError("source_caption_not_usable") from None

        provider = "download"
        model = f"source-caption-v1:{source_artifact_id}:{expected_sha256}"
        if not _AI_TOKEN.fullmatch(provider) or not _AI_TOKEN.fullmatch(model):
            raise EditingError("source_caption_conflict")
        operation = "import_transcription_timeline"
        request_digest = _digest(
            operation,
            {
                "project_id": project_id,
                "source_asset_id": source_asset_id,
                "source_artifact_id": source_artifact_id,
                "mime_type": mime_type,
                "language": language,
                "payload_sha256": expected_sha256,
                "clip_start_ms": clip_start_ms,
                "clip_end_ms": clip_end_ms,
                "segment_boundaries_ms": list(boundaries),
                "cues_sha256": timeline.cues_sha256,
            },
        )

        def matches(record: Mapping[str, Any]) -> bool:
            return (
                record.get("project_id") == project_id
                and record.get("parent_id") is None
                and record.get("kind") == "transcription"
                and record.get("language") == language
                and record.get("provider") == provider
                and record.get("model") == model
                and record.get("cues_sha256") == timeline.cues_sha256
            )

        def require_project_source(db: sqlite3.Connection) -> None:
            source = db.execute(
                "SELECT s.source_asset_id FROM projects p "
                "JOIN sources s ON s.id=p.source_id WHERE p.id=?",
                (project_id,),
            ).fetchone()
            if source is None:
                exists = db.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if exists is None:
                    raise EditingError("project_not_found")
                raise EditingError("editing_data_invalid")
            if source["source_asset_id"] != source_asset_id:
                raise EditingError("source_caption_conflict")

        with self._db() as db:
            require_project_source(db)
        existing = self._request_result(key, operation, request_digest)
        if existing is not None:
            result = self.timeline(existing)
            if not matches(result):
                raise EditingError("editing_data_invalid")
            return result

        revision_id, now = uuid4().hex, _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._request_result_in(db, key, operation, request_digest)
            require_project_source(db)
            if replay is not None:
                result = self._timeline_by_id(db, replay)
                if not matches(result):
                    raise EditingError("editing_data_invalid")
                return result
            db.execute(
                "INSERT INTO timeline_revisions(id,project_id,parent_id,kind,language,"
                "cues,cues_sha256,provider,model,state,review_version,code,created_at,"
                "updated_at) VALUES(?,?,NULL,'transcription',?,?,?,?,?,'review',0,"
                "'source_caption_review_required',?,?)",
                (
                    revision_id,
                    project_id,
                    language,
                    timeline.cues_json,
                    timeline.cues_sha256,
                    provider,
                    model,
                    now,
                    now,
                ),
            )
            self._insert_request(
                db, key, operation, request_digest, revision_id, now
            )
            return self._timeline_by_id(db, revision_id)

    def create_ai_task(
        self,
        project_id: str,
        operation: str,
        provider_id: str,
        model_id: str,
        options: Mapping[str, Any],
        idempotency_key: str,
        *,
        source_revision_id: str | None = None,
        authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one immutable, explicitly reviewable transcription/translation task."""

        try:
            request = canonical_ai_request(
                project_id=project_id,
                operation=operation,
                provider_id=provider_id,
                model_id=model_id,
                source_revision_id=source_revision_id,
                options=options,
                authorization=authorization,
            )
        except (AiPipelineError, TypeError, ValueError) as exc:
            raise EditingError("invalid_ai_request") from exc
        key = _request_id(idempotency_key)
        request_digest = _digest(
            "create_ai_task", {"request_sha256": request.request_sha256}
        )
        existing = self._request_result(key, "create_ai_task", request_digest)
        if existing is not None:
            return self.ai_task(existing)

        with self._db() as db:
            if db.execute(
                "SELECT 1 FROM projects WHERE id=?", (request.project_id,)
            ).fetchone() is None:
                raise EditingError("project_not_found")
            if request.source_revision_id is not None:
                source = db.execute(
                    "SELECT * FROM timeline_revisions WHERE id=?",
                    (request.source_revision_id,),
                ).fetchone()
                self._validate_ai_source_revision(request, source)

        task_id, now = uuid4().hex, _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._request_result_in(
                db, key, "create_ai_task", request_digest
            )
            if replay is not None:
                return self._ai_task_by_id(db, replay)
            if db.execute(
                "SELECT 1 FROM projects WHERE id=?", (request.project_id,)
            ).fetchone() is None:
                raise EditingError("project_not_found")
            if request.source_revision_id is not None:
                source = db.execute(
                    "SELECT * FROM timeline_revisions WHERE id=?",
                    (request.source_revision_id,),
                ).fetchone()
                self._validate_ai_source_revision(request, source)
            db.execute(
                "INSERT INTO ai_tasks(id,project_id,operation,source_revision_id,"
                "target_language,request,request_sha256,provider,model,state,code,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'review',"
                "'explicit_confirmation_required',?,?)",
                (
                    task_id,
                    request.project_id,
                    request.operation,
                    request.source_revision_id,
                    request.target_language,
                    request.request_json,
                    request.request_sha256,
                    request.provider_id,
                    request.model_id,
                    now,
                    now,
                ),
            )
            self._insert_request(
                db, key, "create_ai_task", request_digest, task_id, now
            )
            return self._ai_task_by_id(db, task_id)

    def confirm_ai_task(
        self,
        task_id: str,
        *,
        expected_request_sha256: str | None = None,
    ) -> dict[str, Any]:
        task_id = _identifier(task_id)
        if expected_request_sha256 is not None and (
            not isinstance(expected_request_sha256, str)
            or not _SHA256.fullmatch(expected_request_sha256)
        ):
            raise EditingError("invalid_ai_task_confirmation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise EditingError("ai_task_not_found")
            task, request = self._ai_task_from_row(row)
            if expected_request_sha256 is not None and not hmac.compare_digest(
                request.request_sha256, expected_request_sha256
            ):
                raise EditingError("ai_task_definition_changed")
            if task["state"] in {"running", "canceling", "succeeded"}:
                return task
            if task["state"] in {"review", "queued"}:
                self._assert_ai_invocation_retry_allowed(
                    db, "ai_task_id", task_id
                )
            if task["state"] == "queued":
                return task
            if task["state"] != "review":
                raise EditingError("ai_task_not_reviewable")
            if request.source_revision_id is not None:
                source = db.execute(
                    "SELECT * FROM timeline_revisions WHERE id=?",
                    (request.source_revision_id,),
                ).fetchone()
                self._validate_ai_source_revision(request, source)
            now = _now()
            changed = db.execute(
                "UPDATE ai_tasks SET state='queued',code='',confirmed_at=?,updated_at=? "
                "WHERE id=? AND state='review'",
                (now, now, task_id),
            ).rowcount
            if changed != 1:
                raise EditingError("ai_task_state_conflict")
            return self._ai_task_by_id(db, task_id)

    def retry_ai_task(self, task_id: str, idempotency_key: str) -> dict[str, Any]:
        task_id, key = _identifier(task_id), _request_id(idempotency_key)
        request_digest = _digest("retry_ai_task", {"task_id": task_id})
        existing = self._request_result(key, "retry_ai_task", request_digest)
        if existing is not None:
            with self._db() as db:
                self._assert_ai_invocation_retry_allowed(db, "ai_task_id", existing)
                return self._ai_task_by_id(db, existing)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._request_result_in(db, key, "retry_ai_task", request_digest)
            if replay is not None:
                self._assert_ai_invocation_retry_allowed(db, "ai_task_id", replay)
                return self._ai_task_by_id(db, replay)
            original = db.execute(
                "SELECT * FROM ai_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if original is None:
                raise EditingError("ai_task_not_found")
            original_public, request = self._ai_task_from_row(original)
            if request.authorization is None:
                raise EditingError("ai_authorization_binding_required")
            self._assert_ai_invocation_retry_allowed(db, "ai_task_id", task_id)
            retryable = original_public["state"] in {"failed", "canceled"}
            if original_public["state"] == "succeeded":
                result_revision_id = original_public.get("result_revision_id")
                timeline = (
                    None
                    if result_revision_id is None
                    else db.execute(
                        "SELECT state FROM timeline_revisions WHERE id=?",
                        (result_revision_id,),
                    ).fetchone()
                )
                retryable = timeline is not None and timeline["state"] == "rejected"
            if not retryable:
                raise EditingError("ai_task_retry_not_allowed")
            successor = db.execute(
                "SELECT * FROM ai_tasks WHERE retry_of=?", (task_id,)
            ).fetchone()
            if successor is not None:
                result = self._ai_task_public(successor)
                self._insert_request(
                    db, key, "retry_ai_task", request_digest, result["id"], _now()
                )
                return result
            if request.source_revision_id is not None:
                source = db.execute(
                    "SELECT * FROM timeline_revisions WHERE id=?",
                    (request.source_revision_id,),
                ).fetchone()
                self._validate_ai_source_revision(request, source)
            new_id, now = uuid4().hex, _now()
            db.execute(
                "INSERT INTO ai_tasks(id,project_id,operation,source_revision_id,"
                "target_language,request,request_sha256,provider,model,state,code,"
                "created_at,updated_at,retry_of) VALUES(?,?,?,?,?,?,?,?,?,'review',"
                "'explicit_confirmation_required',?,?,?)",
                (
                    new_id,
                    request.project_id,
                    request.operation,
                    request.source_revision_id,
                    request.target_language,
                    request.request_json,
                    request.request_sha256,
                    request.provider_id,
                    request.model_id,
                    now,
                    now,
                    task_id,
                ),
            )
            self._insert_request(
                db, key, "retry_ai_task", request_digest, new_id, now
            )
            return self._ai_task_by_id(db, new_id)

    def claim_next_ai_task(self) -> dict[str, Any] | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM ai_tasks WHERE state='queued' "
                "ORDER BY confirmed_at,id"
            ).fetchall()
            for row in rows:
                task, request = self._ai_task_from_row(row)
                try:
                    self._assert_ai_invocation_retry_allowed(
                        db, "ai_task_id", task["id"]
                    )
                except EditingError as exc:
                    if exc.code != "ai_remote_retry_blocked":
                        raise
                    now = _now()
                    db.execute(
                        "UPDATE ai_tasks SET state='review',"
                        "code='ai_remote_retry_blocked',confirmed_at=NULL,updated_at=? "
                        "WHERE id=? AND state='queued'",
                        (now, task["id"]),
                    )
                    continue
                if request.source_revision_id is not None:
                    source = db.execute(
                        "SELECT * FROM timeline_revisions WHERE id=?",
                        (request.source_revision_id,),
                    ).fetchone()
                    try:
                        self._validate_ai_source_revision(request, source)
                    except EditingError:
                        now = _now()
                        db.execute(
                            "UPDATE ai_tasks SET state='failed',"
                            "code='source_timeline_unavailable',updated_at=?,finished_at=? "
                            "WHERE id=? AND state='queued'",
                            (now, now, task["id"]),
                        )
                        continue
                token, now = uuid4().hex, _now()
                changed = db.execute(
                    "UPDATE ai_tasks SET state='running',claim_token=?,started_at=?,"
                    "updated_at=? WHERE id=? AND state='queued'",
                    (token, now, now, task["id"]),
                ).rowcount
                if changed != 1:
                    continue
                result = self._ai_task_by_id(db, task["id"])
                result["claim_token"] = token
                return result
            return None

    def _cancel_ai_task_ids_in(
        self, db: sqlite3.Connection, task_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        rows: list[sqlite3.Row] = []
        for task_id in task_ids:
            row = db.execute(
                "SELECT state,code FROM ai_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise EditingError("ai_task_not_found")
            if db.execute(
                "SELECT 1 FROM ai_tasks WHERE retry_of=? LIMIT 1", (task_id,)
            ).fetchone() is not None:
                raise EditingError("ai_task_retry_lineage_changed")
            self._assert_ai_invocation_cancellation_safe(
                db,
                "ai_task_id",
                task_id,
                owner_state=row["state"],
                owner_code=row["code"],
            )
            rows.append(row)
        now = _now()
        for task_id, row in zip(task_ids, rows, strict=True):
            if row["state"] in {"review", "queued"}:
                db.execute(
                    "UPDATE ai_tasks SET state='canceled',code='canceled',"
                    "claim_token=NULL,updated_at=?,finished_at=? WHERE id=?",
                    (now, now, task_id),
                )
            elif row["state"] == "running":
                db.execute(
                    "UPDATE ai_tasks SET state='canceling',"
                    "code='cancellation_requested',updated_at=? WHERE id=?",
                    (now, task_id),
                )
        return [self._ai_task_by_id(db, task_id) for task_id in task_ids]

    def cancel_ai_tasks(self, task_ids: Sequence[str]) -> list[dict[str, Any]]:
        if (
            not isinstance(task_ids, Sequence)
            or isinstance(task_ids, (str, bytes))
            or not task_ids
        ):
            raise EditingError("invalid_ai_task_batch")
        normalized_ids = [_identifier(task_id) for task_id in task_ids]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise EditingError("invalid_ai_task_batch")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._cancel_ai_task_ids_in(db, normalized_ids)

    def cancel_ai_project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        """Atomically validate a project's complete retry graph and stop every leaf."""

        project_id = _identifier(project_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._cancel_ai_project_tasks_in(db, project_id)

    def _cancel_ai_project_tasks_in(
        self, db: sqlite3.Connection, project_id: str
    ) -> list[dict[str, Any]]:
        if db.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone() is None:
            raise EditingError("project_not_found")
        rows = db.execute(
            "SELECT * FROM ai_tasks WHERE project_id=? ORDER BY created_at,id",
            (project_id,),
        ).fetchall()
        records: dict[str, dict[str, Any]] = {}
        successor_by_parent: dict[str, str] = {}
        try:
            for row in rows:
                task = self._ai_task_from_row(row)[0]
                task_id = task["id"]
                if task["project_id"] != project_id or task_id in records:
                    raise EditingError("ai_task_set_invalid")
                records[task_id] = task
            for task_id, task in records.items():
                parent_id = task["retry_of"]
                if parent_id is None:
                    continue
                parent = records.get(parent_id)
                if (
                    parent is None
                    or parent_id == task_id
                    or parent_id in successor_by_parent
                    or any(
                        task[field] != parent[field]
                        for field in (
                            "operation",
                            "source_revision_id",
                            "request_sha256",
                        )
                    )
                ):
                    raise EditingError("ai_task_set_invalid")
                successor_by_parent[parent_id] = task_id
            for task_id in records:
                seen: set[str] = set()
                cursor = task_id
                while cursor in successor_by_parent:
                    if cursor in seen:
                        raise EditingError("ai_task_set_invalid")
                    seen.add(cursor)
                    cursor = successor_by_parent[cursor]
        except EditingError as error:
            if error.code == "ai_task_set_invalid":
                raise
            raise EditingError("ai_task_set_invalid") from error

        leaf_ids = [
            task_id for task_id in records if task_id not in successor_by_parent
        ]
        return self._cancel_ai_task_ids_in(db, leaf_ids)

    def cancel_ai_task(self, task_id: str) -> dict[str, Any]:
        return self.cancel_ai_tasks((task_id,))[0]

    def ai_task_cancellation_requested(self, task_id: str, claim_token: str) -> bool:
        task_id, claim_token = _identifier(task_id), _identifier(claim_token)
        with self._db() as db:
            row = db.execute(
                "SELECT state,claim_token FROM ai_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None or row["claim_token"] != claim_token:
            raise EditingError("stale_ai_claim")
        return row["state"] == "canceling"

    def complete_ai_task(
        self,
        task_id: str,
        claim_token: str,
        result: Sequence[TimelineCue] | TranslationRevision,
        *,
        language: str | None = None,
    ) -> dict[str, Any]:
        task_id, claim_token = _identifier(task_id), _identifier(claim_token)
        with self._db() as db:
            row = db.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise EditingError("ai_task_not_found")
            task, request = self._ai_task_from_row(row)
            if row["claim_token"] != claim_token or task["state"] not in {
                "running",
                "canceling",
            }:
                raise EditingError("stale_ai_claim")
            parent_row = None
            parent = None
            if request.source_revision_id is not None:
                parent_row = db.execute(
                    "SELECT * FROM timeline_revisions WHERE id=?",
                    (request.source_revision_id,),
                ).fetchone()
                self._validate_ai_source_revision(request, parent_row)
                parent = self._timeline_from_row(parent_row)[1]

        if task["state"] == "canceling":
            return self.fail_ai_task(task_id, claim_token, "canceled", canceled=True)
        try:
            if request.operation == "transcribe":
                if isinstance(result, TranslationRevision):
                    raise AiPipelineError("invalid transcription result")
                values = tuple(result)
                result_language = language or (
                    values[0].source_language if values else None
                )
                timeline = canonical_timeline(values, language=result_language)
                requested_language = request.request["options"]["language"]
                if (
                    requested_language is not None
                    and timeline.language.casefold() != requested_language.casefold()
                ):
                    raise AiPipelineError("transcription language mismatch")
                kind, parent_id = "transcription", None
            else:
                if parent is None:
                    raise AiPipelineError("translation source is missing")
                if isinstance(result, TranslationRevision):
                    timeline = translation_timeline(
                        parent,
                        result,
                        provider_id=request.provider_id,
                        model_id=request.model_id,
                        source_language=request.source_language,
                        target_language=request.target_language,
                    )
                else:
                    timeline = canonical_timeline(
                        tuple(result), language=request.target_language
                    )
                    validate_translation_timeline(parent, timeline)
                kind, parent_id = "translation", request.source_revision_id
        except (AiPipelineError, AttributeError, TypeError, ValueError) as exc:
            raise EditingError("invalid_ai_result") from exc

        revision_id, now = uuid4().hex, _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM ai_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if current is None:
                raise EditingError("ai_task_not_found")
            current_public, current_request = self._ai_task_from_row(current)
            if current["claim_token"] != claim_token:
                raise EditingError("stale_ai_claim")
            if current_public["state"] == "canceling":
                db.execute(
                    "UPDATE ai_tasks SET state='canceled',code='canceled',claim_token=NULL,"
                    "updated_at=?,finished_at=? WHERE id=? AND state='canceling' "
                    "AND claim_token=?",
                    (now, now, task_id, claim_token),
                )
                return self._ai_task_by_id(db, task_id)
            if current_public["state"] != "running":
                raise EditingError("stale_ai_claim")
            if current_request.request_sha256 != request.request_sha256:
                raise EditingError("editing_data_invalid")
            self._assert_no_unresolved_ai_invocations(db, "ai_task_id", task_id)
            if parent_id is not None:
                current_parent = db.execute(
                    "SELECT * FROM timeline_revisions WHERE id=?", (parent_id,)
                ).fetchone()
                self._validate_ai_source_revision(current_request, current_parent)
                current_parent_timeline = self._timeline_from_row(current_parent)[1]
                validate_translation_timeline(current_parent_timeline, timeline)
            db.execute(
                "INSERT INTO timeline_revisions(id,project_id,parent_id,kind,language,"
                "cues,cues_sha256,provider,model,state,review_version,code,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,'review',0,"
                "'explicit_review_required',?,?)",
                (
                    revision_id,
                    request.project_id,
                    parent_id,
                    kind,
                    timeline.language,
                    timeline.cues_json,
                    timeline.cues_sha256,
                    request.provider_id,
                    request.model_id,
                    now,
                    now,
                ),
            )
            changed = db.execute(
                "UPDATE ai_tasks SET state='succeeded',code='result_review_required',"
                "claim_token=NULL,result_revision_id=?,updated_at=?,finished_at=? "
                "WHERE id=? AND state='running' AND claim_token=?",
                (revision_id, now, now, task_id, claim_token),
            ).rowcount
            if changed != 1:
                raise EditingError("stale_ai_claim")
            return self._ai_task_by_id(db, task_id)

    def fail_ai_task(
        self,
        task_id: str,
        claim_token: str,
        code: str,
        *,
        canceled: bool = False,
    ) -> dict[str, Any]:
        task_id, claim_token = _identifier(task_id), _identifier(claim_token)
        if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
            raise EditingError("invalid_ai_result")
        state = "canceled" if canceled else "failed"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,claim_token FROM ai_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if (
                row is None
                or row["claim_token"] != claim_token
                or row["state"] not in {"running", "canceling"}
            ):
                raise EditingError("stale_ai_claim")
            now = _now()
            self._finalize_ai_invocations_on_owner_failure(
                db, "ai_task_id", task_id, now
            )
            unknown = db.execute(
                "SELECT 1 FROM ai_invocations WHERE ai_task_id=? "
                "AND state IN ('dispatched','unknown') LIMIT 1",
                (task_id,),
            ).fetchone()
            if unknown is not None:
                state = "failed"
                code = "ai_remote_result_unknown"
            db.execute(
                "UPDATE ai_tasks SET state=?,code=?,claim_token=NULL,updated_at=?,"
                "finished_at=? WHERE id=? AND claim_token=?",
                (state, code, now, now, task_id, claim_token),
            )
            return self._ai_task_by_id(db, task_id)

    def ai_tasks(self, project_id: str | None = None) -> list[dict[str, Any]]:
        query, values = "SELECT * FROM ai_tasks", ()
        if project_id is not None:
            query += " WHERE project_id=?"
            values = (_identifier(project_id),)
        query += " ORDER BY created_at DESC,id"
        with self._db() as db:
            return [self._ai_task_public(row) for row in db.execute(query, values)]

    def ai_task(self, task_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._ai_task_by_id(db, _identifier(task_id))

    def timelines(self, project_id: str | None = None) -> list[dict[str, Any]]:
        query, values = "SELECT * FROM timeline_revisions", ()
        if project_id is not None:
            query += " WHERE project_id=?"
            values = (_identifier(project_id),)
        query += " ORDER BY created_at DESC,id"
        with self._db() as db:
            return [self._timeline_public(row) for row in db.execute(query, values)]

    def timeline(self, revision_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._timeline_by_id(db, _identifier(revision_id))

    def review_timeline(
        self, revision_id: str, decision: str, expected_review_version: int
    ) -> dict[str, Any]:
        revision_id = _identifier(revision_id)
        if decision not in {"approved", "rejected"}:
            raise EditingError("invalid_timeline_decision")
        if (
            isinstance(expected_review_version, bool)
            or not isinstance(expected_review_version, int)
            or expected_review_version < 0
        ):
            raise EditingError("invalid_timeline_review_version")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM timeline_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            if row is None:
                raise EditingError("timeline_not_found")
            public, _timeline = self._timeline_from_row(row)
            if (
                public["state"] != "review"
                or public["review_version"] != expected_review_version
            ):
                raise EditingError("timeline_review_conflict")
            if decision == "approved" and public["parent_id"] is not None:
                parent = db.execute(
                    "SELECT * FROM timeline_revisions WHERE id=?", (public["parent_id"],)
                ).fetchone()
                if parent is None or self._timeline_public(parent)["state"] != "approved":
                    raise EditingError("timeline_parent_not_approved")
            now = _now()
            changed = db.execute(
                "UPDATE timeline_revisions SET state=?,review_version=review_version+1,"
                "code=?,updated_at=?,reviewed_at=? WHERE id=? AND state='review' "
                "AND review_version=?",
                (
                    decision,
                    "timeline_approved" if decision == "approved" else "timeline_rejected",
                    now,
                    now,
                    revision_id,
                    expected_review_version,
                ),
            ).rowcount
            if changed != 1:
                raise EditingError("timeline_review_conflict")
            return self._timeline_by_id(db, revision_id)

    def create_plan(
        self,
        project_id: str,
        expected_version: int,
        idempotency_key: str,
        *,
        timeline_revision_id: str | None = None,
    ) -> dict[str, Any]:
        project_id = _identifier(project_id)
        if timeline_revision_id is not None:
            timeline_revision_id = _identifier(timeline_revision_id)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise EditingError("invalid_version")
        key = _request_id(idempotency_key)
        request_digest = _digest(
            "create_plan",
            {
                "project_id": project_id,
                "expected_version": expected_version,
                "timeline_revision_id": timeline_revision_id,
            },
        )
        existing = self._request_result(key, "create_plan", request_digest)
        if existing is not None:
            return self.plan(existing)
        with self._db() as db:
            project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise EditingError("project_not_found")
            if project["current_version"] != expected_version:
                raise EditingError("draft_version_conflict")
            draft = db.execute(
                "SELECT * FROM drafts WHERE project_id=? AND version=?",
                (project_id, expected_version),
            ).fetchone()
            source_row = db.execute(
                "SELECT s.* FROM sources s JOIN projects p ON p.source_id=s.id WHERE p.id=?",
                (project_id,),
            ).fetchone()
        if draft is None or source_row is None:
            raise EditingError("editing_data_invalid")
        self._draft_public(draft)
        recipe = self._recipe_from_row(draft)
        self._verified_source_row(source_row)
        recipe_revision_id = recipe.translation.revision_id
        if recipe_revision_id is not None:
            if timeline_revision_id is None:
                raise EditingError("ai_timeline_revision_required")
            if recipe_revision_id != timeline_revision_id:
                raise EditingError("ai_timeline_mismatch")
        plan_id, now = uuid4().hex, _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._request_result_in(db, key, "create_plan", request_digest)
            if replay is not None:
                return self._plan_by_id(db, replay)
            current = db.execute(
                "SELECT current_version FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if current is None or current["current_version"] != expected_version:
                raise EditingError("draft_version_conflict")
            binding = self._timeline_binding(
                db,
                project_id=project_id,
                recipe=recipe,
                revision_id=timeline_revision_id,
            )
            db.execute(
                "INSERT INTO render_plans(id,project_id,draft_version,recipe,recipe_sha256,"
                "state,code,created_at,updated_at) VALUES(?,?,?,?,?,'review',"
                "'explicit_confirmation_required',?,?)",
                (plan_id, project_id, expected_version, draft["recipe"], draft["recipe_sha256"], now, now),
            )
            if binding is not None:
                db.execute(
                    "INSERT INTO plan_timeline_bindings("
                    "plan_id,revision_id,cues_sha256,parent_id,parent_cues_sha256,"
                    "source_language,target_language,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        binding["revision_id"],
                        binding["cues_sha256"],
                        binding["parent_id"],
                        binding["parent_cues_sha256"],
                        binding["source_language"],
                        binding["target_language"],
                        now,
                    ),
                )
            self._insert_request(db, key, "create_plan", request_digest, plan_id, now)
            return self._plan_by_id(db, plan_id)

    def confirm_plan(
        self,
        plan_id: str,
        *,
        expected_recipe_sha256: str | None = None,
        ai_data_egress_accepted: bool = False,
    ) -> dict[str, Any]:
        plan_id = _identifier(plan_id)
        if type(ai_data_egress_accepted) is not bool or (
            expected_recipe_sha256 is not None
            and (
                not isinstance(expected_recipe_sha256, str)
                or not _SHA256.fullmatch(expected_recipe_sha256)
            )
        ):
            raise EditingError("invalid_plan_confirmation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM render_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise EditingError("plan_not_found")
            if expected_recipe_sha256 is not None and not hmac.compare_digest(
                row["recipe_sha256"], expected_recipe_sha256
            ):
                raise EditingError("plan_definition_changed")
            if row["state"] in {"running", "canceling", "ready"}:
                return self._plan_public(db, row)
            if row["state"] in {"review", "queued"}:
                self._assert_ai_invocation_retry_allowed(
                    db, "render_plan_id", plan_id
                )
            if row["state"] == "queued":
                return self._plan_public(db, row)
            if row["state"] != "review":
                raise EditingError("plan_not_reviewable")
            recipe = self._recipe_from_row(row)
            if recipe.translation.state == "blocked" or recipe.dubbing.state == "blocked":
                raise EditingError("ai_operation_blocked")
            if (
                recipe.translation.enabled
                and recipe.translation.state != "ready"
            ) or (recipe.dubbing.enabled and recipe.dubbing.state != "ready"):
                raise EditingError("ai_operation_not_ready")
            if recipe.dubbing.enabled and expected_recipe_sha256 is None:
                raise EditingError("ai_data_egress_confirmation_required")
            if (
                recipe.dubbing.enabled
                and recipe.dubbing.authorization is not None
                and recipe.dubbing.authorization.execution == "remote"
                and not ai_data_egress_accepted
            ):
                raise EditingError("ai_data_egress_confirmation_required")
            source_row = db.execute(
                "SELECT s.* FROM sources s JOIN projects p ON p.source_id=s.id "
                "WHERE p.id=?", (row["project_id"],),
            ).fetchone()
        if source_row is None:
            raise EditingError("editing_data_invalid")
        self._verified_source_row(source_row)
        if recipe.translation.enabled or recipe.dubbing.enabled:
            self.approved_timeline_for_plan(plan_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            changed = db.execute(
                "UPDATE render_plans SET state='queued',code='',confirmed_at=?,updated_at=? "
                "WHERE id=? AND state='review'", (now, now, plan_id),
            ).rowcount
            if not changed:
                current = self._plan_by_id(db, plan_id)
                if current["state"] in {"queued", "running", "canceling", "ready"}:
                    return current
                raise EditingError("plan_state_conflict")
            return self._plan_by_id(db, plan_id)

    def retry_plan(self, plan_id: str, idempotency_key: str) -> dict[str, Any]:
        """Create a new review plan from one immutable terminal plan."""

        plan_id, key = _identifier(plan_id), _request_id(idempotency_key)
        request_digest = _digest("retry_plan", {"plan_id": plan_id})
        existing = self._request_result(key, "retry_plan", request_digest)
        if existing is not None:
            with self._db() as db:
                self._assert_ai_invocation_retry_allowed(
                    db, "render_plan_id", existing
                )
                return self._plan_by_id(db, existing)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._request_result_in(db, key, "retry_plan", request_digest)
            if replay is not None:
                self._assert_ai_invocation_retry_allowed(
                    db, "render_plan_id", replay
                )
                return self._plan_by_id(db, replay)
            original = db.execute("SELECT * FROM render_plans WHERE id=?", (plan_id,)).fetchone()
            if original is None:
                raise EditingError("plan_not_found")
            if original["state"] not in {"failed", "canceled"}:
                raise EditingError("plan_retry_not_allowed")
            recipe = self._recipe_from_row(original)
            if recipe.dubbing.enabled and recipe.dubbing.authorization is None:
                raise EditingError("ai_authorization_binding_required")
            self._assert_ai_invocation_retry_allowed(db, "render_plan_id", plan_id)
            successor = db.execute(
                "SELECT id FROM render_plans WHERE retry_of=?", (plan_id,)
            ).fetchone()
            if successor is not None:
                successor_id = _identifier(successor["id"])
                self._insert_request(
                    db, key, "retry_plan", request_digest, successor_id, _now()
                )
                return self._plan_by_id(db, successor_id)
            original_binding = db.execute(
                "SELECT * FROM plan_timeline_bindings WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if (recipe.translation.enabled or recipe.dubbing.enabled) and (
                original_binding is None
            ):
                raise EditingError("ai_timeline_binding_required")
            if not (recipe.translation.enabled or recipe.dubbing.enabled) and (
                original_binding is not None
            ):
                raise EditingError("editing_data_invalid")
            new_id, now = uuid4().hex, _now()
            db.execute(
                "INSERT INTO render_plans(id,project_id,draft_version,recipe,recipe_sha256,"
                "state,code,created_at,updated_at,retry_of) VALUES(?,?,?,?,?,'review',"
                "'explicit_confirmation_required',?,?,?)",
                (new_id, original["project_id"], original["draft_version"], original["recipe"],
                 original["recipe_sha256"], now, now, plan_id),
            )
            if original_binding is not None:
                binding = self._timeline_binding(
                    db,
                    project_id=original["project_id"],
                    recipe=recipe,
                    revision_id=original_binding["revision_id"],
                )
                if binding is None:
                    raise EditingError("editing_data_invalid")
                db.execute(
                    "INSERT INTO plan_timeline_bindings("
                    "plan_id,revision_id,cues_sha256,parent_id,parent_cues_sha256,"
                    "source_language,target_language,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id,
                        binding["revision_id"],
                        binding["cues_sha256"],
                        binding["parent_id"],
                        binding["parent_cues_sha256"],
                        binding["source_language"],
                        binding["target_language"],
                        now,
                    ),
                )
            self._insert_request(db, key, "retry_plan", request_digest, new_id, now)
            return self._plan_by_id(db, new_id)

    def claim_next_plan(self) -> dict[str, Any] | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id FROM render_plans WHERE state='queued' "
                "ORDER BY confirmed_at,id"
            ).fetchall()
            for row in rows:
                try:
                    self._assert_ai_invocation_retry_allowed(
                        db, "render_plan_id", row["id"]
                    )
                except EditingError as exc:
                    if exc.code != "ai_remote_retry_blocked":
                        raise
                    now = _now()
                    db.execute(
                        "UPDATE render_plans SET state='review',"
                        "code='ai_remote_retry_blocked',confirmed_at=NULL,updated_at=? "
                        "WHERE id=? AND state='queued'",
                        (now, row["id"]),
                    )
                    continue
                token, now = uuid4().hex, _now()
                changed = db.execute(
                    "UPDATE render_plans SET state='running',claim_token=?,started_at=?,"
                    "updated_at=? WHERE id=? AND state='queued'",
                    (token, now, now, row["id"]),
                ).rowcount
                if changed != 1:
                    continue
                result = self._plan_by_id(db, row["id"])
                result["claim_token"] = token
                return result
            return None

    def cancel_plan(self, plan_id: str) -> dict[str, Any]:
        plan_id = _identifier(plan_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._cancel_plan_in(db, plan_id)

    def _cancel_plan_in(
        self, db: sqlite3.Connection, plan_id: str
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT state,code FROM render_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise EditingError("plan_not_found")
        if db.execute(
            "SELECT 1 FROM render_plans WHERE retry_of=? LIMIT 1", (plan_id,)
        ).fetchone() is not None:
            raise EditingError("render_retry_lineage_changed")
        self._assert_ai_invocation_cancellation_safe(
            db,
            "render_plan_id",
            plan_id,
            owner_state=row["state"],
            owner_code=row["code"],
        )
        now = _now()
        if row["state"] in {"review", "queued"}:
            db.execute(
                "UPDATE render_plans SET state='canceled',code='canceled',"
                "updated_at=?,finished_at=? WHERE id=?", (now, now, plan_id),
            )
        elif row["state"] == "running":
            db.execute(
                "UPDATE render_plans SET state='canceling',code='cancellation_requested',"
                "updated_at=? WHERE id=?", (now, plan_id),
            )
        return self._plan_by_id(db, plan_id)

    def _latest_plan_for_project_in(
        self,
        db: sqlite3.Connection,
        root_plan_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        rows = db.execute(
            "SELECT * FROM render_plans WHERE project_id=? ORDER BY created_at,id",
            (project_id,),
        ).fetchall()
        records: dict[str, dict[str, Any]] = {}
        successors: dict[str, str] = {}
        try:
            for row in rows:
                plan = self._plan_public(db, row)
                plan_id = _identifier(plan["id"])
                if plan.get("project_id") != project_id or plan_id in records:
                    raise EditingError("edit_plan_set_invalid")
                records[plan_id] = plan
            if root_plan_id not in records:
                raise EditingError("edit_plan_mismatch")
            for plan_id, plan in records.items():
                parent_id = plan.get("retry_of")
                if parent_id is None:
                    continue
                parent_id = _identifier(parent_id)
                parent = records.get(parent_id)
                if (
                    parent is None
                    or parent_id == plan_id
                    or parent_id in successors
                    or any(
                        plan.get(field) != parent.get(field)
                        for field in (
                            "draft_version",
                            "recipe_sha256",
                            "timeline_revision_id",
                        )
                    )
                ):
                    raise EditingError("edit_plan_set_invalid")
                successors[parent_id] = plan_id
            for plan_id in records:
                cursor = plan_id
                seen: set[str] = set()
                while cursor in successors:
                    if cursor in seen:
                        raise EditingError("edit_plan_set_invalid")
                    seen.add(cursor)
                    cursor = successors[cursor]
        except EditingError as error:
            if error.code in {"edit_plan_set_invalid", "edit_plan_mismatch"}:
                raise
            raise EditingError("edit_plan_set_invalid") from error
        leaf_id = root_plan_id
        while leaf_id in successors:
            leaf_id = successors[leaf_id]
        return records[leaf_id]

    def cancel_workflow_request_artifacts(
        self,
        project_request_key: str,
        plan_request_key: str,
        *,
        expected_project_id: str | None,
        expected_name: str,
        expected_source_asset_id: str,
        expected_recipe: EditRecipe | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Discover and stop the furthest editing leaf in one write transaction."""

        project_key = _request_id(project_request_key)
        plan_key = _request_id(plan_request_key)
        if project_key == plan_key:
            raise EditingError("editing_request_invalid")
        if expected_project_id is not None:
            expected_project_id = _identifier(expected_project_id)
        expected_name = _text(expected_name, 160, required=True)
        expected_source_asset_id = _source_asset_identifier(
            expected_source_asset_id
        )
        normalized_recipe, _encoded, recipe_sha256 = _canonical_recipe(
            expected_recipe
        )
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            artifacts = self._workflow_artifacts_for_requests_in(
                db,
                project_key,
                plan_key,
                expected_name=expected_name,
                expected_source_asset_id=expected_source_asset_id,
                expected_recipe=normalized_recipe,
                expected_recipe_sha256=recipe_sha256,
            )
            project = artifacts["project"]
            plan = artifacts["plan"]
            if project is None:
                if plan is not None or expected_project_id is not None:
                    raise EditingError("editing_request_invalid")
                if db.execute(
                    "SELECT 1 FROM requests WHERE id=?", (project_key,)
                ).fetchone() is None:
                    self._insert_request(
                        db,
                        project_key,
                        "cancel_workflow_project",
                        _digest(
                            "cancel_workflow_project",
                            {
                                "name": expected_name,
                                "source_asset_id": expected_source_asset_id,
                                "recipe_sha256": recipe_sha256,
                            },
                        ),
                        expected_source_asset_id,
                        _now(),
                    )
                if db.execute(
                    "SELECT 1 FROM requests WHERE id=?", (plan_key,)
                ).fetchone() is None:
                    self._insert_request(
                        db,
                        plan_key,
                        "cancel_workflow_plan",
                        _digest(
                            "cancel_workflow_plan",
                            {
                                "project_id": None,
                                "project_request_key": project_key,
                            },
                        ),
                        project_key,
                        _now(),
                    )
                return {
                    "kind": "none",
                    "project": None,
                    "plan": None,
                    "ai_tasks": [],
                }
            project_id = _identifier(project["id"])
            if expected_project_id is not None and project_id != expected_project_id:
                raise EditingError("edit_project_mismatch")
            if plan is not None:
                root_plan_id = _identifier(plan["id"])
                leaf = self._latest_plan_for_project_in(
                    db, root_plan_id, project_id
                )
                canceled_plan = self._cancel_plan_in(db, leaf["id"])
                return {
                    "kind": "plan",
                    "project": project,
                    "plan": canceled_plan,
                    "ai_tasks": [],
                }
            canceled_tasks = self._cancel_ai_project_tasks_in(db, project_id)
            if db.execute(
                "SELECT 1 FROM requests WHERE id=?", (plan_key,)
            ).fetchone() is None:
                self._insert_request(
                    db,
                    plan_key,
                    "cancel_workflow_plan",
                    _digest(
                        "cancel_workflow_plan",
                        {
                            "project_id": project_id,
                            "project_request_key": project_key,
                        },
                    ),
                    project_id,
                    _now(),
                )
            return {
                "kind": "ai",
                "project": project,
                "plan": None,
                "ai_tasks": canceled_tasks,
            }

    def plan_cancellation_requested(self, plan_id: str, claim_token: str) -> bool:
        plan_id, claim_token = _identifier(plan_id), _identifier(claim_token)
        with self._db() as db:
            row = db.execute(
                "SELECT state,claim_token FROM render_plans WHERE id=?", (plan_id,)
            ).fetchone()
        if row is None or row["claim_token"] != claim_token:
            raise EditingError("stale_render_claim")
        return row["state"] == "canceling"

    def source_path_for_plan(self, plan_id: str) -> Path:
        return self.source_identity_for_plan(plan_id)[0]

    def _approved_translation_binding(
        self,
        db: sqlite3.Connection,
        *,
        project_id: str,
        recipe: EditRecipe,
        revision_id: str,
    ) -> dict[str, Any]:
        revision_id = _identifier(revision_id)
        if (
            not recipe.translation.enabled
            or recipe.translation.state != "ready"
            or recipe.translation.revision_id is not None
            and recipe.translation.revision_id != revision_id
        ):
            raise EditingError("ai_timeline_mismatch")
        row = db.execute(
            "SELECT * FROM timeline_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise EditingError("ai_timeline_required")
        public, timeline = self._timeline_from_row(row)
        parent_id = public["parent_id"]
        if (
            public["kind"] != "translation"
            or public["state"] != "approved"
            or public["project_id"] != project_id
            or public["language"].casefold()
            != recipe.translation.target_language.casefold()
            or public["provider"] != recipe.translation.provider
            or public["model"] != recipe.translation.model
            or parent_id is None
        ):
            raise EditingError("ai_timeline_mismatch")
        parent_row = db.execute(
            "SELECT * FROM timeline_revisions WHERE id=?", (parent_id,)
        ).fetchone()
        if parent_row is None:
            raise EditingError("editing_data_invalid")
        parent_public, parent = self._timeline_from_row(parent_row)
        if (
            parent_public["kind"] != "transcription"
            or parent_public["state"] != "approved"
            or parent_public["project_id"] != project_id
            or parent_public["parent_id"] is not None
            or (
                recipe.translation.source_language.casefold() != "auto"
                and parent.language.casefold()
                != recipe.translation.source_language.casefold()
            )
            or len(parent.cues) != len(timeline.cues)
            or any(
                (source.id, source.order, source.start_ms, source.end_ms)
                != (translated.id, translated.order, translated.start_ms, translated.end_ms)
                for source, translated in zip(
                    parent.cues, timeline.cues, strict=True
                )
            )
        ):
            raise EditingError("ai_timeline_mismatch")
        return {
            "revision_id": public["id"],
            "cues_sha256": timeline.cues_sha256,
            "parent_id": parent_public["id"],
            "parent_cues_sha256": parent.cues_sha256,
            "source_language": parent.language,
            "target_language": timeline.language,
            "timeline": timeline,
        }

    def _timeline_binding(
        self,
        db: sqlite3.Connection,
        *,
        project_id: str,
        recipe: EditRecipe,
        revision_id: str | None,
    ) -> dict[str, Any] | None:
        ai_enabled = recipe.translation.enabled or recipe.dubbing.enabled
        if not ai_enabled:
            if revision_id is not None:
                raise EditingError("ai_timeline_not_expected")
            return None
        if not recipe.translation.enabled or recipe.translation.state != "ready":
            if revision_id is not None:
                raise EditingError("ai_timeline_not_expected")
            # A blocked or review-state plan can still be inspected, but
            # confirm_plan will refuse to queue it until AI work is ready.
            return None
        if revision_id is None:
            raise EditingError("ai_timeline_revision_required")
        return self._approved_translation_binding(
            db,
            project_id=project_id,
            recipe=recipe,
            revision_id=revision_id,
        )

    def approved_timeline_for_plan(
        self, plan_id: str
    ) -> tuple[TimelineCue, ...] | None:
        """Resolve the exact immutable timeline bound when the plan was created."""

        plan_id = _identifier(plan_id)
        with self._db() as db:
            plan = db.execute(
                "SELECT * FROM render_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise EditingError("plan_not_found")
            recipe = self._recipe_from_row(plan)
            if not recipe.translation.enabled and not recipe.dubbing.enabled:
                unexpected = db.execute(
                    "SELECT 1 FROM plan_timeline_bindings WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()
                if unexpected is not None:
                    raise EditingError("editing_data_invalid")
                return None
            row = db.execute(
                "SELECT * FROM plan_timeline_bindings WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise EditingError("ai_timeline_binding_required")
            binding = self._timeline_binding(
                db,
                project_id=plan["project_id"],
                recipe=recipe,
                revision_id=row["revision_id"],
            )
            if binding is None:
                raise EditingError("editing_data_invalid")
            if (
                row["plan_id"] != plan_id
                or row["cues_sha256"] != binding["cues_sha256"]
                or row["parent_id"] != binding["parent_id"]
                or row["parent_cues_sha256"] != binding["parent_cues_sha256"]
                or row["source_language"] != binding["source_language"]
                or row["target_language"] != binding["target_language"]
            ):
                raise EditingError("editing_data_invalid")
            timeline = binding["timeline"]
            if not isinstance(timeline, CanonicalTimeline):
                raise EditingError("editing_data_invalid")
            return timeline.cues

    def source_identity_for_plan(self, plan_id: str) -> tuple[Path, int, str]:
        """Return a verified path plus the immutable database identity."""

        plan_id = _identifier(plan_id)
        with self._db() as db:
            row = db.execute(
                "SELECT s.* FROM sources s JOIN projects p ON p.source_id=s.id "
                "JOIN render_plans r ON r.project_id=p.id WHERE r.id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise EditingError("plan_not_found")
        return self._verified_source_row(row), row["size"], row["sha256"]

    @staticmethod
    def _speech_checkpoint_request_id(plan_id: str, ordinal: int) -> str:
        return f"__speech_checkpoint_v1__:{plan_id}:{ordinal:06d}"

    def _speech_checkpoint_lineage(
        self, db: sqlite3.Connection, plan_id: str
    ) -> tuple[str, tuple[str, ...], sqlite3.Row]:
        """Return current-to-root plans after checking retry identity."""

        current = db.execute(
            "SELECT * FROM render_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if current is None:
            raise EditingError("plan_not_found")
        expected_plan = tuple(
            current[key]
            for key in (
                "project_id",
                "draft_version",
                "recipe",
                "recipe_sha256",
            )
        )
        expected_binding = db.execute(
            "SELECT revision_id,cues_sha256,parent_id,parent_cues_sha256,"
            "source_language,target_language FROM plan_timeline_bindings "
            "WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if expected_binding is None:
            raise EditingError("ai_timeline_binding_required")
        expected_binding_value = tuple(expected_binding)
        lineage: list[str] = []
        seen: set[str] = set()
        row = current
        child_id: str | None = None
        while True:
            row_id = _identifier(row["id"])
            if row_id in seen or len(lineage) >= 1_000:
                raise EditingError("editing_data_invalid")
            seen.add(row_id)
            lineage.append(row_id)
            if tuple(
                row[key]
                for key in (
                    "project_id",
                    "draft_version",
                    "recipe",
                    "recipe_sha256",
                )
            ) != expected_plan:
                raise EditingError("editing_data_invalid")
            binding = db.execute(
                "SELECT revision_id,cues_sha256,parent_id,parent_cues_sha256,"
                "source_language,target_language FROM plan_timeline_bindings "
                "WHERE plan_id=?",
                (row_id,),
            ).fetchone()
            if binding is None or tuple(binding) != expected_binding_value:
                raise EditingError("editing_data_invalid")
            children = db.execute(
                "SELECT id FROM render_plans WHERE retry_of=? ORDER BY id",
                (row_id,),
            ).fetchall()
            if child_id is None:
                if children:
                    raise EditingError("editing_data_invalid")
            elif (
                row["state"] not in {"failed", "canceled"}
                or len(children) != 1
                or children[0]["id"] != child_id
            ):
                raise EditingError("editing_data_invalid")
            parent_id = row["retry_of"]
            if parent_id is None:
                return row_id, tuple(lineage), current
            parent_id = _identifier(parent_id)
            child_id = row_id
            row = db.execute(
                "SELECT * FROM render_plans WHERE id=?", (parent_id,)
            ).fetchone()
            if row is None:
                raise EditingError("editing_data_invalid")

    def _speech_checkpoint_context(
        self,
        db: sqlite3.Connection,
        plan_id: str,
        claim_token: str,
    ) -> tuple[str, tuple[str, ...], AiOperationAuthorization]:
        root_id, lineage, current = self._speech_checkpoint_lineage(db, plan_id)
        if (
            current["state"] not in {"running", "canceling"}
            or not hmac.compare_digest(
                str(current["claim_token"] or ""), claim_token
            )
        ):
            raise EditingError("stale_render_claim")
        recipe = self._recipe_from_row(current)
        authorization = recipe.dubbing.authorization
        if not recipe.dubbing.enabled or authorization is None:
            raise EditingError("ai_authorization_binding_required")
        return root_id, lineage, authorization

    @staticmethod
    def _speech_checkpoint_values(
        ordinal: int,
        request_key: str,
        invocation_fingerprint: str,
        authorization_sha256: str,
        execution: str,
    ) -> tuple[int, str, str, str, str]:
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 0 <= ordinal <= 999_999
            or not isinstance(request_key, str)
            or _SHA256.fullmatch(request_key) is None
            or not isinstance(invocation_fingerprint, str)
            or _SHA256.fullmatch(invocation_fingerprint) is None
            or not isinstance(authorization_sha256, str)
            or _SHA256.fullmatch(authorization_sha256) is None
            or execution not in {"local", "remote"}
        ):
            raise EditingError("ai_speech_checkpoint_invalid")
        return (
            ordinal,
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        )

    @staticmethod
    def _speech_checkpoint_manifest_digest(
        request_key: str,
        invocation_fingerprint: str,
        authorization_sha256: str,
        execution: str,
    ) -> str:
        payload = "\x00".join(
            (
                _SPEECH_CHECKPOINT_OPERATION,
                request_key,
                invocation_fingerprint,
                authorization_sha256,
                execution,
            )
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _assert_speech_invocation_responded(
        db: sqlite3.Connection,
        *,
        plan_id: str,
        ordinal: int,
        invocation_fingerprint: str,
        authorization_sha256: str,
    ) -> None:
        rows = db.execute(
            "SELECT id FROM ai_invocations WHERE render_plan_id=? "
            "AND operation='synthesize' AND ordinal=? AND request_units=1 "
            "AND request_fingerprint=? AND authorization_sha256=? "
            "AND state='responded' AND legacy=0",
            (
                plan_id,
                ordinal,
                invocation_fingerprint,
                authorization_sha256,
            ),
        ).fetchall()
        if len(rows) != 1:
            raise EditingError("ai_speech_checkpoint_invalid")

    def _lookup_speech_checkpoint(
        self,
        plan_id: str,
        claim_token: str,
        ordinal: int,
        request_key: str,
        invocation_fingerprint: str,
        authorization_sha256: str,
        execution: str,
    ) -> tuple[str, ...]:
        values = self._speech_checkpoint_values(
            ordinal,
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        )
        (
            ordinal,
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        ) = values
        manifest_digest = self._speech_checkpoint_manifest_digest(
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        )
        with self._db() as db:
            _root_id, lineage, authorization = self._speech_checkpoint_context(
                db, plan_id, claim_token
            )
            if (
                authorization.sha256 != authorization_sha256
                or authorization.execution != execution
            ):
                raise EditingError("ai_speech_checkpoint_invalid")
            digests: list[str] = []
            for producer_id in lineage:
                request_id = self._speech_checkpoint_request_id(
                    producer_id, ordinal
                )
                row = db.execute(
                    "SELECT operation,digest,result_id FROM requests WHERE id=?",
                    (request_id,),
                ).fetchone()
                if row is None:
                    continue
                result = _SPEECH_CHECKPOINT_RESULT.fullmatch(
                    str(row["result_id"] or "")
                )
                if (
                    row["operation"] != _SPEECH_CHECKPOINT_OPERATION
                    or result is None
                    or result.group(1) != producer_id
                ):
                    raise EditingError("ai_speech_checkpoint_invalid")
                if row["digest"] != manifest_digest:
                    continue
                if execution == "remote":
                    self._assert_speech_invocation_responded(
                        db,
                        plan_id=producer_id,
                        ordinal=ordinal,
                        invocation_fingerprint=invocation_fingerprint,
                        authorization_sha256=authorization_sha256,
                    )
                digest = result.group(2)
                if digest not in digests:
                    digests.append(digest)
            return tuple(digests)

    def _record_speech_checkpoint(
        self,
        plan_id: str,
        claim_token: str,
        ordinal: int,
        request_key: str,
        invocation_fingerprint: str,
        authorization_sha256: str,
        execution: str,
        audio_sha256: str,
    ) -> None:
        values = self._speech_checkpoint_values(
            ordinal,
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        )
        (
            ordinal,
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        ) = values
        manifest_digest = self._speech_checkpoint_manifest_digest(
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        )
        if (
            not isinstance(audio_sha256, str)
            or _SHA256.fullmatch(audio_sha256) is None
        ):
            raise EditingError("ai_speech_checkpoint_invalid")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            root_id, _lineage, authorization = self._speech_checkpoint_context(
                db, plan_id, claim_token
            )
            if (
                authorization.sha256 != authorization_sha256
                or authorization.execution != execution
            ):
                raise EditingError("ai_speech_checkpoint_invalid")
            if execution == "remote":
                self._assert_speech_invocation_responded(
                    db,
                    plan_id=plan_id,
                    ordinal=ordinal,
                    invocation_fingerprint=invocation_fingerprint,
                    authorization_sha256=authorization_sha256,
                )
            checkpoint_path = (
                self.staging_root
                / root_id
                / "speech-checkpoints"
                / f"cue-{ordinal:06d}-{request_key}-{audio_sha256}.wav"
            )
            try:
                size, digest = _hash_plain_file(
                    checkpoint_path, maximum=MAX_CUE_WAV_BYTES
                )
            except EditingError as exc:
                raise EditingError("ai_speech_checkpoint_invalid") from exc
            if size <= 0 or digest != audio_sha256:
                raise EditingError("ai_speech_checkpoint_invalid")
            request_id = self._speech_checkpoint_request_id(plan_id, ordinal)
            result_id = f"{plan_id}:{audio_sha256}"
            existing = db.execute(
                "SELECT operation,digest,result_id FROM requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if existing is None:
                self._insert_request(
                    db,
                    request_id,
                    _SPEECH_CHECKPOINT_OPERATION,
                    manifest_digest,
                    result_id,
                    _now(),
                )
            elif tuple(existing) != (
                _SPEECH_CHECKPOINT_OPERATION,
                manifest_digest,
                result_id,
            ):
                raise EditingError("ai_speech_checkpoint_conflict")

    def speech_checkpoint_binding(
        self, plan_id: str, claim_token: str
    ) -> SpeechCheckpointBinding:
        """Create a fenced cache binding for one running dubbing plan."""

        plan_id, claim_token = _identifier(plan_id), _identifier(claim_token)
        with self._db() as db:
            root_id, _lineage, _authorization = self._speech_checkpoint_context(
                db, plan_id, claim_token
            )
        root = self.staging_root / root_id
        directory = root / "speech-checkpoints"
        try:
            _plain(self.staging_root, directory=True)
            if root.exists() or root.is_symlink():
                _plain(root, directory=True)
            else:
                root.mkdir()
                _plain(root, directory=True)
            if directory.exists() or directory.is_symlink():
                _plain(directory, directory=True)
            else:
                directory.mkdir()
                _plain(directory, directory=True)
        except (OSError, EditingError) as exc:
            raise EditingError("editing_storage_unavailable") from exc
        return SpeechCheckpointBinding(
            directory=directory,
            lookup=lambda ordinal, key, fingerprint, authorization, execution: (
                self._lookup_speech_checkpoint(
                    plan_id,
                    claim_token,
                    ordinal,
                    key,
                    fingerprint,
                    authorization,
                    execution,
                )
            ),
            record=(
                lambda ordinal, key, fingerprint, authorization, execution, digest: (
                    self._record_speech_checkpoint(
                        plan_id,
                        claim_token,
                        ordinal,
                        key,
                        fingerprint,
                        authorization,
                        execution,
                        digest,
                    )
                )
            ),
        )

    def _discard_speech_checkpoints(self, plan_id: str) -> None:
        try:
            with self._db() as db:
                root_id, _lineage, _current = self._speech_checkpoint_lineage(
                    db, _identifier(plan_id)
                )
        except EditingError:
            return
        self._remove_output_dir(
            self.staging_root / root_id / "speech-checkpoints"
        )

    def output_dir_for_plan(self, plan_id: str, claim_token: str) -> Path:
        plan_id, claim_token = _identifier(plan_id), _identifier(claim_token)
        with self._db() as db:
            row = db.execute(
                "SELECT state,claim_token FROM render_plans WHERE id=?", (plan_id,)
            ).fetchone()
        if row is None:
            raise EditingError("plan_not_found")
        if row["state"] not in {"running", "canceling"} or row["claim_token"] != claim_token:
            raise EditingError("stale_render_claim")
        plan_directory = self.staging_root / plan_id
        directory = plan_directory / claim_token
        try:
            _plain(self.staging_root, directory=True)
            if plan_directory.exists() or plan_directory.is_symlink():
                _plain(plan_directory, directory=True)
            else:
                plan_directory.mkdir()
                _plain(plan_directory, directory=True)
            if directory.exists() or directory.is_symlink():
                _plain(directory, directory=True)
            else:
                directory.mkdir()
            _plain(directory, directory=True)
        except (OSError, EditingError) as exc:
            raise EditingError("editing_storage_unavailable") from exc
        return directory

    def complete_plan(
        self, plan_id: str, claim_token: str, result: RenderResult
    ) -> dict[str, Any]:
        plan_id, claim_token = _identifier(plan_id), _identifier(claim_token)
        if not isinstance(result, RenderResult) or not _SAFE_CODE.fullmatch(result.code):
            raise EditingError("invalid_render_result")
        if result.status in {"failed", "canceled"}:
            return self.fail_plan(plan_id, claim_token, result.code, canceled=result.status == "canceled")
        if result.status != "ready" or not result.assets or len(result.assets) > 201:
            raise EditingError("invalid_render_result")
        with self._db() as db:
            state = db.execute(
                "SELECT state,claim_token FROM render_plans WHERE id=?", (plan_id,)
            ).fetchone()
            self._assert_no_unresolved_ai_invocations(db, "render_plan_id", plan_id)
        if state is None:
            raise EditingError("plan_not_found")
        if state["claim_token"] != claim_token or state["state"] not in {"running", "canceling"}:
            raise EditingError("stale_render_claim")
        if state["state"] == "canceling":
            return self.fail_plan(plan_id, claim_token, "canceled", canceled=True)

        output_dir = self.output_dir_for_plan(plan_id, claim_token)
        records: list[dict[str, Any]] = []
        names: set[str] = set()
        try:
            for asset in result.assets:
                if not isinstance(asset, RenderAsset) or asset.kind not in _ASSET_KINDS:
                    raise EditingError("invalid_render_asset")
                name = self._media_name(asset.name, _ASSET_SUFFIXES)
                suffix = Path(name).suffix.lower()
                source_path = Path(asset.path)
                try:
                    if source_path.parent.resolve(strict=True) != output_dir.resolve(strict=True):
                        raise EditingError("invalid_render_asset")
                except OSError as exc:
                    raise EditingError("invalid_render_asset") from exc
                if source_path.suffix.lower() != suffix or name in names:
                    raise EditingError("invalid_render_asset")
                names.add(name)
                mime_type = _text(asset.mime_type, 120, required=True)
                if "/" not in mime_type or any(character.isspace() for character in mime_type):
                    raise EditingError("invalid_render_asset")
                asset_id = uuid4().hex
                destination = self.asset_root / f"{asset_id}{suffix}"
                size, digest = self._copy_and_hash(source_path, destination, MAX_OUTPUT_BYTES)
                if asset.size_bytes not in {0, size}:
                    self._discard_unregistered_file(destination)
                    raise EditingError("render_asset_size_mismatch")
                if asset.sha256 and asset.sha256 != digest:
                    self._discard_unregistered_file(destination)
                    raise EditingError("render_asset_hash_mismatch")
                ordinal = self._optional_integer(asset.ordinal, minimum=0, required=True)
                duration_ms = self._optional_integer(asset.duration_ms, minimum=0)
                width = self._optional_integer(asset.width, minimum=1)
                height = self._optional_integer(asset.height, minimum=1)
                container = _text(asset.container, 40)
                video_codec = None if asset.video_codec is None else _text(asset.video_codec, 80)
                audio_codec = None if asset.audio_codec is None else _text(asset.audio_codec, 80)
                records.append({
                    "id": asset_id, "plan_id": plan_id, "kind": asset.kind,
                    "name": name, "suffix": suffix, "mime_type": mime_type,
                    "size": size, "sha256": digest, "ordinal": ordinal,
                    "duration_ms": duration_ms, "width": width, "height": height,
                    "container": container, "video_codec": video_codec,
                    "audio_codec": audio_codec, "path": destination,
                })
            now = _now()
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT state,claim_token FROM render_plans WHERE id=?", (plan_id,)
                ).fetchone()
                if (current is None or current["claim_token"] != claim_token
                        or current["state"] != "running"):
                    raise EditingError("stale_render_claim")
                self._assert_no_unresolved_ai_invocations(
                    db, "render_plan_id", plan_id
                )
                db.executemany(
                    "INSERT INTO assets(id,plan_id,kind,name,suffix,mime_type,size,sha256,"
                    "ordinal,duration_ms,width,height,container,video_codec,audio_codec,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(row["id"], row["plan_id"], row["kind"], row["name"], row["suffix"],
                      row["mime_type"], row["size"], row["sha256"], row["ordinal"],
                      row["duration_ms"], row["width"], row["height"], row["container"],
                      row["video_codec"], row["audio_codec"], now) for row in records],
                )
                db.execute(
                    "UPDATE render_plans SET state='ready',code=?,claim_token=NULL,"
                    "updated_at=?,finished_at=? WHERE id=? AND state='running' AND claim_token=?",
                    (result.code, now, now, plan_id, claim_token),
                )
                completed = self._plan_by_id(db, plan_id)
        except Exception:
            for row in records:
                self._discard_unregistered_file(row["path"])
            raise
        self._remove_output_dir(output_dir)
        self._discard_speech_checkpoints(plan_id)
        return completed

    def fail_plan(
        self,
        plan_id: str,
        claim_token: str,
        code: str,
        *,
        canceled: bool = False,
    ) -> dict[str, Any]:
        plan_id, claim_token = _identifier(plan_id), _identifier(claim_token)
        if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
            raise EditingError("invalid_render_result")
        final_state = "canceled" if canceled else "failed"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,claim_token FROM render_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if row is None:
                raise EditingError("plan_not_found")
            if row["claim_token"] != claim_token or row["state"] not in {"running", "canceling"}:
                raise EditingError("stale_render_claim")
            now = _now()
            self._finalize_ai_invocations_on_owner_failure(
                db, "render_plan_id", plan_id, now
            )
            unknown = db.execute(
                "SELECT 1 FROM ai_invocations WHERE render_plan_id=? "
                "AND state IN ('dispatched','unknown') LIMIT 1",
                (plan_id,),
            ).fetchone()
            if unknown is not None:
                final_state = "failed"
                code = "ai_remote_result_unknown"
            db.execute(
                "UPDATE render_plans SET state=?,code=?,claim_token=NULL,updated_at=?,finished_at=? "
                "WHERE id=? AND claim_token=?", (final_state, code, now, now, plan_id, claim_token),
            )
            result = self._plan_by_id(db, plan_id)
        self._remove_output_dir(self.staging_root / plan_id / claim_token)
        return result

    def process_next(self, cancel_event: Event | None = None) -> dict[str, Any] | None:
        if self.processor is None:
            raise EditingError("processor_not_configured")
        claim = self.claim_next_plan()
        if claim is None:
            return None
        plan_id, token = claim["id"], claim["claim_token"]
        try:
            source, source_size, source_sha256 = self.source_identity_for_plan(plan_id)
            result = render_ordinary_plan(
                self.processor,
                source,
                self.output_dir_for_plan(plan_id, token),
                recipe_from_mapping(claim["recipe"]),
                cancel_event=cancel_event,
                expected_source_size=source_size,
                expected_source_sha256=source_sha256,
            )
            return self.complete_plan(plan_id, token, result)
        except EditingError as exc:
            try:
                return self.fail_plan(plan_id, token, exc.code)
            except EditingError:
                raise exc
        except Exception:
            return self.fail_plan(plan_id, token, "processor_failed")

    def plans(self, project_id: str | None = None) -> list[dict[str, Any]]:
        values: tuple[Any, ...] = ()
        query = "SELECT * FROM render_plans"
        if project_id is not None:
            query += " WHERE project_id=?"
            values = (_identifier(project_id),)
        query += " ORDER BY created_at DESC,id"
        with self._db() as db:
            rows = db.execute(query, values).fetchall()
            return [self._plan_public(db, row) for row in rows]

    def plan(self, plan_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._plan_by_id(db, _identifier(plan_id))

    def assets(
        self, plan_id: str | None = None, *, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        if plan_id is not None and project_id is not None:
            raise EditingError("invalid_asset_filter")
        query, values = "SELECT a.* FROM assets a", ()
        if plan_id is not None:
            query += " WHERE a.plan_id=?"
            values = (_identifier(plan_id),)
        elif project_id is not None:
            query += " JOIN render_plans r ON r.id=a.plan_id WHERE r.project_id=?"
            values = (_identifier(project_id),)
        query += (
            " ORDER BY a.created_at,a.plan_id,"
            "CASE a.kind WHEN 'segment' THEN 0 WHEN 'dubbed_video' THEN 1 "
            "WHEN 'cover' THEN 2 ELSE 3 END,a.ordinal,a.id"
        )
        with self._db() as db:
            return [self._asset_public(row) for row in db.execute(query, values)]

    def asset(self, asset_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (_identifier(asset_id),)).fetchone()
        if row is None:
            raise EditingError("asset_not_found")
        return self._asset_public(row)

    def asset_path(self, asset_id: str) -> Path:
        asset_id = _identifier(asset_id)
        with self._db() as db:
            row = db.execute(
                "SELECT a.*,r.state FROM assets a JOIN render_plans r ON r.id=a.plan_id WHERE a.id=?",
                (asset_id,),
            ).fetchone()
        if row is None or row["state"] != "ready":
            raise EditingError("asset_not_ready")
        if row["suffix"] not in _ASSET_SUFFIXES:
            raise EditingError("editing_data_invalid")
        path = self.asset_root / f"{asset_id}{row['suffix']}"
        size, digest = _hash_plain_file(path, maximum=MAX_OUTPUT_BYTES)
        if size != row["size"] or digest != row["sha256"]:
            raise EditingError("asset_changed")
        return path

    def open_asset(
        self, asset_id: str
    ) -> tuple[BinaryIO, os.stat_result, tuple[bytes, ...], dict[str, Any]]:
        asset_id = _identifier(asset_id)
        with self._db() as db:
            row = db.execute(
                "SELECT a.*,r.state FROM assets a "
                "JOIN render_plans r ON r.id=a.plan_id WHERE a.id=?",
                (asset_id,),
            ).fetchone()
        if row is None or row["state"] != "ready":
            raise EditingError("asset_not_ready")
        if row["suffix"] not in _ASSET_SUFFIXES:
            raise EditingError("editing_data_invalid")
        handle, info, chunk_digests = _open_verified_file(
            self.asset_root / f"{asset_id}{row['suffix']}",
            maximum=MAX_OUTPUT_BYTES,
            expected_size=row["size"],
            expected_sha256=row["sha256"],
            error_code="asset_changed",
        )
        return handle, info, chunk_digests, self._asset_public(row)

    def _media_name(self, value: object, suffixes: frozenset[str]) -> str:
        name = _text(value, 240, required=True)
        if Path(name).name != name or name in {".", ".."} or Path(name).suffix.lower() not in suffixes:
            raise EditingError("invalid_media_name")
        return name

    def _optional_integer(
        self, value: object, *, minimum: int, required: bool = False
    ) -> int | None:
        if value is None and not required:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise EditingError("invalid_render_asset")
        return value

    def _copy_and_hash(self, source: Path, destination: Path, maximum: int) -> tuple[int, str]:
        before = _plain(source)
        if not 0 < before.st_size <= maximum:
            raise EditingError("editing_media_size_invalid")
        try:
            _plain(destination.parent, directory=True)
            usage = shutil.disk_usage(destination.parent)
        except (OSError, EditingError) as exc:
            raise EditingError("editing_storage_unavailable") from exc
        if usage.free < before.st_size + EDITING_RESERVE_BYTES:
            raise EditingError("editing_storage_full")
        digest = hashlib.sha256()
        try:
            with source.open("rb") as reader, destination.open("xb") as writer:
                opened = os.fstat(reader.fileno())
                if file_signature(opened) != file_signature(before):
                    raise EditingError("editing_media_changed")
                while chunk := reader.read(1024 * 1024):
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
                after_handle = os.fstat(reader.fileno())
            after = _plain(source)
            copied = _plain(destination)
        except Exception:
            self._discard_unregistered_file(destination)
            raise
        if (file_signature(after_handle) != file_signature(before)
                or file_signature(after) != file_signature(before)
                or copied.st_size != before.st_size):
            self._discard_unregistered_file(destination)
            raise EditingError("editing_media_changed")
        return before.st_size, digest.hexdigest()

    @staticmethod
    def _discard_unregistered_file(path: Path) -> None:
        """Best-effort cleanup must not mask the durable operation result."""

        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _cleanup_orphan_media(self) -> None:
        """Remove only strict managed-looking files absent from the database."""

        with self._db() as db:
            registered_sources = {
                f"{row['id']}{row['suffix']}"
                for row in db.execute("SELECT id,suffix FROM sources")
                if isinstance(row["id"], str)
                and isinstance(row["suffix"], str)
            }
            registered_assets = {
                f"{row['id']}{row['suffix']}"
                for row in db.execute("SELECT id,suffix FROM assets")
                if isinstance(row["id"], str)
                and isinstance(row["suffix"], str)
            }
            active_claims = {
                (row["id"], row["claim_token"])
                for row in db.execute(
                    "SELECT id,claim_token FROM render_plans "
                    "WHERE state IN ('running','canceling')"
                )
                if isinstance(row["id"], str)
                and isinstance(row["claim_token"], str)
                and _ID.fullmatch(row["id"])
                and _ID.fullmatch(row["claim_token"])
            }
            ready_plans = tuple(
                row["id"]
                for row in db.execute(
                    "SELECT id FROM render_plans WHERE state='ready' ORDER BY id"
                )
                if isinstance(row["id"], str) and _ID.fullmatch(row["id"])
            )
        for root, registered, suffixes in (
            (self.source_root, registered_sources, _VIDEO_SUFFIXES),
            (self.asset_root, registered_assets, _ASSET_SUFFIXES),
        ):
            try:
                _plain(root, directory=True)
                with os.scandir(root) as iterator:
                    entries = tuple(iterator)
            except (OSError, EditingError) as exc:
                raise EditingError("editing_storage_unavailable") from exc
            for entry in entries:
                if entry.name in registered:
                    continue
                candidate = Path(entry.name)
                suffix = candidate.suffix.lower()
                if (
                    candidate.name != entry.name
                    or suffix not in suffixes
                    or candidate.suffix != suffix
                    or not _ID.fullmatch(candidate.stem)
                ):
                    continue
                path = root / entry.name
                try:
                    _plain(path)
                except EditingError:
                    continue
                self._discard_unregistered_file(path)

        try:
            _plain(self.staging_root, directory=True)
            with os.scandir(self.staging_root) as iterator:
                plan_entries = tuple(iterator)
        except (OSError, EditingError) as exc:
            raise EditingError("editing_storage_unavailable") from exc
        for plan_entry in plan_entries:
            if not _ID.fullmatch(plan_entry.name):
                continue
            plan_directory = self.staging_root / plan_entry.name
            try:
                _plain(plan_directory, directory=True)
                with os.scandir(plan_directory) as iterator:
                    claim_entries = tuple(iterator)
            except (OSError, EditingError):
                continue
            for claim_entry in claim_entries:
                if not _ID.fullmatch(claim_entry.name):
                    continue
                claim = (plan_entry.name, claim_entry.name)
                if claim in active_claims:
                    continue
                claim_directory = plan_directory / claim_entry.name
                try:
                    _plain(claim_directory, directory=True)
                except EditingError:
                    continue
                self._remove_output_dir(claim_directory)
        for plan_id in ready_plans:
            self._discard_speech_checkpoints(plan_id)

    def _verified_source_row(self, row: sqlite3.Row) -> Path:
        _identifier(row["id"])
        if row["suffix"] not in _VIDEO_SUFFIXES:
            raise EditingError("editing_data_invalid")
        path = self.source_root / f"{row['id']}{row['suffix']}"
        size, digest = _hash_plain_file(path, maximum=MAX_SOURCE_BYTES)
        if size != row["size"] or digest != row["sha256"]:
            raise EditingError("source_changed")
        return path

    def _source_public(self, row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in (
            "id", "source_asset_id", "name", "size", "sha256", "created_at"
        )}

    def _source_by_id(self, db: sqlite3.Connection, source_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise EditingError("source_not_found")
        return self._source_public(row)

    def _draft_public(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw_recipe = json.loads(row["recipe"])
            if raw_recipe == _EMPTY_RECIPE:
                canonical = _EMPTY_RECIPE
            else:
                canonical = recipe_from_mapping(raw_recipe).to_dict()
            encoded = json.dumps(
                canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["recipe_sha256"]:
                raise EditingError("editing_data_invalid")
        except (json.JSONDecodeError, EditingError) as exc:
            raise EditingError("editing_data_invalid") from exc
        return {
            "project_id": row["project_id"], "version": row["version"],
            "recipe": canonical, "recipe_sha256": row["recipe_sha256"],
            "created_at": row["created_at"],
        }

    def _draft_by_version(self, db: sqlite3.Connection, project_id: str, version: int) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM drafts WHERE project_id=? AND version=?", (project_id, version)
        ).fetchone()
        if row is None:
            raise EditingError("draft_not_found")
        return self._draft_public(row)

    def _project_public(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in (
            "id", "source_id", "name", "current_version", "created_at", "updated_at"
        )}
        source = db.execute(
            "SELECT source_asset_id,name,size,sha256 FROM sources WHERE id=?", (row["source_id"],)
        ).fetchone()
        if source is None:
            raise EditingError("editing_data_invalid")
        result.update({
            "source_asset_id": source["source_asset_id"],
            "source_name": source["name"], "source_size": source["size"],
            "source_sha256": source["sha256"],
        })
        result["draft"] = self._draft_by_version(db, row["id"], row["current_version"])
        return result

    def _project_by_id(self, db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise EditingError("project_not_found")
        return self._project_public(db, row)

    def _recipe_from_row(self, row: sqlite3.Row) -> EditRecipe:
        try:
            recipe = recipe_from_mapping(json.loads(row["recipe"]))
            encoded = json.dumps(recipe.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except (json.JSONDecodeError, EditingError) as exc:
            raise EditingError("editing_data_invalid") from exc
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["recipe_sha256"]:
            raise EditingError("editing_data_invalid")
        return recipe

    def _plan_public(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        recipe = self._recipe_from_row(row)
        result = {key: row[key] for key in (
            "id", "project_id", "draft_version", "recipe_sha256", "state", "code",
            "created_at", "updated_at", "confirmed_at", "started_at", "finished_at", "retry_of",
        )}
        result["recipe"] = recipe.to_dict()
        result["dubbing_authorization_sha256"] = (
            None
            if recipe.dubbing.authorization is None
            else recipe.dubbing.authorization.sha256
        )
        binding = db.execute(
            "SELECT revision_id,cues_sha256,parent_id,parent_cues_sha256 "
            "FROM plan_timeline_bindings WHERE plan_id=?",
            (row["id"],),
        ).fetchone()
        if recipe.translation.revision_id is not None and (
            binding is None
            or binding["revision_id"] != recipe.translation.revision_id
        ):
            raise EditingError("editing_data_invalid")
        result["timeline_revision_id"] = (
            None if binding is None else binding["revision_id"]
        )
        result["timeline_cues_sha256"] = (
            None if binding is None else binding["cues_sha256"]
        )
        result["assets"] = [
            self._asset_public(asset) for asset in db.execute(
                "SELECT * FROM assets WHERE plan_id=? ORDER BY "
                "CASE kind WHEN 'segment' THEN 0 WHEN 'dubbed_video' THEN 1 "
                "WHEN 'cover' THEN 2 ELSE 3 END,ordinal,id",
                (row["id"],)
            )
        ]
        return result

    def _plan_by_id(self, db: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM render_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise EditingError("plan_not_found")
        return self._plan_public(db, row)

    def _asset_public(self, row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in (
            "id", "plan_id", "kind", "name", "mime_type", "size", "sha256",
            "ordinal", "duration_ms", "width", "height", "container", "video_codec",
            "audio_codec", "created_at"
        )}

    def _timeline_from_row(
        self, row: sqlite3.Row
    ) -> tuple[dict[str, Any], CanonicalTimeline]:
        try:
            revision_id = _identifier(row["id"])
            project_id = _identifier(row["project_id"])
            parent_id = (
                None if row["parent_id"] is None else _identifier(row["parent_id"])
            )
            kind = row["kind"]
            if (
                kind not in {"transcription", "translation"}
                or (kind == "transcription") != (parent_id is None)
                or not isinstance(row["provider"], str)
                or not _AI_TOKEN.fullmatch(row["provider"])
                or not isinstance(row["model"], str)
                or not _AI_TOKEN.fullmatch(row["model"])
            ):
                raise EditingError("editing_data_invalid")
            timeline = decode_timeline(
                row["cues"], row["cues_sha256"], language=row["language"]
            )
            state = row["state"]
            review_version = row["review_version"]
            if (
                state not in {"review", "approved", "rejected"}
                or isinstance(review_version, bool)
                or review_version not in {0, 1}
                or (
                    state == "review"
                    and (review_version != 0 or row["reviewed_at"] is not None)
                )
                or (
                    state != "review"
                    and (review_version != 1 or row["reviewed_at"] is None)
                )
                or not isinstance(row["code"], str)
                or (row["code"] and not _SAFE_CODE.fullmatch(row["code"]))
            ):
                raise EditingError("editing_data_invalid")
            for field in ("created_at", "updated_at"):
                if _text(row[field], 80, required=True) != row[field]:
                    raise EditingError("editing_data_invalid")
            if row["reviewed_at"] is not None and (
                _text(row["reviewed_at"], 80, required=True) != row["reviewed_at"]
            ):
                raise EditingError("editing_data_invalid")
            public = {
                "id": revision_id,
                "project_id": project_id,
                "parent_id": parent_id,
                "kind": kind,
                "language": timeline.language,
                "provider": row["provider"],
                "model": row["model"],
                "state": state,
                "review_version": review_version,
                "code": row["code"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "reviewed_at": row["reviewed_at"],
                "cues_sha256": timeline.cues_sha256,
                "cue_count": len(timeline.cues),
                "cues": [
                    {
                        "id": cue.id,
                        "order": cue.order,
                        "start_ms": cue.start_ms,
                        "end_ms": cue.end_ms,
                        "source_text": cue.source_text,
                        "source_language": cue.source_language,
                        "speaker_id": cue.speaker_id,
                    }
                    for cue in timeline.cues
                ],
            }
            return public, timeline
        except (AiPipelineError, IndexError, KeyError, TypeError, EditingError) as exc:
            if isinstance(exc, EditingError) and exc.code != "editing_data_invalid":
                raise
            raise EditingError("editing_data_invalid") from exc

    def _timeline_public(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._timeline_from_row(row)[0]

    def _timeline_by_id(
        self, db: sqlite3.Connection, revision_id: str
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM timeline_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise EditingError("timeline_not_found")
        return self._timeline_public(row)

    def _validate_ai_source_revision(
        self, request: CanonicalAiRequest, row: sqlite3.Row | None
    ) -> None:
        if row is None:
            raise EditingError("timeline_not_found")
        public, timeline = self._timeline_from_row(row)
        if (
            public["project_id"] != request.project_id
            or public["state"] != "approved"
            or timeline.language != request.source_language
        ):
            raise EditingError("source_timeline_invalid")

    def _ai_task_from_row(
        self, row: sqlite3.Row
    ) -> tuple[dict[str, Any], CanonicalAiRequest]:
        try:
            task_id = _identifier(row["id"])
            project_id = _identifier(row["project_id"])
            source_revision_id = (
                None
                if row["source_revision_id"] is None
                else _identifier(row["source_revision_id"])
            )
            retry_of = (
                None if row["retry_of"] is None else _identifier(row["retry_of"])
            )
            result_revision_id = (
                None
                if row["result_revision_id"] is None
                else _identifier(row["result_revision_id"])
            )
            request = decode_ai_request(row["request"], row["request_sha256"])
            if (
                request.project_id != project_id
                or request.operation != row["operation"]
                or request.source_revision_id != source_revision_id
                or request.target_language != row["target_language"]
                or request.provider_id != row["provider"]
                or request.model_id != row["model"]
            ):
                raise EditingError("editing_data_invalid")

            state = row["state"]
            if state not in {
                "review",
                "queued",
                "running",
                "canceling",
                "succeeded",
                "failed",
                "canceled",
            }:
                raise EditingError("editing_data_invalid")
            claim_token = row["claim_token"]
            if state in {"running", "canceling"}:
                _identifier(claim_token)
            elif claim_token is not None:
                raise EditingError("editing_data_invalid")
            if (state == "succeeded") != (result_revision_id is not None):
                raise EditingError("editing_data_invalid")
            code = row["code"]
            if not isinstance(code, str) or (code and not _SAFE_CODE.fullmatch(code)):
                raise EditingError("editing_data_invalid")

            timestamps: dict[str, str | None] = {}
            for field in (
                "created_at",
                "updated_at",
                "confirmed_at",
                "started_at",
                "finished_at",
            ):
                value = row[field]
                if value is not None and _text(value, 80, required=True) != value:
                    raise EditingError("editing_data_invalid")
                timestamps[field] = value
            if timestamps["created_at"] is None or timestamps["updated_at"] is None:
                raise EditingError("editing_data_invalid")
            if state == "review" and any(
                timestamps[field] is not None
                for field in ("confirmed_at", "started_at", "finished_at")
            ):
                raise EditingError("editing_data_invalid")
            if state == "queued" and (
                timestamps["confirmed_at"] is None
                or timestamps["started_at"] is not None
                or timestamps["finished_at"] is not None
            ):
                raise EditingError("editing_data_invalid")
            if state in {"running", "canceling", "succeeded"} and (
                timestamps["confirmed_at"] is None or timestamps["started_at"] is None
            ):
                raise EditingError("editing_data_invalid")
            if state in {"running", "canceling"} and timestamps["finished_at"] is not None:
                raise EditingError("editing_data_invalid")
            if state in {"succeeded", "failed", "canceled"} and timestamps["finished_at"] is None:
                raise EditingError("editing_data_invalid")

            public = {
                "id": task_id,
                "project_id": project_id,
                "operation": request.operation,
                "source_revision_id": source_revision_id,
                "target_language": request.target_language,
                "provider": request.provider_id,
                "model": request.model_id,
                "state": state,
                "code": code,
                "request_sha256": request.request_sha256,
                "options": dict(request.request["options"]),
                "authorization": (
                    None
                    if request.authorization is None
                    else request.authorization.to_dict()
                ),
                "authorization_sha256": (
                    None
                    if request.authorization is None
                    else request.authorization.sha256
                ),
                "retry_of": retry_of,
                "result_revision_id": result_revision_id,
                **timestamps,
            }
            return public, request
        except (AiPipelineError, EditingError, IndexError, KeyError, TypeError) as exc:
            if isinstance(exc, EditingError) and exc.code != "editing_data_invalid":
                raise EditingError("editing_data_invalid") from exc
            raise EditingError("editing_data_invalid") from exc

    def _ai_task_public(self, row: sqlite3.Row) -> dict[str, Any]:
        return self._ai_task_from_row(row)[0]

    def _ai_task_by_id(
        self, db: sqlite3.Connection, task_id: str
    ) -> dict[str, Any]:
        row = db.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise EditingError("ai_task_not_found")
        return self._ai_task_public(row)

    def _request_result(self, key: str, operation: str, digest: str) -> str | None:
        with self._db() as db:
            return self._request_result_in(db, key, operation, digest)

    def _request_result_in(
        self, db: sqlite3.Connection, key: str, operation: str, digest: str
    ) -> str | None:
        row = db.execute("SELECT * FROM requests WHERE id=?", (key,)).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["digest"] != digest:
            raise EditingError("idempotency_conflict")
        return row["result_id"]

    def _insert_request(
        self, db: sqlite3.Connection, key: str, operation: str,
        digest: str, result_id: str, now: str,
    ) -> None:
        db.execute(
            "INSERT INTO requests(id,operation,digest,result_id,created_at) VALUES(?,?,?,?,?)",
            (key, operation, digest, result_id, now),
        )

    def _record_request(self, key: str, operation: str, digest: str, result_id: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._request_result_in(db, key, operation, digest)
            if existing is None:
                self._insert_request(db, key, operation, digest, result_id, _now())
            elif existing != result_id:
                raise EditingError("idempotency_conflict")

    def _draft_result_id(self, value: str) -> tuple[str, int]:
        try:
            project_id, version = value.split(":", 1)
            return _identifier(project_id), int(version)
        except (ValueError, EditingError) as exc:
            raise EditingError("editing_data_invalid") from exc

    def _remove_output_dir(self, path: Path) -> None:
        try:
            resolved_root = self.staging_root.resolve(strict=True)
            resolved = path.resolve(strict=False)
            if resolved_root not in resolved.parents or len(resolved.parts) < len(resolved_root.parts) + 2:
                return
            if path.exists() and not path.is_symlink():
                shutil.rmtree(path)
            parent = path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

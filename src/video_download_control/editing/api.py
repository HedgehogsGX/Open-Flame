"""Loopback editing UI/API with a single local media worker."""
from __future__ import annotations

import hmac
import mimetypes
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Condition, Event, RLock, Thread, current_thread
from time import monotonic
from typing import Annotated, Any, BinaryIO, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..ui_assets import page_content_security_policy
from ..uploads.activity_lock import UploadActivityBusy, UploadActivityLease
from .ai import default_capabilities
from .ai_authorization import (
    AiAuthorizationError,
    parse_operation_authorization,
    require_authorization_match,
)
from .ai_bridge import AiBridgeError
from .ai_execution import AiTaskExecutor
from .ai_render import AiRenderProcessor
from .ai_runtime import default_ai_runtime_root
from .contracts import EditingError, MediaProcessor, RenderResult, recipe_from_mapping
from .service import EditingService, VERIFIED_MEDIA_CHUNK_BYTES, default_editing_root
from .web import EDITING_HTML

Identifier = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
Sha256Digest = Annotated[
    str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]
RequestKey = Annotated[
    str,
    Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$"),
]
AiToken = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$"),
]
LanguageCode = Annotated[
    str,
    Field(min_length=2, max_length=71, pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"),
]
_PROGRESS_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class SegmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0, le=604_800_000, strict=True)
    end_ms: int = Field(ge=1, le=604_800_000, strict=True)
    label: str = Field(min_length=1, max_length=120)


class CoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp_ms: int = Field(ge=0, le=604_800_000, strict=True)
    aspect_ratio: Literal["source", "16:9", "4:3", "3:4", "9:16", "1:1"] = "source"
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=240)


class TranslationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)
    source_language: str = Field(default="auto", min_length=1, max_length=32)
    target_language: str = Field(default="", max_length=32)
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=120)
    state: Literal["disabled", "needs_review", "ready", "blocked"] = "disabled"


class DubbingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)
    language: str = Field(default="", max_length=32)
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=120)
    voice: str = Field(default="", max_length=120)
    state: Literal["disabled", "needs_review", "ready", "blocked"] = "disabled"
    replace_original_audio: bool = Field(default=False, strict=True)
    authorization: dict[str, Any] | None = None


class RecipeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[SegmentRequest] = Field(default_factory=list, max_length=100)
    cover: CoverRequest | None = None
    translation: TranslationRequest | None = None
    dubbing: DubbingRequest | None = None


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    idempotency_key: RequestKey


class UpdateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    recipe: RecipeRequest
    idempotency_key: RequestKey


class CreatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: RequestKey
    timeline_revision_id: Identifier | None = None


class ConfirmPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_recipe_sha256: Sha256Digest
    expected_authorization_sha256: Sha256Digest | None = None
    ai_data_egress_accepted: bool = Field(default=False, strict=True)


class RetryPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: RequestKey


class TranscriptionOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: LanguageCode | None = None
    word_timestamps: bool = Field(default=True, strict=True)
    vad: bool = Field(default=True, strict=True)


class GlossaryItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)


class TranslationOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_language: LanguageCode
    target_language: LanguageCode
    glossary: list[GlossaryItemRequest] = Field(default_factory=list, max_length=200)


class CreateTranscriptionTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["transcribe"]
    provider: AiToken
    model: AiToken
    options: TranscriptionOptionsRequest
    expected_authorization_sha256: Sha256Digest
    idempotency_key: RequestKey


class CreateTranslationTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["translate"]
    provider: AiToken
    model: AiToken
    source_revision_id: Identifier
    options: TranslationOptionsRequest
    expected_authorization_sha256: Sha256Digest
    idempotency_key: RequestKey


AiTaskRequest = Annotated[
    CreateTranscriptionTaskRequest | CreateTranslationTaskRequest,
    Field(discriminator="operation"),
]


class RetryAiTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: RequestKey


class ConfirmAiTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_request_sha256: Sha256Digest
    expected_authorization_sha256: Sha256Digest
    ai_data_egress_accepted: bool = Field(default=False, strict=True)


class ReconcileAiInvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, le=999_999, strict=True)
    resolution: Literal[
        "not_accepted", "accepted_without_result", "abandoned"
    ]
    acknowledge: bool = Field(strict=True)


class ReviewTimelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]
    expected_review_version: int = Field(ge=0, le=1, strict=True)


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


def _safe_error(error: EditingError) -> HTTPException:
    code = error.code
    if code.endswith("_not_found") or code in {"source_not_found", "asset_not_found"}:
        status = 404
    elif code in {
        "idempotency_conflict",
        "draft_version_conflict",
        "plan_state_conflict",
        "plan_not_reviewable",
        "stale_render_claim",
        "editing_worker_busy",
        "source_asset_conflict",
        "source_asset_changed",
        "source_changed",
        "asset_changed",
        "ai_operation_blocked",
        "ai_task_not_reviewable",
        "ai_task_state_conflict",
        "ai_task_retry_not_allowed",
        "ai_task_retry_lineage_changed",
        "source_timeline_invalid",
        "timeline_review_conflict",
        "timeline_parent_not_approved",
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
        "ai_task_definition_changed",
        "ai_invocation_conflict",
        "ai_invocation_owner_changed",
        "ai_invocation_owner_inactive",
        "ai_invocation_state_conflict",
        "ai_remote_result_unknown",
        "ai_remote_reconciliation_required",
        "ai_remote_retry_blocked",
        "ai_remote_accepted_without_result",
        "ai_remote_abandoned",
        "render_retry_lineage_changed",
    }:
        status = 409
    elif code in {
        "editing_database_unavailable",
        "editing_storage_unavailable",
        "processor_not_configured",
        "editing_manager_stopped",
        "ai_invocation_database_unavailable",
    }:
        status = 503
    else:
        status = 422
    return HTTPException(status_code=status, detail=code)


def _verified_media_response(
    handle: BinaryIO,
    *,
    file_info,
    chunk_digests: tuple[bytes, ...],
    expected_sha256: str,
    media_type: str,
    filename: str | None,
    disposition: str,
) -> Response:
    """Serve the exact handle and recheck its verified chunks while streaming."""

    # Imported lazily to avoid the top-level API/editing route import cycle.
    from ..api import _VerifiedOriginalFileResponse

    return _VerifiedOriginalFileResponse(
        handle,
        file_info=file_info,
        verified_chunk_sha256=chunk_digests,
        verification_chunk_size=VERIFIED_MEDIA_CHUNK_BYTES,
        filename=filename,
        expected_sha256=expected_sha256,
        media_type=media_type,
        content_disposition_type=disposition,
    )


class EditingManager:
    """Lazily own one editing service and one bounded local worker."""

    def __init__(
        self,
        root: Path,
        processor_factory: Callable[[], MediaProcessor] | None,
        *,
        ai_runtime_root: Path | None = None,
    ) -> None:
        self.root = root
        self.processor_factory = processor_factory
        self.ai_runtime_root = Path(
            ai_runtime_root if ai_runtime_root is not None else default_ai_runtime_root(root)
        )
        self._ai_executor = AiTaskExecutor(
            self.ai_runtime_root, self.root / "ai-work"
        )
        self._lock = RLock()
        self._state_changed = Condition(self._lock)
        self._wake = Event()
        self._stop = Event()
        self._current_cancel: Event | None = None
        self._current_plan_id: str | None = None
        self._current_ai_cancel: Event | None = None
        self._current_ai_task_id: str | None = None
        self._current_ai_progress: tuple[float, str] | None = None
        self._service: EditingService | None = None
        self._thread: Thread | None = None
        self._worker_active = False
        self._active_operations = 0
        self._activity_lease: UploadActivityLease | None = None

    @property
    def media_ready(self) -> bool:
        return self.processor_factory is not None

    def get(self) -> EditingService:
        with self._lock:
            if self._stop.is_set():
                raise EditingError("editing_manager_stopped")
            if self._service is not None:
                return self._service
            try:
                lease = UploadActivityLease.acquire(self.root, exclusive=True)
            except UploadActivityBusy:
                raise EditingError("editing_worker_busy") from None
            try:
                processor = (
                    self.processor_factory() if self.processor_factory else None
                )
                service = EditingService(
                    self.root,
                    processor=processor,
                    recover_interrupted=False,
                )
                service.recover_interrupted(cleanup_orphans=True)
                self._activity_lease = lease
                self._service = service
                thread = Thread(
                    target=self._worker,
                    name="open-flame-editing-worker",
                    daemon=True,
                )
                self._thread = thread
                self._worker_active = True
                thread.start()
            except BaseException:
                self._service = None
                self._thread = None
                self._worker_active = False
                self._activity_lease = None
                lease.release()
                raise
            return self._service

    def _begin_operation(self) -> EditingService:
        with self._lock:
            if self._stop.is_set():
                raise EditingError("editing_manager_stopped")
            self._active_operations += 1
            try:
                return self.get()
            except BaseException:
                self._active_operations -= 1
                self._state_changed.notify_all()
                raise

    def _take_quiescent_lease_locked(self) -> UploadActivityLease | None:
        if (
            not self._stop.is_set()
            or self._worker_active
            or self._active_operations
        ):
            return None
        lease = self._activity_lease
        self._activity_lease = None
        return lease

    def _finish_operation(self) -> None:
        with self._state_changed:
            self._active_operations -= 1
            lease = self._take_quiescent_lease_locked()
            self._state_changed.notify_all()
        if lease is not None:
            lease.release()

    def invoke(self, method: str, *args, **kwargs):
        """Run one service operation while retaining the root activity lease."""

        service = self._begin_operation()
        try:
            return getattr(service, method)(*args, **kwargs)
        finally:
            self._finish_operation()

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, plan_id: str) -> dict[str, Any]:
        service = self._begin_operation()
        try:
            result = service.cancel_plan(plan_id)
            with self._lock:
                if self._current_plan_id == plan_id and self._current_cancel is not None:
                    self._current_cancel.set()
            self._wake.set()
            return result
        finally:
            self._finish_operation()

    def runtime_status(self) -> dict[str, object]:
        result = self._ai_executor.runtime_status()
        with self._lock:
            active = (
                None
                if self._current_ai_task_id is None
                else {
                    "task_id": self._current_ai_task_id,
                    "progress": (
                        None
                        if self._current_ai_progress is None
                        else {
                            "fraction": self._current_ai_progress[0],
                            "code": self._current_ai_progress[1],
                        }
                    ),
                }
            )
        return {**result, "active_task": active}

    def capabilities(self) -> list[dict[str, object]]:
        defaults = [
            item.to_dict() for item in default_capabilities(media_ready=self.media_ready)
        ]
        local = [item for item in defaults if item["operation"] in {"segment", "cover"}]
        return [*local, *self._ai_executor.capabilities()]

    @staticmethod
    def _as_editing_error(
        error: AiBridgeError | AiAuthorizationError,
    ) -> EditingError:
        return EditingError(error.code)

    def validate_ai_operation(
        self,
        operation: str,
        provider_id: str,
        model_id: str,
        stored_authorization: object,
        *,
        expected_authorization_sha256: str,
        voice_id: str | None = None,
    ) -> None:
        """Match a frozen workflow authorization against the current runtime."""

        try:
            current = self._ai_executor.operation_authorization(
                operation,
                provider_id,
                model_id,
                voice_id=voice_id,
            )
            require_authorization_match(
                stored_authorization,
                current,
                expected_sha256=expected_authorization_sha256,
            )
        except (AiBridgeError, AiAuthorizationError) as error:
            raise self._as_editing_error(error) from None

    def create_ai_task(
        self,
        project_id: str,
        operation: str,
        provider_id: str,
        model_id: str,
        options: dict[str, object],
        idempotency_key: str,
        *,
        source_revision_id: str | None = None,
        expected_authorization_sha256: str | None = None,
    ) -> dict[str, Any]:
        if expected_authorization_sha256 is None:
            raise EditingError("ai_authorization_binding_required")
        try:
            authorization = self._ai_executor.operation_authorization(
                operation, provider_id, model_id
            )
            require_authorization_match(
                authorization,
                authorization,
                expected_sha256=expected_authorization_sha256,
            )
        except (AiBridgeError, AiAuthorizationError) as error:
            raise self._as_editing_error(error) from None
        return self.invoke(
            "create_ai_task",
            project_id,
            operation,
            provider_id,
            model_id,
            options,
            idempotency_key,
            source_revision_id=source_revision_id,
            authorization=authorization.to_dict(),
        )

    def confirm_ai_task(
        self,
        task_id: str,
        *,
        expected_request_sha256: str | None = None,
        expected_authorization_sha256: str | None = None,
        ai_data_egress_accepted: bool = False,
    ) -> dict[str, Any]:
        service = self._begin_operation()
        try:
            task = service.ai_task(task_id)
            if expected_authorization_sha256 is None:
                raise EditingError("invalid_ai_task_confirmation")
            try:
                current = self._ai_executor.operation_authorization(
                    task["operation"], task["provider"], task["model"]
                )
                authorization = require_authorization_match(
                    task.get("authorization"),
                    current,
                    expected_sha256=expected_authorization_sha256,
                )
            except (AiBridgeError, AiAuthorizationError) as error:
                raise self._as_editing_error(error) from None
            if type(ai_data_egress_accepted) is not bool:
                raise EditingError("invalid_ai_task_confirmation")
            if authorization.execution == "remote" and not ai_data_egress_accepted:
                raise EditingError("ai_data_egress_confirmation_required")
            if expected_request_sha256 is None:
                raise EditingError("invalid_ai_task_confirmation")
            result = service.confirm_ai_task(
                task_id,
                expected_request_sha256=expected_request_sha256,
            )
            self._wake.set()
            return result
        finally:
            self._finish_operation()

    def confirm_plan(
        self,
        plan_id: str,
        *,
        expected_recipe_sha256: str | None = None,
        expected_authorization_sha256: str | None = None,
        ai_data_egress_accepted: bool = False,
    ) -> dict[str, Any]:
        service = self._begin_operation()
        try:
            plan = service.plan(plan_id)
            recipe = recipe_from_mapping(plan["recipe"])
            if recipe.dubbing.enabled:
                if expected_authorization_sha256 is None:
                    raise EditingError("invalid_plan_confirmation")
                try:
                    authorization = parse_operation_authorization(
                        recipe.dubbing.authorization
                    )
                    current = self._ai_executor.operation_authorization(
                        "synthesize",
                        recipe.dubbing.provider,
                        recipe.dubbing.model,
                    )
                    require_authorization_match(
                        authorization,
                        current,
                        expected_sha256=expected_authorization_sha256,
                    )
                except (AiBridgeError, AiAuthorizationError) as error:
                    raise self._as_editing_error(error) from None
                if authorization.execution == "remote" and not ai_data_egress_accepted:
                    raise EditingError("ai_data_egress_confirmation_required")
            result = service.confirm_plan(
                plan_id,
                expected_recipe_sha256=expected_recipe_sha256,
                ai_data_egress_accepted=ai_data_egress_accepted,
            )
            self._wake.set()
            return result
        finally:
            self._finish_operation()

    def cancel_ai_tasks(self, task_ids: Sequence[str]) -> list[dict[str, Any]]:
        service = self._begin_operation()
        try:
            result = service.cancel_ai_tasks(task_ids)
            self._signal_ai_cancellations(result)
            return result
        finally:
            self._finish_operation()

    def cancel_ai_project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        service = self._begin_operation()
        try:
            result = service.cancel_ai_project_tasks(project_id)
            self._signal_ai_cancellations(result)
            return result
        finally:
            self._finish_operation()

    def cancel_workflow_request_artifacts(
        self,
        project_request_key: str,
        plan_request_key: str,
        *,
        expected_project_id: str | None,
        expected_name: str,
        expected_source_asset_id: str,
        expected_recipe: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically discover workflow editing artifacts and signal active workers."""

        service = self._begin_operation()
        try:
            result = service.cancel_workflow_request_artifacts(
                project_request_key,
                plan_request_key,
                expected_project_id=expected_project_id,
                expected_name=expected_name,
                expected_source_asset_id=expected_source_asset_id,
                expected_recipe=expected_recipe,
            )
            plan = result.get("plan")
            if isinstance(plan, Mapping):
                plan_id = plan.get("id")
                with self._lock:
                    if (
                        self._current_plan_id == plan_id
                        and self._current_cancel is not None
                    ):
                        self._current_cancel.set()
            tasks = result.get("ai_tasks")
            if isinstance(tasks, Sequence) and not isinstance(tasks, (str, bytes)):
                self._signal_ai_cancellations(tasks)
            else:
                self._wake.set()
            return result
        finally:
            self._finish_operation()

    def _signal_ai_cancellations(self, result: Sequence[Mapping[str, Any]]) -> None:
        result_ids = {
            task.get("id") for task in result if isinstance(task, Mapping)
        }
        with self._lock:
            if (
                self._current_ai_task_id in result_ids
                and self._current_ai_cancel is not None
            ):
                self._current_ai_cancel.set()
        self._wake.set()

    def cancel_ai_task(self, task_id: str) -> dict[str, Any]:
        return self.cancel_ai_tasks((task_id,))[0]

    def _worker(self) -> None:
        try:
            self._worker_loop()
        except EditingError:
            if not self._stop.is_set():
                raise
        finally:
            with self._state_changed:
                self._worker_active = False
                lease = self._take_quiescent_lease_locked()
                self._state_changed.notify_all()
            if lease is not None:
                lease.release()

    def _worker_loop(self) -> None:
        service = self.get()
        while not self._stop.is_set():
            ai_claim = None
            claim_ai_task = getattr(service, "claim_next_ai_task", None)
            try:
                if callable(claim_ai_task):
                    ai_claim = claim_ai_task()
            except EditingError:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if ai_claim is not None:
                self._process_ai_claim(service, ai_claim)
                continue

            if getattr(service, "processor", None) is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            claim = None
            try:
                claim = service.claim_next_plan()
            except EditingError:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if claim is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            plan_id, token = claim["id"], claim["claim_token"]
            cancel_event = Event()
            with self._lock:
                self._current_plan_id = plan_id
                self._current_cancel = cancel_event
                if self._stop.is_set():
                    cancel_event.set()
            # A cancellation can land after the database claim but before the
            # in-memory event is published.  Reconcile the durable state once
            # the event is visible; later cancellations set the event directly.
            try:
                if service.plan_cancellation_requested(plan_id, token):
                    cancel_event.set()
            except EditingError:
                pass
            try:
                assert service.processor is not None
                source, source_size, source_sha256 = service.source_identity_for_plan(
                    plan_id
                )
                recipe = recipe_from_mapping(claim["recipe"])
                output_dir = service.output_dir_for_plan(plan_id, token)
                if recipe.translation.enabled or recipe.dubbing.enabled:
                    speech_provider = None
                    if recipe.dubbing.enabled:
                        speech_provider = self._ai_executor.speech_provider(
                            recipe.dubbing.provider,
                            recipe.dubbing.model,
                            recipe.dubbing.authorization,
                            service=service,
                            render_plan_id=plan_id,
                            owner_claim_token=token,
                        )
                    result = AiRenderProcessor(service.processor).render(
                        source,
                        output_dir,
                        recipe,
                        timeline=service.approved_timeline_for_plan(plan_id),
                        speech_provider=speech_provider,
                        cancel_event=cancel_event,
                        expected_source_size=source_size,
                        expected_source_sha256=source_sha256,
                    )
                else:
                    result = service.processor.render(
                        source,
                        output_dir,
                        recipe,
                        cancel_event=cancel_event,
                        expected_source_size=source_size,
                        expected_source_sha256=source_sha256,
                    )
                service.complete_plan(plan_id, token, result)
            except AiBridgeError as error:
                try:
                    service.fail_plan(
                        plan_id,
                        token,
                        error.code,
                        canceled=cancel_event.is_set(),
                    )
                except EditingError:
                    pass
            except EditingError as error:
                try:
                    service.fail_plan(
                        plan_id,
                        token,
                        error.code,
                        canceled=cancel_event.is_set(),
                    )
                except EditingError:
                    pass
            except Exception:
                try:
                    service.fail_plan(
                        plan_id,
                        token,
                        "processor_failed",
                        canceled=cancel_event.is_set(),
                    )
                except EditingError:
                    pass
            finally:
                with self._lock:
                    self._current_plan_id = None
                    self._current_cancel = None

    def _process_ai_claim(
        self, service: EditingService, claim: dict[str, Any]
    ) -> None:
        task_id = claim.get("id")
        token = claim.get("claim_token")
        if not isinstance(task_id, str) or not isinstance(token, str):
            return
        cancel_event = Event()
        with self._lock:
            self._current_ai_task_id = task_id
            self._current_ai_cancel = cancel_event
            self._current_ai_progress = None
            if self._stop.is_set():
                cancel_event.set()

        def cancelled() -> bool:
            if cancel_event.is_set():
                return True
            try:
                if service.ai_task_cancellation_requested(task_id, token):
                    cancel_event.set()
            except (EditingError, sqlite3.Error):
                cancel_event.set()
            return cancel_event.is_set()

        def progress(fraction: float, code: str) -> None:
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (int, float))
                or not 0.0 <= float(fraction) <= 1.0
                or not isinstance(code, str)
                or not _PROGRESS_CODE.fullmatch(code)
            ):
                raise AiBridgeError("ai_progress_invalid")
            with self._lock:
                if self._current_ai_task_id == task_id:
                    self._current_ai_progress = (float(fraction), code)

        try:
            if cancelled():
                raise AiBridgeError("ai_operation_canceled")
            self._ai_executor.execute(
                service,
                claim,
                progress=progress,
                cancelled=cancelled,
                cancel_event=cancel_event,
            )
        except AiBridgeError as error:
            try:
                service.fail_ai_task(
                    task_id,
                    token,
                    error.code,
                    canceled=(cancelled() or error.code == "ai_operation_canceled"),
                )
            except EditingError:
                pass
        except EditingError:
            try:
                service.fail_ai_task(
                    task_id,
                    token,
                    "ai_execution_failed",
                    canceled=cancelled(),
                )
            except EditingError:
                pass
        except Exception:
            try:
                service.fail_ai_task(
                    task_id,
                    token,
                    "ai_execution_failed",
                    canceled=cancelled(),
                )
            except EditingError:
                pass
        finally:
            with self._lock:
                if self._current_ai_task_id == task_id:
                    self._current_ai_task_id = None
                    self._current_ai_cancel = None
                    self._current_ai_progress = None

    def stop(self, *, timeout_seconds: float = 10) -> None:
        deadline = monotonic() + max(0.0, timeout_seconds)
        with self._state_changed:
            self._stop.set()
            self._wake.set()
            if self._current_cancel is not None:
                self._current_cancel.set()
            if self._current_ai_cancel is not None:
                self._current_ai_cancel.set()
            thread = self._thread
        if thread is current_thread():
            return
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - monotonic()))
        with self._state_changed:
            while self._worker_active or self._active_operations:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                self._state_changed.wait(timeout=remaining)
            lease = self._take_quiescent_lease_locked()
        if lease is not None:
            lease.release()

    def resolve_output(self, output_id: str) -> tuple[Path, str, str]:
        service = self._begin_operation()
        try:
            record = service.asset(output_id)
            if record["kind"] not in {"segment", "dubbed_video"}:
                raise EditingError("edit_output_not_video")
            return service.asset_path(output_id), record["sha256"], record["name"]
        finally:
            self._finish_operation()

    def resolve_cover(self, output_id: str) -> tuple[Path, str, str]:
        service = self._begin_operation()
        try:
            record = service.asset(output_id)
            if record["kind"] != "cover":
                raise EditingError("edit_output_not_cover")
            return service.asset_path(output_id), record["sha256"], record["name"]
        finally:
            self._finish_operation()


def install_editing_routes(
    app: FastAPI,
    *,
    data_root: Path,
    original_asset_resolver: Callable[[str], tuple[Path, str]] | None = None,
    processor_factory: Callable[[], MediaProcessor] | None = None,
) -> EditingManager:
    """Install lazy editor routes without creating its database or media root."""

    manager = EditingManager(
        default_editing_root(data_root),
        processor_factory,
        ai_runtime_root=default_ai_runtime_root(data_root),
    )
    nonce = secrets.token_urlsafe(32)
    app.state.editing_manager = manager

    @app.middleware("http")
    async def editing_boundary(request: Request, call_next):
        path = request.url.path
        if path != "/edits" and not path.startswith("/api/v1/edits/"):
            return await call_next(request)
        hosts = request.headers.getlist("host")
        expected = _origin(request.url.scheme + "://" + hosts[0]) if len(hosts) == 1 else None
        origins = request.headers.getlist("origin")
        fetch_sites = request.headers.getlist("sec-fetch-site")
        safe = (
            expected is not None
            and len(origins) <= 1
            and len(fetch_sites) <= 1
            and (not origins or _origin(origins[0]) == expected)
            and (not fetch_sites or fetch_sites[0] in {"same-origin", "none"})
        )
        if request.method not in {"GET", "HEAD"}:
            tokens = request.headers.getlist("x-editing-csrf")
            safe = (
                safe
                and len(tokens) == 1
                and tokens[0].isascii()
                and hmac.compare_digest(tokens[0], nonce)
            )
        if not safe:
            return JSONResponse({"detail": "editing_request_forbidden"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    async def invoke(method: str, *args, **kwargs):
        def work():
            return manager.invoke(method, *args, **kwargs)

        try:
            return await run_in_threadpool(work)
        except EditingError as error:
            raise _safe_error(error) from None
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="editing_database_unavailable") from None

    async def invoke_manager(method: str, *args, **kwargs):
        def work():
            return getattr(manager, method)(*args, **kwargs)

        try:
            return await run_in_threadpool(work)
        except EditingError as error:
            raise _safe_error(error) from None
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="editing_database_unavailable") from None

    @app.get("/edits", response_class=HTMLResponse, include_in_schema=False)
    def page():
        return HTMLResponse(
            EDITING_HTML,
            headers={"Content-Security-Policy": page_content_security_policy(EDITING_HTML)},
        )

    router = APIRouter(prefix="/api/v1/edits")

    @router.get("/session")
    def session():
        return {"csrf_token": nonce}

    @router.get("/capabilities")
    def capabilities():
        return manager.capabilities()

    @router.get("/ai/runtime")
    def ai_runtime_status():
        return manager.runtime_status()

    @router.get("/ai/capabilities")
    def ai_capabilities():
        return [
            item
            for item in manager.capabilities()
            if item["operation"] in {"transcribe", "translate", "dub"}
        ]

    @router.get("/status")
    async def status():
        return await invoke("status")

    @router.get("/projects")
    async def projects():
        return await invoke("projects")

    @router.get("/projects/{project_id}")
    async def project(project_id: Identifier):
        return await invoke("project", project_id)

    @router.post("/projects/assets/{asset_id}", status_code=201)
    async def create_project_from_asset(asset_id: str, payload: CreateProjectRequest):
        if original_asset_resolver is None:
            raise HTTPException(status_code=404, detail="source_asset_not_found")
        try:
            path, expected_sha256 = await run_in_threadpool(
                original_asset_resolver, asset_id
            )
            source = await invoke(
                "import_source",
                path,
                "download-" + asset_id + path.suffix.lower(),
                expected_sha256,
                source_asset_id=asset_id,
                idempotency_key=payload.idempotency_key + ":source",
            )
            return await invoke(
                "create_project",
                source["id"],
                payload.name,
                payload.idempotency_key + ":project",
            )
        except EditingError as error:
            raise _safe_error(error) from None

    @router.get("/projects/{project_id}/draft")
    async def draft(project_id: Identifier):
        return await invoke("draft", project_id)

    @router.put("/projects/{project_id}/draft")
    async def update_draft(project_id: Identifier, payload: UpdateDraftRequest):
        return await invoke(
            "update_draft",
            project_id,
            payload.expected_version,
            payload.recipe.model_dump(),
            payload.idempotency_key,
        )

    @router.post("/projects/{project_id}/plans", status_code=201)
    async def create_plan(project_id: Identifier, payload: CreatePlanRequest):
        return await invoke(
            "create_plan",
            project_id,
            payload.expected_version,
            payload.idempotency_key,
            timeline_revision_id=payload.timeline_revision_id,
        )

    @router.get("/projects/{project_id}/source", response_class=Response)
    async def project_source(project_id: Identifier):
        handle, info, chunk_digests, source = await invoke(
            "open_project_source", project_id
        )
        media_type = mimetypes.guess_type(source["name"])[0] or "application/octet-stream"
        return _verified_media_response(
            handle,
            file_info=info,
            chunk_digests=chunk_digests,
            expected_sha256=source["sha256"],
            media_type=media_type,
            filename=None,
            disposition="inline",
        )

    @router.post("/projects/{project_id}/ai-tasks", status_code=201)
    async def create_ai_task(project_id: Identifier, payload: AiTaskRequest):
        if isinstance(payload, CreateTranscriptionTaskRequest):
            options: dict[str, object] = payload.options.model_dump()
            source_revision_id = None
        else:
            options = {
                "source_language": payload.options.source_language,
                "target_language": payload.options.target_language,
                "glossary": [
                    [item.source, item.target] for item in payload.options.glossary
                ],
            }
            source_revision_id = payload.source_revision_id
        return await invoke_manager(
            "create_ai_task",
            project_id,
            payload.operation,
            payload.provider,
            payload.model,
            options,
            payload.idempotency_key,
            source_revision_id=source_revision_id,
            expected_authorization_sha256=payload.expected_authorization_sha256,
        )

    @router.get("/ai-tasks")
    async def ai_tasks(project_id: Identifier | None = Query(default=None)):
        return await invoke("ai_tasks", project_id=project_id)

    @router.get("/ai-tasks/{task_id}")
    async def ai_task(task_id: Identifier):
        return await invoke("ai_task", task_id)

    @router.post("/ai-tasks/{task_id}/confirm")
    async def confirm_ai_task(task_id: Identifier, payload: ConfirmAiTaskRequest):
        return await invoke_manager(
            "confirm_ai_task",
            task_id,
            expected_request_sha256=payload.expected_request_sha256,
            expected_authorization_sha256=payload.expected_authorization_sha256,
            ai_data_egress_accepted=payload.ai_data_egress_accepted,
        )

    @router.post("/ai-tasks/{task_id}/cancel")
    async def cancel_ai_task(task_id: Identifier):
        return await invoke_manager("cancel_ai_task", task_id)

    @router.post("/ai-tasks/{task_id}/retry", status_code=201)
    async def retry_ai_task(task_id: Identifier, payload: RetryAiTaskRequest):
        return await invoke("retry_ai_task", task_id, payload.idempotency_key)

    @router.get("/ai-invocations")
    async def ai_invocations(
        project_id: Identifier | None = Query(default=None),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        limit: int = Query(default=200, ge=1, le=1_000),
    ):
        return await invoke(
            "ai_invocations",
            project_id=project_id,
            offset=offset,
            limit=limit,
        )

    @router.post("/ai-invocations/{invocation_id}/reconcile")
    async def reconcile_ai_invocation(
        invocation_id: Identifier, payload: ReconcileAiInvocationRequest
    ):
        return await invoke(
            "reconcile_ai_invocation",
            invocation_id,
            payload.expected_revision,
            payload.resolution,
            acknowledge=payload.acknowledge,
        )

    @router.get("/timelines")
    async def timelines(project_id: Identifier | None = Query(default=None)):
        return await invoke("timelines", project_id=project_id)

    @router.get("/timelines/{revision_id}")
    async def timeline(revision_id: Identifier):
        return await invoke("timeline", revision_id)

    @router.post("/timelines/{revision_id}/review")
    async def review_timeline(
        revision_id: Identifier, payload: ReviewTimelineRequest
    ):
        return await invoke(
            "review_timeline",
            revision_id,
            payload.decision,
            payload.expected_review_version,
        )

    @router.get("/plans")
    async def plans(project_id: Identifier | None = Query(default=None)):
        return await invoke("plans", project_id=project_id)

    @router.get("/plans/{plan_id}")
    async def plan(plan_id: Identifier):
        return await invoke("plan", plan_id)

    @router.post("/plans/{plan_id}/confirm")
    async def confirm(
        plan_id: Identifier, payload: ConfirmPlanRequest | None = None
    ):
        if not manager.media_ready:
            raise HTTPException(status_code=409, detail="processor_not_configured")
        result = await invoke_manager(
            "confirm_plan",
            plan_id,
            expected_recipe_sha256=(
                None if payload is None else payload.expected_recipe_sha256
            ),
            ai_data_egress_accepted=(
                False if payload is None else payload.ai_data_egress_accepted
            ),
            expected_authorization_sha256=(
                None if payload is None else payload.expected_authorization_sha256
            ),
        )
        return result

    @router.post("/plans/{plan_id}/cancel")
    async def cancel(plan_id: Identifier):
        try:
            return await run_in_threadpool(manager.cancel, plan_id)
        except EditingError as error:
            raise _safe_error(error) from None

    @router.post("/plans/{plan_id}/retry", status_code=201)
    async def retry(plan_id: Identifier, payload: RetryPlanRequest):
        return await invoke("retry_plan", plan_id, payload.idempotency_key)

    @router.get("/assets")
    async def assets(project_id: Identifier | None = Query(default=None)):
        return await invoke("assets", project_id=project_id)

    @router.get("/assets/{asset_id}")
    async def asset(asset_id: Identifier):
        return await invoke("asset", asset_id)

    @router.get("/assets/{asset_id}/content", response_class=Response)
    async def asset_content(asset_id: Identifier):
        handle, info, chunk_digests, record = await invoke("open_asset", asset_id)
        disposition = "inline" if record["kind"] == "cover" else "attachment"
        return _verified_media_response(
            handle,
            file_info=info,
            chunk_digests=chunk_digests,
            expected_sha256=record["sha256"],
            media_type=record["mime_type"],
            filename=record["name"] if disposition == "attachment" else None,
            disposition=disposition,
        )

    app.include_router(router)
    return manager

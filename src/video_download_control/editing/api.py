"""Loopback editing UI and HTTP API routes."""

from __future__ import annotations

import mimetypes
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..local_http_guard import install_local_http_guard
from ..ui_assets import page_content_security_policy
from ..verified_media_response import VerifiedOpenFileResponse
from .ai_runtime import default_ai_runtime_root
from .contracts import EditingError, MediaProcessor
from .manager import EditingManager
from .service import (
    EditingService,
    VERIFIED_MEDIA_CHUNK_BYTES,
    default_editing_root,
)
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
    revision_id: Identifier | None = None


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
    rate: float = Field(default=1.0, ge=0.88, le=1.12, strict=True, allow_inf_nan=False)


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

    return VerifiedOpenFileResponse(
        handle,
        file_info=file_info,
        verified_chunk_sha256=chunk_digests,
        verification_chunk_size=VERIFIED_MEDIA_CHUNK_BYTES,
        filename=filename,
        expected_sha256=expected_sha256,
        media_type=media_type,
        content_disposition_type=disposition,
    )


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
        _service_factory=EditingService,
    )
    nonce = install_local_http_guard(
        app,
        protects_path=lambda path: path == "/edits"
        or path.startswith("/api/v1/edits/"),
        csrf_header="x-editing-csrf",
        forbidden_detail="editing_request_forbidden",
        requires_csrf=lambda method, _path: method not in {"GET", "HEAD"},
    )
    app.state.editing_manager = manager

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

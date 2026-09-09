"""Loopback API for durable URL-to-publish workflows."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..local_http_guard import install_local_http_guard
from ..ui_assets import page_content_security_policy
from .manager import WorkflowManager
from .presets import WorkflowPresetError, WorkflowPresetStore
from .contracts import WorkflowError
from .web import WORKFLOW_HTML


Identifier = Annotated[
    str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
]
Sha256Digest = Annotated[
    str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]
RequestKey = Annotated[
    str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]{8,128}$")
]


class CreateWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=8, max_length=4096)
    name: str = Field(min_length=1, max_length=160)
    profile: dict[str, Any]
    idempotency_key: RequestKey


class CreatePresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    profile: dict[str, Any]


class ConfirmWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    expected_profile_sha256: Sha256Digest | None = None


def _safe_error(exc: WorkflowError) -> HTTPException:
    code = (
        exc.code
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", exc.code)
        else "workflow_failed"
    )
    if code.endswith("_not_found"):
        status = 404
    elif code in {
        "idempotency_conflict",
        "workflow_revision_conflict",
        "workflow_state_conflict",
        "workflow_profile_changed",
        "workflow_preset_conflict",
        "workflow_preset_authorization_changed",
        "account_session_changed",
        "ai_authorization_changed",
    }:
        status = 409
    elif code in {
        "workflow_database_unavailable",
        "workflow_manager_stopped",
        "workflow_preset_storage_unavailable",
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
    }:
        status = 503
    else:
        status = 422
    return HTTPException(status_code=status, detail=code)


def install_workflow_routes(app: FastAPI, manager: WorkflowManager) -> None:
    """Install a lazy workflow surface without initializing its database."""

    nonce = install_local_http_guard(
        app,
        protects_path=lambda path: path == "/workflows"
        or path == "/api/v1/workflows"
        or path.startswith("/api/v1/workflows/"),
        csrf_header="x-workflow-csrf",
        forbidden_detail="workflow_request_forbidden",
        requires_csrf=lambda method, _path: method not in {"GET", "HEAD"},
    )
    app.state.workflow_manager = manager
    preset_store = WorkflowPresetStore(manager.root)

    async def invoke(method: str, *args, **kwargs):
        try:
            return await run_in_threadpool(manager.invoke, method, *args, **kwargs)
        except WorkflowError as exc:
            raise _safe_error(exc) from None
        except sqlite3.Error:
            raise HTTPException(
                status_code=503, detail="workflow_database_unavailable"
            ) from None

    async def preset_invoke(method: str, *args, **kwargs):
        try:
            return await run_in_threadpool(
                getattr(preset_store, method), *args, **kwargs
            )
        except WorkflowPresetError as exc:
            raise _safe_error(WorkflowError(exc.code)) from None

    @app.get("/workflows", response_class=HTMLResponse, include_in_schema=False)
    def page():
        return HTMLResponse(
            WORKFLOW_HTML,
            headers={
                "Content-Security-Policy": page_content_security_policy(WORKFLOW_HTML)
            },
        )

    router = APIRouter(prefix="/api/v1/workflows")

    @router.get("/session")
    def session():
        return {"csrf_token": nonce}

    @router.get("")
    async def workflows(limit: int = Query(default=50, ge=1, le=100)):
        return await invoke("list", limit=limit)

    @router.get("/presets")
    async def presets():
        return await preset_invoke("list")

    @router.post("/presets", status_code=201)
    async def create_preset(payload: CreatePresetRequest):
        return await preset_invoke("create", payload.name, payload.profile)

    @router.get("/presets/{preset_id}")
    async def preset(preset_id: Identifier):
        return await preset_invoke("get", preset_id)

    @router.post("/presets/{preset_id}/workflows", status_code=201)
    async def create_from_preset(
        preset_id: Identifier, payload: CreateWorkflowRequest
    ):
        profile = await preset_invoke("materialize", preset_id, payload.profile)
        result = await invoke(
            "create",
            source_url=payload.source_url,
            name=payload.name,
            profile=profile,
            idempotency_key=payload.idempotency_key,
        )
        manager.wake()
        return result

    @router.post("", status_code=201)
    async def create(payload: CreateWorkflowRequest):
        result = await invoke("create", **payload.model_dump())
        manager.wake()
        return result

    @router.get("/{workflow_id}")
    async def workflow(workflow_id: Identifier):
        return await invoke("get", workflow_id)

    @router.get("/{workflow_id}/events")
    async def events(workflow_id: Identifier):
        return await invoke("events", workflow_id)

    @router.post("/{workflow_id}/advance")
    async def advance(workflow_id: Identifier):
        result = await invoke("advance", workflow_id)
        manager.wake()
        return result

    @router.post("/{workflow_id}/cancel")
    async def cancel(workflow_id: Identifier, payload: ConfirmWorkflowRequest):
        result = await invoke(
            "cancel", workflow_id, expected_revision=payload.expected_revision
        )
        manager.wake()
        return result

    @router.post("/{workflow_id}/confirm-edit")
    async def confirm_edit(workflow_id: Identifier, payload: ConfirmWorkflowRequest):
        result = await invoke(
            "confirm_edit",
            workflow_id,
            expected_revision=payload.expected_revision,
            expected_profile_sha256=payload.expected_profile_sha256,
        )
        manager.wake()
        return result

    @router.post("/{workflow_id}/confirm-ai")
    async def confirm_ai(workflow_id: Identifier, payload: ConfirmWorkflowRequest):
        result = await invoke(
            "confirm_ai",
            workflow_id,
            expected_revision=payload.expected_revision,
            expected_profile_sha256=payload.expected_profile_sha256,
        )
        manager.wake()
        return result

    @router.post("/{workflow_id}/confirm-upload")
    async def confirm_upload(
        workflow_id: Identifier, payload: ConfirmWorkflowRequest
    ):
        result = await invoke(
            "confirm_upload", workflow_id, expected_revision=payload.expected_revision
        )
        manager.wake()
        return result

    @router.post("/{workflow_id}/retry")
    async def retry(workflow_id: Identifier, payload: ConfirmWorkflowRequest):
        result = await invoke(
            "retry", workflow_id, expected_revision=payload.expected_revision
        )
        manager.wake()
        return result

    app.include_router(router)


__all__ = ["install_workflow_routes"]

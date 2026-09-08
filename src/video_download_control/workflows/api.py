"""Loopback API for durable URL-to-publish workflows."""

from __future__ import annotations

import hmac
import re
import secrets
import sqlite3
from typing import Any, Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..ui_assets import page_content_security_policy
from .manager import WorkflowManager
from .presets import WorkflowPresetError, WorkflowPresetStore
from .service import WorkflowError
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


class CreatePresetWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=8, max_length=4096)
    name: str = Field(min_length=1, max_length=160)
    profile: dict[str, Any]
    idempotency_key: RequestKey


class ConfirmWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    expected_profile_sha256: Sha256Digest | None = None


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
    }:
        status = 409
    elif code in {
        "workflow_database_unavailable",
        "workflow_manager_stopped",
        "workflow_preset_storage_unavailable",
    }:
        status = 503
    else:
        status = 422
    return HTTPException(status_code=status, detail=code)


def install_workflow_routes(app: FastAPI, manager: WorkflowManager) -> None:
    """Install a lazy workflow surface without initializing its database."""

    nonce = secrets.token_urlsafe(32)
    app.state.workflow_manager = manager
    preset_store = WorkflowPresetStore(manager.root)

    @app.middleware("http")
    async def workflow_boundary(request: Request, call_next):
        path = request.url.path
        if (
            path != "/workflows"
            and path != "/api/v1/workflows"
            and not path.startswith("/api/v1/workflows/")
        ):
            return await call_next(request)
        hosts = request.headers.getlist("host")
        expected = (
            _origin(request.url.scheme + "://" + hosts[0]) if len(hosts) == 1 else None
        )
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
            tokens = request.headers.getlist("x-workflow-csrf")
            safe = (
                safe
                and len(tokens) == 1
                and tokens[0].isascii()
                and hmac.compare_digest(tokens[0], nonce)
            )
        if not safe:
            return JSONResponse(
                {"detail": "workflow_request_forbidden"}, status_code=403
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

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
        preset_id: Identifier, payload: CreatePresetWorkflowRequest
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

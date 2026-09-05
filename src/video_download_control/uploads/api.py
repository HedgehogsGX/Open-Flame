"""Lazy, loopback-only upload UI and API; no multipart or credential bodies."""
from __future__ import annotations

import hmac
import os
import re
import secrets
import sqlite3
import tempfile
from pathlib import Path
from threading import RLock
from typing import Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .contracts import UploadError
from .web import UPLOAD_HTML

MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
_ACCOUNT_FIELDS = ("id", "platform", "name", "auth_state", "code")
_SOURCE_FIELDS = ("id", "name", "size", "sha256")
_OPERATION_FIELDS = ("id", "account_id", "action", "state", "code", "login_phase", "qr_available", "qr_revision", "expires_at")
_JOB_FIELDS = (
    "id", "platform", "account_id", "account_name", "source_id", "title",
    "description", "tags", "category_id", "mode", "copyright", "source_credit",
    "state", "code", "created_at", "updated_at", "retry_of", "source_name",
    "source_size", "source_sha256",
)


class AccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["bilibili", "douyin", "tencent"]
    name: str = Field(min_length=1, max_length=60)


class JobsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=64)
    account_ids: list[str] = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    category_id: int | None = Field(default=None, ge=1, le=10000)
    mode: Literal["publish", "draft"] = "publish"
    copyright: Literal[1, 2] | None = None
    source_credit: str = Field(default="", max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=128)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_unknown: bool = False


def _public(record: dict, fields: tuple[str, ...]) -> dict:
    return {key: record.get(key) for key in fields}


def _safe_error(exc: UploadError) -> HTTPException:
    code = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", exc.code) else "upload_failed"
    return HTTPException(status_code=404 if code.endswith("not_found") else 409, detail=code)


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
            return None
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _default_factory(root: Path):
    from .service import UploadService
    return UploadService(root)


class _LazyUploads:
    def __init__(self, app: FastAPI, root: Path):
        self.app, self.root = app, root
        self.lock = RLock()
        self.service = None
        self.closed = False

    def get(self):
        with self.lock:
            if self.closed:
                raise UploadError("uploader_stopped")
            if self.service is None:
                candidate = self.app.state.upload_service_factory(self.root)
                try:
                    candidate.start()
                except BaseException:
                    candidate.stop()
                    raise
                self.service = candidate
            return self.service

    def stop(self):
        with self.lock:
            self.closed = True
            current = self.service
        if current is not None:
            current.stop()

    def recover(self):
        with self.lock:
            if self.closed:
                raise UploadError("uploader_stopped")
            current = self.service
        if current is None:
            current = self.get()
        else:
            try:
                current.start()
            except UploadError:
                raise
            except Exception:
                raise UploadError("scheduler_recovery_failed") from None
        return current.status()


def install_upload_routes(
    app: FastAPI,
    *,
    data_root: Path,
    original_asset_resolver: Callable[[str], tuple[Path, str]] | None = None,
) -> None:
    """Install routes without touching upload directories, workers or accounts."""
    root = data_root.with_name(data_root.name + "-uploads")
    manager = _LazyUploads(app, root)
    nonce = secrets.token_urlsafe(32)
    app.state.upload_service_factory = _default_factory
    app.state.upload_manager = manager

    @app.middleware("http")
    async def upload_boundary(request: Request, call_next):
        path = request.url.path
        if path != "/uploads" and not path.startswith("/api/v1/uploads/"):
            return await call_next(request)
        hosts = request.headers.getlist("host")
        expected = _origin(request.url.scheme + "://" + hosts[0]) if len(hosts) == 1 else None
        origins = request.headers.getlist("origin")
        fetch_sites = request.headers.getlist("sec-fetch-site")
        safe = (expected is not None and len(origins) <= 1 and len(fetch_sites) <= 1
                and (not origins or _origin(origins[0]) == expected)
                and (not fetch_sites or fetch_sites[0] in {"same-origin", "none"}))
        if request.method not in {"GET", "HEAD"} or path.endswith("/qr"):
            tokens = request.headers.getlist("x-upload-csrf")
            safe = safe and len(tokens) == 1 and tokens[0].isascii() and hmac.compare_digest(tokens[0], nonce)
        if not safe:
            return JSONResponse({"detail": "upload_request_forbidden"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    async def invoke(method: str, *args, **kwargs):
        def work():
            return getattr(manager.get(), method)(*args, **kwargs)
        try:
            return await run_in_threadpool(work)
        except UploadError as exc:
            raise _safe_error(exc) from None
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="upload_database_unavailable") from None

    @app.get("/uploads", response_class=HTMLResponse, include_in_schema=False)
    def page():
        return UPLOAD_HTML

    router = APIRouter(prefix="/api/v1/uploads")

    @router.get("/session")
    def session():
        return {"csrf_token": nonce}

    @router.get("/status")
    async def status():
        return await invoke("status")

    @router.post("/recover")
    async def recover():
        try:
            return await run_in_threadpool(manager.recover)
        except UploadError as exc:
            raise _safe_error(exc) from None

    @router.get("/accounts")
    async def accounts():
        return [_public(record, _ACCOUNT_FIELDS) for record in await invoke("accounts")]

    @router.post("/accounts", status_code=201)
    async def add_account(payload: AccountRequest):
        return _public(await invoke("add_account", **payload.model_dump()), _ACCOUNT_FIELDS)

    @router.post("/accounts/{account_id}/{action}", status_code=202)
    async def account_action(account_id: str, action: Literal["login", "check"]):
        return _public(await invoke("account_action", account_id, action=action), _OPERATION_FIELDS)

    @router.get("/operations")
    async def operations():
        return [_public(record, _OPERATION_FIELDS) for record in await invoke("operations")]

    @router.post("/operations/{operation_id}/cancel")
    async def cancel_operation(operation_id: str):
        return _public(await invoke("cancel_operation", operation_id), _OPERATION_FIELDS)

    @router.get("/operations/{operation_id}/qr")
    async def login_qr(operation_id: str):
        return Response(await invoke("login_qr", operation_id), media_type="image/png")

    @router.get("/sources")
    async def sources():
        return [_public(record, _SOURCE_FIELDS) for record in await invoke("sources")]

    @router.get("/sources/page")
    async def source_page(cursor: str | None = Query(default=None, max_length=64),
                          limit: int = Query(default=50, ge=1, le=200)):
        page = await invoke("source_page", cursor=cursor, limit=limit)
        return {"items": [_public(record, _SOURCE_FIELDS) for record in page["items"]],
                "next_cursor": page["next_cursor"]}

    @router.get("/sources/{source_id}")
    async def source(source_id: str):
        return _public(await invoke("source", source_id), _SOURCE_FIELDS)

    @router.post("/sources", status_code=201)
    async def import_source(request: Request, name: str = Query(min_length=1, max_length=180)):
        if any(character in name for character in ("/", "\\", ":", "\x00")) or name in {".", ".."}:
            raise HTTPException(status_code=422, detail="invalid_source_name")
        if Path(name).suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}:
            raise HTTPException(status_code=422, detail="unsupported_video_type")
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(status_code=415, detail="binary_source_required")
        lengths = request.headers.getlist("content-length")
        if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > MAX_SOURCE_BYTES)):
            raise HTTPException(status_code=413, detail="source_too_large")
        try:
            await run_in_threadpool(manager.get)
        except UploadError as exc:
            raise _safe_error(exc) from None
        incoming = root / "incoming"
        incoming.mkdir(exist_ok=True)
        if incoming.is_symlink() or incoming.resolve() != incoming:
            raise HTTPException(status_code=409, detail="source_storage_invalid")
        descriptor, temporary = tempfile.mkstemp(prefix="import-", suffix=Path(name).suffix, dir=incoming)
        source = Path(temporary)
        try:
            total = 0
            with os.fdopen(descriptor, "wb") as handle:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        raise HTTPException(status_code=413, detail="source_too_large")
                    for offset in range(0, len(chunk), 64 * 1024):
                        await run_in_threadpool(handle.write, memoryview(chunk)[offset:offset + 64 * 1024])
                if total == 0:
                    raise HTTPException(status_code=422, detail="source_empty")
                await run_in_threadpool(handle.flush)
                await run_in_threadpool(os.fsync, handle.fileno())
            result = await invoke("import_source", source, name)
            return _public(result, _SOURCE_FIELDS)
        finally:
            source.unlink(missing_ok=True)

    @router.post("/sources/assets/{asset_id}", status_code=201)
    async def import_asset(asset_id: str):
        if original_asset_resolver is None:
            raise HTTPException(status_code=404, detail="asset_not_found")
        path, expected_sha256 = await run_in_threadpool(original_asset_resolver, asset_id)
        result = await invoke("import_source", path, "download-" + asset_id + path.suffix,
                              expected_sha256=expected_sha256)
        return _public(result, _SOURCE_FIELDS)

    @router.get("/jobs")
    async def jobs():
        return [_public(record, _JOB_FIELDS) for record in await invoke("jobs")]

    @router.get("/jobs/page")
    async def job_page(cursor: str | None = Query(default=None, max_length=64),
                       limit: int = Query(default=50, ge=1, le=200)):
        page = await invoke("job_page", cursor=cursor, limit=limit)
        return {"items": [_public(record, _JOB_FIELDS) for record in page["items"]],
                "next_cursor": page["next_cursor"]}

    @router.get("/jobs/resolve")
    async def resolve_jobs(ids: str = Query(min_length=32, max_length=2111)):
        job_ids = ids.split(",")
        return [_public(record, _JOB_FIELDS) for record in await invoke("jobs_by_ids", job_ids)]

    @router.get("/jobs/{job_id}")
    async def job(job_id: str):
        return _public(await invoke("job", job_id), _JOB_FIELDS)

    @router.post("/jobs", status_code=201)
    async def create_jobs(payload: JobsRequest):
        return [_public(record, _JOB_FIELDS) for record in await invoke("create_jobs", **payload.model_dump())]

    @router.post("/jobs/{job_id}/confirm")
    async def confirm(job_id: str):
        return _public(await invoke("confirm", job_id), _JOB_FIELDS)

    @router.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        return _public(await invoke("cancel", job_id), _JOB_FIELDS)

    @router.post("/jobs/{job_id}/retry", status_code=201)
    async def retry(job_id: str, payload: RetryRequest):
        return _public(await invoke("retry", job_id, **payload.model_dump()), _JOB_FIELDS)

    app.include_router(router)

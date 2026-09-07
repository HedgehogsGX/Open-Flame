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
from typing import Annotated, Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from .contracts import UploadError
from ..ui_assets import page_content_security_policy
from .web import UPLOAD_HTML

MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_COVER_BYTES = 20 * 1024 * 1024


def _omit_inherited_default(schema: dict) -> None:
    """Describe an optional inherited field without advertising null as its value."""
    schema.pop("default", None)


def _omit_inherited_default_and_mark_unique(schema: dict) -> None:
    _omit_inherited_default(schema)
    schema["uniqueItems"] = True


Identifier = Annotated[
    str,
    Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
]
TagInput = Annotated[
    str,
    Field(
        min_length=1,
        max_length=20,
        description=(
            "New requests must omit commas and hash markers. The service performs "
            "that history-aware check so an exact pre-Schema-3 idempotent replay "
            "can still be recognized safely."
        ),
    ),
]
_ACCOUNT_FIELDS = (
    "id", "platform", "name", "auth_state", "code", "lifecycle_state",
    "disconnected_at",
)
_SOURCE_FIELDS = (
    "id", "name", "size", "sha256", "media_present", "media_state",
    "media_deleted_at", "active_reference_count", "can_delete",
)
_COVER_FIELDS = (
    "id", "name", "size", "sha256", "mime_type", "width", "height",
    "media_present", "media_state", "media_deleted_at", "active_reference_count",
    "can_delete",
)
_OPERATION_FIELDS = ("id", "account_id", "action", "state", "code", "login_phase", "qr_available", "qr_revision", "expires_at")
_JOB_FIELDS = (
    "id", "platform", "account_id", "account_name", "source_id", "title",
    "description", "tags", "category_id", "mode", "copyright", "source_credit",
    "state", "code", "created_at", "updated_at", "retry_of", "source_name",
    "source_size", "source_sha256", "account_lifecycle_state",
    "source_media_present", "source_media_state",
    "cover_landscape_asset_id", "cover_portrait_asset_id", "publish_at_unix",
    "publish_timezone_offset_minutes", "platform_options",
)


class AccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["bilibili", "douyin", "tencent"]
    name: str = Field(min_length=1, max_length=60)


class PlatformOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dynamic: str | None = Field(default=None, max_length=250)
    # These values inherit the service default when omitted.  Giving a
    # non-nullable annotation with a missing-field default keeps that wire
    # distinction in both validation and the generated OpenAPI schema:
    # omitted is accepted, while an explicit JSON null is not.
    no_reprint: bool = Field(
        default=None, strict=True, json_schema_extra=_omit_inherited_default,
    )
    close_comments: bool = Field(
        default=None, strict=True, json_schema_extra=_omit_inherited_default,
    )
    close_danmu: bool = Field(
        default=None, strict=True, json_schema_extra=_omit_inherited_default,
    )
    declaration: Literal[
        "内容由AI生成", "内容为转载信息", "内容为个人观点或见解",
    ] | None = None
    short_title: str | None = Field(default=None, min_length=7, max_length=15)
    content_label: Literal["含AI生成内容"] | None = None


class TargetOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 2})
    account_id: Identifier
    title: str = Field(
        default=None, min_length=1, max_length=100,
        json_schema_extra=_omit_inherited_default,
    )
    description: str = Field(
        default=None, max_length=2000,
        json_schema_extra=_omit_inherited_default,
    )
    tags: list[TagInput] = Field(
        default=None,
        max_length=10,
        json_schema_extra=_omit_inherited_default_and_mark_unique,
    )
    category_id: int = Field(
        default=None, ge=1, le=10000, strict=True,
        json_schema_extra=_omit_inherited_default,
    )
    mode: Literal["publish", "draft"] = Field(
        default=None, json_schema_extra=_omit_inherited_default,
    )
    copyright: int = Field(
        default=None, ge=1, le=2, strict=True,
        json_schema_extra=_omit_inherited_default,
    )
    source_credit: str = Field(
        default=None, max_length=200,
        json_schema_extra=_omit_inherited_default,
    )
    cover_landscape_asset_id: Identifier | None = None
    cover_portrait_asset_id: Identifier | None = None
    publish_at_unix: int | None = Field(
        default=None, ge=1_700_000_000, le=4_102_444_800, strict=True,
    )
    publish_timezone_offset_minutes: int | None = Field(
        default=None, ge=-840, le=840, strict=True,
    )
    platform_options: PlatformOptionsRequest | None = None

    @model_validator(mode="after")
    def require_an_override(self):
        if self.model_fields_set <= {"account_id"}:
            raise ValueError("at least one target override field is required")
        return self

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value):
        return _validated_tags(value)


class JobsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: Identifier
    account_ids: list[Identifier] = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    tags: list[TagInput] = Field(
        default_factory=list,
        max_length=10,
        json_schema_extra={"uniqueItems": True},
    )
    category_id: int | None = Field(
        default=None,
        ge=1,
        le=10000,
        strict=True,
        description="Legacy shared Bilibili default; ignored for non-Bilibili targets.",
    )
    mode: Literal["publish", "draft"] = "publish"
    copyright: int | None = Field(
        default=None,
        ge=1,
        le=2,
        strict=True,
        description="Legacy shared Bilibili default; ignored for non-Bilibili targets.",
    )
    source_credit: str = Field(
        default="",
        max_length=200,
        description="Legacy shared Bilibili repost source; ignored for non-Bilibili targets.",
    )
    target_overrides: list[TargetOverrideRequest] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value):
        return _validated_tags(value)


def _validated_tags(value: list[str]) -> list[str]:
    normalized = [tag.strip() for tag in value]
    if (
        any(not tag or any(ord(character) < 32 for character in tag) for tag in normalized)
        or len(normalized) != len(set(normalized))
    ):
        raise ValueError("tags must be non-empty, normalized, and unique")
    return normalized


class UploadPlatformOptionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dynamic: str | None = None
    no_reprint: bool | None = None
    close_comments: bool | None = None
    close_danmu: bool | None = None
    declaration: str | None = None
    short_title: str | None = None
    content_label: str | None = None


class UploadJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    platform: Literal["bilibili", "douyin", "tencent"]
    account_id: Identifier
    account_name: str
    source_id: Identifier
    title: str
    description: str
    tags: list[str]
    category_id: int | None
    mode: Literal["publish", "draft"]
    copyright: int
    source_credit: str
    state: str
    code: str
    created_at: str
    updated_at: str
    retry_of: Identifier | None
    source_name: str
    source_size: int
    source_sha256: str
    account_lifecycle_state: str
    source_media_present: bool
    source_media_state: str
    cover_landscape_asset_id: Identifier | None
    cover_portrait_asset_id: Identifier | None
    publish_at_unix: int | None
    publish_timezone_offset_minutes: int | None
    platform_options: UploadPlatformOptionsResponse


class UploadJobPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[UploadJobResponse]
    next_cursor: str | None


class UploadCoverResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    name: str
    size: int
    sha256: str
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    width: int
    height: int
    media_present: bool
    media_state: str
    media_deleted_at: str | None
    active_reference_count: int
    can_delete: bool


class UploadCoverPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[UploadCoverResponse]
    next_cursor: str | None


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_unknown: bool = Field(default=False, strict=True)


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

    async def run_guarded(work):
        try:
            return await run_in_threadpool(work)
        except UploadError as exc:
            raise _safe_error(exc) from None
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="upload_database_unavailable") from None

    async def invoke(method: str, *args, **kwargs):
        def work():
            return getattr(manager.get(), method)(*args, **kwargs)
        return await run_guarded(work)

    @app.get("/uploads", response_class=HTMLResponse, include_in_schema=False)
    def page():
        return HTMLResponse(
            UPLOAD_HTML,
            headers={
                "Content-Security-Policy": page_content_security_policy(UPLOAD_HTML),
            },
        )

    router = APIRouter(prefix="/api/v1/uploads")

    @router.get("/session")
    def session():
        return {"csrf_token": nonce}

    @router.get("/status")
    async def status():
        return await invoke("status")

    @router.post("/recover")
    async def recover():
        return await run_guarded(manager.recover)

    @router.get("/accounts")
    async def accounts():
        return [_public(record, _ACCOUNT_FIELDS) for record in await invoke("accounts")]

    @router.post("/accounts", status_code=201)
    async def add_account(payload: AccountRequest):
        return _public(await invoke("add_account", **payload.model_dump()), _ACCOUNT_FIELDS)

    @router.post("/accounts/{account_id}/disconnect")
    async def disconnect_account(account_id: str):
        result = await invoke("disconnect_account", account_id)
        return {
            **result,
            "account": _public(result["account"], _ACCOUNT_FIELDS),
        }

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

    @router.get("/storage")
    async def storage_usage():
        return await invoke("storage_usage")

    @router.get("/sources/page")
    async def source_page(cursor: str | None = Query(default=None, max_length=64),
                          limit: int = Query(default=50, ge=1, le=200)):
        page = await invoke("source_page", cursor=cursor, limit=limit)
        return {"items": [_public(record, _SOURCE_FIELDS) for record in page["items"]],
                "next_cursor": page["next_cursor"]}

    @router.get("/sources/{source_id}")
    async def source(source_id: str):
        return _public(await invoke("source", source_id), _SOURCE_FIELDS)

    @router.delete("/sources/{source_id}/media")
    async def delete_source_media(source_id: str):
        return _public(await invoke("delete_source_media", source_id), _SOURCE_FIELDS)

    @router.post("/sources/{source_id}/media")
    async def restore_source_media(source_id: str, request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(status_code=415, detail="binary_source_required")
        lengths = request.headers.getlist("content-length")
        if len(lengths) > 1 or (lengths and (
                not lengths[0].isdigit() or int(lengths[0]) > MAX_SOURCE_BYTES)):
            raise HTTPException(status_code=413, detail="source_too_large")
        await run_guarded(manager.get)
        incoming = root / "incoming"
        incoming.mkdir(exist_ok=True)
        if incoming.is_symlink() or incoming.resolve() != incoming:
            raise HTTPException(status_code=409, detail="source_storage_invalid")
        descriptor, temporary = tempfile.mkstemp(prefix="restore-", suffix=".media", dir=incoming)
        source = Path(temporary)
        try:
            total = 0
            with os.fdopen(descriptor, "wb") as handle:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        raise HTTPException(status_code=413, detail="source_too_large")
                    for offset in range(0, len(chunk), 64 * 1024):
                        await run_in_threadpool(
                            handle.write, memoryview(chunk)[offset:offset + 64 * 1024]
                        )
                if total == 0:
                    raise HTTPException(status_code=422, detail="source_empty")
                await run_in_threadpool(handle.flush)
                await run_in_threadpool(os.fsync, handle.fileno())
            result = await invoke("restore_source_media", source_id, source)
            return _public(result, _SOURCE_FIELDS)
        finally:
            source.unlink(missing_ok=True)

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
        await run_guarded(manager.get)
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

    @router.get(
        "/covers",
        response_model=list[UploadCoverResponse],
        deprecated=True,
        description="Compatibility view of at most the newest 200 covers; use /covers/page.",
    )
    async def covers():
        return [_public(record, _COVER_FIELDS) for record in await invoke("covers")]

    @router.get("/covers/page", response_model=UploadCoverPageResponse)
    async def cover_page(cursor: str | None = Query(default=None, max_length=64),
                         limit: int = Query(default=50, ge=1, le=200)):
        page = await invoke("cover_page", cursor=cursor, limit=limit)
        return {
            "items": [_public(record, _COVER_FIELDS) for record in page["items"]],
            "next_cursor": page["next_cursor"],
        }

    @router.get("/covers/resolve", response_model=list[UploadCoverResponse])
    async def resolve_covers(ids: str = Query(min_length=32, max_length=2111)):
        asset_ids = ids.split(",")
        return [
            _public(record, _COVER_FIELDS)
            for record in await invoke("covers_by_ids", asset_ids)
        ]

    @router.post("/covers", status_code=201, response_model=UploadCoverResponse)
    async def import_cover(request: Request, name: str = Query(min_length=1, max_length=180)):
        if any(character in name for character in ("/", "\\", ":", "\x00")) or name in {".", ".."}:
            raise HTTPException(status_code=422, detail="invalid_cover_name")
        if Path(name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise HTTPException(status_code=422, detail="unsupported_cover_type")
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(status_code=415, detail="binary_cover_required")
        lengths = request.headers.getlist("content-length")
        if len(lengths) > 1 or (lengths and (
                not lengths[0].isdigit() or int(lengths[0]) > MAX_COVER_BYTES)):
            raise HTTPException(status_code=413, detail="cover_too_large")
        await run_guarded(manager.get)
        incoming = root / "incoming"
        incoming.mkdir(exist_ok=True)
        if incoming.is_symlink() or incoming.resolve() != incoming:
            raise HTTPException(status_code=409, detail="cover_storage_invalid")
        descriptor, temporary = tempfile.mkstemp(
            prefix="cover-", suffix=Path(name).suffix.lower(), dir=incoming,
        )
        cover_path = Path(temporary)
        try:
            total = 0
            with os.fdopen(descriptor, "wb") as handle:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_COVER_BYTES:
                        raise HTTPException(status_code=413, detail="cover_too_large")
                    for offset in range(0, len(chunk), 64 * 1024):
                        await run_in_threadpool(
                            handle.write, memoryview(chunk)[offset:offset + 64 * 1024]
                        )
                if total == 0:
                    raise HTTPException(status_code=422, detail="cover_empty")
                await run_in_threadpool(handle.flush)
                await run_in_threadpool(os.fsync, handle.fileno())
            result = await invoke("import_cover", cover_path, name)
            return _public(result, _COVER_FIELDS)
        finally:
            cover_path.unlink(missing_ok=True)

    @router.get(
        "/covers/{asset_id}/content",
        response_class=Response,
        responses={
            200: {
                "description": "Managed cover bytes in the recorded image format.",
                "content": {
                    "image/jpeg": {},
                    "image/png": {},
                    "image/webp": {},
                },
            },
        },
    )
    async def cover_content(asset_id: str):
        payload, mime_type = await invoke("cover_content", asset_id)
        return Response(payload, media_type=mime_type)

    @router.delete("/covers/{asset_id}/media", response_model=UploadCoverResponse)
    async def delete_cover_media(asset_id: str):
        return _public(await invoke("delete_cover_media", asset_id), _COVER_FIELDS)

    @router.get("/covers/{asset_id}", response_model=UploadCoverResponse)
    async def cover(asset_id: str):
        return _public(await invoke("cover", asset_id), _COVER_FIELDS)

    @router.get(
        "/jobs",
        response_model=list[UploadJobResponse],
        response_model_exclude_unset=True,
    )
    async def jobs():
        return [_public(record, _JOB_FIELDS) for record in await invoke("jobs")]

    @router.get(
        "/jobs/page",
        response_model=UploadJobPageResponse,
        response_model_exclude_unset=True,
    )
    async def job_page(cursor: str | None = Query(default=None, max_length=64),
                       limit: int = Query(default=50, ge=1, le=200)):
        page = await invoke("job_page", cursor=cursor, limit=limit)
        return {"items": [_public(record, _JOB_FIELDS) for record in page["items"]],
                "next_cursor": page["next_cursor"]}

    @router.get(
        "/jobs/resolve",
        response_model=list[UploadJobResponse],
        response_model_exclude_unset=True,
    )
    async def resolve_jobs(ids: str = Query(min_length=32, max_length=2111)):
        job_ids = ids.split(",")
        return [_public(record, _JOB_FIELDS) for record in await invoke("jobs_by_ids", job_ids)]

    @router.get(
        "/jobs/{job_id}",
        response_model=UploadJobResponse,
        response_model_exclude_unset=True,
    )
    async def job(job_id: str):
        return _public(await invoke("job", job_id), _JOB_FIELDS)

    @router.post(
        "/jobs",
        status_code=201,
        response_model=list[UploadJobResponse],
        response_model_exclude_unset=True,
    )
    async def create_jobs(payload: JobsRequest):
        values = payload.model_dump()
        values["target_overrides"] = [
            override.model_dump(exclude_unset=True)
            for override in payload.target_overrides
        ]
        return [_public(record, _JOB_FIELDS) for record in await invoke("create_jobs", **values)]

    @router.post(
        "/jobs/{job_id}/confirm",
        response_model=UploadJobResponse,
        response_model_exclude_unset=True,
    )
    async def confirm(job_id: str):
        return _public(await invoke("confirm", job_id), _JOB_FIELDS)

    @router.post(
        "/jobs/{job_id}/cancel",
        response_model=UploadJobResponse,
        response_model_exclude_unset=True,
    )
    async def cancel(job_id: str):
        return _public(await invoke("cancel", job_id), _JOB_FIELDS)

    @router.post(
        "/jobs/{job_id}/retry",
        status_code=201,
        response_model=UploadJobResponse,
        response_model_exclude_unset=True,
    )
    async def retry(job_id: str, payload: RetryRequest):
        return _public(await invoke("retry", job_id, **payload.model_dump()), _JOB_FIELDS)

    app.include_router(router)

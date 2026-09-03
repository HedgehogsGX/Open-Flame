from __future__ import annotations

import os
import shutil
import stat
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import perf_counter
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .capabilities import DEFAULT_DOWNLOAD_CAPABILITIES
from .config import Settings
from .database import SCHEMA_VERSION, Database
from .domain import Platform
from .importers import MAX_IMPORT_BYTES, BatchImportError, parse_batch_file
from .observability import collect_metrics
from .repository import BatchRepository
from .runtime_logging import RuntimeLogConfig, RuntimeLogger, safe_exception_type
from .schemas import (
    BatchAssetResponse,
    BatchCreateRequest,
    BatchResponse,
    BatchSummaryResponse,
    DownloadCapabilityResponse,
    HealthResponse,
    InputCancelResponse,
    InputRediscoverResponse,
    JobCancelResponse,
    MetricsResponse,
    PlatformCircuitResponse,
    QueueControlResponse,
    RuntimeLogsResponse,
    ToolchainStatusResponse,
)
from .service import BatchService, BatchValidationError, ShortLinkResolver
from .short_link_transport import (
    ShortLinkTransportConfigurationError,
    UnixAttestedShortLinkTransport,
    load_shared_key,
)
from .short_links import ControlledShortLinkResolver
from .toolchain import inspect_toolchain
from .web import INDEX_HTML
from .worker_repository import (
    InvalidTransition,
    RediscoverConflict,
    WorkerRepository,
)

_BATCH_RESPONSE_FIELDS = (
    "id",
    "name",
    "status",
    "total_count",
    "queued_count",
    "failed_count",
    "duplicate_count",
    "ready_count",
    "canceled_count",
    "partial_success_count",
    "created_at",
    "updated_at",
)
_INPUT_RESPONSE_FIELDS = (
    "id",
    "batch_id",
    "raw_text",
    "submitted_url",
    "canonical_url",
    "platform",
    "source_type",
    "source_id",
    "ordinal",
    "expected_item_count",
    "active_discovery_id",
    "active_run_generation",
    "cancel_requested_at",
    "status",
    "error_code",
    "error_message",
    "duplicate_of_input_record_id",
    "created_at",
)
_JOB_RESPONSE_FIELDS = (
    "id",
    "batch_id",
    "input_record_id",
    "source_item_id",
    "job_kind",
    "status",
    "progress",
    "final_error_code",
    "route_policy_version",
    "available_at",
    "cancel_requested_at",
    "attempt_count",
    "run_generation",
    "generation_attempt_count",
    "reused_from_job_id",
    "created_at",
    "updated_at",
    "platform",
    "source_type",
    "canonical_url",
)

_TOOLCHAIN_SECURITY_NOTE = (
    "本机工具链 ready 只表示固定工具通过离线检查；本机直连 Worker 需要"
    "单独显式启动，控制面不推断其进程在线状态；隔离运行环境与平台级能力"
    "仍未验证。redistribution_status 只描述本机第三方工具包，不描述项目源码"
    "的 Apache-2.0 许可状态。"
)
_TOOLCHAIN_LOG_VERSION_FIELDS = (
    "yt_dlp_version",
    "ffmpeg_version",
    "ffprobe_version",
)


def _toolchain_log_fields(payload: dict[str, object]) -> dict[str, object]:
    """Select the only toolchain values permitted at the log boundary."""

    fields = {
        "state": payload["state"],
        "detail_code": payload["detail_code"],
        "offline_smoke_passed": payload["offline_smoke_passed"],
    }
    fields.update(
        {
            name: payload[name]
            for name in _TOOLCHAIN_LOG_VERSION_FIELDS
            if payload.get(name) is not None
        }
    )
    return fields


def _public_batch_response(payload: dict) -> BatchResponse:
    """Build the API DTO from an explicit allow-list, never from ``SELECT *``."""

    return BatchResponse.model_validate(
        {
            **{field: payload[field] for field in _BATCH_RESPONSE_FIELDS},
            "inputs": [
                {field: item[field] for field in _INPUT_RESPONSE_FIELDS}
                for item in payload["inputs"]
            ],
            "jobs": [
                {field: job[field] for field in _JOB_RESPONSE_FIELDS}
                for job in payload["jobs"]
            ],
        }
    )


def _canonical_asset_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("asset id is invalid")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("asset id is invalid") from exc
    if canonical != value:
        raise ValueError("asset id is invalid")
    return canonical


def _registered_original_file(
    data_root: Path,
    *,
    asset_id: str,
    relative_path: object,
    expected_size: object,
) -> tuple[Path, os.stat_result]:
    """Resolve one immutable AssetStore original without trusting DB path text."""

    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path) > 4096
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("registered original path is invalid")
    stored = PurePosixPath(relative_path)
    if (
        stored.is_absolute()
        or stored.as_posix() != relative_path
        or len(stored.parts) != 4
        or stored.parts[:3] != ("assets", asset_id, "original")
        or any(part in {"", ".", ".."} for part in stored.parts)
    ):
        raise ValueError("registered original path is invalid")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        raise ValueError("registered original size is invalid")

    root = data_root.resolve(strict=True)
    current = root
    final_info: os.stat_result | None = None
    for index, component in enumerate(stored.parts):
        current /= component
        info = current.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or (reparse and attributes & reparse):
            raise ValueError("registered original path contains a link")
        if index < len(stored.parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("registered original parent is not a directory")
        else:
            final_info = info

    assert final_info is not None
    if not stat.S_ISREG(final_info.st_mode) or final_info.st_nlink != 1:
        raise ValueError("registered original is not a plain single-link file")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root) or final_info.st_size != expected_size:
        raise ValueError("registered original does not match its asset record")
    return resolved, final_info


def _configured_short_link_resolver(
    settings: Settings,
) -> ControlledShortLinkResolver | None:
    if not settings.short_link_resolution_enabled:
        return None
    socket_path = settings.short_link_transport_socket
    key_path = settings.short_link_attestation_key_file
    if socket_path is None or key_path is None:
        raise ValueError("short-link security configuration is incomplete")
    configuration_invalid = False
    try:
        if os.name != "posix":
            raise ShortLinkTransportConfigurationError(
                "short-link Unix transport requires POSIX security semantics"
            )
        shared_key = load_shared_key(key_path)
        transport = UnixAttestedShortLinkTransport(
            shared_key=shared_key,
            unix_socket=socket_path,
        )
    except (ShortLinkTransportConfigurationError, OSError, ValueError):
        configuration_invalid = True
    if configuration_invalid:
        # Raise outside the exception handler so neither ``__context__`` nor
        # ``__cause__`` can retain a key/socket path or lower-level diagnostic.
        raise ValueError("short-link security configuration is invalid")
    return ControlledShortLinkResolver(transport=transport)


def create_app(
    settings: Settings | None = None,
    *,
    short_link_resolver: ShortLinkResolver | None = None,
    runtime_logger: RuntimeLogger | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_settings.validate_startup_security()
    active_logger = runtime_logger or RuntimeLogger(
        component="control",
        config=RuntimeLogConfig(
            directory=resolved_settings.data_root / "logs",
            level=resolved_settings.runtime_log_level,
            max_bytes=resolved_settings.runtime_log_max_bytes,
            backup_count=resolved_settings.runtime_log_backup_count,
        ),
    )
    active_logger.emit(
        "control.initializing",
        app_version=__version__,
        app_schema_version=SCHEMA_VERSION,
        x_graph_v2_enabled=resolved_settings.x_graph_v2_enabled,
        short_link_resolution_enabled=(resolved_settings.short_link_resolution_enabled),
    )
    try:
        if (
            short_link_resolver is not None
            and not resolved_settings.short_link_resolution_enabled
        ):
            raise ValueError("short-link resolver requires explicit feature enablement")
        if short_link_resolver is None:
            short_link_resolver = _configured_short_link_resolver(resolved_settings)
        resolved_settings.data_root.mkdir(parents=True, exist_ok=True)
        database = Database(resolved_settings.database_path)
        database.initialize()
        toolchain_payload = dict(
            inspect_toolchain(resolved_settings.tool_root).to_dict()
        )
        # Local binaries and an offline smoke test never establish either the
        # isolated Worker runtime or real-platform behaviour.
        toolchain_payload["isolated_worker_ready"] = False
        toolchain_payload["platform_download_verified"] = False
        toolchain_payload["network_download_enabled"] = False
        toolchain_payload["local_direct_worker_available"] = bool(
            os.name == "nt" and toolchain_payload.get("state") == "ready"
        )
        toolchain_payload["security_note"] = _TOOLCHAIN_SECURITY_NOTE
        cached_toolchain_status = ToolchainStatusResponse.model_validate(
            toolchain_payload
        )
        active_logger.emit(
            "toolchain.inspected",
            **_toolchain_log_fields(cached_toolchain_status.model_dump()),
        )
    except Exception as exc:
        active_logger.emit(
            "control.startup_failed",
            level="ERROR",
            exception_type=safe_exception_type(exc),
        )
        raise
    repository = BatchRepository(database)
    service = BatchService(
        repository=repository,
        max_batch_urls=resolved_settings.max_batch_urls,
        route_policy_version=resolved_settings.route_policy_version,
        x_graph_v2_enabled=resolved_settings.x_graph_v2_enabled,
        short_link_resolver=short_link_resolver,
    )
    worker_repository = WorkerRepository(database)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        active_logger.emit(
            "control.started",
            app_version=__version__,
            app_schema_version=SCHEMA_VERSION,
            port=resolved_settings.port,
        )
        try:
            yield
        finally:
            active_logger.emit("control.stopped")

    app = FastAPI(
        title="Video Download Control",
        version=__version__,
        description="Private control plane for auditable media download jobs",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.batch_service = service
    app.state.worker_repository = worker_repository
    app.state.runtime_logger = active_logger
    app.state.toolchain_status = cached_toolchain_status
    app.state.download_capabilities = DEFAULT_DOWNLOAD_CAPABILITIES

    @app.middleware("http")
    async def runtime_log_middleware(request: Request, call_next):
        request_id = uuid4().hex
        request.state.runtime_request_id = request_id
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - sanitized final safety boundary
            route = _request_route_template(request)
            active_logger.emit(
                "http.request_failed",
                level="ERROR",
                request_id=request_id,
                method=request.method,
                route=route,
                status_code=500,
                duration_ms=(perf_counter() - started) * 1000,
                exception_type=safe_exception_type(exc),
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "内部错误", "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )
        response.headers["X-Request-ID"] = request_id
        route = _request_route_template(request)
        if response.status_code >= 500:
            level = "ERROR"
        elif response.status_code >= 400:
            level = "WARNING"
        elif route in {
            "/health/live",
            "/api/v1/operations/logs",
            "/api/v1/operations/queue",
            "/api/v1/operations/tools",
            "/api/v1/download-capabilities",
            "/api/v1/platform-circuits",
        }:
            level = "DEBUG"
        else:
            level = "INFO"
        active_logger.emit(
            "http.request_completed",
            level=level,
            request_id=request_id,
            method=request.method,
            route=route,
            status_code=response.status_code,
            duration_ms=(perf_counter() - started) * 1000,
        )
        return response

    def health_payload() -> tuple[HealthResponse, bool]:
        database_ok, detail = database.readiness()
        queue_paused = False
        queue_reason: object | None = None
        if database_ok:
            queue_control = worker_repository.get_queue_control()
            queue_paused = bool(queue_control["paused"])
            queue_reason = queue_control["reason"]
        operational = database_ok and not queue_paused
        return (
            HealthResponse(
                status="ok" if operational else "degraded",
                database="ok" if database_ok else "error",
                schema_version=SCHEMA_VERSION,
                worker="paused" if queue_paused else "external_status_unknown",
                detail=(
                    f"queue_paused:{queue_reason}"
                    if queue_paused
                    else (None if database_ok else detail)
                ),
            ),
            operational,
        )

    @app.exception_handler(BatchValidationError)
    async def batch_validation_handler(
        request: Request, exc: BatchValidationError
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(BatchImportError)
    async def batch_import_handler(
        request: Request, exc: BatchImportError
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(exc)},
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return INDEX_HTML

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        payload, _ = health_payload()
        return payload

    @app.get("/health/live", include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=HealthResponse, include_in_schema=False)
    def readiness() -> HealthResponse:
        payload, operational = health_payload()
        if not operational:
            raise HTTPException(status_code=503, detail=payload.model_dump())
        return payload

    @app.post(
        "/api/v1/batches",
        response_model=BatchResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_batch(payload: BatchCreateRequest, request: Request) -> BatchResponse:
        created = service.create_batch(
            name=payload.name,
            raw_inputs=payload.inputs,
        )
        active_logger.emit(
            "batch.created",
            request_id=request.state.runtime_request_id,
            batch_id=str(created["id"]),
            total_count=int(created["total_count"]),
            queued_count=int(created["queued_count"]),
            failed_count=int(created["failed_count"]),
            duplicate_count=int(created["duplicate_count"]),
        )
        return _public_batch_response(created)

    @app.post(
        "/api/v1/batches/import",
        response_model=BatchResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_batch(
        request: Request,
        filename: str = Query(min_length=1, max_length=255),
        name: str | None = Query(default=None, max_length=200),
    ) -> BatchResponse:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type not in {
            "text/plain",
            "text/csv",
            "application/csv",
            "application/octet-stream",
        }:
            raise HTTPException(
                status_code=415,
                detail="导入接口仅接受纯文本或 CSV 请求体",
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_IMPORT_BYTES:
                raise HTTPException(status_code=413, detail="导入文件过大")
        raw_inputs = parse_batch_file(bytes(body), filename=filename)
        created = service.create_batch(
            name=name,
            raw_inputs=raw_inputs,
        )
        active_logger.emit(
            "batch.imported",
            request_id=request.state.runtime_request_id,
            batch_id=str(created["id"]),
            total_count=int(created["total_count"]),
            queued_count=int(created["queued_count"]),
            failed_count=int(created["failed_count"]),
            duplicate_count=int(created["duplicate_count"]),
        )
        return _public_batch_response(created)

    @app.get("/api/v1/batches/{batch_id}", response_model=BatchResponse)
    def get_batch(batch_id: str) -> BatchResponse:
        batch = service.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        return _public_batch_response(batch)

    @app.get(
        "/api/v1/batches/{batch_id}/assets",
        response_model=list[BatchAssetResponse],
    )
    def list_batch_assets(batch_id: str) -> list[BatchAssetResponse]:
        records = service.list_ready_assets_for_batch(batch_id)
        if records is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        response: list[BatchAssetResponse] = []
        for record in records:
            asset_id = _canonical_asset_id(record["asset_id"])
            response.append(
                BatchAssetResponse.model_validate(
                    {
                        "asset_id": asset_id,
                        "job_id": record["job_id"],
                        "ordinal": record["ordinal"],
                        "original": {
                            "media_kind": record["media_kind"],
                            "duration_seconds": record["duration_seconds"],
                            "container": record["container"],
                            "codec": record["codec"],
                            "width": record["width"],
                            "height": record["height"],
                            "size_bytes": record["size_bytes"],
                            "sha256": record["sha256"],
                        },
                        "download_url": f"/api/v1/assets/{asset_id}/download",
                    }
                )
            )
        return response

    @app.get(
        "/api/v1/assets/{asset_id}/download",
        response_class=FileResponse,
    )
    def download_asset(asset_id: str) -> FileResponse:
        try:
            canonical_id = _canonical_asset_id(asset_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="成品不存在") from None
        registered = service.get_ready_original_asset(canonical_id)
        if registered is None:
            raise HTTPException(status_code=404, detail="成品不存在")
        try:
            path, file_info = _registered_original_file(
                resolved_settings.data_root,
                asset_id=canonical_id,
                relative_path=registered["original_path"],
                expected_size=registered["size_bytes"],
            )
        except (OSError, ValueError):
            raise HTTPException(status_code=409, detail="成品文件不可用") from None
        suffix = path.suffix.lower()
        if not suffix.startswith(".") or not suffix[1:].isalnum() or len(suffix) > 12:
            suffix = ""
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=f"asset-{canonical_id}{suffix}",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
            stat_result=file_info,
        )

    @app.get("/api/v1/batches", response_model=list[BatchSummaryResponse])
    def list_batches(
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[BatchSummaryResponse]:
        return [
            BatchSummaryResponse.model_validate(item)
            for item in service.list_batches(limit=limit)
        ]

    @app.get("/api/v1/metrics", response_model=MetricsResponse)
    def metrics() -> MetricsResponse:
        return MetricsResponse.model_validate(
            collect_metrics(database, resolved_settings.data_root)
        )

    @app.get(
        "/api/v1/operations/queue",
        response_model=QueueControlResponse,
    )
    def queue_control() -> QueueControlResponse:
        return QueueControlResponse.model_validate(
            worker_repository.get_queue_control()
        )

    @app.get(
        "/api/v1/operations/logs",
        response_model=RuntimeLogsResponse,
    )
    def runtime_logs(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> RuntimeLogsResponse:
        log_status = active_logger.status()
        return RuntimeLogsResponse.model_validate(
            {
                **log_status,
                "events": active_logger.recent_events(limit=limit),
            }
        )

    @app.get(
        "/api/v1/operations/tools",
        response_model=ToolchainStatusResponse,
    )
    def toolchain_status() -> ToolchainStatusResponse:
        return cached_toolchain_status

    @app.get(
        "/api/v1/download-capabilities",
        response_model=list[DownloadCapabilityResponse],
    )
    def download_capabilities() -> list[DownloadCapabilityResponse]:
        return [
            DownloadCapabilityResponse.model_validate(item.to_public_dict())
            for item in DEFAULT_DOWNLOAD_CAPABILITIES.list(adapter="yt_dlp")
        ]

    @app.post(
        "/api/v1/operations/queue/resume",
        response_model=QueueControlResponse,
    )
    def resume_queue(request: Request) -> QueueControlResponse:
        disk = shutil.disk_usage(resolved_settings.data_root)
        if disk.free < resolved_settings.storage_min_free_bytes:
            raise HTTPException(
                status_code=409,
                detail="磁盘剩余空间仍低于配置水位，队列保持暂停",
            )
        resumed = worker_repository.resume_queue(now=datetime.now(UTC))
        active_logger.emit(
            "queue.resumed",
            request_id=request.state.runtime_request_id,
            queue_paused=bool(resumed["paused"]),
        )
        return QueueControlResponse.model_validate(resumed)

    @app.get(
        "/api/v1/platform-circuits",
        response_model=list[PlatformCircuitResponse],
    )
    def platform_circuits() -> list[PlatformCircuitResponse]:
        return [
            PlatformCircuitResponse.model_validate(item)
            for item in worker_repository.list_platform_circuits()
        ]

    @app.post(
        "/api/v1/platform-circuits/{platform}/reset",
        response_model=PlatformCircuitResponse,
    )
    def reset_platform_circuit(
        platform: Platform, request: Request
    ) -> PlatformCircuitResponse:
        reset = worker_repository.reset_platform_circuit(
            platform=platform, now=datetime.now(UTC)
        )
        active_logger.emit(
            "circuit.reset",
            request_id=request.state.runtime_request_id,
            platform=platform.value,
        )
        return PlatformCircuitResponse.model_validate(reset)

    @app.post(
        "/api/v1/jobs/{job_id}/cancel",
        response_model=JobCancelResponse,
    )
    def cancel_job(job_id: str, request: Request) -> JobCancelResponse:
        job_status = worker_repository.request_cancel(job_id, now=datetime.now(UTC))
        if job_status is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        active_logger.emit(
            "job.cancel_requested",
            request_id=request.state.runtime_request_id,
            job_id=job_id,
            result_status=job_status.value,
        )
        return JobCancelResponse(
            job_id=job_id,
            status=job_status,
            cancel_requested=True,
        )

    @app.post(
        "/api/v1/inputs/{input_id}/cancel",
        response_model=InputCancelResponse,
    )
    def cancel_input(input_id: str, request: Request) -> InputCancelResponse:
        input_status = worker_repository.request_cancel_input(
            input_id, now=datetime.now(UTC)
        )
        if input_status is None:
            raise HTTPException(status_code=404, detail="输入不存在")
        active_logger.emit(
            "input.cancel_requested",
            request_id=request.state.runtime_request_id,
            input_id=input_id,
            result_status=input_status.value,
        )
        return InputCancelResponse(
            input_record_id=input_id,
            status=input_status,
            cancel_requested=True,
        )

    @app.post(
        "/api/v1/inputs/{input_id}/rediscover",
        response_model=InputRediscoverResponse,
    )
    def rediscover_input(input_id: str, request: Request) -> InputRediscoverResponse:
        control = worker_repository.get_input_control(input_id)
        if control is None:
            raise HTTPException(status_code=404, detail="输入不存在")
        if not control["is_graph_v2"]:
            raise HTTPException(status_code=409, detail="输入不是 graph-v2")
        try:
            run_generation = worker_repository.request_rediscover(
                input_id, now=datetime.now(UTC)
            )
        except (InvalidTransition, RediscoverConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        active_logger.emit(
            "input.rediscover_requested",
            request_id=request.state.runtime_request_id,
            input_id=input_id,
            result_status="queued",
            run_generation=run_generation,
        )
        return InputRediscoverResponse(
            input_record_id=input_id,
            status="queued",
            run_generation=run_generation,
        )

    return app


def _request_route_template(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    return template if isinstance(template, str) else "/unmatched"

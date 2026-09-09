from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from secrets import token_hex
from time import perf_counter
from typing import BinaryIO, Iterator
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.types import Receive, Scope, Send

from . import __version__
from .assets import (
    CAPTION_MIME_TYPES,
    DEFAULT_MAX_AUXILIARY_FILE_BYTES,
    THUMBNAIL_MIME_TYPES,
)
from .build_identity import (
    ProductBuildDriftError,
    ProductBuildUnavailableError,
    current_product_identity,
)
from .capabilities import DEFAULT_DOWNLOAD_CAPABILITIES
from .capability_evidence import (
    CapabilityEvidenceRepository,
    CapabilityGovernanceError,
    list_registered_implementations,
)
from .config import Settings
from .credential_defaults import (
    CredentialDefaults,
    CredentialDefaultsError,
    CredentialMode,
    revalidate_credential_defaults,
    resolve_default_profile_locked,
)
from .database import SCHEMA_VERSION, Database
from .domain import ErrorCode, Platform
from .editing.api import install_editing_routes
from .editing.contracts import EditingError
from .editing.media import MediaProcessor as EditingMediaProcessor
from .importers import MAX_IMPORT_BYTES, BatchImportError, parse_batch_file
from .local_http_guard import install_local_http_guard
from .local_short_links import LocalDirectShortLinkTransport
from .observability import collect_metrics
from .repository import BatchRepository
from .runtime_logging import RuntimeLogConfig, RuntimeLogger, safe_exception_type
from .schemas import (
    BatchAssetResponse,
    BatchCreateRequest,
    BatchResponse,
    BatchSummaryResponse,
    CapabilityDecisionResponse,
    CapabilityEvidenceResponse,
    CapabilityImplementationResponse,
    CapabilitySnapshotResponse,
    CredentialDefaultsResponse,
    DownloadCapabilityResponse,
    HealthResponse,
    InputCancelResponse,
    InputRediscoverResponse,
    JobCancelResponse,
    JobRetryRequest,
    JobRetryResponse,
    MetricsResponse,
    PlatformCircuitResponse,
    QueueControlResponse,
    RuntimeLogsResponse,
    ToolchainStatusResponse,
    WorkerRuntimeStatusResponse,
)
from .service import BatchService, BatchValidationError, ShortLinkResolver
from .short_link_transport import (
    ShortLinkTransportConfigurationError,
    UnixAttestedShortLinkTransport,
    load_shared_key,
)
from .short_links import ControlledShortLinkResolver
from .toolchain import inspect_toolchain
from .ui_assets import page_content_security_policy, ui_asset
from .uploads.activity_lock import UploadActivityLease
from .uploads.api import install_upload_routes
from .uploads.service import default_upload_root
from .web import INDEX_HTML
from .workflows.api import install_workflow_routes
from .workflows.local_adapter import LocalWorkflowAdapter
from .workflows.manager import WorkflowManager
from .workflows.service import default_workflow_root
from .worker_runtime_status import ManagedWorkerRuntimeStatus, unknown_runtime_status
from .worker_repository import (
    CircuitResetConflict,
    InvalidTransition,
    RediscoverConflict,
    RetryConflict,
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
    "本机工具链 ready 只表示固定工具通过离线检查；本机托管 Worker 的"
    "运行状态另由应用心跳报告，外部 Worker 状态保持未知；隔离运行环境与平台级能力"
    "仍未验证。redistribution_status 只描述本机第三方工具包，不描述项目源码"
    "的 Apache-2.0 许可状态。"
)

_DELEGATED_API_PREFIXES = (
    "/api/v1/edits",
    "/api/v1/uploads",
    "/api/v1/workflows",
)


def _download_http_path(path: str) -> bool:
    if path == "/":
        return True
    if not path.startswith("/api/v1/"):
        return False
    return not any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _DELEGATED_API_PREFIXES
    )


_TOOLCHAIN_LOG_VERSION_FIELDS = (
    "yt_dlp_version",
    "ffmpeg_version",
    "ffprobe_version",
)
_VERIFIED_STREAM_CHUNK_BYTES = 64 * 1024
_AUXILIARY_SPOOL_MEMORY_BYTES = 1024 * 1024
_ORIGINAL_SPOOL_MEMORY_BYTES = 1024 * 1024
_CLIENT_CLOSED_REQUEST_STATUS = 499


class _OriginalSnapshotCancelled(Exception):
    """Internal cooperative stop between bounded source/spool operations."""


def _raise_if_snapshot_cancelled(cancellation: threading.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise _OriginalSnapshotCancelled


def _close_binary_handle(handle: BinaryIO) -> None:
    with suppress(Exception):
        handle.close()


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


def _public_auxiliary_artifact(record: object) -> dict[str, str | None] | None:
    """Fail closed per sidecar so one legacy row cannot hide valid originals."""

    if not isinstance(record, dict):
        return None
    try:
        artifact_id = _canonical_asset_id(record.get("artifact_id"))
    except ValueError:
        return None
    kind = record.get("kind")
    mime_type = record.get("mime_type")
    language = record.get("language")
    sha256 = record.get("sha256")
    if kind == "thumbnail":
        if mime_type not in THUMBNAIL_MIME_TYPES.values() or language is not None:
            return None
    elif kind == "caption":
        if mime_type not in CAPTION_MIME_TYPES.values() or not isinstance(language, str):
            return None
        if re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language) is None:
            return None
    else:
        return None
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        return None
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "mime_type": mime_type,
        "language": language,
        "sha256": sha256,
        "download_url": f"/api/v1/artifacts/{artifact_id}/download",
    }


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


def _verified_original_payload(
    path: Path,
    *,
    expected_info: os.stat_result,
    expected_sha256: object,
    cancellation: threading.Event | None = None,
) -> BinaryIO:
    """Copy one verified original into a stable, bounded-memory response spool."""

    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ValueError("registered original hash is invalid")
    _raise_if_snapshot_cancelled(cancellation)
    spool: BinaryIO = tempfile.SpooledTemporaryFile(
        max_size=_ORIGINAL_SPOOL_MEMORY_BYTES,
        mode="w+b",
    )
    try:
        digest = hashlib.sha256()
        total_bytes = 0
        with path.open("rb") as handle:
            _raise_if_snapshot_cancelled(cancellation)
            opened_info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_nlink != 1
                or opened_info.st_dev != expected_info.st_dev
                or opened_info.st_ino != expected_info.st_ino
                or opened_info.st_size != expected_info.st_size
            ):
                raise ValueError("registered original changed before reading")
            while True:
                _raise_if_snapshot_cancelled(cancellation)
                chunk = handle.read(_VERIFIED_STREAM_CHUNK_BYTES)
                _raise_if_snapshot_cancelled(cancellation)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > expected_info.st_size:
                    raise ValueError("registered original content is invalid")
                digest.update(chunk)
                spool.write(chunk)
                _raise_if_snapshot_cancelled(cancellation)
            final_opened_info = os.fstat(handle.fileno())
        if (
            total_bytes != expected_info.st_size
            or final_opened_info.st_dev != opened_info.st_dev
            or final_opened_info.st_ino != opened_info.st_ino
            or final_opened_info.st_size != opened_info.st_size
            or final_opened_info.st_mtime_ns != opened_info.st_mtime_ns
            or final_opened_info.st_ctime_ns != opened_info.st_ctime_ns
            or digest.hexdigest() != expected_sha256
        ):
            raise ValueError("registered original content is invalid")
        _raise_if_snapshot_cancelled(cancellation)
        spool.flush()
        spool.seek(0)
        _raise_if_snapshot_cancelled(cancellation)
        return spool
    except BaseException:
        _close_binary_handle(spool)
        raise


class _VerifiedOriginalFileResponse(FileResponse):
    """Serve a verified open handle without reopening its filesystem path."""

    def __init__(
        self,
        handle: BinaryIO,
        *,
        file_info: os.stat_result,
        filename: str | None,
        expected_sha256: str,
        media_type: str = "application/octet-stream",
        content_disposition_type: str = "attachment",
        verified_chunk_sha256: tuple[bytes, ...] | None = None,
        verification_chunk_size: int = _VERIFIED_STREAM_CHUNK_BYTES,
    ) -> None:
        self._verified_handle = handle
        self._size_bytes = int(file_info.st_size)
        self._verified_chunk_sha256 = verified_chunk_sha256
        self._verification_chunk_size = verification_chunk_size
        try:
            if verified_chunk_sha256 is not None:
                if verification_chunk_size < 1:
                    raise ValueError("verified chunk manifest is invalid")
                expected_chunks = (
                    self._size_bytes + verification_chunk_size - 1
                ) // verification_chunk_size
                if (
                    len(verified_chunk_sha256) != expected_chunks
                    or any(len(item) != 32 for item in verified_chunk_sha256)
                ):
                    raise ValueError("verified chunk manifest is invalid")
            super().__init__(
                "<verified-original>",
                media_type=media_type,
                filename=filename,
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "ETag": f'"{expected_sha256}"',
                },
                stat_result=file_info,
                content_disposition_type=content_disposition_type,
            )
        except BaseException:
            handle.close()
            raise

    def _read_verified_chunk(self, index: int) -> bytes:
        assert self._verified_chunk_sha256 is not None
        start = index * self._verification_chunk_size
        expected_size = min(
            self._verification_chunk_size,
            self._size_bytes - start,
        )
        self._verified_handle.seek(start)
        remaining = expected_size
        chunks: list[bytes] = []
        while remaining:
            chunk = self._verified_handle.read(remaining)
            if not chunk:
                raise RuntimeError("verified original handle ended unexpectedly")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).digest() != self._verified_chunk_sha256[index]:
            raise RuntimeError("verified original chunk changed before response")
        return payload

    async def _send_span(
        self,
        send: Send,
        start: int,
        end: int,
        *,
        more_after: bool,
    ) -> None:
        if self._verified_chunk_sha256 is not None:
            position = start
            if position == end:
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": more_after,
                    }
                )
                return
            first = start // self._verification_chunk_size
            last = (end - 1) // self._verification_chunk_size
            for index in range(first, last + 1):
                whole = await run_in_threadpool(self._read_verified_chunk, index)
                chunk_start = index * self._verification_chunk_size
                lower = max(start - chunk_start, 0)
                upper = min(end - chunk_start, len(whole))
                payload = whole[lower:upper]
                position += len(payload)
                await send(
                    {
                        "type": "http.response.body",
                        "body": payload,
                        "more_body": position < end or more_after,
                    }
                )
            return
        await run_in_threadpool(self._verified_handle.seek, start)
        position = start
        if position == end:
            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": more_after,
                }
            )
            return
        while position < end:
            chunk = await run_in_threadpool(
                self._verified_handle.read,
                min(self.chunk_size, end - position),
            )
            if not chunk:
                raise RuntimeError("verified original handle ended unexpectedly")
            position += len(chunk)
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": position < end or more_after,
                }
            )

    async def _handle_simple(
        self,
        send: Send,
        send_header_only: bool,
        _send_pathsend: bool,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        await self._send_span(
            send,
            0,
            self._size_bytes,
            more_after=False,
        )

    async def _handle_single_range(
        self,
        send: Send,
        start: int,
        end: int,
        file_size: int,
        send_header_only: bool,
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        await self._send_span(send, start, end, more_after=False)

    async def _handle_multiple_ranges(
        self,
        send: Send,
        ranges: list[tuple[int, int]],
        file_size: int,
        send_header_only: bool,
    ) -> None:
        boundary = token_hex(13)
        content_length, header_generator = self.generate_multipart(
            ranges,
            boundary,
            file_size,
            self.headers["content-type"],
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        for start, end in ranges:
            await send(
                {
                    "type": "http.response.body",
                    "body": header_generator(start, end),
                    "more_body": True,
                }
            )
            await self._send_span(send, start, end, more_after=True)
            await send(
                {"type": "http.response.body", "body": b"\r\n", "more_body": True}
            )
        await send(
            {
                "type": "http.response.body",
                "body": f"--{boundary}--".encode("latin-1"),
                "more_body": False,
            }
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _close_binary_handle(self._verified_handle)


async def _wait_for_http_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


async def _cancel_and_join(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


async def _close_unclaimed_snapshot(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        if task.done():
            orphan = task.result()
        else:
            orphan = await asyncio.shield(task)
    except (asyncio.CancelledError, Exception):
        return
    _close_binary_handle(orphan)


class _VerifiedOriginalSnapshotResponse(Response):
    """Verify to a stable spool while cooperatively observing disconnects."""

    media_type = "application/octet-stream"

    def __init__(
        self,
        path: Path,
        *,
        file_info: os.stat_result,
        filename: str | None,
        expected_sha256: str,
        media_type: str = "application/octet-stream",
        content_disposition_type: str = "attachment",
        error_detail: str = "成品文件不可用",
    ) -> None:
        super().__init__(content=None, media_type=media_type)
        self._source_path = path
        self._file_info = file_info
        self._filename = filename
        self._expected_sha256 = expected_sha256
        self._response_media_type = media_type
        self._content_disposition_type = content_disposition_type
        self._error_detail = error_detail

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        cancellation = threading.Event()
        snapshot_task = asyncio.create_task(
            run_in_threadpool(
                _verified_original_payload,
                self._source_path,
                expected_info=self._file_info,
                expected_sha256=self._expected_sha256,
                cancellation=cancellation,
            )
        )
        disconnect_task = asyncio.create_task(_wait_for_http_disconnect(receive))
        send_task: asyncio.Task | None = None
        payload: BinaryIO | None = None
        snapshot_claimed = False
        try:
            done, _ = await asyncio.wait(
                {snapshot_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                cancellation.set()
                try:
                    payload = await snapshot_task
                    snapshot_claimed = True
                except (_OriginalSnapshotCancelled, OSError, ValueError):
                    snapshot_claimed = True
                await Response(status_code=_CLIENT_CLOSED_REQUEST_STATUS)(
                    scope,
                    receive,
                    send,
                )
                return
            try:
                payload = await snapshot_task
                snapshot_claimed = True
            except (_OriginalSnapshotCancelled, OSError, ValueError):
                snapshot_claimed = True
                await _cancel_and_join(disconnect_task)
                await JSONResponse(
                    status_code=409,
                    content={"detail": self._error_detail},
                )(scope, receive, send)
                return

            response = _VerifiedOriginalFileResponse(
                payload,
                file_info=self._file_info,
                filename=self._filename,
                expected_sha256=self._expected_sha256,
                media_type=self._response_media_type,
                content_disposition_type=self._content_disposition_type,
            )
            payload = None
            send_task = asyncio.create_task(response(scope, receive, send))
            done, _ = await asyncio.wait(
                {send_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if send_task in done:
                await send_task
            else:
                await _cancel_and_join(send_task)
        finally:
            cancellation.set()
            if send_task is not None and not send_task.done():
                await _cancel_and_join(send_task)
            await _cancel_and_join(disconnect_task)
            if not snapshot_claimed:
                await _close_unclaimed_snapshot(snapshot_task)
            if payload is not None:
                _close_binary_handle(payload)


def _registered_auxiliary_payload(
    data_root: Path,
    *,
    asset_id: object,
    kind: object,
    relative_path: object,
    mime_type: object,
    language: object,
    expected_sha256: object,
) -> tuple[BinaryIO, int, str, str]:
    """Verify one sidecar into a bounded spool before any bytes are served."""

    canonical_asset_id = _canonical_asset_id(asset_id)
    if kind == "thumbnail":
        directory = "thumbnails"
        mime_types = THUMBNAIL_MIME_TYPES
    elif kind == "caption":
        directory = "captions"
        mime_types = CAPTION_MIME_TYPES
    else:
        raise ValueError("registered artifact kind is invalid")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path) > 4096
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("registered artifact path is invalid")
    stored = PurePosixPath(relative_path)
    if (
        stored.is_absolute()
        or stored.as_posix() != relative_path
        or len(stored.parts) != 4
        or stored.parts[:3] != ("assets", canonical_asset_id, directory)
        or any(part in {"", ".", ".."} for part in stored.parts)
    ):
        raise ValueError("registered artifact path is invalid")

    suffix = Path(stored.name).suffix.lower()
    expected_mime = mime_types.get(suffix)
    if (
        expected_mime is None
        or not isinstance(mime_type, str)
        or mime_type != expected_mime
    ):
        raise ValueError("registered artifact MIME is invalid")
    stem = stored.name[: -len(suffix)]
    if kind == "thumbnail":
        match = re.fullmatch(r"thumbnail-([0-9]{4,})", stem)
        if language is not None:
            raise ValueError("registered thumbnail language is invalid")
    else:
        match = re.fullmatch(
            r"caption-([0-9]{4,})-([A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)",
            stem,
        )
        if (
            match is None
            or not isinstance(language, str)
            or match.group(2) != language
        ):
            raise ValueError("registered caption language is invalid")
    if match is None or f"{int(match.group(1)):04d}" != match.group(1):
        raise ValueError("registered artifact ordinal is invalid")
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ValueError("registered artifact hash is invalid")

    root_info = data_root.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    root_attributes = getattr(root_info, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or (reparse and root_attributes & reparse)
    ):
        raise ValueError("asset root is not a plain directory")
    root = data_root.resolve(strict=True)
    current = root
    final_info: os.stat_result | None = None
    for index, component in enumerate(stored.parts):
        current /= component
        info = current.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or (reparse and attributes & reparse):
            raise ValueError("registered artifact path contains a link")
        if index < len(stored.parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("registered artifact parent is not a directory")
        else:
            final_info = info

    assert final_info is not None
    if (
        not stat.S_ISREG(final_info.st_mode)
        or final_info.st_nlink != 1
        or final_info.st_size < 1
        or final_info.st_size > DEFAULT_MAX_AUXILIARY_FILE_BYTES
    ):
        raise ValueError("registered artifact is not a bounded single-link file")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("registered artifact is outside the asset root")
    spool: BinaryIO = tempfile.SpooledTemporaryFile(
        max_size=_AUXILIARY_SPOOL_MEMORY_BYTES,
        mode="w+b",
    )
    try:
        digest = hashlib.sha256()
        total_bytes = 0
        with resolved.open("rb") as handle:
            opened_info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_nlink != 1
                or opened_info.st_dev != final_info.st_dev
                or opened_info.st_ino != final_info.st_ino
                or opened_info.st_size != final_info.st_size
            ):
                raise ValueError("registered artifact changed before reading")
            while chunk := handle.read(_VERIFIED_STREAM_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > DEFAULT_MAX_AUXILIARY_FILE_BYTES:
                    raise ValueError("registered artifact content is invalid")
                digest.update(chunk)
                spool.write(chunk)
            final_opened_info = os.fstat(handle.fileno())
        if (
            total_bytes != opened_info.st_size
            or final_opened_info.st_dev != opened_info.st_dev
            or final_opened_info.st_ino != opened_info.st_ino
            or final_opened_info.st_size != opened_info.st_size
            or digest.hexdigest() != expected_sha256
        ):
            raise ValueError("registered artifact content is invalid")
        spool.seek(0)
        return spool, total_bytes, expected_mime, suffix
    except BaseException:
        spool.close()
        raise


def _stream_auxiliary_payload(handle: BinaryIO) -> Iterator[bytes]:
    """Stream verified bytes and always release a rolled temporary file."""

    try:
        while chunk := handle.read(_VERIFIED_STREAM_CHUNK_BYTES):
            yield chunk
    finally:
        handle.close()


class _VerifiedAuxiliaryStreamingResponse(StreamingResponse):
    """Own the verified spool through every ASGI completion path."""

    def __init__(
        self,
        handle: BinaryIO,
        *,
        size_bytes: int,
        media_type: str,
        filename: str,
    ) -> None:
        self._verified_handle = handle
        try:
            super().__init__(
                _stream_auxiliary_payload(handle),
                media_type=media_type,
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Length": str(size_bytes),
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        except BaseException:
            handle.close()
            raise

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._verified_handle.close()


def _configured_short_link_resolver(
    settings: Settings,
) -> ControlledShortLinkResolver | None:
    if not settings.short_link_resolution_enabled:
        return None
    if settings.local_direct_short_links:
        return ControlledShortLinkResolver(
            transport=LocalDirectShortLinkTransport(acknowledged=True),
        )
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
    credential_defaults: CredentialDefaults | None = None,
    managed_worker_status: ManagedWorkerRuntimeStatus | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_settings.validate_startup_security()
    owns_local_short_link_transport = (
        short_link_resolver is None and resolved_settings.local_direct_short_links
    )
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
        if credential_defaults is not None:
            if (
                not isinstance(credential_defaults, CredentialDefaults)
                or credential_defaults.run_id != active_logger.run_id
            ):
                raise ValueError("configured credential defaults belong to another run")
            revalidate_credential_defaults(credential_defaults)
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
        credential_defaults=credential_defaults,
    )
    worker_repository = WorkerRepository(database)
    capability_repository = CapabilityEvidenceRepository(database)
    managed_product_identity = current_product_identity() if managed_worker_status is not None else None
    upload_root = default_upload_root(resolved_settings.data_root)

    def current_worker_runtime_status() -> WorkerRuntimeStatusResponse:
        if managed_worker_status is None or managed_product_identity is None:
            return unknown_runtime_status()
        try:
            queue = worker_repository.get_queue_control()
        except Exception:
            return unknown_runtime_status(detail_code="queue_state_unavailable")
        return managed_worker_status.snapshot(
            expected_run_id=active_logger.run_id,
            expected_product_identity=managed_product_identity,
            queue_paused=bool(queue["paused"]),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        upload_activity = await run_in_threadpool(
            lambda: UploadActivityLease.acquire(upload_root, exclusive=False)
        )
        try:
            await run_in_threadpool(app.state.workflow_manager.resume_existing)
            active_logger.emit(
                "control.started",
                app_version=__version__,
                app_schema_version=SCHEMA_VERSION,
                port=resolved_settings.port,
            )
            try:
                yield
            finally:
                try:
                    await run_in_threadpool(app.state.workflow_manager.stop)
                finally:
                    try:
                        await run_in_threadpool(app.state.editing_manager.stop)
                    finally:
                        try:
                            await run_in_threadpool(app.state.upload_manager.stop)
                        finally:
                            if owns_local_short_link_transport:
                                assert isinstance(short_link_resolver, ControlledShortLinkResolver)
                                short_link_resolver.transport.close()
                            active_logger.emit("control.stopped")
        finally:
            upload_activity.release()

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
    app.state.capability_evidence_repository = capability_repository

    def upload_original_asset(asset_id: str) -> tuple[Path, str]:
        try:
            canonical_id = _canonical_asset_id(asset_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="asset_not_found") from None
        registered = service.get_ready_original_asset(canonical_id)
        if registered is None or registered.get("media_kind") != "video":
            raise HTTPException(status_code=404, detail="asset_not_found")
        try:
            path, _ = _registered_original_file(
                resolved_settings.data_root, asset_id=canonical_id,
                relative_path=registered["original_path"],
                expected_size=registered["size_bytes"],
            )
        except (OSError, ValueError):
            raise HTTPException(status_code=409, detail="asset_file_unavailable") from None
        return path, registered["sha256"]

    editing_processor_factory = None
    if (
        resolved_settings.tool_root is not None
        and cached_toolchain_status.state == "ready"
        and cached_toolchain_status.offline_smoke_passed
    ):
        editing_processor_factory = lambda: EditingMediaProcessor(
            resolved_settings.tool_root
        )
    editing_manager = install_editing_routes(
        app,
        data_root=resolved_settings.data_root,
        original_asset_resolver=upload_original_asset,
        processor_factory=editing_processor_factory,
    )

    def upload_edited_output(output_id: str) -> tuple[Path, str, str]:
        try:
            return editing_manager.resolve_output(output_id)
        except EditingError as exc:
            status_code = 404 if exc.code in {"asset_not_found", "asset_not_ready"} else 409
            raise HTTPException(status_code=status_code, detail=exc.code) from None

    def upload_edited_cover(output_id: str) -> tuple[Path, str, str]:
        try:
            return editing_manager.resolve_cover(output_id)
        except EditingError as exc:
            status_code = 404 if exc.code in {"asset_not_found", "asset_not_ready"} else 409
            raise HTTPException(status_code=status_code, detail=exc.code) from None

    install_upload_routes(
        app, data_root=resolved_settings.data_root,
        original_asset_resolver=upload_original_asset,
        edited_output_resolver=upload_edited_output,
        edited_cover_resolver=upload_edited_cover,
    )
    workflow_adapter = LocalWorkflowAdapter(
        batch_service=service,
        editing_manager=editing_manager,
        upload_manager=app.state.upload_manager,
        download_asset_resolver=upload_original_asset,
        download_runtime_probe=lambda: current_worker_runtime_status().model_dump(),
        download_control=worker_repository,
    )
    workflow_manager = WorkflowManager(
        default_workflow_root(resolved_settings.data_root), workflow_adapter
    )
    install_workflow_routes(app, workflow_manager)
    download_nonce = install_local_http_guard(
        app,
        protects_path=_download_http_path,
        csrf_header="x-download-csrf",
        forbidden_detail="download_request_forbidden",
        requires_csrf=lambda method, _path: method not in {"GET", "HEAD"},
        preserve_existing_no_store=True,
    )

    @app.exception_handler(CredentialDefaultsError)
    async def credential_defaults_error(_request: Request, _exc: CredentialDefaultsError):
        return JSONResponse(
            status_code=409,
            content={"detail": "默认 Cookie 配置已失效；请检查本地配置并重启，或明确选择匿名下载"},
        )

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
            "/api/v1/credential-defaults",
            "/api/v1/operations/queue",
            "/api/v1/operations/tools",
            "/api/v1/download-capabilities",
            "/api/v1/capability-implementations",
            "/api/v1/capability-evidence",
            "/api/v1/capability-decisions",
            "/api/v1/capability-decisions/{identity_key}/history",
            "/api/v1/capability-snapshot",
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
        database_ok, detail = database.cached_readiness()
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
    def index() -> HTMLResponse:
        return HTMLResponse(
            INDEX_HTML,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": page_content_security_policy(INDEX_HTML),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )

    @app.get("/api/v1/session")
    def download_session():
        return {"csrf_token": download_nonce}

    def shared_ui_asset(name: str) -> Response:
        payload, media_type = ui_asset(name)
        return Response(
            content=payload,
            media_type=media_type,
            headers={
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/assets/open-flame.css", include_in_schema=False)
    def shared_stylesheet() -> Response:
        return shared_ui_asset("open-flame.css")

    @app.get("/assets/open-flame-shell.js", include_in_schema=False)
    def shared_shell_script() -> Response:
        return shared_ui_asset("open-flame-shell.js")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        payload, _ = health_payload()
        return payload

    @app.get("/api/v1/operations/runtime", response_model=WorkerRuntimeStatusResponse)
    def worker_runtime_status() -> WorkerRuntimeStatusResponse:
        return current_worker_runtime_status()

    @app.get("/health/live", include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=HealthResponse, include_in_schema=False)
    def readiness() -> HealthResponse:
        payload, operational = health_payload()
        if not operational:
            raise HTTPException(status_code=503, detail=payload.model_dump())
        return payload

    @app.get("/api/v1/credential-defaults", response_model=CredentialDefaultsResponse)
    def get_credential_defaults() -> CredentialDefaultsResponse:
        if credential_defaults is None:
            return CredentialDefaultsResponse(platforms=[], available=True)
        available = True
        try:
            revalidate_credential_defaults(credential_defaults)
            with database.connect() as connection:
                connection.execute("BEGIN")
                for platform in credential_defaults.platforms:
                    resolve_default_profile_locked(
                        connection, defaults=credential_defaults,
                        platform=platform, now=datetime.now(UTC),
                    )
        except CredentialDefaultsError:
            available = False
        return CredentialDefaultsResponse(
            platforms=list(credential_defaults.platforms), available=available,
        )

    @app.post(
        "/api/v1/batches",
        response_model=BatchResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_batch(payload: BatchCreateRequest, request: Request) -> BatchResponse:
        created = service.create_batch(
            name=payload.name,
            raw_inputs=payload.inputs,
            credential_mode=payload.credential_mode,
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
        credential_mode: CredentialMode = Query(default="use_default"),
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
        created = await run_in_threadpool(
            service.create_batch,
            name=name,
            raw_inputs=raw_inputs,
            credential_mode=credential_mode,
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
            public_artifacts = [
                public
                for artifact in record["artifacts"]
                if (public := _public_auxiliary_artifact(artifact)) is not None
            ]
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
                        "artifacts": public_artifacts,
                    }
                )
            )
        return response

    @app.get(
        "/api/v1/assets/{asset_id}/download",
        response_class=FileResponse,
    )
    def download_asset(asset_id: str) -> Response:
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
        return _VerifiedOriginalSnapshotResponse(
            path,
            file_info=file_info,
            filename=f"asset-{canonical_id}{suffix}",
            expected_sha256=registered["sha256"],
        )

    @app.get("/api/v1/artifacts/{artifact_id}/download")
    def download_auxiliary_artifact(artifact_id: str) -> StreamingResponse:
        try:
            canonical_id = _canonical_asset_id(artifact_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="辅助产物不存在") from None
        registered = service.get_ready_auxiliary_artifact(canonical_id)
        if registered is None:
            raise HTTPException(status_code=404, detail="辅助产物不存在")
        try:
            payload, size_bytes, mime_type, suffix = _registered_auxiliary_payload(
                resolved_settings.data_root,
                asset_id=registered["asset_id"],
                kind=registered["kind"],
                relative_path=registered["artifact_path"],
                mime_type=registered["mime_type"],
                language=registered["language"],
                expected_sha256=registered["sha256"],
            )
        except (OSError, ValueError):
            raise HTTPException(status_code=409, detail="辅助产物文件不可用") from None
        return _VerifiedAuxiliaryStreamingResponse(
            payload,
            size_bytes=size_bytes,
            media_type=mime_type,
            filename=f"artifact-{canonical_id}{suffix}",
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

    @app.get(
        "/api/v1/capability-implementations",
        response_model=list[CapabilityImplementationResponse],
    )
    def capability_implementations() -> list[CapabilityImplementationResponse]:
        return [
            CapabilityImplementationResponse.model_validate(item)
            for item in list_registered_implementations()
        ]

    @app.get(
        "/api/v1/capability-evidence",
        response_model=list[CapabilityEvidenceResponse],
    )
    def capability_evidence(
        limit: int = Query(default=200, ge=1, le=500),
    ) -> list[CapabilityEvidenceResponse]:
        ready, _ = database.readiness()
        if not ready:
            raise HTTPException(status_code=503, detail="capability_store_unavailable")
        return [
            CapabilityEvidenceResponse.model_validate(item)
            for item in capability_repository.list_evidence(limit=limit)
        ]

    @app.get(
        "/api/v1/capability-decisions",
        response_model=list[CapabilityDecisionResponse],
    )
    def capability_decisions(
        limit: int = Query(default=200, ge=1, le=500),
    ) -> list[CapabilityDecisionResponse]:
        ready, _ = database.readiness()
        if not ready:
            raise HTTPException(status_code=503, detail="capability_store_unavailable")
        return [
            CapabilityDecisionResponse.model_validate(item)
            for item in capability_repository.list_current_decisions(limit=limit)
        ]

    @app.get(
        "/api/v1/capability-decisions/{identity_key}/history",
        response_model=list[CapabilityDecisionResponse],
    )
    def capability_decision_history(
        identity_key: str,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> list[CapabilityDecisionResponse]:
        ready, _ = database.readiness()
        if not ready:
            raise HTTPException(status_code=503, detail="capability_store_unavailable")
        try:
            records = capability_repository.history(
                identity_key=identity_key,
                limit=limit,
            )
        except CapabilityGovernanceError as exc:
            if exc.exit_code == 4:
                raise HTTPException(status_code=404, detail=exc.error_code) from None
            if exc.exit_code == 2:
                raise HTTPException(status_code=422, detail=exc.error_code) from None
            raise HTTPException(
                status_code=503, detail="capability_store_unavailable"
            ) from None
        return [
            CapabilityDecisionResponse.model_validate(item) for item in records
        ]

    @app.get(
        "/api/v1/capability-snapshot",
        response_model=CapabilitySnapshotResponse,
    )
    def capability_snapshot(
        limit: int = Query(default=200, ge=1, le=500),
    ) -> CapabilitySnapshotResponse:
        ready, _ = database.readiness()
        if not ready:
            raise HTTPException(status_code=503, detail="capability_store_unavailable")
        snapshot = capability_repository.snapshot(limit=limit)
        try:
            product_identity = current_product_identity()
        except ProductBuildDriftError:
            raise HTTPException(
                status_code=503, detail="product_build_drift"
            ) from None
        except ProductBuildUnavailableError:
            raise HTTPException(
                status_code=503, detail="product_build_unavailable"
            ) from None
        return CapabilitySnapshotResponse.model_validate(
            {
                "current_product_identity": product_identity,
                "implementations": list_registered_implementations(),
                **snapshot,
            }
        )

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
        try:
            reset = worker_repository.reset_platform_circuit(
                platform=platform, now=datetime.now(UTC)
            )
        except CircuitResetConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if reset is None:
            raise HTTPException(status_code=404, detail="平台熔断记录不存在")
        active_logger.emit(
            "circuit.reset",
            request_id=request.state.runtime_request_id,
            platform=platform.value,
        )
        return PlatformCircuitResponse.model_validate(reset)

    @app.post(
        "/api/v1/jobs/{job_id}/retry",
        response_model=JobRetryResponse,
    )
    def retry_job(
        job_id: str, request: Request, payload: JobRetryRequest | None = None,
    ) -> JobRetryResponse:
        try:
            retried = worker_repository.request_retry(
                job_id,
                now=datetime.now(UTC),
                credential_mode=payload.credential_mode if payload is not None else None,
                credential_defaults=credential_defaults,
            )
        except (InvalidTransition, RetryConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if retried is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        retry_log_fields: dict[str, object] = {
            "request_id": request.state.runtime_request_id,
            "job_id": job_id,
            "result_status": str(retried["status"]),
            "run_generation": int(retried["run_generation"]),
            "platform": str(retried["platform"]),
        }
        previous_error_code = retried["previous_error_code"]
        if isinstance(previous_error_code, str):
            try:
                retry_log_fields["error_code"] = ErrorCode(previous_error_code).value
            except ValueError:
                pass
        active_logger.emit("job.retry_requested", **retry_log_fields)
        return JobRetryResponse(
            job_id=job_id,
            status="queued",
            run_generation=int(retried["run_generation"]),
        )

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

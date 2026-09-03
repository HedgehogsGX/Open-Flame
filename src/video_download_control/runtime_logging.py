"""Bounded, structured runtime logs for local troubleshooting.

Records are JSON Lines with an intentionally small field allow-list.  Raw
URLs, request bodies, headers, query strings, cookie material, subprocess
arguments/stdout/stderr, filesystem paths, and arbitrary exception messages
are not accepted at this boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from .domain import ErrorCode, JobStatus, Platform, SourceType

RUNTIME_LOG_SCHEMA_VERSION: Final = 1
DEFAULT_RUNTIME_LOG_MAX_BYTES: Final = 10 * 1024 * 1024
DEFAULT_RUNTIME_LOG_BACKUP_COUNT: Final = 5
MAX_RUNTIME_LOG_LINE_BYTES: Final = 8192
MAX_RUNTIME_LOG_READ_EVENTS: Final = 500
RUNTIME_LOG_COMPONENTS: Final = (
    "control",
    "offline-worker",
    "candidate-worker",
    "local-worker",
)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EVENT = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_UUID_TOKEN = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_APP_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]{0,48})?$")
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}\.[0-9]{3}Z$"
)
_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_ROUTES = frozenset(
    {
        "/",
        "/api/v1/batches",
        "/api/v1/batches/import",
        "/api/v1/batches/{batch_id}",
        "/api/v1/batches/{batch_id}/assets",
        "/api/v1/assets/{asset_id}/download",
        "/api/v1/inputs/{input_id}/cancel",
        "/api/v1/inputs/{input_id}/rediscover",
        "/api/v1/jobs/{job_id}/cancel",
        "/api/v1/metrics",
        "/api/v1/operations/logs",
        "/api/v1/operations/queue",
        "/api/v1/operations/queue/resume",
        "/api/v1/operations/tools",
        "/api/v1/platform-circuits",
        "/api/v1/platform-circuits/{platform}/reset",
        "/docs",
        "/health",
        "/health/live",
        "/health/ready",
        "/openapi.json",
        "/redoc",
        "/unmatched",
    }
)
_ALLOWED_EVENTS = frozenset(
    {
        "batch.created",
        "batch.imported",
        "circuit.reset",
        "control.initializing",
        "control.started",
        "control.startup_failed",
        "control.stopped",
        "http.request_completed",
        "http.request_failed",
        "input.cancel_requested",
        "input.rediscover_requested",
        "job.cancel_requested",
        "queue.resumed",
        "runtime_log.event_rejected",
        "subprocess.completed",
        "subprocess.failed",
        "subprocess.started",
        "toolchain.inspected",
        "worker.cleanup_failed",
        "worker.cycle_failed",
        "worker.heartbeat_failed",
        "worker.job_claimed",
        "worker.job_finished",
        "worker.job_phase",
        "worker.queue_paused",
        "worker.retry_decided",
        "worker.initializing",
        "worker.started",
        "worker.startup_failed",
        "worker.stopped",
    }
)
_ADAPTERS = frozenset({"scripted_fake", "scripted_graph_fake", "yt_dlp"})
_ERROR_CODES = frozenset(item.value for item in ErrorCode)
_PLATFORMS = frozenset(item.value for item in Platform)
_SOURCE_TYPES = frozenset(item.value for item in SourceType)
_RESULT_STATUSES = frozenset(item.value for item in JobStatus) | frozenset(
    {"idle", "lost_lease", "partial_success"}
)
_JOB_KINDS = frozenset({"discover", "download"})
_EXECUTABLES = frozenset(
    {
        "ffmpeg",
        "ffmpeg.exe",
        "ffprobe",
        "ffprobe.exe",
        "python",
        "python3",
        "python3.12",
        "python.exe",
        "yt-dlp",
        "yt-dlp.exe",
    }
)
_REASONS = frozenset(
    {
        "drain_complete",
        "idle",
        "invalid_level",
        "invalid_record",
        "keyboard_interrupt",
        "queue_paused",
        "runtime_error",
        "single_run_complete",
    }
)
_PHASES = frozenset(
    {
        "committing",
        "discovery",
        "downloading",
        "preparing",
        "probing",
        "verifying",
    }
)
_RETRY_ACTIONS = frozenset({"fallback", "retry", "terminal"})
_TOOLCHAIN_STATES = frozenset({"invalid", "ready", "unconfigured"})
_TOOLCHAIN_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_EXCEPTION_TYPES = frozenset(
    {
        "AdapterFailure",
        "AssetStorageError",
        "AssetValidationError",
        "BaseException",
        "CandidateStartupError",
        "CommandCancelled",
        "CommandOutputLimitExceeded",
        "CommandTimedOut",
        "CookiePreparationError",
        "DatabaseError",
        "LostLease",
        "LocalWorkerStartupError",
        "NetworkIsolationError",
        "OSError",
        "RuntimeError",
        "RuntimeLogConfigurationError",
        "SubprocessPolicyError",
        "TypeError",
        "UnhandledException",
        "ValueError",
        "YtDlpContractError",
    }
)
_FAILURE_SITES = frozenset(
    {
        "attempt_directory",
        "heartbeat",
        "pending_asset",
        "policy_validation",
        "process_spawn",
        "process_wait",
        "queue_pause",
        "reconciliation",
    }
)
_CLEANUP_OPERATIONS = frozenset(
    {"attempt_directory", "pending_asset", "reconciliation"}
)
_LOG_FILENAME = re.compile(
    r"^runtime-(?:control|offline-worker|candidate-worker|local-worker)"
    r"(?:-[A-Za-z0-9][A-Za-z0-9._-]{0,63})?\.jsonl"
    r"(?:\.([1-9]|1[0-9]|20))?$"
)

_INTEGER_FIELDS = frozenset(
    {
        "argument_count",
        "app_schema_version",
        "asset_count",
        "attempt_no",
        "canceled_count",
        "duplicate_count",
        "event_count",
        "failed_count",
        "input_count",
        "port",
        "ready_count",
        "rejected_field_count",
        "return_code",
        "retry_delay_ms",
        "run_generation",
        "status_code",
        "stderr_bytes",
        "stdout_bytes",
        "total_count",
        "queued_count",
    }
)
_NUMBER_FIELDS = frozenset({"duration_ms", "timeout_seconds"})
_BOOLEAN_FIELDS = frozenset(
    {
        "direct_network_enabled",
        "js_runtime_enabled",
        "queue_paused",
        "short_link_resolution_enabled",
        "offline_smoke_passed",
        "x_graph_v2_enabled",
    }
)
_TOOLCHAIN_STATE_FIELDS = frozenset({"state"})
_TOOLCHAIN_DETAIL_FIELDS = frozenset({"detail_code"})
_TOOLCHAIN_VERSION_FIELDS = frozenset(
    {"yt_dlp_version", "ffmpeg_version", "ffprobe_version"}
)
_ROUTE_FIELDS = frozenset({"route"})
_METHOD_FIELDS = frozenset({"method"})
_TOKEN_FIELDS = frozenset(
    {
        "adapter",
        "app_version",
        "attempt_id",
        "batch_id",
        "error_code",
        "exception_type",
        "executable",
        "failure_site",
        "input_id",
        "job_id",
        "job_kind",
        "platform",
        "phase",
        "reason",
        "request_id",
        "result_status",
        "retry_action",
        "source_type",
        "subprocess_id",
        "cleanup_operation",
        "worker_id",
    }
)
_ALLOWED_FIELDS = (
    _INTEGER_FIELDS
    | _NUMBER_FIELDS
    | _BOOLEAN_FIELDS
    | _ROUTE_FIELDS
    | _METHOD_FIELDS
    | _TOKEN_FIELDS
    | _TOOLCHAIN_STATE_FIELDS
    | _TOOLCHAIN_DETAIL_FIELDS
    | _TOOLCHAIN_VERSION_FIELDS
)
_BASE_FIELDS = frozenset(
    {
        "component",
        "event",
        "event_id",
        "level",
        "pid",
        "run_id",
        "schema_version",
        "sequence",
        "thread_id",
        "timestamp",
    }
)

_EVENT_FIELDS: Final[dict[str, frozenset[str]]] = {
    "batch.created": frozenset(
        {
            "request_id",
            "batch_id",
            "total_count",
            "queued_count",
            "failed_count",
            "duplicate_count",
        }
    ),
    "batch.imported": frozenset(
        {
            "request_id",
            "batch_id",
            "total_count",
            "queued_count",
            "failed_count",
            "duplicate_count",
        }
    ),
    "circuit.reset": frozenset({"request_id", "platform"}),
    "control.initializing": frozenset(
        {
            "app_version",
            "app_schema_version",
            "x_graph_v2_enabled",
            "short_link_resolution_enabled",
        }
    ),
    "control.started": frozenset({"app_version", "app_schema_version", "port"}),
    "control.startup_failed": frozenset({"exception_type"}),
    "control.stopped": frozenset({"reason"}),
    "http.request_completed": frozenset(
        {"request_id", "method", "route", "status_code", "duration_ms"}
    ),
    "http.request_failed": frozenset(
        {
            "request_id",
            "method",
            "route",
            "status_code",
            "duration_ms",
            "exception_type",
        }
    ),
    "input.cancel_requested": frozenset({"request_id", "input_id", "result_status"}),
    "input.rediscover_requested": frozenset(
        {"request_id", "input_id", "result_status", "run_generation"}
    ),
    "job.cancel_requested": frozenset({"request_id", "job_id", "result_status"}),
    "queue.resumed": frozenset({"request_id", "queue_paused"}),
    "runtime_log.event_rejected": frozenset({"reason", "rejected_field_count"}),
    "subprocess.started": frozenset(
        {"subprocess_id", "executable", "argument_count", "timeout_seconds"}
    ),
    "subprocess.completed": frozenset(
        {
            "subprocess_id",
            "executable",
            "argument_count",
            "return_code",
            "stdout_bytes",
            "stderr_bytes",
            "duration_ms",
        }
    ),
    "subprocess.failed": frozenset(
        {
            "subprocess_id",
            "executable",
            "argument_count",
            "return_code",
            "duration_ms",
            "exception_type",
            "failure_site",
        }
    ),
    "toolchain.inspected": frozenset(
        {
            "state",
            "detail_code",
            "yt_dlp_version",
            "ffmpeg_version",
            "ffprobe_version",
            "offline_smoke_passed",
        }
    ),
    "worker.cleanup_failed": frozenset(
        {
            "worker_id",
            "job_id",
            "attempt_id",
            "error_code",
            "exception_type",
            "failure_site",
            "cleanup_operation",
        }
    ),
    "worker.cycle_failed": frozenset({"worker_id", "exception_type", "duration_ms"}),
    "worker.heartbeat_failed": frozenset(
        {
            "worker_id",
            "job_id",
            "attempt_id",
            "exception_type",
            "failure_site",
        }
    ),
    "worker.job_claimed": frozenset(
        {
            "worker_id",
            "job_id",
            "attempt_id",
            "attempt_no",
            "run_generation",
            "platform",
            "source_type",
            "job_kind",
            "adapter",
        }
    ),
    "worker.job_phase": frozenset(
        {
            "worker_id",
            "job_id",
            "attempt_id",
            "phase",
            "result_status",
        }
    ),
    "worker.retry_decided": frozenset(
        {
            "worker_id",
            "job_id",
            "attempt_id",
            "error_code",
            "retry_action",
            "retry_delay_ms",
        }
    ),
    "worker.job_finished": frozenset(
        {
            "worker_id",
            "job_id",
            "attempt_id",
            "result_status",
            "error_code",
            "asset_count",
            "duration_ms",
        }
    ),
    "worker.queue_paused": frozenset(
        {
            "worker_id",
            "job_id",
            "attempt_id",
            "error_code",
            "exception_type",
            "failure_site",
        }
    ),
    "worker.initializing": frozenset(
        {
            "worker_id",
            "adapter",
            "direct_network_enabled",
            "js_runtime_enabled",
        }
    ),
    "worker.started": frozenset(
        {
            "worker_id",
            "adapter",
            "direct_network_enabled",
            "js_runtime_enabled",
        }
    ),
    "worker.startup_failed": frozenset({"worker_id", "exception_type", "error_code"}),
    "worker.stopped": frozenset({"worker_id", "reason"}),
}


class RuntimeLogConfigurationError(ValueError):
    """The operator supplied an invalid runtime log configuration."""


class RuntimeLogRecordError(ValueError):
    """A caller attempted to cross the runtime log allow-list boundary."""


@dataclass(frozen=True, slots=True)
class RuntimeLogConfig:
    directory: Path
    level: str = "INFO"
    max_bytes: int = DEFAULT_RUNTIME_LOG_MAX_BYTES
    backup_count: int = DEFAULT_RUNTIME_LOG_BACKUP_COUNT

    def __post_init__(self) -> None:
        if not isinstance(self.directory, Path) or not self.directory.is_absolute():
            raise RuntimeLogConfigurationError(
                "runtime log directory must be an absolute path"
            )
        normalized_level = str(self.level).upper()
        if normalized_level not in _LEVELS:
            raise RuntimeLogConfigurationError("runtime log level is invalid")
        object.__setattr__(self, "level", normalized_level)
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or not 1024 <= self.max_bytes <= 1024 * 1024 * 1024
        ):
            raise RuntimeLogConfigurationError(
                "runtime log max bytes must be between 1024 and 1073741824"
            )
        if (
            isinstance(self.backup_count, bool)
            or not isinstance(self.backup_count, int)
            or not 1 <= self.backup_count <= 20
        ):
            raise RuntimeLogConfigurationError(
                "runtime log backup count must be between 1 and 20"
            )


class RuntimeLogger:
    """Append-only JSONL logger with per-component size rotation.

    Components use separate files, so the control plane and Worker processes do
    not contend over a rotation sequence.  Write failures never break the media
    workflow; status remains inspectable through ``status()``.
    """

    def __init__(
        self,
        *,
        component: str,
        config: RuntimeLogConfig,
        clock: Callable[[], datetime] | None = None,
        run_id: str | None = None,
        instance_id: str | None = None,
    ) -> None:
        if (
            not _COMPONENT.fullmatch(component)
            or component not in RUNTIME_LOG_COMPONENTS
        ):
            raise RuntimeLogConfigurationError("runtime log component is invalid")
        resolved_run_id = run_id or uuid4().hex
        if not _TOKEN.fullmatch(resolved_run_id):
            raise RuntimeLogConfigurationError("runtime log run id is invalid")
        if instance_id is not None and not _INSTANCE.fullmatch(instance_id):
            raise RuntimeLogConfigurationError("runtime log instance id is invalid")
        self.component = component
        self.config = config
        self.run_id = resolved_run_id
        # File names must never expose an operator-supplied Worker identifier.
        # A stable digest still gives each configured Worker its own rotation
        # sequence without copying that identifier into the filesystem.
        instance_suffix = (
            f"-{hashlib.sha256(instance_id.encode('utf-8')).hexdigest()[:12]}"
            if instance_id is not None
            else ""
        )
        self.path = config.directory / f"runtime-{component}{instance_suffix}.jsonl"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._sequence_lock = threading.Lock()
        self._sequence = 0
        self._available = False
        self._write_failures = 0
        self._rejected_events = 0
        self._last_failure_code: str | None = None
        try:
            self._ensure_directory()
        except OSError:
            self._last_failure_code = "log_directory_unavailable"

    def emit(self, event: str, *, level: str = "INFO", **fields: object) -> bool:
        """Write one allow-listed event and return whether it was persisted."""

        normalized_level = str(level).upper()
        if normalized_level not in _LEVELS:
            return self._reject_event(len(fields), "invalid_level")
        if _LEVELS[normalized_level] < _LEVELS[self.config.level]:
            return True
        try:
            record = self._build_record(event, normalized_level, fields)
        except (RuntimeLogRecordError, TypeError, ValueError):
            return self._reject_event(len(fields), "invalid_record")
        return self._persist(record)

    def status(self) -> dict[str, str | int]:
        if not self._available:
            status = "error"
        elif self._rejected_events or self._write_failures:
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "run_id": self.run_id,
            "write_failures": self._write_failures,
            "rejected_events": self._rejected_events,
            "last_failure_code": self._last_failure_code or "none",
        }

    def recent_events(self, *, limit: int = 100) -> list[dict[str, object]]:
        return read_recent_runtime_events(
            self.config.directory,
            limit=limit,
            backup_count=self.config.backup_count,
        )

    def _reject_event(self, field_count: int, reason: str) -> bool:
        self._rejected_events += 1
        fallback = self._base_record("runtime_log.event_rejected", "ERROR")
        fallback.update(
            {
                "reason": reason,
                "rejected_field_count": max(0, min(field_count, 1_000_000)),
            }
        )
        self._persist(fallback)
        return False

    def _build_record(
        self, event: str, level: str, fields: dict[str, object]
    ) -> dict[str, object]:
        if not _EVENT.fullmatch(event) or event not in _ALLOWED_EVENTS:
            raise RuntimeLogRecordError("runtime event name is invalid")
        unknown = set(fields) - _EVENT_FIELDS[event]
        if unknown:
            raise RuntimeLogRecordError("runtime event field is not allow-listed")
        record = self._base_record(event, level)
        for key, value in fields.items():
            record[key] = _validated_field(key, value)
        return record

    def _base_record(self, event: str, level: str) -> dict[str, object]:
        try:
            timestamp = self._clock()
        except Exception:  # noqa: BLE001 - logging cannot break the application
            timestamp = datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = datetime.now(UTC)
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        return {
            "schema_version": RUNTIME_LOG_SCHEMA_VERSION,
            "timestamp": timestamp.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": level,
            "event": event,
            "component": self.component,
            "run_id": self.run_id,
            "event_id": uuid4().hex,
            "pid": os.getpid(),
            "sequence": sequence,
            "thread_id": threading.get_ident(),
        }

    def _persist(self, record: dict[str, object]) -> bool:
        try:
            payload = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            if len(payload) > MAX_RUNTIME_LOG_LINE_BYTES:
                raise OSError("runtime log record exceeds the internal byte ceiling")
            with self._lock:
                self._append(payload)
            self._available = True
            self._last_failure_code = None
            return True
        except (OSError, TypeError, ValueError):
            self._available = False
            self._write_failures += 1
            self._last_failure_code = "log_write_failed"
            return False

    def _append(self, payload: bytes) -> None:
        self._ensure_directory()
        if self.path.exists():
            _require_plain_file(self.path)
            if self.path.stat().st_size + len(payload) > self.config.max_bytes:
                self._rotate()
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or _is_reparse(info)
            ):
                raise OSError("runtime log target is not a plain regular file")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("runtime log append made no progress")
                view = view[written:]
        finally:
            os.close(descriptor)

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.config.backup_count}")
        if oldest.exists():
            _require_plain_file(oldest)
            oldest.unlink()
        for index in range(self.config.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if not source.exists():
                continue
            _require_plain_file(source)
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            source.replace(target)
        if self.path.exists():
            _require_plain_file(self.path)
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def _ensure_directory(self) -> None:
        _reject_link_components(self.config.directory)
        self.config.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_link_components(self.config.directory)
        info = self.config.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
        ):
            raise OSError("runtime log directory is not a plain directory")
        if os.name == "posix":
            os.chmod(self.config.directory, 0o700)


def read_recent_runtime_events(
    directory: Path,
    *,
    limit: int = 100,
    backup_count: int = DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
) -> list[dict[str, object]]:
    """Read a bounded, validated tail across known service components."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("runtime log read limit must be between 1 and 500")
    if (
        isinstance(backup_count, bool)
        or not isinstance(backup_count, int)
        or not 1 <= backup_count <= 20
    ):
        raise ValueError("runtime log backup count must be between 1 and 20")
    try:
        info = directory.lstat()
    except OSError:
        return []
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
    ):
        return []

    records: list[dict[str, object]] = []
    candidates: list[tuple[int, Path]] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        match = _LOG_FILENAME.fullmatch(path.name)
        if match is None:
            continue
        suffix = match.group(1)
        if suffix is not None and int(suffix) > backup_count:
            continue
        try:
            _require_plain_file(path)
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:256]:
        try:
            lines = _tail_lines(path, limit)
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if _is_public_runtime_record(record):
                records.append(record)
    records.sort(
        key=lambda item: (
            str(item["timestamp"]),
            str(item["run_id"]),
            int(item["sequence"]),
            str(item["event_id"]),
        )
    )
    return records[-limit:]


def _tail_lines(path: Path, limit: int) -> list[bytes]:
    max_scan_bytes = limit * MAX_RUNTIME_LOG_LINE_BYTES
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        start = max(0, end - max_scan_bytes)
        handle.seek(start)
        payload = handle.read(max_scan_bytes)
    if start and payload:
        first_newline = payload.find(b"\n")
        payload = b"" if first_newline < 0 else payload[first_newline + 1 :]
    return payload.splitlines()[-limit:]


def _validated_field(key: str, value: object) -> object:
    if key in _INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeLogRecordError("runtime integer field is invalid")
        if not -1_000_000_000 <= value <= 1_000_000_000:
            raise RuntimeLogRecordError("runtime integer field is outside bounds")
        return value
    if key in _NUMBER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeLogRecordError("runtime number field is invalid")
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= 86_400_000:
            raise RuntimeLogRecordError("runtime number field is outside bounds")
        return round(number, 3)
    if key in _BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise RuntimeLogRecordError("runtime boolean field is invalid")
        return value
    if key in _TOOLCHAIN_STATE_FIELDS:
        if not isinstance(value, str) or value not in _TOOLCHAIN_STATES:
            raise RuntimeLogRecordError("runtime toolchain state is invalid")
        return value
    if key in _TOOLCHAIN_DETAIL_FIELDS | _TOOLCHAIN_VERSION_FIELDS:
        if not isinstance(value, str) or not _TOOLCHAIN_TEXT.fullmatch(value):
            raise RuntimeLogRecordError("runtime toolchain field is invalid")
        return value
    if not isinstance(value, str):
        raise RuntimeLogRecordError("runtime string field is invalid")
    if key in _METHOD_FIELDS:
        if value not in _METHODS:
            raise RuntimeLogRecordError("runtime HTTP method is invalid")
        return value
    if key in _ROUTE_FIELDS:
        if value not in _ROUTES:
            raise RuntimeLogRecordError("runtime route template is invalid")
        return value
    if key in _TOKEN_FIELDS:
        if not _TOKEN.fullmatch(value):
            raise RuntimeLogRecordError("runtime token field is invalid")
        if key in {
            "attempt_id",
            "batch_id",
            "input_id",
            "job_id",
            "request_id",
            "subprocess_id",
        } and not _UUID_TOKEN.fullmatch(value):
            raise RuntimeLogRecordError("runtime identifier field is invalid")
        if key == "worker_id" and not _INSTANCE.fullmatch(value):
            raise RuntimeLogRecordError("runtime Worker identifier is invalid")
        if key == "exception_type" and not _EXCEPTION_TYPE.fullmatch(value):
            raise RuntimeLogRecordError("runtime exception type is invalid")
        if key == "exception_type" and value not in _EXCEPTION_TYPES:
            raise RuntimeLogRecordError("runtime exception type is not allow-listed")
        if key == "app_version" and not _APP_VERSION.fullmatch(value):
            raise RuntimeLogRecordError("runtime application version is invalid")
        semantic_sets = {
            "adapter": _ADAPTERS,
            "cleanup_operation": _CLEANUP_OPERATIONS,
            "error_code": _ERROR_CODES,
            "executable": _EXECUTABLES,
            "failure_site": _FAILURE_SITES,
            "job_kind": _JOB_KINDS,
            "phase": _PHASES,
            "platform": _PLATFORMS,
            "reason": _REASONS,
            "result_status": _RESULT_STATUSES,
            "retry_action": _RETRY_ACTIONS,
            "source_type": _SOURCE_TYPES,
        }
        accepted = semantic_sets.get(key)
        if accepted is not None and value not in accepted:
            raise RuntimeLogRecordError("runtime semantic token is invalid")
        return value
    raise RuntimeLogRecordError("runtime event field is not allow-listed")


def _is_public_runtime_record(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) - (_BASE_FIELDS | _ALLOWED_FIELDS):
        return False
    required = {
        "schema_version",
        "timestamp",
        "level",
        "event",
        "component",
        "run_id",
        "event_id",
        "pid",
        "sequence",
        "thread_id",
    }
    if not required.issubset(value):
        return False
    if value["schema_version"] != RUNTIME_LOG_SCHEMA_VERSION:
        return False
    if value["level"] not in _LEVELS:
        return False
    if not isinstance(value["timestamp"], str) or not _UTC_TIMESTAMP.fullmatch(
        value["timestamp"]
    ):
        return False
    try:
        datetime.fromisoformat(value["timestamp"])
    except ValueError:
        return False
    if (
        not isinstance(value["event"], str)
        or not _EVENT.fullmatch(value["event"])
        or value["event"] not in _ALLOWED_EVENTS
    ):
        return False
    if set(value) - _BASE_FIELDS - _EVENT_FIELDS[value["event"]]:
        return False
    if (
        not isinstance(value["component"], str)
        or value["component"] not in RUNTIME_LOG_COMPONENTS
    ):
        return False
    for key in set(value) & _ALLOWED_FIELDS:
        try:
            _validated_field(key, value[key])
        except RuntimeLogRecordError:
            return False
    return (
        all(
            isinstance(value[key], int) and not isinstance(value[key], bool)
            for key in ("pid", "sequence", "thread_id")
        )
        and value["sequence"] >= 1
        and all(
            isinstance(value[key], str) and _TOKEN.fullmatch(value[key])
            for key in ("run_id", "event_id")
        )
    )


def safe_exception_type(error: BaseException) -> str:
    """Collapse an exception to a fixed diagnostic category without its text."""

    name = type(error).__name__
    if name in _EXCEPTION_TYPES:
        return name
    if type(error).__module__ == "sqlite3":
        return "DatabaseError"
    if isinstance(error, OSError):
        return "OSError"
    if isinstance(error, ValueError):
        return "ValueError"
    if isinstance(error, RuntimeError):
        return "RuntimeError"
    if isinstance(error, TypeError):
        return "TypeError"
    if isinstance(error, Exception):
        return "UnhandledException"
    return "BaseException"


def _require_plain_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
    ):
        raise OSError("runtime log path is not a plain regular file")


def _reject_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise OSError("runtime log path may not contain links")


def _is_reparse(info: object) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(reparse and attributes & reparse)


__all__ = [
    "DEFAULT_RUNTIME_LOG_BACKUP_COUNT",
    "DEFAULT_RUNTIME_LOG_MAX_BYTES",
    "MAX_RUNTIME_LOG_READ_EVENTS",
    "RUNTIME_LOG_SCHEMA_VERSION",
    "RuntimeLogConfig",
    "RuntimeLogConfigurationError",
    "RuntimeLogger",
    "read_recent_runtime_events",
    "safe_exception_type",
]

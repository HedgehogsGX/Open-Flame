"""Windows supervisor for the standalone local application.

The supervisor is the sole owner of the browser-facing control process and the
direct-network Worker process.  Configuration crosses the process boundary as
typed multiprocessing data, readiness crosses a private pipe, and the public
HTTP listener is reserved before either child is allowed to initialise.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import math
import multiprocessing
import os
import re
import signal
import socket
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn

from . import __version__
from .adapters import YtDlpJsRuntime
from .api import create_app
from .credential_defaults import prepare_credential_defaults
from .build_identity import current_product_identity
from .candidate_cookies import (
    DEFAULT_MAX_COOKIE_BYTES,
    AttemptCookieResolver,
    CookieSource,
)
from .config import DEFAULT_STORAGE_MIN_FREE_BYTES, Settings
from .cookie_source_config import (
    CookieSourceConfig,
    CookieSourceConfigError,
    load_cookie_source_config,
    revalidate_cookie_source_config,
)
from .database import SCHEMA_VERSION, Database
from .domain import ErrorCode
from .local_worker_cli import (
    FEATURE_GATE,
    LocalWorkerConfig,
    _exclusive_local_worker,
    _runtime_logger as _local_worker_runtime_logger,
    build_local_worker,
)
from .local_app_storage import (
    LocalAppLockUnavailable,
    LocalAppStorageError,
    default_local_app_root as _storage_default_local_app_root,
    ensure_plain_directory_tree as _storage_ensure_plain_directory_tree,
    exclusive_local_app as _storage_exclusive_local_app,
    plain_directory_info as _storage_plain_directory_info,
    reject_existing_link_components as _storage_reject_existing_link_components,
    validate_windows_local_path as _storage_validate_windows_local_path,
)
from .runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
    RuntimeLogConfig,
    RuntimeLogger,
    safe_exception_type,
)
from .windows_job import WindowsJobError, WindowsKillOnCloseJob
from .worker_repository import WorkerRepository
from .worker_pool import run_concurrent_worker
from .worker_runtime_status import ManagedWorkerRuntimeStatus


_PROTOCOL_VERSION = 1
_HANDSHAKE_KEYS = frozenset(
    {
        "protocol",
        "status",
        "role",
        "phase",
        "run_id",
        "pid",
        "app_version",
        "schema_version",
        "product_identity",
    }
)
_MAX_HANDSHAKE_BYTES = 8 * 1024
_MAX_HTTP_BODY_BYTES = 1024 * 1024
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_PRODUCT_IDENTITY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,95}\+build\.sha256\.[0-9a-f]{64}$"
)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CHILD_ROLES = frozenset({"control", "worker"})
_COMMAND_LAUNCH = b"L"
_COMMAND_CLAIM = b"C"
_COMMAND_CHECK_COMPLETE = b"K"
_MAX_COMMAND_BYTES = 1
_ROLE_PHASES = {
    "control": frozenset({"control_ready"}),
    "worker": frozenset({"worker_preflight_ready", "worker_ready"}),
}
_CHILD_ENVIRONMENT_ALLOWLIST = frozenset(
    {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TZ"}
)
_CONTROL_CHILD_SECRET_ENVIRONMENT_ALLOWLIST = frozenset(
    {"OPEN_FLAME_AI_OPENAI_API_KEY"}
)
_MAX_CONTROL_CHILD_SECRET_CHARACTERS = 16_384


class LocalAppError(RuntimeError):
    """A bounded, path- and credential-free local application failure."""


class LocalAppStartupFailure(Enum):
    """Known failures at boundaries that precede runtime logger creation."""

    PORT_UNAVAILABLE = "local_port_unavailable"
    TOOLCHAIN_UNAVAILABLE = "local_toolchain_unavailable"
    COOKIE_CONFIG_INVALID = "local_cookie_config_invalid"


class LocalAppStartupError(LocalAppError):
    """Carry a closed failure kind, never a path or underlying exception text."""

    def __init__(self, failure: LocalAppStartupFailure) -> None:
        if type(failure) is not LocalAppStartupFailure:
            raise ValueError("invalid local application startup failure")
        self.failure = failure
        super().__init__(failure.value)


def _environment_value(
    environment: Mapping[str, str],
    name: str,
) -> str | None:
    wanted = name.upper()
    for key, value in environment.items():
        if key.upper() == wanted:
            return value
    return None


def _normalized_absolute_path(path: object) -> bool:
    try:
        _validate_windows_local_path(path, "local application path")
    except LocalAppError:
        return False
    return True


def _validate_windows_local_path(path: object, label: str) -> Path:
    """Reject Windows aliases that can escape lexical path comparisons."""

    del label  # Public failures deliberately never reproduce caller text or paths.
    try:
        return _storage_validate_windows_local_path(path)
    except LocalAppStorageError:
        raise LocalAppError("local application path is invalid") from None


def _path_trees_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def default_local_app_root(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Derive the one supported default root without consulting the CWD."""

    source = os.environ if environment is None else environment
    raw = _environment_value(source, "LOCALAPPDATA")
    if not isinstance(raw, str) or not raw:
        raise LocalAppError("the Windows local application data root is unavailable")
    try:
        return _storage_default_local_app_root(source)
    except LocalAppStorageError:
        raise LocalAppError("the Windows local application data root is invalid") from None


@dataclass(frozen=True, slots=True)
class LocalAppConfig:
    """Closed configuration for one standalone Windows application instance."""

    app_root: Path
    data_root: Path | None = None
    database_path: Path | None = None
    tool_root: Path | None = None
    port: int = 8000
    allow_direct_network: bool = False
    js_runtime: YtDlpJsRuntime | None = None
    cookie_config_path: Path | None = field(default=None, repr=False)
    cookie_source_config: CookieSourceConfig | None = field(
        default=None,
        repr=False,
    )
    cookie_sources: tuple[CookieSource, ...] = field(default=(), repr=False)
    max_cookie_bytes: int = DEFAULT_MAX_COOKIE_BYTES
    worker_id: str = "local-app-worker"
    open_browser: bool = True
    check_only: bool = False
    startup_timeout_seconds: float = 120.0
    poll_interval_seconds: float = 2.0
    shutdown_timeout_seconds: float = 30.0
    runtime_log_level: str = "INFO"
    runtime_log_max_bytes: int = DEFAULT_RUNTIME_LOG_MAX_BYTES
    runtime_log_backup_count: int = DEFAULT_RUNTIME_LOG_BACKUP_COUNT
    max_height: int = 1080
    max_file_bytes: int = 8 * 1024 * 1024 * 1024
    storage_min_free_bytes: int = DEFAULT_STORAGE_MIN_FREE_BYTES
    socket_timeout_seconds: int = 20
    probe_timeout_seconds: float = 90.0
    download_timeout_seconds: float = 30.0 * 60.0
    ffprobe_timeout_seconds: float = 30.0
    attempt_timeout_seconds: float = 60.0 * 60.0
    max_items_per_source: int = 20

    def __post_init__(self) -> None:
        try:
            if not _normalized_absolute_path(self.app_root):
                raise ValueError
            expected_data_root = self.app_root / "data"
            data_root = self.data_root or expected_data_root
            if data_root != expected_data_root:
                raise ValueError
            expected_database_path = data_root / "control.sqlite3"
            database_path = self.database_path or expected_database_path
            if database_path != expected_database_path:
                raise ValueError
            tool_root = self.tool_root or (
                self.app_root / "runtime-tools" / "windows-x64"
            )
            if not all(
                _normalized_absolute_path(path)
                for path in (data_root, database_path, tool_root)
            ):
                raise ValueError
            if _path_trees_overlap(data_root, tool_root):
                raise ValueError
            object.__setattr__(self, "data_root", data_root)
            object.__setattr__(self, "database_path", database_path)
            object.__setattr__(self, "tool_root", tool_root)

            if (
                isinstance(self.port, bool)
                or not isinstance(self.port, int)
                or not 1 <= self.port <= 65535
            ):
                raise ValueError
            if not all(
                isinstance(value, bool)
                for value in (
                    self.allow_direct_network,
                    self.open_browser,
                    self.check_only,
                )
            ):
                raise ValueError
            for value, minimum, maximum in (
                (self.startup_timeout_seconds, 1.0, 600.0),
                (self.poll_interval_seconds, 0.1, 60.0),
                (self.shutdown_timeout_seconds, 1.0, 300.0),
            ):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not minimum <= value <= maximum
                ):
                    raise ValueError
            if not isinstance(self.worker_id, str) or not _WORKER_ID.fullmatch(
                self.worker_id
            ):
                raise ValueError
            if not isinstance(self.cookie_sources, tuple):
                raise ValueError

            snapshot = self.cookie_source_config
            config_path = self.cookie_config_path
            if snapshot is not None and not isinstance(snapshot, CookieSourceConfig):
                raise ValueError
            if config_path is not None and not _normalized_absolute_path(config_path):
                raise ValueError
            if snapshot is not None:
                if config_path is not None and snapshot.path != config_path:
                    raise ValueError
                config_path = snapshot.path
            elif config_path is not None:
                snapshot = load_cookie_source_config(config_path)
            if snapshot is not None:
                if self.cookie_sources and self.cookie_sources != snapshot.sources:
                    raise ValueError
                object.__setattr__(self, "cookie_sources", snapshot.sources)
                object.__setattr__(self, "cookie_source_config", snapshot)
                object.__setattr__(self, "cookie_config_path", snapshot.path)
            AttemptCookieResolver(
                self.cookie_sources,
                max_cookie_bytes=self.max_cookie_bytes,
            )
            for source in self.cookie_sources:
                if not _normalized_absolute_path(source.path) or any(
                    _path_trees_overlap(source.path, root)
                    for root in (data_root, tool_root)
                ):
                    raise ValueError
            if config_path is not None and any(
                _path_trees_overlap(config_path, root)
                for root in (data_root, tool_root)
            ):
                raise ValueError

            log_config = RuntimeLogConfig(
                directory=data_root / "logs",
                level=self.runtime_log_level,
                max_bytes=self.runtime_log_max_bytes,
                backup_count=self.runtime_log_backup_count,
            )
            object.__setattr__(self, "runtime_log_level", log_config.level)
            # Reuse the Worker's complete bounds and structural validation.
            self.worker_config()
        except LocalAppError:
            raise
        except CookieSourceConfigError:
            raise LocalAppStartupError(
                LocalAppStartupFailure.COOKIE_CONFIG_INVALID
            ) from None
        except (TypeError, ValueError):
            raise LocalAppError("local application configuration is invalid") from None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def tool_root_override(self) -> Path | None:
        default = self.app_root / "runtime-tools" / "windows-x64"
        return None if self.tool_root == default else self.tool_root

    def control_settings(self) -> Settings:
        assert self.data_root is not None
        assert self.database_path is not None
        assert self.tool_root is not None
        return Settings(
            data_root=self.data_root,
            database_path=self.database_path,
            host="127.0.0.1",
            port=self.port,
            storage_min_free_bytes=self.storage_min_free_bytes,
            runtime_log_level=self.runtime_log_level,
            runtime_log_max_bytes=self.runtime_log_max_bytes,
            runtime_log_backup_count=self.runtime_log_backup_count,
            tool_root=self.tool_root,
            short_link_resolution_enabled=self.allow_direct_network,
            local_direct_short_links=self.allow_direct_network,
        )

    def worker_config(self) -> LocalWorkerConfig:
        assert self.data_root is not None
        assert self.database_path is not None
        assert self.tool_root is not None
        return LocalWorkerConfig(
            data_root=self.data_root,
            database_path=self.database_path,
            tool_root=self.tool_root,
            worker_id=self.worker_id,
            allow_direct_network=self.allow_direct_network,
            js_runtime=self.js_runtime,
            cookie_sources=self.cookie_sources,
            cookie_config_path=self.cookie_config_path,
            max_cookie_bytes=self.max_cookie_bytes,
            runtime_log_level=self.runtime_log_level,
            runtime_log_max_bytes=self.runtime_log_max_bytes,
            runtime_log_backup_count=self.runtime_log_backup_count,
            max_height=self.max_height,
            max_file_bytes=self.max_file_bytes,
            storage_min_free_bytes=self.storage_min_free_bytes,
            socket_timeout_seconds=self.socket_timeout_seconds,
            probe_timeout_seconds=self.probe_timeout_seconds,
            download_timeout_seconds=self.download_timeout_seconds,
            ffprobe_timeout_seconds=self.ffprobe_timeout_seconds,
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            max_items_per_source=self.max_items_per_source,
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        app_root: Path | None = None,
        port: int = 8000,
        allow_direct_network: bool = False,
        open_browser: bool = True,
        check_only: bool = False,
    ) -> LocalAppConfig:
        source = os.environ if environment is None else environment
        root = app_root or default_local_app_root(source)
        return cls(
            app_root=root,
            port=port,
            allow_direct_network=allow_direct_network,
            open_browser=open_browser,
            check_only=check_only,
        )

    @classmethod
    def from_args(
        cls,
        args: argparse.Namespace,
        environment: Mapping[str, str] | None = None,
    ) -> LocalAppConfig:
        """Build the complete config; the CLI remains a parsing-only adapter."""

        source = os.environ if environment is None else environment
        root = getattr(args, "app_root", None) or default_local_app_root(source)
        base = cls.from_environment(
            source,
            app_root=root,
            port=getattr(args, "port", 8000),
            allow_direct_network=getattr(args, "allow_direct_network", False),
            open_browser=not getattr(args, "no_open_browser", False),
            check_only=getattr(args, "check", False),
        )
        cookie_path = getattr(args, "cookie_config", None)
        cookie_sources = tuple(getattr(args, "cookie_source", ()) or ())
        snapshot: CookieSourceConfig | None = None
        if cookie_path is not None:
            try:
                snapshot = load_cookie_source_config(cookie_path)
            except CookieSourceConfigError:
                raise LocalAppStartupError(
                    LocalAppStartupFailure.COOKIE_CONFIG_INVALID
                ) from None
        return replace(
            base,
            tool_root=getattr(args, "tool_root", None),
            js_runtime=getattr(args, "js_runtime", None),
            cookie_config_path=cookie_path,
            cookie_source_config=snapshot,
            cookie_sources=cookie_sources,
            max_cookie_bytes=getattr(
                args,
                "max_cookie_bytes",
                base.max_cookie_bytes,
            ),
            runtime_log_level=getattr(
                args,
                "runtime_log_level",
                base.runtime_log_level,
            ),
            runtime_log_max_bytes=getattr(
                args,
                "runtime_log_max_bytes",
                base.runtime_log_max_bytes,
            ),
            runtime_log_backup_count=getattr(
                args,
                "runtime_log_backup_count",
                base.runtime_log_backup_count,
            ),
            startup_timeout_seconds=getattr(
                args,
                "startup_timeout_seconds",
                base.startup_timeout_seconds,
            ),
            poll_interval_seconds=getattr(
                args,
                "poll_interval_seconds",
                base.poll_interval_seconds,
            ),
            shutdown_timeout_seconds=getattr(
                args,
                "shutdown_timeout_seconds",
                base.shutdown_timeout_seconds,
            ),
        )


def _parse_strict_json(raw: bytes, *, max_bytes: int) -> object:
    if not raw or len(raw) > max_bytes:
        raise ValueError

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _validate_ready_message(
    message: object,
    *,
    role: str | None = None,
    phase: str | None = None,
    pid: int | None = None,
    run_id: str | None = None,
    product_identity: str | None = None,
    app_version: str = __version__,
    schema_version: int = SCHEMA_VERSION,
    expected: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate one exact, bounded child-to-parent readiness object."""

    try:
        received = (
            _parse_strict_json(message, max_bytes=_MAX_HANDSHAKE_BYTES)
            if isinstance(message, bytes)
            else message
        )
        if not isinstance(received, dict) or set(received) != _HANDSHAKE_KEYS:
            raise ValueError
        if (
            received["protocol"] != _PROTOCOL_VERSION
            or received["status"] != "ready"
            or received["role"] not in _CHILD_ROLES
            or received["phase"] not in _ROLE_PHASES[received["role"]]
            or isinstance(received["pid"], bool)
            or not isinstance(received["pid"], int)
            or received["pid"] <= 0
            or not isinstance(received["run_id"], str)
            or not _RUN_ID.fullmatch(received["run_id"])
            or not isinstance(received["app_version"], str)
            or not _VERSION.fullmatch(received["app_version"])
            or isinstance(received["schema_version"], bool)
            or not isinstance(received["schema_version"], int)
            or received["schema_version"] <= 0
            or not isinstance(received["product_identity"], str)
            or not _PRODUCT_IDENTITY.fullmatch(received["product_identity"])
        ):
            raise ValueError
        if expected is not None:
            if set(expected) != _HANDSHAKE_KEYS or received != dict(expected):
                raise ValueError
        else:
            if None in (role, phase, pid, run_id, product_identity):
                raise ValueError
            if received != {
                "protocol": _PROTOCOL_VERSION,
                "status": "ready",
                "role": role,
                "phase": phase,
                "run_id": run_id,
                "pid": pid,
                "app_version": app_version,
                "schema_version": schema_version,
                "product_identity": product_identity,
            }:
                raise ValueError
        return dict(received)
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise LocalAppError("a local application child failed readiness") from None


def _validate_control_health(
    health_payload: object,
    capability_payload: object,
    *,
    schema_version: int = SCHEMA_VERSION,
    product_identity: str | None = None,
    expected_product_identity: str | None = None,
) -> None:
    """Validate direct control ownership, including an allowed paused queue."""

    expected_identity = product_identity or expected_product_identity
    try:
        if not isinstance(expected_identity, str):
            raise ValueError
        if not isinstance(health_payload, dict) or set(health_payload) != {
            "status",
            "database",
            "schema_version",
            "worker",
            "detail",
        }:
            raise ValueError
        if (
            health_payload["database"] != "ok"
            or isinstance(health_payload["schema_version"], bool)
            or health_payload["schema_version"] != schema_version
        ):
            raise ValueError
        paused = (
            health_payload["status"] == "degraded"
            and health_payload["worker"] == "paused"
            and isinstance(health_payload["detail"], str)
            and health_payload["detail"].startswith("queue_paused:")
            and health_payload["detail"].removeprefix("queue_paused:")
            in {error.value for error in ErrorCode}
        )
        healthy = (
            health_payload["status"] == "ok"
            and health_payload["worker"] == "external_status_unknown"
            and health_payload["detail"] is None
        )
        if not (healthy or paused):
            raise ValueError
        if not isinstance(capability_payload, dict) or set(capability_payload) != {
            "current_product_identity",
            "implementations",
            "evidence",
            "decisions",
            "evidence_total",
            "decision_total",
            "evidence_truncated",
            "decision_truncated",
        }:
            raise ValueError
        if capability_payload["current_product_identity"] != expected_identity:
            raise ValueError
        if not all(
            isinstance(capability_payload[name], list)
            for name in ("implementations", "evidence", "decisions")
        ):
            raise ValueError
        if not all(
            isinstance(capability_payload[name], int)
            and not isinstance(capability_payload[name], bool)
            and capability_payload[name] >= 0
            for name in ("evidence_total", "decision_total")
        ):
            raise ValueError
        if not all(
            isinstance(capability_payload[name], bool)
            for name in ("evidence_truncated", "decision_truncated")
        ):
            raise ValueError
        return None
    except (KeyError, TypeError, ValueError):
        raise LocalAppError("the local control identity check failed") from None


def _sanitized_child_environment(
    inherited: Mapping[str, str] | LocalAppConfig,
    legacy_inherited: Mapping[str, str] | None = None,
    *,
    config: LocalAppConfig | None = None,
    enable_worker: bool = False,
) -> dict[str, str]:
    """Rebuild a spawn environment; configuration itself stays on private IPC."""

    # Accept the early `(config, environment)` seam without propagating its paths.
    if isinstance(inherited, LocalAppConfig):
        config = inherited
        source = legacy_inherited or os.environ
    else:
        source = inherited
    del config
    sanitized: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in source.items():
        normalized = key.upper()
        control_secret = (
            not enable_worker
            and normalized in _CONTROL_CHILD_SECRET_ENVIRONMENT_ALLOWLIST
        )
        if (
            normalized in _CHILD_ENVIRONMENT_ALLOWLIST or control_secret
        ) and normalized not in seen:
            valid = (
                isinstance(value, str)
                and "\x00" not in value
                and len(value)
                <= (
                    _MAX_CONTROL_CHILD_SECRET_CHARACTERS
                    if control_secret
                    else 4096
                )
            )
            if control_secret:
                # The browser-facing control process owns AI execution.  Keep
                # its one declared provider secret out of the download Worker,
                # and apply the same value contract used by AiTaskExecutor.
                valid = valid and bool(value) and not any(
                    ord(character) < 33 for character in value
                )
            if valid:
                sanitized[normalized if control_secret else key] = value
                seen.add(normalized)
    if enable_worker:
        sanitized[FEATURE_GATE] = "1"
    return sanitized


def _reserve_loopback_socket(
    port_or_host: int | str,
    legacy_port: int | None = None,
) -> socket.socket:
    """Exclusively pre-bind the numeric IPv4 loopback listener."""

    host = "127.0.0.1"
    port = port_or_host
    if isinstance(port_or_host, str):
        host = port_or_host
        port = legacy_port
    listener: socket.socket | None = None
    try:
        if host != "127.0.0.1" or isinstance(port, bool) or not isinstance(port, int):
            raise ValueError
        if not 1 <= port <= 65535 or not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            raise ValueError
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((host, port))
        listener.listen(2048)
        listener.set_inheritable(False)
        return listener
    except (OSError, TypeError, ValueError):
        if listener is not None:
            listener.close()
        raise LocalAppStartupError(
            LocalAppStartupFailure.PORT_UNAVAILABLE
        ) from None


def _plain_directory_info(path: Path) -> os.stat_result:
    return _storage_plain_directory_info(path)


def _ensure_plain_directory_tree(path: Path) -> None:
    try:
        _storage_ensure_plain_directory_tree(path)
    except LocalAppStorageError:
        raise LocalAppError("the local application storage is unavailable") from None


def _reject_existing_link_components(path: Path) -> None:
    try:
        _storage_reject_existing_link_components(path)
    except LocalAppStorageError:
        raise LocalAppError("the local application storage is unavailable") from None


def _require_plain_directory_tree(path: Path) -> None:
    """Require an existing link-free directory without creating anything."""

    try:
        _reject_existing_link_components(path)
        _plain_directory_info(path)
    except (LocalAppError, OSError):
        raise LocalAppStartupError(
            LocalAppStartupFailure.TOOLCHAIN_UNAVAILABLE
        ) from None


@contextmanager
def _exclusive_local_app(app_root: Path) -> Iterator[None]:
    """Hold one Windows byte-range lock for the whole supervisor lifetime."""

    try:
        with _storage_exclusive_local_app(app_root):
            yield
    except LocalAppLockUnavailable:
        raise LocalAppError("another local application instance is already active") from None


def _open_browser(url: str) -> None:
    if os.name != "nt" or not re.fullmatch(
        r"http://127\.0\.0\.1:(?:[1-9][0-9]{0,4})/",
        url,
    ):
        raise LocalAppError("the local application browser URL is invalid")
    os.startfile(url)  # type: ignore[attr-defined]


def _runtime_logger(config: LocalAppConfig, *, run_id: str) -> RuntimeLogger:
    assert config.data_root is not None
    return RuntimeLogger(
        component="local-app",
        config=RuntimeLogConfig(
            directory=config.data_root / "logs",
            level=config.runtime_log_level,
            max_bytes=config.runtime_log_max_bytes,
            backup_count=config.runtime_log_backup_count,
        ),
        run_id=run_id,
    )


def _handshake_payload(
    *,
    status: str,
    role: str,
    phase: str,
    run_id: str,
    product_identity: str,
) -> dict[str, object]:
    return {
        "protocol": _PROTOCOL_VERSION,
        "status": status,
        "role": role,
        "phase": phase,
        "run_id": run_id,
        "pid": os.getpid(),
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "product_identity": product_identity,
    }


def _send_handshake(connection: Connection, **fields: object) -> None:
    payload = json.dumps(
        _handshake_payload(**fields),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _MAX_HANDSHAKE_BYTES:
        raise RuntimeError("local application handshake is unavailable")
    connection.send_bytes(payload)


def _apply_child_environment(config: LocalAppConfig, *, enable_worker: bool) -> None:
    environment = _sanitized_child_environment(
        os.environ,
        config=config,
        enable_worker=enable_worker,
    )
    os.environ.clear()
    os.environ.update(environment)


def _ignore_supervisor_signals() -> None:
    """Keep Ctrl+C in the supervisor; owned children use command-pipe EOF."""

    if multiprocessing.parent_process() is None:
        return
    for name in ("SIGINT", "SIGBREAK"):
        child_signal = getattr(signal, name, None)
        if child_signal is not None:
            signal.signal(child_signal, signal.SIG_IGN)


@contextmanager
def _supervisor_signal_handlers() -> Iterator[None]:
    """Translate Windows console breaks into the normal shutdown path."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[signal.Signals, Any] = {}
    try:
        for name in ("SIGINT", "SIGBREAK"):
            supervisor_signal = getattr(signal, name, None)
            if supervisor_signal is None:
                continue
            previous[supervisor_signal] = signal.getsignal(supervisor_signal)
            signal.signal(supervisor_signal, signal.default_int_handler)
        yield
    finally:
        for supervisor_signal, handler in previous.items():
            signal.signal(supervisor_signal, handler)


def _parent_is_alive() -> bool:
    try:
        parent = multiprocessing.parent_process()
        return parent is None or parent.is_alive()
    except (AssertionError, OSError):
        return False


def _send_child_command(connection: Connection, command: bytes) -> None:
    if command not in {
        _COMMAND_LAUNCH,
        _COMMAND_CLAIM,
        _COMMAND_CHECK_COMPLETE,
    }:
        raise LocalAppError("local application child command is invalid")
    try:
        connection.send_bytes(command)
    except (BrokenPipeError, EOFError, OSError):
        raise LocalAppError(
            "a local application child stopped before it was ready"
        ) from None


def _wait_for_child_command(
    connection: Connection,
    expected_command: bytes,
) -> bool:
    """Wait for one strict command, or return False when the owner disappears."""

    while True:
        try:
            available = connection.poll(0.1)
        except (EOFError, OSError):
            return False
        if not available:
            if not _parent_is_alive():
                return False
            continue
        try:
            command = connection.recv_bytes(_MAX_COMMAND_BYTES)
        except EOFError:
            return False
        except OSError:
            raise RuntimeError("local application child command is invalid") from None
        if command != expected_command:
            raise RuntimeError("local application child command is invalid")
        return True


def _command_channel_closed(connection: Connection, timeout_seconds: float) -> bool:
    """Return True when the supervisor closes a child's command channel."""

    try:
        available = connection.poll(max(timeout_seconds, 0.0))
    except (EOFError, OSError):
        return True
    if not available:
        return not _parent_is_alive()
    try:
        # Launch/claim are consumed before the resident loop. Any later data or
        # EOF is therefore a stop request. Unlike multiprocessing.Event, this
        # cannot retain a shared condition lock when a child is terminated.
        connection.recv_bytes(_MAX_COMMAND_BYTES)
    except (EOFError, OSError):
        pass
    return True


class _NoSignalUvicornServer(uvicorn.Server):
    """Uvicorn server whose supervisor, not the child, owns OS signals."""

    def __init__(self, *args: object, ready_callback: Any, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._ready_callback = ready_callback

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self._ready_callback()


def _control_child_main(
    config: LocalAppConfig,
    listener: socket.socket,
    ready_connection: Connection,
    command_connection: Connection,
    run_id: str,
    expected_identity: str,
    managed_worker_status: ManagedWorkerRuntimeStatus | None = None,
) -> None:
    runtime_logger: RuntimeLogger | None = None
    phase = "control_ready"
    try:
        _apply_child_environment(config, enable_worker=False)
        _ignore_supervisor_signals()
        if not _wait_for_child_command(command_connection, _COMMAND_LAUNCH):
            return
        observed_identity = current_product_identity()
        if observed_identity != expected_identity:
            raise RuntimeError("local application build identity changed")
        settings = config.control_settings()
        assert config.data_root is not None
        runtime_logger = RuntimeLogger(
            component="control",
            config=RuntimeLogConfig(
                directory=config.data_root / "logs",
                level=config.runtime_log_level,
                max_bytes=config.runtime_log_max_bytes,
                backup_count=config.runtime_log_backup_count,
            ),
            run_id=run_id,
        )
        if (
            not config.check_only
            and config.cookie_source_config is not None
            and config.cookie_source_config.default_cookie_platforms
        ):
            config.data_root.mkdir(parents=True, exist_ok=True)
            database = Database(settings.database_path)
            database.initialize()
            defaults = prepare_credential_defaults(
                database,
                config.cookie_source_config,
                run_id=run_id,
                max_cookie_bytes=config.max_cookie_bytes,
            )
            app = create_app(
                settings, runtime_logger=runtime_logger, credential_defaults=defaults,
                managed_worker_status=managed_worker_status,
            )
        else:
            app = create_app(settings, runtime_logger=runtime_logger,
                             managed_worker_status=managed_worker_status)
        if runtime_logger.status()["status"] != "ok":
            raise RuntimeError("local control runtime log is unavailable")
        uvicorn_config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=config.port,
            access_log=False,
            log_config=None,
            log_level="critical",
            lifespan="on",
        )

        def ready() -> None:
            _send_handshake(
                ready_connection,
                status="ready",
                role="control",
                phase=phase,
                run_id=run_id,
                product_identity=observed_identity,
            )

        server = _NoSignalUvicornServer(uvicorn_config, ready_callback=ready)

        def observe_stop() -> None:
            while not _command_channel_closed(command_connection, 0.1):
                pass
            server.should_exit = True

        threading.Thread(target=observe_stop, daemon=True).start()
        asyncio.run(server.serve(sockets=[listener]))
    except BaseException as exc:
        if runtime_logger is not None:
            runtime_logger.emit(
                "control.startup_failed",
                level="ERROR",
                exception_type=safe_exception_type(exc),
            )
        try:
            _send_handshake(
                ready_connection,
                status="failed",
                role="control",
                phase=phase,
                run_id=run_id,
                product_identity=expected_identity,
            )
        except BaseException:
            pass
        raise SystemExit(1) from None
    finally:
        ready_connection.close()
        command_connection.close()
        listener.close()


def _worker_child_main(
    config: LocalAppConfig,
    ready_connection: Connection,
    command_connection: Connection,
    run_id: str,
    expected_identity: str,
) -> None:
    worker_config: LocalWorkerConfig | None = None
    runtime_logger: RuntimeLogger | None = None
    last_phase = "worker_preflight_ready"
    stop_reason = "runtime_error"
    preflight_complete = False
    cycle_failure_logged = False
    try:
        _apply_child_environment(config, enable_worker=True)
        _ignore_supervisor_signals()
        if not _wait_for_child_command(command_connection, _COMMAND_LAUNCH):
            stop_reason = "supervisor_shutdown"
            return
        worker_config = config.worker_config()
        observed_identity = current_product_identity()
        if observed_identity != expected_identity:
            raise RuntimeError("local application build identity changed")
        assert config.data_root is not None
        with _exclusive_local_worker(config.data_root):
            runtime_logger = _local_worker_runtime_logger(worker_config)
            if isinstance(runtime_logger, RuntimeLogger):
                # The reused Worker logger has emitted nothing yet, so adopting
                # the supervisor nonce gives all three process logs one run id.
                runtime_logger.run_id = run_id
            if not runtime_logger.emit(
                "worker.preflight_started",
                worker_id=worker_config.worker_id,
                adapter="yt_dlp",
                direct_network_enabled=True,
                js_runtime_enabled=worker_config.js_runtime is not None,
            ):
                raise RuntimeError("local Worker runtime log is unavailable")
            if config.cookie_source_config is not None:
                revalidate_cookie_source_config(config.cookie_source_config)
            worker = build_local_worker(
                worker_config,
                runtime_logger=runtime_logger,
                claim_gate_run_id=run_id,
            )
            if not runtime_logger.emit(
                "worker.preflight_succeeded",
                worker_id=worker_config.worker_id,
            ):
                raise RuntimeError("local Worker runtime log is unavailable")
            preflight_complete = True
            _send_handshake(
                ready_connection,
                status="ready",
                role="worker",
                phase=last_phase,
                run_id=run_id,
                product_identity=observed_identity,
            )
            next_command = (
                _COMMAND_CHECK_COMPLETE if config.check_only else _COMMAND_CLAIM
            )
            if not _wait_for_child_command(command_connection, next_command):
                stop_reason = "supervisor_shutdown"
                return
            if config.check_only:
                stop_reason = "check_complete"
                return
            if config.cookie_source_config is not None:
                revalidate_cookie_source_config(config.cookie_source_config)
            if not runtime_logger.emit(
                "worker.started",
                worker_id=worker_config.worker_id,
                adapter="yt_dlp",
                direct_network_enabled=True,
                js_runtime_enabled=worker_config.js_runtime is not None,
            ):
                raise RuntimeError("local Worker runtime log is unavailable")
            last_phase = "worker_ready"
            _send_handshake(
                ready_connection,
                status="ready",
                role="worker",
                phase=last_phase,
                run_id=run_id,
                product_identity=observed_identity,
            )
            cycle_started = time.perf_counter()
            try:
                run_concurrent_worker(
                    worker,
                    poll_interval_seconds=config.poll_interval_seconds,
                    wait_for_stop=lambda timeout: _command_channel_closed(
                        command_connection, timeout,
                    ),
                )
            except BaseException as exc:
                runtime_logger.emit(
                    "worker.cycle_failed",
                    level="ERROR",
                    worker_id=worker_config.worker_id,
                    exception_type=safe_exception_type(exc),
                    duration_ms=min(
                        max((time.perf_counter() - cycle_started) * 1000.0, 0.0),
                        86_400_000.0,
                    ),
                )
                cycle_failure_logged = True
                raise
            stop_reason = "supervisor_shutdown"
    except BaseException as exc:
        if runtime_logger is not None:
            assert worker_config is not None
            if not preflight_complete:
                runtime_logger.emit(
                    "worker.preflight_failed",
                    level="ERROR",
                    worker_id=worker_config.worker_id,
                    exception_type=safe_exception_type(exc),
                )
            elif not cycle_failure_logged:
                runtime_logger.emit(
                    "worker.cycle_failed",
                    level="ERROR",
                    worker_id=worker_config.worker_id,
                    exception_type=safe_exception_type(exc),
                    duration_ms=0.0,
                )
        try:
            _send_handshake(
                ready_connection,
                status="failed",
                role="worker",
                phase=last_phase,
                run_id=run_id,
                product_identity=expected_identity,
            )
        except BaseException:
            pass
        raise SystemExit(1) from None
    finally:
        if runtime_logger is not None:
            assert worker_config is not None
            runtime_logger.emit(
                "worker.stopped",
                worker_id=worker_config.worker_id,
                reason=stop_reason,
            )
        ready_connection.close()
        command_connection.close()


@contextmanager
def _temporary_process_environment(environment: Mapping[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environment)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _start_owned_child(
    process: multiprocessing.Process,
    *,
    environment: Mapping[str, str],
    job: WindowsKillOnCloseJob,
) -> None:
    try:
        with _temporary_process_environment(environment):
            process.start()
        if process.pid is None:
            raise RuntimeError
        job.assign_pid(process.pid)
    except BaseException:
        try:
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(5.0)
        except (AssertionError, OSError, ValueError):
            pass
        raise LocalAppError("a local application child could not be started") from None


def _await_ready(
    connection: Connection,
    process: multiprocessing.Process,
    *,
    role: str,
    phase: str,
    run_id: str,
    product_identity: str,
    deadline: float,
) -> dict[str, object]:
    while time.monotonic() < deadline:
        if connection.poll(min(0.1, max(deadline - time.monotonic(), 0.0))):
            try:
                payload = connection.recv_bytes(_MAX_HANDSHAKE_BYTES)
            except (EOFError, OSError):
                break
            return _validate_ready_message(
                payload,
                role=role,
                phase=phase,
                pid=process.pid,
                run_id=run_id,
                product_identity=product_identity,
            )
        if not process.is_alive():
            break
    raise LocalAppError("a local application child failed readiness")


def _strict_http_json(
    port: int,
    path: str,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, object]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise LocalAppError("the local control identity check failed")
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=min(timeout_seconds, 5.0),
    )
    try:
        connection.request(
            "GET",
            path,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip()
        length = response.getheader("Content-Length")
        if (
            response.status != 200
            or content_type != "application/json"
            or (
                length is not None
                and (not length.isdigit() or int(length) > _MAX_HTTP_BODY_BYTES)
            )
        ):
            raise ValueError
        raw = response.read(_MAX_HTTP_BODY_BYTES + 1)
        if len(raw) > _MAX_HTTP_BODY_BYTES:
            raise ValueError
        payload = _parse_strict_json(raw, max_bytes=_MAX_HTTP_BODY_BYTES)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (OSError, http.client.HTTPException, UnicodeError, ValueError):
        raise LocalAppError("the local control identity check failed") from None
    finally:
        connection.close()


def _startup_time_remaining(deadline: float | None) -> float:
    if deadline is None:
        return 5.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LocalAppError("local application startup timed out")
    return min(remaining, 5.0)


def _verify_control(
    port: int,
    product_identity: str,
    *,
    deadline: float | None = None,
) -> None:
    health = _strict_http_json(
        port,
        "/health",
        timeout_seconds=_startup_time_remaining(deadline),
    )
    capability = _strict_http_json(
        port,
        "/api/v1/capability-snapshot?limit=1",
        timeout_seconds=_startup_time_remaining(deadline),
    )
    _validate_control_health(
        health,
        capability,
        schema_version=SCHEMA_VERSION,
        product_identity=product_identity,
    )


def _supervisor_claim_gate_repository(
    config: LocalAppConfig,
) -> WorkerRepository:
    """Open the supervisor's independent SQLite control path."""

    assert config.database_path is not None
    return WorkerRepository(Database(config.database_path))


def _exit_category(process: multiprocessing.Process) -> str:
    exit_code = process.exitcode
    if exit_code is None:
        return "unknown"
    if exit_code == 0:
        return "clean"
    if exit_code < 0:
        return "terminated"
    return "error"


def _join_until(process: multiprocessing.Process, deadline: float) -> None:
    if process.pid is None:
        return
    remaining = max(0.0, deadline - time.monotonic())
    process.join(remaining)


def _cleanup_children(
    *,
    worker: multiprocessing.Process | None,
    control: multiprocessing.Process | None,
    worker_command: Connection,
    control_command: Connection,
    job: WindowsKillOnCloseJob,
    timeout_seconds: float,
    claim_gate_repository: WorkerRepository | None = None,
    claim_gate_run_id: str | None = None,
    runtime_logger: RuntimeLogger | None = None,
    worker_id: str | None = None,
) -> bool:
    """Request Worker-first shutdown, then kill only the owned Job if needed."""

    deadline = time.monotonic() + timeout_seconds
    forced = False
    gate_stop_failed = False
    if claim_gate_repository is not None:
        if claim_gate_run_id is None:
            gate_stop_failed = True
        else:
            try:
                gate_stopped = claim_gate_repository.stop_claim_gate(
                    run_id=claim_gate_run_id,
                    now=datetime.now(UTC),
                )
                if runtime_logger is not None and worker_id is not None:
                    if gate_stopped:
                        runtime_logger.emit(
                            "local_app.claim_gate_stopped",
                            worker_id=worker_id,
                            gate_state="stopped",
                        )
                    else:
                        runtime_logger.emit(
                            "local_app.claim_gate_stopped",
                            level="WARNING",
                            worker_id=worker_id,
                            gate_state="fenced",
                        )
            except Exception:
                gate_stop_failed = True
        if gate_stop_failed:
            # If SQLite cannot establish the stop-before-claim ordering, kill
            # the Job before touching the advisory Pipe. The owned processes
            # then cannot cross the uncertain gate state and claim more work.
            forced = True
            if runtime_logger is not None and worker_id is not None:
                runtime_logger.emit(
                    "local_app.claim_gate_stopped",
                    level="ERROR",
                    worker_id=worker_id,
                    gate_state="stop_failed",
                )
            try:
                job.terminate()
            except WindowsJobError:
                for process in (worker, control):
                    if (
                        process is not None
                        and process.pid is not None
                        and process.is_alive()
                    ):
                        try:
                            process.terminate()
                        except OSError:
                            pass
            if runtime_logger is not None:
                runtime_logger.emit(
                    "local_app.forced_shutdown",
                    level="WARNING",
                    cause="gate_stop_failed",
                    failure_site="claim_gate_stop",
                )
    # Closing a one-way command channel is a non-blocking stop signal. Unlike
    # multiprocessing.Event.set(), it cannot wait forever on a condition lock
    # abandoned by an abruptly terminated child.
    worker_command.close()
    if worker is not None:
        _join_until(worker, deadline)
    control_command.close()
    if control is not None:
        _join_until(control, deadline)
    alive = [
        process
        for process in (worker, control)
        if process is not None and process.pid is not None and process.is_alive()
    ]
    if not alive:
        return forced
    if not gate_stop_failed:
        forced = True
        if runtime_logger is not None:
            runtime_logger.emit(
                "local_app.forced_shutdown",
                level="WARNING",
                cause="child_timeout",
                failure_site="child_shutdown",
            )
        try:
            job.terminate()
        except WindowsJobError:
            for process in alive:
                try:
                    process.terminate()
                except OSError:
                    pass
    else:
        for process in alive:
            try:
                process.terminate()
            except OSError:
                pass
    for process in alive:
        process.join(5.0)
    return forced


def _create_supervisor_ipc(context: Any) -> tuple[Any, ...]:
    """Create private child channels while closing any partial allocation."""

    connections: list[Connection] = []
    try:
        control_receive, control_send = context.Pipe(duplex=False)
        connections.extend((control_receive, control_send))
        worker_receive, worker_send = context.Pipe(duplex=False)
        connections.extend((worker_receive, worker_send))
        control_command_receive, control_command_send = context.Pipe(duplex=False)
        connections.extend((control_command_receive, control_command_send))
        worker_command_receive, worker_command_send = context.Pipe(duplex=False)
        connections.extend((worker_command_receive, worker_command_send))
        return (
            control_receive,
            control_send,
            worker_receive,
            worker_send,
            control_command_receive,
            control_command_send,
            worker_command_receive,
            worker_command_send,
        )
    except BaseException:
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        raise LocalAppError("local application process IPC is unavailable") from None


def _require_windows_host() -> None:
    if os.name != "nt" or sys.platform != "win32":
        raise LocalAppError("the standalone local application requires Windows")


def _prepare_layout(config: LocalAppConfig) -> None:
    assert config.data_root is not None
    _ensure_plain_directory_tree(config.app_root)
    _ensure_plain_directory_tree(config.data_root)
    _ensure_plain_directory_tree(config.data_root / "logs")


def _emit_required(logger: RuntimeLogger, event: str, **fields: object) -> None:
    if not logger.emit(event, **fields):
        raise LocalAppError("the local application runtime log is unavailable")


def _run_local_app_with_listener(
    config: LocalAppConfig,
    listener: socket.socket,
) -> int:
    assert config.tool_root is not None
    # An invalid or linked tool root must not cause a fresh database or runtime
    # log tree to be created before the Worker rejects it.
    _require_plain_directory_tree(config.tool_root)
    _prepare_layout(config)
    if config.cookie_source_config is not None:
        try:
            revalidate_cookie_source_config(config.cookie_source_config)
        except CookieSourceConfigError:
            raise LocalAppStartupError(
                LocalAppStartupFailure.COOKIE_CONFIG_INVALID
            ) from None
    try:
        product_identity = current_product_identity()
    except Exception:
        raise LocalAppError("the local application build identity is unavailable") from None

    run_id = uuid4().hex
    assert config.data_root is not None
    with _exclusive_local_app(config.app_root):
        logger = _runtime_logger(config, run_id=run_id)
        _emit_required(
            logger,
            "local_app.initializing",
            app_version=__version__,
            app_schema_version=SCHEMA_VERSION,
            port=config.port,
            direct_network_enabled=True,
            browser_enabled=config.open_browser and not config.check_only,
            check_only=config.check_only,
        )
        try:
            job = WindowsKillOnCloseJob()
        except WindowsJobError:
            logger.emit(
                "local_app.shutdown_requested",
                level="ERROR",
                reason="startup_failed",
            )
            logger.emit("local_app.stopped", reason="startup_failed")
            raise LocalAppError("Windows process ownership is unavailable") from None

        try:
            context = multiprocessing.get_context("spawn")
            (
                control_receive,
                control_send,
                worker_receive,
                worker_send,
                control_command_receive,
                control_command_send,
                worker_command_receive,
                worker_command_send,
            ) = _create_supervisor_ipc(context)
        except BaseException:
            job.close()
            logger.emit(
                "local_app.shutdown_requested",
                level="ERROR",
                reason="startup_failed",
            )
            logger.emit("local_app.stopped", reason="startup_failed")
            raise LocalAppError("local application process IPC is unavailable") from None
        control: multiprocessing.Process | None = None
        worker: multiprocessing.Process | None = None
        claim_gate_repository: WorkerRepository | None = None
        failure: LocalAppError | None = None
        stop_reason = "normal"
        forced = False
        managed_worker_status: ManagedWorkerRuntimeStatus | None = None
        try:
            managed_worker_status = ManagedWorkerRuntimeStatus(run_id=run_id, product_identity=product_identity)
            managed_worker_status.publish('check_only' if config.check_only else 'starting')
            startup_deadline = time.monotonic() + config.startup_timeout_seconds
            control = context.Process(
                target=_control_child_main,
                name="video-download-control-local-control",
                args=(
                    config,
                    listener,
                    control_send,
                    control_command_receive,
                    run_id,
                    product_identity,
                    managed_worker_status,
                ),
            )
            worker = context.Process(
                target=_worker_child_main,
                name="video-download-control-local-worker",
                args=(
                    config,
                    worker_send,
                    worker_command_receive,
                    run_id,
                    product_identity,
                ),
            )
            _start_owned_child(
                control,
                environment=_sanitized_child_environment(
                    os.environ,
                    config=config,
                    enable_worker=False,
                ),
                job=job,
            )
            _start_owned_child(
                worker,
                environment=_sanitized_child_environment(
                    os.environ,
                    config=config,
                    enable_worker=True,
                ),
                job=job,
            )
            control_send.close()
            worker_send.close()
            control_command_receive.close()
            worker_command_receive.close()
            # Parent no longer owns a listening descriptor after spawn transfer.
            listener.close()
            _send_child_command(control_command_send, _COMMAND_LAUNCH)
            _await_ready(
                control_receive,
                control,
                role="control",
                phase="control_ready",
                run_id=run_id,
                product_identity=product_identity,
                deadline=startup_deadline,
            )
            _emit_required(
                logger,
                "local_app.child_ready",
                child_role="control",
                app_phase="control_ready",
            )
            _verify_control(
                config.port,
                product_identity,
                deadline=startup_deadline,
            )
            claim_gate_repository = _supervisor_claim_gate_repository(config)
            try:
                claim_gate_repository.prepare_claim_gate(
                    run_id=run_id,
                    worker_id=config.worker_id,
                    now=datetime.now(UTC),
                )
            except Exception:
                logger.emit(
                    "local_app.claim_gate_prepared",
                    level="ERROR",
                    worker_id=config.worker_id,
                    gate_state="prepare_failed",
                )
                raise
            _emit_required(
                logger,
                "local_app.claim_gate_prepared",
                worker_id=config.worker_id,
                gate_state="prepared",
            )
            _send_child_command(worker_command_send, _COMMAND_LAUNCH)
            _await_ready(
                worker_receive,
                worker,
                role="worker",
                phase="worker_preflight_ready",
                run_id=run_id,
                product_identity=product_identity,
                deadline=startup_deadline,
            )
            _emit_required(
                logger,
                "local_app.child_ready",
                child_role="worker",
                app_phase="worker_preflight_ready",
            )
            _verify_control(
                config.port,
                product_identity,
                deadline=startup_deadline,
            )
            if config.check_only:
                _send_child_command(
                    worker_command_send,
                    _COMMAND_CHECK_COMPLETE,
                )
                print(
                    json.dumps(
                        {"status": "checked"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                stop_reason = "check_complete"
            else:
                if config.cookie_source_config is not None:
                    revalidate_cookie_source_config(config.cookie_source_config)
                try:
                    claim_gate_repository.activate_claim_gate(
                        run_id=run_id,
                        worker_id=config.worker_id,
                        now=datetime.now(UTC),
                    )
                except Exception:
                    logger.emit(
                        "local_app.claim_gate_activated",
                        level="ERROR",
                        worker_id=config.worker_id,
                        gate_state="activate_failed",
                    )
                    raise
                _emit_required(
                    logger,
                    "local_app.claim_gate_activated",
                    worker_id=config.worker_id,
                    gate_state="active",
                )
                _send_child_command(worker_command_send, _COMMAND_CLAIM)
                _await_ready(
                    worker_receive,
                    worker,
                    role="worker",
                    phase="worker_ready",
                    run_id=run_id,
                    product_identity=product_identity,
                    deadline=startup_deadline,
                )
                _emit_required(
                    logger,
                    "local_app.child_ready",
                    child_role="worker",
                    app_phase="worker_ready",
                )
                managed_worker_status.publish('online', worker_pid=worker.pid)
                _verify_control(
                    config.port,
                    product_identity,
                    deadline=startup_deadline,
                )
                if config.open_browser:
                    try:
                        _open_browser(config.url)
                    except Exception as exc:
                        logger.emit(
                            "local_app.browser_open_failed",
                            level="WARNING",
                            exception_type=safe_exception_type(exc),
                        )
                _emit_required(
                    logger,
                    "local_app.ready",
                    port=config.port,
                    browser_enabled=config.open_browser,
                )
                print(
                    json.dumps(
                        {"status": "ready", "url": config.url},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                while control.is_alive() and worker.is_alive():
                    managed_worker_status.publish('online', worker_pid=worker.pid)
                    time.sleep(0.2)
                failed_child = control if not control.is_alive() else worker
                failed_role = "control" if failed_child is control else "worker"
                logger.emit(
                    "local_app.child_exited",
                    level="ERROR",
                    child_role=failed_role,
                    exit_category=_exit_category(failed_child),
                )
                stop_reason = "child_failed"
                failure = LocalAppError(
                    "a local application child stopped unexpectedly; inspect the runtime log"
                )
        except KeyboardInterrupt:
            stop_reason = "normal"
            logger.emit(
                "local_app.shutdown_requested",
                reason="supervisor_shutdown",
            )
        except Exception as exc:
            stop_reason = "startup_failed"
            failure = (
                exc
                if isinstance(exc, LocalAppError)
                else LocalAppError(
                    "the local application could not start; inspect the runtime log"
                )
            )
            logger.emit(
                "local_app.shutdown_requested",
                level="ERROR",
                reason="startup_failed",
            )
        finally:
            if managed_worker_status is not None:
                managed_worker_status.publish('stopping', worker_pid=worker.pid if worker is not None else None)
            try:
                forced = _cleanup_children(
                    worker=worker,
                    control=control,
                    worker_command=worker_command_send,
                    control_command=control_command_send,
                    job=job,
                    timeout_seconds=config.shutdown_timeout_seconds,
                    claim_gate_repository=claim_gate_repository,
                    claim_gate_run_id=(
                        run_id if claim_gate_repository is not None else None
                    ),
                    runtime_logger=logger,
                    worker_id=config.worker_id,
                )
            finally:
                for connection in (
                    control_receive,
                    control_send,
                    worker_receive,
                    worker_send,
                    control_command_receive,
                    control_command_send,
                    worker_command_receive,
                    worker_command_send,
                ):
                    try:
                        connection.close()
                    except OSError:
                        pass
                listener.close()
                job.close()
                if managed_worker_status is not None:
                    managed_worker_status.publish('stopped')
            if forced:
                if stop_reason == "normal":
                    stop_reason = "forced_shutdown"
            logger.emit("local_app.stopped", reason=stop_reason)
        if failure is not None:
            raise failure from None
        return 0


def run_local_app(config: LocalAppConfig) -> int:
    """Run both owned children until Ctrl+C or fail closed with a safe error."""

    # Host rejection intentionally precedes every filesystem, socket, DB,
    # process and browser action.
    _require_windows_host()
    if not isinstance(config, LocalAppConfig):
        raise LocalAppError("local application configuration is invalid")
    if not config.allow_direct_network:
        raise LocalAppError("local application requires --allow-direct-network")
    with _supervisor_signal_handlers():
        try:
            # Port ownership is established before the application creates its
            # root or database.  An old listener can therefore never be
            # mistaken for our child.
            listener = _reserve_loopback_socket(config.port)
            try:
                return _run_local_app_with_listener(config, listener)
            finally:
                listener.close()
        except KeyboardInterrupt:
            # Interrupts after children exist are handled inside the lifecycle
            # block.  This catches the smaller pre-spawn/pre-log window while
            # still returning a normal operator-requested shutdown status.
            return 0


__all__ = [
    "LocalAppConfig",
    "LocalAppError",
    "LocalAppStartupError",
    "LocalAppStartupFailure",
    "default_local_app_root",
    "run_local_app",
]

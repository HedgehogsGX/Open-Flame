from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
    RuntimeLogConfig,
)

LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_STORAGE_MIN_FREE_BYTES = 1024 * 1024 * 1024


def require_loopback_bind_host(host: str) -> None:
    if host.lower() not in LOOPBACK_BIND_HOSTS:
        raise ValueError(
            "unauthenticated control plane must bind to a loopback host "
            "(127.0.0.1, ::1, or localhost)"
        )


def _resolved_path(raw: str | None, fallback: Path) -> Path:
    path = Path(raw).expanduser() if raw else fallback
    return path.resolve()


def _absolute_optional_path(raw: str | None, *, name: str) -> Path | None:
    if raw is None or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise ValueError(f"{name} must be an absolute normalized path")
    # Do not call resolve(): the key/socket loader must still be able to see
    # and reject a symlink or reparse-point component.
    return path


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be an explicit boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    data_root: Path
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    max_batch_urls: int = 50
    route_policy_version: str = "mvp-v1"
    x_graph_v2_enabled: bool = False
    short_link_resolution_enabled: bool = False
    short_link_transport_socket: Path | None = field(default=None, repr=False)
    short_link_attestation_key_file: Path | None = field(default=None, repr=False)
    storage_min_free_bytes: int = DEFAULT_STORAGE_MIN_FREE_BYTES
    runtime_log_level: str = "INFO"
    runtime_log_max_bytes: int = DEFAULT_RUNTIME_LOG_MAX_BYTES
    runtime_log_backup_count: int = DEFAULT_RUNTIME_LOG_BACKUP_COUNT
    tool_root: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        require_loopback_bind_host(self.host)
        if self.storage_min_free_bytes < 0:
            raise ValueError("storage_min_free_bytes must be non-negative")
        log_config = RuntimeLogConfig(
            directory=self.data_root / "logs",
            level=self.runtime_log_level,
            max_bytes=self.runtime_log_max_bytes,
            backup_count=self.runtime_log_backup_count,
        )
        object.__setattr__(self, "runtime_log_level", log_config.level)
        if self.short_link_resolution_enabled and (
            self.short_link_transport_socket is None
            or self.short_link_attestation_key_file is None
        ):
            raise ValueError(
                "short-link resolution requires both transport socket and "
                "attestation key file"
            )
        if self.tool_root is not None and (
            not self.tool_root.is_absolute()
            or self.tool_root != Path(os.path.normpath(self.tool_root))
        ):
            raise ValueError("tool root must be an absolute normalized path")
        for path in (
            self.short_link_transport_socket,
            self.short_link_attestation_key_file,
        ):
            if path is not None and (
                not path.is_absolute() or path != Path(os.path.normpath(path))
            ):
                raise ValueError("short-link security paths must be absolute")
        if (
            self.short_link_transport_socket is not None
            and self.short_link_transport_socket == self.short_link_attestation_key_file
        ):
            raise ValueError(
                "short-link socket and attestation key must be different paths"
            )

    def validate_startup_security(self) -> None:
        """Re-check the bind boundary immediately before app construction."""

        require_loopback_bind_host(self.host)

    @classmethod
    def from_env(cls) -> Settings:
        working_root = Path.cwd()
        data_root = _resolved_path(os.getenv("VDC_DATA_ROOT"), working_root / "data")
        database_path = _resolved_path(
            os.getenv("VDC_DATABASE_PATH"), data_root / "control.sqlite3"
        )
        return cls(
            data_root=data_root,
            database_path=database_path,
            tool_root=_absolute_optional_path(
                os.getenv("VDC_TOOL_ROOT"),
                name="VDC_TOOL_ROOT",
            ),
            host=os.getenv("VDC_HOST", "127.0.0.1"),
            port=int(os.getenv("VDC_PORT", "8000")),
            max_batch_urls=int(os.getenv("VDC_MAX_BATCH_URLS", "50")),
            route_policy_version=os.getenv("VDC_ROUTE_POLICY_VERSION", "mvp-v1"),
            x_graph_v2_enabled=_env_flag("VDC_ENABLE_X_GRAPH_V2", default=False),
            short_link_resolution_enabled=_env_flag(
                "VDC_ENABLE_SHORT_LINK_RESOLUTION", default=False
            ),
            short_link_transport_socket=_absolute_optional_path(
                os.getenv("VDC_SHORT_LINK_TRANSPORT_SOCKET"),
                name="VDC_SHORT_LINK_TRANSPORT_SOCKET",
            ),
            short_link_attestation_key_file=_absolute_optional_path(
                os.getenv("VDC_SHORT_LINK_ATTESTATION_KEY_FILE"),
                name="VDC_SHORT_LINK_ATTESTATION_KEY_FILE",
            ),
            storage_min_free_bytes=int(
                os.getenv(
                    "VDC_STORAGE_MIN_FREE_BYTES",
                    str(DEFAULT_STORAGE_MIN_FREE_BYTES),
                )
            ),
            runtime_log_level=os.getenv("VDC_RUNTIME_LOG_LEVEL", "INFO"),
            runtime_log_max_bytes=int(
                os.getenv(
                    "VDC_RUNTIME_LOG_MAX_BYTES",
                    str(DEFAULT_RUNTIME_LOG_MAX_BYTES),
                )
            ),
            runtime_log_backup_count=int(
                os.getenv(
                    "VDC_RUNTIME_LOG_BACKUP_COUNT",
                    str(DEFAULT_RUNTIME_LOG_BACKUP_COUNT),
                )
            ),
        )

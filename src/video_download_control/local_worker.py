"""Importable local Worker configuration, preflight, and execution ownership."""

from __future__ import annotations

import math
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from .adapters import DirectEgress, YtDlpAdapter, YtDlpCommandFactory, YtDlpJsRuntime
from .assets import AssetStore, AssetValidationError
from .candidate_cookies import (
    DEFAULT_MAX_COOKIE_BYTES,
    AttemptCookieResolver,
    CookieSource,
)
from .database import Database
from .runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
    RuntimeLogConfig,
    RuntimeLogger,
)
from .subprocess_runner import SecureSubprocessRunner
from .toolchain import ToolchainError, load_toolchain_lock, verify_toolchain
from .verifiers import FfprobeVerifier
from .worker import Worker
from .worker_repository import WorkerRepository


FEATURE_GATE = "VDC_ENABLE_LOCAL_REAL_WORKER"


_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


_MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024


_MAX_STORAGE_RESERVE_BYTES = 16 * 1024 * 1024 * 1024


class LocalWorkerStartupError(RuntimeError):
    """A bounded startup/guard failure safe to show at the CLI boundary."""


class LocalDirectNetworkGuard:
    """Reconfirm the operator's direct-network opt-in before every claim."""

    def __init__(
        self,
        *,
        acknowledged: bool,
    ) -> None:
        self._acknowledged = acknowledged

    def assert_ready(self, *, adapter_name: str) -> None:
        _require_windows_host()
        if adapter_name != "yt_dlp":
            raise LocalWorkerStartupError("local direct Worker adapter is invalid")
        if os.getenv(FEATURE_GATE) != "1":
            raise LocalWorkerStartupError("local real Worker is disabled")
        if not self._acknowledged:
            raise LocalWorkerStartupError(
                "local real Worker requires --allow-direct-network"
            )


@dataclass(frozen=True, slots=True)
class LocalWorkerConfig:
    data_root: Path
    database_path: Path
    tool_root: Path
    worker_id: str
    allow_direct_network: bool
    js_runtime: YtDlpJsRuntime | None = None
    cookie_sources: tuple[CookieSource, ...] = ()
    cookie_config_path: Path | None = field(default=None, repr=False)
    max_cookie_bytes: int = DEFAULT_MAX_COOKIE_BYTES
    runtime_log_level: str = "INFO"
    runtime_log_max_bytes: int = DEFAULT_RUNTIME_LOG_MAX_BYTES
    runtime_log_backup_count: int = DEFAULT_RUNTIME_LOG_BACKUP_COUNT
    max_height: int = 1080
    max_file_bytes: int = 8 * 1024 * 1024 * 1024
    storage_min_free_bytes: int = 1024 * 1024 * 1024
    socket_timeout_seconds: int = 20
    probe_timeout_seconds: float = 90.0
    download_timeout_seconds: float = 30.0 * 60.0
    ffprobe_timeout_seconds: float = 30.0
    attempt_timeout_seconds: float = 60.0 * 60.0
    max_items_per_source: int = 20

    def __post_init__(self) -> None:
        if not isinstance(self.cookie_sources, tuple):
            raise ValueError("local Worker cookie sources must be an immutable tuple")
        try:
            AttemptCookieResolver(
                self.cookie_sources,
                max_cookie_bytes=self.max_cookie_bytes,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("local Worker cookie source configuration is invalid") from exc
        paths = (
            self.data_root,
            self.database_path,
            self.tool_root,
            *((self.cookie_config_path,) if self.cookie_config_path is not None else ()),
            *(source.path for source in self.cookie_sources),
        )
        if any(
            not isinstance(path, Path)
            or not path.is_absolute()
            or Path(os.path.abspath(path)) != path
            for path in paths
        ):
            raise ValueError("local Worker paths must be normalized and absolute")
        if self.data_root == Path(self.data_root.anchor):
            raise ValueError("local Worker data root may not be a filesystem root")
        try:
            relative_database = self.database_path.relative_to(self.data_root)
        except ValueError as exc:
            raise ValueError("local Worker database must stay below the data root") from exc
        if not relative_database.parts or self.database_path == self.data_root:
            raise ValueError("local Worker database path must name a file")
        if _path_trees_overlap(self.data_root, self.tool_root):
            raise ValueError("local Worker data and tool roots must be separate")
        if any(
            _path_trees_overlap(source.path, root)
            for source in self.cookie_sources
            for root in (self.data_root, self.tool_root)
        ):
            raise ValueError(
                "local Worker Cookie sources must stay outside data and tool roots"
            )
        if self.cookie_config_path is not None and any(
            _path_trees_overlap(self.cookie_config_path, root)
            for root in (self.data_root, self.tool_root)
        ):
            raise ValueError(
                "local Worker Cookie configuration must stay outside data and tool roots"
            )
        if not isinstance(self.worker_id, str) or not _WORKER_ID.fullmatch(
            self.worker_id
        ):
            raise ValueError("local Worker id must be a short strict token")
        if not isinstance(self.allow_direct_network, bool):
            raise ValueError("local Worker direct-network acknowledgement is invalid")
        if self.js_runtime is not None and not isinstance(
            self.js_runtime, YtDlpJsRuntime
        ):
            raise ValueError("local Worker JavaScript runtime is invalid")
        log_config = RuntimeLogConfig(
            directory=self.data_root / "logs",
            level=self.runtime_log_level,
            max_bytes=self.runtime_log_max_bytes,
            backup_count=self.runtime_log_backup_count,
        )
        object.__setattr__(self, "runtime_log_level", log_config.level)
        _bounded_integer(self.max_height, "max height", 144, 2160)
        _bounded_integer(self.max_file_bytes, "max file bytes", 1, _MAX_FILE_BYTES)
        _bounded_integer(
            self.storage_min_free_bytes,
            "storage reserve bytes",
            0,
            _MAX_STORAGE_RESERVE_BYTES,
        )
        _bounded_integer(self.socket_timeout_seconds, "socket timeout", 1, 120)
        _bounded_integer(self.max_items_per_source, "source item limit", 1, 50)
        _bounded_integer(
            self.max_cookie_bytes,
            "cookie byte limit",
            1,
            64 * 1024 * 1024,
        )
        for value, label, maximum in (
            (self.probe_timeout_seconds, "probe timeout", 30 * 60),
            (self.download_timeout_seconds, "download timeout", 24 * 60 * 60),
            (self.ffprobe_timeout_seconds, "ffprobe timeout", 10 * 60),
            (self.attempt_timeout_seconds, "attempt timeout", 24 * 60 * 60),
        ):
            _bounded_number(value, label, maximum)
        if self.attempt_timeout_seconds < max(
            self.probe_timeout_seconds,
            self.download_timeout_seconds,
            self.ffprobe_timeout_seconds,
        ):
            raise ValueError("attempt timeout must cover every operation timeout")


def _bounded_integer(value: int, label: str, minimum: int, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} is outside the supported range")


def _bounded_number(value: float, label: str, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"{label} is outside the supported range")


def _path_trees_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_feature_gate() -> None:
    if os.getenv(FEATURE_GATE) != "1":
        raise LocalWorkerStartupError(
            f"local real Worker is disabled; set {FEATURE_GATE}=1 explicitly"
        )


def _require_windows_host() -> None:
    if sys.platform != "win32":
        raise LocalWorkerStartupError(
            "local real Worker currently requires the verified Windows toolchain"
        )





def _require_plain_directory(path: Path, label: str) -> None:
    _reject_existing_link_components(path, label)
    try:
        info = path.lstat()
    except OSError as exc:
        raise LocalWorkerStartupError(f"{label} is unavailable") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or (reparse and attributes & reparse)
    ):
        raise LocalWorkerStartupError(f"{label} must be a plain directory")


def _require_plain_file(path: Path, label: str) -> None:
    _reject_existing_link_components(path, label)
    try:
        info = path.lstat()
    except OSError as exc:
        raise LocalWorkerStartupError(f"{label} is unavailable") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or (reparse and attributes & reparse)
    ):
        raise LocalWorkerStartupError(f"{label} must be a plain file")


def _reject_existing_link_components(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LocalWorkerStartupError(
                f"{label} path cannot be inspected"
            ) from exc
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or (reparse and attributes & reparse):
            raise LocalWorkerStartupError(f"{label} path may not contain links")


def validate_local_worker_paths(config: LocalWorkerConfig) -> None:
    _require_plain_directory(config.data_root, "local Worker data root")
    _require_plain_file(config.database_path, "local Worker control database")
    database = Database(config.database_path)
    database_ok, _detail = database.readiness()
    if not database_ok:
        raise LocalWorkerStartupError("local Worker control database is not ready")


@contextmanager
def exclusive_local_worker(data_root: Path) -> Iterator[None]:
    """Hold a process-scoped Windows byte-range lock for one data root."""

    log_directory = data_root / "logs"
    _reject_existing_link_components(log_directory, "local Worker log directory")
    try:
        log_directory.mkdir(parents=False, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise LocalWorkerStartupError(
            "local Worker log directory is unavailable"
        ) from exc
    _require_plain_directory(log_directory, "local Worker log directory")
    lock_path = log_directory / ".local-worker.lock"
    _reject_existing_link_components(lock_path, "local Worker singleton lock")
    if lock_path.exists():
        _require_plain_file(lock_path, "local Worker singleton lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        info = os.fstat(descriptor)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (reparse and attributes & reparse)
        ):
            raise LocalWorkerStartupError(
                "local Worker singleton lock must be a plain file"
            )
        if info.st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except (ImportError, OSError) as exc:
            raise LocalWorkerStartupError(
                "another local Worker is already using this data root"
            ) from exc
    except LocalWorkerStartupError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except (ImportError, OSError) as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise LocalWorkerStartupError(
            "local Worker singleton lock is unavailable"
        ) from exc

    try:
        yield
    finally:
        assert descriptor is not None
        try:
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass


def local_worker_logger(config: LocalWorkerConfig) -> RuntimeLogger:
    return RuntimeLogger(
        component="local-worker",
        config=RuntimeLogConfig(
            directory=config.data_root / "logs",
            level=config.runtime_log_level,
            max_bytes=config.runtime_log_max_bytes,
            backup_count=config.runtime_log_backup_count,
        ),
        instance_id=config.worker_id,
    )


def build_local_worker(
    config: LocalWorkerConfig,
    *,
    runner: SecureSubprocessRunner | None = None,
    runtime_logger: RuntimeLogger | None = None,
    claim_gate_run_id: str | None = None,
) -> Worker:
    """Assemble a direct Worker only after every local opt-in is revalidated.

    The console entry point writes ``worker.initializing`` before calling this
    function.  A logger is mandatory here as well so an imported builder cannot
    silently create an unobservable direct-network Worker.
    """

    require_local_worker_opt_in(allow_direct_network=config.allow_direct_network)
    validate_local_worker_paths(config)
    if runtime_logger is None:
        raise LocalWorkerStartupError("local Worker requires a runtime logger")
    if config.js_runtime is not None:
        _require_plain_file(
            config.js_runtime.executable,
            "local Worker JavaScript runtime",
        )
    cookie_resolver = AttemptCookieResolver(
        config.cookie_sources,
        max_cookie_bytes=config.max_cookie_bytes,
    )
    cookie_resolver.validate_sources()
    lock = load_toolchain_lock()
    python_executable = Path(sys.executable).resolve(strict=True)
    try:
        verified = verify_toolchain(
            config.tool_root,
            python_executable=python_executable,
            lock=lock,
        )
    except (OSError, ToolchainError) as exc:
        raise LocalWorkerStartupError("local Worker toolchain is not ready") from exc
    if not verified.offline_smoke_passed:
        raise LocalWorkerStartupError("local Worker toolchain smoke is not ready")
    if not runtime_logger.emit(
        "toolchain.inspected",
        state="ready",
        detail_code="ok",
        yt_dlp_version=verified.yt_dlp_version,
        ffmpeg_version=verified.ffmpeg_version,
        ffprobe_version=verified.ffprobe_version,
        offline_smoke_passed=verified.offline_smoke_passed,
    ):
        raise LocalWorkerStartupError("local Worker runtime log is unavailable")

    yt_dlp_entrypoint = config.tool_root / Path(lock.yt_dlp.entrypoint)
    ffmpeg_directory = config.tool_root / "ffmpeg" / "bin"
    ffmpeg_executable = ffmpeg_directory / "ffmpeg.exe"
    ffprobe_executable = ffmpeg_directory / "ffprobe.exe"
    command_runner = (
        runner
        if runner is not None
        else SecureSubprocessRunner(
            allowed_executable_roots=(python_executable.parent, ffmpeg_directory),
            runtime_logger=runtime_logger,
        )
    )
    factory = YtDlpCommandFactory(
        executable=python_executable,
        zipimport_entrypoint=yt_dlp_entrypoint,
        js_runtime=config.js_runtime,
        ffmpeg_directory=ffmpeg_directory,
        expected_version=verified.yt_dlp_version,
        egress=DirectEgress(),
        max_height=config.max_height,
        max_file_bytes=config.max_file_bytes,
        socket_timeout_seconds=config.socket_timeout_seconds,
    )
    verifier = FfprobeVerifier(
        executable=ffprobe_executable,
        runner=command_runner,
        expected_version=verified.ffprobe_version,
        timeout_seconds=config.ffprobe_timeout_seconds,
    )
    try:
        verifier.validate_runtime()
    except (AssetValidationError, OSError) as exc:
        raise LocalWorkerStartupError("local Worker ffprobe is not ready") from exc
    adapter = YtDlpAdapter(
        factory=factory,
        runner=command_runner,
        require_thumbnail_mapping_payload=runner is None,
        cookie_resolver=cookie_resolver,
        probe_timeout_seconds=config.probe_timeout_seconds,
        download_timeout_seconds=config.download_timeout_seconds,
    )
    database = Database(config.database_path)
    return Worker(
        worker_id=config.worker_id,
        repository=WorkerRepository(database),
        adapter=adapter,
        asset_store=AssetStore(
            config.data_root,
            max_file_bytes=config.max_file_bytes,
            min_free_bytes=config.storage_min_free_bytes,
        ),
        verifier=verifier,
        attempt_timeout_seconds=config.attempt_timeout_seconds,
        max_height=config.max_height,
        max_items_per_source=config.max_items_per_source,
        skip_unsupported_graph_jobs=True,
        network_guard=LocalDirectNetworkGuard(
            acknowledged=config.allow_direct_network,
        ),
        runtime_logger=runtime_logger,
        claim_gate_run_id=claim_gate_run_id,
        stop_event=(
            command_runner.stop_event
            if isinstance(command_runner, SecureSubprocessRunner)
            else Event()
        ),
    )


def require_local_worker_opt_in(*, allow_direct_network: bool) -> None:
    """Check the explicit host/network policy before parsing private config or I/O."""
    _require_feature_gate()
    if not allow_direct_network:
        raise LocalWorkerStartupError("local real Worker requires --allow-direct-network")
    _require_windows_host()

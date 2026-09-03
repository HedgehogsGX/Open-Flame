"""Explicitly enabled local Worker for the verified project toolchain.

Unlike the Linux production candidate, this mode uses the host's direct
network route.  It is intended for one-user local operation and never enables
itself implicitly: both an exact environment gate and a CLI acknowledgement
are required, and the guard rechecks the gate before every queue claim.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .adapters import (
    DirectEgress,
    YtDlpAdapter,
    YtDlpCommandFactory,
    YtDlpJsRuntime,
    YtDlpJsRuntimeName,
)
from .assets import AssetStore, AssetValidationError
from .database import Database
from .runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
    RuntimeLogConfig,
    RuntimeLogger,
    safe_exception_type,
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


def _absolute_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise argparse.ArgumentTypeError("an explicit normalized absolute path is required")
    return path


def _js_runtime(raw: str) -> YtDlpJsRuntime:
    name, separator, raw_path = raw.partition(":")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError(
            "JavaScript runtime must use NAME:ABSOLUTE_EXECUTABLE"
        )
    try:
        return YtDlpJsRuntime(YtDlpJsRuntimeName(name), Path(raw_path))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "JavaScript runtime must use a supported name and absolute plain executable"
        ) from exc


def _poll_interval(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("poll interval must be a number") from exc
    if not math.isfinite(value) or not 0.1 <= value <= 60:
        raise argparse.ArgumentTypeError(
            "poll interval must be between 0.1 and 60 seconds"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the verified yt-dlp toolchain as a local direct-network Worker"
        )
    )
    parser.add_argument("--data-root", type=_absolute_path, required=True)
    parser.add_argument(
        "--database-path",
        type=_absolute_path,
        help="existing control database; defaults to DATA_ROOT/control.sqlite3",
    )
    parser.add_argument("--tool-root", type=_absolute_path, required=True)
    parser.add_argument(
        "--js-runtime",
        type=_js_runtime,
        metavar="NAME:ABSOLUTE_EXECUTABLE",
        help="optional explicit yt-dlp JavaScript runtime",
    )
    parser.add_argument("--worker-id", default="local-real-worker")
    parser.add_argument(
        "--allow-direct-network",
        action="store_true",
        help="acknowledge that this local Worker is not network-isolated",
    )
    parser.add_argument("--max-height", type=int, default=1080)
    parser.add_argument("--max-file-bytes", type=int, default=8 * 1024 * 1024 * 1024)
    parser.add_argument("--storage-min-free-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--socket-timeout-seconds", type=int, default=20)
    parser.add_argument("--probe-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--download-timeout-seconds", type=float, default=30.0 * 60.0)
    parser.add_argument("--ffprobe-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--attempt-timeout-seconds", type=float, default=60.0 * 60.0)
    parser.add_argument("--max-items-per-source", type=int, default=20)
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--drain",
        action="store_true",
        help="process queued jobs until none are immediately available",
    )
    run_mode.add_argument(
        "--poll-interval-seconds",
        type=_poll_interval,
        help="stay resident and poll an idle queue at this bounded interval",
    )
    return parser


@dataclass(frozen=True, slots=True)
class LocalWorkerConfig:
    data_root: Path
    database_path: Path
    tool_root: Path
    worker_id: str
    allow_direct_network: bool
    js_runtime: YtDlpJsRuntime | None = None
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
        paths = (self.data_root, self.database_path, self.tool_root)
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

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> LocalWorkerConfig:
        data_root = args.data_root
        return cls(
            data_root=data_root,
            database_path=args.database_path or data_root / "control.sqlite3",
            tool_root=args.tool_root,
            worker_id=args.worker_id,
            allow_direct_network=args.allow_direct_network,
            js_runtime=args.js_runtime,
            max_height=args.max_height,
            max_file_bytes=args.max_file_bytes,
            storage_min_free_bytes=args.storage_min_free_bytes,
            socket_timeout_seconds=args.socket_timeout_seconds,
            probe_timeout_seconds=args.probe_timeout_seconds,
            download_timeout_seconds=args.download_timeout_seconds,
            ffprobe_timeout_seconds=args.ffprobe_timeout_seconds,
            attempt_timeout_seconds=args.attempt_timeout_seconds,
            max_items_per_source=args.max_items_per_source,
        )


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


def _require_acknowledgement(config: LocalWorkerConfig) -> None:
    if not config.allow_direct_network:
        raise LocalWorkerStartupError(
            "local real Worker requires --allow-direct-network"
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


def _validate_control_paths(config: LocalWorkerConfig) -> None:
    _require_plain_directory(config.data_root, "local Worker data root")
    _require_plain_file(config.database_path, "local Worker control database")
    database = Database(config.database_path)
    database_ok, _detail = database.readiness()
    if not database_ok:
        raise LocalWorkerStartupError("local Worker control database is not ready")


@contextmanager
def _exclusive_local_worker(data_root: Path) -> Iterator[None]:
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


def _runtime_logger(config: LocalWorkerConfig) -> RuntimeLogger:
    return RuntimeLogger(
        component="local-worker",
        config=RuntimeLogConfig(
            directory=config.data_root / "logs",
            level=os.getenv("VDC_RUNTIME_LOG_LEVEL", "INFO"),
            max_bytes=int(
                os.getenv(
                    "VDC_RUNTIME_LOG_MAX_BYTES",
                    str(DEFAULT_RUNTIME_LOG_MAX_BYTES),
                )
            ),
            backup_count=int(
                os.getenv(
                    "VDC_RUNTIME_LOG_BACKUP_COUNT",
                    str(DEFAULT_RUNTIME_LOG_BACKUP_COUNT),
                )
            ),
        ),
        instance_id=config.worker_id,
    )


def build_local_worker(
    config: LocalWorkerConfig,
    *,
    runner: SecureSubprocessRunner | None = None,
    runtime_logger: RuntimeLogger | None = None,
) -> Worker:
    """Assemble a direct Worker only after every local opt-in is revalidated.

    The console entry point writes ``worker.initializing`` before calling this
    function.  A logger is mandatory here as well so an imported builder cannot
    silently create an unobservable direct-network Worker.
    """

    _require_feature_gate()
    _require_acknowledgement(config)
    _require_windows_host()
    _validate_control_paths(config)
    if runtime_logger is None:
        raise LocalWorkerStartupError("local Worker requires a runtime logger")
    if config.js_runtime is not None:
        _require_plain_file(
            config.js_runtime.executable,
            "local Worker JavaScript runtime",
        )
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
    command_runner = runner or SecureSubprocessRunner(
        allowed_executable_roots=(python_executable.parent, ffmpeg_directory),
        runtime_logger=runtime_logger,
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
    )


def _print_result(result) -> None:
    if result is None:
        print(json.dumps({"status": "idle"}, ensure_ascii=False))
        return
    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "attempt_id": result.attempt_id,
                "status": result.status,
                "error_code": result.error_code,
            },
            ensure_ascii=False,
        )
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        _require_feature_gate()
        if not args.allow_direct_network:
            raise LocalWorkerStartupError(
                "local real Worker requires --allow-direct-network"
            )
        _require_windows_host()
        config = LocalWorkerConfig.from_args(args)
        # This check intentionally precedes logger construction, tool execution,
        # and any database access.  A typo must not create a second empty queue.
        _validate_control_paths(config)
    except (LocalWorkerStartupError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

    try:
        with _exclusive_local_worker(config.data_root):
            try:
                runtime_logger = _runtime_logger(config)
            except Exception:  # noqa: BLE001 - no logger exists at this boundary
                raise SystemExit(
                    "local real Worker runtime log could not be initialized; "
                    "no job was claimed"
                ) from None
            if not runtime_logger.emit(
                "worker.initializing",
                worker_id=config.worker_id,
                adapter="yt_dlp",
                direct_network_enabled=True,
                js_runtime_enabled=config.js_runtime is not None,
            ):
                raise SystemExit(
                    "local real Worker runtime log is unavailable; no job was claimed"
                )
            try:
                worker = build_local_worker(
                    config,
                    runtime_logger=runtime_logger,
                )
                if not runtime_logger.emit(
                    "worker.started",
                    worker_id=config.worker_id,
                    adapter="yt_dlp",
                    direct_network_enabled=True,
                    js_runtime_enabled=config.js_runtime is not None,
                ):
                    raise LocalWorkerStartupError(
                        "local Worker runtime log is unavailable"
                    )
            except Exception as exc:  # noqa: BLE001 - public boundary redacts causes
                if runtime_logger is not None:
                    runtime_logger.emit(
                        "worker.startup_failed",
                        level="ERROR",
                        worker_id=config.worker_id,
                        exception_type=safe_exception_type(exc),
                    )
                raise SystemExit(
                    "local real Worker startup failed; inspect the runtime log"
                ) from None

            stop_reason = "runtime_error"
            cycle_started = time.perf_counter()
            try:
                while True:
                    result = worker.run_once()
                    _print_result(result)
                    if args.poll_interval_seconds is not None:
                        if result is None:
                            time.sleep(args.poll_interval_seconds)
                        cycle_started = time.perf_counter()
                        continue
                    if result is None or not args.drain:
                        if result is None:
                            stop_reason = "drain_complete" if args.drain else "idle"
                        else:
                            stop_reason = "single_run_complete"
                        return
                    cycle_started = time.perf_counter()
            except KeyboardInterrupt:
                stop_reason = "keyboard_interrupt"
                return
            except Exception as exc:  # noqa: BLE001 - public boundary removes details
                runtime_logger.emit(
                    "worker.cycle_failed",
                    level="ERROR",
                    worker_id=config.worker_id,
                    exception_type=safe_exception_type(exc),
                    duration_ms=min(
                        max((time.perf_counter() - cycle_started) * 1000.0, 0.0),
                        86_400_000.0,
                    ),
                )
                raise SystemExit(
                    "local real Worker failed; inspect the runtime log"
                ) from None
            finally:
                runtime_logger.emit(
                    "worker.stopped",
                    worker_id=config.worker_id,
                    reason=stop_reason,
                )
    except LocalWorkerStartupError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()


__all__ = [
    "FEATURE_GATE",
    "LocalDirectNetworkGuard",
    "LocalWorkerConfig",
    "LocalWorkerStartupError",
    "build_local_worker",
    "build_parser",
    "main",
]

"""Explicitly gated assembly entry for the production-candidate real Worker.

This module is intentionally separate from the ordinary offline QA Worker.
It has no implicit data/tool/version paths and refuses to initialize or claim
work unless the Linux loopback-only namespace and Unix-socket relay pass the
deployment-owned guard.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .adapters import (
    ControlledEgressEndpoint,
    YtDlpAdapter,
    YtDlpCommandFactory,
    YtDlpJsRuntime,
    YtDlpJsRuntimeName,
)
from .assets import AssetStore, AssetValidationError
from .candidate_cookies import (
    AttemptCookieResolver,
    CookiePreparationError,
    CookieSource,
)
from .database import Database
from .domain import Platform
from .runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
    RuntimeLogConfig,
    RuntimeLogger,
    safe_exception_type,
)
from .security import NetworkIsolationError, UnixRelayNetworkGuard
from .subprocess_runner import (
    CommandResult,
    CommandSpec,
    SecureSubprocessRunner,
    SubprocessExecutionError,
)
from .verifiers import FfprobeVerifier
from .worker import Worker
from .worker_repository import WorkerRepository

FEATURE_GATE = "VDC_ENABLE_CANDIDATE_REAL_WORKER"
CREDENTIAL_PROFILE_WIRING_STATUS = (
    "credential_profile_id is claim-validated and resolves only to an opaque "
    "secret_ref; mounted cookie paths remain deployment-owned"
)
_STRICT_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024
_MAX_STORAGE_RESERVE_BYTES = 16 * 1024 * 1024 * 1024


class CandidateStartupError(RuntimeError):
    """A bounded, configuration-safe startup failure."""


class CommandRunner(Protocol):
    def run(
        self,
        spec: CommandSpec,
        *,
        is_cancelled=None,
    ) -> CommandResult: ...


class Guard(Protocol):
    def assert_ready(self, *, adapter_name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CandidateWorkerConfig:
    data_root: Path
    database_path: Path
    yt_dlp_executable: Path
    ffmpeg_directory: Path
    ffmpeg_executable: Path
    ffprobe_executable: Path
    unix_socket_path: Path
    yt_dlp_version: str
    ffmpeg_version: str
    ffprobe_version: str
    egress_policy_version: str
    relay_port: int
    max_height: int
    max_file_bytes: int
    storage_min_free_bytes: int
    socket_timeout_seconds: int
    probe_timeout_seconds: float
    download_timeout_seconds: float
    ffprobe_timeout_seconds: float
    attempt_timeout_seconds: float
    max_items_per_source: int
    max_cookie_bytes: int
    worker_id: str
    cookie_sources: tuple[CookieSource, ...] = ()
    yt_dlp_zipimport_entrypoint: Path | None = None
    js_runtime: YtDlpJsRuntime | None = None

    def __post_init__(self) -> None:
        if self.js_runtime is not None and not isinstance(
            self.js_runtime, YtDlpJsRuntime
        ):
            raise ValueError("candidate Worker JavaScript runtime is invalid")
        zipimport_paths = (
            (self.yt_dlp_zipimport_entrypoint,)
            if self.yt_dlp_zipimport_entrypoint is not None
            else ()
        )
        js_runtime_paths = (
            (self.js_runtime.executable,) if self.js_runtime is not None else ()
        )
        paths = (
            self.data_root,
            self.database_path,
            self.yt_dlp_executable,
            *zipimport_paths,
            *js_runtime_paths,
            self.ffmpeg_directory,
            self.ffmpeg_executable,
            self.ffprobe_executable,
            self.unix_socket_path,
            *(source.path for source in self.cookie_sources),
        )
        if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
            raise ValueError("candidate Worker paths must be explicit and absolute")
        if any(Path(os.path.abspath(path)) != path for path in paths):
            raise ValueError("candidate Worker paths must be normalized")
        if self.data_root == Path(self.data_root.anchor):
            raise ValueError("data root may not be a filesystem root")
        try:
            database_relative = self.database_path.relative_to(self.data_root)
        except ValueError as exc:
            raise ValueError("database path must stay below the data root") from exc
        if not database_relative.parts or self.database_path == self.data_root:
            raise ValueError("database path must name a file below the data root")
        for value, label in (
            (self.yt_dlp_version, "yt-dlp version"),
            (self.ffmpeg_version, "ffmpeg version"),
            (self.ffprobe_version, "ffprobe version"),
            (self.egress_policy_version, "egress policy version"),
        ):
            if not isinstance(value, str) or not _STRICT_TOKEN.fullmatch(value):
                raise ValueError(f"{label} must be an explicit strict token")
        if not isinstance(self.worker_id, str) or not _WORKER_ID.fullmatch(
            self.worker_id
        ):
            raise ValueError("worker id must be a short strict token")
        self._bounded_integer(self.relay_port, "relay port", 1, 65535)
        self._bounded_integer(self.max_height, "max height", 144, 2160)
        self._bounded_integer(self.max_file_bytes, "max file bytes", 1, _MAX_FILE_BYTES)
        self._bounded_integer(
            self.storage_min_free_bytes,
            "storage reserve bytes",
            0,
            _MAX_STORAGE_RESERVE_BYTES,
        )
        self._bounded_integer(self.socket_timeout_seconds, "socket timeout", 1, 120)
        self._bounded_integer(self.max_items_per_source, "source item limit", 1, 50)
        self._bounded_integer(
            self.max_cookie_bytes, "cookie byte limit", 1, 64 * 1024 * 1024
        )
        for value, label, upper in (
            (self.probe_timeout_seconds, "probe timeout", 30 * 60),
            (self.download_timeout_seconds, "download timeout", 24 * 60 * 60),
            (self.ffprobe_timeout_seconds, "ffprobe timeout", 10 * 60),
            (self.attempt_timeout_seconds, "attempt timeout", 24 * 60 * 60),
        ):
            self._bounded_number(value, label, upper)
        if self.attempt_timeout_seconds < max(
            self.probe_timeout_seconds,
            self.download_timeout_seconds,
            self.ffprobe_timeout_seconds,
        ):
            raise ValueError("attempt timeout must cover every operation timeout")

    @staticmethod
    def _bounded_integer(value: int, label: str, minimum: int, maximum: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise ValueError(f"{label} is outside the strict supported range")

    @staticmethod
    def _bounded_number(value: float, label: str, maximum: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(f"{label} is outside the strict supported range")


def _absolute_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("an explicit absolute path is required")
    return path


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


def _cookie_source(raw: str) -> CookieSource:
    """Parse PLATFORM:CREDENTIAL_REF=ABSOLUTE_PATH without echoing failures."""

    try:
        identity, raw_path = raw.split("=", 1)
        raw_platform, credential_ref = identity.split(":", 1)
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError
        return CookieSource(
            platform=Platform(raw_platform),
            credential_ref=credential_ref,
            path=path,
        )
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "cookie source must use PLATFORM:OPAQUE_REF=ABSOLUTE_PATH"
        ) from exc


def _js_runtime(raw: str) -> YtDlpJsRuntime:
    """Parse NAME:ABSOLUTE_EXECUTABLE without reflecting unsafe input."""

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly gated production-candidate yt-dlp Worker",
        epilog=CREDENTIAL_PROFILE_WIRING_STATUS,
    )
    parser.add_argument("--data-root", type=_absolute_path, required=True)
    parser.add_argument("--database-path", type=_absolute_path, required=True)
    parser.add_argument("--yt-dlp-executable", type=_absolute_path, required=True)
    parser.add_argument(
        "--yt-dlp-zipimport-entrypoint",
        type=_absolute_path,
        help=(
            "trusted yt-dlp zipimport entrypoint invoked through the configured "
            "Python executable"
        ),
    )
    parser.add_argument(
        "--js-runtime",
        type=_js_runtime,
        metavar="NAME:ABSOLUTE_EXECUTABLE",
        help=(
            "enable exactly one explicit yt-dlp JavaScript runtime "
            "(deno, node, bun, or quickjs)"
        ),
    )
    parser.add_argument("--ffmpeg-directory", type=_absolute_path, required=True)
    parser.add_argument("--ffmpeg-executable", type=_absolute_path, required=True)
    parser.add_argument("--ffprobe-executable", type=_absolute_path, required=True)
    parser.add_argument("--unix-socket-path", type=_absolute_path, required=True)
    parser.add_argument("--yt-dlp-version", required=True)
    parser.add_argument("--ffmpeg-version", required=True)
    parser.add_argument("--ffprobe-version", required=True)
    parser.add_argument("--egress-policy-version", required=True)
    parser.add_argument("--relay-port", type=int, required=True)
    parser.add_argument("--max-height", type=int, required=True)
    parser.add_argument("--max-file-bytes", type=int, required=True)
    parser.add_argument("--storage-min-free-bytes", type=int, required=True)
    parser.add_argument("--socket-timeout-seconds", type=int, required=True)
    parser.add_argument("--probe-timeout-seconds", type=float, required=True)
    parser.add_argument("--download-timeout-seconds", type=float, required=True)
    parser.add_argument("--ffprobe-timeout-seconds", type=float, required=True)
    parser.add_argument("--attempt-timeout-seconds", type=float, required=True)
    parser.add_argument("--max-items-per-source", type=int, required=True)
    parser.add_argument("--max-cookie-bytes", type=int, required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--cookie-source",
        action="append",
        default=[],
        type=_cookie_source,
        metavar="PLATFORM:OPAQUE_REF=ABSOLUTE_PATH",
        help=(
            "deployment-owned read-only cookie file selected only by a "
            "claim-validated opaque secret_ref"
        ),
    )
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


def config_from_args(args: argparse.Namespace) -> CandidateWorkerConfig:
    return CandidateWorkerConfig(
        data_root=args.data_root,
        database_path=args.database_path,
        yt_dlp_executable=args.yt_dlp_executable,
        yt_dlp_zipimport_entrypoint=args.yt_dlp_zipimport_entrypoint,
        js_runtime=args.js_runtime,
        ffmpeg_directory=args.ffmpeg_directory,
        ffmpeg_executable=args.ffmpeg_executable,
        ffprobe_executable=args.ffprobe_executable,
        unix_socket_path=args.unix_socket_path,
        yt_dlp_version=args.yt_dlp_version,
        ffmpeg_version=args.ffmpeg_version,
        ffprobe_version=args.ffprobe_version,
        egress_policy_version=args.egress_policy_version,
        relay_port=args.relay_port,
        max_height=args.max_height,
        max_file_bytes=args.max_file_bytes,
        storage_min_free_bytes=args.storage_min_free_bytes,
        socket_timeout_seconds=args.socket_timeout_seconds,
        probe_timeout_seconds=args.probe_timeout_seconds,
        download_timeout_seconds=args.download_timeout_seconds,
        ffprobe_timeout_seconds=args.ffprobe_timeout_seconds,
        attempt_timeout_seconds=args.attempt_timeout_seconds,
        max_items_per_source=args.max_items_per_source,
        max_cookie_bytes=args.max_cookie_bytes,
        worker_id=args.worker_id,
        cookie_sources=tuple(args.cookie_source),
    )


def build_candidate_worker(
    config: CandidateWorkerConfig,
    *,
    runner: CommandRunner | None = None,
    network_guard: Guard | None = None,
    runtime_logger: RuntimeLogger | None = None,
) -> Worker:
    """Assemble only after isolation and immutable tool inputs are proven."""

    _require_feature_gate()
    _validate_managed_paths(config)
    guard = network_guard or UnixRelayNetworkGuard(
        unix_socket_path=config.unix_socket_path,
        relay_host="127.0.0.1",
        relay_port=config.relay_port,
    )
    # This preflight occurs before Database.initialize().  Worker.run_once()
    # repeats it immediately before reading queue state and claiming a job.
    try:
        guard.assert_ready(adapter_name="yt_dlp")
    except NetworkIsolationError:
        raise
    except OSError as exc:
        raise CandidateStartupError("network isolation preflight failed") from exc

    command_runner = runner or SecureSubprocessRunner(
        allowed_executable_roots=(
            config.yt_dlp_executable.parent,
            config.ffmpeg_directory,
        ),
        runtime_logger=runtime_logger,
    )
    endpoint = ControlledEgressEndpoint(
        f"http://127.0.0.1:{config.relay_port}",
        policy_version=config.egress_policy_version,
    )
    factory = YtDlpCommandFactory(
        executable=config.yt_dlp_executable,
        zipimport_entrypoint=config.yt_dlp_zipimport_entrypoint,
        js_runtime=config.js_runtime,
        ffmpeg_directory=config.ffmpeg_directory,
        expected_version=config.yt_dlp_version,
        egress=endpoint,
        max_height=config.max_height,
        max_file_bytes=config.max_file_bytes,
        socket_timeout_seconds=config.socket_timeout_seconds,
    )
    cookie_resolver = AttemptCookieResolver(
        config.cookie_sources,
        max_cookie_bytes=config.max_cookie_bytes,
    )
    cookie_resolver.validate_sources()
    _validate_yt_dlp_runtime(factory, command_runner)
    _validate_ffmpeg_runtime(config, command_runner)
    verifier = FfprobeVerifier(
        executable=config.ffprobe_executable,
        runner=command_runner,  # type: ignore[arg-type]
        expected_version=config.ffprobe_version,
        timeout_seconds=config.ffprobe_timeout_seconds,
    )
    try:
        verifier.validate_runtime()
    except (AssetValidationError, OSError) as exc:
        raise CandidateStartupError("ffprobe runtime preflight failed") from exc

    adapter = YtDlpAdapter(
        factory=factory,
        runner=command_runner,  # type: ignore[arg-type]
        cookie_resolver=cookie_resolver,
        probe_timeout_seconds=config.probe_timeout_seconds,
        download_timeout_seconds=config.download_timeout_seconds,
    )
    database = Database(config.database_path)
    database.initialize()
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
        network_guard=guard,
        runtime_logger=runtime_logger,
    )


def _validate_yt_dlp_runtime(
    factory: YtDlpCommandFactory,
    runner: CommandRunner,
) -> None:
    command = factory.version_command()
    try:
        result = runner.run(
            CommandSpec(
                executable=command.executable,
                arguments=command.arguments,
                timeout_seconds=10.0,
                stdout_limit_bytes=256,
                stderr_limit_bytes=1024,
            )
        )
    except (SubprocessExecutionError, OSError) as exc:
        raise CandidateStartupError("yt-dlp runtime preflight failed") from exc
    try:
        observed = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CandidateStartupError("yt-dlp runtime preflight failed") from exc
    if result.returncode != 0 or observed != factory.expected_version:
        raise CandidateStartupError("yt-dlp runtime preflight failed")


def _validate_ffmpeg_runtime(
    config: CandidateWorkerConfig,
    runner: CommandRunner,
) -> None:
    try:
        result = runner.run(
            CommandSpec(
                executable=config.ffmpeg_executable,
                arguments=("-version",),
                timeout_seconds=10.0,
                stdout_limit_bytes=64 * 1024,
                stderr_limit_bytes=16 * 1024,
            )
        )
    except (SubprocessExecutionError, OSError) as exc:
        raise CandidateStartupError("ffmpeg runtime preflight failed") from exc
    first_line = result.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    fields = first_line[0].split() if first_line else []
    if (
        result.returncode != 0
        or len(fields) < 3
        or fields[:2] != ["ffmpeg", "version"]
        or fields[2] != config.ffmpeg_version
    ):
        raise CandidateStartupError("ffmpeg runtime preflight failed")


def _require_feature_gate() -> None:
    if os.getenv(FEATURE_GATE) != "1":
        raise CandidateStartupError(
            "candidate real Worker is disabled; set "
            f"{FEATURE_GATE}=1 only inside the isolated Linux deployment"
        )


def _validate_managed_paths(config: CandidateWorkerConfig) -> None:
    _require_plain_directory(config.data_root, "data root")
    _require_plain_directory(config.database_path.parent, "database directory")
    _require_plain_file(config.yt_dlp_executable, "yt-dlp executable")
    if config.yt_dlp_zipimport_entrypoint is not None:
        _require_plain_file(
            config.yt_dlp_zipimport_entrypoint,
            "yt-dlp zipimport entrypoint",
        )
    if config.js_runtime is not None:
        _require_plain_file(
            config.js_runtime.executable,
            "yt-dlp JavaScript runtime executable",
        )
    _require_plain_directory(config.ffmpeg_directory, "ffmpeg directory")
    _require_plain_file(config.ffmpeg_executable, "ffmpeg executable")
    _require_plain_file(config.ffprobe_executable, "ffprobe executable")
    for executable, label in (
        (config.ffmpeg_executable, "ffmpeg"),
        (config.ffprobe_executable, "ffprobe"),
    ):
        try:
            executable.relative_to(config.ffmpeg_directory)
        except ValueError as exc:
            raise CandidateStartupError(
                f"{label} executable must stay below the explicit ffmpeg directory"
            ) from exc
    if config.database_path.exists():
        _require_plain_file(config.database_path, "database")
    _reject_existing_link_components(config.unix_socket_path, "Unix socket path")


def _require_plain_directory(path: Path, label: str) -> None:
    _reject_existing_link_components(path, label)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CandidateStartupError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
    ):
        raise CandidateStartupError(f"{label} must be a plain directory")


def _require_plain_file(path: Path, label: str) -> None:
    _reject_existing_link_components(path, label)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CandidateStartupError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
    ):
        raise CandidateStartupError(f"{label} must be a plain regular file")


def _reject_existing_link_components(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CandidateStartupError(f"{label} path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise CandidateStartupError(f"{label} path may not contain links")


def _is_reparse(info: object) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(reparse and attributes & reparse)


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


def _runtime_logger(config: CandidateWorkerConfig) -> RuntimeLogger:
    return RuntimeLogger(
        component="candidate-worker",
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


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    startup_message: str | None = None
    try:
        _require_feature_gate()
    except CandidateStartupError as exc:
        startup_message = str(exc)
    if startup_message is not None:
        raise SystemExit(startup_message)
    runtime_logger: RuntimeLogger | None = None
    try:
        config = config_from_args(args)
        runtime_logger = _runtime_logger(config)
        worker = build_candidate_worker(config, runtime_logger=runtime_logger)
    except Exception as exc:  # noqa: BLE001 - public process boundary redacts causes
        if runtime_logger is not None:
            runtime_logger.emit(
                "worker.startup_failed",
                level="ERROR",
                worker_id=config.worker_id,
                exception_type=safe_exception_type(exc),
            )
        if isinstance(exc, CookiePreparationError):
            startup_message = "candidate Worker credential preparation failed"
        elif isinstance(exc, (CandidateStartupError, NetworkIsolationError)):
            startup_message = str(exc)
        else:
            startup_message = "candidate Worker configuration or startup check failed"
    if startup_message is not None:
        # This raise deliberately occurs after the handler so SystemExit has no
        # suppressed context that can retain a credential path or tool output.
        raise SystemExit(startup_message)

    assert runtime_logger is not None
    runtime_logger.emit(
        "worker.started",
        worker_id=config.worker_id,
        adapter="yt_dlp",
    )
    runtime_message: str | None = None
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
    except Exception as exc:  # noqa: BLE001 - public boundary removes sensitive context
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
        runtime_message = "candidate Worker runtime failed safely"
    finally:
        runtime_logger.emit(
            "worker.stopped",
            worker_id=config.worker_id,
            reason=stop_reason,
        )
    if runtime_message is not None:
        raise SystemExit(runtime_message)


if __name__ == "__main__":
    main()


__all__ = [
    "CREDENTIAL_PROFILE_WIRING_STATUS",
    "FEATURE_GATE",
    "CandidateStartupError",
    "CandidateWorkerConfig",
    "build_candidate_worker",
    "build_parser",
    "config_from_args",
    "main",
]

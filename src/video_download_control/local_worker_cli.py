"""Command-line arguments and process reporting for the local Worker."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path

from .candidate_cookies import (
    DEFAULT_MAX_COOKIE_BYTES,
    CookiePreparationError,
    CookieSource,
)
from .cookie_source_config import CookieSourceConfigError, load_cookie_source_config
from .domain import Platform
from .runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
    safe_exception_type,
)
from .worker import Worker
from .worker_pool import run_concurrent_worker
from .local_worker import (
    FEATURE_GATE,
    LocalDirectNetworkGuard,
    LocalWorkerConfig,
    LocalWorkerStartupError,
    build_local_worker,
    exclusive_local_worker,
    local_worker_logger,
    require_local_worker_opt_in,
    validate_local_worker_paths,
)
from .worker_cli_support import (
    js_runtime as _js_runtime,
    poll_interval as _poll_interval,
    print_worker_result as _print_result,
)


def _absolute_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise argparse.ArgumentTypeError("an explicit normalized absolute path is required")
    return path


def _cookie_source(raw: str) -> CookieSource:
    """Parse one redaction-safe PLATFORM:OPAQUE_REF=ABSOLUTE_PATH mapping."""

    try:
        identity, raw_path = raw.split("=", 1)
        raw_platform, credential_ref = identity.split(":", 1)
        path = Path(raw_path)
        if not path.is_absolute() or Path(os.path.abspath(path)) != path:
            raise ValueError
        return CookieSource(
            platform=Platform(raw_platform),
            credential_ref=credential_ref,
            path=path,
        )
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "cookie source must use PLATFORM:OPAQUE_REF=ABSOLUTE_PATH"
        ) from None


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
    parser.add_argument(
        "--max-cookie-bytes",
        type=int,
        default=DEFAULT_MAX_COOKIE_BYTES,
        help="maximum bytes accepted from each configured read-only Cookie source",
    )
    cookie_input = parser.add_mutually_exclusive_group()
    cookie_input.add_argument(
        "--cookie-source",
        action="append",
        default=[],
        type=_cookie_source,
        metavar="PLATFORM:OPAQUE_REF=ABSOLUTE_PATH",
        help=(
            "map one platform credential reference to a read-only Netscape "
            "Cookie file; repeat for different platforms"
        ),
    )
    cookie_input.add_argument(
        "--cookie-config",
        type=_absolute_path,
        metavar="ABSOLUTE_JSON_PATH",
        help=(
            "load platform credential references and Cookie source paths from "
            "one strict read-only JSON file"
        ),
    )
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--check",
        action="store_true",
        help="validate database, tools and Cookie sources without claiming a job",
    )
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


def config_from_args(args: argparse.Namespace) -> LocalWorkerConfig:
    data_root = args.data_root
    cookie_config_path = args.cookie_config
    cookie_sources = tuple(args.cookie_source)
    if cookie_config_path is not None:
        try:
            cookie_sources = load_cookie_source_config(
                cookie_config_path
            ).sources
        except CookieSourceConfigError:
            raise ValueError(
                "local Worker cookie source configuration is invalid"
            ) from None
    return LocalWorkerConfig(
        data_root=data_root,
        database_path=args.database_path or data_root / "control.sqlite3",
        tool_root=args.tool_root,
        worker_id=args.worker_id,
        allow_direct_network=args.allow_direct_network,
        js_runtime=args.js_runtime,
        cookie_sources=cookie_sources,
        cookie_config_path=cookie_config_path,
        max_cookie_bytes=args.max_cookie_bytes,
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


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        require_local_worker_opt_in(allow_direct_network=args.allow_direct_network)
        config = config_from_args(args)
        # This check intentionally precedes logger construction, tool execution,
        # and any database access.  A typo must not create a second empty queue.
        validate_local_worker_paths(config)
    except (LocalWorkerStartupError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

    try:
        with exclusive_local_worker(config.data_root):
            try:
                runtime_logger = local_worker_logger(config)
            except Exception:  # noqa: BLE001 - no logger exists at this boundary
                raise SystemExit(
                    "local real Worker runtime log could not be initialized; "
                    "no job was claimed"
                ) from None
            if not runtime_logger.emit(
                "worker.preflight_started" if args.check else "worker.initializing",
                worker_id=config.worker_id,
                adapter="yt_dlp",
                direct_network_enabled=True,
                js_runtime_enabled=config.js_runtime is not None,
            ):
                raise SystemExit(
                    "local real Worker runtime log is unavailable; no job was claimed"
                )
            worker: Worker | None = None
            startup_message: str | None = None
            try:
                worker = build_local_worker(
                    config,
                    runtime_logger=runtime_logger,
                )
                if not args.check:
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
                failure_event = (
                    "worker.preflight_failed"
                    if args.check
                    else "worker.startup_failed"
                )
                runtime_logger.emit(
                    failure_event,
                    level="ERROR",
                    worker_id=config.worker_id,
                    exception_type=safe_exception_type(exc),
                )
                if isinstance(exc, CookiePreparationError):
                    startup_message = (
                        "local real Worker credential preparation failed; "
                        "inspect the runtime log"
                    )
                elif args.check:
                    startup_message = (
                        "local real Worker preflight failed; inspect the runtime log"
                    )
                else:
                    startup_message = (
                        "local real Worker startup failed; inspect the runtime log"
                    )
            if startup_message is not None:
                # Raise outside the handler so SystemExit retains no private
                # exception context from Cookie or toolchain validation.
                raise SystemExit(startup_message)
            assert worker is not None

            if args.check:
                if not runtime_logger.emit(
                    "worker.preflight_succeeded",
                    worker_id=config.worker_id,
                ):
                    raise SystemExit(
                        "local real Worker runtime log is unavailable; "
                        "no job was claimed"
                    )
                print(
                    json.dumps(
                        {
                            "cookie_platforms": [
                                source.platform.value
                                for source in config.cookie_sources
                            ],
                            "status": "ready",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return

            stop_reason = "runtime_error"
            cycle_started = time.perf_counter()
            try:
                if args.drain or args.poll_interval_seconds is not None:
                    run_concurrent_worker(
                        worker,
                        poll_interval_seconds=args.poll_interval_seconds,
                        on_result=_print_result,
                    )
                    stop_reason = "drain_complete"
                else:
                    result = worker.run_once()
                    _print_result(result)
                    stop_reason = "idle" if result is None else "single_run_complete"
                return
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
    "exclusive_local_worker",
    "local_worker_logger",
    "require_local_worker_opt_in",
    "validate_local_worker_paths",
    "build_parser",
    "config_from_args",
    "main",
]

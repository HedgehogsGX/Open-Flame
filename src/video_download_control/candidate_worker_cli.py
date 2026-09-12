"""Command-line arguments and process reporting for the candidate Worker."""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

from .candidate_cookies import CookiePreparationError, CookieSource
from .domain import Platform
from .runtime_logging import RuntimeLogger, safe_exception_type
from .security import NetworkIsolationError
from .candidate_worker import (
    CREDENTIAL_PROFILE_WIRING_STATUS,
    FEATURE_GATE,
    CandidateStartupError,
    CandidateWorkerConfig,
    build_candidate_worker,
    candidate_worker_logger,
    require_candidate_worker_enabled,
)
from .worker_cli_support import (
    js_runtime as _js_runtime,
    poll_interval as _poll_interval,
    print_worker_result as _print_result,
)


def _absolute_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("an explicit absolute path is required")
    return path


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


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    startup_message: str | None = None
    try:
        require_candidate_worker_enabled()
    except CandidateStartupError as exc:
        startup_message = str(exc)
    if startup_message is not None:
        raise SystemExit(startup_message)
    runtime_logger: RuntimeLogger | None = None
    try:
        config = config_from_args(args)
        runtime_logger = candidate_worker_logger(config)
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
    "candidate_worker_logger",
    "require_candidate_worker_enabled",
    "build_parser",
    "config_from_args",
    "main",
]

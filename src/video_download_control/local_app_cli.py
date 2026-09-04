"""Thin command-line boundary for the Windows local application supervisor.

The supervisor owns business storage and process lifecycle side effects. This
module parses a bounded operator-facing configuration, persists independent
fixed-directory failure diagnostics, and emits a stable JSON contract that
never includes paths, arguments, environment values, or exception messages.
"""

from __future__ import annotations

import argparse
import math
import multiprocessing
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from .adapters import YtDlpJsRuntime, YtDlpJsRuntimeName
from .candidate_cookies import DEFAULT_MAX_COOKIE_BYTES
from .local_app import (
    LocalAppConfig,
    LocalAppError,
    LocalAppStartupError,
    LocalAppStartupFailure,
    run_local_app,
)
from .runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
)
from .startup_diagnostics import DiagnosticCode, emit_failure

_MAX_COOKIE_BYTES = 64 * 1024 * 1024
_MAX_STARTUP_TIMEOUT_SECONDS = 10 * 60.0
_MAX_SHUTDOWN_TIMEOUT_SECONDS = 5 * 60.0


class _PrivateArgumentParser(argparse.ArgumentParser):
    """Reject invalid arguments without reflecting the raw command line."""

    def error(self, message: str) -> None:
        del message
        emit_failure(DiagnosticCode.INVALID_ARGUMENTS)
        self.exit(2)


def _absolute_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise argparse.ArgumentTypeError(
            "an explicit normalized absolute path is required"
        )
    return path


def _bounded_integer(
    *, label: str, minimum: int, maximum: int
) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {minimum} and {maximum}"
            )
        return value

    return parse


def _bounded_number(
    *, label: str, minimum: float, maximum: float
) -> Callable[[str], float]:
    def parse(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{label} must be a number") from None
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} is outside the supported range"
            )
        return value

    return parse


def _js_runtime(raw: str) -> YtDlpJsRuntime:
    name, separator, raw_path = raw.partition(":")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError(
            "JavaScript runtime must use NAME:ABSOLUTE_EXECUTABLE"
        )
    try:
        return YtDlpJsRuntime(YtDlpJsRuntimeName(name), Path(raw_path))
    except (OSError, TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "JavaScript runtime must use a supported name and absolute plain executable"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = _PrivateArgumentParser(
        prog="video-download-local-app",
        description=(
            "Run the Windows local control plane and direct-network Worker "
            "under one supervised lifecycle."
        )
    )
    parser.add_argument(
        "--app-root",
        type=_absolute_path,
        help=(
            "absolute application root; defaults below Windows LOCALAPPDATA "
            "and never depends on the current directory"
        ),
    )
    parser.add_argument(
        "--tool-root",
        type=_absolute_path,
        help="optional absolute override for the verified Windows tool bundle",
    )
    parser.add_argument(
        "--port",
        type=_bounded_integer(label="port", minimum=1, maximum=65535),
        default=8000,
        help="numeric loopback HTTP port (default: 8000)",
    )
    parser.add_argument(
        "--js-runtime",
        type=_js_runtime,
        metavar="NAME:ABSOLUTE_EXECUTABLE",
        help="optional explicit yt-dlp JavaScript runtime",
    )
    parser.add_argument(
        "--cookie-config",
        type=_absolute_path,
        metavar="ABSOLUTE_JSON",
        help=(
            "optional read-only Cookie-source mapping; its private contents "
            "are never accepted as command-line values"
        ),
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="do not ask Windows to open the ready loopback page",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate startup without claiming a Job or keeping services running",
    )
    parser.add_argument(
        "--allow-direct-network",
        action="store_true",
        help="acknowledge that the local Worker uses the host network directly",
    )
    parser.add_argument(
        "--startup-timeout-seconds",
        type=_bounded_number(
            label="startup timeout",
            minimum=1.0,
            maximum=_MAX_STARTUP_TIMEOUT_SECONDS,
        ),
        default=120.0,
        help="bounded startup deadline in seconds (default: 120)",
    )
    parser.add_argument(
        "--shutdown-timeout-seconds",
        type=_bounded_number(
            label="shutdown timeout",
            minimum=1.0,
            maximum=_MAX_SHUTDOWN_TIMEOUT_SECONDS,
        ),
        default=30.0,
        help="bounded graceful-shutdown deadline in seconds (default: 30)",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=_bounded_number(label="poll interval", minimum=0.1, maximum=60.0),
        default=2.0,
        help="bounded idle queue and supervisor poll interval (default: 2)",
    )
    parser.add_argument(
        "--max-cookie-bytes",
        type=_bounded_integer(
            label="Cookie byte limit",
            minimum=1,
            maximum=_MAX_COOKIE_BYTES,
        ),
        default=DEFAULT_MAX_COOKIE_BYTES,
        help="maximum bytes accepted from each Cookie source (default: 8388608)",
    )
    parser.add_argument(
        "--runtime-log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="structured runtime log threshold (default: INFO)",
    )
    parser.add_argument(
        "--runtime-log-max-bytes",
        type=_bounded_integer(
            label="runtime log max bytes",
            minimum=1024,
            maximum=1024 * 1024 * 1024,
        ),
        default=DEFAULT_RUNTIME_LOG_MAX_BYTES,
        help="maximum bytes in each active structured log (default: 10485760)",
    )
    parser.add_argument(
        "--runtime-log-backup-count",
        type=_bounded_integer(
            label="runtime log backup count",
            minimum=1,
            maximum=20,
        ),
        default=DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
        help="number of rotated structured logs retained (default: 5)",
    )
    return parser


def _write_error(
    error_code: str, *, startup_failure: LocalAppStartupFailure | None = None
) -> None:
    if type(startup_failure) is LocalAppStartupFailure:
        code = DiagnosticCode(startup_failure.value)
    elif error_code == "internal_error":
        code = DiagnosticCode.INTERNAL_ERROR
    else:
        code = DiagnosticCode.LOCAL_APP_FAILED
    emit_failure(code)


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    arguments = build_parser().parse_args(argv)
    try:
        config = LocalAppConfig.from_args(arguments)
        return run_local_app(config)
    except LocalAppStartupError as exc:
        _write_error("local_app_failed", startup_failure=exc.failure)
        return 2
    except (LocalAppError, ValueError):
        _write_error("local_app_failed")
        return 2
    except Exception:  # noqa: BLE001 - sanitize the final process boundary
        _write_error("internal_error")
        return 70


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]

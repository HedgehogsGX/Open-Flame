from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from .api import create_app
from .config import Settings
from .runtime_logging import RuntimeLogConfig, RuntimeLogger, safe_exception_type


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="video-download-control",
        description=(
            "Run the loopback-only video download control plane. "
            "Runtime configuration is read from VDC_* environment variables."
        ),
    )


def main(argv: Sequence[str] | None = None) -> None:
    build_parser().parse_args(argv)
    settings = Settings.from_env()
    runtime_logger = RuntimeLogger(
        component="control",
        config=RuntimeLogConfig(
            directory=settings.data_root / "logs",
            level=settings.runtime_log_level,
            max_bytes=settings.runtime_log_max_bytes,
            backup_count=settings.runtime_log_backup_count,
        ),
    )
    app = create_app(settings, runtime_logger=runtime_logger)
    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            reload=False,
            access_log=False,
        )
    except Exception as exc:  # noqa: BLE001 - sanitize process-boundary failure
        runtime_logger.emit(
            "control.startup_failed",
            level="ERROR",
            exception_type=safe_exception_type(exc),
        )
        raise SystemExit("control startup failed; inspect the runtime log") from None


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from time import perf_counter

from .adapters import ScriptedFakeAdapter
from .assets import AssetStore, NonEmptyTestVerifier
from .config import Settings
from .database import Database
from .runtime_logging import RuntimeLogConfig, RuntimeLogger, safe_exception_type
from .worker import Worker
from .worker_repository import WorkerRepository

_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_worker_id(value: object) -> str:
    candidate = str(value)
    return candidate if _WORKER_ID.fullmatch(candidate) else "worker-id-unavailable"


def _path_trees_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Video Download Control independent Worker"
    )
    parser.add_argument(
        "--offline-fake",
        action="store_true",
        help="run the deterministic offline fake adapter (QA only)",
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        help="process queued jobs until none are immediately available",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="explicit isolated data root for offline QA only",
    )
    parser.add_argument("--worker-id", default="offline-fake-worker")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.offline_fake:
        raise SystemExit(
            "真实 yt-dlp Adapter 尚未启用；当前 Worker 只支持显式离线 QA 模式"
        )
    if os.getenv("VDC_ENABLE_OFFLINE_FAKE_WORKER") != "1":
        raise SystemExit(
            "离线假 Worker 会消费排队任务；如确认用于隔离 QA，请设置 "
            "VDC_ENABLE_OFFLINE_FAKE_WORKER=1"
        )

    configured_settings = Settings.from_env()
    qa_data_root = args.data_root.expanduser().resolve()
    qa_database_path = qa_data_root / "control.sqlite3"
    ordinary_roots = {
        configured_settings.data_root,
        configured_settings.database_path.parent,
    }
    same_database_file = qa_database_path == configured_settings.database_path or (
        qa_database_path.exists()
        and configured_settings.database_path.exists()
        and qa_database_path.samefile(configured_settings.database_path)
    )
    if (
        any(
            _path_trees_overlap(qa_data_root, ordinary_root)
            for ordinary_root in ordinary_roots
        )
        or same_database_file
    ):
        raise SystemExit("离线假 Worker 的 --data-root 必须与普通控制面数据目录隔离")

    log_worker_id = _safe_worker_id(args.worker_id)
    runtime_logger = RuntimeLogger(
        component="offline-worker",
        config=RuntimeLogConfig(
            directory=qa_data_root / "logs",
            level=configured_settings.runtime_log_level,
            max_bytes=configured_settings.runtime_log_max_bytes,
            backup_count=configured_settings.runtime_log_backup_count,
        ),
        instance_id=log_worker_id,
    )
    try:
        database = Database(qa_database_path)
        database.initialize()
        worker = Worker(
            worker_id=args.worker_id,
            repository=WorkerRepository(database),
            adapter=ScriptedFakeAdapter(),
            asset_store=AssetStore(
                qa_data_root,
                min_free_bytes=configured_settings.storage_min_free_bytes,
            ),
            verifier=NonEmptyTestVerifier(),
            runtime_logger=runtime_logger,
        )
    except Exception as exc:
        runtime_logger.emit(
            "worker.startup_failed",
            level="ERROR",
            worker_id=log_worker_id,
            exception_type=safe_exception_type(exc),
        )
        raise

    runtime_logger.emit(
        "worker.started",
        worker_id=log_worker_id,
        adapter="scripted_fake",
    )
    stop_reason = "runtime_error"
    cycle_started = perf_counter()

    try:
        while True:
            result = worker.run_once()
            if result is None:
                print(json.dumps({"status": "idle"}, ensure_ascii=False))
                stop_reason = "drain_complete" if args.drain else "idle"
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
            if not args.drain:
                stop_reason = "single_run_complete"
                return
            cycle_started = perf_counter()
    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"
        raise
    except Exception as exc:
        runtime_logger.emit(
            "worker.cycle_failed",
            level="ERROR",
            worker_id=log_worker_id,
            exception_type=safe_exception_type(exc),
            duration_ms=min(
                max((perf_counter() - cycle_started) * 1000.0, 0.0),
                86_400_000.0,
            ),
        )
        raise
    finally:
        runtime_logger.emit(
            "worker.stopped",
            worker_id=log_worker_id,
            reason=stop_reason,
        )


if __name__ == "__main__":
    main()

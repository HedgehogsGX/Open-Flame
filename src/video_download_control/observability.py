from __future__ import annotations

import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .database import Database


def collect_metrics(database: Database, data_root: Path) -> dict[str, object]:
    with database.connect() as connection:
        job_rows = connection.execute(
            """
            SELECT s.platform, j.status, COUNT(*) AS count
            FROM download_jobs AS j
            JOIN source_items AS s ON s.id = j.source_item_id
            GROUP BY s.platform, j.status
            ORDER BY s.platform, j.status
            """
        ).fetchall()
        outcome_rows = connection.execute(
            """
            SELECT s.platform, j.status, COUNT(*) AS count
            FROM download_jobs AS j
            JOIN source_items AS s ON s.id = j.source_item_id
            WHERE j.job_kind = 'download'
            GROUP BY s.platform, j.status
            ORDER BY s.platform, j.status
            """
        ).fetchall()
        error_rows = connection.execute(
            """
            SELECT final_error_code, COUNT(*) AS count
            FROM download_jobs
            WHERE final_error_code IS NOT NULL
            GROUP BY final_error_code
            ORDER BY final_error_code
            """
        ).fetchall()
        attempt_row = connection.execute(
            """
            SELECT COALESCE(AVG(attempt_count), 0) AS average_attempts
            FROM download_jobs
            """
        ).fetchone()
        duration_rows = connection.execute(
            """
            SELECT (julianday(finished_at) - julianday(started_at)) * 86400.0
                   AS duration_seconds
            FROM job_attempts
            WHERE finished_at IS NOT NULL AND started_at IS NOT NULL
            """
        ).fetchall()
        queue_control = connection.execute(
            "SELECT paused, reason FROM queue_control WHERE id = 1"
        ).fetchone()
        circuit_rows = connection.execute(
            """
            SELECT platform, state, consecutive_failures, last_error_code,
                   opened_at, cooldown_until, requires_manual_reset
            FROM platform_circuits ORDER BY platform
            """
        ).fetchall()

    if queue_control is None:
        raise RuntimeError("queue control singleton is missing")

    disk = shutil.disk_usage(data_root)
    jobs = [
        {
            "platform": row["platform"],
            "status": row["status"],
            "count": int(row["count"]),
        }
        for row in job_rows
    ]
    counts_by_platform: dict[str, Counter[str]] = {}
    for row in outcome_rows:
        counts_by_platform.setdefault(row["platform"], Counter())[row["status"]] = int(
            row["count"]
        )
    outcomes = []
    for platform, counts in sorted(counts_by_platform.items()):
        terminal = counts["ready"] + counts["failed"] + counts["canceled"]
        outcomes.append(
            {
                "platform": platform,
                "ready": counts["ready"],
                "failed": counts["failed"],
                "canceled": counts["canceled"],
                "success_rate": counts["ready"] / terminal if terminal else None,
            }
        )
    durations = sorted(
        float(row["duration_seconds"])
        for row in duration_rows
        if row["duration_seconds"] is not None and row["duration_seconds"] >= 0
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "queue_depth": sum(
            item["count"] for item in jobs if item["status"] == "queued"
        ),
        "active_jobs": sum(
            item["count"]
            for item in jobs
            if item["status"]
            in {"probing", "downloading", "postprocessing", "verifying"}
        ),
        "average_attempts": float(attempt_row["average_attempts"]),
        "attempt_duration_p50_seconds": _percentile(durations, 0.50),
        "attempt_duration_p95_seconds": _percentile(durations, 0.95),
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "queue_paused": bool(queue_control["paused"]),
        "queue_pause_reason": queue_control["reason"],
        "jobs": jobs,
        "platform_outcomes": outcomes,
        "platform_circuits": [
            {
                **dict(row),
                "requires_manual_reset": bool(row["requires_manual_reset"]),
            }
            for row in circuit_rows
        ],
        "errors": {
            row["final_error_code"]: int(row["count"]) for row in error_rows
        },
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, int((len(values) * fraction) + 0.999999) - 1))
    return values[index]

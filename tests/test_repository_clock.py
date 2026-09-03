from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from video_download_control.database import Database
from video_download_control.repository import BatchRepository
from video_download_control.service import BatchService


def build_service(path: Path, clock) -> BatchService:
    database = Database(path / "control.sqlite3")
    database.initialize()
    return BatchService(
        repository=BatchRepository(database, clock=clock),
        max_batch_urls=50,
        route_policy_version="clock-test-v1",
    )


def test_repository_clock_is_injectable_and_normalized_to_utc(tmp_path: Path) -> None:
    fixed = datetime(2026, 9, 3, 9, 30, tzinfo=timezone(timedelta(hours=9, minutes=30)))
    service = build_service(tmp_path, lambda: fixed)

    batch = service.create_batch(
        name="deterministic clock",
        raw_inputs=["https://youtu.be/repository-clock"],
    )

    assert batch["created_at"] == "2026-09-03T00:00:00.000Z"
    assert batch["updated_at"] == "2026-09-03T00:00:00.000Z"
    assert batch["jobs"][0]["available_at"] == "2026-09-03T00:00:00.000Z"


def test_repository_rejects_naive_injected_clock(tmp_path: Path) -> None:
    service = build_service(tmp_path, lambda: datetime(2026, 9, 3))

    with pytest.raises(ValueError, match="timezone-aware"):
        service.create_batch(
            name="invalid clock",
            raw_inputs=["https://youtu.be/repository-clock-naive"],
        )

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_download_control.config import Settings
from video_download_control.database import Database
from video_download_control.repository import BatchRepository
from video_download_control.service import BatchService

REPOSITORY_NOW = datetime(2026, 9, 2, 23, 59, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    return Settings(
        data_root=data_root,
        database_path=data_root / "test.sqlite3",
        max_batch_urls=50,
        route_policy_version="test-v1",
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


@pytest.fixture
def repository(database: Database) -> BatchRepository:
    return BatchRepository(database, clock=lambda: REPOSITORY_NOW)


@pytest.fixture
def service(repository: BatchRepository, settings: Settings) -> BatchService:
    return BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
    )

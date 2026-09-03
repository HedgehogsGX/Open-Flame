from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from video_download_control.database import Database
from video_download_control.repository import BatchRepository
from video_download_control.runtime_logging import read_recent_runtime_events
from video_download_control.service import BatchService
from video_download_control.worker_cli import build_parser, main


def test_offline_fake_requires_explicit_qa_data_root() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--offline-fake"])


def test_offline_fake_cannot_consume_configured_default_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    normal_root = tmp_path / "normal"
    normal_database_path = normal_root / "control.sqlite3"
    normal_database = Database(normal_database_path)
    normal_database.initialize()
    service = BatchService(
        repository=BatchRepository(normal_database),
        max_batch_urls=50,
        route_policy_version="test-v1",
    )
    batch = service.create_batch(
        name="must remain queued",
        raw_inputs=["https://www.youtube.com/watch?v=normal-queue"],
    )

    qa_root = tmp_path / "offline-qa"
    monkeypatch.setenv("VDC_ENABLE_OFFLINE_FAKE_WORKER", "1")
    monkeypatch.setenv("VDC_DATA_ROOT", str(normal_root))
    monkeypatch.setenv("VDC_DATABASE_PATH", str(normal_database_path))
    monkeypatch.setenv("VDC_RUNTIME_LOG_LEVEL", "INFO")
    monkeypatch.setenv("VDC_RUNTIME_LOG_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setenv("VDC_RUNTIME_LOG_BACKUP_COUNT", "2")
    monkeypatch.setattr(
        sys,
        "argv",
        ["video-download-worker", "--offline-fake", "--data-root", str(qa_root)],
    )

    main()

    assert capsys.readouterr().out.strip() == '{"status": "idle"}'
    assert service.get_batch(batch["id"])["jobs"][0]["status"] == "queued"
    assert (qa_root / "control.sqlite3").is_file()
    events = read_recent_runtime_events(qa_root / "logs")
    assert {event["event"] for event in events} == {
        "worker.started",
        "worker.stopped",
    }
    started = next(event for event in events if event["event"] == "worker.started")
    stopped = next(event for event in events if event["event"] == "worker.stopped")
    assert started["worker_id"] == "offline-fake-worker"
    assert started["adapter"] == "scripted_fake"
    assert stopped["reason"] == "idle"
    assert not any(
        "offline-fake-worker" in path.name
        for path in (qa_root / "logs").iterdir()
    )


def test_offline_fake_rejects_overlapping_configured_default_data_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    normal_root = tmp_path / "normal"
    monkeypatch.setenv("VDC_ENABLE_OFFLINE_FAKE_WORKER", "1")
    monkeypatch.setenv("VDC_DATA_ROOT", str(normal_root))

    for unsafe_root in (normal_root, normal_root / "qa", tmp_path):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "video-download-worker",
                "--offline-fake",
                "--data-root",
                str(unsafe_root),
            ],
        )
        with pytest.raises(SystemExit, match="普通控制面"):
            main()


def test_offline_fake_rejects_hard_link_to_default_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    normal_root = tmp_path / "normal"
    normal_database_path = normal_root / "control.sqlite3"
    Database(normal_database_path).initialize()
    qa_root = tmp_path / "qa"
    qa_root.mkdir()
    os.link(normal_database_path, qa_root / "control.sqlite3")
    monkeypatch.setenv("VDC_ENABLE_OFFLINE_FAKE_WORKER", "1")
    monkeypatch.setenv("VDC_DATA_ROOT", str(normal_root))
    monkeypatch.setenv("VDC_DATABASE_PATH", str(normal_database_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["video-download-worker", "--offline-fake", "--data-root", str(qa_root)],
    )

    with pytest.raises(SystemExit, match="普通控制面"):
        main()

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.domain import ErrorCode, JobStatus, Platform
from video_download_control.worker_repository import (
    CircuitResetConflict,
    WorkerRepository,
)


NOW = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)


def make_job(service, url: str) -> str:
    batch = service.create_batch(name="operations", raw_inputs=[url])
    return batch["jobs"][0]["id"]


def claim(
    repository: WorkerRepository,
    worker_id: str,
    now: datetime,
):
    return repository.claim_next(
        worker_id=worker_id,
        adapter="fake",
        adapter_version="test",
        now=now,
        lease_seconds=60,
    )


def test_queue_pause_is_persistent_across_repository_instances_and_resume(
    service, database
) -> None:
    job_id = make_job(
        service, "https://www.youtube.com/watch?v=persistent-pause"
    )
    first_process = WorkerRepository(database)
    second_process = WorkerRepository(database)

    paused = first_process.pause_queue(
        reason=ErrorCode.STORAGE_ERROR,
        now=NOW,
    )

    assert paused["paused"] is True
    assert second_process.get_queue_control()["reason"] == "storage_error"
    assert claim(second_process, "blocked-worker", NOW) is None

    resumed = second_process.resume_queue(now=NOW + timedelta(seconds=1))
    lease = claim(first_process, "restarted-worker", NOW + timedelta(seconds=1))

    assert resumed["paused"] is False
    assert lease is not None
    assert lease.job_id == job_id


def test_rate_limit_opens_cooldown_then_one_successful_probe_closes_it(
    service, database
) -> None:
    repository = WorkerRepository(database)
    job_id = make_job(service, "https://www.youtube.com/watch?v=rate-window")
    first = claim(repository, "first", NOW)
    assert first and first.job_id == job_id

    assert repository.finish_failure(
        first,
        error_code=ErrorCode.RATE_LIMITED,
        diagnostic="limited",
        now=NOW,
        retry_at=NOW + timedelta(seconds=60),
    ) == JobStatus.QUEUED
    circuit = repository.list_platform_circuits()[0]
    assert circuit == {
        "platform": "youtube",
        "state": "open",
        "consecutive_failures": 1,
        "last_error_code": "rate_limited",
        "opened_at": "2026-09-03T02:00:00.000Z",
        "cooldown_until": "2026-09-03T02:01:00.000Z",
        "requires_manual_reset": False,
        "updated_at": "2026-09-03T02:00:00.000Z",
    }
    assert claim(repository, "too-early", NOW + timedelta(seconds=59)) is None

    probe_time = NOW + timedelta(seconds=60)
    probe = claim(repository, "probe", probe_time)
    assert probe and probe.job_id == job_id
    assert repository.list_platform_circuits()[0]["state"] == "half_open"
    repository.transition(
        probe,
        status=JobStatus.DOWNLOADING,
        progress=0.5,
        now=probe_time,
    )
    repository.transition(
        probe,
        status=JobStatus.VERIFYING,
        progress=0.9,
        now=probe_time,
    )
    assert repository.finish_success(probe, now=probe_time) == JobStatus.READY

    closed = repository.list_platform_circuits()[0]
    assert closed["state"] == "closed"
    assert closed["consecutive_failures"] == 0
    assert closed["last_error_code"] is None


def test_bilibili_rate_limit_opens_only_the_bilibili_cooldown(
    service, database
) -> None:
    repository = WorkerRepository(database)
    job_id = make_job(service, "https://www.bilibili.com/video/BV1Fb4111732/")
    lease = claim(repository, "bilibili-worker", NOW)
    assert lease and lease.job_id == job_id
    assert lease.platform is Platform.BILIBILI

    assert repository.finish_failure(
        lease,
        error_code=ErrorCode.RATE_LIMITED,
        diagnostic="platform request throttling",
        now=NOW,
        retry_at=NOW + timedelta(seconds=60),
    ) == JobStatus.QUEUED

    assert repository.list_platform_circuits() == [
        {
            "platform": "bilibili",
            "state": "open",
            "consecutive_failures": 1,
            "last_error_code": "rate_limited",
            "opened_at": "2026-09-03T02:00:00.000Z",
            "cooldown_until": "2026-09-03T02:01:00.000Z",
            "requires_manual_reset": False,
            "updated_at": "2026-09-03T02:00:00.000Z",
        }
    ]


def test_repeated_extractor_breakage_requires_explicit_platform_reset(
    service, database
) -> None:
    repository = WorkerRepository(database)
    first_job = make_job(service, "https://www.youtube.com/watch?v=broken-one")
    second_job = make_job(service, "https://www.youtube.com/watch?v=broken-two")
    third_job = make_job(service, "https://www.youtube.com/watch?v=broken-three")

    first = claim(repository, "first", NOW)
    assert first and first.job_id == first_job
    assert repository.finish_failure(
        first,
        error_code=ErrorCode.EXTRACTOR_BROKEN,
        diagnostic="extractor changed",
        now=NOW,
    ) == JobStatus.FAILED

    second = claim(repository, "second", NOW + timedelta(seconds=1))
    assert second and second.job_id == second_job
    assert repository.finish_failure(
        second,
        error_code=ErrorCode.EXTRACTOR_BROKEN,
        diagnostic="extractor changed again",
        now=NOW + timedelta(seconds=1),
    ) == JobStatus.FAILED

    opened = repository.list_platform_circuits()[0]
    assert opened["state"] == "open"
    assert opened["requires_manual_reset"] is True
    assert opened["cooldown_until"] is None
    assert claim(repository, "blocked", NOW + timedelta(days=1)) is None

    reset = repository.reset_platform_circuit(
        platform=Platform.YOUTUBE,
        now=NOW + timedelta(days=1, seconds=1),
    )
    assert reset["state"] == "closed"
    third = claim(repository, "after-reset", NOW + timedelta(days=1, seconds=1))
    assert third and third.job_id == third_job


def test_automatic_rate_limit_cooldown_cannot_be_manually_bypassed(
    service, database
) -> None:
    repository = WorkerRepository(database)
    make_job(service, "https://www.youtube.com/watch?v=cooldown-no-reset")
    lease = claim(repository, "limited", NOW)
    assert lease is not None
    repository.finish_failure(
        lease,
        error_code=ErrorCode.RATE_LIMITED,
        diagnostic="limited",
        now=NOW,
    )

    with pytest.raises(CircuitResetConflict, match="manual reset"):
        repository.reset_platform_circuit(
            platform=Platform.YOUTUBE,
            now=NOW + timedelta(seconds=1),
        )

    circuit = repository.list_platform_circuits()[0]
    assert circuit["state"] == "open"
    assert circuit["requires_manual_reset"] is False


def test_platform_circuit_reset_api_rejects_automatic_cooldown_and_missing(
    settings,
) -> None:
    app = create_app(settings)
    with app.state.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO platform_circuits(
                platform, state, consecutive_failures, last_error_code,
                opened_at, cooldown_until, requires_manual_reset,
                probe_job_id, probe_lease_token, updated_at
            ) VALUES (
                'youtube', 'open', 1, 'rate_limited',
                '2026-09-03T02:00:00.000Z',
                '2026-09-03T02:01:00.000Z', 0,
                NULL, NULL, '2026-09-03T02:00:00.000Z'
            )
            """
        )

    with TestClient(app) as client:
        automatic = client.post("/api/v1/platform-circuits/youtube/reset")
        missing = client.post("/api/v1/platform-circuits/instagram/reset")

    assert automatic.status_code == 409
    assert missing.status_code == 404


def test_expired_half_open_probe_is_recovered_without_sticking_circuit(
    service, database
) -> None:
    repository = WorkerRepository(database)
    job_id = make_job(service, "https://www.youtube.com/watch?v=probe-expiry")
    first = claim(repository, "first", NOW)
    assert first
    repository.finish_failure(
        first,
        error_code=ErrorCode.RATE_LIMITED,
        diagnostic="limited",
        now=NOW,
        retry_at=NOW + timedelta(seconds=60),
    )
    probe_time = NOW + timedelta(seconds=60)
    abandoned_probe = claim(repository, "probe-that-dies", probe_time)
    assert abandoned_probe and abandoned_probe.attempt_no == 2
    assert repository.list_platform_circuits()[0]["state"] == "half_open"

    recovered = claim(
        repository,
        "replacement-probe",
        probe_time + timedelta(seconds=60),
    )

    assert recovered and recovered.job_id == job_id
    assert recovered.attempt_no == 3
    assert repository.attempts_for(job_id)[1]["status"] == "abandoned"
    circuit = repository.list_platform_circuits()[0]
    assert circuit["state"] == "half_open"


@pytest.mark.parametrize(
    "error_code",
    [ErrorCode.AUTHENTICATION_REQUIRED, ErrorCode.CONTENT_UNAVAILABLE],
)
def test_content_specific_failure_does_not_globally_trip_platform(
    service, database, error_code: ErrorCode
) -> None:
    repository = WorkerRepository(database)
    first_job = make_job(service, "https://www.youtube.com/watch?v=auth-one")
    second_job = make_job(service, "https://www.youtube.com/watch?v=auth-two")
    first = claim(repository, "first", NOW)
    assert first and first.job_id == first_job

    repository.finish_failure(
        first,
        error_code=error_code,
        diagnostic="content-specific terminal failure",
        now=NOW,
    )

    assert repository.list_platform_circuits() == []
    second = claim(repository, "second", NOW + timedelta(seconds=1))
    assert second and second.job_id == second_job


def test_operations_api_reports_pause_and_refuses_unsafe_resume(
    tmp_path, monkeypatch
) -> None:
    data_root = tmp_path / "data"
    settings = Settings(
        data_root=data_root,
        database_path=data_root / "control.sqlite3",
        storage_min_free_bytes=100,
    )
    app = create_app(settings)
    app.state.worker_repository.pause_queue(
        reason=ErrorCode.STORAGE_ERROR,
        now=NOW,
    )

    with TestClient(app) as client:
        assert client.get("/health").json()["worker"] == "paused"
        assert client.get("/health/ready").status_code == 503
        assert client.get("/api/v1/operations/queue").json()["paused"] is True

        monkeypatch.setattr(
            "video_download_control.api.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=1000, used=950, free=50),
        )
        denied = client.post("/api/v1/operations/queue/resume")
        assert denied.status_code == 409
        assert client.get("/api/v1/operations/queue").json()["paused"] is True

        monkeypatch.setattr(
            "video_download_control.api.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=1000, used=900, free=100),
        )
        resumed = client.post("/api/v1/operations/queue/resume")

    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False


def test_platform_circuit_api_exposes_and_resets_manual_state(settings) -> None:
    app = create_app(settings)
    with app.state.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO platform_circuits(
                platform, state, consecutive_failures, last_error_code,
                opened_at, cooldown_until, requires_manual_reset,
                probe_job_id, probe_lease_token, updated_at
            ) VALUES (
                'x', 'open', 2, 'extractor_broken',
                '2026-09-03T02:00:00.000Z', NULL, 1,
                NULL, NULL, '2026-09-03T02:00:00.000Z'
            )
            """
        )

    with TestClient(app) as client:
        listed = client.get("/api/v1/platform-circuits")
        reset = client.post("/api/v1/platform-circuits/x/reset")

    assert listed.status_code == 200
    assert listed.json()[0]["requires_manual_reset"] is True
    assert reset.status_code == 200
    assert reset.json()["state"] == "closed"
    assert reset.json()["consecutive_failures"] == 0

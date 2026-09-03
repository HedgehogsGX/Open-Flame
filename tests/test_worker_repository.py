from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from video_download_control.assets import CommittedAsset, VerificationResult
from video_download_control.domain import ErrorCode, JobStatus
from video_download_control.service import BatchService
from video_download_control.worker_repository import (
    InvalidTransition,
    JobLease,
    LostLease,
    WorkerRepository,
)


NOW = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)


def make_job(service: BatchService, url: str) -> str:
    batch = service.create_batch(name=None, raw_inputs=[url])
    return batch["jobs"][0]["id"]


def claim(repository: WorkerRepository, worker_id: str, now: datetime = NOW) -> JobLease | None:
    return repository.claim_next(
        worker_id=worker_id,
        adapter="fake",
        adapter_version="1.0",
        now=now,
    )


@pytest.fixture
def worker_repository(database) -> WorkerRepository:
    return WorkerRepository(database)


def test_two_workers_cannot_claim_the_same_job(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=lease-test1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(lambda name: claim(worker_repository, name), ["a", "b"]))

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    assert claimed[0].attempt_no == 1
    attempts = worker_repository.attempts_for(claimed[0].job_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "running"


def test_local_flat_policy_atomically_skips_unsupported_graph_jobs(
    repository,
    settings,
    database,
    worker_repository: WorkerRepository,
) -> None:
    graph_service = BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
        x_graph_v2_enabled=True,
    )
    graph_batch = graph_service.create_batch(
        name="graph-kept-queued",
        raw_inputs=["https://x.com/example/status/991001"],
    )
    flat_batch = graph_service.create_batch(
        name="flat-can-run",
        raw_inputs=["https://www.youtube.com/watch?v=flat-after-graph"],
    )

    lease = worker_repository.claim_next(
        worker_id="local-flat",
        adapter="yt_dlp",
        adapter_version="test",
        now=NOW,
        supports_exact_selector=False,
        skip_unsupported_graph_jobs=True,
    )

    assert lease is not None
    assert lease.job_id == flat_batch["jobs"][0]["id"]
    with database.connect() as connection:
        graph_row = connection.execute(
            "SELECT status FROM download_jobs WHERE id = ?",
            (graph_batch["jobs"][0]["id"],),
        ).fetchone()
    assert graph_row is not None
    assert graph_row["status"] == "queued"


def test_platform_concurrency_is_one(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=platform-one")
    make_job(service, "https://www.youtube.com/watch?v=platform-two")
    make_job(service, "https://x.com/example/status/900001")

    first = claim(worker_repository, "a")
    second = claim(worker_repository, "b")
    third = claim(worker_repository, "c")

    assert first and first.platform == "youtube"
    assert second and second.platform == "x"
    assert third is None


def test_global_concurrency_is_two_across_three_platforms(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=global-limit")
    make_job(service, "https://x.com/example/status/900002")
    make_job(service, "https://www.bilibili.com/video/BV1xx411c7mD")

    first = claim(worker_repository, "a")
    second = claim(worker_repository, "b")
    third = claim(worker_repository, "c")

    assert first is not None
    assert second is not None
    assert first.platform != second.platform
    assert third is None


def test_heartbeat_extends_lease_and_reports_cancel(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=heartbeat01")
    lease = claim(worker_repository, "worker")
    assert lease

    assert worker_repository.heartbeat(lease, now=NOW + timedelta(seconds=15)) is False
    assert worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=16)) == JobStatus.PROBING
    assert worker_repository.heartbeat(lease, now=NOW + timedelta(seconds=30)) is True
    worker_repository.finish_canceled(lease, now=NOW + timedelta(seconds=31))
    assert worker_repository.get_job(job_id)["status"] == "canceled"
    assert worker_repository.attempts_for(job_id)[0]["status"] == "canceled"


def test_progress_transition_extends_lease(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=progress-renew")
    lease = claim(worker_repository, "worker")
    assert lease

    worker_repository.transition(
        lease,
        status=JobStatus.DOWNLOADING,
        progress=0.5,
        now=NOW + timedelta(seconds=50),
    )

    job = worker_repository.get_job(job_id)
    assert job["heartbeat_at"] == "2026-09-03T00:00:50.000Z"
    assert job["lease_expires_at"] == "2026-09-03T00:01:50.000Z"
    assert claim(worker_repository, "other", NOW + timedelta(seconds=61)) is None
    assert worker_repository.get_job(job_id)["attempt_count"] == 1


def test_queued_cancel_never_creates_attempt(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-queue")
    assert worker_repository.request_cancel(job_id, now=NOW) == JobStatus.CANCELED
    assert claim(worker_repository, "worker") is None
    assert worker_repository.attempts_for(job_id) == []
    batch = service.get_batch(
        worker_repository.get_job(job_id)["batch_id"]
    )
    assert batch["status"] == "canceled"
    assert batch["canceled_count"] == 1
    assert batch["inputs"][0]["status"] == "canceled"


def test_cancel_requested_while_verifying_wins_over_success(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-success")
    lease = claim(worker_repository, "worker")
    assert lease
    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.transition(
        lease, status=JobStatus.VERIFYING, progress=0.9, now=NOW
    )

    assert (
        worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))
        == JobStatus.VERIFYING
    )
    final_status = worker_repository.finish_success(
        lease, now=NOW + timedelta(seconds=2)
    )

    assert final_status == JobStatus.CANCELED
    assert worker_repository.get_job(job_id)["status"] == "canceled"
    assert worker_repository.attempts_for(job_id)[0]["status"] == "canceled"
    batch = service.get_batch(worker_repository.get_job(job_id)["batch_id"])
    assert batch["status"] == "canceled"
    assert batch["inputs"][0]["status"] == "canceled"


def test_expired_lease_preserves_pending_cancel(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-expired")
    lease = claim(worker_repository, "worker")
    assert lease
    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))

    assert claim(worker_repository, "recovery", NOW + timedelta(seconds=61)) is None

    assert worker_repository.get_job(job_id)["status"] == "canceled"
    assert worker_repository.attempts_for(job_id)[0]["status"] == "canceled"
    batch = service.get_batch(worker_repository.get_job(job_id)["batch_id"])
    assert batch["status"] == "canceled"
    assert batch["inputs"][0]["status"] == "canceled"


def test_cancel_requested_while_downloading_wins_over_failure(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=cancel-failure")
    lease = claim(worker_repository, "worker")
    assert lease
    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))

    final_status = worker_repository.finish_failure(
        lease,
        error_code=ErrorCode.NETWORK_ERROR,
        diagnostic="download failed after cancellation",
        now=NOW + timedelta(seconds=2),
        retry_at=NOW + timedelta(seconds=10),
    )

    assert final_status == JobStatus.CANCELED
    assert worker_repository.get_job(job_id)["status"] == "canceled"
    attempt = worker_repository.attempts_for(job_id)[0]
    assert attempt["status"] == "canceled"
    assert attempt["error_code"] is None


def test_state_machine_rejects_regression_and_finishes_success(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=success0001")
    lease = claim(worker_repository, "worker")
    assert lease

    worker_repository.transition(
        lease, status=JobStatus.DOWNLOADING, progress=0.2, now=NOW
    )
    with pytest.raises(InvalidTransition):
        worker_repository.transition(
            lease, status=JobStatus.PROBING, progress=0.3, now=NOW
        )
    with pytest.raises(InvalidTransition, match="progress cannot decrease"):
        worker_repository.transition(
            lease, status=JobStatus.DOWNLOADING, progress=0.1, now=NOW
        )
    worker_repository.transition(
        lease, status=JobStatus.VERIFYING, progress=0.9, now=NOW
    )
    worker_repository.finish_success(lease, now=NOW)

    job = worker_repository.get_job(job_id)
    assert job["status"] == "ready"
    assert job["progress"] == 1
    assert worker_repository.attempts_for(job_id)[0]["status"] == "succeeded"
    batch = service.get_batch(job["batch_id"])
    assert batch["status"] == "ready"
    assert batch["ready_count"] == 1
    assert batch["inputs"][0]["status"] == "ready"


def test_lost_lease_cannot_heartbeat_or_finalize(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    make_job(service, "https://www.youtube.com/watch?v=lost-lease1")
    lease = claim(worker_repository, "worker")
    assert lease
    forged = replace(lease, lease_token="stale")
    with pytest.raises(LostLease):
        worker_repository.heartbeat(forged, now=NOW)
    with pytest.raises(LostLease):
        worker_repository.finish_success(forged, now=NOW)


@pytest.mark.parametrize(
    "operation",
    [
        "heartbeat",
        "transition",
        "record_probe",
        "finish_success",
        "finish_success_with_asset",
        "finish_failure",
        "finish_canceled",
    ],
)
def test_exact_lease_expiry_fences_every_worker_write(
    operation: str,
    service: BatchService,
    worker_repository: WorkerRepository,
) -> None:
    job_id = make_job(
        service, f"https://www.youtube.com/watch?v=expiry-{operation}"
    )
    lease = claim(worker_repository, "worker")
    assert lease
    expires_at = NOW + timedelta(seconds=60)

    if operation in {"finish_success", "finish_success_with_asset"}:
        worker_repository.transition(
            lease, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
        )
        worker_repository.transition(
            lease, status=JobStatus.VERIFYING, progress=0.9, now=NOW
        )
    if operation == "finish_canceled":
        worker_repository.request_cancel(job_id, now=NOW + timedelta(seconds=1))

    with pytest.raises(LostLease):
        if operation == "heartbeat":
            worker_repository.heartbeat(lease, now=expires_at)
        elif operation == "transition":
            worker_repository.transition(
                lease,
                status=JobStatus.DOWNLOADING,
                progress=0.5,
                now=expires_at,
            )
        elif operation == "record_probe":
            worker_repository.record_probe(
                lease,
                expected_item_count=1,
                discovery_snapshot_hash="expiry-snapshot",
                sanitized_source={"title": "must not be recorded"},
                now=expires_at,
            )
        elif operation == "finish_success":
            worker_repository.finish_success(lease, now=expires_at)
        elif operation == "finish_success_with_asset":
            worker_repository.finish_success_with_asset(
                lease,
                asset=CommittedAsset(
                    asset_id="44444444-4444-4444-8444-444444444444",
                    media_key="expiry-asset",
                    media_kind="video",
                    role="original",
                    ordinal=0,
                    size_bytes=1,
                    sha256="a" * 64,
                    relative_original_path="assets/expiry/original/source.mp4",
                    relative_manifest_path="assets/expiry/metadata/manifest.json",
                    manifest_sha256="b" * 64,
                    verification=VerificationResult(media_kind="video"),
                ),
                now=expires_at,
            )
        elif operation == "finish_failure":
            worker_repository.finish_failure(
                lease,
                error_code=ErrorCode.NETWORK_ERROR,
                diagnostic="expired worker failure",
                now=expires_at,
            )
        else:
            worker_repository.finish_canceled(lease, now=expires_at)

    assert worker_repository.attempts_for(job_id)[0]["status"] == "running"
    recovered = claim(worker_repository, "recovery", expires_at)
    if operation == "finish_canceled":
        assert recovered is None
        assert worker_repository.get_job(job_id)["status"] == "canceled"
    else:
        assert recovered and recovered.job_id == job_id
        assert recovered.attempt_no == 2


def test_expired_lease_is_abandoned_and_reclaimed(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=recover0001")
    old = claim(worker_repository, "old")
    assert old

    new = claim(worker_repository, "new", NOW + timedelta(seconds=61))
    assert new
    assert new.job_id == job_id
    assert new.attempt_no == 2
    assert new.attempt_id != old.attempt_id
    attempts = worker_repository.attempts_for(job_id)
    assert [attempt["status"] for attempt in attempts] == ["abandoned", "running"]
    assert attempts[0]["error_code"] == "worker_lost"
    with pytest.raises(LostLease):
        worker_repository.heartbeat(old, now=NOW + timedelta(seconds=62))


def test_failure_can_retry_but_hard_stops_at_four_attempts(
    service: BatchService, worker_repository: WorkerRepository
) -> None:
    job_id = make_job(service, "https://www.youtube.com/watch?v=retry-test1")
    current_time = NOW
    for expected_attempt in range(1, 5):
        lease = claim(worker_repository, f"worker-{expected_attempt}", current_time)
        assert lease and lease.attempt_no == expected_attempt
        status = worker_repository.finish_failure(
            lease,
            error_code=ErrorCode.NETWORK_ERROR,
            diagnostic="temporary",
            now=current_time,
            retry_at=current_time + timedelta(seconds=1),
        )
        expected = JobStatus.QUEUED if expected_attempt < 4 else JobStatus.FAILED
        assert status == expected
        current_time += timedelta(seconds=1)

    assert worker_repository.get_job(job_id)["final_error_code"] == "network_error"
    assert claim(worker_repository, "fifth", current_time) is None
    assert len(worker_repository.attempts_for(job_id)) == 4
    batch = service.get_batch(worker_repository.get_job(job_id)["batch_id"])
    assert batch["status"] == "failed"
    assert batch["failed_count"] == 1
    assert batch["inputs"][0]["error_code"] == "network_error"


def test_input_and_batch_wait_for_all_jobs_before_terminal_aggregation(
    service: BatchService, worker_repository: WorkerRepository, database
) -> None:
    first_job_id = make_job(
        service, "https://www.youtube.com/watch?v=aggregate-first"
    )
    first_job = worker_repository.get_job(first_job_id)
    second_source_id = "22222222-2222-4222-8222-222222222222"
    second_job_id = "33333333-3333-4333-8333-333333333333"
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_jobs SET created_at = ? WHERE id = ?",
            ("2026-09-02T23:59:00.000Z", first_job_id),
        )
        connection.execute(
            """
            INSERT INTO source_items(
                id, platform, source_type, source_id, canonical_url,
                created_at, updated_at
            ) VALUES (?, 'youtube', 'youtube_video', ?, ?, ?, ?)
            """,
            (
                second_source_id,
                "aggregate-second",
                "https://www.youtube.com/watch?v=aggregate-second",
                "2026-09-03T00:00:00.000Z",
                "2026-09-03T00:00:00.000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO download_jobs(
                id, batch_id, input_record_id, source_item_id, job_kind,
                status, progress, route_policy_version, available_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'download', 'queued', 0, 'test-v1', ?, ?, ?)
            """,
            (
                second_job_id,
                first_job["batch_id"],
                first_job["input_record_id"],
                second_source_id,
                "2026-09-03T00:00:00.000Z",
                "2026-09-03T00:00:00.000Z",
                "2026-09-03T00:00:00.000Z",
            ),
        )

    first = claim(worker_repository, "first")
    assert first and first.job_id == first_job_id
    worker_repository.transition(
        first, status=JobStatus.DOWNLOADING, progress=0.5, now=NOW
    )
    worker_repository.transition(
        first, status=JobStatus.VERIFYING, progress=0.9, now=NOW
    )
    assert worker_repository.finish_success(first, now=NOW) == JobStatus.READY

    pending = service.get_batch(first_job["batch_id"])
    assert pending["status"] == "queued"
    assert pending["queued_count"] == 1
    assert pending["ready_count"] == 0
    assert pending["inputs"][0]["status"] == "queued"

    second = claim(worker_repository, "second")
    assert second and second.job_id == second_job_id
    final = worker_repository.finish_failure(
        second,
        error_code=ErrorCode.CONTENT_UNAVAILABLE,
        diagnostic="second child unavailable",
        now=NOW + timedelta(seconds=1),
    )
    assert final == JobStatus.FAILED

    aggregate = service.get_batch(first_job["batch_id"])
    assert aggregate["status"] == "partial_success"
    assert aggregate["queued_count"] == 0
    assert aggregate["inputs"][0]["status"] == "partial_success"
    assert aggregate["inputs"][0]["error_code"] == "content_unavailable"

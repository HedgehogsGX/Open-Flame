from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest

from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.credentials import CredentialRepository
from video_download_control.domain import ErrorCode
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


NOW = datetime(2026, 9, 3, 3, 0, tzinfo=UTC)


class CredentialRecordingAdapter(ScriptedFakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.probe_credential_refs: list[str | None] = []
        self.download_credential_refs: list[str | None] = []

    def probe(self, request, context):
        self.probe_credential_refs.append(request.credential_ref)
        return super().probe(request, context)

    def download(self, request, context, progress, cancellation):
        self.download_credential_refs.append(request.credential_ref)
        return super().download(request, context, progress, cancellation)


def insert_profile(
    database,
    *,
    profile_id: str,
    platform: str = "youtube",
    secret_ref: str = "youtube-primary",
    expires_at: str | None = "2026-09-04T03:00:00.000Z",
    disabled_at: str | None = None,
) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO credential_profiles(
                id, platform, name, secret_ref, expires_at,
                last_verified_at, created_at, disabled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                platform,
                f"profile-{profile_id}",
                secret_ref,
                expires_at,
                "2026-09-03T02:00:00.000Z",
                "2026-09-03T01:00:00.000Z",
                disabled_at,
            ),
        )


def assign_profile(database, *, profile_id: str, job_id: str) -> None:
    CredentialRepository(database, clock=lambda: NOW).assign(
        profile_id=profile_id,
        job_ids=[job_id],
    )


def force_assignment(database, *, profile_id: str, job_id: str) -> None:
    """Model missing/corrupt/TOCTOU state that the admin path rejects."""

    with database.connect() as connection:
        connection.execute(
            "UPDATE download_jobs SET credential_profile_id = ? WHERE id = ?",
            (profile_id, job_id),
        )


def make_worker(settings, database, adapter) -> Worker:
    return Worker(
        worker_id="credential-flow-worker",
        repository=WorkerRepository(database),
        adapter=adapter,
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
        clock=lambda: NOW,
    )


def test_valid_profile_passes_only_opaque_ref_to_probe_and_download(
    service,
    settings,
    database,
) -> None:
    profile_id = "profile-youtube-valid"
    opaque_ref = "youtube-primary"
    insert_profile(
        database,
        profile_id=profile_id,
        secret_ref=opaque_ref,
    )
    batch = service.create_batch(
        name="credential flow",
        raw_inputs=["https://www.youtube.com/watch?v=credential-valid"],
    )
    assign_profile(database, profile_id=profile_id, job_id=batch["jobs"][0]["id"])
    adapter = CredentialRecordingAdapter()

    result = make_worker(settings, database, adapter).run_once()

    assert result is not None and result.status == "ready"
    assert adapter.probe_credential_refs == [opaque_ref]
    assert adapter.download_credential_refs == [opaque_ref]
    repository = WorkerRepository(database)
    job = repository.get_job(batch["jobs"][0]["id"])
    assert job["credential_profile_id"] == profile_id
    assert opaque_ref not in job.values()
    attempt = repository.attempts_for(job["id"])[0]
    assert opaque_ref not in attempt.values()


def test_job_without_profile_remains_compatible_and_passes_none(
    service,
    settings,
    database,
) -> None:
    batch = service.create_batch(
        name="no credential",
        raw_inputs=["https://x.com/example/status/7654321"],
    )
    adapter = CredentialRecordingAdapter()

    result = make_worker(settings, database, adapter).run_once()

    assert result is not None and result.status == "ready"
    assert adapter.probe_credential_refs == [None]
    assert adapter.download_credential_refs == [None]
    assert WorkerRepository(database).get_job(batch["jobs"][0]["id"])[
        "credential_profile_id"
    ] is None


@pytest.mark.parametrize(
    ("case", "profile_kwargs"),
    [
        ("missing", None),
        ("wrong-platform", {"platform": "x"}),
        ("expired", {"expires_at": "2026-09-03T03:00:00.000Z"}),
        ("disabled", {"disabled_at": "2026-09-03T02:30:00.000Z"}),
        ("malformed-expiry", {"expires_at": "not-a-timestamp"}),
        ("path-shaped-ref", {"secret_ref": "/run/secrets/youtube.cookies"}),
        ("empty-ref", {"secret_ref": ""}),
    ],
)
def test_invalid_profile_fails_terminal_without_attempt_or_adapter_call(
    case: str,
    profile_kwargs: dict[str, str | None] | None,
    service,
    settings,
    database,
) -> None:
    profile_id = f"profile-{case}"
    if profile_kwargs is not None:
        insert_profile(
            database,
            profile_id=profile_id,
            **profile_kwargs,
        )
    batch = service.create_batch(
        name=case,
        raw_inputs=[f"https://www.youtube.com/watch?v=credential-{case}"],
    )
    job_id = batch["jobs"][0]["id"]
    force_assignment(database, profile_id=profile_id, job_id=job_id)
    adapter = CredentialRecordingAdapter()

    result = make_worker(settings, database, adapter).run_once()

    assert result is None
    assert adapter.probe_credential_refs == []
    assert adapter.download_credential_refs == []
    repository = WorkerRepository(database)
    job = repository.get_job(job_id)
    assert job["status"] == "failed"
    assert job["final_error_code"] == ErrorCode.AUTHENTICATION_REQUIRED.value
    assert job["attempt_count"] == 0
    assert repository.attempts_for(job_id) == []
    refreshed = service.get_batch(batch["id"])
    assert refreshed["status"] == "failed"
    assert refreshed["inputs"][0]["status"] == "failed"
    assert refreshed["inputs"][0]["error_code"] == (
        ErrorCode.AUTHENTICATION_REQUIRED.value
    )
    diagnostic = refreshed["inputs"][0]["error_message"]
    assert profile_id not in diagnostic
    assert "secrets" not in diagnostic


def test_valid_lease_hides_credential_ref_from_repr(
    service,
    database,
) -> None:
    profile_id = "profile-repr"
    opaque_ref = "opaque-repr-ref"
    insert_profile(database, profile_id=profile_id, secret_ref=opaque_ref)
    service.create_batch(
        name="repr",
        raw_inputs=["https://www.youtube.com/watch?v=credential-repr"],
    )
    with database.connect() as connection:
        job_id = connection.execute(
            "SELECT id FROM download_jobs WHERE status = 'queued'"
        ).fetchone()["id"]
    assign_profile(database, profile_id=profile_id, job_id=job_id)

    lease = WorkerRepository(database).claim_next(
        worker_id="repr-worker",
        adapter="fake",
        adapter_version="1",
        now=NOW,
    )

    assert lease is not None
    assert lease.credential_ref == opaque_ref
    assert opaque_ref not in repr(lease)


def test_concurrent_claim_fails_invalid_job_once_and_claims_next_valid_job_once(
    service,
    database,
) -> None:
    invalid = service.create_batch(
        name="invalid first",
        raw_inputs=["https://www.youtube.com/watch?v=credential-concurrent-bad"],
    )
    valid = service.create_batch(
        name="valid second",
        raw_inputs=["https://www.youtube.com/watch?v=credential-concurrent-good"],
    )
    invalid_job_id = invalid["jobs"][0]["id"]
    valid_job_id = valid["jobs"][0]["id"]
    force_assignment(
        database,
        profile_id="missing-concurrent-profile",
        job_id=invalid_job_id,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_jobs SET created_at = ? WHERE id = ?",
            ("2026-09-03T00:00:00.000Z", invalid_job_id),
        )
        connection.execute(
            "UPDATE download_jobs SET created_at = ? WHERE id = ?",
            ("2026-09-03T00:00:01.000Z", valid_job_id),
        )
    repository = WorkerRepository(database)

    def claim(worker_id: str):
        return repository.claim_next(
            worker_id=worker_id,
            adapter="fake",
            adapter_version="1",
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(claim, ("worker-a", "worker-b")))

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    assert claimed[0].job_id == valid_job_id
    assert repository.get_job(invalid_job_id)["status"] == "failed"
    assert repository.get_job(valid_job_id)["status"] == "probing"
    assert repository.attempts_for(invalid_job_id) == []
    assert len(repository.attempts_for(valid_job_id)) == 1


def test_future_expiry_is_timezone_normalized_at_claim(
    service,
    database,
) -> None:
    profile_id = "profile-timezone"
    insert_profile(
        database,
        profile_id=profile_id,
        expires_at=(NOW + timedelta(hours=1)).astimezone(
            timezone(timedelta(hours=9, minutes=30))
        ).isoformat(),
    )
    service.create_batch(
        name="timezone",
        raw_inputs=["https://www.youtube.com/watch?v=credential-timezone"],
    )
    with database.connect() as connection:
        job_id = connection.execute(
            "SELECT id FROM download_jobs WHERE status = 'queued'"
        ).fetchone()["id"]
    assign_profile(database, profile_id=profile_id, job_id=job_id)

    lease = WorkerRepository(database).claim_next(
        worker_id="timezone-worker",
        adapter="fake",
        adapter_version="1",
        now=NOW,
    )

    assert lease is not None
    assert lease.credential_ref == "youtube-primary"

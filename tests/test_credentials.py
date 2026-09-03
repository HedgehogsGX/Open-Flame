from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from video_download_control.credential_cli import main
from video_download_control.credentials import (
    CredentialProfileError,
    CredentialRepository,
)
from video_download_control.database import Database
from video_download_control.domain import JobStatus, Platform
from video_download_control.repository import BatchRepository
from video_download_control.service import BatchService


NOW = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)


def make_repository(tmp_path: Path) -> tuple[Database, CredentialRepository]:
    database = Database(tmp_path / "control.sqlite3")
    database.initialize()
    return database, CredentialRepository(database, clock=lambda: NOW)


def create_batch(database: Database) -> dict[str, object]:
    return BatchService(
        repository=BatchRepository(
            database, clock=lambda: NOW - timedelta(seconds=1)
        ),
        max_batch_urls=50,
        route_policy_version="credential-test-v1",
    ).create_batch(
        name="credential scope",
        raw_inputs=[
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://x.com/example/status/1234567890",
        ],
    )


def test_register_persists_only_opaque_reference_and_lists_metadata(
    tmp_path: Path,
) -> None:
    database, repository = make_repository(tmp_path)

    profile = repository.register(
        platform=Platform.YOUTUBE,
        name="Primary read-only",
        secret_ref="youtube-primary",
        expires_at="2026-10-01T12:30:00+09:30",
    )

    assert profile["platform"] == "youtube"
    assert profile["secret_ref"] == "youtube-primary"
    assert profile["expires_at"] == "2026-10-01T03:00:00.000Z"
    assert profile["disabled_at"] is None
    assert repository.list(platform=Platform.YOUTUBE) == [profile]
    with database.connect() as connection:
        stored = dict(
            connection.execute(
                "SELECT * FROM credential_profiles WHERE id = ?", (profile["id"],)
            ).fetchone()
        )
    assert stored["secret_ref"] == "youtube-primary"
    assert "cookie" not in {key.lower() for key in stored}
    assert not any("/" in str(value) or "\\" in str(value) for value in stored.values())


@pytest.mark.parametrize(
    "reference",
    ["", "../cookie.txt", "C:\\secret\\cookie.txt", "contains space", "x" * 65],
)
def test_register_rejects_non_opaque_references(
    tmp_path: Path, reference: str
) -> None:
    _, repository = make_repository(tmp_path)
    with pytest.raises(CredentialProfileError, match="opaque token"):
        repository.register(
            platform=Platform.X,
            name="invalid",
            secret_ref=reference,
        )


def test_register_rejects_past_expiry_and_duplicate_platform_name(
    tmp_path: Path,
) -> None:
    _, repository = make_repository(tmp_path)
    with pytest.raises(CredentialProfileError, match="future"):
        repository.register(
            platform=Platform.X,
            name="expired",
            secret_ref="x-expired",
            expires_at="2026-09-03T01:59:59Z",
        )
    repository.register(
        platform=Platform.X,
        name="primary",
        secret_ref="x-primary",
    )
    with pytest.raises(CredentialProfileError, match="already exists"):
        repository.register(
            platform=Platform.X,
            name="primary",
            secret_ref="x-other",
        )


def test_batch_assignment_only_touches_matching_queued_platform(
    tmp_path: Path,
) -> None:
    database, repository = make_repository(tmp_path)
    batch = create_batch(database)
    profile = repository.register(
        platform=Platform.YOUTUBE,
        name="primary",
        secret_ref="youtube-primary",
    )

    result = repository.assign(profile_id=profile["id"], batch_id=batch["id"])

    assert result["assigned_count"] == 1
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT s.platform, j.credential_profile_id
            FROM download_jobs AS j
            JOIN source_items AS s ON s.id = j.source_item_id
            WHERE j.batch_id = ? ORDER BY s.platform
            """,
            (batch["id"],),
        ).fetchall()
    assert [(row["platform"], row["credential_profile_id"]) for row in rows] == [
        ("x", None),
        ("youtube", profile["id"]),
    ]


def test_explicit_job_assignment_is_atomic_on_platform_mismatch(
    tmp_path: Path,
) -> None:
    database, repository = make_repository(tmp_path)
    batch = create_batch(database)
    profile = repository.register(
        platform=Platform.YOUTUBE,
        name="primary",
        secret_ref="youtube-primary",
    )
    job_ids = [job["id"] for job in batch["jobs"]]

    with pytest.raises(CredentialProfileError, match="does not match"):
        repository.assign(profile_id=profile["id"], job_ids=job_ids)

    with database.connect() as connection:
        assigned = connection.execute(
            "SELECT COUNT(*) AS count FROM download_jobs WHERE credential_profile_id IS NOT NULL"
        ).fetchone()["count"]
    assert assigned == 0


def test_assignment_can_be_cleared_only_before_claim(tmp_path: Path) -> None:
    database, repository = make_repository(tmp_path)
    batch = create_batch(database)
    youtube_job = next(
        job for job in batch["jobs"] if job["platform"] == Platform.YOUTUBE
    )
    profile = repository.register(
        platform=Platform.YOUTUBE,
        name="primary",
        secret_ref="youtube-primary",
    )
    repository.assign(profile_id=profile["id"], job_ids=[youtube_job["id"]])

    assert repository.clear_assignment(job_ids=[youtube_job["id"]]) == {
        "cleared_job_ids": [youtube_job["id"]],
        "cleared_count": 1,
    }
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_jobs SET status = ? WHERE id = ?",
            (JobStatus.PROBING, youtube_job["id"]),
        )
    with pytest.raises(CredentialProfileError, match="only be cleared"):
        repository.clear_assignment(job_ids=[youtube_job["id"]])


def test_explicit_assignment_does_not_partially_ignore_terminal_jobs(
    tmp_path: Path,
) -> None:
    database, repository = make_repository(tmp_path)
    first = create_batch(database)
    second = BatchService(
        repository=BatchRepository(
            database, clock=lambda: NOW - timedelta(seconds=1)
        ),
        max_batch_urls=50,
        route_policy_version="credential-test-v1",
    ).create_batch(
        name="second credential scope",
        raw_inputs=["https://www.youtube.com/watch?v=secondcred1"],
    )
    first_job = next(
        job for job in first["jobs"] if job["platform"] == Platform.YOUTUBE
    )
    second_job = second["jobs"][0]
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_jobs SET status = ? WHERE id = ?",
            (JobStatus.FAILED, first_job["id"]),
        )
    profile = repository.register(
        platform=Platform.YOUTUBE,
        name="primary",
        secret_ref="youtube-primary",
    )

    with pytest.raises(CredentialProfileError, match="explicitly selected queued"):
        repository.assign(
            profile_id=profile["id"],
            job_ids=[first_job["id"], second_job["id"]],
        )

    with database.connect() as connection:
        assigned = connection.execute(
            "SELECT credential_profile_id FROM download_jobs WHERE id = ?",
            (second_job["id"],),
        ).fetchone()["credential_profile_id"]
    assert assigned is None


def test_disable_is_idempotent_and_requests_active_job_cancellation(
    tmp_path: Path,
) -> None:
    database, repository = make_repository(tmp_path)
    batch = create_batch(database)
    youtube_job = next(
        job for job in batch["jobs"] if job["platform"] == Platform.YOUTUBE
    )
    profile = repository.register(
        platform=Platform.YOUTUBE,
        name="primary",
        secret_ref="youtube-primary",
    )
    repository.assign(profile_id=profile["id"], job_ids=[youtube_job["id"]])
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_jobs SET status = ? WHERE id = ?",
            (JobStatus.DOWNLOADING, youtube_job["id"]),
        )

    first = repository.disable(profile_id=profile["id"])
    second = repository.disable(profile_id=profile["id"])

    assert first["active_cancel_requests"] == 1
    assert second["active_cancel_requests"] == 0
    assert second["disabled_at"] == first["disabled_at"]
    with database.connect() as connection:
        job = connection.execute(
            "SELECT cancel_requested_at FROM download_jobs WHERE id = ?",
            (youtube_job["id"],),
        ).fetchone()
    assert job["cancel_requested_at"] == "2026-09-03T02:00:00.000Z"
    assert repository.list() == []
    assert repository.list(include_disabled=True)[0]["disabled_at"] == first["disabled_at"]


def test_cli_requires_existing_database_and_outputs_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = (tmp_path / "missing.sqlite3").resolve()
    with pytest.raises(SystemExit, match="does not exist"):
        main(["--database-path", str(missing), "list"])

    database = Database((tmp_path / "control.sqlite3").resolve())
    database.initialize()
    main(
        [
            "--database-path",
            str(database.path),
            "register",
            "--platform",
            "bilibili",
            "--name",
            "Primary",
            "--secret-ref",
            "bili-primary",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["platform"] == "bilibili"
    assert payload["secret_ref"] == "bili-primary"

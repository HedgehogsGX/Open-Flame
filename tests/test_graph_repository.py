from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from video_download_control.domain import ErrorCode, InputStatus, JobStatus
from video_download_control.graph import (
    GraphValidationError,
    XAttachmentProbeItem,
    XPostIdentity,
    build_x_attachment_discovery,
)
from video_download_control.service import BatchService
from video_download_control.worker_repository import (
    JobLease,
    LostLease,
    RediscoverConflict,
    WorkerRepository,
)

NOW = datetime(2026, 9, 3, 3, 0, tzinfo=UTC)


@pytest.fixture
def worker_repository(database) -> WorkerRepository:
    return WorkerRepository(database)


def graph_service(repository, *, route: str = "graph-test-v2") -> BatchService:
    return BatchService(
        repository=repository,
        max_batch_urls=50,
        route_policy_version=route,
        x_graph_v2_enabled=True,
    )


def create_parent(repository, source_id: str, *, route: str = "graph-test-v2"):
    batch = graph_service(repository, route=route).create_batch(
        name=None,
        raw_inputs=[f"https://x.com/example/status/{source_id}"],
    )
    return batch, batch["jobs"][0]


def claim_graph(
    repository: WorkerRepository,
    *,
    now: datetime = NOW,
) -> JobLease:
    lease = repository.claim_next(
        worker_id="graph-worker",
        adapter="graph-fake",
        adapter_version="1",
        now=now,
        supports_exact_selector=True,
    )
    assert lease is not None
    return lease


def attachment_items(*selectors: str) -> tuple[XAttachmentProbeItem, ...]:
    return tuple(
        XAttachmentProbeItem(
            stable_key=selector,
            selector_key=selector,
            expected_media_key=selector,
            media_kind="image" if selector.endswith("-image") else "video",
        )
        for selector in selectors
    )


def snapshot(source_id: str, items: tuple[XAttachmentProbeItem, ...]):
    return build_x_attachment_discovery(
        parent=XPostIdentity(source_id),
        parent_canonical_url=f"https://x.com/i/status/{source_id}",
        probe_items=items,
    )


def commit(
    repository: WorkerRepository,
    lease: JobLease,
    source_id: str,
    *selectors: str,
    now: datetime = NOW + timedelta(seconds=1),
) -> JobStatus:
    items = attachment_items(*selectors)
    return repository.commit_discovery(
        lease,
        probe_items=items,
        discovery_snapshot_hash=snapshot(source_id, items).snapshot_hash,
        sanitized_source={"title": "safe parent"},
        now=now,
    )


def table_counts(database) -> dict[str, int]:
    tables = (
        "source_discoveries",
        "source_relations",
        "source_items",
        "download_jobs",
        "download_job_targets",
        "input_relation_jobs",
        "job_attempts",
    )
    with database.connect() as connection:
        return {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
            )
            for table in tables
        }


@pytest.mark.parametrize("member_count", [1, 50])
def test_commit_discovery_accepts_one_and_fifty_atomically(
    member_count: int,
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = str(810000 + member_count)
    batch, parent = create_parent(repository, source_id)
    lease = claim_graph(worker_repository)
    selectors = tuple(f"media-{index}" for index in range(member_count))

    assert commit(worker_repository, lease, source_id, *selectors) == JobStatus.READY

    with database.connect() as connection:
        discovery = connection.execute(
            "SELECT member_count, discovered_by_attempt_id FROM source_discoveries"
        ).fetchone()
        children = connection.execute(
            """
            SELECT j.status, j.route_policy_version, target.selector_key
            FROM download_jobs AS j
            JOIN download_job_targets AS target ON target.job_id = j.id
            WHERE j.input_record_id = ? ORDER BY target.selector_key
            """,
            (parent["input_record_id"],),
        ).fetchall()
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE id = ?", (lease.attempt_id,)
        ).fetchone()
    assert discovery["member_count"] == member_count
    assert discovery["discovered_by_attempt_id"] == lease.attempt_id
    assert len(children) == member_count
    assert {row["status"] for row in children} == {"queued"}
    assert attempt["status"] == "succeeded"
    assert attempt["run_generation"] == 1
    assert attempt["generation_attempt_no"] == 1
    refreshed = repository.get_batch(batch["id"])
    assert refreshed["inputs"][0]["expected_item_count"] == member_count
    assert refreshed["inputs"][0]["status"] == "queued"


@pytest.mark.parametrize("member_count", [0, 51])
def test_commit_discovery_rejects_zero_and_fifty_one_without_partial_rows(
    member_count: int,
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = str(820000 + member_count)
    _, parent = create_parent(repository, source_id)
    lease = claim_graph(worker_repository)
    items = attachment_items(*(f"media-{index}" for index in range(member_count)))
    before = table_counts(database)

    with pytest.raises(GraphValidationError, match="count"):
        worker_repository.commit_discovery(
            lease,
            probe_items=items,
            discovery_snapshot_hash="0" * 64,
            sanitized_source={},
            now=NOW + timedelta(seconds=1),
        )

    assert table_counts(database) == before
    assert worker_repository.get_job(parent["id"])["status"] == "probing"
    assert worker_repository.attempts_for(parent["id"])[0]["status"] == "running"


def test_commit_replay_is_zero_write_and_target_secrets_stay_out_of_repr(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "830001"
    _, _ = create_parent(repository, source_id)
    parent_lease = claim_graph(worker_repository)
    assert commit(worker_repository, parent_lease, source_id, "do-not-log") == JobStatus.READY
    before = table_counts(database)

    assert commit(worker_repository, parent_lease, source_id, "do-not-log") == JobStatus.READY
    assert table_counts(database) == before

    child_lease = claim_graph(worker_repository, now=NOW + timedelta(seconds=2))
    assert child_lease.job_kind == "download"
    assert child_lease.source_type == "x_attachment"
    assert child_lease.canonical_url == "https://x.com/i/status/830001"
    assert child_lease.selector_key == "do-not-log"
    assert child_lease.expected_media_key == "do-not-log"
    assert "do-not-log" not in repr(child_lease)


def test_stale_discovery_is_fenced_and_input_cancel_wins_before_fanout(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "840001"
    batch, parent = create_parent(repository, source_id)
    lease = claim_graph(worker_repository)
    forged = replace(lease, lease_token="stale")

    with pytest.raises(LostLease):
        commit(worker_repository, forged, source_id, "one")
    assert table_counts(database)["source_discoveries"] == 0

    assert (
        worker_repository.request_cancel_input(
            parent["input_record_id"], now=NOW + timedelta(milliseconds=500)
        )
        == InputStatus.QUEUED
    )
    assert commit(worker_repository, lease, source_id, "one") == JobStatus.CANCELED
    assert table_counts(database)["source_discoveries"] == 0
    refreshed = repository.get_batch(batch["id"])
    assert refreshed["inputs"][0]["status"] == "canceled"
    assert refreshed["status"] == "canceled"


def test_mid_fanout_identity_conflict_rolls_back_the_entire_transaction(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "842001"
    _, parent = create_parent(repository, source_id)
    lease = claim_graph(worker_repository)
    items = attachment_items("conflict")
    member = snapshot(source_id, items).members[0]
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO source_items(
                id, platform, source_type, source_id, canonical_url,
                created_at, updated_at
            ) VALUES (
                'conflicting-owner', 'youtube', 'youtube_video', 'other',
                ?, '2026-09-03T03:00:00.000Z', '2026-09-03T03:00:00.000Z'
            )
            """,
            (member.canonical_url,),
        )
    before = table_counts(database)

    with pytest.raises(RuntimeError, match="already owned"):
        worker_repository.commit_discovery(
            lease,
            probe_items=items,
            discovery_snapshot_hash=snapshot(source_id, items).snapshot_hash,
            sanitized_source={},
            now=NOW + timedelta(seconds=1),
        )

    assert table_counts(database) == before
    assert worker_repository.get_job(parent["id"])["status"] == "probing"


@pytest.mark.parametrize(
    "trigger_sql",
    [
        """CREATE TRIGGER graph_fault BEFORE INSERT ON source_discoveries
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE INSERT ON source_items
           WHEN NEW.source_type = 'x_attachment'
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE INSERT ON source_relations
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE INSERT ON download_jobs
           WHEN NEW.job_kind = 'download'
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE INSERT ON download_job_targets
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE INSERT ON input_relation_jobs
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE UPDATE ON source_items
           WHEN OLD.source_type = 'x_post'
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE UPDATE ON input_records
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE UPDATE ON job_attempts
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE UPDATE ON download_jobs
           WHEN OLD.job_kind = 'discover'
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
        """CREATE TRIGGER graph_fault BEFORE UPDATE ON batches
           BEGIN SELECT RAISE(ABORT, 'graph fault'); END""",
    ],
)
def test_each_discovery_commit_write_stage_rolls_back_atomically(
    trigger_sql: str,
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "843001"
    _, parent = create_parent(repository, source_id)
    lease = claim_graph(worker_repository)
    before = table_counts(database)
    with database.connect() as connection:
        connection.execute(trigger_sql)

    try:
        with pytest.raises(sqlite3.IntegrityError, match="graph fault"):
            commit(worker_repository, lease, source_id, "one", "two")
    finally:
        with database.connect() as connection:
            connection.execute("DROP TRIGGER IF EXISTS graph_fault")

    assert table_counts(database) == before
    assert worker_repository.get_job(parent["id"])["status"] == "probing"
    assert worker_repository.attempts_for(parent["id"])[0]["status"] == "running"


def test_claim_fails_closed_without_exact_selector_support_and_creates_no_attempt(
    repository,
    worker_repository: WorkerRepository,
) -> None:
    batch, parent = create_parent(repository, "845001")

    assert worker_repository.claim_next(
        worker_id="flat-only",
        adapter="flat",
        adapter_version="1",
        now=NOW,
    ) is None

    assert worker_repository.attempts_for(parent["id"]) == []
    assert worker_repository.get_job(parent["id"])["final_error_code"] == (
        ErrorCode.ADAPTER_UNSUPPORTED.value
    )
    refreshed = repository.get_batch(batch["id"])
    assert refreshed["inputs"][0]["status"] == "failed"


def test_children_inherit_route_and_opaque_credential_without_repr_leak(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "850001"
    _, parent = create_parent(repository, source_id, route="route-frozen-7")
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO credential_profiles(
                id, platform, name, secret_ref, created_at
            ) VALUES ('profile-x', 'x', 'x profile', 'opaque.cookie.7', ?)
            """,
            ("2026-09-03T00:00:00.000Z",),
        )
        connection.execute(
            "UPDATE download_jobs SET credential_profile_id = 'profile-x' WHERE id = ?",
            (parent["id"],),
        )
    parent_lease = claim_graph(worker_repository)
    assert parent_lease.credential_ref == "opaque.cookie.7"
    assert "opaque.cookie.7" not in repr(parent_lease)

    commit(worker_repository, parent_lease, source_id, "credential-selector")
    with database.connect() as connection:
        child = connection.execute(
            """
            SELECT route_policy_version, credential_profile_id
            FROM download_jobs WHERE input_record_id = ? AND job_kind = 'download'
            """,
            (parent["input_record_id"],),
        ).fetchone()
    assert dict(child) == {
        "route_policy_version": "route-frozen-7",
        "credential_profile_id": "profile-x",
    }
    child_lease = claim_graph(worker_repository, now=NOW + timedelta(seconds=2))
    assert child_lease.credential_ref == "opaque.cookie.7"
    assert "credential-selector" not in repr(child_lease)
    assert "opaque.cookie.7" not in repr(child_lease)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("ready", "queued"), "queued"),
        (("ready", "ready"), "ready"),
        (("ready", "failed"), "partial_success"),
        (("ready", "canceled"), "partial_success"),
        (("failed", "canceled"), "failed"),
        (("canceled", "canceled"), "canceled"),
    ],
)
def test_graph_aggregation_uses_only_active_children_and_counts_partial_success(
    statuses: tuple[str, str],
    expected: str,
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "86" + str(abs(hash(statuses)) % 10000).zfill(4)
    batch, parent = create_parent(repository, source_id)
    lease = claim_graph(worker_repository)
    commit(worker_repository, lease, source_id, "first", "second")

    with database.connect() as connection:
        children = connection.execute(
            """
            SELECT id FROM download_jobs
            WHERE input_record_id = ? AND job_kind = 'download'
            ORDER BY created_at, id
            """,
            (parent["input_record_id"],),
        ).fetchall()
        for child, status in zip(children, statuses, strict=True):
            connection.execute(
                """
                UPDATE download_jobs
                SET status = ?, final_error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    ErrorCode.CONTENT_UNAVAILABLE.value
                    if status == "failed"
                    else None,
                    "2026-09-03T03:00:02.000Z",
                    child["id"],
                ),
            )
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=parent["id"],
            input_status="ready",
            error_code=ErrorCode.CONTENT_UNAVAILABLE.value,
            error_message="one attachment failed",
            now_text="2026-09-03T03:00:02.000Z",
        )

    refreshed = repository.get_batch(batch["id"])
    assert refreshed["inputs"][0]["status"] == expected
    assert refreshed["status"] == expected
    assert refreshed["partial_success_count"] == int(expected == "partial_success")
    assert refreshed["ready_count"] == int(expected == "ready")
    assert refreshed["failed_count"] == int(expected == "failed")
    assert refreshed["canceled_count"] == int(expected == "canceled")


def test_input_cancel_preserves_ready_child_and_fences_active_and_queued_children(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "870001"
    batch, parent = create_parent(repository, source_id)
    parent_lease = claim_graph(worker_repository)
    commit(worker_repository, parent_lease, source_id, "ready", "active", "queued")
    with database.connect() as connection:
        first = connection.execute(
            """
            SELECT j.id FROM download_jobs AS j
            JOIN download_job_targets AS target ON target.job_id = j.id
            WHERE j.input_record_id = ? AND target.selector_key = 'ready'
            """,
            (parent["input_record_id"],),
        ).fetchone()
        connection.execute(
            "UPDATE download_jobs SET status = 'ready', progress = 1 WHERE id = ?",
            (first["id"],),
        )
    active = claim_graph(worker_repository, now=NOW + timedelta(seconds=2))

    assert worker_repository.request_cancel_input(
        parent["input_record_id"], now=NOW + timedelta(seconds=3)
    ) == InputStatus.QUEUED
    assert worker_repository.heartbeat(
        active, now=NOW + timedelta(seconds=4)
    ) is True
    worker_repository.finish_canceled(active, now=NOW + timedelta(seconds=5))

    with database.connect() as connection:
        statuses = [
            row["status"]
            for row in connection.execute(
                """
                SELECT j.status FROM download_jobs AS j
                JOIN download_job_targets AS target ON target.job_id = j.id
                WHERE j.input_record_id = ? ORDER BY target.selector_key
                """,
                (parent["input_record_id"],),
            ).fetchall()
        ]
    assert sorted(statuses) == ["canceled", "canceled", "ready"]
    refreshed = repository.get_batch(batch["id"])
    assert refreshed["inputs"][0]["status"] == "partial_success"
    assert refreshed["partial_success_count"] == 1


def test_rediscover_reuses_retained_jobs_and_only_adds_changed_members(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "880001"
    _, parent = create_parent(repository, source_id)
    first_lease = claim_graph(worker_repository)
    commit(worker_repository, first_lease, source_id, "retained", "removed")
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE download_jobs
            SET status = CASE target.selector_key
                    WHEN 'retained' THEN 'ready' ELSE 'failed' END,
                progress = CASE target.selector_key
                    WHEN 'retained' THEN 1 ELSE 0 END,
                final_error_code = CASE target.selector_key
                    WHEN 'removed' THEN 'content_unavailable' ELSE NULL END
            FROM download_job_targets AS target
            WHERE download_jobs.id = target.job_id
              AND download_jobs.input_record_id = ?
            """,
            (parent["input_record_id"],),
        )
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=parent["id"],
            input_status="ready",
            error_code=None,
            error_message=None,
            now_text="2026-09-03T03:00:02.000Z",
        )
        original_jobs = {
            row["selector_key"]: row["id"]
            for row in connection.execute(
                """
                SELECT target.selector_key, j.id
                FROM download_jobs AS j
                JOIN download_job_targets AS target ON target.job_id = j.id
                WHERE j.input_record_id = ?
                """,
                (parent["input_record_id"],),
            ).fetchall()
        }

    assert worker_repository.request_rediscover(parent["input_record_id"], now=NOW + timedelta(seconds=3)) == 2
    second_lease = claim_graph(worker_repository, now=NOW + timedelta(seconds=4))
    assert second_lease.attempt_no == 2
    assert second_lease.run_generation == 2
    assert commit(
        worker_repository,
        second_lease,
        source_id,
        "retained",
        "removed",
        now=NOW + timedelta(seconds=5),
    ) == JobStatus.READY
    unchanged_counts = table_counts(database)
    assert unchanged_counts["download_jobs"] == 3
    assert unchanged_counts["source_discoveries"] == 1
    assert unchanged_counts["source_relations"] == 2
    assert unchanged_counts["input_relation_jobs"] == 4

    assert worker_repository.request_rediscover(parent["input_record_id"], now=NOW + timedelta(seconds=6)) == 3
    third_lease = claim_graph(worker_repository, now=NOW + timedelta(seconds=7))
    commit(
        worker_repository,
        third_lease,
        source_id,
        "retained",
        "added-image",
        now=NOW + timedelta(seconds=8),
    )
    with database.connect() as connection:
        generation_three = {
            row["selector_key"]: row["job_id"]
            for row in connection.execute(
                """
                SELECT target.selector_key, link.job_id
                FROM input_relation_jobs AS link
                JOIN download_job_targets AS target ON target.job_id = link.job_id
                WHERE link.input_record_id = ? AND link.run_generation = 3
                """,
                (parent["input_record_id"],),
            ).fetchall()
        }
        parent_state = connection.execute(
            """
            SELECT attempt_count, generation_attempt_count, run_generation
            FROM download_jobs WHERE id = ?
            """,
            (parent["id"],),
        ).fetchone()
        connection.execute(
            """
            UPDATE download_jobs SET status = 'ready', progress = 1
            WHERE id = ?
            """,
            (generation_three["added-image"],),
        )
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=parent["id"],
            input_status="ready",
            error_code=None,
            error_message=None,
            now_text="2026-09-03T03:00:09.000Z",
        )
        active_input = connection.execute(
            "SELECT status FROM input_records WHERE id = ?",
            (parent["input_record_id"],),
        ).fetchone()
    assert generation_three["retained"] == original_jobs["retained"]
    assert generation_three["added-image"] not in original_jobs.values()
    assert table_counts(database)["download_jobs"] == 4
    assert dict(parent_state) == {
        "attempt_count": 3,
        "generation_attempt_count": 1,
        "run_generation": 3,
    }
    assert active_input["status"] == "ready"


def test_rediscover_conflicts_when_even_a_queued_child_remains(
    repository,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "885001"
    _, parent = create_parent(repository, source_id)
    lease = claim_graph(worker_repository)
    commit(worker_repository, lease, source_id, "still-queued")

    control = worker_repository.get_input_control(parent["input_record_id"])
    assert control == {
        "id": parent["input_record_id"],
        "status": "queued",
        "active_run_generation": 1,
        "is_graph_v2": True,
    }
    assert worker_repository.get_input_control("missing") is None

    with pytest.raises(RediscoverConflict, match="non-terminal"):
        worker_repository.request_rediscover(
            parent["input_record_id"], now=NOW + timedelta(seconds=2)
        )


def test_rediscover_retained_identity_with_changed_target_gets_a_new_job(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "887001"
    _, parent = create_parent(repository, source_id)
    first_lease = claim_graph(worker_repository)
    first_items = (
        XAttachmentProbeItem(
            stable_key="stable-A",
            selector_key="selector-1",
            expected_media_key="expected-1",
            media_kind="video",
        ),
    )
    first_snapshot = snapshot(source_id, first_items)
    worker_repository.commit_discovery(
        first_lease,
        probe_items=first_items,
        discovery_snapshot_hash=first_snapshot.snapshot_hash,
        sanitized_source={},
        now=NOW + timedelta(seconds=1),
    )
    with database.connect() as connection:
        old_job = connection.execute(
            """
            SELECT j.id, j.source_item_id
            FROM download_jobs AS j
            JOIN download_job_targets AS target ON target.job_id = j.id
            WHERE j.input_record_id = ? AND target.selector_key = 'selector-1'
            """,
            (parent["input_record_id"],),
        ).fetchone()
        connection.execute(
            "UPDATE download_jobs SET status = 'ready', progress = 1 WHERE id = ?",
            (old_job["id"],),
        )
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=parent["id"],
            input_status="ready",
            error_code=None,
            error_message=None,
            now_text="2026-09-03T03:00:02.000Z",
        )

    worker_repository.request_rediscover(
        parent["input_record_id"], now=NOW + timedelta(seconds=3)
    )
    second_lease = claim_graph(worker_repository, now=NOW + timedelta(seconds=4))
    second_items = (
        XAttachmentProbeItem(
            stable_key="stable-A",
            selector_key="selector-2",
            expected_media_key="expected-2",
            media_kind="video",
        ),
    )
    second_snapshot = snapshot(source_id, second_items)
    worker_repository.commit_discovery(
        second_lease,
        probe_items=second_items,
        discovery_snapshot_hash=second_snapshot.snapshot_hash,
        sanitized_source={},
        now=NOW + timedelta(seconds=5),
    )
    before_replay = table_counts(database)
    assert worker_repository.commit_discovery(
        second_lease,
        probe_items=second_items,
        discovery_snapshot_hash=second_snapshot.snapshot_hash,
        sanitized_source={},
        now=NOW + timedelta(seconds=6),
    ) == JobStatus.READY
    assert table_counts(database) == before_replay

    with database.connect() as connection:
        jobs = connection.execute(
            """
            SELECT j.id, j.source_item_id, j.run_generation,
                   target.selector_key, target.expected_media_key
            FROM download_jobs AS j
            JOIN download_job_targets AS target ON target.job_id = j.id
            WHERE j.input_record_id = ? ORDER BY j.run_generation
            """,
            (parent["input_record_id"],),
        ).fetchall()
        active_link = connection.execute(
            """
            SELECT job_id FROM input_relation_jobs
            WHERE input_record_id = ? AND run_generation = 2
            """,
            (parent["input_record_id"],),
        ).fetchone()
    assert len(jobs) == 2
    assert jobs[0]["id"] == old_job["id"]
    assert jobs[0]["selector_key"] == "selector-1"
    assert jobs[0]["expected_media_key"] == "expected-1"
    assert jobs[1]["id"] != old_job["id"]
    assert jobs[1]["source_item_id"] == old_job["source_item_id"]
    assert jobs[1]["selector_key"] == "selector-2"
    assert jobs[1]["expected_media_key"] == "expected-2"
    assert active_link["job_id"] == jobs[1]["id"]


def test_ready_asset_reuse_creates_a_distinct_logical_job_for_the_new_input(
    repository,
    database,
    worker_repository: WorkerRepository,
) -> None:
    source_id = "890001"
    _, first_parent = create_parent(repository, source_id)
    first_lease = claim_graph(worker_repository)
    commit(worker_repository, first_lease, source_id, "shared")
    with database.connect() as connection:
        donor = connection.execute(
            """
            SELECT j.id, j.source_item_id
            FROM download_jobs AS j
            JOIN download_job_targets AS target ON target.job_id = j.id
            WHERE j.input_record_id = ? AND target.selector_key = 'shared'
            """,
            (first_parent["input_record_id"],),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO media_assets(
                id, source_item_id, media_key, media_kind,
                size_bytes, sha256, status, created_at
            ) VALUES ('ready-asset', ?, 'shared', 'video', 1, ?, 'ready', ?)
            """,
            (donor["source_item_id"], "a" * 64, "2026-09-03T03:00:02.000Z"),
        )
        connection.execute(
            """
            INSERT INTO job_assets(job_id, asset_id, role, ordinal)
            VALUES (?, 'ready-asset', 'original', 0)
            """,
            (donor["id"],),
        )
        connection.execute(
            "UPDATE download_jobs SET status = 'ready', progress = 1 WHERE id = ?",
            (donor["id"],),
        )

    second_batch, second_parent = create_parent(repository, source_id)
    second_lease = claim_graph(worker_repository, now=NOW + timedelta(seconds=3))
    commit(
        worker_repository,
        second_lease,
        source_id,
        "shared",
        now=NOW + timedelta(seconds=4),
    )
    with database.connect() as connection:
        reused = connection.execute(
            """
            SELECT j.id, j.status, j.reused_from_job_id, ja.asset_id
            FROM download_jobs AS j
            JOIN job_assets AS ja ON ja.job_id = j.id
            WHERE j.input_record_id = ? AND j.job_kind = 'download'
            """,
            (second_parent["input_record_id"],),
        ).fetchone()
    assert reused["id"] != donor["id"]
    assert reused["status"] == "ready"
    assert reused["reused_from_job_id"] == donor["id"]
    assert reused["asset_id"] == "ready-asset"
    refreshed = repository.get_batch(second_batch["id"])
    assert refreshed["inputs"][0]["status"] == "ready"


def test_generation_budget_resets_without_resetting_lifetime_attempt_numbers(
    repository,
    worker_repository: WorkerRepository,
) -> None:
    _, parent = create_parent(repository, "895001")
    current_time = NOW
    for attempt_no in range(1, 5):
        lease = claim_graph(worker_repository, now=current_time)
        assert lease.attempt_no == attempt_no
        assert lease.run_generation == 1
        status = worker_repository.finish_failure(
            lease,
            error_code=ErrorCode.NETWORK_ERROR,
            diagnostic="temporary graph probe failure",
            now=current_time,
            retry_at=current_time + timedelta(seconds=1),
        )
        assert status == (JobStatus.QUEUED if attempt_no < 4 else JobStatus.FAILED)
        current_time += timedelta(seconds=1)

    assert worker_repository.request_rediscover(
        parent["input_record_id"], now=current_time
    ) == 2
    fifth = claim_graph(worker_repository, now=current_time)
    assert fifth.attempt_no == 5
    assert fifth.run_generation == 2
    attempt = worker_repository.attempts_for(parent["id"])[-1]
    assert attempt["generation_attempt_no"] == 1
    assert attempt["run_generation"] == 2

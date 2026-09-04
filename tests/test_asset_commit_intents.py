from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from uuid import uuid4

import pytest

from video_download_control.assets import (
    AssetStore,
    NonEmptyTestVerifier,
    StagedAsset,
)
from video_download_control.domain import JobStatus
from video_download_control.worker_repository import LostLease, WorkerRepository


NOW = datetime(2026, 9, 3, 2, 0, tzinfo=UTC)


def verifying_attempt(service, settings, database, suffix: str):
    batch = service.create_batch(
        name="asset intent",
        raw_inputs=[f"https://www.youtube.com/watch?v={suffix}"],
    )
    repository = WorkerRepository(database)
    store = AssetStore(settings.data_root, min_free_bytes=0)
    lease = repository.claim_next(
        worker_id="intent-worker",
        adapter="fake",
        adapter_version="1",
        now=NOW,
        remove_pending_asset=store.remove_pending_asset,
    )
    assert lease is not None
    repository.transition(
        lease,
        status=JobStatus.DOWNLOADING,
        progress=0.5,
        now=NOW,
    )
    repository.transition(
        lease,
        status=JobStatus.VERIFYING,
        progress=0.9,
        now=NOW,
    )
    attempt = store.prepare_attempt(lease.job_id, lease.attempt_id)
    return batch, repository, store, lease, attempt


def stage_asset(
    store: AssetStore,
    lease,
    attempt,
    *,
    name: str = "clip.fake",
    media_key: str = "media-1",
    ordinal: int = 0,
) -> StagedAsset:
    produced = attempt.output / name
    produced.write_bytes(f"offline-{media_key}".encode())
    return store.stage_file(
        attempt=attempt,
        produced_path=produced,
        media_key=media_key,
        media_kind="video",
        role="original",
        ordinal=ordinal,
        sanitized_source={"platform": "youtube"},
        producer={"adapter": "fake", "adapter_version": "1"},
        verifier=NonEmptyTestVerifier(),
        job_id=lease.job_id,
        attempt_id=lease.attempt_id,
        source_item_id=lease.source_item_id,
    )


def intent_count(database) -> int:
    with database.connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM asset_commit_intents"
            ).fetchone()[0]
        )


def test_same_attempt_can_hold_and_finalize_multiple_intents(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-multiple"
    )
    first = stage_asset(store, lease, attempt, media_key="media-a", ordinal=0)
    second = stage_asset(
        store,
        lease,
        attempt,
        name="second.fake",
        media_key="media-b",
        ordinal=1,
    )

    repository.record_asset_commit_intent(lease, asset=first, now=NOW)
    repository.record_asset_commit_intent(lease, asset=second, now=NOW)

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT asset_id, attempt_id FROM asset_commit_intents
            ORDER BY asset_id
            """
        ).fetchall()
    assert len(rows) == 2
    assert {row["attempt_id"] for row in rows} == {lease.attempt_id}

    status = repository.finalize_asset_commit_intents(
        lease,
        assets=[first, second],
        publish_staged=store.publish_staged,
        remove_pending_asset=store.remove_pending_asset,
        now=NOW,
    )

    assert status == JobStatus.READY
    assert intent_count(database) == 0
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM job_assets").fetchone()[0] == 2


def test_future_exact_active_intent_is_skipped(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-active"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    cleaned: list[str] = []

    recovered = repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=30),
        remove_pending_asset=lambda asset_id, _: cleaned.append(asset_id),
    )

    assert recovered == 0
    assert cleaned == []
    assert intent_count(database) == 1
    assert staged.staged_directory.is_dir()
    assert repository.get_job(lease.job_id)["status"] == "verifying"


@pytest.mark.parametrize("supply_cleanup", [False, True])
def test_refill_leaves_live_canceled_intent_for_its_execution_owner(
    service, settings, database, supply_cleanup: bool
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-live-cancel"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    repository.request_cancel(lease.job_id, now=NOW)
    batch = service.create_batch(
        name="other platform refill",
        raw_inputs=["https://x.com/example/status/900008"],
    )

    refilled = repository.claim_next(
        worker_id="intent-worker",
        adapter="fake",
        adapter_version="1",
        now=NOW + timedelta(seconds=1),
        remove_pending_asset=store.remove_pending_asset if supply_cleanup else None,
        perform_recovery=False,
        excluded_job_ids=frozenset({lease.job_id}),
    )

    assert refilled is not None and refilled.job_id == batch["jobs"][0]["id"]
    assert repository.get_job(lease.job_id)["status"] == "verifying"
    assert repository.attempts_for(lease.job_id)[0]["status"] == "running"
    assert staged.staged_directory.is_dir()
    assert intent_count(database) == 1


def test_expired_published_orphan_is_recycled_then_removed(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-expired"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    committed = store.publish_staged(staged)
    final = store.assets_root / committed.asset_id
    assert final.is_dir()

    recovered = repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=60),
        remove_pending_asset=store.remove_pending_asset,
    )

    assert recovered == 1
    assert intent_count(database) == 0
    assert not final.exists()
    assert repository.get_job(lease.job_id)["status"] == "queued"
    assert repository.attempts_for(lease.job_id)[0]["status"] == "abandoned"


def test_token_mismatch_intent_does_not_recycle_new_attempt(
    service, settings, database
) -> None:
    _, repository, store, old_lease, attempt = verifying_attempt(
        service, settings, database, "intent-token-mismatch"
    )
    staged = stage_asset(store, old_lease, attempt)
    repository.record_asset_commit_intent(old_lease, asset=staged, now=NOW)
    new_attempt_id = str(uuid4())
    new_token = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE download_jobs
            SET lease_token = ?, lease_owner = 'new-worker', attempt_count = 2,
                lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_token,
                "2026-09-03T02:05:00.000Z",
                "2026-09-03T02:01:00.000Z",
                "2026-09-03T02:01:00.000Z",
                old_lease.job_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO job_attempts(
                id, job_id, attempt_no, adapter, adapter_version,
                status, started_at, lease_token
            ) VALUES (?, ?, 2, 'fake', '1', 'running', ?, ?)
            """,
            (
                new_attempt_id,
                old_lease.job_id,
                "2026-09-03T02:01:00.000Z",
                new_token,
            ),
        )

    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=61),
        remove_pending_asset=store.remove_pending_asset,
    ) == 1

    job = repository.get_job(old_lease.job_id)
    attempts = repository.attempts_for(old_lease.job_id)
    assert job["status"] == "verifying"
    assert job["lease_token"] == new_token
    assert [row["status"] for row in attempts] == ["abandoned", "running"]
    assert intent_count(database) == 0


def test_registered_database_asset_only_clears_stale_intent(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-db-wins"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    committed = store.publish_staged(staged)
    final = store.assets_root / committed.asset_id
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO media_assets(
                id, source_item_id, media_key, media_kind,
                size_bytes, sha256, status, created_at
            ) VALUES (?, ?, ?, 'video', ?, ?, 'ready', ?)
            """,
            (
                committed.asset_id,
                lease.source_item_id,
                committed.media_key,
                committed.size_bytes,
                committed.sha256,
                "2026-09-03T02:00:00.000Z",
            ),
        )

    recovered = repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=60),
        remove_pending_asset=lambda *_: pytest.fail(
            "registered asset must not be removed"
        ),
    )

    assert recovered == 1
    assert intent_count(database) == 0
    assert final.is_dir()


def test_cleanup_permission_error_releases_claim_and_retains_intent(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-permission"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)

    def denied(_asset_id: str, _attempt_id: str) -> None:
        raise PermissionError("simulated cleanup denial")

    with pytest.raises(PermissionError, match="cleanup denial"):
        repository.recover_asset_commit_intents(
            now=NOW + timedelta(seconds=60),
            remove_pending_asset=denied,
        )

    assert intent_count(database) == 1
    assert staged.staged_directory.is_dir()
    assert repository.get_job(lease.job_id)["status"] == "queued"
    assert repository.attempts_for(lease.job_id)[0]["status"] == "abandoned"
    with database.connect() as connection:
        recovery = connection.execute(
            """
            SELECT recovery_token, recovery_expires_at
            FROM asset_commit_intents WHERE asset_id = ?
            """,
            (staged.asset_id,),
        ).fetchone()
    assert recovery is not None
    assert recovery["recovery_token"] is None
    assert recovery["recovery_expires_at"] is None

    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=60),
        remove_pending_asset=store.remove_pending_asset,
    ) == 1
    assert intent_count(database) == 0


def test_cleanup_failure_releases_all_unattempted_batch_claims(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-batch-cleanup-failure"
    )
    first = stage_asset(store, lease, attempt, media_key="media-a", ordinal=0)
    second = stage_asset(
        store,
        lease,
        attempt,
        name="second.fake",
        media_key="media-b",
        ordinal=1,
    )
    repository.record_asset_commit_intent(lease, asset=first, now=NOW)
    repository.record_asset_commit_intent(lease, asset=second, now=NOW)

    def denied(_asset_id: str, _attempt_id: str) -> None:
        raise PermissionError("first cleanup failed")

    with pytest.raises(PermissionError, match="first cleanup failed"):
        repository.recover_asset_commit_intents(
            now=NOW + timedelta(seconds=60),
            remove_pending_asset=denied,
        )

    with database.connect() as connection:
        claims = connection.execute(
            """
            SELECT recovery_token, recovery_expires_at
            FROM asset_commit_intents ORDER BY asset_id
            """
        ).fetchall()
    assert len(claims) == 2
    assert all(row["recovery_token"] is None for row in claims)
    assert all(row["recovery_expires_at"] is None for row in claims)

    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=60),
        remove_pending_asset=store.remove_pending_asset,
    ) == 2
    assert intent_count(database) == 0


class FailingAssetInsertRepository(WorkerRepository):
    @staticmethod
    def _insert_asset_records_locked(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("simulated asset SQL failure")


def test_sql_failure_after_publish_keeps_intent_for_recovery(
    service, settings, database
) -> None:
    _, _, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-sql-failure"
    )
    repository = FailingAssetInsertRepository(database)
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)

    with pytest.raises(sqlite3.OperationalError, match="asset SQL failure"):
        repository.finalize_asset_commit_intent(
            lease,
            asset=staged,
            publish_staged=store.publish_staged,
            remove_pending_asset=store.remove_pending_asset,
            now=NOW,
        )

    final = store.assets_root / staged.asset_id
    assert final.is_dir()
    assert intent_count(database) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    assert repository.get_job(lease.job_id)["status"] == "verifying"
    assert repository.attempts_for(lease.job_id)[0]["status"] == "running"

    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=60),
        remove_pending_asset=store.remove_pending_asset,
    ) == 1
    assert not final.exists()
    assert intent_count(database) == 0


def test_concurrent_recovery_cleans_one_intent_once(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-concurrent"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    committed = store.publish_staged(staged)
    cleanup_calls = 0
    calls_lock = Lock()

    def cleanup(asset_id: str, attempt_id: str) -> None:
        nonlocal cleanup_calls
        with calls_lock:
            cleanup_calls += 1
        store.remove_pending_asset(asset_id, attempt_id)

    def recover(_: int) -> int:
        return WorkerRepository(database).recover_asset_commit_intents(
            now=NOW + timedelta(seconds=60),
            remove_pending_asset=cleanup,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(recover, range(2)))

    assert sorted(results) == [0, 1]
    assert cleanup_calls == 1
    assert intent_count(database) == 0
    assert not (store.assets_root / committed.asset_id).exists()


def test_slow_cleanup_does_not_hold_sqlite_writer_lock(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-slow-cleanup"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)

    second_batch = service.create_batch(
        name="unrelated heartbeat",
        raw_inputs=["https://x.com/example/status/1234567890123456789"],
    )
    second_lease = repository.claim_next(
        worker_id="heartbeat-worker",
        adapter="fake",
        adapter_version="1",
        now=NOW,
        lease_seconds=300,
        remove_pending_asset=store.remove_pending_asset,
    )
    assert second_lease is not None
    assert second_lease.job_id == second_batch["jobs"][0]["id"]

    cleanup_entered = Event()
    release_cleanup = Event()

    def slow_cleanup(asset_id: str, attempt_id: str) -> None:
        cleanup_entered.set()
        if not release_cleanup.wait(10):
            raise TimeoutError("test did not release cleanup")
        store.remove_pending_asset(asset_id, attempt_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery = executor.submit(
            repository.recover_asset_commit_intents,
            now=NOW + timedelta(seconds=60),
            remove_pending_asset=slow_cleanup,
        )
        assert cleanup_entered.wait(2)
        heartbeat = executor.submit(
            repository.heartbeat,
            second_lease,
            now=NOW + timedelta(seconds=60),
            lease_seconds=300,
        )
        try:
            assert heartbeat.result(timeout=1) is False
        finally:
            release_cleanup.set()
        assert recovery.result(timeout=5) == 1

    assert intent_count(database) == 0


class SimulatedRecoveryCrash(BaseException):
    pass


def test_expired_recovery_claim_retries_idempotent_cleanup_after_crash(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-recovery-crash"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)

    def crash_after_cleanup(asset_id: str, attempt_id: str) -> None:
        store.remove_pending_asset(asset_id, attempt_id)
        raise SimulatedRecoveryCrash

    with pytest.raises(SimulatedRecoveryCrash):
        repository.recover_asset_commit_intents(
            now=NOW + timedelta(seconds=60),
            remove_pending_asset=crash_after_cleanup,
            recovery_lease_seconds=10,
        )

    assert not staged.staged_directory.exists()
    assert intent_count(database) == 1
    with database.connect() as connection:
        recovery = connection.execute(
            """
            SELECT recovery_token, recovery_expires_at
            FROM asset_commit_intents WHERE asset_id = ?
            """,
            (staged.asset_id,),
        ).fetchone()
    assert recovery is not None
    assert recovery["recovery_token"] is not None
    assert recovery["recovery_expires_at"] == "2026-09-03T02:01:10.000Z"
    assert repository.get_job(lease.job_id)["status"] == "queued"
    assert repository.attempts_for(lease.job_id)[0]["status"] == "abandoned"

    early_calls: list[str] = []
    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=65),
        remove_pending_asset=lambda asset_id, _: early_calls.append(asset_id),
        recovery_lease_seconds=10,
    ) == 0
    assert early_calls == []

    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(seconds=71),
        remove_pending_asset=store.remove_pending_asset,
        recovery_lease_seconds=10,
    ) == 1
    assert intent_count(database) == 0


def test_recovery_completion_is_fenced_by_claim_token(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-recovery-fencing"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    cleanup_entered = Event()
    release_cleanup = Event()

    def controlled_cleanup(asset_id: str, attempt_id: str) -> None:
        cleanup_entered.set()
        if not release_cleanup.wait(10):
            raise TimeoutError("test did not release cleanup")
        store.remove_pending_asset(asset_id, attempt_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        recovery = executor.submit(
            repository.recover_asset_commit_intents,
            now=NOW + timedelta(seconds=60),
            remove_pending_asset=controlled_cleanup,
        )
        assert cleanup_entered.wait(2)
        with database.connect() as connection:
            original = connection.execute(
                """
                SELECT recovery_token FROM asset_commit_intents
                WHERE asset_id = ?
                """,
                (staged.asset_id,),
            ).fetchone()
            assert original is not None
            assert original["recovery_token"] is not None
            connection.execute(
                """
                UPDATE asset_commit_intents
                SET recovery_token = 'replacement-owner',
                    recovery_expires_at = ?
                WHERE asset_id = ? AND recovery_token = ?
                """,
                (
                    "2026-09-03T02:10:00.000Z",
                    staged.asset_id,
                    original["recovery_token"],
                ),
            )
        release_cleanup.set()
        with pytest.raises(RuntimeError, match="recovery lease was lost"):
            recovery.result(timeout=5)

    with database.connect() as connection:
        remaining = connection.execute(
            """
            SELECT recovery_token FROM asset_commit_intents
            WHERE asset_id = ?
            """,
            (staged.asset_id,),
        ).fetchone()
    assert remaining is not None
    assert remaining["recovery_token"] == "replacement-owner"

    assert repository.recover_asset_commit_intents(
        now=NOW + timedelta(minutes=11),
        remove_pending_asset=store.remove_pending_asset,
    ) == 1
    assert intent_count(database) == 0


def test_expired_worker_cannot_invoke_publish_callback(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-expired-publisher"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    publish_calls = 0

    def publish(_asset: StagedAsset):
        nonlocal publish_calls
        publish_calls += 1
        return store.publish_staged(_asset)

    with pytest.raises(LostLease):
        repository.finalize_asset_commit_intent(
            lease,
            asset=staged,
            publish_staged=publish,
            remove_pending_asset=store.remove_pending_asset,
            now=NOW + timedelta(seconds=60),
        )

    assert publish_calls == 0
    assert intent_count(database) == 1
    assert staged.staged_directory.is_dir()


def test_cancel_with_intent_cleans_without_publishing(
    service, settings, database
) -> None:
    _, repository, store, lease, attempt = verifying_attempt(
        service, settings, database, "intent-cancel"
    )
    staged = stage_asset(store, lease, attempt)
    repository.record_asset_commit_intent(lease, asset=staged, now=NOW)
    repository.request_cancel(lease.job_id, now=NOW + timedelta(seconds=1))

    def must_not_publish(_asset: StagedAsset):
        pytest.fail("canceled asset must not be published")

    status = repository.finalize_asset_commit_intent(
        lease,
        asset=staged,
        publish_staged=must_not_publish,
        remove_pending_asset=store.remove_pending_asset,
        now=NOW + timedelta(seconds=2),
    )

    assert status == JobStatus.CANCELED
    assert intent_count(database) == 0
    assert not staged.staged_directory.exists()
    assert repository.get_job(lease.job_id)["status"] == "canceled"

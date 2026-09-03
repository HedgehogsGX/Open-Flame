from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.domain import ErrorCode
from video_download_control.worker_repository import WorkerRepository


NOW = datetime(2026, 9, 3, 3, 0, tzinfo=UTC)


def make_job(service, url: str) -> str:
    return service.create_batch(name="reconcile", raw_inputs=[url])["jobs"][0]["id"]


def claim(repository: WorkerRepository, worker: str, now: datetime):
    return repository.claim_next(
        worker_id=worker,
        adapter="fake",
        adapter_version="1",
        now=now,
        lease_seconds=60,
    )


def create_leftovers(store: AssetStore, lease):
    attempt = store.prepare_attempt(lease.job_id, lease.attempt_id)
    produced = attempt.output / "orphan.fake"
    produced.write_bytes(b"orphan payload")
    staged = store.stage_file(
        attempt=attempt,
        produced_path=produced,
        media_key="orphan",
        media_kind="video",
        role="original",
        ordinal=0,
        sanitized_source={},
        producer={"adapter": "fake", "adapter_version": "1"},
        verifier=NonEmptyTestVerifier(),
        job_id=lease.job_id,
        attempt_id=lease.attempt_id,
        source_item_id=lease.source_item_id,
    )
    return attempt, staged


def test_terminal_attempt_scavenger_removes_unjournaled_temp_and_stage(
    service, database, settings
) -> None:
    repository = WorkerRepository(database)
    store = AssetStore(settings.data_root, min_free_bytes=0)
    job_id = make_job(service, "https://www.youtube.com/watch?v=orphan-terminal")
    lease = claim(repository, "crashing-worker", NOW)
    assert lease and lease.job_id == job_id
    attempt, staged = create_leftovers(store, lease)
    repository.finish_failure(
        lease,
        error_code=ErrorCode.CONTENT_UNAVAILABLE,
        diagnostic="terminal",
        now=NOW,
    )

    assert attempt.root.exists()
    assert staged.staged_directory.exists()
    assert store.list_managed_attempt_ids() == (lease.attempt_id,)
    assert repository.reconcile_attempt_directories(
        list_attempt_ids=store.list_managed_attempt_ids,
        cleanup_attempt=store.cleanup_attempt_identity,
    ) == 1

    assert not attempt.root.exists()
    assert not staged.staged_directory.exists()
    assert store.list_managed_attempt_ids() == ()


def test_running_attempt_is_never_scavenged(service, database, settings) -> None:
    repository = WorkerRepository(database)
    store = AssetStore(settings.data_root, min_free_bytes=0)
    make_job(service, "https://www.youtube.com/watch?v=active-attempt")
    lease = claim(repository, "active-worker", NOW)
    assert lease
    attempt, staged = create_leftovers(store, lease)

    assert repository.reconcile_attempt_directories(
        list_attempt_ids=store.list_managed_attempt_ids,
        cleanup_attempt=store.cleanup_attempt_identity,
    ) == 0
    assert attempt.root.exists()
    assert staged.staged_directory.exists()


def test_expired_attempt_is_scavenged_after_lease_recovery(
    service, database, settings
) -> None:
    repository = WorkerRepository(database)
    store = AssetStore(settings.data_root, min_free_bytes=0)
    job_id = make_job(service, "https://www.youtube.com/watch?v=expired-orphan")
    old = claim(repository, "lost-worker", NOW)
    assert old
    old_attempt, old_stage = create_leftovers(store, old)

    replacement = claim(repository, "replacement", NOW + timedelta(seconds=60))
    assert replacement and replacement.job_id == job_id
    assert replacement.attempt_id != old.attempt_id
    assert repository.reconcile_attempt_directories(
        list_attempt_ids=store.list_managed_attempt_ids,
        cleanup_attempt=store.cleanup_attempt_identity,
    ) == 1
    assert not old_attempt.root.exists()
    assert not old_stage.staged_directory.exists()
    assert repository.attempts_for(job_id)[0]["status"] == "abandoned"


def test_slow_scavenger_cleanup_does_not_hold_sqlite_writer_lock(
    service, database
) -> None:
    repository = WorkerRepository(database)
    first_job = make_job(service, "https://www.youtube.com/watch?v=cleanup-finished")
    finished = claim(repository, "finished", NOW)
    assert finished and finished.job_id == first_job
    repository.finish_failure(
        finished,
        error_code=ErrorCode.CONTENT_UNAVAILABLE,
        diagnostic="finished",
        now=NOW,
    )
    second_job = make_job(service, "https://x.com/example/status/1234567001")
    active = claim(repository, "active", NOW + timedelta(seconds=1))
    assert active and active.job_id == second_job

    entered = Event()
    release = Event()

    def slow_cleanup(job_id: str, attempt_id: str) -> None:
        assert (job_id, attempt_id) == (finished.job_id, finished.attempt_id)
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            repository.reconcile_attempt_directories,
            list_attempt_ids=lambda limit: (finished.attempt_id,),
            cleanup_attempt=slow_cleanup,
        )
        assert entered.wait(2)
        assert repository.heartbeat(
            active,
            now=NOW + timedelta(seconds=2),
            lease_seconds=60,
        ) is False
        release.set()
        assert future.result(timeout=5) == 1

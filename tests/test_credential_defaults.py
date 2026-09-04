from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

from video_download_control.cookie_source_config import CookieSourceConfigError, load_cookie_source_config
from video_download_control.credentials import CredentialRepository
from video_download_control.domain import ErrorCode, InputStatus, Platform
from video_download_control.normalization import normalize_url
from video_download_control.worker_repository import WorkerRepository


NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


def _configuration(tmp_path: Path, *, defaults: list[str], version: int = 2):
    source = (tmp_path / "synthetic.cookies.txt").resolve()
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    source.chmod(0o444)
    path = (tmp_path / "cookie-sources.json").resolve()
    document = {
        "schema_version": version,
        "cookie_sources": [
            {"platform": "douyin", "credential_ref": "synthetic-ref", "path": str(source)}
        ],
    }
    if version == 2:
        document["default_cookie_platforms"] = defaults
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o444)
    return load_cookie_source_config(path)


def test_explicit_v2_default_registers_once_and_is_scoped_to_each_run(
    tmp_path: Path, database
) -> None:
    from video_download_control.credential_defaults import prepare_credential_defaults

    config = _configuration(tmp_path, defaults=["douyin"])
    first = prepare_credential_defaults(database, config, run_id=uuid4().hex, now=NOW)
    second = prepare_credential_defaults(database, config, run_id=uuid4().hex, now=NOW)

    assert config.default_cookie_platforms == (Platform.DOUYIN,)
    assert first.platforms == second.platforms == (Platform.DOUYIN,)
    assert first.run_id != second.run_id
    profiles = CredentialRepository(database, clock=lambda: NOW).list()
    assert len(profiles) == 1
    assert profiles[0]["platform"] == "douyin"
    assert profiles[0]["secret_ref"] == "synthetic-ref"
    assert "synthetic-ref" not in repr(first)
    assert str(tmp_path) not in repr(first)


def _inputs(*urls: str):
    return [
        {
            "ordinal": ordinal,
            "raw_text": url,
            "normalized": normalize_url(url),
            "status": InputStatus.QUEUED,
        }
        for ordinal, url in enumerate(urls, start=1)
    ]


def test_batch_binds_only_opted_in_platforms_within_creation_transaction(
    tmp_path: Path, database, repository
) -> None:
    from video_download_control.credential_defaults import prepare_credential_defaults

    defaults = prepare_credential_defaults(
        database, _configuration(tmp_path, defaults=["douyin"]),
        run_id=uuid4().hex, now=NOW,
    )
    batch = repository.create_batch(
        name=None,
        inputs=_inputs(
            "https://www.douyin.com/video/7123456789012345678",
            "https://www.youtube.com/watch?v=abcdefghijk",
        ),
        route_policy_version="test-defaults",
        credential_mode="use_default",
        credential_defaults=defaults,
    )
    jobs = {job["platform"]: job for job in batch["jobs"]}
    profile = CredentialRepository(database).list()[0]
    assert jobs["douyin"]["credential_profile_id"] == profile["id"]
    assert jobs["youtube"]["credential_profile_id"] is None


@pytest.mark.parametrize("mode", [None, "anonymous", "use_default"])
def test_failed_retry_atomically_preserves_clears_or_selects_default_credentials(
    tmp_path: Path, database, repository, mode
) -> None:
    from video_download_control.credential_defaults import prepare_credential_defaults

    defaults = prepare_credential_defaults(
        database, _configuration(tmp_path, defaults=["douyin"]),
        run_id=uuid4().hex, now=NOW,
    )
    profile = CredentialRepository(database).list()[0]
    batch = repository.create_batch(
        name=None,
        inputs=_inputs("https://www.douyin.com/video/7123456789012345678"),
        route_policy_version="test-defaults",
        credential_mode="anonymous" if mode == "use_default" else "use_default",
        credential_defaults=defaults,
    )
    worker_repository = WorkerRepository(database)
    lease = worker_repository.claim_next(
        worker_id="test", adapter="fake", adapter_version="test", now=NOW,
    )
    assert lease is not None
    worker_repository.finish_failure(
        lease, error_code=ErrorCode.AUTHENTICATION_REQUIRED,
        diagnostic="synthetic authentication failure", now=NOW,
    )
    retried = worker_repository.request_retry(
        lease.job_id, now=NOW, credential_mode=mode, credential_defaults=defaults,
    )

    assert retried["run_generation"] == 2
    job = worker_repository.get_job(lease.job_id)
    assert job["credential_profile_id"] == (None if mode == "anonymous" else profile["id"])
    assert job["status"] == "queued"
    assert job["attempt_count"] == 1
    assert len(worker_repository.attempts_for(lease.job_id)) == 1


@pytest.mark.parametrize("defaults", [None, {}, "douyin", [True], ["unknown"], ["youtube"], ["douyin", "douyin"]])
def test_v2_rejects_invalid_or_unmapped_default_platforms(tmp_path: Path, defaults) -> None:
    with pytest.raises(CookieSourceConfigError):
        _configuration(tmp_path, defaults=defaults)


def test_v1_and_explicit_anonymous_never_register_or_apply_a_default(
    tmp_path: Path, database, repository
) -> None:
    from video_download_control.credential_defaults import prepare_credential_defaults

    v1 = _configuration(tmp_path, defaults=[], version=1)
    no_defaults = prepare_credential_defaults(database, v1, run_id=uuid4().hex, now=NOW)
    assert no_defaults.platforms == ()
    assert CredentialRepository(database).list() == []
    # A manually registered matching profile is not an implicit global default.
    CredentialRepository(database, clock=lambda: NOW).register(
        platform=Platform.DOUYIN, name="manual", secret_ref="synthetic-ref",
    )
    batch = repository.create_batch(
        name=None,
        inputs=_inputs("https://www.douyin.com/video/7123456789012345678"),
        route_policy_version="test-defaults",
        credential_mode="use_default",
        credential_defaults=no_defaults,
    )
    assert batch["jobs"][0]["credential_profile_id"] is None


@pytest.mark.parametrize("state", ["disabled", "expired", "ambiguous"])
def test_prepare_never_revives_or_silently_selects_ineligible_existing_profiles(
    tmp_path: Path, database, state: str
) -> None:
    from video_download_control.credential_defaults import CredentialDefaultsError, prepare_credential_defaults

    credentials = CredentialRepository(database, clock=lambda: NOW - timedelta(days=2))
    profile = credentials.register(
        platform=Platform.DOUYIN, name="old", secret_ref="synthetic-ref",
        expires_at=(NOW - timedelta(days=1)).isoformat() if state == "expired" else None,
    )
    if state == "disabled":
        credentials.disable(profile_id=profile["id"])
    elif state == "ambiguous":
        credentials.register(platform=Platform.DOUYIN, name="other", secret_ref="synthetic-ref")
    before = credentials.list(include_disabled=True)

    with pytest.raises(CredentialDefaultsError) as captured:
        prepare_credential_defaults(
            database, _configuration(tmp_path, defaults=["douyin"]),
            run_id=uuid4().hex, now=NOW,
        )

    assert str(captured.value) == "configured credential defaults are unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert credentials.list(include_disabled=True) == before


@pytest.mark.parametrize("mutation", ["disabled", "expired", "changed_ref"])
def test_changed_default_profile_rolls_back_entire_batch_but_anonymous_still_works(
    tmp_path: Path, database, repository, mutation: str
) -> None:
    from video_download_control.credential_defaults import CredentialDefaultsError, prepare_credential_defaults

    defaults = prepare_credential_defaults(
        database, _configuration(tmp_path, defaults=["douyin"]),
        run_id=uuid4().hex, now=NOW,
    )
    credentials = CredentialRepository(database, clock=lambda: NOW)
    profile_id = credentials.list()[0]["id"]
    if mutation == "disabled":
        credentials.disable(profile_id=profile_id)
    else:
        with database.connect() as connection:
            if mutation == "expired":
                connection.execute(
                    "UPDATE credential_profiles SET expires_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00.000Z", profile_id),
                )
            else:
                connection.execute(
                    "UPDATE credential_profiles SET secret_ref = ? WHERE id = ?",
                    ("another-synthetic-ref", profile_id),
                )
    inputs = _inputs(
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://www.douyin.com/video/7123456789012345678",
    )
    with pytest.raises(CredentialDefaultsError):
        repository.create_batch(
            name=None, inputs=inputs, route_policy_version="test-defaults",
            credential_mode="use_default", credential_defaults=defaults,
        )
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM batches").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM download_jobs").fetchone()[0] == 0
    batch = repository.create_batch(
        name=None, inputs=inputs, route_policy_version="test-defaults",
        credential_mode="anonymous", credential_defaults=defaults,
    )
    assert all(job["credential_profile_id"] is None for job in batch["jobs"])


@pytest.mark.parametrize("operation", ["create", "retry"])
def test_changed_configuration_at_precommit_rolls_back_entire_write(
    tmp_path: Path, database, repository, monkeypatch, operation: str
) -> None:
    from video_download_control.credential_defaults import CredentialDefaultsError, prepare_credential_defaults
    import video_download_control.repository as batch_module
    import video_download_control.worker_repository as worker_module

    config = _configuration(tmp_path, defaults=["douyin"])
    defaults = prepare_credential_defaults(database, config, run_id=uuid4().hex, now=NOW)
    arguments = dict(
        name=None,
        inputs=_inputs("https://www.douyin.com/video/7123456789012345678"),
        route_policy_version="test-defaults",
    )
    worker_repository = WorkerRepository(database)
    if operation == "retry":
        batch = repository.create_batch(**arguments)
        lease = worker_repository.claim_next(
            worker_id="test", adapter="fake", adapter_version="test", now=NOW,
        )
        worker_repository.finish_failure(
            lease, error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            diagnostic="synthetic authentication failure", now=NOW,
        )
        before = worker_repository.get_job(lease.job_id)
        target = worker_module
    else:
        target = batch_module
    original_revalidate = target.revalidate_credential_defaults
    calls = 0

    def replace_before_commit(current):
        nonlocal calls
        calls += 1
        if calls == 2:
            document = json.loads(config.path.read_text("utf-8"))
            document["default_cookie_platforms"] = []
            config.path.chmod(0o666)
            config.path.write_text(json.dumps(document), encoding="utf-8")
            config.path.chmod(0o444)
        original_revalidate(current)

    monkeypatch.setattr(target, "revalidate_credential_defaults", replace_before_commit)
    with pytest.raises(CredentialDefaultsError):
        if operation == "create":
            repository.create_batch(
                **arguments, credential_mode="use_default", credential_defaults=defaults,
            )
        else:
            worker_repository.request_retry(
                lease.job_id, now=NOW, credential_mode="use_default", credential_defaults=defaults,
            )
    assert calls == 2
    if operation == "retry":
        assert worker_repository.get_job(lease.job_id) == before
    else:
        with database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM batches").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["create", "retry"])
def test_resident_claim_cannot_observe_default_binding_as_anonymous(
    tmp_path: Path, database, repository, monkeypatch, operation: str
) -> None:
    from video_download_control.credential_defaults import prepare_credential_defaults
    import video_download_control.repository as batch_module
    import video_download_control.worker_repository as worker_module

    defaults = prepare_credential_defaults(
        database, _configuration(tmp_path, defaults=["douyin"]),
        run_id=uuid4().hex, now=NOW,
    )
    arguments = dict(
        name=None,
        inputs=_inputs("https://www.douyin.com/video/7123456789012345678"),
        route_policy_version="test-defaults",
    )
    worker_repository = WorkerRepository(database)
    if operation == "retry":
        repository.create_batch(**arguments)
        previous = worker_repository.claim_next(
            worker_id="previous", adapter="fake", adapter_version="test", now=NOW,
        )
        worker_repository.finish_failure(
            previous, error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            diagnostic="synthetic authentication failure", now=NOW,
        )
        target = worker_module
    else:
        target = batch_module
    inside_write = Event()
    release_write = Event()
    claim_entered = Event()
    claim_finished = Event()
    errors = []
    claimed = []
    original_resolve = target.resolve_default_profile_locked

    def block_inside_write(connection, **kwargs):
        assert connection.in_transaction
        inside_write.set()
        assert release_write.wait(5), "test did not release its write transaction"
        return original_resolve(connection, **kwargs)

    def write() -> None:
        try:
            if operation == "create":
                repository.create_batch(
                    **arguments, credential_mode="use_default", credential_defaults=defaults,
                )
            else:
                worker_repository.request_retry(
                    previous.job_id, now=NOW, credential_mode="use_default", credential_defaults=defaults,
                )
        except BaseException as exc:
            errors.append(exc)

    def claim() -> None:
        claim_entered.set()
        try:
            claimed.append(worker_repository.claim_next(
                worker_id="resident", adapter="fake", adapter_version="test", now=NOW,
            ))
        except BaseException as exc:
            errors.append(exc)
        finally:
            claim_finished.set()

    monkeypatch.setattr(target, "resolve_default_profile_locked", block_inside_write)
    writer = Thread(target=write, daemon=True)
    claimer = Thread(target=claim, daemon=True)
    writer.start()
    try:
        assert inside_write.wait(5)
        claimer.start()
        assert claim_entered.wait(5)
        assert not claim_finished.wait(0.1), "claim escaped the uncommitted binding transaction"
    finally:
        release_write.set()
        writer.join(5)
        if claimer.ident is not None:
            claimer.join(5)
    assert not writer.is_alive()
    assert not claimer.is_alive()
    assert errors == []
    assert len(claimed) == 1 and claimed[0] is not None
    assert claimed[0].credential_ref == "synthetic-ref"


@pytest.mark.parametrize("initial_mode,new_mode", [("anonymous", "use_default"), ("use_default", "anonymous")])
@pytest.mark.parametrize("active", [False, True])
def test_cross_batch_duplicate_never_rebinds_an_existing_job(
    tmp_path: Path, database, repository, initial_mode, new_mode, active: bool
) -> None:
    from video_download_control.credential_defaults import prepare_credential_defaults

    defaults = prepare_credential_defaults(
        database, _configuration(tmp_path, defaults=["douyin"]),
        run_id=uuid4().hex, now=NOW,
    )
    arguments = dict(
        name=None,
        inputs=_inputs("https://www.douyin.com/video/7123456789012345678"),
        route_policy_version="test-defaults",
        credential_defaults=defaults,
    )
    original = repository.create_batch(**arguments, credential_mode=initial_mode)
    job_id = original["jobs"][0]["id"]
    worker_repository = WorkerRepository(database)
    if active:
        lease = worker_repository.claim_next(
            worker_id="existing", adapter="fake", adapter_version="test", now=NOW,
        )
        assert lease is not None
    before = worker_repository.get_job(job_id)

    duplicate = repository.create_batch(**arguments, credential_mode=new_mode)

    assert duplicate["status"] == "duplicate"
    assert duplicate["jobs"] == []
    assert duplicate["duplicate_count"] == 1
    assert duplicate["inputs"][0]["duplicate_of_input_record_id"] == original["inputs"][0]["id"]
    assert worker_repository.get_job(job_id) == before


def test_duplicate_keeps_a_manually_changed_profile_instead_of_restoring_default(
    tmp_path: Path, database, repository
) -> None:
    from video_download_control.credential_defaults import prepare_credential_defaults

    defaults = prepare_credential_defaults(
        database, _configuration(tmp_path, defaults=["douyin"]),
        run_id=uuid4().hex, now=NOW,
    )
    arguments = dict(
        name=None,
        inputs=_inputs("https://www.douyin.com/video/7123456789012345678"),
        route_policy_version="test-defaults",
        credential_mode="use_default", credential_defaults=defaults,
    )
    original = repository.create_batch(**arguments)
    job_id = original["jobs"][0]["id"]
    credentials = CredentialRepository(database, clock=lambda: NOW)
    replacement = credentials.register(
        platform=Platform.DOUYIN, name="manual replacement", secret_ref="other-synthetic-ref",
    )
    credentials.assign(profile_id=replacement["id"], job_ids=[job_id])
    worker_repository = WorkerRepository(database)
    before = worker_repository.get_job(job_id)

    duplicate = repository.create_batch(**arguments)

    assert duplicate["jobs"] == []
    assert worker_repository.get_job(job_id) == before
    assert before["credential_profile_id"] == replacement["id"]


def test_prepare_rolls_back_new_profiles_when_a_later_default_is_disabled(
    tmp_path: Path, database
) -> None:
    from video_download_control.credential_defaults import CredentialDefaultsError, prepare_credential_defaults

    config = _configuration(tmp_path, defaults=["douyin"])
    second_source = (tmp_path / "second-synthetic.cookies.txt").resolve()
    second_source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    second_source.chmod(0o444)
    document = json.loads(config.path.read_text("utf-8"))
    document["cookie_sources"].append({
        "platform": "youtube", "credential_ref": "second-synthetic-ref", "path": str(second_source),
    })
    document["default_cookie_platforms"] = ["douyin", "youtube"]
    config.path.chmod(0o666)
    config.path.write_text(json.dumps(document), encoding="utf-8")
    config.path.chmod(0o444)
    config = load_cookie_source_config(config.path)
    credentials = CredentialRepository(database, clock=lambda: NOW)
    disabled = credentials.register(
        platform=Platform.YOUTUBE, name="disabled", secret_ref="second-synthetic-ref",
    )
    credentials.disable(profile_id=disabled["id"])
    before = credentials.list(include_disabled=True)

    with pytest.raises(CredentialDefaultsError):
        prepare_credential_defaults(database, config, run_id=uuid4().hex, now=NOW)

    assert credentials.list(include_disabled=True) == before


def test_prepare_rolls_back_registration_if_config_changes_before_commit(
    tmp_path: Path, database, monkeypatch
) -> None:
    import video_download_control.credential_defaults as defaults_module

    config = _configuration(tmp_path, defaults=["douyin"])
    original = defaults_module._revalidate

    def invalidate_before_commit(defaults):
        document = json.loads(config.path.read_text("utf-8"))
        document["default_cookie_platforms"] = []
        config.path.chmod(0o666)
        config.path.write_text(json.dumps(document), encoding="utf-8")
        config.path.chmod(0o444)
        original(defaults)

    monkeypatch.setattr(defaults_module, "_revalidate", invalidate_before_commit)
    with pytest.raises(defaults_module.CredentialDefaultsError) as captured:
        defaults_module.prepare_credential_defaults(database, config, run_id=uuid4().hex, now=NOW)

    assert CredentialRepository(database).list() == []
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

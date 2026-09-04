from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.cookie_source_config import load_cookie_source_config
from video_download_control.credential_defaults import prepare_credential_defaults
from video_download_control.domain import ErrorCode, JobStatus
from video_download_control.runtime_logging import RuntimeLogConfig, RuntimeLogger
from video_download_control.worker_repository import WorkerRepository


@pytest.fixture
def default_config(tmp_path, settings, database):
    source = tmp_path / "test.cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\ttest\tSYNTHETIC\n", encoding="utf-8")
    source.chmod(0o444)
    config_path = tmp_path / "cookie-config.json"
    config_path.write_text(json.dumps({
        "schema_version": 2,
        "cookie_sources": [{"platform": "youtube", "credential_ref": "api-default-test", "path": str(source)}],
        "default_cookie_platforms": ["youtube"],
    }), encoding="utf-8")
    config_path.chmod(0o444)
    config = load_cookie_source_config(config_path)
    run_id = uuid4().hex
    defaults = prepare_credential_defaults(database, config, run_id=run_id)
    logger = RuntimeLogger(component="control", config=RuntimeLogConfig(directory=settings.data_root / "logs", level="DEBUG"), run_id=run_id)
    return defaults, logger


@pytest.mark.parametrize("mode", ["use_default", "anonymous"])
def test_json_and_file_creation_bind_credentials_without_disclosing_them(settings, database, default_config, mode):
    defaults, logger = default_config
    app = create_app(settings, credential_defaults=defaults, runtime_logger=logger)
    with TestClient(app) as client:
        status = client.get("/api/v1/credential-defaults")
        assert status.status_code == 200
        assert status.json()["platforms"] == ["youtube"]
        assert status.json()["available"] is True
        assert logger.status()["status"] == "ok"
        assert logger.status()["rejected_events"] == 0
        records = client.get("/api/v1/operations/logs").json()["events"]
        assert any(event.get("route") == "/api/v1/credential-defaults" for event in records)
        responses = [
            client.post("/api/v1/batches", json={"inputs": ["https://www.youtube.com/watch?v=api-default"], "credential_mode": mode}),
            client.post(f"/api/v1/batches/import?filename=links.txt&credential_mode={mode}", content="https://www.youtube.com/watch?v=import-default", headers={"Content-Type": "text/plain"}),
        ]
    for response in responses:
        assert response.status_code == 201
        assert response.json()["queued_count"] == 1
        job_id = response.json()["jobs"][0]["id"]
        job = WorkerRepository(database).get_job(job_id)
        assert (job["credential_profile_id"] is not None) is (mode == "use_default")
        assert "api-default-test" not in response.text
        assert "credential_profile_id" not in response.text


def test_default_profile_change_fails_without_partial_batch(settings, database, default_config):
    defaults, logger = default_config
    with TestClient(create_app(settings, credential_defaults=defaults, runtime_logger=logger)) as client:
        with database.connect() as connection:
            connection.execute("UPDATE credential_profiles SET disabled_at = '2026-09-01T00:00:00.000Z'")
        response = client.post("/api/v1/batches", json={"inputs": ["https://www.youtube.com/watch?v=disabled-default"]})
        assert response.status_code == 409
        assert client.get("/api/v1/credential-defaults").json() == {"platforms": ["youtube"], "available": False}
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM download_jobs").fetchone()[0] == 0


def test_other_run_defaults_are_rejected(settings, default_config):
    defaults, _logger = default_config
    with pytest.raises(ValueError, match="credential defaults"):
        create_app(settings, credential_defaults=defaults)


@pytest.mark.parametrize("mode", [None, "use_default", "anonymous"])
@pytest.mark.parametrize("initial_mode", ["use_default", "anonymous"])
def test_retry_explicit_choice_or_legacy_preserve(settings, database, default_config, mode, initial_mode):
    defaults, logger = default_config
    app = create_app(settings, credential_defaults=defaults, runtime_logger=logger)
    with TestClient(app) as client:
        batch = client.post("/api/v1/batches", json={
            "inputs": ["https://youtu.be/credential-retry"], "credential_mode": initial_mode,
        }).json()
        job_id = batch["jobs"][0]["id"]
        now = datetime.fromisoformat(batch["created_at"]) + timedelta(seconds=1)
        lease = app.state.worker_repository.claim_next(
            worker_id="default-api-test", adapter="fake", adapter_version="test", now=now,
        )
        assert lease is not None
        assert app.state.worker_repository.finish_failure(
            lease, error_code=ErrorCode.NETWORK_ERROR, diagnostic="synthetic", now=now,
        ) is JobStatus.FAILED
        previous = WorkerRepository(database).get_job(job_id)
        options = {} if mode is None else {"json": {"credential_mode": mode}}
        response = client.post(f"/api/v1/jobs/{job_id}/retry", **options)
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"job_id", "status", "run_generation"}
        actual = WorkerRepository(database).get_job(job_id)
        expected_default = (initial_mode if mode is None else mode) == "use_default"
        assert (actual["credential_profile_id"] is not None) is expected_default
        assert actual["run_generation"] == previous["run_generation"] + 1
        if mode is None:
            assert actual["credential_profile_id"] == previous["credential_profile_id"]


@pytest.mark.parametrize("body", [
    {"credential_mode": "bogus"}, {"credential_mode": True},
    {"credential_mode": "anonymous", "credential_ref": "untrusted"},
])
def test_invalid_credential_modes_rejected_before_creating_or_retrying(settings, database, body):
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/batches", json={"inputs": ["https://youtu.be/bad-mode"], **body}).status_code == 422
        assert client.post("/api/v1/jobs/not-found/retry", json=body).status_code == 422
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_changed_config_can_only_be_bypassed_by_explicit_anonymous_choice(settings, database, default_config):
    defaults, logger = default_config
    with TestClient(create_app(settings, credential_defaults=defaults, runtime_logger=logger)) as client:
        defaults.config.path.chmod(0o644)
        defaults.config.path.write_text("{}", encoding="utf-8")
        defaults.config.path.chmod(0o444)
        status = client.get("/api/v1/credential-defaults")
        assert status.json() == {"platforms": ["youtube"], "available": False}
        blocked = client.post("/api/v1/batches", json={"inputs": ["https://youtu.be/config-drift"]})
        assert blocked.status_code == 409
        anonymous = client.post("/api/v1/batches", json={"inputs": ["https://youtu.be/config-drift"], "credential_mode": "anonymous"})
        assert anonymous.status_code == 201
        job = WorkerRepository(database).get_job(anonymous.json()["jobs"][0]["id"])
        assert job["credential_profile_id"] is None
        log_response = client.get("/api/v1/operations/logs")
        assert log_response.status_code == 200
        logs = log_response.text
        for marker in [str(defaults.config.path), "api-default-test", "SYNTHETIC"]:
            assert marker not in status.text + blocked.text + anonymous.text + logs

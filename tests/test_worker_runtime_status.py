from __future__ import annotations

import multiprocessing
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import video_download_control.api as api_module
from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.domain import ErrorCode
from video_download_control.runtime_logging import RuntimeLogger, RuntimeLogConfig
from video_download_control.worker_runtime_status import ManagedWorkerRuntimeStatus


RUN_ID = '1' * 32
PRODUCT_IDENTITY = '0.24.3+build.sha256.' + 'a' * 64


def _status() -> ManagedWorkerRuntimeStatus:
    return ManagedWorkerRuntimeStatus(run_id=RUN_ID, product_identity=PRODUCT_IDENTITY)


def _read(source: ManagedWorkerRuntimeStatus, **kwargs):
    return source.snapshot(expected_run_id=RUN_ID, expected_product_identity=PRODUCT_IDENTITY, **kwargs)


def _publish_from_spawn(source: ManagedWorkerRuntimeStatus) -> None:
    source.publish('online', worker_pid=5678)


def test_control_only_reports_unknown_worker_instead_of_disabled_network(settings: Settings) -> None:
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        response = client.get('/api/v1/operations/runtime')
    assert response.status_code == 200
    payload = response.json()
    assert payload['mode'] == 'external_unknown'
    assert payload['state'] == 'unknown'
    assert payload['network_download_enabled'] is None
    assert payload['worker_pid'] is None


@pytest.mark.parametrize('phase', ['starting', 'online', 'stopping', 'stopped', 'check_only'])
def test_runtime_reports_observed_phase_without_claiming_unstarted_network(phase: str) -> None:
    source = _status()
    source.publish(phase, worker_pid=1234 if phase == 'online' else None, now=100.0)
    payload = _read(source, now=101.0)
    assert payload.state == phase
    assert payload.network_download_enabled is (phase == 'online')
    assert payload.heartbeat_age_seconds == 1.0


def test_runtime_heartbeat_expires_even_while_claim_gate_would_remain_active() -> None:
    source = _status()
    source.publish('online', worker_pid=1234, now=100.0)
    payload = _read(source, now=104.0)
    assert payload.state == 'stale'
    assert payload.network_download_enabled is None
    assert payload.detail_code == 'heartbeat_expired'


@pytest.mark.parametrize('phase', ['starting', 'check_only', 'stopping', 'stopped'])
def test_recorded_non_online_lifecycle_phase_survives_slow_preflight_or_cleanup(phase: str) -> None:
    source = _status()
    source.publish(phase, now=100.0)
    payload = _read(source, now=220.0)
    assert payload.state == phase
    assert payload.network_download_enabled is False
    assert payload.heartbeat_age_seconds == 120.0


@pytest.mark.parametrize('wrong_key,wrong_value', [('expected_run_id', '2' * 32),
                                                  ('expected_product_identity', '0.24.3+build.sha256.' + 'b' * 64)])
def test_other_run_or_build_cannot_be_displayed_online(wrong_key: str, wrong_value: str) -> None:
    source = _status()
    source.publish('online', worker_pid=1234)
    expected = {'expected_run_id': RUN_ID, 'expected_product_identity': PRODUCT_IDENTITY, wrong_key: wrong_value}
    payload = source.snapshot(**expected)
    assert payload.state == 'unknown'
    assert payload.network_download_enabled is None
    assert payload.run_id is None


def test_paused_queue_is_independent_of_worker_liveness() -> None:
    source = _status()
    source.publish('online', worker_pid=1234, now=100.0)
    payload = _read(source, now=101.0, queue_paused=True)
    assert payload.state == 'paused'
    assert payload.network_download_enabled is True
    assert payload.queue_paused is True


def test_shared_channel_survives_actual_spawn() -> None:
    source = _status()
    process = multiprocessing.get_context('spawn').Process(target=_publish_from_spawn, args=(source,))
    process.start()
    process.join(10)
    try:
        assert process.exitcode == 0
        payload = _read(source)
        assert payload.state == 'online'
        assert payload.worker_pid == 5678
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()


def test_locked_snapshot_has_bounded_read_and_write() -> None:
    source = _status()
    locked, release = threading.Event(), threading.Event()
    def hold():
        with source._shared.get_lock():
            locked.set()
            release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    assert locked.wait(1)
    try:
        started = time.monotonic()
        assert _read(source).state == 'unknown'
        assert source.publish('online', worker_pid=1234) is False
        assert time.monotonic() - started < 1
    finally:
        release.set()
        thread.join(2)


def test_api_consumes_same_run_supervisor_status_and_never_upgrades_tool_policy(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _status()
    monkeypatch.setattr(api_module, 'current_product_identity', lambda: PRODUCT_IDENTITY)
    logger = RuntimeLogger(component='control', config=RuntimeLogConfig(directory=settings.data_root / 'logs'), run_id=RUN_ID)
    source.publish('online', worker_pid=1234)
    app = create_app(settings, runtime_logger=logger, managed_worker_status=source)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        online = client.get('/api/v1/operations/runtime')
        assert online.status_code == 200
        assert online.json()['state'] == 'online'
        assert online.json()['network_download_enabled'] is True
        assert client.get('/api/v1/operations/tools').json()['network_download_enabled'] is False
        app.state.worker_repository.pause_queue(reason=ErrorCode.STORAGE_ERROR, now=datetime.now(UTC))
        assert client.get('/api/v1/operations/runtime').json()['state'] == 'paused'
        source.publish('stopping', worker_pid=1234)
        assert client.get('/api/v1/operations/runtime').json()['network_download_enabled'] is False
        assert logger.status()['rejected_events'] == 0

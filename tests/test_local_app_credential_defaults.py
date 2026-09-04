from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import video_download_control.api as api_module
import video_download_control.local_app as local_app_module
from video_download_control import __version__
from video_download_control.cookie_source_config import load_cookie_source_config
from video_download_control.credential_defaults import prepare_credential_defaults
from video_download_control.credentials import CredentialRepository
from video_download_control.database import Database
from video_download_control.domain import Platform
from video_download_control.repository import BatchRepository
from video_download_control.service import BatchService

PRODUCT_IDENTITY = f"{__version__}+build.sha256." + "a" * 64
RUN_ID = "1234567890abcdef1234567890abcdef"


class ClosedSurface:
    def __init__(self) -> None:
        self.closed = False
        self.sent = []

    def send_bytes(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


@pytest.fixture
def configuration(tmp_path: Path):
    source = (tmp_path / "synthetic.cookies.txt").resolve()
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    source.chmod(0o444)
    config_path = (tmp_path / "synthetic-cookie-sources.json").resolve()
    created_paths = [source, config_path]

    def make(*, check_only=False, version=2, defaults=True):
        if config_path.exists():
            config_path.chmod(0o600)
        document = {
            "schema_version": version,
            "cookie_sources": [
                {
                    "platform": "douyin",
                    "credential_ref": "synthetic-local-default",
                    "path": str(source),
                },
            ],
        }
        if version == 2:
            document["default_cookie_platforms"] = ["douyin"] if defaults else []
        config_path.write_text(json.dumps(document), encoding="utf-8")
        config_path.chmod(0o444)
        snapshot = load_cookie_source_config(config_path)
        return local_app_module.LocalAppConfig(
            app_root=(tmp_path / "app").resolve(),
            allow_direct_network=True,
            check_only=check_only,
            cookie_source_config=snapshot,
            max_cookie_bytes=4096,
            open_browser=False,
        )

    yield make

    for path in created_paths:
        if path.exists():
            path.chmod(0o600)


@pytest.fixture
def control_child_seam(monkeypatch):
    prepared_calls = []
    app_calls = []
    apps = []

    def prepare(database, config, **kwargs):
        prepared_calls.append((database, config, kwargs))
        # Production preparation requires a fully initialized DB. Do not
        # initialize it here: the first-startup test must prove the child does.
        return prepare_credential_defaults(database, config, **kwargs)

    def create_app(settings, **kwargs):
        app_calls.append((settings, kwargs))
        app = api_module.create_app(settings, **kwargs)
        apps.append(app)
        return app

    class NoSocketServer:
        def __init__(self, uvicorn_config, *, ready_callback):
            self.app = uvicorn_config.app
            self.ready_callback = ready_callback
            self.should_exit = False

        async def serve(self, *, sockets):
            assert len(sockets) == 1
            async with self.app.router.lifespan_context(self.app):
                self.ready_callback()

    monkeypatch.setattr(local_app_module, "_apply_child_environment", lambda *_a, **_k: None)
    monkeypatch.setattr(local_app_module, "_ignore_supervisor_signals", lambda: None)
    monkeypatch.setattr(local_app_module, "_wait_for_child_command", lambda *_a: True)
    monkeypatch.setattr(local_app_module, "_command_channel_closed", lambda *_a: True)
    monkeypatch.setattr(local_app_module, "current_product_identity", lambda: PRODUCT_IDENTITY)
    monkeypatch.setattr(local_app_module, "_NoSignalUvicornServer", NoSocketServer)
    monkeypatch.setattr(local_app_module, "create_app", create_app)
    monkeypatch.setattr(
        local_app_module, "prepare_credential_defaults", prepare, raising=False,
    )
    return prepared_calls, app_calls, apps


def run_control_child(config):
    listener = ClosedSurface()
    ready = ClosedSurface()
    command = ClosedSurface()
    local_app_module._control_child_main(
        config, listener, ready, command, RUN_ID, PRODUCT_IDENTITY,
    )
    assert listener.closed and ready.closed and command.closed
    assert len(ready.sent) == 1
    handshake = json.loads(ready.sent[0])
    assert handshake["status"] == "ready"
    assert handshake["phase"] == "control_ready"
    assert handshake["run_id"] == RUN_ID


@pytest.mark.skipif(os.name != "nt", reason="standalone control child requires Windows")
@pytest.mark.parametrize(
    ("check_only", "version", "explicit_defaults", "should_prepare"),
    [(False, 2, True, True), (True, 2, True, False), (False, 1, False, False), (False, 2, False, False)],
)
def test_control_child_only_prepares_explicit_defaults_for_normal_startup(
    configuration, control_child_seam,
    check_only, version, explicit_defaults, should_prepare,
) -> None:
    config = configuration(check_only=check_only, version=version, defaults=explicit_defaults)
    prepared_calls, app_calls, apps = control_child_seam
    snapshot = config.cookie_source_config
    before_config = snapshot.path.read_bytes()
    before_source = snapshot.sources[0].path.read_bytes()
    assert not config.database_path.exists()

    run_control_child(config)

    assert len(app_calls) == 1
    _, app_kwargs = app_calls[0]
    assert app_kwargs["runtime_logger"].run_id == RUN_ID
    profiles = CredentialRepository(Database(config.database_path)).list()
    if should_prepare:
        assert len(prepared_calls) == 1
        preparation_db, preparation_config, preparation_kwargs = prepared_calls[0]
        assert preparation_db.path == config.database_path
        assert preparation_config is snapshot
        assert preparation_kwargs == {"run_id": RUN_ID, "max_cookie_bytes": 4096}
        defaults = app_kwargs["credential_defaults"]
        assert defaults.run_id == RUN_ID
        assert defaults.platforms == (Platform.DOUYIN,)
        assert len(profiles) == 1
        assert profiles[0]["secret_ref"] == "synthetic-local-default"
        assert apps[0].state.batch_service.credential_defaults is defaults
    else:
        assert prepared_calls == []
        assert "credential_defaults" not in app_kwargs
        assert profiles == []
    assert snapshot.path.read_bytes() == before_config
    assert snapshot.sources[0].path.read_bytes() == before_source


@pytest.mark.skipif(os.name != "nt", reason="standalone control child requires Windows")
def test_check_preserves_existing_profiles_and_existing_job_bindings(
    configuration, control_child_seam,
) -> None:
    config = configuration(check_only=True)
    database = Database(config.database_path)
    database.initialize()
    credentials = CredentialRepository(database)
    profile = credentials.register(
        platform=Platform.DOUYIN,
        name="existing user-managed profile",
        secret_ref="existing-synthetic-reference",
    )
    service = BatchService(
        repository=BatchRepository(database),
        max_batch_urls=50,
        route_policy_version="local-default-check-test",
    )
    batch = service.create_batch(
        name="existing untouched job",
        raw_inputs=["https://www.douyin.com/video/1234567890"],
    )
    credentials.assign(job_ids=(batch["jobs"][0]["id"],), profile_id=profile["id"])
    credentials.disable(profile_id=profile["id"])

    def snapshot_rows():
        with database.connect() as connection:
            return {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
                for table in ("credential_profiles", "download_jobs", "job_attempts")
            }

    before = snapshot_rows()

    run_control_child(config)

    assert snapshot_rows() == before
    prepared_calls, app_calls, _ = control_child_seam
    assert prepared_calls == []
    assert "credential_defaults" not in app_calls[0][1]

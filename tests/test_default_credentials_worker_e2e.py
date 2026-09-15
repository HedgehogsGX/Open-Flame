"""Offline HTTP -> real Worker/yt-dlp command -> private Cookie -> asset checks."""

from __future__ import annotations

from local_http_client import download_client

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from video_download_control.adapters import DirectEgress, YtDlpAdapter, YtDlpCommandFactory
from video_download_control.api import create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.candidate_cookies import AttemptCookieResolver
from video_download_control.cookie_source_config import load_cookie_source_config
from video_download_control.credential_defaults import prepare_credential_defaults
from video_download_control.runtime_logging import RuntimeLogConfig, RuntimeLogger
from video_download_control.subprocess_runner import CommandResult
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


MEDIA_ID = "defaults-e2e-video"
MEDIA_BYTES = b"synthetic-default-credential-offline-video"
COOKIE_VALUE = "synthetic-cookie-payload-never-publish"
PRIVATE_REF = "opaque-default-e2e-private-ref"
VERSION = "2026.08.19"


class OfflineRunnerGuard:
    """Deployment seam for a runner which never starts a subprocess or socket."""

    def assert_ready(self, *, adapter_name: str) -> None:
        assert adapter_name == "yt_dlp"


class CookieInspectingRunner:
    def __init__(self, *, source: Path, data_root: Path, authenticated: bool) -> None:
        self.source = source
        self.data_root = data_root
        self.authenticated = authenticated
        self.cookie_paths: list[Path] = []
        self.operations: list[str] = []

    def run(self, spec, *, is_cancelled=None):
        assert is_cancelled is None or not is_cancelled()
        arguments = spec.arguments
        assert "--cookies-from-browser" not in arguments
        if "--version" in arguments:
            assert "--cookies" not in arguments
            return CommandResult(0, (VERSION + "\n").encode(), b"", 0.01)

        assert ("--cookies" in arguments) is self.authenticated
        if self.authenticated:
            cookie_path = Path(arguments[arguments.index("--cookies") + 1])
            assert cookie_path != self.source
            assert not cookie_path.samefile(self.source)
            assert cookie_path.is_relative_to(self.data_root / "temporary")
            assert cookie_path.parent.name == "secrets"
            assert cookie_path.read_bytes() == self.source.read_bytes()
            assert cookie_path.stat().st_nlink == 1
            if os.name == "posix":
                assert cookie_path.stat().st_mode & 0o777 == 0o600
                assert cookie_path.parent.stat().st_mode & 0o777 == 0o700
            self.cookie_paths.append(cookie_path)
        else:
            assert list((self.data_root / "temporary").rglob("*.cookies.txt")) == []

        if "--dump-single-json" in arguments:
            self.operations.append("probe")
            payload = {
                "id": MEDIA_ID,
                "title": "Offline default credential video",
                "duration": 1.0,
                "height": 720,
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
            }
            return CommandResult(0, json.dumps(payload).encode(), b"", 0.01)

        self.operations.append("download")
        output = Path(arguments[arguments.index("--paths") + 1])
        original = output / f"{MEDIA_ID}.mp4"
        original.write_bytes(MEDIA_BYTES)
        mapping = Path(arguments[arguments.index("--print-to-file") + 2].replace("%%", "%"))
        assert mapping.is_file()
        with mapping.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": MEDIA_ID, "filepath": str(original.resolve())}) + "\n")
        return CommandResult(0, b"", b"", 0.01)


@pytest.fixture
def prepared_defaults(tmp_path, settings, database):
    source = (tmp_path / "private-e2e-source.cookies.txt").resolve()
    source_bytes = (
        "# Netscape HTTP Cookie File\n"
        f".youtube.com\tTRUE\t/\tTRUE\t2147483647\tsynthetic\t{COOKIE_VALUE}\n"
    ).encode()
    source.write_bytes(source_bytes)
    config_path = (tmp_path / "private-e2e-defaults.json").resolve()
    config_path.write_text(
        json.dumps({
            "schema_version": 2,
            "cookie_sources": [{
                "platform": "youtube", "credential_ref": PRIVATE_REF, "path": str(source),
            }],
            "default_cookie_platforms": ["youtube"],
        }),
        encoding="utf-8",
    )
    source.chmod(0o444)
    config_path.chmod(0o444)
    try:
        config = load_cookie_source_config(config_path)
        run_id = uuid4().hex
        defaults = prepare_credential_defaults(database, config, run_id=run_id)
        log_config = RuntimeLogConfig(directory=settings.data_root / "logs")
        control_logger = RuntimeLogger(component="control", config=log_config, run_id=run_id)
        worker_logger = RuntimeLogger(component="local-worker", config=log_config, run_id=run_id)
        yield defaults, config, source, config_path, source_bytes, control_logger, worker_logger
    finally:
        source.chmod(0o600)
        config_path.chmod(0o600)


@pytest.mark.parametrize("mode", ["use_default", "anonymous"])
@pytest.mark.parametrize("entrypoint", ["json", "txt", "csv"])
def test_http_default_mode_reaches_private_cookie_and_ready_asset(
    tmp_path, settings, database, prepared_defaults, mode, entrypoint,
) -> None:
    defaults, config, source, config_path, source_bytes, control_logger, worker_logger = prepared_defaults
    app = create_app(settings, credential_defaults=defaults, runtime_logger=control_logger)
    tool_root = (tmp_path / "offline-tools").resolve()
    tool_root.mkdir()
    executable = tool_root / "never-executed-yt-dlp.exe"
    executable.write_bytes(b"offline-runner-placeholder")
    runner = CookieInspectingRunner(
        source=source, data_root=settings.data_root, authenticated=mode == "use_default",
    )
    adapter = YtDlpAdapter(
        factory=YtDlpCommandFactory(
            executable=executable,
            ffmpeg_directory=tool_root,
            expected_version=VERSION,
            egress=DirectEgress(),
        ),
        runner=runner,
        cookie_resolver=AttemptCookieResolver(config.sources),
    )
    worker = Worker(
        worker_id="default-cookie-http-e2e",
        repository=WorkerRepository(database),
        adapter=adapter,
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
        network_guard=OfflineRunnerGuard(),
        runtime_logger=worker_logger,
    )
    public_payloads: list[str] = []
    with download_client(app) as client:
        defaults_status = client.get("/api/v1/credential-defaults")
        assert defaults_status.status_code == 200
        assert defaults_status.json()["platforms"] == ["youtube"]
        public_payloads.append(defaults_status.text)
        url = f"https://www.youtube.com/watch?v={MEDIA_ID}"
        if entrypoint == "json":
            response = client.post("/api/v1/batches", json={
                "inputs": [url], "credential_mode": mode,
            })
        else:
            response = client.post(
                "/api/v1/batches/import",
                params={"filename": f"links.{entrypoint}", "credential_mode": mode},
                content=("url\n" if entrypoint == "csv" else "") + url + "\n",
                headers={"Content-Type": "text/csv" if entrypoint == "csv" else "text/plain"},
            )
        assert response.status_code == 201
        public_payloads.append(response.text)
        batch = response.json()
        assert batch["queued_count"] == 1
        result = worker.run_once()
        assert result is not None and result.status == "ready"
        # Do not let the next maintenance/claim cycle hide a cleanup failure.
        assert all(not path.exists() for path in runner.cookie_paths)
        assert list((settings.data_root / "temporary").rglob("*.cookies.txt")) == []
        assert worker.run_once() is None

        refreshed = client.get(f"/api/v1/batches/{batch['id']}")
        assert refreshed.status_code == 200
        assert refreshed.json()["ready_count"] == 1
        assert refreshed.json()["status"] == "ready"
        public_payloads.append(refreshed.text)
        assets = client.get(f"/api/v1/batches/{batch['id']}/assets")
        assert assets.status_code == 200
        assert len(assets.json()) == 1
        public_payloads.append(assets.text)
        download = client.get(assets.json()[0]["download_url"])
        assert download.status_code == 200
        assert download.content == MEDIA_BYTES

    assert runner.operations == ["probe", "download"]
    if mode == "use_default":
        assert len(runner.cookie_paths) == 2
        assert len(set(runner.cookie_paths)) == 2
        assert all(not path.exists() for path in runner.cookie_paths)
    else:
        assert runner.cookie_paths == []
    assert list((settings.data_root / "temporary").rglob("*.cookies.txt")) == []
    assert source.read_bytes() == source_bytes

    source_metadata = list((settings.data_root / "assets").rglob("source.json"))
    manifests = list((settings.data_root / "assets").rglob("manifest.json"))
    assert len(source_metadata) == len(manifests) == 1
    assert json.loads(manifests[0].read_text("utf-8"))["producer"]["adapter"] == "yt_dlp"
    retained_text = public_payloads + [
        path.read_text("utf-8")
        for path in source_metadata + manifests + list((settings.data_root / "logs").glob("*.jsonl"))
    ]
    private_values = [
        COOKIE_VALUE, PRIVATE_REF, str(source), source.name, str(config_path), config_path.name,
        "credential_profile_id", *(str(path) for path in runner.cookie_paths),
        *(path.name for path in runner.cookie_paths),
    ]
    for text in retained_text:
        for private_value in private_values:
            assert private_value not in text

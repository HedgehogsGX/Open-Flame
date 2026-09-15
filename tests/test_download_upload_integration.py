"""Offline end-to-end handoff using real download registration and upload APIs.

Only the media-producing adapter and platform backend are synthetic. No
repository/asset lookup is monkeypatched, and no account login or submission
is required for creating local drafts.
"""
from __future__ import annotations

from local_http_client import download_client

import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.api import create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.database import SCHEMA_VERSION
from video_download_control.editing.contracts import RenderAsset, RenderResult
from video_download_control.editing.service import default_editing_root
from video_download_control.uploads.service import UploadService, default_upload_root
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


PAYLOAD = b"offline adapter output: download-to-upload integration\n"
SOURCE_URL = "https://www.bilibili.com/video/av170001"


class _OfflineVideoAdapter(ScriptedFakeAdapter):
    """Use an accepted video suffix; NonEmptyTestVerifier remains explicit."""

    def download(self, request, context, progress, cancellation):
        result = super().download(request, context, progress, cancellation)
        files = []
        for produced in result.files:
            video = produced.path.with_suffix(".mp4")
            produced.path.rename(video)
            files.append(replace(produced, path=video))
        return replace(result, files=tuple(files))


class _OfflineNonVideoAdapter(_OfflineVideoAdapter):
    """Register a non-video original with an upload-accepted suffix."""

    def __init__(self, *, payload: bytes, media_kind: str = "audio") -> None:
        super().__init__(payload=payload)
        self.media_kind = media_kind

    def probe(self, request, context):
        result = super().probe(request, context)
        return replace(
            result,
            items=tuple(
                replace(item, media_kind=self.media_kind) for item in result.items
            ),
        )

    def download(self, request, context, progress, cancellation):
        result = super().download(request, context, progress, cancellation)
        return replace(
            result,
            files=tuple(
                replace(produced, media_kind=self.media_kind)
                for produced in result.files
            ),
        )


class _NoRemoteBackend:
    def __init__(self):
        self.actions = []

    def inspect(self):
        return {"ready": False, "code": "synthetic_offline"}

    def _forbidden(self, action):
        self.actions.append(action)
        raise AssertionError("platform actions are forbidden in a local draft test")

    def login(self, *args, **kwargs):
        return self._forbidden("login")

    def check(self, *args, **kwargs):
        return self._forbidden("check")

    def upload(self, *args, **kwargs):
        return self._forbidden("upload")


class _OfflineEditProcessor:
    def render(
        self,
        source,
        output_dir,
        recipe,
        *,
        cancel_event=None,
        expected_source_size=None,
        expected_source_sha256=None,
    ):
        assert source.read_bytes() == PAYLOAD
        assert expected_source_size == len(PAYLOAD)
        assert expected_source_sha256 == hashlib.sha256(PAYLOAD).hexdigest()
        target = output_dir / "segment-001.mp4"
        target.write_bytes(b"derived edit output\n")
        return RenderResult(
            status="ready",
            code="render_complete",
            assets=(
                RenderAsset(
                    kind="segment",
                    path=target,
                    name=target.name,
                    mime_type="video/mp4",
                    ordinal=1,
                    duration_ms=1000,
                    width=1280,
                    height=720,
                    container="mov,mp4,m4a,3gp,3g2,mj2",
                    video_codec="h264",
                    audio_codec="aac",
                ),
            ),
        )


@pytest.fixture
def handoff_app(settings):
    app = create_app(settings)
    backend = _NoRemoteBackend()
    app.state.upload_service_factory = lambda root: UploadService(root, backend)
    with download_client(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Upload-CSRF"] = client.get("/api/v1/uploads/session").json()["csrf_token"]
        yield client, app, backend


def _download(client, app, settings, *, duplicate=False):
    response = client.post("/api/v1/batches", json={"name": "Offline handoff integration", "inputs": [SOURCE_URL]})
    assert response.status_code == 201
    owner = response.json()
    worker = Worker(
        worker_id="offline-handoff-worker", repository=WorkerRepository(app.state.database),
        adapter=_OfflineVideoAdapter(payload=PAYLOAD),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
    )
    result = worker.run_once()
    assert result is not None and result.status == "ready"
    batch = client.get(f"/api/v1/batches/{owner['id']}").json()
    assert batch["status"] == "ready"
    if duplicate:
        response = client.post("/api/v1/batches", json={"name": "Reopened duplicate handoff", "inputs": [SOURCE_URL]})
        assert response.status_code == 201
        batch = response.json()
        assert batch["status"] == "duplicate" and batch["jobs"] == []
    response = client.get(f"/api/v1/batches/{batch['id']}/assets")
    assert response.status_code == 200
    assert len(response.json()) == 1
    asset = response.json()[0]
    registered = app.state.batch_service.get_ready_original_asset(asset["asset_id"])
    assert registered is not None
    assert asset["job_id"] == owner["jobs"][0]["id"]
    assert registered["sha256"] == registered["original_sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    original = settings.data_root.joinpath(*Path(registered["original_path"]).parts)
    assert original.read_bytes() == PAYLOAD
    return batch, asset, original


def _download_snapshot(app):
    # These records belong solely to this test's temporary download database.
    tables = ("batches", "input_records", "source_items", "download_jobs", "job_attempts",
              "media_assets", "job_assets", "artifacts", "credential_profiles")
    with app.state.database.connect() as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in tables}


@pytest.mark.parametrize("duplicate", [False, True], ids=["ready-owner", "ready-reused-by-duplicate"])
def test_registered_download_copies_to_upload_and_three_platform_local_drafts(handoff_app, settings, duplicate):
    client, app, backend = handoff_app
    batch, asset, original = _download(client, app, settings, duplicate=duplicate)
    download_before = _download_snapshot(app)
    original_before = original.stat()
    manifest = original.parent.parent / "metadata" / "manifest.json"
    manifest_before = manifest.read_bytes()
    upload_root = default_upload_root(settings.data_root)

    page = client.get("/uploads", params={"asset_id": asset["asset_id"]})
    assert page.status_code == 200
    assert not upload_root.exists(), "opening an upload link must not import or submit"
    assert client.get(asset["download_url"]).content == PAYLOAD

    imported = client.post(f"/api/v1/uploads/sources/assets/{asset['asset_id']}")
    assert imported.status_code == 201, imported.text
    source = imported.json()
    assert source["sha256"] == asset["original"]["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert source["size"] == asset["original"]["size_bytes"] == len(PAYLOAD)
    assert source["name"] == f"download-{asset['asset_id']}.mp4"
    assert set(source) == {
        "id", "name", "size", "sha256", "media_present", "media_state",
        "media_deleted_at", "active_reference_count", "can_delete",
    }
    assert source["media_present"] is True
    assert source["media_state"] == "present"
    assert source["active_reference_count"] == 0
    assert source["can_delete"] is True
    copied = upload_root / "media" / f"{source['id']}.mp4"
    assert copied.read_bytes() == PAYLOAD
    assert not os.path.samefile(original, copied)
    assert copied.stat().st_nlink == original.stat().st_nlink == 1
    assert not copied.is_relative_to(settings.data_root)

    jobs = []
    for platform, mode in (("bilibili", "publish"), ("douyin", "publish"), ("tencent", "draft")):
        account_response = client.post("/api/v1/uploads/accounts", json={"platform": platform, "name": "Offline " + platform})
        assert account_response.status_code == 201
        account = account_response.json()
        assert account["auth_state"] == "unchecked"
        payload = {"source_id": source["id"], "account_ids": [account["id"]], "title": "Offline handoff test",
                   "description": "Synthetic integration; never submitted.", "tags": ["synthetic"],
                   "category_id": 21, "copyright": 1, "mode": mode, "idempotency_key": "handoff-" + platform}
        created = client.post("/api/v1/uploads/jobs", json=payload)
        assert created.status_code == 201, created.text
        assert len(created.json()) == 1
        job = created.json()[0]
        assert job["state"] == "draft" and job["source_id"] == source["id"]
        assert job["account_id"] == account["id"] and job["platform"] == platform and job["mode"] == mode
        jobs.append(job)

    assert {job["id"] for job in client.get("/api/v1/uploads/jobs").json()} == {job["id"] for job in jobs}
    assert _download_snapshot(app) == download_before
    assert original.read_bytes() == PAYLOAD and original.stat().st_mtime_ns == original_before.st_mtime_ns
    assert manifest.read_bytes() == manifest_before
    assert backend.actions == []
    assert SCHEMA_VERSION == 11
    assert app.state.database.readiness() == (True, "ok")
    # Upload source is an independent retained copy, not a link into downloads.
    original.write_bytes(b"x" * len(PAYLOAD))
    assert copied.read_bytes() == PAYLOAD
    assert app.state.upload_manager.service._source_path(source["id"]) == copied


@pytest.mark.parametrize("duplicate", [False, True], ids=["owner-link", "duplicate-link"])
def test_real_ready_asset_response_builds_usable_upload_link_in_download_dom(handoff_app, settings, duplicate):
    from test_batch_assets_ui import ASSET_HELPERS, run_frontend

    client, app, backend = handoff_app
    batch, asset, _ = _download(client, app, settings, duplicate=duplicate)
    exercise = ASSET_HELPERS + "\n__test.created=" + json.dumps(batch) + ";"
    exercise += "\n__test.assets[" + json.dumps(batch["id"]) + "]=" + json.dumps([asset]) + ";"
    exercise += """
      document.querySelector('#inputs').value = 'https://www.bilibili.com/video/av170001';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      return snapshot();
    """
    rendered = run_frontend(exercise)
    links = [link for link in rendered["links"] if "用于上传" in link["text"]]
    assert len(links) == 1
    assert links[0]["href"] == "/uploads?asset_id=" + asset["asset_id"]
    assert client.get(links[0]["href"]).status_code == 200
    assert not default_upload_root(settings.data_root).exists()
    assert backend.actions == []


@pytest.mark.parametrize("damage", ["owner-not-ready", "asset-not-ready", "registration-hash-mismatch"])
def test_asset_import_requires_real_ready_owner_and_consistent_registration(handoff_app, settings, damage):
    client, app, backend = handoff_app
    _, asset, original = _download(client, app, settings)
    with app.state.database.connect() as db:
        if damage == "owner-not-ready":
            db.execute("UPDATE download_jobs SET status='failed' WHERE id=?", (asset["job_id"],))
        elif damage == "asset-not-ready":
            db.execute("UPDATE media_assets SET status='failed' WHERE id=?", (asset["asset_id"],))
        else:
            db.execute("UPDATE artifacts SET sha256=? WHERE asset_id=? AND kind='original'", ("0" * 64, asset["asset_id"]))
    rejected = client.post(f"/api/v1/uploads/sources/assets/{asset['asset_id']}")
    assert rejected.status_code == 404
    assert rejected.json() == {"detail": "asset_not_found"}
    assert not default_upload_root(settings.data_root).exists()
    assert original.read_bytes() == PAYLOAD
    assert backend.actions == []


@pytest.mark.parametrize("damage,expected_code", [("missing", "asset_file_unavailable"), ("size-changed", "asset_file_unavailable"), ("bytes-changed", "source_hash_mismatch")])
def test_changed_registered_download_cannot_enter_upload_sources(handoff_app, settings, damage, expected_code):
    client, app, backend = handoff_app
    _, asset, original = _download(client, app, settings)
    download_before = _download_snapshot(app)
    if damage == "missing":
        original.unlink()
    else:
        original.write_bytes(b"x" * (len(PAYLOAD) if damage == "bytes-changed" else len(PAYLOAD) + 1))
    rejected = client.post(f"/api/v1/uploads/sources/assets/{asset['asset_id']}")
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": expected_code}
    assert client.get("/api/v1/uploads/sources").json() == []
    assert list((default_upload_root(settings.data_root) / "media").iterdir()) == []
    assert _download_snapshot(app) == download_before
    assert backend.actions == []


@pytest.mark.parametrize("media_kind", ["audio", "image", "unknown"])
def test_nonvideo_ready_asset_cannot_create_an_upload_source(
    handoff_app,
    settings,
    media_kind,
):
    client, app, backend = handoff_app
    created = client.post(
        "/api/v1/batches",
        json={"name": "Offline non-video boundary", "inputs": [SOURCE_URL]},
    )
    assert created.status_code == 201
    result = Worker(
        worker_id="offline-nonvideo-boundary-worker",
        repository=WorkerRepository(app.state.database),
        adapter=_OfflineNonVideoAdapter(
            payload=PAYLOAD,
            media_kind="video" if media_kind == "unknown" else media_kind,
        ),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
    ).run_once()
    assert result is not None and result.status == "ready"
    assets = client.get(
        f"/api/v1/batches/{created.json()['id']}/assets"
    ).json()
    assert len(assets) == 1
    if media_kind == "unknown":
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE media_assets SET media_kind = 'unknown' WHERE id = ?",
                (assets[0]["asset_id"],),
            )
    else:
        assert assets[0]["original"]["media_kind"] == media_kind
    downloaded = client.get(assets[0]["download_url"])

    rejected = client.post(
        f"/api/v1/uploads/sources/assets/{assets[0]['asset_id']}"
    )

    assert downloaded.status_code == 200
    assert downloaded.content == PAYLOAD
    assert rejected.status_code == 404
    assert rejected.json() == {"detail": "asset_not_found"}
    assert not default_upload_root(settings.data_root).exists()
    assert backend.actions == []


def test_download_edit_output_copies_into_upload_without_platform_action(
    handoff_app,
    settings,
):
    client, app, backend = handoff_app
    _, asset, original = _download(client, app, settings)
    download_before = _download_snapshot(app)
    original_before = original.read_bytes()
    app.state.editing_manager.processor_factory = _OfflineEditProcessor
    client.headers["X-Editing-CSRF"] = client.get(
        "/api/v1/edits/session"
    ).json()["csrf_token"]

    page = client.get("/edits", params={"asset_id": asset["asset_id"]})
    assert page.status_code == 200
    assert not default_editing_root(settings.data_root).exists()
    project_response = client.post(
        f"/api/v1/edits/projects/assets/{asset['asset_id']}",
        json={"name": "Offline edit handoff", "idempotency_key": "handoff-project"},
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    saved = client.put(
        f"/api/v1/edits/projects/{project['id']}/draft",
        json={
            "expected_version": 1,
            "idempotency_key": "handoff-draft",
            "recipe": {
                "segments": [{"start_ms": 0, "end_ms": 1000, "label": "clip"}],
                "cover": None,
                "translation": None,
                "dubbing": None,
            },
        },
    )
    assert saved.status_code == 200, saved.text
    plan = client.post(
        f"/api/v1/edits/projects/{project['id']}/plans",
        json={"expected_version": 2, "idempotency_key": "handoff-plan"},
    ).json()
    assert plan["state"] == "review"
    assert client.post(f"/api/v1/edits/plans/{plan['id']}/confirm").status_code == 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        plan = client.get(f"/api/v1/edits/plans/{plan['id']}").json()
        if plan["state"] == "ready":
            break
        time.sleep(0.02)
    assert plan["state"] == "ready"
    output = plan["assets"][0]
    edited_path = app.state.editing_manager.resolve_output(output["id"])[0]
    assert edited_path.read_bytes() == b"derived edit output\n"
    assert original.read_bytes() == original_before
    assert _download_snapshot(app) == download_before
    assert backend.actions == []

    upload_page = client.get("/uploads", params={"edit_output_id": output["id"]})
    assert upload_page.status_code == 200
    assert client.get("/api/v1/uploads/sources").json() == []
    imported = client.post(f"/api/v1/uploads/sources/edits/{output['id']}")
    assert imported.status_code == 201, imported.text
    upload_source = imported.json()
    upload_path = default_upload_root(settings.data_root) / "media" / (
        upload_source["id"] + ".mp4"
    )
    assert upload_path.read_bytes() == edited_path.read_bytes()
    assert upload_path.resolve() != edited_path.resolve()
    assert original.read_bytes() == original_before
    assert _download_snapshot(app) == download_before
    assert backend.actions == []

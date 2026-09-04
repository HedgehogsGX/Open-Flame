from __future__ import annotations

import hashlib
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.uploads import api as upload_api
from video_download_control.uploads.contracts import BackendResult
from video_download_control.uploads.service import UploadService, default_upload_root


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.outcome = "submitted"

    def inspect(self):
        return {"ready": True, "code": "synthetic_ready"}

    def login(self, platform, account_id, stop):
        self.calls.append(("login", platform, account_id))
        return BackendResult("ready", "synthetic_ready")

    def check(self, platform, account_id, stop):
        return self.login(platform, account_id, stop)

    def upload(self, request, stop):
        self.calls.append(("upload", request))
        state = "draft_saved" if request.mode == "draft" else self.outcome
        return BackendResult(state, "synthetic_result")


@pytest.fixture
def upload_client(settings):
    backend = FakeBackend()
    app = create_app(settings)
    app.state.upload_service_factory = lambda root: UploadService(root, backend)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/v1/uploads/session").json()["csrf_token"]
        client.headers["X-Upload-CSRF"] = token
        yield client, app, backend


def _wait(client, path, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200
        if predicate(response.json()):
            return response.json()
        time.sleep(0.01)
    pytest.fail("synthetic upload operation did not complete")


def _account(client, platform="bilibili"):
    response = client.post("/api/v1/uploads/accounts", json={"platform": platform, "name": "synthetic account"})
    assert response.status_code == 201
    account = response.json()
    response = client.post(f"/api/v1/uploads/accounts/{account['id']}/login")
    assert response.status_code == 202
    _wait(client, "/api/v1/uploads/accounts", lambda rows: any(row["id"] == account["id"] and row["auth_state"] == "ready" for row in rows))
    return account


def _source(client):
    response = client.post("/api/v1/uploads/sources?name=synthetic.mp4", content=b"synthetic-video-only",
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 201
    return response.json()


def _draft(client, account, source, **changes):
    payload = {"source_id": source["id"], "account_ids": [account["id"]], "title": "Synthetic video", "description": "", "tags": ["synthetic"],
               "category_id": 21, "copyright": 1, "mode": "publish", "idempotency_key": "synthetic-" + uuid4().hex}
    payload.update(changes)
    response = client.post("/api/v1/uploads/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()[0], payload


def test_download_startup_and_upload_html_do_not_start_upload_worker(settings):
    app = create_app(settings)
    root = default_upload_root(settings.data_root)
    app.state.upload_service_factory = lambda _: pytest.fail("eager upload initialization")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
        page = client.get("/uploads")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["x-frame-options"] == "DENY"
        assert "打开上传器" in client.get("/").text
        assert not root.exists()
        assert client.get("/api/v1/uploads/session").status_code == 200
    assert not root.exists()


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example"}, {"Host": "127.0.0.1.evil.example"},
    {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:8888"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_upload_reads_reject_cross_origin_and_non_loopback(upload_client, headers):
    client, app, backend = upload_client
    response = client.get("/api/v1/uploads/accounts", headers=headers)
    assert response.status_code == 403
    assert app.state.upload_manager.service is None
    assert backend.calls == []


def test_mutations_require_session_nonce_and_matching_origin(upload_client):
    client, app, _ = upload_client
    payload = {"platform": "douyin", "name": "synthetic"}
    token = client.headers.pop("X-Upload-CSRF")
    assert client.post("/api/v1/uploads/accounts", json=payload).status_code == 403
    assert client.post("/api/v1/uploads/accounts", json=payload, headers={"X-Upload-CSRF": "wrong"}).status_code == 403
    assert client.post("/api/v1/uploads/accounts", json=payload, headers={"X-Upload-CSRF": token, "Origin": "http://localhost"}).status_code == 403
    assert app.state.upload_manager.service is None
    assert client.post("/api/v1/uploads/accounts", json=payload, headers={"X-Upload-CSRF": token, "Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}).status_code == 201


@pytest.mark.parametrize("platform,mode,result", [("bilibili", "publish", "submitted"), ("douyin", "publish", "submitted"), ("tencent", "draft", "draft_saved"), ("tencent", "publish", "submitted")])
def test_explicit_preview_confirm_and_idempotency(upload_client, platform, mode, result):
    client, app, backend = upload_client
    account = _account(client, platform)
    source = _source(client)
    assert set(source) == {"id", "name", "size", "sha256"}
    assert source["sha256"] == hashlib.sha256(b"synthetic-video-only").hexdigest()
    job, payload = _draft(client, account, source, mode=mode)
    assert job["state"] == "draft"
    assert not any(call[0] == "upload" for call in backend.calls)
    duplicate = client.post("/api/v1/uploads/jobs", json=payload)
    assert duplicate.json()[0]["id"] == job["id"]
    assert client.post(f"/api/v1/uploads/jobs/{job['id']}/confirm").status_code == 200
    _wait(client, "/api/v1/uploads/jobs", lambda rows: rows[0]["state"] == result)
    assert client.post(f"/api/v1/uploads/jobs/{job['id']}/confirm").status_code == 200
    assert len([call for call in backend.calls if call[0] == "upload"]) == 1
    assert all("path" not in key and "secret" not in key for key in job)
    assert list((app.state.upload_manager.root / "incoming").iterdir()) == []


def test_unknown_requires_acknowledgement_and_creates_new_unconfirmed_draft(upload_client):
    client, _, backend = upload_client
    backend.outcome = "unknown"
    job, _ = _draft(client, _account(client, "douyin"), _source(client))
    client.post(f"/api/v1/uploads/jobs/{job['id']}/confirm")
    _wait(client, "/api/v1/uploads/jobs", lambda rows: rows[0]["state"] == "unknown")
    assert client.post(f"/api/v1/uploads/jobs/{job['id']}/retry", json={}).status_code == 409
    retry = client.post(f"/api/v1/uploads/jobs/{job['id']}/retry", json={"acknowledge_unknown": True})
    assert retry.status_code == 201
    assert retry.json()["state"] == "draft" and retry.json()["id"] != job["id"]
    assert len([call for call in backend.calls if call[0] == "upload"]) == 1


def test_stream_limits_and_invalid_metadata_leave_no_incoming_file(upload_client, monkeypatch):
    client, app, _ = upload_client
    monkeypatch.setattr(upload_api, "MAX_SOURCE_BYTES", 4)
    response = client.post("/api/v1/uploads/sources?name=synthetic.mp4", content=iter([b"123", b"45"]), headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 413
    assert list((app.state.upload_manager.root / "incoming").iterdir()) == []
    assert client.get("/api/v1/uploads/sources").json() == []
    assert client.post("/api/v1/uploads/sources?name=../x.mp4", content=b"1", headers={"Content-Type": "application/octet-stream"}).status_code == 422
    assert client.post("/api/v1/uploads/sources?name=x.mp4", content=b"", headers={"Content-Type": "application/octet-stream"}).status_code == 422
    assert client.post("/api/v1/uploads/sources?name=x.mp4", content=b"x").status_code == 415


def test_ready_asset_import_checks_registered_hash(upload_client, settings, monkeypatch):
    client, app, _ = upload_client
    asset_id = str(uuid4())
    path = settings.data_root / "assets" / asset_id / "original" / "original.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic-ready-video")
    record = {"original_path": path.relative_to(settings.data_root).as_posix(), "size_bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    monkeypatch.setattr(type(app.state.batch_service), "get_ready_original_asset", lambda self, value: record if value == asset_id else None)
    response = client.post(f"/api/v1/uploads/sources/assets/{asset_id}")
    assert response.status_code == 201
    assert response.json()["sha256"] == record["sha256"]
    path.write_bytes(b"x" * path.stat().st_size)
    assert client.post(f"/api/v1/uploads/sources/assets/{asset_id}").status_code == 409
    assert len(client.get("/api/v1/uploads/sources").json()) == 1


def test_shutdown_stops_only_created_upload_service(upload_client):
    client, app, _ = upload_client
    assert client.get("/api/v1/uploads/status").json()["worker_running"] is True
    app.state.upload_manager.stop()
    assert app.state.upload_manager.service.status()["worker_running"] is False
    assert client.get("/api/v1/uploads/status").status_code == 409


def test_unknown_account_is_404_and_validation_is_422(upload_client):
    client, _, _ = upload_client
    assert client.post("/api/v1/uploads/accounts/" + uuid4().hex + "/check").status_code == 404
    assert client.post("/api/v1/uploads/accounts", json={"platform": "youtube", "name": "unsupported"}).status_code == 422

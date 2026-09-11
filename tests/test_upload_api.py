from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from video_download_control.uploads.backend import SauBackend
from video_download_control.api import create_app
from video_download_control.uploads import api as upload_api
from video_download_control.uploads import schema as upload_schema
from video_download_control.uploads.contracts import BackendResult
from video_download_control.uploads.service import UploadService, default_upload_root


class FakeBackend:
    # The service pins the adapter identity into every attempt receipt and
    # rejects anything outside the owned allowlist, so borrow the real one.
    receipt_identity = staticmethod(SauBackend.receipt_identity)
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
        download_page = client.get("/")
        assert download_page.status_code == 200
        assert '<a class="nav-link" href="/uploads">上传</a>' in download_page.text
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
    assert set(source) == {
        "id", "name", "size", "sha256", "media_present", "media_state",
        "media_deleted_at", "active_reference_count", "can_delete",
    }
    assert source["media_present"] is True
    assert source["media_state"] == "present"
    assert source["active_reference_count"] == 0
    assert source["can_delete"] is True
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


def test_mixed_publish_and_platform_draft_are_created_atomically(upload_client):
    client, _, backend = upload_client
    bilibili = _account(client, "bilibili")
    tencent = _account(client, "tencent")
    source = _source(client)
    payload = {
        "source_id": source["id"],
        "account_ids": [bilibili["id"], tencent["id"]],
        "title": "公共标题",
        "description": "公共简介",
        "tags": ["公共标签"],
        "category_id": 21,
        "copyright": 1,
        "source_credit": "",
        "mode": "publish",
        "target_overrides": [
            {
                "account_id": bilibili["id"],
                "mode": "publish",
                "category_id": 21,
                "copyright": 1,
                "source_credit": "",
                "platform_options": {
                    "dynamic": "动态文案",
                    "no_reprint": True,
                    "close_comments": False,
                    "close_danmu": False,
                },
            },
            {
                "account_id": tencent["id"],
                "mode": "draft",
                "platform_options": {
                    "short_title": "视频号短标题测试",
                    "content_label": "含AI生成内容",
                },
            },
        ],
        "idempotency_key": "mixed-" + uuid4().hex,
    }

    response = client.post("/api/v1/uploads/jobs", json=payload)

    assert response.status_code == 201, response.text
    jobs = {job["platform"]: job for job in response.json()}
    assert set(jobs) == {"bilibili", "tencent"}
    assert jobs["bilibili"]["mode"] == "publish"
    assert jobs["bilibili"]["category_id"] == 21
    assert jobs["tencent"]["mode"] == "draft"
    assert jobs["tencent"]["category_id"] is None
    assert jobs["tencent"]["source_credit"] == ""
    assert all(job["state"] == "draft" for job in jobs.values())
    assert not any(call[0] == "upload" for call in backend.calls)


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
    record = {
        "original_path": path.relative_to(settings.data_root).as_posix(),
        "media_kind": "video",
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
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


def test_cached_upload_service_can_be_explicitly_recovered(upload_client):
    client, app, _ = upload_client
    assert client.get("/api/v1/uploads/status").json()["worker_running"] is True
    service = app.state.upload_manager.service
    service.stop()
    assert service.status()["worker_running"] is False
    response = client.post("/api/v1/uploads/recover")
    assert response.status_code == 200
    assert response.json()["worker_running"] is True
    assert app.state.upload_manager.service is service


def test_job_and_source_history_pages_are_bounded_and_records_are_addressable(upload_client):
    client, _, _ = upload_client
    account = _account(client, "douyin")
    source = _source(client)
    job, _ = _draft(client, account, source)

    jobs = client.get("/api/v1/uploads/jobs/page?limit=1")
    sources = client.get("/api/v1/uploads/sources/page?limit=1")
    assert jobs.status_code == sources.status_code == 200
    assert jobs.json() == {"items": [job], "next_cursor": None}
    current_source = sources.json()["items"][0]
    assert sources.json()["next_cursor"] is None
    assert {key: current_source[key] for key in ("id", "name", "size", "sha256")} == {
        key: source[key] for key in ("id", "name", "size", "sha256")
    }
    assert current_source["active_reference_count"] == 1
    assert current_source["can_delete"] is False
    assert client.get(f"/api/v1/uploads/jobs/{job['id']}").json() == job
    assert client.get(f"/api/v1/uploads/sources/{source['id']}").json() == current_source
    assert client.get("/api/v1/uploads/jobs/page?limit=201").status_code == 422
    assert client.get("/api/v1/uploads/jobs/page?cursor=bad").status_code == 409


def test_database_read_failure_is_a_safe_service_unavailable_response(upload_client, monkeypatch):
    client, app, _ = upload_client
    client.get("/api/v1/uploads/status")
    service = app.state.upload_manager.service

    @contextmanager
    def unavailable_database(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic private database detail")
        yield

    monkeypatch.setattr(service, "_db", unavailable_database)
    response = client.get("/api/v1/uploads/jobs/page?limit=200")
    assert response.status_code == 503
    assert response.json() == {"detail": "upload_database_unavailable"}
    assert "synthetic" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/uploads/recover"),
        ("post", "/api/v1/uploads/sources?name=synthetic.mp4"),
        ("post", "/api/v1/uploads/sources/" + "a" * 32 + "/media"),
    ],
)
def test_database_failure_during_manager_preflight_is_safe_503(
    upload_client, monkeypatch, method, path
):
    client, app, _ = upload_client

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic private database detail")

    target = "recover" if path.endswith("/recover") else "get"
    monkeypatch.setattr(app.state.upload_manager, target, unavailable)
    response = getattr(client, method)(
        path,
        content=b"synthetic-video-only",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "upload_database_unavailable"}
    assert "synthetic private" not in response.text


def test_unknown_account_is_404_and_validation_is_422(upload_client):
    client, _, _ = upload_client
    assert client.post("/api/v1/uploads/accounts/" + uuid4().hex + "/check").status_code == 404
    assert client.post("/api/v1/uploads/accounts", json={"platform": "youtube", "name": "unsupported"}).status_code == 422


def test_copyright_boolean_is_not_coerced_to_original(upload_client):
    client, _, _ = upload_client
    account = _account(client)
    source = _source(client)
    base = {
        "source_id": source["id"], "account_ids": [account["id"]],
        "title": "Strict copyright", "description": "", "tags": ["test"],
        "category_id": 21, "copyright": True, "mode": "publish",
        "idempotency_key": "strict-copyright",
    }
    assert client.post("/api/v1/uploads/jobs", json=base).status_code == 422
    base["copyright"] = 1
    base["target_overrides"] = [{"account_id": account["id"], "copyright": True}]
    assert client.post("/api/v1/uploads/jobs", json=base).status_code == 422


def test_platform_integers_booleans_and_unknown_acknowledgement_are_strict(
    upload_client,
):
    client, _, _ = upload_client
    account = _account(client)
    source = _source(client)
    base = {
        "source_id": source["id"], "account_ids": [account["id"]],
        "title": "Strict API fields", "description": "", "tags": ["test"],
        "category_id": 21, "copyright": 1, "mode": "publish",
        "idempotency_key": "strict-fields",
    }
    for category in (True, 21.0, "21"):
        payload = {**base, "category_id": category}
        assert client.post("/api/v1/uploads/jobs", json=payload).status_code == 422
    for option in (1, "true"):
        payload = {
            **base,
            "target_overrides": [{
                "account_id": account["id"],
                "platform_options": {"no_reprint": option},
            }],
        }
        assert client.post("/api/v1/uploads/jobs", json=payload).status_code == 422
    for acknowledgement in (1, "true"):
        response = client.post(
            "/api/v1/uploads/jobs/" + "a" * 32 + "/retry",
            json={"acknowledge_unknown": acknowledgement},
        )
        assert response.status_code == 422


def test_target_override_inherited_fields_reject_explicit_null(upload_client):
    client, _, _ = upload_client
    account = _account(client)
    source = _source(client)
    base = {
        "source_id": source["id"],
        "account_ids": [account["id"]],
        "title": "Strict nullable API fields",
        "description": "",
        "tags": ["test"],
        "category_id": 21,
        "copyright": 1,
        "mode": "publish",
    }
    cases = [
        {"title": None},
        {"description": None},
        {"tags": None},
        {"category_id": None},
        {"mode": None},
        {"copyright": None},
        {"source_credit": None},
        {"platform_options": {"no_reprint": None}},
        {"platform_options": {"close_comments": None}},
        {"platform_options": {"close_danmu": None}},
    ]
    for index, values in enumerate(cases):
        response = client.post(
            "/api/v1/uploads/jobs",
            json={
                **base,
                "idempotency_key": f"null-field-{index}",
                "target_overrides": [{"account_id": account["id"], **values}],
            },
        )
        assert response.status_code == 422, response.text


def test_jobs_request_identifiers_noop_overrides_and_openapi_are_precise(upload_client):
    client, _, _ = upload_client
    account = _account(client)
    source = _source(client)
    base = {
        "source_id": source["id"],
        "account_ids": [account["id"]],
        "title": "Strict identifiers",
        "description": "",
        "tags": ["test"],
        "category_id": 21,
        "copyright": 1,
        "mode": "publish",
        "idempotency_key": "strict-identifiers",
    }
    assert client.post(
        "/api/v1/uploads/jobs",
        json={**base, "account_ids": ["a" * 65]},
    ).status_code == 422
    assert client.post(
        "/api/v1/uploads/jobs",
        json={**base, "target_overrides": [{"account_id": account["id"]}]},
    ).status_code == 422
    for invalid_tags in (["duplicate", "duplicate"], ["x" * 21]):
        assert client.post(
            "/api/v1/uploads/jobs",
            json={**base, "tags": invalid_tags},
        ).status_code == 422
    invalid_syntax = client.post(
        "/api/v1/uploads/jobs",
        json={**base, "tags": ["has#syntax"]},
    )
    assert invalid_syntax.status_code == 409
    assert invalid_syntax.json() == {"detail": "invalid_tags"}
    assert client.post(
        "/api/v1/uploads/jobs",
        json={
            **base,
            "target_overrides": [{
                "account_id": account["id"],
                "platform_options": {"short_title": "太短了"},
            }],
        },
    ).status_code == 422

    openapi = client.get("/openapi.json").json()
    response_schema = openapi["paths"]["/api/v1/uploads/jobs"]["post"]["responses"][
        "201"
    ]["content"]["application/json"]["schema"]
    assert response_schema["items"]["$ref"].endswith("/UploadJobResponse")
    override_properties = openapi["components"]["schemas"][
        "TargetOverrideRequest"
    ]["properties"]
    for field in (
        "title", "description", "tags", "category_id", "mode", "copyright",
        "source_credit",
    ):
        assert "anyOf" not in override_properties[field]
        assert "default" not in override_properties[field]
    platform_option_properties = openapi["components"]["schemas"][
        "PlatformOptionsRequest"
    ]["properties"]
    for field in ("no_reprint", "close_comments", "close_danmu"):
        assert "anyOf" not in platform_option_properties[field]
        assert "default" not in platform_option_properties[field]
    covers_operation = openapi["paths"]["/api/v1/uploads/covers"]["get"]
    assert covers_operation["deprecated"] is True
    assert covers_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["items"]["$ref"].endswith("/UploadCoverResponse")
    cover_content_types = openapi["paths"][
        "/api/v1/uploads/covers/{asset_id}/content"
    ]["get"]["responses"]["200"]["content"]
    assert set(cover_content_types) == {"image/jpeg", "image/png", "image/webp"}


def test_schema2_legacy_tags_remain_http_idempotent_replay_compatible(settings):
    root = default_upload_root(settings.data_root)
    root.mkdir(parents=True)
    database = root / "uploads.sqlite3"
    account_id, source_id, job_id = "a" * 32, "b" * 32, "c" * 32
    request_id = "request-key"
    old_tags = ["#topic", "旅＃行", "中文，标签", "#"]
    old_payload = [
        source_id, [account_id], "title", "description", old_tags,
        None, "publish", None, "",
    ]
    old_digest = upload_schema._legacy_request_digest(old_payload)
    assert old_digest is not None
    with sqlite3.connect(database) as db:
        db.executescript(upload_schema.SCHEMA_V2_DDL)
        db.execute("INSERT INTO metadata VALUES(2)")
        db.execute(
            "INSERT INTO accounts(id,platform,name,auth_state,code,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (account_id, "douyin", "account", "ready", "account_ready", "created"),
        )
        db.execute(
            "INSERT INTO sources(id,name,suffix,size,sha256,created_at) VALUES(?,?,?,?,?,?)",
            (source_id, "video.mp4", ".mp4", 5, "1" * 64, "created"),
        )
        db.execute(
            "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
            "mode,copyright,source_credit,state,code,created_at,updated_at,retry_of) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, account_id, source_id, "title", "description",
                json.dumps(old_tags, ensure_ascii=False), None, "publish", 1, "",
                "queued", "", "created", "updated", None,
            ),
        )
        db.execute(
            "INSERT INTO requests(id,digest,job_ids) VALUES(?,?,?)",
            (request_id, old_digest, json.dumps([job_id])),
        )

    backend = FakeBackend()
    app = create_app(settings)
    app.state.upload_service_factory = lambda upload_root: UploadService(
        upload_root, backend
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Upload-CSRF"] = client.get(
            "/api/v1/uploads/session"
        ).json()["csrf_token"]
        request = {
            "source_id": source_id,
            "account_ids": [account_id],
            "title": "title",
            "description": "description",
            "tags": old_tags,
            "mode": "publish",
            "idempotency_key": request_id,
        }
        replay = client.post("/api/v1/uploads/jobs", json=request)
        assert replay.status_code == 201, replay.text
        assert replay.json()[0]["id"] == job_id
        assert replay.json()[0]["tags"] == ["topic", "旅井行", "中文、标签", "井"]

        rejected = client.post(
            "/api/v1/uploads/jobs",
            json={**request, "idempotency_key": "new-legacy-tags"},
        )
        assert rejected.status_code == 409
        assert rejected.json() == {"detail": "invalid_tags"}


def test_disconnect_api_keeps_tombstone_and_revokes_queued_confirmation(upload_client):
    client, app, _ = upload_client
    account = _account(client, "douyin")
    source = _source(client)
    job, _ = _draft(client, account, source)
    app.state.upload_manager.service.stop()
    assert client.post(f"/api/v1/uploads/jobs/{job['id']}/confirm").json()["state"] == "queued"

    response = client.post(f"/api/v1/uploads/accounts/{account['id']}/disconnect")

    assert response.status_code == 200
    account_result = response.json()["account"]
    assert account_result == {
        "id": account["id"], "platform": "douyin", "name": "synthetic account",
        "auth_state": "unchecked", "code": "account_disconnected",
        "lifecycle_state": "disconnected", "disconnected_at": account_result["disconnected_at"],
    }
    assert response.json()["revoked_confirmation_count"] == 1
    assert response.json()["canceled_operation_count"] == 0
    assert response.json()["local_login_removed"] is True
    assert client.get("/api/v1/uploads/accounts").json() == [account_result]
    current = client.get(f"/api/v1/uploads/jobs/{job['id']}").json()
    assert current["state"] == "draft"
    assert current["code"] == "account_disconnected_confirmation_revoked"
    assert client.post(f"/api/v1/uploads/jobs/{job['id']}/confirm").status_code == 409


def test_storage_and_media_delete_api_preserve_source_history(upload_client):
    client, app, _ = upload_client
    source = _source(client)
    usage = client.get("/api/v1/uploads/storage")
    assert usage.status_code == 200
    assert usage.json()["registered_source_count"] == 1
    assert usage.json()["present_source_count"] == 1
    assert usage.json()["managed_bytes"] == len(b"synthetic-video-only")

    deleted = client.delete(f"/api/v1/uploads/sources/{source['id']}/media")

    assert deleted.status_code == 200
    assert deleted.json()["media_present"] is False
    assert deleted.json()["media_state"] == "deleted"
    assert deleted.json()["sha256"] == source["sha256"]
    assert not list((app.state.upload_manager.root / "media").iterdir())
    assert client.get("/api/v1/uploads/storage").json()["deleted_source_count"] == 1

    mismatch = client.post(
        f"/api/v1/uploads/sources/{source['id']}/media",
        content=b"different-video-only",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json() == {"detail": "source_restore_mismatch"}
    assert not list((app.state.upload_manager.root / "media").iterdir())

    restored = client.post(
        f"/api/v1/uploads/sources/{source['id']}/media",
        content=b"synthetic-video-only",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert restored.status_code == 200
    assert restored.json()["media_present"] is True
    assert restored.json()["media_state"] == "present"
    assert restored.json()["media_deleted_at"] is None


def test_media_delete_api_rejects_active_draft_without_removing_file(upload_client):
    client, app, _ = upload_client
    source = _source(client)
    _draft(client, _account(client, "douyin"), source)

    response = client.delete(f"/api/v1/uploads/sources/{source['id']}/media")

    assert response.status_code == 409
    assert response.json() == {"detail": "source_in_use"}
    assert len(list((app.state.upload_manager.root / "media").iterdir())) == 1

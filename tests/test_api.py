from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.credentials import CredentialRepository
from video_download_control.database import SCHEMA_VERSION
from video_download_control.domain import ErrorCode, JobStatus, Platform, SourceType
from video_download_control.graph import (
    XAttachmentProbeItem,
    XPostIdentity,
    build_x_attachment_discovery,
)
from video_download_control.normalization import NormalizedURL
from video_download_control.short_links import ShortLinkResolution

INTERNAL_JOB_FIELDS = {
    "credential_profile_id",
    "lease_owner",
    "lease_expires_at",
    "lease_token",
    "heartbeat_at",
}
INTERNAL_GRAPH_JOB_FIELDS = {
    "credential_profile_id",
    "fetch_source_item_id",
    "selector_key",
    "expected_media_key",
    "expected_media_kind",
    "target",
    "fragment",
}


class _AttestedResolver:
    def resolve(
        self, submitted_url: str, *, timeout_seconds: float
    ) -> ShortLinkResolution:
        assert submitted_url == "https://t.co/api-safe"
        assert 0 < timeout_seconds <= 15
        return ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url=(
                    "https://x.com/user/status/700001?signature=never-public"
                ),
                canonical_url="https://x.com/i/status/700001",
                platform=Platform.X,
                source_type=SourceType.X_POST,
                source_id="700001",
            ),
            platform=Platform.X,
            redirect_count=1,
            policy_hosts=("t.co", "x.com"),
        )


class _TikTokAttestedResolver:
    def resolve(
        self, submitted_url: str, *, timeout_seconds: float
    ) -> ShortLinkResolution:
        assert submitted_url == "https://vm.tiktok.com/ZApiSafe"
        assert 0 < timeout_seconds <= 15
        return ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url=(
                    "https://www.tiktok.com/@Example.User/video/7461234567890123456"
                    "?signature=never-public"
                ),
                canonical_url=(
                    "https://www.tiktok.com/@example.user/video/7461234567890123456"
                ),
                platform=Platform.TIKTOK,
                source_type=SourceType.TIKTOK_VIDEO,
                source_id="7461234567890123456",
            ),
            platform=Platform.TIKTOK,
            redirect_count=1,
            policy_hosts=("vm.tiktok.com", "www.tiktok.com"),
        )


def test_health_and_web_page(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        health = client.get("/health")
        page = client.get("/")
        openapi = client.get("/openapi.json")
        remote_docs = client.get("/docs")
        remote_redoc = client.get("/redoc")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "ok",
        "schema_version": SCHEMA_VERSION,
        "worker": "external_status_unknown",
        "detail": None,
    }
    assert page.status_code == 200
    assert openapi.status_code == 200
    assert openapi.json()["info"]["version"] == "0.26.0"
    assert remote_docs.status_code == 404
    assert remote_redoc.status_code == 404
    assert "<title>Open-Flame · 下载</title>" in page.text
    assert '<body class="download-page">' in page.text
    assert '<a class="nav-link" href="/" aria-current="page">' in page.text
    assert '<a class="nav-link" href="/uploads">' in page.text
    assert 'href="/assets/open-flame.css"' in page.text
    assert 'src="/assets/open-flame-shell.js"' in page.text
    assert "下载并整理视频" in page.text
    assert "本次运行状态由应用心跳报告" in page.text
    assert "/api/v1/operations/runtime" in page.text
    assert "TXT/CSV" in page.text
    assert f"Open-Flame {openapi.json()['info']['version']}" in page.text
    assert 'id="asset-links"' in page.text
    assert "可下载成品" in page.text
    assert r".split(/\r?\n/)" in page.text
    assert "async function fetchJson" in page.text
    assert "文本与文件只能选择一种输入方式" in page.text
    assert "if (generation !== pollGeneration) return;" in page.text
    assert "requestId !== pollRequestId" in page.text
    assert "jobActions.replaceChildren();" in page.text
    assert "不代表 Worker 已启动" in page.text
    assert "下载能力" in page.text
    assert "capability.job_kind" in page.text
    assert "/api/v1/capability-snapshot" in page.text
    assert "/api/v1/capability-implementations" not in page.text
    assert "/api/v1/capability-evidence" not in page.text
    assert "/api/v1/capability-decisions" not in page.text
    assert 'id="refresh-capabilities"' in page.text
    assert "candidate: '已实现（静态候选）'" in page.text
    assert "gated: '短链需单独启用解析服务'" in page.text
    assert "deferred: '短链尚未接入'" in page.text
    assert "https://www.tiktok.com/@user/video/..." in page.text
    assert "https://www.instagram.com/reel/..." in page.text
    assert "状态轮询失败" in page.text
    assert "/api/v1/jobs/${encodeURIComponent(action.jobId)}/retry" in page.text
    assert "/api/v1/jobs/${encodeURIComponent(action.jobId)}/cancel" in page.text
    assert "job.status === 'failed'" in page.text
    assert "job.job_kind === 'download'" in page.text
    assert "job.source_type !== 'x_attachment'" in page.text
    assert page.text.count(
        "if (action.generation !== pollGeneration) return;"
    ) == 2
    assert "await pollBatch(action.batchId, action.generation);" in page.text
    assert "const jobActionInFlight = new Set();" in page.text
    assert "const submitGeneration = pollGeneration;" in page.text
    assert page.text.count(
        "if (submitGeneration !== pollGeneration) return;"
    ) == 2
    assert "let queueRequestId = 0;" in page.text
    assert "let circuitRequestId = 0;" in page.text
    assert page.text.count("requestId !== queueRequestId") == 2
    assert page.text.count("requestId !== circuitRequestId") == 2
    assert "circuit.requires_manual_reset" in page.text
    assert "circuit.cooldown_until" in page.text
    assert "自动冷却至" in page.text
    assert "本机工具链" in page.text
    assert "显示控制端启动时缓存的 yt-dlp、FFmpeg 与 ffprobe 检查" in page.text
    assert "工具链检查与本次运行状态分别展示" in page.text
    assert "外部 Worker 状态保持未知" in page.text
    assert "无法据此判断独立本机 Worker 是否在线" in page.text
    assert "/api/v1/operations/tools" in page.text
    assert "refreshTools.addEventListener('click', loadToolchain)" in page.text
    assert "运行日志" in page.text
    assert "日志是辅助线索" in page.text
    assert "手动刷新" in page.text
    assert "/api/v1/operations/logs?limit=100" in page.text
    assert "refreshLogs.addEventListener('click', loadRuntimeLogs)" in page.text
    assert "events.slice().reverse()" in page.text
    assert r".join('\n')" in page.text
    assert "cdn.jsdelivr.net" not in page.text
    assert "fonts.googleapis.com" not in page.text


def test_download_capability_matrix_is_public_and_candidate_only(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/download-capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert {item["platform"] for item in payload} == {
        "x",
        "youtube",
        "bilibili",
        "douyin",
        "tiktok",
        "instagram",
    }
    assert all(item["adapter"] == "yt_dlp" for item in payload)
    assert all(item["status"] == "candidate" for item in payload)
    assert all(item["adapter_version"] is None for item in payload)
    assert all(item["environment"] is None for item in payload)
    by_platform_and_type = {
        (item["platform"], item["source_type"]): item["short_link_status"]
        for item in payload
    }
    assert by_platform_and_type[("youtube", "youtube_video")] == "supported"
    assert by_platform_and_type[("bilibili", "bilibili_video")] == "gated"
    assert by_platform_and_type[("tiktok", "tiktok_video")] == "gated"
    assert all(
        set(item)
        == {
            "platform",
            "source_type",
            "job_kind",
            "adapter",
            "status",
            "authentication",
            "adapter_version",
            "environment",
            "short_link_status",
        }
        for item in payload
    )


def test_liveness_and_readiness_detect_broken_schema(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        with app.state.database.connect() as connection:
            connection.execute("DROP TABLE batches")
        health = client.get("/health")
        readiness = client.get("/health/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert "missing_tables:batches" in health.json()["detail"]
    assert readiness.status_code == 503


def test_health_coalesces_deep_readiness_until_schema_changes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    original = app.state.database.readiness
    calls = 0

    def counted_readiness():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(app.state.database, "readiness", counted_readiness)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert calls == 1
        with app.state.database.connect() as connection:
            connection.execute("DROP TABLE batches")
        degraded = client.get("/health")

    assert degraded.status_code == 200
    assert degraded.json()["database"] == "error"
    assert calls == 2


def test_health_fails_closed_if_schema_changes_during_deep_audit(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    cookies = iter((10, 11))
    monkeypatch.setattr(
        app.state.database, "_schema_cookie", lambda: next(cookies)
    )
    monkeypatch.setattr(
        app.state.database, "readiness", lambda: (True, "ok")
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert (
        response.json()["detail"]["detail"]
        == "schema_changed_during_readiness"
    )


def test_health_fails_closed_if_final_schema_cookie_is_unavailable(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    calls = 0

    def schema_cookie() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 10
        raise sqlite3.OperationalError(r"C:\Users\PRIVATE-CANARY")

    monkeypatch.setattr(app.state.database, "_schema_cookie", schema_cookie)
    monkeypatch.setattr(
        app.state.database, "readiness", lambda: (True, "ok")
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["detail"] == "schema_version_unavailable"
    assert "PRIVATE-CANARY" not in response.text


def test_create_and_get_batch(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={
                "name": "API 测试",
                "inputs": ["https://youtu.be/dQw4w9WgXcQ"],
            },
        )
        created_payload = created.json()
        claim_time = datetime.fromisoformat(created_payload["created_at"]) + timedelta(
            seconds=1
        )
        lease = app.state.worker_repository.claim_next(
            worker_id="private-worker-name",
            adapter="fake",
            adapter_version="test",
            now=claim_time,
        )
        assert lease is not None
        fetched = client.get(f"/api/v1/batches/{created_payload['id']}")

    assert created.status_code == 201
    assert created_payload["queued_count"] == 1
    assert fetched.status_code == 200
    assert INTERNAL_JOB_FIELDS.isdisjoint(created_payload["jobs"][0])
    assert INTERNAL_JOB_FIELDS.isdisjoint(fetched.json()["jobs"][0])


def test_missing_batch_and_invalid_payload(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        missing = client.get("/api/v1/batches/not-found")
        invalid = client.post("/api/v1/batches", json={"inputs": []})

    assert missing.status_code == 404
    assert invalid.status_code == 422


def test_api_queues_attested_short_link_without_exposing_redirect_material(
    settings: Settings,
) -> None:
    enabled_settings = replace(
        settings,
        short_link_resolution_enabled=True,
        short_link_transport_socket=settings.data_root / "short-link.sock",
        short_link_attestation_key_file=settings.data_root / "short-link.key",
    )
    with TestClient(
        create_app(enabled_settings, short_link_resolver=_AttestedResolver())
    ) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://t.co/api-safe"]},
        )

    assert created.status_code == 201
    payload = created.json()
    assert payload["queued_count"] == 1
    assert payload["inputs"][0]["submitted_url"] == "https://t.co/api-safe"
    assert payload["inputs"][0]["canonical_url"] == ("https://x.com/i/status/700001")
    assert "signature" not in created.text
    assert "never-public" not in created.text


def test_api_rejects_injected_short_link_resolver_without_explicit_gate(
    settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="explicit feature enablement"):
        create_app(settings, short_link_resolver=_AttestedResolver())


def test_api_keeps_tiktok_short_link_resolution_default_off(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://vt.tiktok.com/ZShortApi"]},
        )

    assert created.status_code == 201
    payload = created.json()
    assert payload["queued_count"] == 0
    assert payload["failed_count"] == 1
    assert payload["inputs"][0]["error_code"] == (
        "short_link_resolution_required"
    )


def test_api_queues_attested_tiktok_short_link_as_tiktok_video(
    settings: Settings,
) -> None:
    enabled_settings = replace(
        settings,
        short_link_resolution_enabled=True,
        short_link_transport_socket=settings.data_root / "short-link.sock",
        short_link_attestation_key_file=settings.data_root / "short-link.key",
    )
    with TestClient(
        create_app(
            enabled_settings,
            short_link_resolver=_TikTokAttestedResolver(),
        )
    ) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://vm.tiktok.com/ZApiSafe?tracking=drop"]},
        )

    assert created.status_code == 201
    payload = created.json()
    assert payload["queued_count"] == 1
    assert payload["inputs"][0]["submitted_url"] == (
        "https://vm.tiktok.com/ZApiSafe?tracking=drop"
    )
    assert payload["inputs"][0]["canonical_url"] == (
        "https://www.tiktok.com/@example.user/video/7461234567890123456"
    )
    assert payload["jobs"][0]["platform"] == "tiktok"
    assert payload["jobs"][0]["source_type"] == "tiktok_video"
    assert "signature" not in created.text
    assert "never-public" not in created.text


def test_batch_api_rejects_admin_only_credential_profile_id(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        rejected = client.post(
            "/api/v1/batches",
            json={
                "inputs": ["https://youtu.be/credential-api"],
                "credential_profile_id": "youtube-profile-1",
            },
        )
        with app.state.database.connect() as connection:
            job_count = connection.execute(
                "SELECT COUNT(*) AS count FROM download_jobs"
            ).fetchone()["count"]

    assert rejected.status_code == 422
    assert job_count == 0


def test_cancel_queued_job_updates_batch_aggregate(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/cancel-api1"]},
        ).json()
        job_id = created["jobs"][0]["id"]
        canceled = client.post(f"/api/v1/jobs/{job_id}/cancel")
        refreshed = client.get(f"/api/v1/batches/{created['id']}").json()
        missing = client.post("/api/v1/jobs/not-found/cancel")

    assert canceled.status_code == 200
    assert canceled.json() == {
        "job_id": job_id,
        "status": "canceled",
        "cancel_requested": True,
    }
    assert refreshed["status"] == "canceled"
    assert refreshed["canceled_count"] == 1
    assert refreshed["inputs"][0]["status"] == "canceled"
    assert missing.status_code == 404


def test_explicit_job_retry_returns_narrow_new_generation_response(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/retry-api"]},
        ).json()
        job_id = created["jobs"][0]["id"]
        claim_time = datetime.fromisoformat(created["created_at"]) + timedelta(
            seconds=1
        )
        lease = app.state.worker_repository.claim_next(
            worker_id="retry-api-worker",
            adapter="fake",
            adapter_version="1",
            now=claim_time,
        )
        assert lease is not None
        assert app.state.worker_repository.finish_failure(
            lease,
            error_code=ErrorCode.NETWORK_ERROR,
            diagnostic="private terminal diagnostic",
            now=claim_time,
        ) is JobStatus.FAILED

        retried = client.post(f"/api/v1/jobs/{job_id}/retry")
        refreshed = client.get(f"/api/v1/batches/{created['id']}")

    assert retried.status_code == 200
    assert retried.json() == {
        "job_id": job_id,
        "status": "queued",
        "run_generation": 2,
    }
    assert "private terminal diagnostic" not in retried.text
    payload = refreshed.json()
    assert payload["status"] == "queued"
    assert payload["jobs"][0]["run_generation"] == 2
    assert payload["jobs"][0]["attempt_count"] == 1
    assert payload["inputs"][0]["active_run_generation"] == 2


def test_explicit_job_retry_maps_missing_and_conflicting_state(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/retry-conflict-api"]},
        ).json()
        queued = client.post(
            f"/api/v1/jobs/{created['jobs'][0]['id']}/retry"
        )
        missing = client.post("/api/v1/jobs/not-found/retry")

    assert queued.status_code == 409
    assert missing.status_code == 404


def test_cancel_input_updates_batch_aggregate_and_missing_is_404(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/cancel-input-api"]},
        ).json()
        input_id = created["inputs"][0]["id"]
        canceled = client.post(f"/api/v1/inputs/{input_id}/cancel")
        refreshed = client.get(f"/api/v1/batches/{created['id']}").json()
        missing = client.post("/api/v1/inputs/not-found/cancel")

    assert canceled.status_code == 200
    assert canceled.json() == {
        "input_record_id": input_id,
        "status": "canceled",
        "cancel_requested": True,
    }
    assert refreshed["status"] == "canceled"
    assert refreshed["inputs"][0]["status"] == "canceled"
    assert missing.status_code == 404


def test_rediscover_rejects_missing_non_graph_and_nonterminal_inputs(
    settings: Settings,
) -> None:
    graph_settings = replace(settings, x_graph_v2_enabled=True)
    with TestClient(create_app(graph_settings)) as client:
        flat = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/rediscover-flat-api"]},
        ).json()
        graph = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://x.com/example/status/910001"]},
        ).json()
        missing = client.post("/api/v1/inputs/not-found/rediscover")
        non_graph = client.post(f"/api/v1/inputs/{flat['inputs'][0]['id']}/rediscover")
        nonterminal = client.post(
            f"/api/v1/inputs/{graph['inputs'][0]['id']}/rediscover"
        )

    assert missing.status_code == 404
    assert non_graph.status_code == 409
    assert nonterminal.status_code == 409


def test_terminal_graph_rediscover_returns_only_public_control_fields(
    settings: Settings,
) -> None:
    source_id = "910002"
    fragment_marker = "private-fragment-marker"
    selector_marker = "private-selector-marker"
    target_marker = "private-target-marker"
    credential_marker = "private-credential-ref"
    graph_settings = replace(settings, x_graph_v2_enabled=True)
    app = create_app(graph_settings)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": [f"https://x.com/example/status/{source_id}"]},
        ).json()
        input_id = created["inputs"][0]["id"]
        parent_job_id = created["jobs"][0]["id"]
        claim_time = datetime.fromisoformat(created["created_at"]) + timedelta(
            seconds=1
        )
        credential_repository = CredentialRepository(
            app.state.database,
            clock=lambda: claim_time,
        )
        credential = credential_repository.register(
            platform=Platform.X,
            name="private graph API profile",
            secret_ref=credential_marker,
        )
        credential_repository.assign(
            profile_id=credential["id"],
            job_ids=[parent_job_id],
        )
        lease = app.state.worker_repository.claim_next(
            worker_id="graph-api-worker",
            adapter="graph-fake",
            adapter_version="1",
            now=claim_time,
            supports_exact_selector=True,
        )
        assert lease is not None
        probe_items = (
            XAttachmentProbeItem(
                stable_key=fragment_marker,
                selector_key=selector_marker,
                expected_media_key=target_marker,
                media_kind="video",
            ),
        )
        discovery = build_x_attachment_discovery(
            parent=XPostIdentity(source_id),
            parent_canonical_url=lease.canonical_url,
            probe_items=probe_items,
        )
        app.state.worker_repository.commit_discovery(
            lease,
            probe_items=probe_items,
            discovery_snapshot_hash=discovery.snapshot_hash,
            sanitized_source={"title": "safe parent"},
            now=claim_time + timedelta(seconds=1),
        )

        canceled = client.post(f"/api/v1/inputs/{input_id}/cancel")
        rediscovered = client.post(f"/api/v1/inputs/{input_id}/rediscover")
        fetched = client.get(f"/api/v1/batches/{created['id']}")

    assert canceled.status_code == 200
    assert set(canceled.json()) == {
        "input_record_id",
        "status",
        "cancel_requested",
    }
    assert rediscovered.status_code == 200
    assert rediscovered.json() == {
        "input_record_id": input_id,
        "status": "queued",
        "run_generation": 2,
    }
    assert fetched.status_code == 200
    assert all(
        INTERNAL_GRAPH_JOB_FIELDS.isdisjoint(job) for job in fetched.json()["jobs"]
    )
    public_payload = canceled.text + rediscovered.text + fetched.text
    for internal_value in (
        fragment_marker,
        selector_marker,
        target_marker,
        credential_marker,
        credential["id"],
        "#vdc-media=",
    ):
        assert internal_value not in public_payload


def test_openapi_defines_nested_batch_contract(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert "InputRecordResponse" in schemas
    assert "DownloadJobResponse" in schemas
    assert schemas["BatchResponse"]["properties"]["inputs"]["items"] == {
        "$ref": "#/components/schemas/InputRecordResponse"
    }
    assert schemas["BatchResponse"]["properties"]["jobs"]["items"] == {
        "$ref": "#/components/schemas/DownloadJobResponse"
    }
    assert INTERNAL_JOB_FIELDS.isdisjoint(schemas["DownloadJobResponse"]["properties"])
    assert schemas["InputCancelResponse"]["additionalProperties"] is False
    assert schemas["InputRediscoverResponse"]["additionalProperties"] is False
    assert schemas["JobRetryResponse"]["additionalProperties"] is False
    assert INTERNAL_GRAPH_JOB_FIELDS.isdisjoint(
        schemas["InputCancelResponse"]["properties"]
    )
    assert INTERNAL_GRAPH_JOB_FIELDS.isdisjoint(
        schemas["InputRediscoverResponse"]["properties"]
    )

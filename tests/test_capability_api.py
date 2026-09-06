from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import video_download_control.api as api_module
from video_download_control.api import create_app
from video_download_control.build_identity import (
    ProductBuildDriftError,
    ProductBuildUnavailableError,
    current_product_identity,
)
from video_download_control.capability_evidence import (
    capability_identity_key,
    evidence_sha256_from_payload,
    implementation_id,
)
from video_download_control.config import Settings


LEGACY_CAPABILITY_FIELDS = {
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
IMPLEMENTATION_FIELDS = {
    "implementation_id",
    "platform",
    "source_type",
    "job_kind",
    "adapter",
    "implementation_status",
    "authentication",
    "short_link_status",
}
EVIDENCE_FIELDS = {
    "evidence_id",
    "identity_key",
    "implementation_id",
    "platform",
    "source_type",
    "job_kind",
    "adapter",
    "downloader_version",
    "environment",
    "product_version",
    "evidence_kind",
    "policy_version",
    "assessment",
    "positive_samples",
    "negative_samples",
    "complete_runs",
    "recent_positive_rates",
    "recent_negative_rates",
    "evaluated_at",
    "imported_at",
}
DECISION_FIELDS = {
    "decision_id",
    "identity_key",
    "evidence_id",
    "platform",
    "source_type",
    "job_kind",
    "adapter",
    "downloader_version",
    "environment",
    "product_version",
    "action",
    "state",
    "revision",
    "reason_code",
    "decided_at",
}
FORBIDDEN_RAW_FIELDS = {
    "manifest_sha256",
    "results_sha256",
    "bundle_sha256",
    "evidence_sha256",
    "manifest_path",
    "results_path",
    "sample_id",
    "run_id",
    "submitted_url",
    "canonical_url",
    "positive_rates_json",
    "negative_rates_json",
}
SNAPSHOT_FIELDS = {
    "current_product_identity",
    "implementations",
    "evidence",
    "decisions",
    "evidence_total",
    "decision_total",
    "evidence_truncated",
    "decision_truncated",
}


def _seed_approved_capability(app) -> dict[str, str]:
    identity = {
        "platform": "youtube",
        "source_type": "youtube_video",
        "job_kind": "download",
        "adapter": "yt_dlp",
        "downloader_version": "2026.08.19",
        "environment": "windows-x64-direct-no-cookie",
        "product_version": current_product_identity(),
    }
    identity_key = capability_identity_key(**identity)
    evidence_id = "11111111-1111-4111-8111-111111111111"
    decision_id = "22222222-2222-4222-8222-222222222222"
    evidence_payload = {
        **identity,
        "identity_key": identity_key,
        "evidence_kind": "stage0_csv_v3",
        "policy_version": "stage0-v3",
        "bundle_sha256": "c" * 64,
        "positive_sample_count": 10,
        "negative_sample_count": 1,
        "complete_run_count": 3,
        "positive_rates": [0.9, 0.95, 1.0],
        "negative_rates": [1.0, 1.0, 1.0],
        "verdict": "verified",
        "evaluated_at": "2026-09-03T00:00:00.000Z",
    }
    private_hashes = {
        "manifest_sha256": "a" * 64,
        "results_sha256": "b" * 64,
        "bundle_sha256": "c" * 64,
        "evidence_sha256": evidence_sha256_from_payload(evidence_payload),
    }
    with app.state.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO capability_evidence(
                evidence_id, identity_key, platform, source_type, job_kind,
                adapter, downloader_version, environment, product_version,
                evidence_kind, policy_version, manifest_sha256, results_sha256,
                bundle_sha256, evidence_sha256, positive_sample_count,
                negative_sample_count, complete_run_count, positive_rates_json,
                negative_rates_json, verdict, evaluated_at, imported_at
            ) VALUES (
                :evidence_id, :identity_key, :platform, :source_type, :job_kind,
                :adapter, :downloader_version, :environment, :product_version,
                'stage0_csv_v3', 'stage0-v3', :manifest_sha256,
                :results_sha256, :bundle_sha256, :evidence_sha256, 10, 1, 3,
                '[0.9,0.95,1.0]', '[1.0,1.0,1.0]', 'verified',
                '2026-09-03T00:00:00.000Z', '2026-09-03T00:01:00.000Z'
            )
            """,
            {
                **identity,
                **private_hashes,
                "identity_key": identity_key,
                "evidence_id": evidence_id,
            },
        )
        connection.execute(
            """
            INSERT INTO capability_decisions(
                decision_id, identity_key, platform, source_type, job_kind,
                adapter, downloader_version, environment, product_version,
                evidence_id, action, status, supersedes_decision_id, revision,
                reason_code, decided_at
            ) VALUES (
                :decision_id, :identity_key, :platform, :source_type, :job_kind,
                :adapter, :downloader_version, :environment, :product_version,
                :evidence_id, 'approve', 'verified', NULL, 1,
                'stage0-reviewed', '2026-09-03T00:02:00.000Z'
            )
            """,
            {
                **identity,
                "identity_key": identity_key,
                "evidence_id": evidence_id,
                "decision_id": decision_id,
            },
        )
    return {
        **identity,
        **private_hashes,
        "identity_key": identity_key,
        "implementation_id": implementation_id(
            platform=identity["platform"],
            source_type=identity["source_type"],
            job_kind=identity["job_kind"],
            adapter=identity["adapter"],
        ),
        "evidence_id": evidence_id,
        "decision_id": decision_id,
    }


def _seed_unapproved_evidence(
    app,
    seeded: dict[str, str],
    *,
    evidence_id: str,
    marker: str,
    imported_at: str,
) -> None:
    identity = {
        name: seeded[name]
        for name in (
            "platform",
            "source_type",
            "job_kind",
            "adapter",
            "downloader_version",
            "environment",
            "product_version",
        )
    }
    evidence_payload = {
        **identity,
        "identity_key": seeded["identity_key"],
        "evidence_kind": "stage0_csv_v3",
        "policy_version": "stage0-v3",
        "bundle_sha256": marker * 64,
        "positive_sample_count": 9,
        "negative_sample_count": 1,
        "complete_run_count": 3,
        "positive_rates": [1.0, 1.0, 1.0],
        "negative_rates": [1.0, 1.0, 1.0],
        "verdict": "candidate",
        "evaluated_at": imported_at,
    }
    with app.state.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO capability_evidence(
                evidence_id, identity_key, platform, source_type, job_kind,
                adapter, downloader_version, environment, product_version,
                evidence_kind, policy_version, manifest_sha256, results_sha256,
                bundle_sha256, evidence_sha256, positive_sample_count,
                negative_sample_count, complete_run_count, positive_rates_json,
                negative_rates_json, verdict, evaluated_at, imported_at
            ) VALUES (
                :evidence_id, :identity_key, :platform, :source_type, :job_kind,
                :adapter, :downloader_version, :environment, :product_version,
                'stage0_csv_v3', 'stage0-v3', :manifest_sha256,
                :results_sha256, :bundle_sha256, :evidence_sha256, 9, 1, 3,
                '[1.0,1.0,1.0]', '[1.0,1.0,1.0]', 'candidate',
                :evaluated_at, :imported_at
            )
            """,
            {
                **identity,
                "identity_key": seeded["identity_key"],
                "evidence_id": evidence_id,
                "manifest_sha256": marker * 64,
                "results_sha256": marker * 64,
                "bundle_sha256": marker * 64,
                "evidence_sha256": evidence_sha256_from_payload(
                    evidence_payload
                ),
                "evaluated_at": imported_at,
                "imported_at": imported_at,
            },
        )


def test_capability_static_layers_and_empty_governance_store(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        legacy = client.get("/api/v1/download-capabilities")
        implementations = client.get("/api/v1/capability-implementations")
        evidence = client.get("/api/v1/capability-evidence")
        decisions = client.get("/api/v1/capability-decisions")
        snapshot = client.get("/api/v1/capability-snapshot")

    assert legacy.status_code == 200
    assert legacy.json()
    assert all(set(item) == LEGACY_CAPABILITY_FIELDS for item in legacy.json())
    assert all(item["status"] == "candidate" for item in legacy.json())
    assert all(item["adapter_version"] is None for item in legacy.json())
    assert all(item["environment"] is None for item in legacy.json())

    assert implementations.status_code == 200
    assert implementations.json()
    assert all(
        set(item) == IMPLEMENTATION_FIELDS for item in implementations.json()
    )
    assert all(
        item["implementation_status"] == "candidate"
        for item in implementations.json()
    )
    assert evidence.status_code == 200
    assert evidence.json() == []
    assert decisions.status_code == 200
    assert decisions.json() == []
    assert snapshot.status_code == 200
    assert set(snapshot.json()) == SNAPSHOT_FIELDS
    assert snapshot.json()["current_product_identity"] == current_product_identity()
    assert snapshot.json()["implementations"] == implementations.json()
    assert snapshot.json()["evidence"] == []
    assert snapshot.json()["decisions"] == []
    assert snapshot.json()["evidence_total"] == 0
    assert snapshot.json()["decision_total"] == 0
    assert snapshot.json()["evidence_truncated"] is False
    assert snapshot.json()["decision_truncated"] is False


def test_capability_evidence_and_decision_dtos_are_strictly_separated(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        seeded = _seed_approved_capability(app)
        implementations = client.get("/api/v1/capability-implementations")
        evidence_response = client.get("/api/v1/capability-evidence")
        decisions_response = client.get("/api/v1/capability-decisions")
        snapshot_response = client.get("/api/v1/capability-snapshot")

    assert implementations.status_code == 200
    matching_implementation = next(
        item
        for item in implementations.json()
        if item["implementation_id"] == seeded["implementation_id"]
    )
    assert set(matching_implementation) == IMPLEMENTATION_FIELDS
    assert "downloader_version" not in matching_implementation
    assert "environment" not in matching_implementation
    assert "product_version" not in matching_implementation

    assert evidence_response.status_code == 200
    assert len(evidence_response.json()) == 1
    evidence = evidence_response.json()[0]
    assert set(evidence) == EVIDENCE_FIELDS
    assert evidence["identity_key"] == seeded["identity_key"]
    assert evidence["implementation_id"] == seeded["implementation_id"]
    assert evidence["assessment"] == "qualified"
    assert evidence["positive_samples"] == 10
    assert evidence["negative_samples"] == 1
    assert evidence["complete_runs"] == 3
    assert {"action", "state", "revision", "reason_code"}.isdisjoint(evidence)

    assert decisions_response.status_code == 200
    assert len(decisions_response.json()) == 1
    decision = decisions_response.json()[0]
    assert set(decision) == DECISION_FIELDS
    assert decision["decision_id"] == seeded["decision_id"]
    assert decision["identity_key"] == seeded["identity_key"]
    assert decision["evidence_id"] == seeded["evidence_id"]
    assert decision["action"] == "approve"
    assert decision["state"] == "approved"
    assert decision["revision"] == 1
    assert {
        "implementation_status",
        "authentication",
        "short_link_status",
        "assessment",
        "positive_samples",
        "negative_samples",
        "complete_runs",
    }.isdisjoint(decision)

    for payload in (evidence, decision):
        assert FORBIDDEN_RAW_FIELDS.isdisjoint(payload)
    combined_public_text = evidence_response.text + decisions_response.text
    for private_hash_name in (
        "manifest_sha256",
        "results_sha256",
        "bundle_sha256",
        "evidence_sha256",
    ):
        assert seeded[private_hash_name] not in combined_public_text
    for private_marker in (
        "https://private.invalid/source",
        r"C:\private\stage0\manifest.csv",
        "private-sample-id",
        "private-run-id",
    ):
        assert private_marker not in combined_public_text
    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.json()
    assert set(snapshot) == SNAPSHOT_FIELDS
    assert snapshot["current_product_identity"] == current_product_identity()
    assert snapshot["evidence"] == [evidence]
    assert snapshot["decisions"] == [decision]
    assert snapshot["evidence_total"] == 1
    assert snapshot["decision_total"] == 1
    assert snapshot["evidence_truncated"] is False
    assert snapshot["decision_truncated"] is False


def test_capability_governance_endpoints_fail_closed_when_readiness_breaks(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.database.connect() as connection:
            connection.execute("DROP TRIGGER trg_capability_evidence_no_update")

        evidence = client.get("/api/v1/capability-evidence")
        decisions = client.get("/api/v1/capability-decisions")
        snapshot = client.get("/api/v1/capability-snapshot")
        legacy = client.get("/api/v1/download-capabilities")
        implementations = client.get("/api/v1/capability-implementations")

    assert evidence.status_code == 503
    assert evidence.json() == {"detail": "capability_store_unavailable"}
    assert decisions.status_code == 503
    assert decisions.json() == {"detail": "capability_store_unavailable"}
    assert snapshot.status_code == 503
    assert snapshot.json() == {"detail": "capability_store_unavailable"}
    assert legacy.status_code == 200
    assert implementations.status_code == 200


@pytest.mark.parametrize(
    ("error", "detail"),
    (
        (
            ProductBuildDriftError(r"C:\Users\PRIVATE-DRIFT-CANARY"),
            "product_build_drift",
        ),
        (
            ProductBuildUnavailableError(
                r"C:\Users\PRIVATE-UNAVAILABLE-CANARY"
            ),
            "product_build_unavailable",
        ),
    ),
)
def test_snapshot_sanitizes_product_identity_failures(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    error: RuntimeError,
    detail: str,
) -> None:
    def fail_identity() -> str:
        raise error

    monkeypatch.setattr(api_module, "current_product_identity", fail_identity)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/capability-snapshot")

    assert response.status_code == 503
    assert response.json() == {"detail": detail}
    assert "PRIVATE" not in response.text


def test_home_page_exposes_three_capability_layers_with_safe_dom_updates(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        page = client.get("/")

    assert page.status_code == 200
    assert "迭代 0.24.4" in page.text
    assert "本次启动配置的默认平台 Cookie" in page.text
    assert "双槽下载" in page.text
    assert "本次运行状态由应用心跳报告" in page.text
    assert "/api/v1/operations/runtime" in page.text
    assert "实现、精确构建/环境证据与人工决定分别展示" in page.text
    assert "/api/v1/capability-snapshot" in page.text
    for endpoint in (
        "/api/v1/capability-implementations",
        "/api/v1/capability-evidence",
        "/api/v1/capability-decisions",
    ):
        assert endpoint not in page.text
    assert 'id="refresh-capabilities"' in page.text
    assert "current.evidence_id === item.evidence_id" not in page.text
    assert "identityDecision?.evidence_id === item.evidence_id" in page.text
    assert "当前决定未知（窗口外）" in page.text
    assert "历史构建（不适用于当前运行）" in page.text
    operations_body = page.text.split(
        "async function loadOperations()", maxsplit=1
    )[1].split("async function refreshOperations()", maxsplit=1)[0]
    assert "loadCapabilityState" not in operations_body
    assert "capabilityStatus.textContent" in page.text
    assert "line.textContent" in page.text
    assert "detail.textContent" in page.text
    assert ".innerHTML" not in page.text


def test_snapshot_includes_older_evidence_referenced_by_current_decision(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        seeded = _seed_approved_capability(app)
        _seed_unapproved_evidence(
            app,
            seeded,
            evidence_id="33333333-3333-4333-8333-333333333333",
            marker="d",
            imported_at="2026-09-03T00:03:00.000Z",
        )
        _seed_unapproved_evidence(
            app,
            seeded,
            evidence_id="44444444-4444-4444-8444-444444444444",
            marker="e",
            imported_at="2026-09-03T00:04:00.000Z",
        )

        response = client.get("/api/v1/capability-snapshot?limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence_total"] == 3
    assert payload["decision_total"] == 1
    assert payload["evidence_truncated"] is True
    assert payload["decision_truncated"] is False
    assert {item["evidence_id"] for item in payload["evidence"]} == {
        seeded["evidence_id"],
        "44444444-4444-4444-8444-444444444444",
    }
    assert payload["decisions"][0]["evidence_id"] == seeded["evidence_id"]


def test_snapshot_performs_one_readiness_check(
    settings: Settings,
    monkeypatch,
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
        response = client.get("/api/v1/capability-snapshot")

    assert response.status_code == 200
    assert calls == 1


def test_capability_routes_do_not_create_rejected_log_events(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        seeded = _seed_approved_capability(app)
        responses = [
            client.get("/api/v1/download-capabilities"),
            client.get("/api/v1/capability-implementations"),
            client.get("/api/v1/capability-evidence"),
            client.get("/api/v1/capability-decisions"),
            client.get("/api/v1/capability-snapshot"),
            client.get(
                f"/api/v1/capability-decisions/{seeded['identity_key']}/history"
            ),
        ]
        logs = client.get("/api/v1/operations/logs")

    assert all(response.status_code == 200 for response in responses)
    assert logs.status_code == 200
    assert logs.json()["rejected_events"] == 0

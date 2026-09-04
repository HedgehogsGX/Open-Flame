from __future__ import annotations

import csv
import io
from dataclasses import replace
from pathlib import Path

import pytest

import video_download_control.capability_evidence as capability_evidence_module
from video_download_control.build_identity import (
    ProductBuildDriftError,
    ProductBuildUnavailableError,
    current_product_identity,
)
from video_download_control.capabilities import (
    CapabilityStatus,
    PlatformCapabilityRegistry,
)
from video_download_control.capability_evidence import (
    CapabilityEvidenceRepository,
    CapabilityGovernanceError,
    capability_identity_key,
)
from video_download_control.database import Database


MANIFEST_FIELDS = (
    "sample_id",
    "platform",
    "source_type",
    "job_kind",
    "url",
    "expected_outcome",
    "expected_output_count",
    "requires_cookie",
    "region",
)
RESULT_FIELDS = (
    "run_id",
    "sample_id",
    "job_kind",
    "observed_outcome",
    "observed_output_count",
    "completed_at",
    "adapter",
    "downloader_version",
    "environment",
    "product_version",
)
ENVIRONMENT = "windows-x64-direct-no-cookie"


def _csv_bytes(
    fields: tuple[str, ...], rows: list[dict[str, object]]
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _stage0_rows(
    *,
    positive_count: int = 10,
    environment: str = ENVIRONMENT,
    product_version: str | None = None,
    label: str = "base",
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    resolved_product_version = (
        current_product_identity() if product_version is None else product_version
    )
    samples = [
        {
            "sample_id": f"{label}-positive-{index}",
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "url": f"https://www.youtube.com/watch?v={label}-video-{index}",
            "expected_outcome": "ready",
            "expected_output_count": 1,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
        for index in range(positive_count)
    ]
    samples.append(
        {
            "sample_id": f"{label}-negative",
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "url": f"https://www.youtube.com/watch?v={label}-unavailable",
            "expected_outcome": "content_unavailable",
            "expected_output_count": 0,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
    )
    results: list[dict[str, object]] = []
    for run in range(1, 4):
        for sample in samples:
            results.append(
                {
                    "run_id": f"{label}-run-{run}",
                    "sample_id": sample["sample_id"],
                    "job_kind": sample["job_kind"],
                    "observed_outcome": sample["expected_outcome"],
                    "observed_output_count": sample["expected_output_count"],
                    "completed_at": f"2026-09-0{run}T10:00:00+09:30",
                    "adapter": "yt_dlp",
                    "downloader_version": "2026.08.19",
                    "environment": environment,
                    "product_version": resolved_product_version,
                }
            )
    return samples, results


def _stage0_bundle(
    *,
    positive_count: int = 10,
    environment: str = ENVIRONMENT,
    product_version: str | None = None,
    label: str = "base",
    reverse_rows: bool = False,
) -> tuple[bytes, bytes]:
    samples, results = _stage0_rows(
        positive_count=positive_count,
        environment=environment,
        product_version=product_version,
        label=label,
    )
    if reverse_rows:
        samples.reverse()
        results.reverse()
    return (
        _csv_bytes(MANIFEST_FIELDS, samples),
        _csv_bytes(RESULT_FIELDS, results),
    )


def _tiktok_alias_bundle(*, host: str, handle: str) -> tuple[bytes, bytes]:
    product_identity = current_product_identity()
    samples = [
        {
            "sample_id": f"tiktok-positive-{index}",
            "platform": "tiktok",
            "source_type": "tiktok_video",
            "job_kind": "download",
            "url": (
                f"https://{host}/@{handle}/video/"
                f"712345678901234{index:04d}"
            ),
            "expected_outcome": "ready",
            "expected_output_count": 1,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
        for index in range(10)
    ]
    samples.append(
        {
            "sample_id": "tiktok-negative",
            "platform": "tiktok",
            "source_type": "tiktok_video",
            "job_kind": "download",
            "url": f"https://{host}/@{handle}/video/7999999999999999999",
            "expected_outcome": "content_unavailable",
            "expected_output_count": 0,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
    )
    results: list[dict[str, object]] = []
    for run in range(1, 4):
        for sample in samples:
            results.append(
                {
                    "run_id": f"tiktok-alias-run-{run}",
                    "sample_id": sample["sample_id"],
                    "job_kind": "download",
                    "observed_outcome": sample["expected_outcome"],
                    "observed_output_count": sample["expected_output_count"],
                    "completed_at": f"2026-09-0{run}T10:00:00+09:30",
                    "adapter": "yt_dlp",
                    "downloader_version": "2026.08.19",
                    "environment": ENVIRONMENT,
                    "product_version": product_identity,
                }
            )
    return (
        _csv_bytes(MANIFEST_FIELDS, samples),
        _csv_bytes(RESULT_FIELDS, results),
    )


@pytest.fixture
def repository(tmp_path: Path) -> CapabilityEvidenceRepository:
    database = Database(tmp_path / "capability.sqlite3")
    database.initialize()
    return CapabilityEvidenceRepository(database)


def _import(
    repository: CapabilityEvidenceRepository,
    bundle: tuple[bytes, bytes],
    *,
    environment: str = ENVIRONMENT,
):
    manifest, results = bundle
    return repository.import_csv_bundle(
        manifest_content=manifest,
        results_content=results,
        environment=environment,
    )


def test_stage0_v3_import_records_qualified_and_insufficient_without_approval(
    repository: CapabilityEvidenceRepository,
) -> None:
    qualified = _import(
        repository,
        _stage0_bundle(positive_count=10, label="qualified"),
    )
    insufficient = _import(
        repository,
        _stage0_bundle(positive_count=9, label="insufficient"),
    )

    evidence = repository.list_evidence()

    assert qualified.created_count == 1
    assert insufficient.created_count == 1
    assert {record["assessment"] for record in evidence} == {
        "qualified",
        "insufficient",
    }
    assert {
        (record["positive_samples"], record["complete_runs"])
        for record in evidence
    } == {(10, 3), (9, 3)}
    assert repository.list_current_decisions() == []


def test_import_is_idempotent_for_the_same_canonical_bundle(
    repository: CapabilityEvidenceRepository,
) -> None:
    first = _import(
        repository,
        _stage0_bundle(label="idempotent"),
    )
    reordered = _import(
        repository,
        _stage0_bundle(label="idempotent", reverse_rows=True),
    )

    assert first.created_count == 1
    assert reordered.created_count == 0
    assert reordered.evidence_ids == first.evidence_ids
    assert [record["evidence_id"] for record in repository.list_evidence()] == [
        first.evidence_ids[0]
    ]
    assert repository.list_current_decisions() == []


def test_tiktok_handle_aliases_produce_the_same_evidence_identity(
    repository: CapabilityEvidenceRepository,
) -> None:
    first = _import(
        repository,
        _tiktok_alias_bundle(host="www.tiktok.com", handle="First.Handle"),
    )
    alias = _import(
        repository,
        _tiktok_alias_bundle(host="m.tiktok.com", handle="second_handle"),
    )

    assert first.created_count == 1
    assert alias.created_count == 0
    assert alias.evidence_ids == first.evidence_ids
    assert len(repository.list_evidence()) == 1


def test_approve_revoke_revision_history_and_replay_rules(
    repository: CapabilityEvidenceRepository,
) -> None:
    imported = _import(repository, _stage0_bundle(label="lifecycle"))
    evidence_id = imported.evidence_ids[0]
    evidence = repository.list_evidence()[0]

    approved = repository.approve(
        evidence_id=evidence_id,
        expected_revision=0,
        reason_code="stage0-reviewed",
    )
    duplicate_active_approval = repository.approve(
        evidence_id=evidence_id,
        expected_revision=1,
        reason_code="stage0-reviewed",
    )

    assert approved.created is True
    assert approved.action == "approve"
    assert approved.status == "verified"
    assert approved.revision == 1
    assert duplicate_active_approval.created is False
    assert duplicate_active_approval.decision_id == approved.decision_id
    assert duplicate_active_approval.revision == 1

    with pytest.raises(CapabilityGovernanceError) as stale:
        repository.revoke(
            evidence_id=evidence_id,
            expected_revision=0,
            reason_code="regression",
        )
    assert stale.value.error_code == "decision_revision_mismatch"

    revoked = repository.revoke(
        evidence_id=evidence_id,
        expected_revision=1,
        reason_code="regression",
    )

    assert revoked.created is True
    assert revoked.action == "revoke"
    assert revoked.status == "candidate"
    assert revoked.revision == 2
    assert repository.list_current_decisions() == [
        {
            "decision_id": revoked.decision_id,
            "identity_key": evidence["identity_key"],
            "evidence_id": evidence_id,
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "adapter": "yt_dlp",
            "downloader_version": "2026.08.19",
            "environment": ENVIRONMENT,
            "product_version": current_product_identity(),
            "action": "revoke",
            "state": "revoked",
            "revision": 2,
            "reason_code": "regression",
            "decided_at": revoked.decided_at,
        }
    ]
    history = repository.history(identity_key=evidence["identity_key"])
    assert [(item["revision"], item["action"], item["state"]) for item in history] == [
        (2, "revoke", "revoked"),
        (1, "approve", "approved"),
    ]

    with pytest.raises(CapabilityGovernanceError) as replay:
        repository.approve(
            evidence_id=evidence_id,
            expected_revision=2,
            reason_code="replacement-reviewed",
        )
    assert replay.value.error_code == "evidence_replay_not_allowed"
    assert len(repository.history(identity_key=evidence["identity_key"])) == 2


def test_insufficient_evidence_cannot_be_approved(
    repository: CapabilityEvidenceRepository,
) -> None:
    imported = _import(
        repository,
        _stage0_bundle(positive_count=9, label="candidate"),
    )

    with pytest.raises(CapabilityGovernanceError) as captured:
        repository.approve(
            evidence_id=imported.evidence_ids[0],
            expected_revision=0,
            reason_code="stage0-reviewed",
        )

    assert captured.value.error_code == "evidence_not_qualified"
    assert repository.list_current_decisions() == []


@pytest.mark.parametrize(
    ("bundle_environment", "requested_environment", "product_version", "error_code"),
    (
        (
            ENVIRONMENT,
            "linux-x64-isolated-no-cookie",
            current_product_identity(),
            "environment_mismatch",
        ),
        (ENVIRONMENT, ENVIRONMENT, "0.12.0", "product_version_mismatch"),
        (ENVIRONMENT, ENVIRONMENT, "0.13.0", "product_version_mismatch"),
        (
            ENVIRONMENT,
            ENVIRONMENT,
            "0.14.0+build.sha256." + ("0" * 64),
            "product_version_mismatch",
        ),
    ),
)
def test_import_rejects_product_or_environment_identity_mismatch(
    repository: CapabilityEvidenceRepository,
    bundle_environment: str,
    requested_environment: str,
    product_version: str,
    error_code: str,
) -> None:
    bundle = _stage0_bundle(
        environment=bundle_environment,
        product_version=product_version,
        label="identity-mismatch",
    )

    with pytest.raises(CapabilityGovernanceError) as captured:
        _import(repository, bundle, environment=requested_environment)

    assert captured.value.error_code == error_code
    assert repository.list_evidence() == []
    assert repository.list_current_decisions() == []


def test_disabled_route_remains_a_tombstone_for_revoke_only(
    repository: CapabilityEvidenceRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _import(repository, _stage0_bundle(label="before-disable-one"))
    second = _import(repository, _stage0_bundle(label="before-disable-two"))
    first_id = first.evidence_ids[0]
    second_id = second.evidence_ids[0]
    repository.approve(
        evidence_id=first_id,
        expected_revision=0,
        reason_code="stage0-reviewed",
    )

    disabled_registry = PlatformCapabilityRegistry(
        tuple(
            replace(item, status=CapabilityStatus.DISABLED)
            if item.route.platform.value == "youtube"
            and item.route.source_type.value == "youtube_video"
            else item
            for item in capability_evidence_module.DEFAULT_DOWNLOAD_CAPABILITIES.list()
        )
    )
    monkeypatch.setattr(
        capability_evidence_module,
        "DEFAULT_DOWNLOAD_CAPABILITIES",
        disabled_registry,
    )

    revoked = repository.revoke(
        evidence_id=first_id,
        expected_revision=1,
        reason_code="operator-withdrawn",
    )
    assert revoked.action == "revoke"
    assert revoked.revision == 2

    with pytest.raises(CapabilityGovernanceError) as approve_error:
        repository.approve(
            evidence_id=second_id,
            expected_revision=2,
            reason_code="replacement-reviewed",
        )
    assert approve_error.value.error_code == "implementation_not_registered"

    with pytest.raises(CapabilityGovernanceError) as import_error:
        _import(repository, _stage0_bundle(label="after-disable"))
    assert import_error.value.error_code == "implementation_not_registered"


def test_build_drift_blocks_approval_but_not_historical_revoke(
    repository: CapabilityEvidenceRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _import(repository, _stage0_bundle(label="build-one"))
    second = _import(repository, _stage0_bundle(label="build-two"))
    first_id = first.evidence_ids[0]
    second_id = second.evidence_ids[0]
    repository.approve(
        evidence_id=first_id,
        expected_revision=0,
        reason_code="stage0-reviewed",
    )
    monkeypatch.setattr(
        capability_evidence_module,
        "current_product_identity",
        lambda: "0.14.0+build.sha256." + ("f" * 64),
    )

    with pytest.raises(CapabilityGovernanceError) as approval_error:
        repository.approve(
            evidence_id=second_id,
            expected_revision=1,
            reason_code="replacement-reviewed",
        )
    assert approval_error.value.error_code == "product_version_mismatch"

    revoked = repository.revoke(
        evidence_id=first_id,
        expected_revision=1,
        reason_code="superseded",
    )
    assert revoked.action == "revoke"
    assert revoked.revision == 2
    assert len(repository.history(identity_key=revoked.identity_key)) == 2


@pytest.mark.parametrize(
    ("error", "error_code"),
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
def test_product_identity_failures_block_import_with_stable_error(
    repository: CapabilityEvidenceRepository,
    monkeypatch: pytest.MonkeyPatch,
    error: RuntimeError,
    error_code: str,
) -> None:
    def fail_identity() -> str:
        raise error

    monkeypatch.setattr(
        capability_evidence_module, "current_product_identity", fail_identity
    )

    with pytest.raises(CapabilityGovernanceError) as captured:
        _import(repository, _stage0_bundle(label="identity-error"))

    assert captured.value.error_code == error_code
    assert captured.value.exit_code == 6
    assert "PRIVATE" not in str(captured.value)
    assert repository.list_evidence() == []
    assert repository.list_current_decisions() == []


def test_product_identity_failure_blocks_approval_but_not_revoke(
    repository: CapabilityEvidenceRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _import(repository, _stage0_bundle(label="approved-before-drift"))
    second = _import(repository, _stage0_bundle(label="pending-before-drift"))
    first_id = first.evidence_ids[0]
    repository.approve(
        evidence_id=first_id,
        expected_revision=0,
        reason_code="stage0-reviewed",
    )

    def fail_identity() -> str:
        raise ProductBuildDriftError(r"C:\Users\PRIVATE-DRIFT-CANARY")

    monkeypatch.setattr(
        capability_evidence_module, "current_product_identity", fail_identity
    )

    with pytest.raises(CapabilityGovernanceError) as captured:
        repository.approve(
            evidence_id=second.evidence_ids[0],
            expected_revision=1,
            reason_code="replacement-reviewed",
        )

    assert captured.value.error_code == "product_build_drift"
    revoked = repository.revoke(
        evidence_id=first_id,
        expected_revision=1,
        reason_code="operator-withdrawn",
    )
    assert revoked.action == "revoke"
    assert revoked.revision == 2


def test_product_identity_is_rechecked_before_import_commit(
    repository: CapabilityEvidenceRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _stage0_bundle(label="import-boundary-drift")
    product_identity = current_product_identity()
    calls = 0

    def identity_then_drift() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return product_identity
        raise ProductBuildDriftError(r"C:\Users\PRIVATE-DRIFT-CANARY")

    monkeypatch.setattr(
        capability_evidence_module,
        "current_product_identity",
        identity_then_drift,
    )

    with pytest.raises(CapabilityGovernanceError) as captured:
        _import(repository, bundle)

    assert captured.value.error_code == "product_build_drift"
    assert repository.list_evidence() == []


def test_product_identity_is_rechecked_before_approval_commit(
    repository: CapabilityEvidenceRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = _import(repository, _stage0_bundle(label="approval-boundary-drift"))
    product_identity = current_product_identity()
    calls = 0

    def identity_then_drift() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return product_identity
        raise ProductBuildDriftError(r"C:\Users\PRIVATE-DRIFT-CANARY")

    monkeypatch.setattr(
        capability_evidence_module,
        "current_product_identity",
        identity_then_drift,
    )

    with pytest.raises(CapabilityGovernanceError) as captured:
        repository.approve(
            evidence_id=imported.evidence_ids[0],
            expected_revision=0,
            reason_code="stage0-reviewed",
        )

    assert captured.value.error_code == "product_build_drift"
    assert repository.list_current_decisions() == []


def test_build_digest_is_part_of_the_capability_identity_key() -> None:
    identity = {
        "platform": "youtube",
        "source_type": "youtube_video",
        "job_kind": "download",
        "adapter": "yt_dlp",
        "downloader_version": "2026.08.19",
        "environment": ENVIRONMENT,
    }

    first = capability_identity_key(
        **identity,
        product_version="0.14.0+build.sha256." + ("1" * 64),
    )
    second = capability_identity_key(
        **identity,
        product_version="0.14.0+build.sha256." + ("2" * 64),
    )

    assert first != second

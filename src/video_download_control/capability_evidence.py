from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .capabilities import (
    DEFAULT_DOWNLOAD_CAPABILITIES,
    CapabilityStatus,
)
from .build_identity import (
    ProductBuildDriftError,
    ProductBuildUnavailableError,
    current_product_identity,
)
from .database import Database
from .validation import (
    STAGE0_POLICY_VERSION,
    CapabilityEvidence,
    ValidationResult,
    ValidationSample,
    evaluate_capabilities,
    parse_validation_manifest,
    parse_validation_results,
)


MINIMUM_POSITIVE_SAMPLES = 10
REQUIRED_CONSECUTIVE_RUNS = 3
MINIMUM_POSITIVE_RATE = 0.9
EVIDENCE_KIND = "stage0_csv_v3"

_IDENTITY_KEY = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_APPROVE_REASONS = frozenset({"stage0-reviewed", "replacement-reviewed"})
_REVOKE_REASONS = frozenset(
    {"regression", "superseded", "operator-withdrawn"}
)


class CapabilityGovernanceError(RuntimeError):
    """A stable, privacy-safe error suitable for a CLI/API boundary."""

    def __init__(self, error_code: str, *, exit_code: int = 3) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class EvidenceImportResult:
    created_count: int
    evidence_ids: tuple[str, ...]

    @property
    def created(self) -> bool:
        return self.created_count > 0

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "operation": "import",
            "created": self.created,
            "created_count": self.created_count,
            "evidence_count": len(self.evidence_ids),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class DecisionResult:
    decision_id: str
    identity_key: str
    evidence_id: str
    action: str
    status: str
    revision: int
    reason_code: str
    decided_at: str
    created: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "operation": self.action,
            "created": self.created,
            "decision": {
                "decision_id": self.decision_id,
                "identity_key": self.identity_key,
                "evidence_id": self.evidence_id,
                "state": "approved" if self.status == "verified" else "revoked",
                "revision": self.revision,
                "reason_code": self.reason_code,
                "decided_at": self.decided_at,
            },
        }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def capability_identity_key(
    *,
    platform: str,
    source_type: str,
    job_kind: str,
    adapter: str,
    downloader_version: str,
    environment: str,
    product_version: str,
) -> str:
    return _sha256_json(
        {
            "adapter": adapter,
            "downloader_version": downloader_version,
            "environment": environment,
            "job_kind": job_kind,
            "platform": platform,
            "product_version": product_version,
            "source_type": source_type,
        }
    )


def implementation_id(
    *, platform: str, source_type: str, job_kind: str, adapter: str
) -> str:
    return "impl_" + _sha256_json(
        {
            "adapter": adapter,
            "job_kind": job_kind,
            "platform": platform,
            "source_type": source_type,
        }
    )[:24]


def _registered_implementation(
    *, platform: str, source_type: str, job_kind: str, adapter: str
) -> bool:
    return any(
        item.route.platform.value == platform
        and item.route.source_type.value == source_type
        and item.route.job_kind.value == job_kind
        and item.adapter == adapter
        and item.status is not CapabilityStatus.DISABLED
        for item in DEFAULT_DOWNLOAD_CAPABILITIES.list()
    )


def _known_implementation(
    *, platform: str, source_type: str, job_kind: str, adapter: str
) -> bool:
    return any(
        item.route.platform.value == platform
        and item.route.source_type.value == source_type
        and item.route.job_kind.value == job_kind
        and item.adapter == adapter
        for item in DEFAULT_DOWNLOAD_CAPABILITIES.list()
    )


def _canonical_private_bundle(
    samples: tuple[ValidationSample, ...],
    results: tuple[ValidationResult, ...],
) -> dict[str, object]:
    """Return content used only for a digest; callers must never persist it."""

    return {
        "format_version": 3,
        "policy_version": STAGE0_POLICY_VERSION,
        "policy": {
            "minimum_positive_samples": MINIMUM_POSITIVE_SAMPLES,
            "required_consecutive_runs": REQUIRED_CONSECUTIVE_RUNS,
            "minimum_positive_rate_ppm": int(MINIMUM_POSITIVE_RATE * 1_000_000),
        },
        "samples": sorted(
            (
                {
                    "expected_outcome": item.expected_outcome,
                    "expected_output_count": item.expected_output_count,
                    "job_kind": item.job_kind.value,
                    "platform": item.platform.value,
                    "region": item.region,
                    "requires_cookie": item.requires_cookie,
                    "sample_id": item.sample_id,
                    "source_type": item.source_type.value,
                    "source_identity_sha256": item.source_identity_sha256,
                }
                for item in samples
            ),
            key=lambda item: (str(item["sample_id"]), str(item["job_kind"])),
        ),
        "results": sorted(
            (
                {
                    "adapter": item.adapter,
                    "completed_at": item.completed_at.isoformat(),
                    "downloader_version": item.downloader_version,
                    "environment": item.environment,
                    "job_kind": item.job_kind.value,
                    "observed_outcome": item.observed_outcome,
                    "observed_output_count": item.observed_output_count,
                    "product_version": item.product_version,
                    "run_id": item.run_id,
                    "sample_id": item.sample_id,
                }
                for item in results
            ),
            key=lambda item: (
                str(item["run_id"]),
                str(item["sample_id"]),
            ),
        ),
    }


def _evaluated_at(
    evidence: CapabilityEvidence,
    samples: tuple[ValidationSample, ...],
    results: tuple[ValidationResult, ...],
) -> str:
    sample_ids = {
        item.sample_id
        for item in samples
        if item.platform is evidence.platform
        and item.source_type is evidence.source_type
        and item.job_kind is evidence.job_kind
    }
    completed = [
        item.completed_at
        for item in results
        if item.sample_id in sample_ids
        and item.adapter == evidence.adapter
        and item.downloader_version == evidence.downloader_version
        and item.environment == evidence.environment
        and item.product_version == evidence.product_version
    ]
    if not completed:
        raise CapabilityGovernanceError("evidence_cell_unexecuted")
    return max(completed).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _evidence_payload(
    evidence: CapabilityEvidence,
    *,
    bundle_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    identity = {
        "platform": evidence.platform.value,
        "source_type": evidence.source_type.value,
        "job_kind": evidence.job_kind.value,
        "adapter": evidence.adapter,
        "downloader_version": evidence.downloader_version,
        "environment": evidence.environment,
        "product_version": evidence.product_version,
    }
    return {
        **identity,
        "identity_key": capability_identity_key(**identity),
        "evidence_kind": EVIDENCE_KIND,
        "policy_version": STAGE0_POLICY_VERSION,
        "bundle_sha256": bundle_sha256,
        "positive_sample_count": evidence.positive_samples,
        "negative_sample_count": evidence.negative_samples,
        "complete_run_count": evidence.complete_runs,
        "positive_rates": list(evidence.last_three_positive_rates),
        "negative_rates": list(evidence.last_three_negative_rates),
        "verdict": evidence.status.value,
        "evaluated_at": evaluated_at,
    }


def evidence_sha256_from_payload(payload: dict[str, object]) -> str:
    return _sha256_json(payload)


def _canonical_uuid(raw: str, *, error_code: str) -> str:
    try:
        value = str(UUID(raw))
    except (AttributeError, ValueError) as exc:
        raise CapabilityGovernanceError(error_code, exit_code=2) from exc
    if value != raw:
        raise CapabilityGovernanceError(error_code, exit_code=2)
    return value


def _validate_identity_key(raw: str) -> str:
    if not _IDENTITY_KEY.fullmatch(raw):
        raise CapabilityGovernanceError("invalid_identity_key", exit_code=2)
    return raw


def _validate_reason(raw: str, *, action: str) -> str:
    allowed = _APPROVE_REASONS if action == "approve" else _REVOKE_REASONS
    if not _REASON_CODE.fullmatch(raw) or raw not in allowed:
        raise CapabilityGovernanceError("invalid_reason_code", exit_code=2)
    return raw


def _required_current_product_identity() -> str:
    try:
        return current_product_identity()
    except ProductBuildDriftError:
        raise CapabilityGovernanceError(
            "product_build_drift", exit_code=6
        ) from None
    except ProductBuildUnavailableError:
        raise CapabilityGovernanceError(
            "product_build_unavailable", exit_code=6
        ) from None


class CapabilityEvidenceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def import_csv_bundle(
        self,
        *,
        manifest_content: bytes,
        results_content: bytes,
        environment: str,
    ) -> EvidenceImportResult:
        samples = parse_validation_manifest(manifest_content)
        results = parse_validation_results(results_content)
        if not results:
            raise CapabilityGovernanceError("validation_results_empty", exit_code=2)
        if any(item.environment != environment for item in results):
            raise CapabilityGovernanceError("environment_mismatch")
        product_identity = _required_current_product_identity()
        if any(item.product_version != product_identity for item in results):
            raise CapabilityGovernanceError("product_version_mismatch")

        evaluated = evaluate_capabilities(
            samples,
            results,
            minimum_positive_samples=MINIMUM_POSITIVE_SAMPLES,
            required_consecutive_runs=REQUIRED_CONSECUTIVE_RUNS,
            minimum_positive_rate=MINIMUM_POSITIVE_RATE,
        )
        if not evaluated:
            raise CapabilityGovernanceError("evidence_bundle_empty")
        bundle_sha256 = _sha256_json(_canonical_private_bundle(samples, results))
        manifest_sha256 = _sha256_bytes(manifest_content)
        results_sha256 = _sha256_bytes(results_content)
        imported_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        records: list[tuple[dict[str, object], str]] = []
        for item in evaluated:
            if item.adapter == "unexecuted":
                raise CapabilityGovernanceError("evidence_cell_unexecuted")
            identity = {
                "platform": item.platform.value,
                "source_type": item.source_type.value,
                "job_kind": item.job_kind.value,
                "adapter": item.adapter,
            }
            if not _registered_implementation(**identity):
                raise CapabilityGovernanceError("implementation_not_registered")
            evaluated_at = _evaluated_at(item, samples, results)
            payload = _evidence_payload(
                item,
                bundle_sha256=bundle_sha256,
                evaluated_at=evaluated_at,
            )
            records.append((payload, evidence_sha256_from_payload(payload)))

        created = 0
        evidence_ids: list[str] = []
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for payload, evidence_sha256 in sorted(
                    records, key=lambda item: item[1]
                ):
                    existing = connection.execute(
                        """
                        SELECT evidence_id FROM capability_evidence
                        WHERE evidence_sha256 = ?
                        """,
                        (evidence_sha256,),
                    ).fetchone()
                    if existing is not None:
                        evidence_ids.append(str(existing["evidence_id"]))
                        continue
                    evidence_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO capability_evidence(
                            evidence_id, identity_key, platform, source_type,
                            job_kind, adapter, downloader_version, environment,
                            product_version, evidence_kind, policy_version,
                            manifest_sha256, results_sha256, bundle_sha256,
                            evidence_sha256, positive_sample_count,
                            negative_sample_count, complete_run_count,
                            positive_rates_json, negative_rates_json, verdict,
                            evaluated_at, imported_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            evidence_id,
                            payload["identity_key"],
                            payload["platform"],
                            payload["source_type"],
                            payload["job_kind"],
                            payload["adapter"],
                            payload["downloader_version"],
                            payload["environment"],
                            payload["product_version"],
                            payload["evidence_kind"],
                            payload["policy_version"],
                            manifest_sha256,
                            results_sha256,
                            payload["bundle_sha256"],
                            evidence_sha256,
                            payload["positive_sample_count"],
                            payload["negative_sample_count"],
                            payload["complete_run_count"],
                            _canonical_json(payload["positive_rates"]),
                            _canonical_json(payload["negative_rates"]),
                            payload["verdict"],
                            payload["evaluated_at"],
                            imported_at,
                        ),
                    )
                    created += 1
                    evidence_ids.append(evidence_id)
                if _required_current_product_identity() != product_identity:
                    raise CapabilityGovernanceError("product_build_drift", exit_code=6)
        except sqlite3.IntegrityError as exc:
            raise CapabilityGovernanceError(
                "evidence_integrity_conflict", exit_code=6
            ) from exc
        return EvidenceImportResult(
            created_count=created,
            evidence_ids=tuple(sorted(evidence_ids)),
        )

    def approve(
        self,
        *,
        evidence_id: str,
        expected_revision: int,
        reason_code: str,
    ) -> DecisionResult:
        return self._decide(
            action="approve",
            evidence_id=_canonical_uuid(
                evidence_id, error_code="invalid_evidence_id"
            ),
            expected_revision=expected_revision,
            reason_code=_validate_reason(reason_code, action="approve"),
        )

    def revoke(
        self,
        *,
        evidence_id: str,
        expected_revision: int,
        reason_code: str,
    ) -> DecisionResult:
        return self._decide(
            action="revoke",
            evidence_id=_canonical_uuid(
                evidence_id, error_code="invalid_evidence_id"
            ),
            expected_revision=expected_revision,
            reason_code=_validate_reason(reason_code, action="revoke"),
        )

    def _decide(
        self,
        *,
        action: str,
        evidence_id: str,
        expected_revision: int,
        reason_code: str,
    ) -> DecisionResult:
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise CapabilityGovernanceError("invalid_expected_revision", exit_code=2)
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                evidence = connection.execute(
                    "SELECT * FROM capability_evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
                if evidence is None:
                    raise CapabilityGovernanceError("evidence_not_found", exit_code=4)
                approval_product_identity: str | None = None
                if action == "approve":
                    approval_product_identity = _required_current_product_identity()
                    if evidence["product_version"] != approval_product_identity:
                        raise CapabilityGovernanceError("product_version_mismatch")
                implementation_check = (
                    _registered_implementation
                    if action == "approve"
                    else _known_implementation
                )
                if not implementation_check(
                    platform=evidence["platform"],
                    source_type=evidence["source_type"],
                    job_kind=evidence["job_kind"],
                    adapter=evidence["adapter"],
                ):
                    raise CapabilityGovernanceError("implementation_not_registered")
                head = connection.execute(
                    """
                    SELECT * FROM platform_capabilities
                    WHERE identity_key = ?
                    """,
                    (evidence["identity_key"],),
                ).fetchone()
                revision = int(head["revision"]) if head is not None else 0
                if revision != expected_revision:
                    raise CapabilityGovernanceError(
                        "decision_revision_mismatch", exit_code=5
                    )
                if action == "approve":
                    if evidence["verdict"] != CapabilityStatus.VERIFIED.value:
                        raise CapabilityGovernanceError("evidence_not_qualified")
                    if (
                        head is not None
                        and head["status"] == CapabilityStatus.VERIFIED.value
                        and head["evidence_id"] == evidence_id
                    ):
                        return _decision_from_row(head, created=False)
                    used = connection.execute(
                        """
                        SELECT 1 FROM capability_decisions
                        WHERE evidence_id = ? LIMIT 1
                        """,
                        (evidence_id,),
                    ).fetchone()
                    if used is not None:
                        raise CapabilityGovernanceError("evidence_replay_not_allowed")
                    status = CapabilityStatus.VERIFIED.value
                else:
                    if (
                        head is None
                        or head["status"] != CapabilityStatus.VERIFIED.value
                        or head["evidence_id"] != evidence_id
                    ):
                        raise CapabilityGovernanceError("invalid_decision_transition")
                    status = CapabilityStatus.CANDIDATE.value

                decision_id = str(uuid4())
                decided_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                connection.execute(
                    """
                    INSERT INTO capability_decisions(
                        decision_id, identity_key, platform, source_type,
                        job_kind, adapter, downloader_version, environment,
                        product_version, evidence_id, action, status,
                        supersedes_decision_id, revision, reason_code, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        evidence["identity_key"],
                        evidence["platform"],
                        evidence["source_type"],
                        evidence["job_kind"],
                        evidence["adapter"],
                        evidence["downloader_version"],
                        evidence["environment"],
                        evidence["product_version"],
                        evidence_id,
                        action,
                        status,
                        head["current_decision_id"] if head is not None else None,
                        revision + 1,
                        reason_code,
                        decided_at,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM platform_capabilities
                    WHERE current_decision_id = ?
                    """,
                    (decision_id,),
                ).fetchone()
                if row is None:
                    raise CapabilityGovernanceError(
                        "decision_projection_unavailable", exit_code=6
                    )
                if (
                    approval_product_identity is not None
                    and _required_current_product_identity()
                    != approval_product_identity
                ):
                    raise CapabilityGovernanceError(
                        "product_build_drift", exit_code=6
                    )
                return _decision_from_row(row, created=True)
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "unique" in message:
                raise CapabilityGovernanceError(
                    "decision_revision_mismatch", exit_code=5
                ) from exc
            raise CapabilityGovernanceError(
                "decision_integrity_conflict", exit_code=6
            ) from exc

    def list_evidence(self, *, limit: int = 200) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise CapabilityGovernanceError("invalid_limit", exit_code=2)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, identity_key, platform, source_type,
                       job_kind, adapter, downloader_version, environment,
                       product_version, evidence_kind, policy_version,
                       positive_sample_count, negative_sample_count,
                       complete_run_count, positive_rates_json,
                       negative_rates_json, verdict, evaluated_at, imported_at
                FROM capability_evidence
                ORDER BY imported_at DESC, evidence_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_public_evidence(row) for row in rows]

    def list_current_decisions(self, *, limit: int = 200) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise CapabilityGovernanceError("invalid_limit", exit_code=2)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM platform_capabilities
                ORDER BY decided_at DESC, current_decision_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_public_decision(row) for row in rows]

    def snapshot(self, *, limit: int = 200) -> dict[str, object]:
        """Read evidence and current decisions from one SQLite snapshot.

        Evidence referenced by a returned current decision is always included,
        even when it falls outside the newest-evidence window.  This prevents a
        bounded UI view from rendering a decision against the wrong evidence.
        """

        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise CapabilityGovernanceError("invalid_limit", exit_code=2)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            evidence_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM capability_evidence"
                ).fetchone()[0]
            )
            decision_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM platform_capabilities"
                ).fetchone()[0]
            )
            decision_rows = connection.execute(
                """
                SELECT * FROM platform_capabilities
                ORDER BY decided_at DESC, current_decision_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            evidence_rows = list(
                connection.execute(
                    """
                    SELECT evidence_id, identity_key, platform, source_type,
                           job_kind, adapter, downloader_version, environment,
                           product_version, evidence_kind, policy_version,
                           positive_sample_count, negative_sample_count,
                           complete_run_count, positive_rates_json,
                           negative_rates_json, verdict, evaluated_at, imported_at
                    FROM capability_evidence
                    ORDER BY imported_at DESC, evidence_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )
            included_ids = {str(row["evidence_id"]) for row in evidence_rows}
            referenced_ids = {
                str(row["evidence_id"]) for row in decision_rows
            } - included_ids
            if referenced_ids:
                placeholders = ", ".join("?" for _ in referenced_ids)
                evidence_rows.extend(
                    connection.execute(
                        f"""
                        SELECT evidence_id, identity_key, platform, source_type,
                               job_kind, adapter, downloader_version, environment,
                               product_version, evidence_kind, policy_version,
                               positive_sample_count, negative_sample_count,
                               complete_run_count, positive_rates_json,
                               negative_rates_json, verdict, evaluated_at,
                               imported_at
                        FROM capability_evidence
                        WHERE evidence_id IN ({placeholders})
                        """,
                        tuple(sorted(referenced_ids)),
                    ).fetchall()
                )
        evidence_rows.sort(
            key=lambda row: (str(row["imported_at"]), str(row["evidence_id"])),
            reverse=True,
        )
        return {
            "evidence": [_public_evidence(row) for row in evidence_rows],
            "decisions": [_public_decision(row) for row in decision_rows],
            "evidence_total": evidence_total,
            "decision_total": decision_total,
            "evidence_truncated": evidence_total > len(evidence_rows),
            "decision_truncated": decision_total > len(decision_rows),
        }

    def history(
        self, *, identity_key: str, limit: int = 200
    ) -> list[dict[str, object]]:
        identity_key = _validate_identity_key(identity_key)
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise CapabilityGovernanceError("invalid_limit", exit_code=2)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT decision_id AS current_decision_id, identity_key,
                       platform, source_type, job_kind, adapter,
                       downloader_version, environment, product_version,
                       evidence_id, action, status, supersedes_decision_id,
                       revision, reason_code, decided_at
                FROM capability_decisions
                WHERE identity_key = ?
                ORDER BY revision DESC
                LIMIT ?
                """,
                (identity_key, limit),
            ).fetchall()
        if not rows:
            raise CapabilityGovernanceError("decision_history_not_found", exit_code=4)
        return [_public_decision(row) for row in rows]


def _decision_from_row(row: sqlite3.Row, *, created: bool) -> DecisionResult:
    return DecisionResult(
        decision_id=str(row["current_decision_id"]),
        identity_key=str(row["identity_key"]),
        evidence_id=str(row["evidence_id"]),
        action=str(row["action"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        reason_code=str(row["reason_code"]),
        decided_at=str(row["decided_at"]),
        created=created,
    )


def _public_evidence(row: sqlite3.Row) -> dict[str, object]:
    return {
        "evidence_id": row["evidence_id"],
        "identity_key": row["identity_key"],
        "implementation_id": implementation_id(
            platform=row["platform"],
            source_type=row["source_type"],
            job_kind=row["job_kind"],
            adapter=row["adapter"],
        ),
        "platform": row["platform"],
        "source_type": row["source_type"],
        "job_kind": row["job_kind"],
        "adapter": row["adapter"],
        "downloader_version": row["downloader_version"],
        "environment": row["environment"],
        "product_version": row["product_version"],
        "evidence_kind": row["evidence_kind"],
        "policy_version": row["policy_version"],
        "assessment": (
            "qualified" if row["verdict"] == "verified" else "insufficient"
        ),
        "positive_samples": int(row["positive_sample_count"]),
        "negative_samples": int(row["negative_sample_count"]),
        "complete_runs": int(row["complete_run_count"]),
        "recent_positive_rates": json.loads(row["positive_rates_json"]),
        "recent_negative_rates": json.loads(row["negative_rates_json"]),
        "evaluated_at": row["evaluated_at"],
        "imported_at": row["imported_at"],
    }


def _public_decision(row: sqlite3.Row) -> dict[str, object]:
    return {
        "decision_id": row["current_decision_id"],
        "identity_key": row["identity_key"],
        "evidence_id": row["evidence_id"],
        "platform": row["platform"],
        "source_type": row["source_type"],
        "job_kind": row["job_kind"],
        "adapter": row["adapter"],
        "downloader_version": row["downloader_version"],
        "environment": row["environment"],
        "product_version": row["product_version"],
        "action": row["action"],
        "state": "approved" if row["status"] == "verified" else "revoked",
        "revision": int(row["revision"]),
        "reason_code": row["reason_code"],
        "decided_at": row["decided_at"],
    }


def list_registered_implementations() -> list[dict[str, object]]:
    return [
        {
            "implementation_id": implementation_id(
                platform=item.route.platform.value,
                source_type=item.route.source_type.value,
                job_kind=item.route.job_kind.value,
                adapter=item.adapter,
            ),
            "platform": item.route.platform.value,
            "source_type": item.route.source_type.value,
            "job_kind": item.route.job_kind.value,
            "adapter": item.adapter,
            "implementation_status": item.status.value,
            "authentication": item.authentication.value,
            "short_link_status": item.short_link_status.value,
        }
        for item in DEFAULT_DOWNLOAD_CAPABILITIES.list()
    ]


__all__ = [
    "CapabilityEvidenceRepository",
    "CapabilityGovernanceError",
    "DecisionResult",
    "EVIDENCE_KIND",
    "EvidenceImportResult",
    "MINIMUM_POSITIVE_RATE",
    "MINIMUM_POSITIVE_SAMPLES",
    "REQUIRED_CONSECUTIVE_RUNS",
    "capability_identity_key",
    "evidence_sha256_from_payload",
    "implementation_id",
    "list_registered_implementations",
]

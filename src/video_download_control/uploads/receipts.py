"""Pure Upload receipt state contract, shared by execution and backup audits.

Callers retain ownership of database snapshots, immutable request/media identity,
adapter history and restore policy. This module never reads a store or dispatches.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .contracts import (
    UPLOAD_ATTEMPT_STATES,
    UPLOAD_CODE_PATTERN,
    UPLOAD_EVIDENCE_KINDS,
    UPLOAD_RECONCILIATIONS,
    UPLOAD_RESULT_STATUSES,
    UploadError,
    upload_evidence_is_valid,
)


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def validate_upload_attempt_state(
    job: Mapping[str, object], attempt: Mapping[str, object]
) -> None:
    """Reject a receipt whose state cannot have been produced for its job."""

    state = attempt.get("state")
    status = attempt.get("result_status")
    code = attempt.get("result_code")
    evidence_kind = attempt.get("evidence_kind")
    conclusion = attempt.get("reconciliation")
    reconciliation_evidence = attempt.get("reconciliation_evidence_kind")
    revision = attempt.get("revision")
    dispatch_at = attempt.get("dispatch_started_at")
    responded_at = attempt.get("responded_at")
    reconciled_at = attempt.get("reconciled_at")
    optional_timestamps = (dispatch_at, responded_at, reconciled_at)
    if (
        state not in UPLOAD_ATTEMPT_STATES
        or status is not None and status not in UPLOAD_RESULT_STATUSES
        or code is not None
        and (not isinstance(code, str) or UPLOAD_CODE_PATTERN.fullmatch(code) is None)
        or evidence_kind is not None and evidence_kind not in UPLOAD_EVIDENCE_KINDS
        or conclusion is not None and conclusion not in UPLOAD_RECONCILIATIONS
        or reconciliation_evidence not in {None, "operator_platform_check"}
        or not _is_timestamp(attempt.get("created_at"))
        or any(value is not None and not _is_timestamp(value) for value in optional_timestamps)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= 2_147_483_647
        or not upload_evidence_is_valid(
            job.get("platform"),
            job.get("mode"),
            status,
            evidence_kind,
        )
    ):
        raise UploadError("upload_attempt_identity_changed")

    terminal_code = isinstance(code, str) and bool(code)
    raw_unknown_result = (
        status in {"unknown", "canceled"}
        or status == "submitted" and job.get("mode") != "publish"
        or status == "draft_saved" and job.get("mode") != "draft"
    )
    no_reconciliation = all(
        value is None
        for value in (conclusion, reconciliation_evidence, reconciled_at)
    )
    if state == "reserved":
        valid = (
            status is None
            and code is None
            and evidence_kind is None
            and dispatch_at is None
            and responded_at is None
            and revision == 0
            and job.get("state") == "running"
            and no_reconciliation
        )
    elif state == "dispatch_may_have_started":
        valid = (
            status is None
            and code is None
            and evidence_kind is None
            and dispatch_at is not None
            and responded_at is None
            and revision == 1
            and job.get("state") == "running"
            and no_reconciliation
        )
    elif state == "responded":
        valid = (
            terminal_code
            and responded_at is not None
            and no_reconciliation
            and job.get("state") == status
            and job.get("code") == code
            and (
                dispatch_at is None
                and revision == 1
                and status in {"failed", "canceled"}
                and evidence_kind is None
                or dispatch_at is not None
                and revision == 2
                and (
                    status == "failed"
                    or status == "submitted" and job.get("mode") == "publish"
                    or status == "draft_saved" and job.get("mode") == "draft"
                )
            )
        )
    elif state == "unknown":
        valid = (
            terminal_code
            and raw_unknown_result
            and dispatch_at is not None
            and revision == 2
            and no_reconciliation
            and job.get("state") == "unknown"
            and job.get("code") == code
            and (
                responded_at is not None
                or status == "unknown"
                and code == "interrupted_result_unknown"
                and evidence_kind is None
            )
        )
    else:
        expected_job_result = {
            "not_accepted": ("failed", "manual_remote_not_accepted"),
            "submission_acknowledged": (
                "submitted",
                "manual_submission_acknowledged",
            ),
            "draft_saved": ("draft_saved", "manual_platform_draft_saved"),
        }.get(conclusion)
        conclusion_valid = (
            conclusion == "not_accepted"
            or conclusion == "submission_acknowledged"
            and job.get("mode") == "publish"
            or conclusion == "draft_saved"
            and job.get("platform") == "tencent"
            and job.get("mode") == "draft"
        )
        valid = (
            terminal_code
            and raw_unknown_result
            and dispatch_at is not None
            and revision == 3
            and conclusion_valid
            and reconciliation_evidence == "operator_platform_check"
            and reconciled_at is not None
            and expected_job_result is not None
            and (job.get("state"), job.get("code")) == expected_job_result
            and (
                responded_at is not None
                or status == "unknown"
                and code == "interrupted_result_unknown"
                and evidence_kind is None
            )
        )
    if not valid:
        raise UploadError("upload_attempt_identity_changed")

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from . import __version__
from .credentials import CredentialProfileError, validate_opaque_reference
from .database import Database
from .diagnostics import sanitize_diagnostic
from .domain import ErrorCode, InputStatus, JobStatus, Platform, SourceType
from .graph import (
    X_ATTACHMENT_RELATION_TYPE,
    X_ATTACHMENT_SOURCE_TYPE,
    GraphValidationError,
    XAttachmentProbeItem,
    XPostIdentity,
    build_x_attachment_discovery,
    x_attachment_members_json,
    x_attachment_snapshot_hash,
)

if TYPE_CHECKING:
    from .assets import CommittedAsset, StagedAsset


ACTIVE_STATUSES = {
    JobStatus.PROBING,
    JobStatus.DOWNLOADING,
    JobStatus.POSTPROCESSING,
    JobStatus.VERIFYING,
}
TERMINAL_STATUSES = {JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELED}
MAX_ATTEMPTS = 4
MAX_ACTIVE_JOBS = 2
ASSET_INTENT_RECOVERY_LIMIT = 32
ASSET_INTENT_RECOVERY_LEASE_SECONDS = 300
RATE_LIMIT_MANUAL_RESET_THRESHOLD = 3
PLATFORM_FAILURE_THRESHOLDS = {
    ErrorCode.EXTRACTOR_BROKEN: 2,
}
_CREDENTIAL_FAILURE_DIAGNOSTIC = (
    "credential profile is unavailable or invalid for this job"
)

PendingAssetCleanup = Callable[[str, str], None]
StagedAssetPublisher = Callable[["StagedAsset"], "CommittedAsset"]
ManagedAttemptLister = Callable[[int], Sequence[str]]
ManagedAttemptCleanup = Callable[[str, str], None]


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("worker timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    attempt_id: str
    attempt_no: int
    lease_token: str = field(repr=False)
    worker_id: str
    lease_expires_at: str
    source_item_id: str
    canonical_url: str = field(repr=False)
    platform: Platform
    source_type: SourceType
    adapter: str
    adapter_version: str
    job_kind: str = "download"
    run_generation: int = 1
    selector_key: str | None = field(default=None, repr=False)
    expected_media_key: str | None = field(default=None, repr=False)
    expected_media_kind: str | None = field(default=None, repr=False)
    credential_ref: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _AssetIntentRecoveryClaim:
    asset_id: str
    job_id: str
    attempt_id: str
    lease_token: str = field(repr=False)
    recovery_token: str = field(repr=False)


class LostLease(RuntimeError):
    pass


class InvalidTransition(RuntimeError):
    pass


class RediscoverConflict(RuntimeError):
    pass


class WorkerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_queue_control(self) -> dict[str, object]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT paused, reason, paused_at, resumed_at, updated_at
                FROM queue_control WHERE id = 1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("queue control singleton is missing")
        payload = dict(row)
        payload["paused"] = bool(payload["paused"])
        return payload

    def pause_queue(self, *, reason: ErrorCode, now: datetime) -> dict[str, object]:
        now_text = utc_text(now)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE queue_control
                SET paused = 1, reason = ?,
                    paused_at = CASE WHEN paused = 0 THEN ? ELSE paused_at END,
                    updated_at = ?
                WHERE id = 1
                RETURNING paused, reason, paused_at, resumed_at, updated_at
                """,
                (reason.value, now_text, now_text),
            ).fetchone()
        if row is None:
            raise RuntimeError("queue control singleton is missing")
        payload = dict(row)
        payload["paused"] = bool(payload["paused"])
        return payload

    def resume_queue(self, *, now: datetime) -> dict[str, object]:
        now_text = utc_text(now)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE queue_control
                SET paused = 0, reason = NULL, resumed_at = ?, updated_at = ?
                WHERE id = 1
                RETURNING paused, reason, paused_at, resumed_at, updated_at
                """,
                (now_text, now_text),
            ).fetchone()
        if row is None:
            raise RuntimeError("queue control singleton is missing")
        payload = dict(row)
        payload["paused"] = bool(payload["paused"])
        return payload

    def list_platform_circuits(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT platform, state, consecutive_failures, last_error_code,
                       opened_at, cooldown_until, requires_manual_reset,
                       updated_at
                FROM platform_circuits ORDER BY platform
                """
            ).fetchall()
        payloads: list[dict[str, object]] = []
        for row in rows:
            payload = dict(row)
            payload["requires_manual_reset"] = bool(
                payload["requires_manual_reset"]
            )
            payloads.append(payload)
        return payloads

    def reset_platform_circuit(
        self, *, platform: Platform, now: datetime
    ) -> dict[str, object]:
        now_text = utc_text(now)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO platform_circuits(
                    platform, state, consecutive_failures, last_error_code,
                    opened_at, cooldown_until, requires_manual_reset,
                    probe_job_id, probe_lease_token, updated_at
                ) VALUES (?, 'closed', 0, NULL, NULL, NULL, 0, NULL, NULL, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    state = 'closed', consecutive_failures = 0,
                    last_error_code = NULL, opened_at = NULL,
                    cooldown_until = NULL, requires_manual_reset = 0,
                    probe_job_id = NULL, probe_lease_token = NULL,
                    updated_at = excluded.updated_at
                RETURNING platform, state, consecutive_failures, last_error_code,
                          opened_at, cooldown_until, requires_manual_reset,
                          updated_at
                """,
                (platform.value, now_text),
            ).fetchone()
        assert row is not None
        payload = dict(row)
        payload["requires_manual_reset"] = bool(payload["requires_manual_reset"])
        return payload

    def reconcile_attempt_directories(
        self,
        *,
        list_attempt_ids: ManagedAttemptLister,
        cleanup_attempt: ManagedAttemptCleanup,
        limit: int = 64,
    ) -> int:
        """Remove bounded filesystem leftovers only for immutable attempts.

        Running attempts are always skipped. Attempt status is monotonic, so
        filesystem deletion can happen without holding SQLite's writer lock
        and cannot race a terminal attempt back into active execution.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("attempt reconciliation limit must be positive")
        attempt_ids = tuple(list_attempt_ids(limit))
        if len(attempt_ids) > limit or len(set(attempt_ids)) != len(attempt_ids):
            raise RuntimeError("attempt lister violated its bounded contract")
        cleaned = 0
        for attempt_id in attempt_ids:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT job_id, status FROM job_attempts WHERE id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
            if row is None or row["status"] == "running":
                continue
            cleanup_attempt(row["job_id"], attempt_id)
            cleaned += 1
        return cleaned

    def claim_next(
        self,
        *,
        worker_id: str,
        adapter: str,
        adapter_version: str,
        now: datetime,
        lease_seconds: int = 60,
        remove_pending_asset: PendingAssetCleanup | None = None,
        recovery_limit: int = ASSET_INTENT_RECOVERY_LIMIT,
        supports_exact_selector: bool = False,
        skip_unsupported_graph_jobs: bool = False,
    ) -> JobLease | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds < 10:
            raise ValueError("lease_seconds must be at least 10")
        if recovery_limit < 1:
            raise ValueError("recovery_limit must be positive")
        if not isinstance(skip_unsupported_graph_jobs, bool):
            raise ValueError("graph skip policy must be boolean")
        now_text = utc_text(now)
        expires_text = utc_text(now + timedelta(seconds=lease_seconds))

        # Recovery may touch slow or unhealthy storage.  Fence the abandoned
        # attempt in a short transaction, perform cleanup without a SQLite
        # writer lock, then CAS-delete the durable intent.  Recheck queue state
        # inside the claim transaction because it may change during cleanup.
        if self.get_queue_control()["paused"]:
            return None
        if remove_pending_asset is None:
            _, _, recovery_blocked = self._prepare_asset_intent_recovery(
                now_text=now_text,
                recovery_expires_text=now_text,
                limit=recovery_limit,
                claim_cleanup=False,
            )
            if recovery_blocked:
                return None
        else:
            self.recover_asset_commit_intents(
                now=now,
                remove_pending_asset=remove_pending_asset,
                limit=recovery_limit,
            )

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            queue_control = connection.execute(
                "SELECT paused FROM queue_control WHERE id = 1"
            ).fetchone()
            if queue_control is None:
                raise RuntimeError("queue control singleton is missing")
            if queue_control["paused"]:
                return None
            self._recover_expired(connection, now_text)
            while True:
                row = connection.execute(
                    """
                    SELECT
                        j.id, j.source_item_id, j.job_kind, j.attempt_count,
                        j.run_generation, j.generation_attempt_count,
                        j.credential_profile_id,
                        CASE WHEN target.job_id IS NULL
                             THEN s.canonical_url
                             ELSE fetch_source.canonical_url
                        END AS canonical_url,
                        s.platform, s.source_type,
                        target.fetch_source_item_id,
                        target.selector_key,
                        target.expected_media_key,
                        target.expected_media_kind,
                        credential.id AS resolved_credential_profile_id,
                        credential.platform AS credential_platform,
                        credential.secret_ref AS credential_ref,
                        credential.expires_at AS credential_expires_at,
                        credential.disabled_at AS credential_disabled_at
                    FROM download_jobs AS j
                    JOIN source_items AS s ON s.id = j.source_item_id
                    LEFT JOIN download_job_targets AS target ON target.job_id = j.id
                    LEFT JOIN source_items AS fetch_source
                      ON fetch_source.id = target.fetch_source_item_id
                    LEFT JOIN credential_profiles AS credential
                      ON credential.id = j.credential_profile_id
                    LEFT JOIN platform_circuits AS circuit
                      ON circuit.platform = s.platform
                    WHERE j.status = 'queued'
                      AND (
                          ? = 0
                          OR (
                              j.job_kind <> 'discover'
                              AND s.source_type <> ?
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM asset_commit_intents AS pending
                          WHERE pending.job_id = j.id
                      )
                      AND COALESCE(j.available_at, j.created_at) <= ?
                      AND j.generation_attempt_count < ?
                      AND (
                          circuit.platform IS NULL
                          OR circuit.state = 'closed'
                          OR (
                              circuit.state = 'open'
                              AND circuit.requires_manual_reset = 0
                              AND circuit.cooldown_until IS NOT NULL
                              AND circuit.cooldown_until <= ?
                          )
                      )
                      AND (
                          SELECT COUNT(*)
                          FROM download_jobs AS globally_active
                          WHERE globally_active.status IN (
                              'probing', 'downloading', 'postprocessing', 'verifying'
                          )
                      ) < ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM download_jobs AS active
                          JOIN source_items AS active_source
                            ON active_source.id = active.source_item_id
                          WHERE active.status IN (
                              'probing', 'downloading', 'postprocessing', 'verifying'
                          )
                            AND active_source.platform = s.platform
                      )
                    ORDER BY j.created_at, j.rowid
                    LIMIT 1
                    """,
                    (
                        int(skip_unsupported_graph_jobs),
                        X_ATTACHMENT_SOURCE_TYPE,
                        now_text,
                        MAX_ATTEMPTS,
                        now_text,
                        MAX_ACTIVE_JOBS,
                    ),
                ).fetchone()
                if row is None:
                    return None
                is_graph_job = (
                    row["job_kind"] == "discover"
                    or row["source_type"] == X_ATTACHMENT_SOURCE_TYPE
                )
                if is_graph_job and not supports_exact_selector:
                    self._fail_graph_claim_locked(
                        connection,
                        job_id=row["id"],
                        error_code=ErrorCode.ADAPTER_UNSUPPORTED,
                        diagnostic="adapter does not support exact attachment selection",
                        now_text=now_text,
                    )
                    continue
                if is_graph_job and not self._graph_shape_valid_for_claim(row):
                    self._fail_graph_claim_locked(
                        connection,
                        job_id=row["id"],
                        error_code=ErrorCode.VALIDATION_FAILED,
                        diagnostic="graph job target is invalid",
                        now_text=now_text,
                    )
                    continue
                if self._credential_is_valid_for_claim(row, now=now):
                    break
                self._fail_invalid_credential_locked(
                    connection,
                    job_id=row["id"],
                    now_text=now_text,
                )

            attempt_no = int(row["attempt_count"]) + 1
            generation_attempt_no = int(row["generation_attempt_count"]) + 1
            attempt_id = str(uuid4())
            lease_token = str(uuid4())
            updated = connection.execute(
                """
                UPDATE download_jobs
                SET status = 'probing', lease_owner = ?, lease_token = ?,
                    heartbeat_at = ?, lease_expires_at = ?,
                    attempt_count = ?, generation_attempt_count = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                  AND run_generation = ? AND generation_attempt_count = ?
                """,
                (
                    worker_id,
                    lease_token,
                    now_text,
                    expires_text,
                    attempt_no,
                    generation_attempt_no,
                    now_text,
                    row["id"],
                    row["run_generation"],
                    row["generation_attempt_count"],
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("job claim lost inside immediate transaction")
            connection.execute(
                """
                INSERT INTO job_attempts(
                    id, job_id, attempt_no, adapter, adapter_version,
                    status, started_at, lease_token, run_generation,
                    generation_attempt_no
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    row["id"],
                    attempt_no,
                    adapter,
                    adapter_version,
                    now_text,
                    lease_token,
                    row["run_generation"],
                    generation_attempt_no,
                ),
            )
            circuit = connection.execute(
                """
                SELECT state FROM platform_circuits WHERE platform = ?
                """,
                (row["platform"],),
            ).fetchone()
            if circuit is not None and circuit["state"] == "open":
                transitioned = connection.execute(
                    """
                    UPDATE platform_circuits
                    SET state = 'half_open', probe_job_id = ?,
                        probe_lease_token = ?, updated_at = ?
                    WHERE platform = ? AND state = 'open'
                      AND requires_manual_reset = 0
                      AND cooldown_until IS NOT NULL
                      AND cooldown_until <= ?
                    """,
                    (
                        row["id"],
                        lease_token,
                        now_text,
                        row["platform"],
                        now_text,
                    ),
                )
                if transitioned.rowcount != 1:
                    raise RuntimeError("platform circuit probe claim was lost")

        return JobLease(
            job_id=row["id"],
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            lease_token=lease_token,
            worker_id=worker_id,
            lease_expires_at=expires_text,
            source_item_id=row["source_item_id"],
            canonical_url=row["canonical_url"],
            platform=Platform(row["platform"]),
            source_type=SourceType(row["source_type"]),
            adapter=adapter,
            adapter_version=adapter_version,
            job_kind=row["job_kind"],
            run_generation=int(row["run_generation"]),
            selector_key=row["selector_key"],
            expected_media_key=row["expected_media_key"],
            expected_media_kind=row["expected_media_kind"],
            credential_ref=row["credential_ref"],
        )

    @staticmethod
    def _graph_shape_valid_for_claim(row) -> bool:
        if row["job_kind"] == "discover":
            return (
                row["source_type"] == SourceType.X_POST.value
                and row["fetch_source_item_id"] is None
                and row["selector_key"] is None
                and row["expected_media_key"] is None
                and row["expected_media_kind"] is None
            )
        if row["source_type"] == X_ATTACHMENT_SOURCE_TYPE:
            return (
                row["job_kind"] == "download"
                and row["fetch_source_item_id"] is not None
                and row["selector_key"] is not None
                and row["expected_media_key"] is not None
                and row["expected_media_kind"] is not None
                and row["canonical_url"] is not None
            )
        return True

    @staticmethod
    def _fail_graph_claim_locked(
        connection,
        *,
        job_id: str,
        error_code: ErrorCode,
        diagnostic: str,
        now_text: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE download_jobs
            SET status = 'failed', progress = 0, final_error_code = ?,
                lease_owner = NULL, lease_token = NULL,
                heartbeat_at = NULL, lease_expires_at = NULL,
                cancel_requested_at = NULL, available_at = ?, updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (error_code.value, now_text, now_text, job_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("graph capability preflight lost queued job")
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=job_id,
            input_status="failed",
            error_code=error_code.value,
            error_message=diagnostic,
            now_text=now_text,
        )

    @staticmethod
    def _credential_is_valid_for_claim(row, *, now: datetime) -> bool:
        """Validate a profile snapshot without exposing its secret reference."""

        profile_id = row["credential_profile_id"]
        if profile_id is None:
            return True
        if row["resolved_credential_profile_id"] is None:
            return False
        if row["credential_platform"] != row["platform"]:
            return False
        if row["credential_disabled_at"] is not None:
            return False
        credential_ref = row["credential_ref"]
        try:
            validate_opaque_reference(credential_ref)
        except CredentialProfileError:
            return False
        expires_at = row["credential_expires_at"]
        if expires_at is None:
            return True
        if not isinstance(expires_at, str) or not expires_at or len(expires_at) > 64:
            return False
        try:
            normalized = (
                expires_at[:-1] + "+00:00"
                if expires_at.endswith("Z")
                else expires_at
            )
            expiry = datetime.fromisoformat(normalized)
            if expiry.tzinfo is None:
                return False
        except ValueError:
            return False
        return expiry.astimezone(UTC) > now.astimezone(UTC)

    @staticmethod
    def _fail_invalid_credential_locked(
        connection,
        *,
        job_id: str,
        now_text: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE download_jobs
            SET status = 'failed', progress = 0, final_error_code = ?,
                lease_owner = NULL, lease_token = NULL,
                heartbeat_at = NULL, lease_expires_at = NULL,
                cancel_requested_at = NULL, available_at = ?, updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (
                ErrorCode.AUTHENTICATION_REQUIRED.value,
                now_text,
                now_text,
                job_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("credential preflight lost queued job")
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=job_id,
            input_status="failed",
            error_code=ErrorCode.AUTHENTICATION_REQUIRED.value,
            error_message=_CREDENTIAL_FAILURE_DIAGNOSTIC,
            now_text=now_text,
        )

    def recover_asset_commit_intents(
        self,
        *,
        now: datetime,
        remove_pending_asset: PendingAssetCleanup,
        limit: int = ASSET_INTENT_RECOVERY_LIMIT,
        recovery_lease_seconds: int = ASSET_INTENT_RECOVERY_LEASE_SECONDS,
    ) -> int:
        """Recover a bounded set of durable asset intents in three phases.

        A short transaction CAS-claims each stale intent and fences its former
        job attempt.  Filesystem cleanup then runs without any database
        transaction.  A final short transaction deletes the intent only when
        the recovery token still matches.  A process crash leaves a durable,
        expiring claim, so a later recovery can repeat the idempotent cleanup.
        """
        if limit < 1:
            raise ValueError("recovery limit must be positive")
        if recovery_lease_seconds < 10:
            raise ValueError("recovery lease must be at least 10 seconds")
        now_text = utc_text(now)
        recovery_expires_text = utc_text(
            now + timedelta(seconds=recovery_lease_seconds)
        )
        recovered, claims, _ = self._prepare_asset_intent_recovery(
            now_text=now_text,
            recovery_expires_text=recovery_expires_text,
            limit=limit,
            claim_cleanup=True,
        )
        for index, claim in enumerate(claims):
            try:
                remove_pending_asset(claim.asset_id, claim.attempt_id)
            except Exception:
                # A normal cleanup failure is immediately retryable.  A hard
                # process exit leaves the claim in place until its lease
                # expires, which is the crash-recovery path.
                try:
                    self._release_asset_intent_recovery_claims(claims[index:])
                except Exception:
                    pass
                raise
            try:
                self._complete_asset_intent_recovery_claim(claim)
            except Exception:
                # The current intent stays claimed because cleanup may already
                # have completed; release only work that was not attempted.
                try:
                    self._release_asset_intent_recovery_claims(
                        claims[index + 1 :]
                    )
                except Exception:
                    pass
                raise
            recovered += 1
        return recovered

    def heartbeat(
        self,
        lease: JobLease,
        *,
        now: datetime,
        lease_seconds: int = 60,
    ) -> bool:
        if lease_seconds < 10:
            raise ValueError("lease_seconds must be at least 10")
        now_text = utc_text(now)
        expires_text = utc_text(now + timedelta(seconds=lease_seconds))
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE download_jobs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_token = ?
                  AND lease_expires_at > ?
                  AND status IN (
                      'probing', 'downloading', 'postprocessing', 'verifying'
                  )
                RETURNING cancel_requested_at
                """,
                (
                    now_text,
                    expires_text,
                    now_text,
                    lease.job_id,
                    lease.lease_token,
                    now_text,
                ),
            ).fetchone()
        if row is None:
            raise LostLease(lease.job_id)
        return row["cancel_requested_at"] is not None

    def transition(
        self,
        lease: JobLease,
        *,
        status: JobStatus,
        progress: float,
        now: datetime,
        lease_seconds: int = 60,
    ) -> None:
        if status not in ACTIVE_STATUSES:
            raise InvalidTransition(f"worker cannot transition directly to {status}")
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        if lease_seconds < 10:
            raise ValueError("lease_seconds must be at least 10")
        now_text = utc_text(now)
        expires_text = utc_text(now + timedelta(seconds=lease_seconds))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT status, progress FROM download_jobs
                WHERE id = ? AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (lease.job_id, lease.lease_token, now_text),
            ).fetchone()
            if current is None or JobStatus(current["status"]) not in ACTIVE_STATUSES:
                raise LostLease(lease.job_id)
            current_status = JobStatus(current["status"])
            if not self._transition_allowed(current_status, status):
                raise InvalidTransition(f"{current_status} -> {status}")
            if progress < float(current["progress"]):
                raise InvalidTransition("progress cannot decrease")
            updated = connection.execute(
                """
                UPDATE download_jobs
                SET status = ?, progress = ?, heartbeat_at = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    status,
                    progress,
                    now_text,
                    expires_text,
                    now_text,
                    lease.job_id,
                    lease.lease_token,
                    now_text,
                ),
            )
            if updated.rowcount != 1:
                raise LostLease(lease.job_id)

    def request_cancel(self, job_id: str, *, now: datetime) -> JobStatus | None:
        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM download_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            current = JobStatus(row["status"])
            if current == JobStatus.QUEUED:
                connection.execute(
                    """
                    UPDATE download_jobs
                    SET status = 'canceled',
                        cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now_text, now_text, job_id),
                )
                self._mark_input_and_refresh_batch(
                    connection,
                    job_id=job_id,
                    input_status="canceled",
                    error_code=None,
                    error_message=None,
                    now_text=now_text,
                )
                return JobStatus.CANCELED
            if current in ACTIVE_STATUSES:
                connection.execute(
                    """
                    UPDATE download_jobs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now_text, now_text, job_id),
                )
            return current

    def request_cancel_input(
        self, input_record_id: str, *, now: datetime
    ) -> InputStatus | None:
        """Cancel all non-terminal work owned by one Input atomically."""

        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            input_row = connection.execute(
                """
                SELECT id, status FROM input_records WHERE id = ?
                """,
                (input_record_id,),
            ).fetchone()
            if input_row is None:
                return None
            current = InputStatus(input_row["status"])
            if current == InputStatus.DUPLICATE:
                return current
            connection.execute(
                """
                UPDATE input_records
                SET cancel_requested_at = COALESCE(cancel_requested_at, ?)
                WHERE id = ?
                """,
                (now_text, input_record_id),
            )
            connection.execute(
                """
                UPDATE download_jobs
                SET status = 'canceled', final_error_code = NULL,
                    cancel_requested_at = COALESCE(cancel_requested_at, ?),
                    updated_at = ?
                WHERE input_record_id = ? AND status = 'queued'
                """,
                (now_text, now_text, input_record_id),
            )
            connection.execute(
                """
                UPDATE download_jobs
                SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                    updated_at = ?
                WHERE input_record_id = ?
                  AND status IN (
                      'probing', 'downloading', 'postprocessing', 'verifying'
                  )
                """,
                (now_text, now_text, input_record_id),
            )
            job = connection.execute(
                """
                SELECT id FROM download_jobs
                WHERE input_record_id = ?
                ORDER BY CASE WHEN job_kind = 'discover' THEN 0 ELSE 1 END,
                         created_at, id
                LIMIT 1
                """,
                (input_record_id,),
            ).fetchone()
            if job is None:
                connection.execute(
                    """
                    UPDATE input_records
                    SET status = 'canceled', error_code = NULL,
                        error_message = NULL
                    WHERE id = ?
                    """,
                    (input_record_id,),
                )
                self._refresh_batch_locked(
                    connection,
                    input_record_id=input_record_id,
                    now_text=now_text,
                )
                return InputStatus.CANCELED
            self._mark_input_and_refresh_batch(
                connection,
                job_id=job["id"],
                input_status="canceled",
                error_code=None,
                error_message=None,
                now_text=now_text,
            )
            refreshed = connection.execute(
                "SELECT status FROM input_records WHERE id = ?",
                (input_record_id,),
            ).fetchone()
            assert refreshed is not None
            return InputStatus(refreshed["status"])

    def request_rediscover(self, input_record_id: str, *, now: datetime) -> int:
        """Queue a new parent discovery generation after all work is terminal."""

        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute(
                """
                SELECT j.id, j.run_generation, j.attempt_count,
                       i.active_run_generation
                FROM input_records AS i
                JOIN download_jobs AS j
                  ON j.input_record_id = i.id AND j.job_kind = 'discover'
                WHERE i.id = ?
                ORDER BY j.created_at, j.id
                LIMIT 1
                """,
                (input_record_id,),
            ).fetchone()
            if parent is None:
                raise InvalidTransition("input is not graph-v2")
            nonterminal = connection.execute(
                """
                SELECT 1 FROM download_jobs
                WHERE input_record_id = ?
                  AND status IN (
                      'queued', 'probing', 'downloading',
                      'postprocessing', 'verifying'
                  )
                LIMIT 1
                """,
                (input_record_id,),
            ).fetchone()
            if nonterminal is not None:
                raise RediscoverConflict("input still has non-terminal work")
            if parent["run_generation"] != parent["active_run_generation"]:
                raise RuntimeError("input and parent generation are inconsistent")
            next_generation = int(parent["run_generation"]) + 1
            updated = connection.execute(
                """
                UPDATE download_jobs
                SET status = 'queued', progress = 0, final_error_code = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    heartbeat_at = NULL, lease_expires_at = NULL,
                    cancel_requested_at = NULL, available_at = ?,
                    run_generation = ?, generation_attempt_count = 0,
                    updated_at = ?
                WHERE id = ? AND job_kind = 'discover'
                  AND run_generation = ?
                  AND status IN ('ready', 'failed', 'canceled')
                """,
                (
                    now_text,
                    next_generation,
                    now_text,
                    parent["id"],
                    parent["run_generation"],
                ),
            )
            if updated.rowcount != 1:
                raise RediscoverConflict("parent discovery is not terminal")
            input_updated = connection.execute(
                """
                UPDATE input_records
                SET expected_item_count = NULL, active_discovery_id = NULL,
                    active_run_generation = ?, cancel_requested_at = NULL,
                    status = 'queued', error_code = NULL, error_message = NULL
                WHERE id = ? AND active_run_generation = ?
                """,
                (
                    next_generation,
                    input_record_id,
                    parent["active_run_generation"],
                ),
            )
            if input_updated.rowcount != 1:
                raise RuntimeError("input generation update was lost")
            self._mark_input_and_refresh_batch(
                connection,
                job_id=parent["id"],
                input_status="ready",
                error_code=None,
                error_message=None,
                now_text=now_text,
            )
            return next_generation

    def record_probe(
        self,
        lease: JobLease,
        *,
        expected_item_count: int,
        discovery_snapshot_hash: str,
        sanitized_source: Mapping[str, object],
        now: datetime,
    ) -> None:
        if expected_item_count < 1:
            raise ValueError("probe must discover at least one item")
        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """
                SELECT input_record_id, source_item_id
                FROM download_jobs
                WHERE id = ? AND lease_token = ? AND status = 'probing'
                  AND lease_expires_at > ?
                """,
                (lease.job_id, lease.lease_token, now_text),
            ).fetchone()
            if job is None:
                raise LostLease(lease.job_id)
            connection.execute(
                """
                UPDATE input_records SET expected_item_count = ?
                WHERE id = ?
                """,
                (expected_item_count, job["input_record_id"]),
            )
            connection.execute(
                """
                UPDATE source_items
                SET title = ?, author = ?, published_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    sanitized_source.get("title"),
                    sanitized_source.get("author"),
                    sanitized_source.get("published_at"),
                    now_text,
                    job["source_item_id"],
                ),
            )
            updated = connection.execute(
                """
                UPDATE job_attempts
                SET discovered_item_count = ?, discovery_snapshot_hash = ?
                WHERE id = ? AND lease_token = ? AND status = 'running'
                """,
                (
                    expected_item_count,
                    discovery_snapshot_hash,
                    lease.attempt_id,
                    lease.lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise LostLease(lease.job_id)

    def commit_discovery(
        self,
        lease: JobLease,
        *,
        probe_items: Sequence[XAttachmentProbeItem],
        discovery_snapshot_hash: str,
        sanitized_source: Mapping[str, object],
        now: datetime,
    ) -> JobStatus:
        """Atomically commit one X discovery and its Input-scoped fan-out.

        The repository derives the graph identities itself.  The adapter's
        hash is only a cross-boundary assertion and never an authority for
        persisted membership.
        """

        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute(
                """
                SELECT
                    j.id, j.batch_id, j.input_record_id, j.source_item_id,
                    j.job_kind, j.status, j.route_policy_version,
                    j.credential_profile_id, j.lease_token,
                    j.lease_expires_at, j.cancel_requested_at,
                    j.run_generation,
                    i.active_discovery_id, i.active_run_generation,
                    i.cancel_requested_at AS input_cancel_requested_at,
                    s.platform, s.source_type, s.source_id, s.canonical_url,
                    a.status AS attempt_status,
                    a.lease_token AS attempt_lease_token,
                    a.run_generation AS attempt_run_generation,
                    a.discovery_snapshot_hash AS attempt_snapshot_hash
                FROM download_jobs AS j
                JOIN input_records AS i ON i.id = j.input_record_id
                JOIN source_items AS s ON s.id = j.source_item_id
                JOIN job_attempts AS a ON a.id = ? AND a.job_id = j.id
                WHERE j.id = ?
                """,
                (lease.attempt_id, lease.job_id),
            ).fetchone()
            if parent is None:
                raise LostLease(lease.job_id)
            if (
                parent["job_kind"] != "discover"
                or parent["platform"] != Platform.X.value
                or parent["source_type"] != SourceType.X_POST.value
                or parent["run_generation"] != lease.run_generation
                or parent["attempt_run_generation"] != lease.run_generation
                or parent["attempt_lease_token"] != lease.lease_token
            ):
                raise LostLease(lease.job_id)

            live = (
                parent["status"] == JobStatus.PROBING.value
                and parent["attempt_status"] == "running"
                and parent["lease_token"] == lease.lease_token
                and parent["lease_expires_at"] is not None
                and parent["lease_expires_at"] > now_text
                and parent["active_run_generation"] == lease.run_generation
            )
            if live and (
                parent["cancel_requested_at"] is not None
                or parent["input_cancel_requested_at"] is not None
            ):
                self._finish_canceled_locked(connection, lease, now_text=now_text)
                return JobStatus.CANCELED

            discovery = build_x_attachment_discovery(
                parent=XPostIdentity(parent["source_id"]),
                parent_canonical_url=parent["canonical_url"],
                probe_items=probe_items,
            )
            if (
                discovery.snapshot_hash != discovery_snapshot_hash
                or discovery.snapshot_hash
                != x_attachment_snapshot_hash(discovery.members)
            ):
                raise GraphValidationError(
                    "attachment discovery snapshot is inconsistent"
                )

            if not live:
                replay = (
                    parent["status"] == JobStatus.READY.value
                    and parent["attempt_status"] == "succeeded"
                    and parent["attempt_snapshot_hash"] == discovery.snapshot_hash
                    and parent["active_discovery_id"] is not None
                    and parent["active_run_generation"] == lease.run_generation
                )
                if replay:
                    return JobStatus.READY
                raise LostLease(lease.job_id)

            members_payload = [
                {
                    "child_source_id": member.source_id,
                    "media_kind": member.media_kind,
                    "selector_key": member.selector_key,
                }
                for member in discovery.members
            ]
            members_json = x_attachment_members_json(discovery.members)
            discovery_id, created_discovery = self._get_or_create_discovery_locked(
                connection,
                parent_source_item_id=parent["source_item_id"],
                snapshot_hash=discovery.snapshot_hash,
                members_json=members_json,
                members_payload=members_payload,
                discovered_by_attempt_id=lease.attempt_id,
                now_text=now_text,
            )

            child_source_ids: list[str] = []
            for member in discovery.members:
                child_source_ids.append(
                    self._get_or_create_child_source_locked(
                        connection,
                        member=member,
                        now_text=now_text,
                    )
                )

            relations = self._get_or_create_relations_locked(
                connection,
                discovery_id=discovery_id,
                created_discovery=created_discovery,
                parent_source_item_id=parent["source_item_id"],
                child_source_ids=child_source_ids,
                members=discovery.members,
                attempt_id=lease.attempt_id,
                now_text=now_text,
            )
            for relation, member, child_source_id in zip(
                relations, discovery.members, child_source_ids, strict=True
            ):
                child_job_id = self._get_or_create_child_job_locked(
                    connection,
                    batch_id=parent["batch_id"],
                    input_record_id=parent["input_record_id"],
                    child_source_item_id=child_source_id,
                    fetch_source_item_id=parent["source_item_id"],
                    member=member,
                    route_policy_version=parent["route_policy_version"],
                    credential_profile_id=parent["credential_profile_id"],
                    run_generation=lease.run_generation,
                    now_text=now_text,
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO input_relation_jobs(
                        input_record_id, discovery_id, run_generation,
                        relation_id, job_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parent["input_record_id"],
                        discovery_id,
                        lease.run_generation,
                        relation["id"],
                        child_job_id,
                        now_text,
                    ),
                )
                link = connection.execute(
                    """
                    SELECT discovery_id, job_id
                    FROM input_relation_jobs
                    WHERE input_record_id = ? AND run_generation = ?
                      AND relation_id = ?
                    """,
                    (
                        parent["input_record_id"],
                        lease.run_generation,
                        relation["id"],
                    ),
                ).fetchone()
                if (
                    link is None
                    or link["discovery_id"] != discovery_id
                    or link["job_id"] != child_job_id
                ):
                    raise RuntimeError("discovery relation link is inconsistent")

            connection.execute(
                """
                UPDATE source_items
                SET title = ?, author = ?, published_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    sanitized_source.get("title"),
                    sanitized_source.get("author"),
                    sanitized_source.get("published_at"),
                    now_text,
                    parent["source_item_id"],
                ),
            )
            input_updated = connection.execute(
                """
                UPDATE input_records
                SET expected_item_count = ?, active_discovery_id = ?,
                    status = 'queued', error_code = NULL, error_message = NULL
                WHERE id = ? AND active_run_generation = ?
                  AND cancel_requested_at IS NULL
                """,
                (
                    discovery.expected_item_count,
                    discovery_id,
                    parent["input_record_id"],
                    lease.run_generation,
                ),
            )
            if input_updated.rowcount != 1:
                raise LostLease(lease.job_id)
            attempt_updated = connection.execute(
                """
                UPDATE job_attempts
                SET status = 'succeeded', finished_at = ?, error_code = NULL,
                    diagnostic = NULL, exit_code = 0,
                    discovered_item_count = ?, discovery_snapshot_hash = ?
                WHERE id = ? AND job_id = ? AND lease_token = ?
                  AND status = 'running' AND run_generation = ?
                """,
                (
                    now_text,
                    discovery.expected_item_count,
                    discovery.snapshot_hash,
                    lease.attempt_id,
                    lease.job_id,
                    lease.lease_token,
                    lease.run_generation,
                ),
            )
            if attempt_updated.rowcount != 1:
                raise LostLease(lease.job_id)
            self._release_half_open_locked(
                connection,
                job_id=lease.job_id,
                lease_token=lease.lease_token,
                now_text=now_text,
            )
            job_updated = connection.execute(
                """
                UPDATE download_jobs
                SET status = 'ready', progress = 1, final_error_code = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    heartbeat_at = NULL, lease_expires_at = NULL,
                    cancel_requested_at = NULL, updated_at = ?
                WHERE id = ? AND job_kind = 'discover'
                  AND status = 'probing' AND lease_token = ?
                  AND lease_expires_at > ? AND run_generation = ?
                """,
                (
                    now_text,
                    lease.job_id,
                    lease.lease_token,
                    now_text,
                    lease.run_generation,
                ),
            )
            if job_updated.rowcount != 1:
                raise LostLease(lease.job_id)
            self._mark_input_and_refresh_batch(
                connection,
                job_id=lease.job_id,
                input_status="ready",
                error_code=None,
                error_message=None,
                now_text=now_text,
            )
            return JobStatus.READY

    @staticmethod
    def _get_or_create_discovery_locked(
        connection,
        *,
        parent_source_item_id: str,
        snapshot_hash: str,
        members_json: str,
        members_payload: list[dict[str, str]],
        discovered_by_attempt_id: str,
        now_text: str,
    ) -> tuple[str, bool]:
        existing = connection.execute(
            """
            SELECT id, members_json, member_count FROM source_discoveries
            WHERE parent_source_item_id = ? AND relation_type = ?
              AND snapshot_hash = ?
            """,
            (
                parent_source_item_id,
                X_ATTACHMENT_RELATION_TYPE,
                snapshot_hash,
            ),
        ).fetchone()
        if existing is not None:
            try:
                stored_members = json.loads(existing["members_json"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("stored discovery membership is invalid") from exc
            if (
                stored_members != members_payload
                or existing["members_json"] != members_json
                or existing["member_count"] != len(members_payload)
            ):
                raise RuntimeError("stored discovery membership is inconsistent")
            return existing["id"], False
        discovery_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO source_discoveries(
                id, parent_source_item_id, relation_type,
                snapshot_hash, members_json, member_count,
                discovered_by_attempt_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discovery_id,
                parent_source_item_id,
                X_ATTACHMENT_RELATION_TYPE,
                snapshot_hash,
                members_json,
                len(members_payload),
                discovered_by_attempt_id,
                now_text,
            ),
        )
        return discovery_id, True

    @staticmethod
    def _get_or_create_child_source_locked(
        connection,
        *,
        member,
        now_text: str,
    ) -> str:
        existing = connection.execute(
            """
            SELECT id, platform, source_type, source_id, canonical_url
            FROM source_items
            WHERE platform = 'x' AND source_type = ? AND source_id = ?
            """,
            (X_ATTACHMENT_SOURCE_TYPE, member.source_id),
        ).fetchone()
        if existing is not None:
            if existing["canonical_url"] != member.canonical_url:
                raise RuntimeError("stored attachment source is inconsistent")
            return existing["id"]
        canonical_conflict = connection.execute(
            "SELECT id FROM source_items WHERE canonical_url = ?",
            (member.canonical_url,),
        ).fetchone()
        if canonical_conflict is not None:
            raise RuntimeError("attachment canonical locator is already owned")
        source_item_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO source_items(
                id, platform, source_type, source_id, canonical_url,
                created_at, updated_at
            ) VALUES (?, 'x', ?, ?, ?, ?, ?)
            """,
            (
                source_item_id,
                X_ATTACHMENT_SOURCE_TYPE,
                member.source_id,
                member.canonical_url,
                now_text,
                now_text,
            ),
        )
        return source_item_id

    @staticmethod
    def _get_or_create_relations_locked(
        connection,
        *,
        discovery_id: str,
        created_discovery: bool,
        parent_source_item_id: str,
        child_source_ids: Sequence[str],
        members: Sequence,
        attempt_id: str,
        now_text: str,
    ):
        if created_discovery:
            for member, child_source_id in zip(
                members, child_source_ids, strict=True
            ):
                connection.execute(
                    """
                    INSERT INTO source_relations(
                        id, discovery_id, parent_source_item_id,
                        child_source_item_id, relation_type, ordinal,
                        media_kind, discovered_by_attempt_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        discovery_id,
                        parent_source_item_id,
                        child_source_id,
                        X_ATTACHMENT_RELATION_TYPE,
                        member.ordinal,
                        member.media_kind,
                        attempt_id,
                        now_text,
                    ),
                )
        rows = connection.execute(
            """
            SELECT id, parent_source_item_id, child_source_item_id,
                   relation_type, ordinal, media_kind
            FROM source_relations
            WHERE discovery_id = ?
            ORDER BY ordinal, id
            """,
            (discovery_id,),
        ).fetchall()
        if len(rows) != len(members):
            raise RuntimeError("stored discovery relations are incomplete")
        for row, member, child_source_id in zip(
            rows, members, child_source_ids, strict=True
        ):
            if (
                row["parent_source_item_id"] != parent_source_item_id
                or row["child_source_item_id"] != child_source_id
                or row["relation_type"] != X_ATTACHMENT_RELATION_TYPE
                or row["ordinal"] != member.ordinal
                or row["media_kind"] != member.media_kind
            ):
                raise RuntimeError("stored discovery relation is inconsistent")
        return rows

    @staticmethod
    def _get_or_create_child_job_locked(
        connection,
        *,
        batch_id: str,
        input_record_id: str,
        child_source_item_id: str,
        fetch_source_item_id: str,
        member,
        route_policy_version: str,
        credential_profile_id: str | None,
        run_generation: int,
        now_text: str,
    ) -> str:
        historical = connection.execute(
            """
            SELECT j.id, j.batch_id, j.run_generation,
                   j.route_policy_version, j.credential_profile_id,
                   target.fetch_source_item_id, target.selector_key,
                   target.expected_media_key, target.expected_media_kind
            FROM download_jobs AS j
            LEFT JOIN download_job_targets AS target ON target.job_id = j.id
            WHERE j.input_record_id = ? AND j.source_item_id = ?
              AND j.job_kind = 'download'
            ORDER BY j.run_generation DESC, j.created_at DESC, j.id
            """,
            (input_record_id, child_source_item_id),
        ).fetchall()
        for row in historical:
            target_matches = (
                row["fetch_source_item_id"] == fetch_source_item_id
                and row["selector_key"] == member.selector_key
                and row["expected_media_key"] == member.expected_media_key
                and row["expected_media_kind"] == member.media_kind
            )
            policy_matches = (
                row["batch_id"] == batch_id
                and row["route_policy_version"] == route_policy_version
                and row["credential_profile_id"] == credential_profile_id
            )
            if target_matches and policy_matches:
                return row["id"]
            if row["run_generation"] == run_generation:
                raise RuntimeError(
                    "current generation attachment target or policy conflicts"
                )

        reusable = connection.execute(
            """
            SELECT donor.id
            FROM download_jobs AS donor
            JOIN download_job_targets AS target ON target.job_id = donor.id
            WHERE donor.input_record_id <> ?
              AND donor.source_item_id = ?
              AND donor.job_kind = 'download'
              AND donor.status = 'ready'
              AND donor.route_policy_version = ?
              AND donor.credential_profile_id IS ?
              AND target.fetch_source_item_id = ?
              AND target.selector_key = ?
              AND target.expected_media_key = ?
              AND target.expected_media_kind = ?
              AND EXISTS (
                  SELECT 1
                  FROM job_assets AS original_link
                  JOIN media_assets AS asset
                    ON asset.id = original_link.asset_id
                  WHERE original_link.job_id = donor.id
                    AND original_link.role = 'original'
                    AND asset.source_item_id = donor.source_item_id
                    AND asset.media_key = target.expected_media_key
                    AND asset.status = 'ready'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM job_assets AS any_link
                  JOIN media_assets AS any_asset
                    ON any_asset.id = any_link.asset_id
                  WHERE any_link.job_id = donor.id
                    AND any_asset.source_item_id <> donor.source_item_id
              )
            ORDER BY donor.created_at, donor.id
            LIMIT 1
            """,
            (
                input_record_id,
                child_source_item_id,
                route_policy_version,
                credential_profile_id,
                fetch_source_item_id,
                member.selector_key,
                member.expected_media_key,
                member.media_kind,
            ),
        ).fetchone()
        child_job_id = str(uuid4())
        reused_from_job_id = reusable["id"] if reusable is not None else None
        status = JobStatus.READY.value if reusable is not None else JobStatus.QUEUED.value
        progress = 1 if reusable is not None else 0
        connection.execute(
            """
            INSERT INTO download_jobs(
                id, batch_id, input_record_id, source_item_id, job_kind,
                status, progress, route_policy_version,
                credential_profile_id, available_at, created_at, updated_at,
                run_generation, generation_attempt_count, reused_from_job_id
            ) VALUES (?, ?, ?, ?, 'download', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                child_job_id,
                batch_id,
                input_record_id,
                child_source_item_id,
                status,
                progress,
                route_policy_version,
                credential_profile_id,
                now_text,
                now_text,
                now_text,
                run_generation,
                reused_from_job_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO download_job_targets(
                job_id, fetch_source_item_id, selector_key,
                expected_media_key, expected_media_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                child_job_id,
                fetch_source_item_id,
                member.selector_key,
                member.expected_media_key,
                member.media_kind,
                now_text,
            ),
        )
        if reused_from_job_id is not None:
            connection.execute(
                """
                INSERT INTO job_assets(job_id, asset_id, role, ordinal)
                SELECT ?, asset_id, role, ordinal
                FROM job_assets WHERE job_id = ?
                """,
                (child_job_id, reused_from_job_id),
            )
        return child_job_id

    def record_asset_commit_intent(
        self,
        lease: JobLease,
        *,
        asset: "StagedAsset",
        now: datetime,
    ) -> None:
        """Durably register a staged asset before it can be published."""
        if asset.attempt_id != lease.attempt_id:
            raise LostLease(lease.job_id)
        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT j.id
                FROM download_jobs AS j
                JOIN job_attempts AS a
                  ON a.id = ? AND a.job_id = j.id
                WHERE j.id = ?
                  AND j.status = 'verifying'
                  AND j.lease_token = ?
                  AND j.lease_expires_at > ?
                  AND a.status = 'running'
                  AND a.lease_token = ?
                """,
                (
                    lease.attempt_id,
                    lease.job_id,
                    lease.lease_token,
                    now_text,
                    lease.lease_token,
                ),
            ).fetchone()
            if current is None:
                raise LostLease(lease.job_id)
            existing = connection.execute(
                """
                SELECT job_id, attempt_id, lease_token
                FROM asset_commit_intents WHERE asset_id = ?
                """,
                (asset.asset_id,),
            ).fetchone()
            identity = (lease.job_id, lease.attempt_id, lease.lease_token)
            if existing is not None:
                if tuple(existing) == identity:
                    return
                raise LostLease(lease.job_id)
            connection.execute(
                """
                INSERT INTO asset_commit_intents(
                    asset_id, job_id, attempt_id, lease_token, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    asset.asset_id,
                    lease.job_id,
                    lease.attempt_id,
                    lease.lease_token,
                    now_text,
                ),
            )

    def finalize_asset_commit_intent(
        self,
        lease: JobLease,
        *,
        asset: "StagedAsset",
        publish_staged: StagedAssetPublisher,
        remove_pending_asset: PendingAssetCleanup,
        now: datetime,
    ) -> JobStatus:
        """Finalize the single-asset Worker path through the batch-safe API."""
        return self.finalize_asset_commit_intents(
            lease,
            assets=[asset],
            publish_staged=publish_staged,
            remove_pending_asset=remove_pending_asset,
            now=now,
        )

    def finalize_asset_commit_intents(
        self,
        lease: JobLease,
        *,
        assets: Sequence["StagedAsset"],
        publish_staged: StagedAssetPublisher,
        remove_pending_asset: PendingAssetCleanup,
        now: datetime,
    ) -> JobStatus:
        """Publish and register every intent for one attempt atomically.

        The exact intent set is required so a future multi-attachment Worker
        cannot accidentally make the job terminal after registering only a
        subset.  The publish callbacks run under SQLite's writer lock; SQL
        failure rolls the transaction back while the independently committed
        intents remain available to recovery.
        """
        if not assets:
            raise ValueError("at least one staged asset is required")
        asset_ids = [asset.asset_id for asset in assets]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("duplicate staged asset id")
        if any(asset.attempt_id != lease.attempt_id for asset in assets):
            raise LostLease(lease.job_id)
        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT j.source_item_id, j.cancel_requested_at
                FROM download_jobs AS j
                JOIN job_attempts AS a
                  ON a.id = ? AND a.job_id = j.id
                WHERE j.id = ?
                  AND j.status = 'verifying'
                  AND j.lease_token = ?
                  AND j.lease_expires_at > ?
                  AND a.status = 'running'
                  AND a.lease_token = ?
                """,
                (
                    lease.attempt_id,
                    lease.job_id,
                    lease.lease_token,
                    now_text,
                    lease.lease_token,
                ),
            ).fetchone()
            if current is None:
                raise LostLease(lease.job_id)
            intent_ids = {
                row["asset_id"]
                for row in connection.execute(
                    """
                    SELECT asset_id FROM asset_commit_intents
                    WHERE job_id = ? AND attempt_id = ? AND lease_token = ?
                    """,
                    (lease.job_id, lease.attempt_id, lease.lease_token),
                ).fetchall()
            }
            if intent_ids != set(asset_ids):
                raise LostLease(lease.job_id)
            if current["cancel_requested_at"] is not None:
                self._finish_canceled_locked(connection, lease, now_text=now_text)
                self._remove_and_delete_intents_locked(
                    connection,
                    job_id=lease.job_id,
                    attempt_id=lease.attempt_id,
                    lease_token=lease.lease_token,
                    remove_pending_asset=remove_pending_asset,
                )
                return JobStatus.CANCELED

            committed_assets: list["CommittedAsset"] = []
            for staged in assets:
                committed = publish_staged(staged)
                if (
                    committed.asset_id != staged.asset_id
                    or committed.attempt_id != lease.attempt_id
                ):
                    raise RuntimeError("publisher returned a different asset")
                committed_assets.append(committed)
            for committed in committed_assets:
                self._insert_asset_records_locked(
                    connection,
                    lease=lease,
                    source_item_id=current["source_item_id"],
                    asset=committed,
                    now_text=now_text,
                )
            self._finish_ready_locked(connection, lease, now_text=now_text)
            deleted = connection.execute(
                """
                DELETE FROM asset_commit_intents
                WHERE job_id = ? AND attempt_id = ? AND lease_token = ?
                """,
                (lease.job_id, lease.attempt_id, lease.lease_token),
            )
            if deleted.rowcount != len(asset_ids):
                raise LostLease(lease.job_id)
        return JobStatus.READY

    def finish_success_with_asset(
        self,
        lease: JobLease,
        *,
        asset: "CommittedAsset",
        now: datetime,
        remove_pending_asset: PendingAssetCleanup | None = None,
    ) -> JobStatus:
        now_text = utc_text(now)
        verification = asset.verification
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT source_item_id, cancel_requested_at FROM download_jobs
                WHERE id = ? AND lease_token = ? AND status = 'verifying'
                  AND lease_expires_at > ?
                """,
                (lease.job_id, lease.lease_token, now_text),
            ).fetchone()
            if current is None:
                raise LostLease(lease.job_id)
            if current["cancel_requested_at"] is not None:
                self._finish_canceled_locked(connection, lease, now_text=now_text)
                if remove_pending_asset is not None:
                    self._remove_and_delete_intents_locked(
                        connection,
                        job_id=lease.job_id,
                        attempt_id=lease.attempt_id,
                        lease_token=lease.lease_token,
                        remove_pending_asset=remove_pending_asset,
                )
                return JobStatus.CANCELED
            self._record_platform_success_locked(
                connection, lease=lease, now_text=now_text
            )
            connection.execute(
                """
                INSERT INTO media_assets(
                    id, source_item_id, media_key, media_kind,
                    duration_seconds, container, codec, width, height,
                    size_bytes, sha256, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)
                """,
                (
                    asset.asset_id,
                    current["source_item_id"],
                    asset.media_key,
                    asset.media_kind,
                    verification.duration_seconds,
                    verification.container,
                    verification.codec,
                    verification.width,
                    verification.height,
                    asset.size_bytes,
                    asset.sha256,
                    now_text,
                ),
            )
            connection.execute(
                """
                INSERT INTO job_assets(job_id, asset_id, role, ordinal)
                VALUES (?, ?, ?, ?)
                """,
                (lease.job_id, asset.asset_id, asset.role, asset.ordinal),
            )
            self._insert_artifact_records_locked(
                connection,
                lease=lease,
                asset=asset,
                now_text=now_text,
            )
            updated = connection.execute(
                """
                UPDATE download_jobs
                SET status = 'ready', progress = 1, final_error_code = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    heartbeat_at = NULL, lease_expires_at = NULL,
                    cancel_requested_at = NULL, updated_at = ?
                WHERE id = ? AND lease_token = ? AND status = 'verifying'
                  AND cancel_requested_at IS NULL
                  AND lease_expires_at > ?
                """,
                (now_text, lease.job_id, lease.lease_token, now_text),
            )
            if updated.rowcount != 1:
                raise LostLease(lease.job_id)
            self._finish_attempt(
                connection,
                lease,
                status="succeeded",
                now_text=now_text,
                error_code=None,
                diagnostic=None,
                exit_code=0,
            )
            self._mark_input_and_refresh_batch(
                connection,
                job_id=lease.job_id,
                input_status="ready",
                error_code=None,
                error_message=None,
                now_text=now_text,
            )
        return JobStatus.READY

    def finish_success(
        self,
        lease: JobLease,
        *,
        now: datetime,
        remove_pending_asset: PendingAssetCleanup | None = None,
    ) -> JobStatus:
        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT cancel_requested_at FROM download_jobs
                WHERE id = ? AND lease_token = ? AND status = 'verifying'
                  AND lease_expires_at > ?
                """,
                (lease.job_id, lease.lease_token, now_text),
            ).fetchone()
            if current is None:
                raise LostLease(lease.job_id)
            if current["cancel_requested_at"] is not None:
                self._finish_canceled_locked(connection, lease, now_text=now_text)
                if remove_pending_asset is not None:
                    self._remove_and_delete_intents_locked(
                        connection,
                        job_id=lease.job_id,
                        attempt_id=lease.attempt_id,
                        lease_token=lease.lease_token,
                        remove_pending_asset=remove_pending_asset,
                )
                return JobStatus.CANCELED
            self._record_platform_success_locked(
                connection, lease=lease, now_text=now_text
            )
            updated = connection.execute(
                """
                UPDATE download_jobs
                SET status = 'ready', progress = 1, final_error_code = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    heartbeat_at = NULL, lease_expires_at = NULL,
                    cancel_requested_at = NULL, updated_at = ?
                WHERE id = ? AND lease_token = ? AND status = 'verifying'
                  AND cancel_requested_at IS NULL
                  AND lease_expires_at > ?
                """,
                (now_text, lease.job_id, lease.lease_token, now_text),
            )
            if updated.rowcount != 1:
                raise LostLease(lease.job_id)
            self._finish_attempt(
                connection,
                lease,
                status="succeeded",
                now_text=now_text,
                error_code=None,
                diagnostic=None,
                exit_code=0,
            )
            self._mark_input_and_refresh_batch(
                connection,
                job_id=lease.job_id,
                input_status="ready",
                error_code=None,
                error_message=None,
                now_text=now_text,
            )
        return JobStatus.READY

    def finish_failure(
        self,
        lease: JobLease,
        *,
        error_code: ErrorCode,
        diagnostic: str,
        now: datetime,
        retry_at: datetime | None = None,
        exit_code: int | None = None,
        remove_pending_asset: PendingAssetCleanup | None = None,
    ) -> JobStatus:
        now_text = utc_text(now)
        diagnostic = sanitize_diagnostic(diagnostic)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT generation_attempt_count, cancel_requested_at
                FROM download_jobs
                WHERE id = ? AND lease_token = ?
                  AND lease_expires_at > ?
                  AND status IN (
                      'probing', 'downloading', 'postprocessing', 'verifying'
                  )
                """,
                (lease.job_id, lease.lease_token, now_text),
            ).fetchone()
            if current is None:
                raise LostLease(lease.job_id)
            if current["cancel_requested_at"] is not None:
                self._finish_canceled_locked(connection, lease, now_text=now_text)
                if remove_pending_asset is not None:
                    self._remove_and_delete_intents_locked(
                        connection,
                        job_id=lease.job_id,
                        attempt_id=lease.attempt_id,
                        lease_token=lease.lease_token,
                        remove_pending_asset=remove_pending_asset,
                    )
                return JobStatus.CANCELED
            should_retry = (
                retry_at is not None
                and int(current["generation_attempt_count"]) < MAX_ATTEMPTS
            )
            next_status = JobStatus.QUEUED if should_retry else JobStatus.FAILED
            available_at = utc_text(retry_at) if should_retry else now_text
            self._record_platform_failure_locked(
                connection,
                lease=lease,
                error_code=error_code,
                now=now,
                retry_at=retry_at,
            )
            connection.execute(
                """
                UPDATE download_jobs
                SET status = ?, progress = 0, final_error_code = ?,
                    lease_owner = NULL, lease_token = NULL,
                    heartbeat_at = NULL, lease_expires_at = NULL,
                    cancel_requested_at = NULL, available_at = ?, updated_at = ?
                WHERE id = ? AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    next_status,
                    None if should_retry else error_code,
                    available_at,
                    now_text,
                    lease.job_id,
                    lease.lease_token,
                    now_text,
                ),
            )
            self._finish_attempt(
                connection,
                lease,
                status="failed",
                now_text=now_text,
                error_code=error_code,
                diagnostic=diagnostic,
                exit_code=exit_code,
            )
            if not should_retry:
                self._mark_input_and_refresh_batch(
                    connection,
                    job_id=lease.job_id,
                    input_status="failed",
                    error_code=error_code.value,
                    error_message=diagnostic,
                    now_text=now_text,
                )
        return next_status

    def finish_canceled(
        self,
        lease: JobLease,
        *,
        now: datetime,
        remove_pending_asset: PendingAssetCleanup | None = None,
    ) -> JobStatus:
        now_text = utc_text(now)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._finish_canceled_locked(connection, lease, now_text=now_text)
            if remove_pending_asset is not None:
                self._remove_and_delete_intents_locked(
                    connection,
                    job_id=lease.job_id,
                    attempt_id=lease.attempt_id,
                    lease_token=lease.lease_token,
                    remove_pending_asset=remove_pending_asset,
                )
        return JobStatus.CANCELED

    def get_job(self, job_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM download_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_input_control(self, input_record_id: str) -> dict | None:
        """Return public orchestration state without graph target material."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT i.id, i.status, i.active_run_generation,
                       EXISTS (
                           SELECT 1 FROM download_jobs AS parent
                           WHERE parent.input_record_id = i.id
                             AND parent.job_kind = 'discover'
                       ) AS is_graph_v2
                FROM input_records AS i
                WHERE i.id = ?
                """,
                (input_record_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["is_graph_v2"] = bool(payload["is_graph_v2"])
        return payload

    def attempts_for(
        self, job_id: str, *, run_generation: int | None = None
    ) -> list[dict]:
        if run_generation is not None and (
            isinstance(run_generation, bool)
            or not isinstance(run_generation, int)
            or run_generation < 1
        ):
            raise ValueError("run_generation must be a positive integer")
        with self.database.connect() as connection:
            if run_generation is None:
                rows = connection.execute(
                    """
                    SELECT * FROM job_attempts
                    WHERE job_id = ? ORDER BY attempt_no
                    """,
                    (job_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM job_attempts
                    WHERE job_id = ? AND run_generation = ?
                    ORDER BY generation_attempt_no, attempt_no
                    """,
                    (job_id, run_generation),
                ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _insert_asset_records_locked(
        connection,
        *,
        lease: JobLease,
        source_item_id: str,
        asset: "CommittedAsset",
        now_text: str,
    ) -> None:
        verification = asset.verification
        connection.execute(
            """
            INSERT INTO media_assets(
                id, source_item_id, media_key, media_kind,
                duration_seconds, container, codec, width, height,
                size_bytes, sha256, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)
            """,
            (
                asset.asset_id,
                source_item_id,
                asset.media_key,
                asset.media_kind,
                verification.duration_seconds,
                verification.container,
                verification.codec,
                verification.width,
                verification.height,
                asset.size_bytes,
                asset.sha256,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO job_assets(job_id, asset_id, role, ordinal)
            VALUES (?, ?, ?, ?)
            """,
            (lease.job_id, asset.asset_id, asset.role, asset.ordinal),
        )
        WorkerRepository._insert_artifact_records_locked(
            connection,
            lease=lease,
            asset=asset,
            now_text=now_text,
        )

    @staticmethod
    def _insert_artifact_records_locked(
        connection,
        *,
        lease: JobLease,
        asset: "CommittedAsset",
        now_text: str,
    ) -> None:
        """Persist original, manifest and sidecars in one SQL transaction."""

        original_artifact_id = str(uuid4())
        rows = [
            (
                original_artifact_id,
                asset.asset_id,
                "original",
                asset.relative_original_path,
                None,
                asset.sha256,
                None,
                lease.adapter,
                lease.adapter_version,
                now_text,
            ),
            (
                str(uuid4()),
                asset.asset_id,
                "manifest",
                asset.relative_manifest_path,
                "application/json",
                asset.manifest_sha256,
                None,
                "video-download-control",
                __version__,
                now_text,
            ),
        ]
        caption_rows = []
        for artifact in asset.artifacts:
            artifact_id = str(uuid4())
            rows.append(
                (
                    artifact_id,
                    asset.asset_id,
                    artifact.kind,
                    artifact.relative_path,
                    artifact.mime_type,
                    artifact.sha256,
                    original_artifact_id,
                    lease.adapter,
                    lease.adapter_version,
                    now_text,
                )
            )
            if artifact.kind == "caption":
                caption_rows.append(
                    (
                        str(uuid4()),
                        artifact_id,
                        artifact.language,
                        "platform",
                        1,
                        "ready",
                    )
                )
        connection.executemany(
            """
            INSERT INTO artifacts(
                id, asset_id, kind, path, mime_type, sha256,
                parent_artifact_id, tool_name, tool_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        if caption_rows:
            connection.executemany(
                """
                INSERT INTO captions(
                    id, artifact_id, language, origin, revision, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                caption_rows,
            )

    @staticmethod
    def _finish_ready_locked(connection, lease: JobLease, *, now_text: str) -> None:
        WorkerRepository._record_platform_success_locked(
            connection, lease=lease, now_text=now_text
        )
        updated = connection.execute(
            """
            UPDATE download_jobs
            SET status = 'ready', progress = 1, final_error_code = NULL,
                lease_owner = NULL, lease_token = NULL,
                heartbeat_at = NULL, lease_expires_at = NULL,
                cancel_requested_at = NULL, updated_at = ?
            WHERE id = ? AND lease_token = ? AND status = 'verifying'
              AND cancel_requested_at IS NULL
              AND lease_expires_at > ?
            """,
            (now_text, lease.job_id, lease.lease_token, now_text),
        )
        if updated.rowcount != 1:
            raise LostLease(lease.job_id)
        WorkerRepository._finish_attempt(
            connection,
            lease,
            status="succeeded",
            now_text=now_text,
            error_code=None,
            diagnostic=None,
            exit_code=0,
        )
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=lease.job_id,
            input_status="ready",
            error_code=None,
            error_message=None,
            now_text=now_text,
        )

    @staticmethod
    def _record_platform_success_locked(
        connection, *, lease: JobLease, now_text: str
    ) -> None:
        row = connection.execute(
            """
            SELECT s.platform
            FROM download_jobs AS j
            JOIN source_items AS s ON s.id = j.source_item_id
            WHERE j.id = ?
            """,
            (lease.job_id,),
        ).fetchone()
        if row is None:
            raise LostLease(lease.job_id)
        connection.execute(
            """
            INSERT INTO platform_circuits(
                platform, state, consecutive_failures, last_error_code,
                opened_at, cooldown_until, requires_manual_reset,
                probe_job_id, probe_lease_token, updated_at
            ) VALUES (?, 'closed', 0, NULL, NULL, NULL, 0, NULL, NULL, ?)
            ON CONFLICT(platform) DO UPDATE SET
                state = 'closed', consecutive_failures = 0,
                last_error_code = NULL, opened_at = NULL,
                cooldown_until = NULL, requires_manual_reset = 0,
                probe_job_id = NULL, probe_lease_token = NULL,
                updated_at = excluded.updated_at
            """,
            (row["platform"], now_text),
        )

    @staticmethod
    def _record_platform_failure_locked(
        connection,
        *,
        lease: JobLease,
        error_code: ErrorCode,
        now: datetime,
        retry_at: datetime | None,
    ) -> None:
        now_text = utc_text(now)
        row = connection.execute(
            """
            SELECT s.platform, c.state, c.consecutive_failures,
                   c.last_error_code, c.probe_job_id, c.probe_lease_token
            FROM download_jobs AS j
            JOIN source_items AS s ON s.id = j.source_item_id
            LEFT JOIN platform_circuits AS c ON c.platform = s.platform
            WHERE j.id = ?
            """,
            (lease.job_id,),
        ).fetchone()
        if row is None:
            raise LostLease(lease.job_id)
        half_open_probe = (
            row["state"] == "half_open"
            and row["probe_job_id"] == lease.job_id
            and row["probe_lease_token"] == lease.lease_token
        )
        tracked = (
            error_code in PLATFORM_FAILURE_THRESHOLDS
            or error_code == ErrorCode.RATE_LIMITED
        )
        if not tracked:
            if half_open_probe:
                WorkerRepository._release_half_open_locked(
                    connection,
                    job_id=lease.job_id,
                    lease_token=lease.lease_token,
                    now_text=now_text,
                )
            return

        previous_count = (
            int(row["consecutive_failures"] or 0)
            if row["last_error_code"] == error_code.value
            else 0
        )
        failures = previous_count + 1
        manual_reset = False
        should_open = half_open_probe
        cooldown_until: str | None = None

        if error_code == ErrorCode.RATE_LIMITED:
            should_open = True
            manual_reset = failures >= RATE_LIMIT_MANUAL_RESET_THRESHOLD
            if not manual_reset:
                cooldown_until = utc_text(
                    max(now + timedelta(seconds=60), retry_at or now)
                )
        elif error_code == ErrorCode.EXTRACTOR_BROKEN:
            should_open = (
                should_open
                or failures >= PLATFORM_FAILURE_THRESHOLDS[error_code]
            )
            manual_reset = should_open

        state = "open" if should_open else "closed"
        connection.execute(
            """
            INSERT INTO platform_circuits(
                platform, state, consecutive_failures, last_error_code,
                opened_at, cooldown_until, requires_manual_reset,
                probe_job_id, probe_lease_token, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(platform) DO UPDATE SET
                state = excluded.state,
                consecutive_failures = excluded.consecutive_failures,
                last_error_code = excluded.last_error_code,
                opened_at = excluded.opened_at,
                cooldown_until = excluded.cooldown_until,
                requires_manual_reset = excluded.requires_manual_reset,
                probe_job_id = NULL,
                probe_lease_token = NULL,
                updated_at = excluded.updated_at
            """,
            (
                row["platform"],
                state,
                failures,
                error_code.value,
                now_text if should_open else None,
                cooldown_until,
                int(manual_reset),
                now_text,
            ),
        )

    @staticmethod
    def _release_half_open_locked(
        connection, *, job_id: str, lease_token: str, now_text: str
    ) -> None:
        connection.execute(
            """
            UPDATE platform_circuits
            SET state = 'open', opened_at = COALESCE(opened_at, ?),
                cooldown_until = ?, requires_manual_reset = 0,
                probe_job_id = NULL, probe_lease_token = NULL,
                updated_at = ?
            WHERE state = 'half_open' AND probe_job_id = ?
              AND probe_lease_token = ?
            """,
            (now_text, now_text, now_text, job_id, lease_token),
        )

    @staticmethod
    def _remove_and_delete_intents_locked(
        connection,
        *,
        job_id: str,
        attempt_id: str,
        lease_token: str,
        remove_pending_asset: PendingAssetCleanup,
    ) -> int:
        rows = connection.execute(
            """
            SELECT asset_id FROM asset_commit_intents
            WHERE job_id = ? AND attempt_id = ? AND lease_token = ?
            ORDER BY created_at, asset_id
            """,
            (job_id, attempt_id, lease_token),
        ).fetchall()
        for row in rows:
            remove_pending_asset(row["asset_id"], attempt_id)
        if rows:
            deleted = connection.execute(
                """
                DELETE FROM asset_commit_intents
                WHERE job_id = ? AND attempt_id = ? AND lease_token = ?
                """,
                (job_id, attempt_id, lease_token),
            )
            if deleted.rowcount != len(rows):
                raise RuntimeError("asset intent cleanup lost a row")
        return len(rows)

    @staticmethod
    def _transition_allowed(current: JobStatus, target: JobStatus) -> bool:
        if current == target:
            return True
        return target in {
            JobStatus.PROBING: {JobStatus.DOWNLOADING},
            JobStatus.DOWNLOADING: {
                JobStatus.POSTPROCESSING,
                JobStatus.VERIFYING,
            },
            JobStatus.POSTPROCESSING: {JobStatus.VERIFYING},
            JobStatus.VERIFYING: set(),
        }[current]

    @staticmethod
    def _finish_attempt(
        connection,
        lease: JobLease,
        *,
        status: str,
        now_text: str,
        error_code: ErrorCode | None,
        diagnostic: str | None,
        exit_code: int | None,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE job_attempts
            SET status = ?, finished_at = ?, error_code = ?,
                diagnostic = ?, exit_code = ?
            WHERE id = ? AND lease_token = ? AND status = 'running'
            """,
            (
                status,
                now_text,
                error_code,
                diagnostic,
                exit_code,
                lease.attempt_id,
                lease.lease_token,
            ),
        )
        if updated.rowcount != 1:
            raise LostLease(lease.job_id)

    @staticmethod
    def _finish_canceled_locked(connection, lease: JobLease, *, now_text: str) -> None:
        WorkerRepository._release_half_open_locked(
            connection,
            job_id=lease.job_id,
            lease_token=lease.lease_token,
            now_text=now_text,
        )
        updated = connection.execute(
            """
            UPDATE download_jobs
            SET status = 'canceled', final_error_code = NULL,
                lease_owner = NULL, lease_token = NULL,
                heartbeat_at = NULL, lease_expires_at = NULL,
                available_at = ?, updated_at = ?
            WHERE id = ? AND lease_token = ?
              AND lease_expires_at > ?
              AND status IN (
                  'probing', 'downloading', 'postprocessing', 'verifying'
              )
            """,
            (now_text, now_text, lease.job_id, lease.lease_token, now_text),
        )
        if updated.rowcount != 1:
            raise LostLease(lease.job_id)
        WorkerRepository._finish_attempt(
            connection,
            lease,
            status="canceled",
            now_text=now_text,
            error_code=None,
            diagnostic=None,
            exit_code=None,
        )
        WorkerRepository._mark_input_and_refresh_batch(
            connection,
            job_id=lease.job_id,
            input_status="canceled",
            error_code=None,
            error_message=None,
            now_text=now_text,
        )

    @staticmethod
    def _mark_input_and_refresh_batch(
        connection,
        *,
        job_id: str,
        input_status: str,
        error_code: str | None,
        error_message: str | None,
        now_text: str,
    ) -> None:
        job = connection.execute(
            "SELECT input_record_id, batch_id FROM download_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise RuntimeError("job disappeared during aggregate update")
        if input_status not in {"ready", "failed", "canceled"}:
            raise ValueError("unsupported terminal input status")
        input_row = connection.execute(
            """
            SELECT active_discovery_id, active_run_generation
            FROM input_records WHERE id = ?
            """,
            (job["input_record_id"],),
        ).fetchone()
        if input_row is None:
            raise RuntimeError("input disappeared during aggregate update")

        if input_row["active_discovery_id"] is not None:
            scoped_jobs = connection.execute(
                """
                SELECT DISTINCT j.id, j.status
                FROM input_relation_jobs AS link
                JOIN download_jobs AS j ON j.id = link.job_id
                WHERE link.input_record_id = ?
                  AND link.discovery_id = ?
                  AND link.run_generation = ?
                ORDER BY j.id
                """,
                (
                    job["input_record_id"],
                    input_row["active_discovery_id"],
                    input_row["active_run_generation"],
                ),
            ).fetchall()
        else:
            discover = connection.execute(
                """
                SELECT id, status
                FROM download_jobs
                WHERE input_record_id = ? AND job_kind = 'discover'
                  AND run_generation = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (job["input_record_id"], input_row["active_run_generation"]),
            ).fetchone()
            if discover is not None:
                scoped_jobs = [discover]
            else:
                scoped_jobs = connection.execute(
                    """
                    SELECT id, status FROM download_jobs
                    WHERE input_record_id = ?
                    ORDER BY created_at, id
                    """,
                    (job["input_record_id"],),
                ).fetchall()
        if not scoped_jobs:
            raise RuntimeError("input has no aggregatable jobs")
        job_counts: dict[str, int] = {}
        for scoped_job in scoped_jobs:
            status = scoped_job["status"]
            job_counts[status] = job_counts.get(status, 0) + 1
        active_or_queued = job_counts.get("queued", 0) + sum(
            job_counts.get(status.value, 0) for status in ACTIVE_STATUSES
        )
        ready_jobs = job_counts.get("ready", 0)
        failed_jobs = job_counts.get("failed", 0)
        canceled_jobs = job_counts.get("canceled", 0)
        total_jobs = sum(job_counts.values())
        if active_or_queued:
            aggregate_input_status = "queued"
        elif ready_jobs and (failed_jobs or canceled_jobs):
            aggregate_input_status = "partial_success"
        elif ready_jobs == total_jobs:
            aggregate_input_status = "ready"
        elif failed_jobs:
            aggregate_input_status = "failed"
        elif canceled_jobs == total_jobs:
            aggregate_input_status = "canceled"
        else:
            raise RuntimeError("input has no aggregatable jobs")

        aggregate_error_code = None
        aggregate_error_message = None
        if aggregate_input_status in {"failed", "partial_success"}:
            scoped_job_ids = [row["id"] for row in scoped_jobs]
            placeholders = ",".join("?" for _ in scoped_job_ids)
            failure = connection.execute(
                f"""
                SELECT j.final_error_code, a.diagnostic
                FROM download_jobs AS j
                LEFT JOIN job_attempts AS a ON a.id = (
                    SELECT latest.id
                    FROM job_attempts AS latest
                    WHERE latest.job_id = j.id
                      AND latest.error_code IS NOT NULL
                    ORDER BY latest.attempt_no DESC
                    LIMIT 1
                )
                WHERE j.id IN ({placeholders})
                  AND j.status = 'failed'
                  AND j.final_error_code IS NOT NULL
                ORDER BY j.updated_at DESC, j.id
                LIMIT 1
                """,
                scoped_job_ids,
            ).fetchone()
            if failure is not None:
                aggregate_error_code = failure["final_error_code"]
                aggregate_error_message = (
                    failure["diagnostic"]
                    if failure["diagnostic"] is not None
                    else error_message
                )
            else:
                aggregate_error_code = error_code
                aggregate_error_message = error_message

        connection.execute(
            """
            UPDATE input_records
            SET status = ?, error_code = ?, error_message = ?
            WHERE id = ?
            """,
            (
                aggregate_input_status,
                aggregate_error_code,
                aggregate_error_message,
                job["input_record_id"],
            ),
        )
        WorkerRepository._refresh_batch_locked(
            connection,
            input_record_id=job["input_record_id"],
            now_text=now_text,
        )

    @staticmethod
    def _refresh_batch_locked(
        connection,
        *,
        input_record_id: str,
        now_text: str,
    ) -> None:
        input_row = connection.execute(
            "SELECT batch_id FROM input_records WHERE id = ?",
            (input_record_id,),
        ).fetchone()
        if input_row is None:
            raise RuntimeError("input disappeared during batch refresh")
        counts = {
            row["status"]: int(row["count"])
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM input_records WHERE batch_id = ? GROUP BY status
                """,
                (input_row["batch_id"],),
            ).fetchall()
        }
        total = sum(counts.values())
        queued = counts.get("queued", 0)
        ready = counts.get("ready", 0)
        failed = counts.get("failed", 0)
        duplicate = counts.get("duplicate", 0)
        canceled = counts.get("canceled", 0)
        partial_success = counts.get("partial_success", 0)
        if queued:
            batch_status = "queued"
        elif partial_success:
            batch_status = "partial_success"
        elif ready and (failed or canceled):
            batch_status = "partial_success"
        elif ready and ready + duplicate == total:
            batch_status = "ready"
        elif failed:
            batch_status = "failed"
        elif canceled:
            batch_status = "canceled"
        elif duplicate == total:
            batch_status = "duplicate"
        else:
            batch_status = "failed"
        connection.execute(
            """
            UPDATE batches
            SET status = ?, queued_count = ?, ready_count = ?,
                failed_count = ?, duplicate_count = ?, canceled_count = ?,
                partial_success_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                batch_status,
                queued,
                ready,
                failed,
                duplicate,
                canceled,
                partial_success,
                now_text,
                input_row["batch_id"],
            ),
        )

    def _prepare_asset_intent_recovery(
        self,
        *,
        now_text: str,
        recovery_expires_text: str,
        limit: int,
        claim_cleanup: bool,
    ) -> tuple[int, list[_AssetIntentRecoveryClaim], bool]:
        """Claim stale intents and fence their attempts in one short write."""

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._prepare_asset_intent_recovery_locked(
                connection,
                now_text=now_text,
                recovery_expires_text=recovery_expires_text,
                limit=limit,
                claim_cleanup=claim_cleanup,
            )

    @staticmethod
    def _prepare_asset_intent_recovery_locked(
        connection,
        *,
        now_text: str,
        recovery_expires_text: str,
        limit: int,
        claim_cleanup: bool,
    ) -> tuple[int, list[_AssetIntentRecoveryClaim], bool]:
        rows = connection.execute(
            """
            SELECT
                i.asset_id, i.job_id, i.attempt_id,
                i.lease_token AS intent_lease_token,
                j.status AS job_status,
                j.lease_token AS job_lease_token,
                j.lease_expires_at,
                j.cancel_requested_at,
                j.generation_attempt_count,
                a.status AS attempt_status,
                a.lease_token AS attempt_lease_token,
                m.id AS registered_asset_id
            FROM asset_commit_intents AS i
            JOIN download_jobs AS j ON j.id = i.job_id
            JOIN job_attempts AS a ON a.id = i.attempt_id
            LEFT JOIN media_assets AS m ON m.id = i.asset_id
            WHERE i.recovery_token IS NULL
               OR i.recovery_expires_at IS NULL
               OR i.recovery_expires_at <= ?
            ORDER BY
                CASE WHEN
                    m.id IS NULL
                    AND j.status = 'verifying'
                    AND j.lease_token = i.lease_token
                    AND j.lease_expires_at > ?
                    AND j.cancel_requested_at IS NULL
                    AND a.status = 'running'
                    AND a.lease_token = i.lease_token
                THEN 1 ELSE 0 END,
                i.created_at,
                i.asset_id
            LIMIT ?
            """,
            (now_text, now_text, limit),
        ).fetchall()
        recovered = 0
        blocked = False
        claims: list[_AssetIntentRecoveryClaim] = []
        for row in rows:
            identity = (
                row["asset_id"],
                row["job_id"],
                row["attempt_id"],
                row["intent_lease_token"],
            )
            recovery_available_sql = """
                AND (
                    recovery_token IS NULL
                    OR recovery_expires_at IS NULL
                    OR recovery_expires_at <= ?
                )
            """
            if row["registered_asset_id"] is not None:
                deleted = connection.execute(
                    """
                    DELETE FROM asset_commit_intents
                    WHERE asset_id = ? AND job_id = ?
                      AND attempt_id = ? AND lease_token = ?
                    """
                    + recovery_available_sql,
                    (*identity, now_text),
                )
                recovered += deleted.rowcount
                continue

            exact_active = (
                row["job_status"] == JobStatus.VERIFYING.value
                and row["job_lease_token"] == row["intent_lease_token"]
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now_text
                and row["cancel_requested_at"] is None
                and row["attempt_status"] == "running"
                and row["attempt_lease_token"] == row["intent_lease_token"]
            )
            if exact_active:
                continue
            if not claim_cleanup:
                blocked = True
                continue

            recovery_token = str(uuid4())
            claimed = connection.execute(
                """
                UPDATE asset_commit_intents
                SET recovery_token = ?, recovery_expires_at = ?
                WHERE asset_id = ? AND job_id = ?
                  AND attempt_id = ? AND lease_token = ?
                """
                + recovery_available_sql,
                (
                    recovery_token,
                    recovery_expires_text,
                    *identity,
                    now_text,
                ),
            )
            if claimed.rowcount != 1:
                continue

            job_matches_intent = (
                row["job_status"] in {status.value for status in ACTIVE_STATUSES}
                and row["job_lease_token"] == row["intent_lease_token"]
            )
            cancel_requested = (
                job_matches_intent and row["cancel_requested_at"] is not None
            )
            connection.execute(
                """
                UPDATE job_attempts
                SET status = ?, finished_at = ?, error_code = ?, diagnostic = ?
                WHERE job_id = ? AND lease_token = ? AND status = 'running'
                """,
                (
                    "canceled" if cancel_requested else "abandoned",
                    now_text,
                    None if cancel_requested else ErrorCode.WORKER_LOST.value,
                    None if cancel_requested else "worker asset commit was abandoned",
                    row["job_id"],
                    row["intent_lease_token"],
                ),
            )
            if job_matches_intent:
                WorkerRepository._release_half_open_locked(
                    connection,
                    job_id=row["job_id"],
                    lease_token=row["intent_lease_token"],
                    now_text=now_text,
                )
                exhausted = int(row["generation_attempt_count"]) >= MAX_ATTEMPTS
                if cancel_requested:
                    next_status = JobStatus.CANCELED
                    final_error = None
                else:
                    next_status = JobStatus.FAILED if exhausted else JobStatus.QUEUED
                    final_error = ErrorCode.WORKER_LOST if exhausted else None
                updated = connection.execute(
                    """
                    UPDATE download_jobs
                    SET status = ?, progress = 0, final_error_code = ?,
                        lease_owner = NULL, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL,
                        cancel_requested_at = CASE WHEN ? THEN cancel_requested_at ELSE NULL END,
                        available_at = ?, updated_at = ?
                    WHERE id = ? AND lease_token = ?
                      AND status IN (
                          'probing', 'downloading', 'postprocessing', 'verifying'
                      )
                    """,
                    (
                        next_status,
                        final_error,
                        cancel_requested,
                        now_text,
                        now_text,
                        row["job_id"],
                        row["intent_lease_token"],
                    ),
                )
                if updated.rowcount == 1 and (cancel_requested or exhausted):
                    WorkerRepository._mark_input_and_refresh_batch(
                        connection,
                        job_id=row["job_id"],
                        input_status="canceled" if cancel_requested else "failed",
                        error_code=(
                            None
                            if cancel_requested
                            else ErrorCode.WORKER_LOST.value
                        ),
                        error_message=(
                            None
                            if cancel_requested
                            else "worker asset commit was abandoned"
                        ),
                        now_text=now_text,
                    )
            claims.append(
                _AssetIntentRecoveryClaim(
                    asset_id=row["asset_id"],
                    job_id=row["job_id"],
                    attempt_id=row["attempt_id"],
                    lease_token=row["intent_lease_token"],
                    recovery_token=recovery_token,
                )
            )
        return recovered, claims, blocked

    def _release_asset_intent_recovery_claims(
        self, claims: Sequence[_AssetIntentRecoveryClaim]
    ) -> None:
        if not claims:
            return
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for claim in claims:
                connection.execute(
                    """
                    UPDATE asset_commit_intents
                    SET recovery_token = NULL, recovery_expires_at = NULL
                    WHERE asset_id = ? AND job_id = ?
                      AND attempt_id = ? AND lease_token = ?
                      AND recovery_token = ?
                    """,
                    (
                        claim.asset_id,
                        claim.job_id,
                        claim.attempt_id,
                        claim.lease_token,
                        claim.recovery_token,
                    ),
                )

    def _complete_asset_intent_recovery_claim(
        self, claim: _AssetIntentRecoveryClaim
    ) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                """
                DELETE FROM asset_commit_intents
                WHERE asset_id = ? AND job_id = ?
                  AND attempt_id = ? AND lease_token = ?
                  AND recovery_token = ?
                """,
                (
                    claim.asset_id,
                    claim.job_id,
                    claim.attempt_id,
                    claim.lease_token,
                    claim.recovery_token,
                ),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("asset intent recovery lease was lost")

    @staticmethod
    def _recover_expired(connection, now_text: str) -> None:
        rows = connection.execute(
            """
            SELECT id, lease_token, generation_attempt_count,
                   cancel_requested_at
            FROM download_jobs
            WHERE status IN (
                'probing', 'downloading', 'postprocessing', 'verifying'
            )
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM asset_commit_intents AS pending
                  WHERE pending.job_id = download_jobs.id
              )
            ORDER BY lease_expires_at, id
            """,
            (now_text,),
        ).fetchall()
        for row in rows:
            WorkerRepository._release_half_open_locked(
                connection,
                job_id=row["id"],
                lease_token=row["lease_token"],
                now_text=now_text,
            )
            if row["cancel_requested_at"] is not None:
                connection.execute(
                    """
                    UPDATE job_attempts
                    SET status = 'canceled', finished_at = ?,
                        error_code = NULL, diagnostic = NULL
                    WHERE job_id = ? AND lease_token = ? AND status = 'running'
                    """,
                    (now_text, row["id"], row["lease_token"]),
                )
                connection.execute(
                    """
                    UPDATE download_jobs
                    SET status = 'canceled', final_error_code = NULL,
                        lease_owner = NULL, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL,
                        available_at = ?, updated_at = ?
                    WHERE id = ? AND lease_token = ?
                    """,
                    (now_text, now_text, row["id"], row["lease_token"]),
                )
                WorkerRepository._mark_input_and_refresh_batch(
                    connection,
                    job_id=row["id"],
                    input_status="canceled",
                    error_code=None,
                    error_message=None,
                    now_text=now_text,
                )
                continue
            connection.execute(
                """
                UPDATE job_attempts
                SET status = 'abandoned', finished_at = ?,
                    error_code = 'worker_lost',
                    diagnostic = 'worker lease expired'
                WHERE job_id = ? AND lease_token = ? AND status = 'running'
                """,
                (now_text, row["id"], row["lease_token"]),
            )
            exhausted = int(row["generation_attempt_count"]) >= MAX_ATTEMPTS
            connection.execute(
                """
                UPDATE download_jobs
                SET status = ?, progress = 0, final_error_code = ?,
                    lease_owner = NULL, lease_token = NULL,
                    heartbeat_at = NULL, lease_expires_at = NULL,
                    cancel_requested_at = NULL, available_at = ?, updated_at = ?
                WHERE id = ? AND lease_token = ?
                """,
                (
                    JobStatus.FAILED if exhausted else JobStatus.QUEUED,
                    ErrorCode.WORKER_LOST if exhausted else None,
                    now_text,
                    now_text,
                    row["id"],
                    row["lease_token"],
                ),
            )
            if exhausted:
                WorkerRepository._mark_input_and_refresh_batch(
                    connection,
                    job_id=row["id"],
                    input_status="failed",
                    error_code=ErrorCode.WORKER_LOST.value,
                    error_message="worker lease expired",
                    now_text=now_text,
                )

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .database import Database
from .domain import JobStatus, Platform


_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ACTIVE_JOB_STATUSES = (
    JobStatus.PROBING.value,
    JobStatus.DOWNLOADING.value,
    JobStatus.POSTPROCESSING.value,
    JobStatus.VERIFYING.value,
)


class CredentialProfileError(ValueError):
    """A stable, secret-free error raised by the local credential admin path."""


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise CredentialProfileError("timestamp must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def normalize_expiry(value: str | None, *, now: datetime) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        raise CredentialProfileError("expiry must not be blank")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CredentialProfileError(
            "expiry must be an ISO-8601 timestamp with a timezone"
        ) from exc
    normalized = _utc_text(parsed)
    if parsed.astimezone(UTC) <= now.astimezone(UTC):
        raise CredentialProfileError("expiry must be in the future")
    return normalized


def validate_opaque_reference(value: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_REFERENCE.fullmatch(value):
        raise CredentialProfileError(
            "credential reference must be a 1-64 character opaque token"
        )
    return value


def _validate_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 80 or any(ord(character) < 32 for character in name):
        raise CredentialProfileError(
            "credential profile name must contain 1-80 printable characters"
        )
    return name


class CredentialRepository:
    """Admin-only operations over opaque credential references.

    The repository never accepts a Cookie path or Cookie contents.  Those stay
    in deployment-owned Worker configuration and are matched only by the short
    opaque ``secret_ref``.
    """

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        platform: Platform,
        name: str,
        secret_ref: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(platform, Platform):
            raise CredentialProfileError("platform must be explicit")
        now = self.clock()
        now_text = _utc_text(now)
        profile_id = str(uuid4())
        normalized_name = _validate_name(name)
        normalized_ref = validate_opaque_reference(secret_ref)
        normalized_expiry = normalize_expiry(expires_at, now=now)
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO credential_profiles(
                        id, platform, name, secret_ref, expires_at,
                        last_verified_at, created_at, disabled_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)
                    """,
                    (
                        profile_id,
                        platform.value,
                        normalized_name,
                        normalized_ref,
                        normalized_expiry,
                        now_text,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CredentialProfileError(
                "an identically named profile already exists for this platform"
            ) from exc
        result = self.get(profile_id)
        if result is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("registered credential profile could not be read")
        return result

    def get(self, profile_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, platform, name, secret_ref, expires_at,
                       last_verified_at, created_at, disabled_at
                FROM credential_profiles WHERE id = ?
                """,
                (profile_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list(
        self,
        *,
        platform: Platform | None = None,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if platform is not None:
            clauses.append("platform = ?")
            parameters.append(platform.value)
        if not include_disabled:
            clauses.append("disabled_at IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, platform, name, secret_ref, expires_at,
                       last_verified_at, created_at, disabled_at
                FROM credential_profiles
                """
                + where
                + " ORDER BY platform, name, id",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def assign(
        self,
        *,
        profile_id: str,
        job_ids: Sequence[str] = (),
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        unique_job_ids = tuple(dict.fromkeys(job_ids))
        if bool(unique_job_ids) == bool(batch_id):
            raise CredentialProfileError(
                "assign exactly one scope: one or more job IDs, or one batch ID"
            )
        now_text = _utc_text(self.clock())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = connection.execute(
                """
                SELECT id, platform, expires_at, disabled_at
                FROM credential_profiles WHERE id = ?
                """,
                (profile_id,),
            ).fetchone()
            if profile is None:
                raise CredentialProfileError("credential profile does not exist")
            if profile["disabled_at"] is not None:
                raise CredentialProfileError("credential profile is disabled")
            if profile["expires_at"] is not None and profile["expires_at"] <= now_text:
                raise CredentialProfileError("credential profile is expired")

            if batch_id is not None:
                batch = connection.execute(
                    "SELECT id FROM batches WHERE id = ?", (batch_id,)
                ).fetchone()
                if batch is None:
                    raise CredentialProfileError("batch does not exist")
                rows = connection.execute(
                    """
                    SELECT j.id, j.status, s.platform
                    FROM download_jobs AS j
                    JOIN source_items AS s ON s.id = j.source_item_id
                    WHERE j.batch_id = ? AND s.platform = ?
                    ORDER BY j.created_at, j.id
                    """,
                    (batch_id, profile["platform"]),
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in unique_job_ids)
                rows = connection.execute(
                    f"""
                    SELECT j.id, j.status, s.platform
                    FROM download_jobs AS j
                    JOIN source_items AS s ON s.id = j.source_item_id
                    WHERE j.id IN ({placeholders})
                    ORDER BY j.created_at, j.id
                    """,
                    unique_job_ids,
                ).fetchall()
                if len(rows) != len(unique_job_ids):
                    raise CredentialProfileError("one or more jobs do not exist")

            if not rows:
                raise CredentialProfileError(
                    "scope contains no jobs for the credential platform"
                )
            mismatched = [row["id"] for row in rows if row["platform"] != profile["platform"]]
            if mismatched:
                raise CredentialProfileError(
                    "credential platform does not match every selected job"
                )
            active = [row["id"] for row in rows if row["status"] in _ACTIVE_JOB_STATUSES]
            if active:
                raise CredentialProfileError(
                    "credential cannot be changed after a selected job was claimed"
                )
            if batch_id is None and any(
                row["status"] != JobStatus.QUEUED for row in rows
            ):
                raise CredentialProfileError(
                    "credential can only be assigned to explicitly selected queued jobs"
                )
            queued = [row["id"] for row in rows if row["status"] == JobStatus.QUEUED]
            if not queued:
                raise CredentialProfileError("scope contains no queued jobs")
            placeholders = ",".join("?" for _ in queued)
            connection.execute(
                f"""
                UPDATE download_jobs
                SET credential_profile_id = ?, updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'queued'
                """,
                (profile_id, now_text, *queued),
            )
        return {
            "profile_id": profile_id,
            "platform": profile["platform"],
            "assigned_job_ids": queued,
            "assigned_count": len(queued),
        }

    def clear_assignment(self, *, job_ids: Sequence[str]) -> dict[str, Any]:
        unique_job_ids = tuple(dict.fromkeys(job_ids))
        if not unique_job_ids:
            raise CredentialProfileError("at least one job ID is required")
        now_text = _utc_text(self.clock())
        placeholders = ",".join("?" for _ in unique_job_ids)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT id, status FROM download_jobs WHERE id IN ({placeholders})",
                unique_job_ids,
            ).fetchall()
            if len(rows) != len(unique_job_ids):
                raise CredentialProfileError("one or more jobs do not exist")
            if any(row["status"] != JobStatus.QUEUED for row in rows):
                raise CredentialProfileError(
                    "credential can only be cleared from queued jobs"
                )
            connection.execute(
                f"""
                UPDATE download_jobs
                SET credential_profile_id = NULL, updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'queued'
                """,
                (now_text, *unique_job_ids),
            )
        return {"cleared_job_ids": list(unique_job_ids), "cleared_count": len(unique_job_ids)}

    def disable(self, *, profile_id: str) -> dict[str, Any]:
        now_text = _utc_text(self.clock())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, disabled_at FROM credential_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            if row is None:
                raise CredentialProfileError("credential profile does not exist")
            if row["disabled_at"] is None:
                connection.execute(
                    "UPDATE credential_profiles SET disabled_at = ? WHERE id = ?",
                    (now_text, profile_id),
                )
            requested = connection.execute(
                """
                UPDATE download_jobs
                SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                    updated_at = ?
                WHERE credential_profile_id = ?
                  AND status IN ('probing', 'downloading', 'postprocessing', 'verifying')
                  AND cancel_requested_at IS NULL
                """,
                (now_text, now_text, profile_id),
            ).rowcount
        return {
            "profile_id": profile_id,
            "disabled_at": row["disabled_at"] or now_text,
            "active_cancel_requests": requested,
        }


__all__ = [
    "CredentialProfileError",
    "CredentialRepository",
    "normalize_expiry",
    "validate_opaque_reference",
]

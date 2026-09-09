from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .credential_defaults import (
    CredentialDefaults,
    CredentialMode,
    resolve_default_profile_locked,
    revalidate_credential_defaults,
    validate_credential_mode,
)
from .database import Database
from .domain import BatchStatus, InputStatus, JobStatus, Platform, SourceType
from .normalization import NormalizedURL


_MAX_DUPLICATE_OWNER_DEPTH = 32


def utc_now(value: datetime | None = None) -> str:
    resolved = datetime.now(UTC) if value is None else value
    if resolved.tzinfo is None:
        raise ValueError("repository clock must return a timezone-aware datetime")
    return (
        resolved.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _system_clock() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class BatchRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        self.database = database
        self.clock = clock

    def create_batch(
        self,
        *,
        name: str | None,
        inputs: list[dict[str, Any]],
        route_policy_version: str,
        enable_x_graph_v2: bool = False,
        credential_mode: CredentialMode = "anonymous",
        credential_defaults: CredentialDefaults | None = None,
    ) -> dict[str, Any]:
        validate_credential_mode(credential_mode)
        batch_id = new_id()
        moment = self.clock()
        now = utc_now(moment)
        queued_count = sum(item["status"] == InputStatus.QUEUED for item in inputs)
        failed_count = sum(item["status"] == InputStatus.FAILED for item in inputs)
        duplicate_count = sum(item["status"] == InputStatus.DUPLICATE for item in inputs)
        status = BatchStatus.QUEUED if queued_count else BatchStatus.FAILED
        final_queued_count = queued_count
        final_duplicate_count = duplicate_count

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if credential_mode == "use_default":
                revalidate_credential_defaults(credential_defaults)
            connection.execute(
                """
                INSERT INTO batches(
                    id, name, status, total_count, queued_count, failed_count,
                    duplicate_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    name,
                    status,
                    len(inputs),
                    queued_count,
                    failed_count,
                    duplicate_count,
                    now,
                    now,
                ),
            )

            source_cache: dict[str, str] = {}
            input_id_by_ordinal: dict[int, str] = {}
            for item in inputs:
                input_id = new_id()
                input_id_by_ordinal[item["ordinal"]] = input_id
                duplicate_of = item.get("duplicate_of_ordinal")
                duplicate_of_id = (
                    input_id_by_ordinal.get(duplicate_of)
                    if duplicate_of is not None
                    else None
                )
                normalized: NormalizedURL | None = item.get("normalized")
                connection.execute(
                    """
                    INSERT INTO input_records(
                        id, batch_id, raw_text, submitted_url, canonical_url,
                        platform, source_type, source_id, ordinal, status,
                        error_code, error_message, duplicate_of_input_record_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        input_id,
                        batch_id,
                        item["raw_text"],
                        normalized.submitted_url if normalized else item.get("submitted_url"),
                        normalized.canonical_url if normalized else None,
                        normalized.platform if normalized else None,
                        normalized.source_type if normalized else None,
                        normalized.source_id if normalized else None,
                        item["ordinal"],
                        item["status"],
                        item.get("error_code"),
                        item.get("error_message"),
                        duplicate_of_id,
                        now,
                    ),
                )

                if item["status"] != InputStatus.QUEUED or not normalized:
                    continue

                job_kind = (
                    "discover"
                    if enable_x_graph_v2
                    and normalized.platform == Platform.X
                    and normalized.source_type == SourceType.X_POST
                    else "download"
                )

                source_item_id = source_cache.get(normalized.canonical_url)
                if not source_item_id:
                    if normalized.source_id:
                        existing = connection.execute(
                            """
                            SELECT id FROM source_items
                            WHERE platform = ? AND source_type = ? AND source_id = ?
                            """,
                            (
                                normalized.platform,
                                normalized.source_type,
                                normalized.source_id,
                            ),
                        ).fetchone()
                    else:
                        existing = connection.execute(
                            "SELECT id FROM source_items WHERE canonical_url = ?",
                            (normalized.canonical_url,),
                        ).fetchone()
                    if existing:
                        source_item_id = existing["id"]
                    else:
                        source_item_id = new_id()
                        connection.execute(
                            """
                            INSERT INTO source_items(
                                id, platform, source_type, source_id,
                                canonical_url, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source_item_id,
                                normalized.platform,
                                normalized.source_type,
                                normalized.source_id,
                                normalized.canonical_url,
                                now,
                                now,
                            ),
                        )
                    source_cache[normalized.canonical_url] = source_item_id

                existing_live_job = None
                if job_kind == "download":
                    existing_live_job = connection.execute(
                        """
                        SELECT input_record_id FROM download_jobs
                        WHERE source_item_id = ?
                          AND status IN (
                              'queued', 'probing', 'downloading',
                              'postprocessing', 'verifying', 'ready'
                          )
                        ORDER BY created_at, id
                        LIMIT 1
                        """,
                        (source_item_id,),
                    ).fetchone()
                if existing_live_job:
                    connection.execute(
                        """
                        UPDATE input_records
                        SET status = ?, duplicate_of_input_record_id = ?
                        WHERE id = ?
                        """,
                        (
                            InputStatus.DUPLICATE,
                            existing_live_job["input_record_id"],
                            input_id,
                        ),
                    )
                    final_queued_count -= 1
                    final_duplicate_count += 1
                    continue

                profile_id = (
                    resolve_default_profile_locked(
                        connection,
                        defaults=credential_defaults,
                        platform=normalized.platform,
                        now=moment,
                    )
                    if credential_mode == "use_default"
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO download_jobs(
                        id, batch_id, input_record_id, source_item_id,
                        job_kind, status, progress, route_policy_version,
                        credential_profile_id, available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        batch_id,
                        input_id,
                        source_item_id,
                        job_kind,
                        JobStatus.QUEUED,
                        route_policy_version,
                        profile_id,
                        now,
                        now,
                        now,
                    ),
                )

            if final_queued_count:
                final_status = BatchStatus.QUEUED
            elif failed_count:
                final_status = BatchStatus.FAILED
            else:
                final_status = BatchStatus.DUPLICATE
            connection.execute(
                """
                UPDATE batches
                SET status = ?, queued_count = ?, duplicate_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    final_queued_count,
                    final_duplicate_count,
                    utc_now(self.clock()),
                    batch_id,
                ),
            )
            if credential_mode == "use_default":
                revalidate_credential_defaults(credential_defaults)

        result = self.get_batch(batch_id)
        if result is None:
            raise RuntimeError("新建批次后无法读取")
        return result

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            return self._get_batch_in(connection, batch_id)

    @staticmethod
    def _get_batch_in(connection, batch_id: str) -> dict[str, Any] | None:
        batch = connection.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            return None
        inputs = connection.execute(
            """
            SELECT * FROM input_records
            WHERE batch_id = ? ORDER BY ordinal
            """,
            (batch_id,),
        ).fetchall()
        jobs = connection.execute(
            """
            SELECT j.*, s.platform, s.source_type,
                   CASE
                       WHEN s.source_type = 'x_attachment'
                       THEN fetch_source.canonical_url
                       ELSE s.canonical_url
                   END AS canonical_url
            FROM download_jobs AS j
            JOIN source_items AS s ON s.id = j.source_item_id
            LEFT JOIN download_job_targets AS target ON target.job_id = j.id
            LEFT JOIN source_items AS fetch_source
              ON fetch_source.id = target.fetch_source_item_id
            WHERE j.batch_id = ? ORDER BY j.created_at, j.id
            """,
            (batch_id,),
        ).fetchall()
        return {
            **dict(batch),
            "inputs": [dict(row) for row in inputs],
            "jobs": [dict(row) for row in jobs],
        }

    def list_batches(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("batch list limit must be between 1 and 100")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, status, total_count, queued_count,
                       failed_count, duplicate_count, ready_count,
                       canceled_count, partial_success_count,
                       created_at, updated_at
                FROM batches
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_batches_by_name(self, name: str) -> list[dict[str, Any]]:
        """Resolve an internal workflow batch name without a recency window."""

        if not isinstance(name, str) or not name or len(name) > 200:
            raise ValueError("invalid batch name")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM batches WHERE name=? ORDER BY created_at,id", (name,)
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            batch = self.get_batch(row["id"])
            if batch is None:
                raise RuntimeError("named batch disappeared")
            results.append(batch)
        return results

    def list_ready_assets_for_batch(
        self, batch_id: str
    ) -> list[dict[str, Any]] | None:
        """Return ready originals owned by this batch or its duplicate inputs.

        Artifact paths deliberately stay inside the repository boundary.  The
        public API exposes a stable download route instead of a filesystem
        location. Duplicate references reuse the original input's Job, never a
        later Job that happens to have the same source identity.
        """

        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return self._list_ready_assets_for_batch_in(connection, batch_id)

    @staticmethod
    def _list_ready_assets_for_batch_in(
        connection, batch_id: str
    ) -> list[dict[str, Any]] | None:
        batch = connection.execute(
            "SELECT 1 FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            return None
        rows = connection.execute(
            """
            WITH RECURSIVE asset_inputs(id) AS (
                SELECT id FROM input_records WHERE batch_id = ?
                UNION
                SELECT original.id
                FROM asset_inputs AS reachable
                JOIN input_records AS duplicate
                  ON duplicate.id = reachable.id
                JOIN input_records AS original
                  ON original.id = duplicate.duplicate_of_input_record_id
                 AND original.platform = duplicate.platform
                 AND original.source_type = duplicate.source_type
                 AND original.source_id = duplicate.source_id
                WHERE duplicate.status = 'duplicate'
            )
            SELECT
                asset.id AS asset_id,
                job.id AS job_id,
                link.ordinal,
                asset.media_kind,
                asset.duration_seconds,
                asset.container,
                asset.codec,
                asset.width,
                asset.height,
                asset.size_bytes,
                asset.sha256
            FROM download_jobs AS job
            JOIN job_assets AS link
              ON link.job_id = job.id AND link.role = 'original'
            JOIN media_assets AS asset
              ON asset.id = link.asset_id AND asset.status = 'ready'
            WHERE job.input_record_id IN (SELECT id FROM asset_inputs)
              AND job.status = 'ready'
              AND asset.source_item_id = job.source_item_id
              AND asset.size_bytes IS NOT NULL
              AND asset.sha256 IS NOT NULL
              AND (
                  SELECT COUNT(*)
                  FROM artifacts AS original
                  WHERE original.asset_id = asset.id
                    AND original.kind = 'original'
              ) = 1
              AND EXISTS (
                  SELECT 1
                  FROM artifacts AS original
                  WHERE original.asset_id = asset.id
                    AND original.kind = 'original'
                    AND original.sha256 = asset.sha256
              )
            ORDER BY job.created_at, job.id, link.ordinal, asset.id
            """,
            (batch_id,),
        ).fetchall()
        records = [{**dict(row), "artifacts": []} for row in rows]
        if records:
            placeholders = ", ".join("?" for _ in records)
            auxiliary_rows = connection.execute(
                f"""
                SELECT
                    auxiliary.id AS artifact_id,
                    auxiliary.asset_id,
                    auxiliary.kind,
                    auxiliary.mime_type,
                    auxiliary.sha256,
                    caption.language
                FROM artifacts AS auxiliary
                LEFT JOIN captions AS caption
                  ON caption.artifact_id = auxiliary.id
                WHERE auxiliary.asset_id IN ({placeholders})
                  AND auxiliary.kind IN ('thumbnail', 'caption')
                  AND EXISTS (
                      SELECT 1
                      FROM artifacts AS original
                      WHERE original.id = auxiliary.parent_artifact_id
                        AND original.asset_id = auxiliary.asset_id
                        AND original.kind = 'original'
                  )
                  AND (
                      auxiliary.kind = 'thumbnail'
                      OR caption.status = 'ready'
                  )
                ORDER BY
                    auxiliary.asset_id,
                    CASE auxiliary.kind
                        WHEN 'thumbnail' THEN 0
                        ELSE 1
                    END,
                    auxiliary.path,
                    auxiliary.id
                """,
                tuple(record["asset_id"] for record in records),
            ).fetchall()
            by_asset = {
                record["asset_id"]: record["artifacts"] for record in records
            }
            for row in auxiliary_rows:
                by_asset[row["asset_id"]].append(dict(row))
        return records

    def inspect_single_input_download(
        self, batch_id: str
    ) -> dict[str, Any] | None:
        """Read one workflow download and its duplicate owner atomically."""

        with self.database.connect() as connection:
            connection.execute("BEGIN")
            batch = self._get_batch_in(connection, batch_id)
            if batch is None:
                return None
            assets = self._list_ready_assets_for_batch_in(connection, batch_id)
            if assets is None:
                raise RuntimeError("batch disappeared from download observation")
            duplicate_owner = None
            if batch.get("status") == BatchStatus.DUPLICATE:
                duplicate_owner = self._resolve_duplicate_owner_in(connection, batch)
            return {
                "batch": batch,
                "assets": assets,
                "duplicate_owner": duplicate_owner,
            }

    @staticmethod
    def _resolve_duplicate_owner_in(
        connection, batch: dict[str, Any]
    ) -> dict[str, Any]:
        invalid = {"valid": False}
        inputs = batch.get("inputs")
        jobs = batch.get("jobs")
        expected_counts = {
            "total_count": 1,
            "queued_count": 0,
            "failed_count": 0,
            "duplicate_count": 1,
            "ready_count": 0,
            "canceled_count": 0,
            "partial_success_count": 0,
        }
        if (
            not isinstance(inputs, list)
            or len(inputs) != 1
            or jobs != []
            or any(batch.get(key) != value for key, value in expected_counts.items())
        ):
            return invalid
        current = inputs[0]
        if not isinstance(current, dict):
            return invalid
        identity = tuple(
            current.get(key)
            for key in ("platform", "source_type", "source_id", "canonical_url")
        )
        if (
            not isinstance(identity[0], str)
            or not isinstance(identity[1], str)
            or not isinstance(identity[3], str)
            or not identity[3]
        ):
            return invalid

        seen: set[str] = set()
        while current.get("status") == InputStatus.DUPLICATE:
            current_id = current.get("id")
            owner_id = current.get("duplicate_of_input_record_id")
            if (
                not isinstance(current_id, str)
                or not current_id
                or current_id in seen
                or len(seen) >= _MAX_DUPLICATE_OWNER_DEPTH
                or not isinstance(owner_id, str)
                or not owner_id
                or current.get("error_code") is not None
                or current.get("error_message") is not None
                or connection.execute(
                    "SELECT COUNT(*) FROM download_jobs WHERE input_record_id=?",
                    (current_id,),
                ).fetchone()[0]
                != 0
            ):
                return invalid
            seen.add(current_id)
            owner = connection.execute(
                """
                SELECT id,batch_id,canonical_url,platform,source_type,source_id,
                       status,error_code,error_message,duplicate_of_input_record_id,
                       active_discovery_id,active_run_generation
                FROM input_records WHERE id=?
                """,
                (owner_id,),
            ).fetchone()
            if owner is None:
                return invalid
            current = dict(owner)
            if tuple(
                current.get(key)
                for key in ("platform", "source_type", "source_id", "canonical_url")
            ) != identity:
                return invalid

        current_id = current.get("id")
        if (
            not isinstance(current_id, str)
            or not current_id
            or current_id in seen
            or current.get("duplicate_of_input_record_id") is not None
        ):
            return invalid
        owner_jobs = connection.execute(
            """
            SELECT
                job.status,
                job.final_error_code,
                job.job_kind,
                job.batch_id,
                job.run_generation,
                source.platform AS source_platform,
                source.source_type AS source_type,
                source.source_id AS source_id,
                CASE
                    WHEN source.source_type = 'x_attachment'
                    THEN fetch_source.canonical_url
                    ELSE source.canonical_url
                END AS canonical_url,
                EXISTS (
                    SELECT 1 FROM download_job_targets AS owner_target
                    WHERE owner_target.job_id = job.id
                ) AS has_target,
                EXISTS (
                    SELECT 1 FROM download_jobs AS discover
                    WHERE discover.input_record_id = job.input_record_id
                      AND discover.job_kind = 'discover'
                ) AS has_discover_parent
            FROM download_jobs AS job
            JOIN source_items AS source ON source.id = job.source_item_id
            LEFT JOIN download_job_targets AS target ON target.job_id = job.id
            LEFT JOIN source_items AS fetch_source
              ON fetch_source.id = target.fetch_source_item_id
            WHERE job.input_record_id=?
            ORDER BY job.created_at,job.id
            """,
            (current_id,),
        ).fetchall()
        if len(owner_jobs) != 1:
            return invalid
        owner_job = owner_jobs[0]
        if (
            owner_job["job_kind"] != "download"
            or owner_job["batch_id"] != current.get("batch_id")
            or current.get("active_discovery_id") is not None
            or owner_job["run_generation"]
            != current.get("active_run_generation")
            or owner_job["has_target"]
            or owner_job["has_discover_parent"]
            or (
                owner_job["source_platform"],
                owner_job["source_type"],
                owner_job["source_id"],
                owner_job["canonical_url"],
            )
            != identity
        ):
            return invalid
        return {
            "valid": True,
            "input_id": current_id,
            "input_status": current.get("status"),
            "input_error_code": current.get("error_code"),
            "input_error_message": current.get("error_message"),
            "job_status": owner_job["status"],
            "job_error_code": owner_job["final_error_code"],
            "job_kind": owner_job["job_kind"],
        }

    def get_ready_original_asset(self, asset_id: str) -> dict[str, Any] | None:
        """Resolve one ready original with its registered media classification."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    asset.id AS asset_id,
                    asset.media_kind,
                    asset.size_bytes,
                    asset.sha256,
                    original.path AS original_path,
                    original.sha256 AS original_sha256
                FROM media_assets AS asset
                JOIN artifacts AS original
                  ON original.asset_id = asset.id
                 AND original.kind = 'original'
                WHERE asset.id = ?
                  AND asset.status = 'ready'
                  AND asset.size_bytes IS NOT NULL
                  AND asset.sha256 IS NOT NULL
                  AND original.sha256 = asset.sha256
                  AND (
                      SELECT COUNT(*)
                      FROM artifacts AS candidate
                      WHERE candidate.asset_id = asset.id
                        AND candidate.kind = 'original'
                  ) = 1
                  AND EXISTS (
                      SELECT 1
                      FROM job_assets AS ready_link
                      JOIN download_jobs AS ready_job
                        ON ready_job.id = ready_link.job_id
                      WHERE ready_link.asset_id = asset.id
                        AND ready_link.role = 'original'
                        AND ready_job.status = 'ready'
                  )
                """,
                (asset_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_ready_auxiliary_artifact(
        self, artifact_id: str
    ) -> dict[str, Any] | None:
        """Resolve one registered sidecar owned by a ready asset and job."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    auxiliary.id AS artifact_id,
                    auxiliary.asset_id,
                    auxiliary.kind,
                    auxiliary.path AS artifact_path,
                    auxiliary.mime_type,
                    auxiliary.sha256,
                    caption.language
                FROM artifacts AS auxiliary
                JOIN media_assets AS asset
                  ON asset.id = auxiliary.asset_id
                LEFT JOIN captions AS caption
                  ON caption.artifact_id = auxiliary.id
                WHERE auxiliary.id = ?
                  AND auxiliary.kind IN ('thumbnail', 'caption')
                  AND asset.status = 'ready'
                  AND asset.sha256 IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM artifacts AS original
                      WHERE original.id = auxiliary.parent_artifact_id
                        AND original.asset_id = auxiliary.asset_id
                        AND original.kind = 'original'
                        AND original.sha256 = asset.sha256
                  )
                  AND (
                      SELECT COUNT(*)
                      FROM artifacts AS original
                      WHERE original.asset_id = asset.id
                        AND original.kind = 'original'
                  ) = 1
                  AND EXISTS (
                      SELECT 1
                      FROM artifacts AS original
                      WHERE original.asset_id = asset.id
                        AND original.kind = 'original'
                        AND original.sha256 = asset.sha256
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM job_assets AS ready_link
                      JOIN download_jobs AS ready_job
                        ON ready_job.id = ready_link.job_id
                      WHERE ready_link.asset_id = asset.id
                        AND ready_link.role = 'original'
                        AND ready_job.status = 'ready'
                  )
                  AND (
                      auxiliary.kind = 'thumbnail'
                      OR caption.status = 'ready'
                  )
                """,
                (artifact_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def count_rows(self, table: str) -> int:
        allowed = {"batches", "input_records", "source_items", "download_jobs"}
        if table not in allowed:
            raise ValueError("unsupported table")
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"])

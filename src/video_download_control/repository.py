from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .database import Database
from .domain import BatchStatus, InputStatus, JobStatus, Platform, SourceType
from .normalization import NormalizedURL


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
    ) -> dict[str, Any]:
        batch_id = new_id()
        now = utc_now(self.clock())
        queued_count = sum(item["status"] == InputStatus.QUEUED for item in inputs)
        failed_count = sum(item["status"] == InputStatus.FAILED for item in inputs)
        duplicate_count = sum(item["status"] == InputStatus.DUPLICATE for item in inputs)
        status = BatchStatus.QUEUED if queued_count else BatchStatus.FAILED
        final_queued_count = queued_count
        final_duplicate_count = duplicate_count

        with self.database.connect() as connection:
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

                connection.execute(
                    """
                    INSERT INTO download_jobs(
                        id, batch_id, input_record_id, source_item_id,
                        job_kind, status, progress, route_policy_version,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        batch_id,
                        input_id,
                        source_item_id,
                        job_kind,
                        JobStatus.QUEUED,
                        route_policy_version,
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

        result = self.get_batch(batch_id)
        if result is None:
            raise RuntimeError("新建批次后无法读取")
        return result

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
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

    def list_ready_assets_for_batch(
        self, batch_id: str
    ) -> list[dict[str, Any]] | None:
        """Return only ready original assets linked to one existing batch.

        Artifact paths deliberately stay inside the repository boundary.  The
        public API exposes a stable download route instead of a filesystem
        location.
        """

        with self.database.connect() as connection:
            batch = connection.execute(
                "SELECT 1 FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                return None
            rows = connection.execute(
                """
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
                WHERE job.batch_id = ?
                  AND job.status = 'ready'
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
        return [dict(row) for row in rows]

    def get_ready_original_asset(self, asset_id: str) -> dict[str, Any] | None:
        """Resolve one DB-registered original for the read-only download API."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    asset.id AS asset_id,
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

    def count_rows(self, table: str) -> int:
        allowed = {"batches", "input_records", "source_items", "download_jobs"}
        if table not in allowed:
            raise ValueError("unsupported table")
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"])

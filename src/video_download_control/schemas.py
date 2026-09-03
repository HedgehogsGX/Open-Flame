from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import (
    BatchStatus,
    ErrorCode,
    InputStatus,
    JobStatus,
    Platform,
    SourceType,
)


class BatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    inputs: list[str] = Field(min_length=1)

    @field_validator("inputs")
    @classmethod
    def reject_blank_collection(cls, value: list[str]) -> list[str]:
        if not any(item.strip() for item in value):
            raise ValueError("至少需要一条非空输入")
        return value


class InputRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    batch_id: str
    raw_text: str
    submitted_url: str | None
    canonical_url: str | None
    platform: Platform | None
    source_type: SourceType | None
    source_id: str | None
    ordinal: int
    expected_item_count: int | None
    active_discovery_id: str | None
    active_run_generation: int
    cancel_requested_at: str | None
    status: InputStatus
    error_code: ErrorCode | None
    error_message: str | None
    duplicate_of_input_record_id: str | None
    created_at: str


class DownloadJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    batch_id: str
    input_record_id: str
    source_item_id: str
    job_kind: Literal["discover", "download"]
    status: JobStatus
    progress: float
    final_error_code: ErrorCode | None
    route_policy_version: str
    available_at: str | None
    cancel_requested_at: str | None
    attempt_count: int
    run_generation: int
    generation_attempt_count: int
    reused_from_job_id: str | None
    created_at: str
    updated_at: str
    platform: Platform
    source_type: SourceType
    canonical_url: str


class BatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None
    status: BatchStatus
    total_count: int
    queued_count: int
    failed_count: int
    duplicate_count: int
    ready_count: int
    canceled_count: int
    partial_success_count: int
    created_at: str
    updated_at: str
    inputs: list[InputRecordResponse]
    jobs: list[DownloadJobResponse]


class OriginalAssetMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_kind: Literal["video", "audio", "image"]
    duration_seconds: float | None
    container: str | None
    codec: str | None
    width: int | None
    height: int | None
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BatchAssetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    job_id: str
    ordinal: int = Field(ge=0)
    original: OriginalAssetMetadataResponse
    download_url: str


class BatchSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None
    status: BatchStatus
    total_count: int
    queued_count: int
    failed_count: int
    duplicate_count: int
    ready_count: int
    canceled_count: int
    partial_success_count: int
    created_at: str
    updated_at: str


class HealthResponse(BaseModel):
    status: str
    database: str
    schema_version: int
    worker: str
    detail: str | None = None


class JobCancelResponse(BaseModel):
    job_id: str
    status: JobStatus
    cancel_requested: bool


class InputCancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_record_id: str
    status: InputStatus
    cancel_requested: bool


class InputRediscoverResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_record_id: str
    status: Literal["queued"]
    run_generation: int = Field(ge=2)


class QueueControlResponse(BaseModel):
    paused: bool
    reason: ErrorCode | None
    paused_at: str | None
    resumed_at: str | None
    updated_at: str


class RuntimeLogsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "error"]
    run_id: str
    write_failures: int = Field(ge=0)
    rejected_events: int = Field(ge=0)
    last_failure_code: str
    events: list[dict[str, str | int | float | bool | None]]


class ToolchainStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["unconfigured", "invalid", "ready"]
    detail_code: str
    yt_dlp_version: str | None
    ffmpeg_version: str | None
    ffprobe_version: str | None
    offline_smoke_passed: bool
    isolated_worker_ready: Literal[False]
    platform_download_verified: Literal[False]
    redistribution_status: str
    network_download_enabled: Literal[False]
    local_direct_worker_available: bool
    security_note: str


class PlatformCircuitResponse(BaseModel):
    platform: Platform
    state: Literal["closed", "open", "half_open"]
    consecutive_failures: int
    last_error_code: ErrorCode | None
    opened_at: str | None
    cooldown_until: str | None
    requires_manual_reset: bool


class JobCountMetric(BaseModel):
    platform: Platform
    status: JobStatus
    count: int


class PlatformOutcomeMetric(BaseModel):
    platform: Platform
    ready: int
    failed: int
    canceled: int
    success_rate: float | None


class MetricsResponse(BaseModel):
    generated_at: str
    queue_depth: int
    active_jobs: int
    average_attempts: float
    attempt_duration_p50_seconds: float | None
    attempt_duration_p95_seconds: float | None
    disk_total_bytes: int
    disk_free_bytes: int
    queue_paused: bool
    queue_pause_reason: ErrorCode | None
    jobs: list[JobCountMetric]
    platform_outcomes: list[PlatformOutcomeMetric]
    platform_circuits: list[PlatformCircuitResponse]
    errors: dict[str, int]

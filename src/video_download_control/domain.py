from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    X = "x"
    YOUTUBE = "youtube"
    BILIBILI = "bilibili"
    DOUYIN = "douyin"


class SourceType(StrEnum):
    X_POST = "x_post"
    X_ATTACHMENT = "x_attachment"
    YOUTUBE_VIDEO = "youtube_video"
    YOUTUBE_SHORT = "youtube_short"
    BILIBILI_VIDEO = "bilibili_video"
    DOUYIN_VIDEO = "douyin_video"
    SHORT_LINK = "short_link"


class BatchStatus(StrEnum):
    QUEUED = "queued"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    READY = "ready"
    PARTIAL_SUCCESS = "partial_success"
    CANCELED = "canceled"


class InputStatus(StrEnum):
    QUEUED = "queued"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    READY = "ready"
    PARTIAL_SUCCESS = "partial_success"
    CANCELED = "canceled"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"
    CANCELED = "canceled"


class ErrorCode(StrEnum):
    INVALID_URL = "invalid_url"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    UNSUPPORTED_LINK_TYPE = "unsupported_link_type"
    SHORT_LINK_RESOLUTION_REQUIRED = "short_link_resolution_required"
    ADAPTER_UNSUPPORTED = "adapter_unsupported"
    EXTRACTOR_BROKEN = "extractor_broken"
    NETWORK_ERROR = "network_error"
    RATE_LIMITED = "rate_limited"
    VALIDATION_FAILED = "validation_failed"
    STORAGE_ERROR = "storage_error"
    AUTHENTICATION_REQUIRED = "authentication_required"
    PRIVATE_CONTENT = "private_content"
    CONTENT_UNAVAILABLE = "content_unavailable"
    GEO_RESTRICTED = "geo_restricted"
    DRM_PROTECTED = "drm_protected"
    EGRESS_POLICY_BLOCKED = "egress_policy_blocked"
    WORKER_LOST = "worker_lost"
    WORKER_TIMEOUT = "worker_timeout"
    WORKER_INTERNAL = "worker_internal"

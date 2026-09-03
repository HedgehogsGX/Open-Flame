"""Pure contracts shared by download adapters and the Worker.

This module deliberately contains no downloader implementation and performs no
network, subprocess, or filesystem I/O.  An adapter executes exactly one probe
or download attempt.  It must not retry internally or choose another adapter;
those decisions belong to the Worker retry policy and adapter registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..diagnostics import (
    MAX_DIAGNOSTIC_LENGTH as MAX_DIAGNOSTIC_LENGTH,
    sanitize_diagnostic,
)
from ..domain import ErrorCode, Platform, SourceType


class AdapterNetworkMode(StrEnum):
    OFFLINE = "offline"
    CONTROLLED_EGRESS = "controlled_egress"
    DIRECT_EGRESS = "direct_egress"


@runtime_checkable
class NetworkExecutionGuard(Protocol):
    """Deployment-owned check proving the controlled network path is ready."""

    def assert_ready(self, *, adapter_name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Worker-owned execution context for one immutable attempt directory."""

    worker_id: str
    attempt_id: str
    temporary_dir: Path
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Input required to inspect one already-normalized source URL."""

    job_id: str
    canonical_url: str
    platform: Platform
    source_type: SourceType
    credential_ref: str | None = field(default=None, repr=False)
    max_height: int = 1080
    max_items: int = 1


@dataclass(frozen=True, slots=True)
class ProbeItem:
    """One source/media item discovered by a probe.

    ``metadata`` must already be reduced to the product's safe allow-list.  Raw
    extractor objects, request headers, cookies, and temporary signed URLs do
    not belong in this DTO.
    """

    canonical_url: str
    media_key: str
    media_kind: str
    stable_key: str | None = field(default=None, repr=False)
    selector_key: str | None = field(default=None, repr=False)
    source_id: str | None = None
    title: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Safe, serializable discovery result returned by an adapter."""

    sanitized_source: Mapping[str, object]
    items: tuple[ProbeItem, ...]
    expected_item_count: int
    discovery_snapshot_hash: str


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Input for downloading one logical source into an attempt directory."""

    job_id: str
    source_item_id: str
    canonical_url: str
    platform: Platform
    source_type: SourceType
    output_dir: Path
    expected_media_keys: tuple[str, ...] = field(repr=False)
    selector_key: str | None = field(default=None, repr=False)
    credential_ref: str | None = field(default=None, repr=False)
    max_height: int = 1080


@dataclass(frozen=True, slots=True)
class ProducedFile:
    """A controlled file path produced beneath ``DownloadRequest.output_dir``."""

    path: Path
    media_key: str
    media_kind: str
    role: str
    ordinal: int = 0


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Files produced by one adapter attempt, before verification/commit."""

    files: tuple[ProducedFile, ...]
    thumbnails: tuple[ProducedFile, ...] = ()
    captions: tuple[ProducedFile, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """Monotonic progress observation emitted by an adapter."""

    phase: str
    fraction: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None


@runtime_checkable
class ProgressReporter(Protocol):
    """Worker callback; persistence and rate limiting stay outside adapters."""

    def __call__(self, update: ProgressUpdate, /) -> None: ...


@runtime_checkable
class CancellationToken(Protocol):
    """Cooperative cancellation view owned by the Worker."""

    def is_cancelled(self) -> bool: ...


class AdapterFailure(RuntimeError):
    """Only expected failure type crossing the Adapter/Worker boundary.

    Concrete adapters should still map raw downloader exceptions, but this
    boundary centrally redacts and bounds diagnostics before they can reach
    Worker persistence.
    """

    def __init__(
        self,
        code: ErrorCode,
        diagnostic: str,
        retry_after: float | None = None,
    ) -> None:
        if retry_after is not None and retry_after < 0:
            raise ValueError("retry_after must be non-negative")
        bounded_diagnostic = sanitize_diagnostic(diagnostic)
        self.code = code
        self.diagnostic = bounded_diagnostic
        self.retry_after = retry_after
        super().__init__(f"{code.value}: {bounded_diagnostic}")

    @property
    def sanitized_detail(self) -> str:
        """Compatibility name emphasizing the diagnostic's safety contract."""

        return self.diagnostic


@runtime_checkable
class DownloadAdapter(Protocol):
    """One-attempt adapter contract; implementations may perform external I/O."""

    name: str
    version: str
    network_mode: AdapterNetworkMode
    supports_exact_selector: bool

    def probe(
        self,
        request: ProbeRequest,
        context: AdapterContext,
    ) -> ProbeResult: ...

    def download(
        self,
        request: DownloadRequest,
        context: AdapterContext,
        progress: ProgressReporter,
        cancellation: CancellationToken,
    ) -> DownloadResult: ...

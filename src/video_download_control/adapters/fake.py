from __future__ import annotations

import hashlib
import os
import re
from collections import deque
from collections.abc import Iterable
from pathlib import Path

from ..domain import ErrorCode, Platform, SourceType
from ..graph import (
    MAX_DISCOVERY_ITEMS,
    GraphValidationError,
    XAttachmentProbeItem,
    XPostIdentity,
    build_x_attachment_discovery,
)
from .base import (
    AdapterContext,
    AdapterFailure,
    AdapterNetworkMode,
    CancellationToken,
    DownloadRequest,
    DownloadResult,
    ProbeItem,
    ProbeRequest,
    ProbeResult,
    ProducedFile,
    ProgressReporter,
    ProgressUpdate,
)


class ScriptedFakeAdapter:
    """Deterministic offline adapter for Worker integration tests only."""

    name = "scripted_fake"
    version = "1"
    network_mode = AdapterNetworkMode.OFFLINE
    supports_exact_selector = False

    def __init__(
        self,
        *,
        probe_failures: list[AdapterFailure] | None = None,
        download_failures: list[AdapterFailure] | None = None,
        payload: bytes = b"offline fake media payload\n",
    ) -> None:
        self._probe_failures = deque(probe_failures or [])
        self._download_failures = deque(download_failures or [])
        self._payload = payload

    def probe(self, request: ProbeRequest, context: AdapterContext) -> ProbeResult:
        del context
        if self._probe_failures:
            raise self._probe_failures.popleft()
        identity = hashlib.sha256(request.canonical_url.encode("utf-8")).hexdigest()
        media_key = f"fake-{identity[:16]}"
        sanitized_source = {
            "platform": request.platform.value,
            "source_type": request.source_type.value,
            "canonical_url": request.canonical_url,
            "title": "Offline fake asset",
            "author": "test-only",
            # This key intentionally proves AssetStore applies its own
            # allow-list instead of trusting an adapter mapping wholesale.
            "authorization": "must-not-be-persisted",
        }
        item = ProbeItem(
            canonical_url=request.canonical_url,
            media_key=media_key,
            media_kind="video",
            metadata={},
        )
        return ProbeResult(
            sanitized_source=sanitized_source,
            items=(item,),
            expected_item_count=1,
            discovery_snapshot_hash=identity,
        )

    def download(
        self,
        request: DownloadRequest,
        context: AdapterContext,
        progress: ProgressReporter,
        cancellation: CancellationToken,
    ) -> DownloadResult:
        del context
        if self._download_failures:
            raise self._download_failures.popleft()
        if cancellation.is_cancelled():
            return DownloadResult(files=())
        identity = hashlib.sha256(request.canonical_url.encode("utf-8")).hexdigest()
        expected_media_key = f"fake-{identity[:16]}"
        if request.expected_media_keys != (expected_media_key,):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "download expectations do not match the fake probe",
            )
        request.output_dir.mkdir(parents=True, exist_ok=True)
        progress(ProgressUpdate(phase="downloading", fraction=0.1))
        target = request.output_dir / "source.fake"
        with target.open("xb") as handle:
            handle.write(self._payload)
            handle.flush()
            os.fsync(handle.fileno())
        progress(
            ProgressUpdate(
                phase="downloading",
                fraction=1.0,
                downloaded_bytes=len(self._payload),
                total_bytes=len(self._payload),
            )
        )
        return DownloadResult(
            files=(
                ProducedFile(
                    path=Path(target),
                    media_key=expected_media_key,
                    media_kind="video",
                    role="original",
                    ordinal=0,
                ),
            )
        )


_NORMALIZED_X_POST = re.compile(
    r"https://x\.com/i/status/([0-9]{1,64})"
)
_GRAPH_PROBE_DIAGNOSTIC = "graph fake probe request is invalid"
_GRAPH_DOWNLOAD_DIAGNOSTIC = "graph fake download request is invalid"
_GRAPH_STORAGE_DIAGNOSTIC = "graph fake output could not be written"
_GRAPH_VALIDATION_PARENT = XPostIdentity("1")
_GRAPH_VALIDATION_PARENT_URL = "https://x.com/i/status/1"


class _RedactedGraphProbeItem(ProbeItem):
    __slots__ = ()

    def __repr__(self) -> str:
        return (
            "ProbeItem("
            f"source_id={self.source_id!r}, "
            f"media_kind={self.media_kind!r}, "
            f"title={self.title!r}, "
            f"metadata={self.metadata!r})"
        )


class _RedactedGraphProducedFile(ProducedFile):
    __slots__ = ()

    def __repr__(self) -> str:
        return (
            "ProducedFile("
            f"path={self.path!r}, "
            f"media_kind={self.media_kind!r}, "
            f"role={self.role!r}, "
            f"ordinal={self.ordinal!r})"
        )


class ScriptedGraphFakeAdapter:
    """Exact-selector offline fake for graph-v2 orchestration tests."""

    __slots__ = (
        "_download_call_count",
        "_payload",
        "_probe_call_count",
        "_probe_items",
    )

    name = "scripted_graph_fake"
    version = "1"
    network_mode = AdapterNetworkMode.OFFLINE
    supports_exact_selector = True

    def __init__(
        self,
        probe_items: Iterable[XAttachmentProbeItem],
        *,
        payload: bytes = b"offline graph fake media payload\n",
    ) -> None:
        bounded_items: list[XAttachmentProbeItem] = []
        for index, probe_item in enumerate(probe_items):
            if index >= MAX_DISCOVERY_ITEMS:
                raise GraphValidationError(
                    "attachment discovery count is outside the limit"
                )
            bounded_items.append(probe_item)
        self._probe_items = tuple(bounded_items)
        build_x_attachment_discovery(
            parent=_GRAPH_VALIDATION_PARENT,
            parent_canonical_url=_GRAPH_VALIDATION_PARENT_URL,
            probe_items=self._probe_items,
        )
        if not isinstance(payload, bytes):
            raise TypeError("graph fake payload must be bytes")
        self._payload = payload
        self._probe_call_count = 0
        self._download_call_count = 0

    @property
    def probe_call_count(self) -> int:
        return self._probe_call_count

    @property
    def download_call_count(self) -> int:
        return self._download_call_count

    def probe(self, request: ProbeRequest, context: AdapterContext) -> ProbeResult:
        del context
        self._probe_call_count += 1
        parent = self._validate_parent_request(
            request.canonical_url,
            request.platform,
            request.source_type,
            expected_source_type=SourceType.X_POST,
            diagnostic=_GRAPH_PROBE_DIAGNOSTIC,
        )
        if (
            not isinstance(request.max_items, int)
            or isinstance(request.max_items, bool)
            or not len(self._probe_items)
            <= request.max_items
            <= MAX_DISCOVERY_ITEMS
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                _GRAPH_PROBE_DIAGNOSTIC,
            )
        try:
            discovery = build_x_attachment_discovery(
                parent=parent,
                parent_canonical_url=request.canonical_url,
                probe_items=self._probe_items,
            )
        except GraphValidationError:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                _GRAPH_PROBE_DIAGNOSTIC,
            ) from None
        return ProbeResult(
            sanitized_source={
                "platform": Platform.X.value,
                "source_type": SourceType.X_POST.value,
                "canonical_url": request.canonical_url,
            },
            items=tuple(
                _RedactedGraphProbeItem(
                    canonical_url=member.canonical_url,
                    source_id=member.source_id,
                    stable_key=member.stable_key,
                    selector_key=member.selector_key,
                    media_key=member.expected_media_key,
                    media_kind=member.media_kind,
                    metadata={},
                )
                for member in discovery.members
            ),
            expected_item_count=discovery.expected_item_count,
            discovery_snapshot_hash=discovery.snapshot_hash,
        )

    def download(
        self,
        request: DownloadRequest,
        context: AdapterContext,
        progress: ProgressReporter,
        cancellation: CancellationToken,
    ) -> DownloadResult:
        del context
        self._download_call_count += 1
        self._validate_parent_request(
            request.canonical_url,
            request.platform,
            request.source_type,
            expected_source_type=SourceType.X_ATTACHMENT,
            diagnostic=_GRAPH_DOWNLOAD_DIAGNOSTIC,
        )
        if (
            not isinstance(request.expected_media_keys, tuple)
            or len(request.expected_media_keys) != 1
            or not isinstance(request.selector_key, str)
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                _GRAPH_DOWNLOAD_DIAGNOSTIC,
            )
        expected_media_key = request.expected_media_keys[0]
        selected_item = next(
            (
                item
                for item in self._probe_items
                if item.selector_key == request.selector_key
            ),
            None,
        )
        if (
            selected_item is None
            or expected_media_key != selected_item.expected_media_key
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                _GRAPH_DOWNLOAD_DIAGNOSTIC,
            )
        if cancellation.is_cancelled():
            return DownloadResult(files=())

        try:
            request.output_dir.mkdir(parents=True, exist_ok=True)
            progress(ProgressUpdate(phase="downloading", fraction=0.1))
            target = request.output_dir / "attachment.fake"
            with target.open("xb") as handle:
                handle.write(self._payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise AdapterFailure(
                ErrorCode.STORAGE_ERROR,
                _GRAPH_STORAGE_DIAGNOSTIC,
            ) from None
        progress(
            ProgressUpdate(
                phase="downloading",
                fraction=1.0,
                downloaded_bytes=len(self._payload),
                total_bytes=len(self._payload),
            )
        )
        return DownloadResult(
            files=(
                _RedactedGraphProducedFile(
                    path=target,
                    media_key=selected_item.expected_media_key,
                    media_kind=selected_item.media_kind,
                    role="original",
                    ordinal=0,
                ),
            )
        )

    @staticmethod
    def _validate_parent_request(
        canonical_url: object,
        platform: object,
        source_type: object,
        *,
        expected_source_type: SourceType,
        diagnostic: str,
    ) -> XPostIdentity:
        match = (
            _NORMALIZED_X_POST.fullmatch(canonical_url)
            if isinstance(canonical_url, str)
            else None
        )
        if (
            platform is not Platform.X
            or source_type is not expected_source_type
            or match is None
        ):
            raise AdapterFailure(ErrorCode.VALIDATION_FAILED, diagnostic)
        try:
            return XPostIdentity(match.group(1))
        except GraphValidationError:
            raise AdapterFailure(ErrorCode.VALIDATION_FAILED, diagnostic) from None

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from video_download_control.adapters import (
    AdapterContext,
    AdapterFailure,
    AdapterNetworkMode,
    DownloadAdapter,
    DownloadRequest,
    DownloadResult,
    ProbeItem,
    ProbeRequest,
    ProbeResult,
    ProducedFile,
)
from video_download_control.adapters.base import MAX_DIAGNOSTIC_LENGTH
from video_download_control.domain import ErrorCode, Platform, SourceType


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class _OfflineFakeAdapter:
    name = "offline-fake"
    version = "test-v1"
    network_mode = AdapterNetworkMode.OFFLINE
    supports_exact_selector = False

    def probe(
        self, request: ProbeRequest, context: AdapterContext
    ) -> ProbeResult:
        item = ProbeItem(
            canonical_url=request.canonical_url,
            source_id="video-1",
            media_key="video-1",
            media_kind="video",
        )
        return ProbeResult(
            sanitized_source={"canonical_url": request.canonical_url},
            items=(item,),
            expected_item_count=1,
            discovery_snapshot_hash="abc123",
        )

    def download(
        self,
        request: DownloadRequest,
        context: AdapterContext,
        progress,
        cancellation,
    ) -> DownloadResult:
        assert not cancellation.is_cancelled()
        produced = ProducedFile(
            path=request.output_dir / "video.mp4",
            media_key="video-1",
            media_kind="video",
            role="original",
        )
        return DownloadResult(files=(produced,))


def test_offline_fake_satisfies_adapter_protocol(tmp_path: Path) -> None:
    adapter = _OfflineFakeAdapter()
    assert isinstance(adapter, DownloadAdapter)

    context = AdapterContext(
        worker_id="worker-1",
        attempt_id="attempt-1",
        temporary_dir=tmp_path,
    )
    probe_request = ProbeRequest(
        job_id="job-1",
        canonical_url="https://www.youtube.com/watch?v=abc",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
    )
    result = adapter.probe(probe_request, context)
    assert result.expected_item_count == 1
    assert result.items[0].media_key == "video-1"

    download_request = DownloadRequest(
        job_id="job-1",
        source_item_id="source-1",
        canonical_url=probe_request.canonical_url,
        platform=probe_request.platform,
        source_type=probe_request.source_type,
        output_dir=tmp_path / "output",
        expected_media_keys=("video-1",),
    )
    download = adapter.download(
        download_request,
        context,
        lambda update: None,
        _NeverCancelled(),
    )
    assert download.files[0].path == tmp_path / "output" / "video.mp4"


def test_request_dtos_are_frozen() -> None:
    request = ProbeRequest(
        job_id="job-1",
        canonical_url="https://x.com/i/status/1",
        platform=Platform.X,
        source_type=SourceType.X_POST,
    )
    with pytest.raises(FrozenInstanceError):
        request.job_id = "changed"  # type: ignore[misc]


def test_adapter_failure_keeps_stable_code_and_bounds_safe_diagnostic() -> None:
    failure = AdapterFailure(
        ErrorCode.NETWORK_ERROR,
        "x" * (MAX_DIAGNOSTIC_LENGTH + 50),
        retry_after=12.5,
    )

    assert failure.code is ErrorCode.NETWORK_ERROR
    assert len(failure.diagnostic) == MAX_DIAGNOSTIC_LENGTH
    assert failure.sanitized_detail == failure.diagnostic
    assert failure.retry_after == 12.5


def test_adapter_failure_rejects_negative_retry_after() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        AdapterFailure(ErrorCode.RATE_LIMITED, "safe detail", retry_after=-1)

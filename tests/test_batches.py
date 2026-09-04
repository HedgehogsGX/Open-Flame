from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from video_download_control.domain import Platform, SourceType
from video_download_control.normalization import NormalizedURL
from video_download_control.repository import BatchRepository
from video_download_control.service import BatchService, BatchValidationError
from video_download_control.short_links import (
    ShortLinkResolution,
    ShortLinkResolutionError,
)


class _ShortLinkResolver:
    def __init__(
        self,
        result: ShortLinkResolution | ShortLinkResolutionError,
    ) -> None:
        self.result = result
        self.calls: list[str] = []

    def resolve(
        self, submitted_url: str, *, timeout_seconds: float
    ) -> ShortLinkResolution:
        assert 0 < timeout_seconds <= 15
        self.calls.append(submitted_url)
        if isinstance(self.result, ShortLinkResolutionError):
            raise self.result
        return self.result


def test_creates_batch_records_and_jobs_for_four_platforms(
    service: BatchService, repository: BatchRepository
) -> None:
    batch = service.create_batch(
        name="四平台",
        raw_inputs=[
            "https://x.com/example/status/1234567890",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=test",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://www.douyin.com/video/7123456789012345678",
        ],
    )

    assert batch["status"] == "queued"
    assert batch["total_count"] == 4
    assert batch["queued_count"] == 4
    assert batch["failed_count"] == 0
    assert len(batch["jobs"]) == 4
    assert {job["platform"] for job in batch["jobs"]} == {
        "x",
        "youtube",
        "bilibili",
        "douyin",
    }
    assert repository.count_rows("source_items") == 4


def test_preserves_invalid_and_duplicate_inputs_without_extra_jobs(
    service: BatchService,
) -> None:
    batch = service.create_batch(
        name=None,
        raw_inputs=[
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "不是 URL",
            "https://example.com/video/1",
        ],
    )

    assert batch["total_count"] == 4
    assert batch["queued_count"] == 1
    assert batch["duplicate_count"] == 1
    assert batch["failed_count"] == 2
    assert len(batch["jobs"]) == 1
    assert [item["status"] for item in batch["inputs"]] == [
        "queued",
        "duplicate",
        "failed",
        "failed",
    ]
    assert (
        batch["inputs"][1]["duplicate_of_input_record_id"] == batch["inputs"][0]["id"]
    )
    assert batch["inputs"][2]["error_code"] == "invalid_url"
    assert batch["inputs"][3]["error_code"] == "unsupported_platform"


def test_short_links_are_preserved_but_not_queued_before_safe_expansion(
    service: BatchService,
) -> None:
    batch = service.create_batch(
        name="分享文本",
        raw_inputs=[
            (
                "A https://v.douyin.com/AbCdEf/ B https://b23.tv/xyz123 "
                "C https://vm.tiktok.com/ZShort123/"
            )
        ],
    )
    assert batch["total_count"] == 3
    assert batch["queued_count"] == 0
    assert batch["failed_count"] == 3
    assert len(batch["jobs"]) == 0
    assert {item["error_code"] for item in batch["inputs"]} == {
        "short_link_resolution_required"
    }


def test_attested_short_link_resolution_queues_only_scrubbed_canonical_target(
    repository: BatchRepository,
) -> None:
    signed_location = "https://x.com/example/status/1234567890?token=must-not-persist"
    resolver = _ShortLinkResolver(
        ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url=signed_location,
                canonical_url="https://x.com/i/status/1234567890",
                platform=Platform.X,
                source_type=SourceType.X_POST,
                source_id="1234567890",
            ),
            platform=Platform.X,
            redirect_count=1,
            policy_hosts=("t.co", "x.com"),
        )
    )
    service = BatchService(
        repository=repository,
        max_batch_urls=50,
        route_policy_version="short-link-test",
        short_link_resolver=resolver,
    )

    batch = service.create_batch(
        name="attested",
        raw_inputs=["https://t.co/a?first=1", "https://t.co/a?second=2"],
    )

    assert resolver.calls == ["https://t.co/a"]
    assert batch["queued_count"] == 1
    assert batch["duplicate_count"] == 1
    assert batch["inputs"][0]["submitted_url"] == "https://t.co/a?first=1"
    assert batch["inputs"][1]["submitted_url"] == "https://t.co/a?second=2"
    assert batch["inputs"][0]["canonical_url"] == ("https://x.com/i/status/1234567890")
    assert "must-not-persist" not in repr(batch)
    assert repository.count_rows("download_jobs") == 1


def test_short_link_resolution_failure_is_fixed_and_does_not_persist_location(
    repository: BatchRepository,
) -> None:
    resolver = _ShortLinkResolver(
        ShortLinkResolutionError(
            "cross_platform",
            "private https://evil.example/path?signature=must-not-persist",
        )
    )
    service = BatchService(
        repository=repository,
        max_batch_urls=50,
        route_policy_version="short-link-test",
        short_link_resolver=resolver,
    )

    batch = service.create_batch(name=None, raw_inputs=["https://t.co/b"])

    assert batch["failed_count"] == 1
    assert batch["inputs"][0]["error_code"] == "egress_policy_blocked"
    assert batch["inputs"][0]["error_message"] == (
        "短链未通过受控展开策略，当前不会入队"
    )
    assert "must-not-persist" not in repr(batch)
    assert repository.count_rows("download_jobs") == 0


def test_attested_tiktok_short_link_queues_canonical_video_route(
    repository: BatchRepository,
) -> None:
    signed_location = (
        "https://www.tiktok.com/@Example.User/video/7461234567890123456"
        "?signature=must-not-persist"
    )
    resolver = _ShortLinkResolver(
        ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url=signed_location,
                canonical_url=(
                    "https://www.tiktok.com/@example.user/video/7461234567890123456"
                ),
                platform=Platform.TIKTOK,
                source_type=SourceType.TIKTOK_VIDEO,
                source_id="7461234567890123456",
            ),
            platform=Platform.TIKTOK,
            redirect_count=1,
            policy_hosts=("vm.tiktok.com", "www.tiktok.com"),
        )
    )
    service = BatchService(
        repository=repository,
        max_batch_urls=50,
        route_policy_version="tiktok-short-link-test",
        short_link_resolver=resolver,
    )

    batch = service.create_batch(
        name="tiktok attested",
        raw_inputs=["https://vm.tiktok.com/ZShort123/?tracking=drop"],
    )

    assert resolver.calls == ["https://vm.tiktok.com/ZShort123"]
    assert batch["queued_count"] == 1
    assert batch["inputs"][0]["submitted_url"] == (
        "https://vm.tiktok.com/ZShort123/?tracking=drop"
    )
    assert batch["inputs"][0]["canonical_url"] == (
        "https://www.tiktok.com/@example.user/video/7461234567890123456"
    )
    assert batch["jobs"][0]["platform"] == "tiktok"
    assert batch["jobs"][0]["source_type"] == "tiktok_video"
    assert "must-not-persist" not in repr(batch)
    assert repository.count_rows("download_jobs") == 1


@pytest.mark.parametrize(
    "malicious_result",
    [
        object(),
        ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url="https://x.com/user/status/123?token=resolver-only",
                canonical_url="https://x.com/i/status/123?token=resolver-only",
                platform=Platform.X,
                source_type=SourceType.X_POST,
                source_id="123",
            ),
            platform=Platform.X,
            redirect_count=1,
            policy_hosts=("t.co", "x.com"),
        ),
        ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url="https://x.com/i/status/123",
                canonical_url="https://x.com/i/status/123",
                platform=Platform.X,
                source_type=SourceType.X_POST,
                source_id="attacker-controlled-id",
            ),
            platform=Platform.X,
            redirect_count=1,
            policy_hosts=("t.co", "x.com"),
        ),
    ],
    ids=("wrong-result-type", "signed-canonical", "inconsistent-source-id"),
)
def test_short_link_resolver_postconditions_fail_closed_without_persistence(
    repository: BatchRepository,
    malicious_result: object,
) -> None:
    class MaliciousResolver:
        def resolve(self, submitted_url: str, *, timeout_seconds: float) -> object:
            del submitted_url, timeout_seconds
            return malicious_result

    service = BatchService(
        repository=repository,
        max_batch_urls=50,
        route_policy_version="short-link-postcondition-test",
        short_link_resolver=MaliciousResolver(),  # type: ignore[arg-type]
    )

    batch = service.create_batch(name=None, raw_inputs=["https://t.co/unsafe"])

    assert batch["failed_count"] == 1
    assert batch["inputs"][0]["error_code"] == "egress_policy_blocked"
    assert batch["inputs"][0]["error_message"] == (
        "短链未通过受控展开策略，当前不会入队"
    )
    assert "resolver-only" not in repr(batch)
    assert "attacker-controlled-id" not in repr(batch)
    assert repository.count_rows("download_jobs") == 0


def test_short_link_batch_budget_rejects_late_and_remaining_resolutions(
    repository: BatchRepository,
) -> None:
    now = 100.0

    class SlowResolver:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def resolve(
            self, submitted_url: str, *, timeout_seconds: float
        ) -> ShortLinkResolution:
            nonlocal now
            assert timeout_seconds == 15.0
            self.calls.append(submitted_url)
            now += 16.0
            return ShortLinkResolution(
                normalized=NormalizedURL(
                    submitted_url="https://x.com/user/status/700101?signature=late",
                    canonical_url="https://x.com/i/status/700101",
                    platform=Platform.X,
                    source_type=SourceType.X_POST,
                    source_id="700101",
                ),
                platform=Platform.X,
                redirect_count=1,
                policy_hosts=("t.co", "x.com"),
            )

    resolver = SlowResolver()
    service = BatchService(
        repository=repository,
        max_batch_urls=50,
        route_policy_version="short-link-budget-test",
        short_link_resolver=resolver,
        short_link_batch_timeout_seconds=15.0,
        monotonic=lambda: now,
    )

    batch = service.create_batch(
        name=None,
        raw_inputs=["https://t.co/slow-one", "https://t.co/slow-two"],
    )

    assert resolver.calls == ["https://t.co/slow-one"]
    assert batch["failed_count"] == 2
    assert {item["error_code"] for item in batch["inputs"]} == {"egress_policy_blocked"}
    assert "signature=late" not in repr(batch)
    assert repository.count_rows("download_jobs") == 0


@pytest.mark.parametrize("timeout", [True, 0, float("nan"), float("inf"), 61])
def test_short_link_batch_budget_is_strictly_bounded(
    repository: BatchRepository,
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="short-link batch timeout"):
        BatchService(
            repository=repository,
            max_batch_urls=50,
            route_policy_version="short-link-budget-test",
            short_link_batch_timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_x_aliases_do_not_violate_source_identity(
    service: BatchService, repository: BatchRepository
) -> None:
    batch = service.create_batch(
        name="X aliases",
        raw_inputs=[
            "https://x.com/alice/status/1234567890",
            "https://twitter.com/Bob/status/1234567890",
        ],
    )
    assert batch["queued_count"] == 1
    assert batch["duplicate_count"] == 1
    assert repository.count_rows("source_items") == 1

    second_batch = service.create_batch(
        name="X alias in another batch",
        raw_inputs=["https://x.com/Carol/status/1234567890"],
    )
    assert second_batch["status"] == "duplicate"
    assert second_batch["queued_count"] == 0
    assert second_batch["duplicate_count"] == 1
    assert repository.count_rows("source_items") == 1


def test_cross_batch_duplicate_reuses_live_source_job(
    service: BatchService, repository: BatchRepository
) -> None:
    first = service.create_batch(
        name="first",
        raw_inputs=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    second = service.create_batch(
        name="second",
        raw_inputs=["https://youtu.be/dQw4w9WgXcQ"],
    )

    assert first["queued_count"] == 1
    assert second["status"] == "duplicate"
    assert second["queued_count"] == 0
    assert second["duplicate_count"] == 1
    assert second["jobs"] == []
    assert (
        second["inputs"][0]["duplicate_of_input_record_id"] == first["inputs"][0]["id"]
    )
    assert repository.count_rows("download_jobs") == 1


def test_raw_text_preserves_original_whitespace(service: BatchService) -> None:
    raw = "  分享 https://www.youtube.com/watch?v=dQw4w9WgXcQ  \r\n"
    batch = service.create_batch(name=None, raw_inputs=[raw])
    assert batch["inputs"][0]["raw_text"] == raw


def test_rejects_more_than_configured_limit(service: BatchService) -> None:
    links = [f"https://x.com/example/status/{1000 + index}" for index in range(51)]
    with pytest.raises(BatchValidationError, match="最多接受 50"):
        service.create_batch(name=None, raw_inputs=links)


def test_accepts_exactly_configured_limit(service: BatchService) -> None:
    links = [f"https://x.com/example/status/{2000 + index}" for index in range(50)]
    batch = service.create_batch(name="limit", raw_inputs=links)
    assert batch["total_count"] == 50
    assert batch["queued_count"] == 50


def test_concurrent_duplicate_submissions_create_one_live_job(
    service: BatchService, repository: BatchRepository
) -> None:
    barrier = Barrier(2)

    def submit(name: str) -> dict:
        barrier.wait()
        return service.create_batch(
            name=name,
            raw_inputs=["https://www.youtube.com/watch?v=concurrent01"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(submit, "one")
        second_future = executor.submit(submit, "two")
        batches = [first_future.result(), second_future.result()]

    assert sorted(batch["queued_count"] for batch in batches) == [0, 1]
    assert sorted(batch["duplicate_count"] for batch in batches) == [0, 1]
    assert repository.count_rows("source_items") == 1
    assert repository.count_rows("download_jobs") == 1


def test_x_graph_v2_gate_creates_input_owned_discover_jobs_without_changing_flat_v1(
    repository: BatchRepository, settings
) -> None:
    graph_service = BatchService(
        repository=repository,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
        x_graph_v2_enabled=True,
    )
    first = graph_service.create_batch(
        name="graph-one",
        raw_inputs=["https://x.com/first/status/888001"],
    )
    second = graph_service.create_batch(
        name="graph-two",
        raw_inputs=["https://twitter.com/second/status/888001"],
    )
    flat = graph_service.create_batch(
        name="flat-youtube",
        raw_inputs=["https://youtu.be/graph-gate-flat"],
    )

    assert first["jobs"][0]["job_kind"] == "discover"
    assert second["jobs"][0]["job_kind"] == "discover"
    assert first["jobs"][0]["source_item_id"] == second["jobs"][0]["source_item_id"]
    assert first["jobs"][0]["id"] != second["jobs"][0]["id"]
    assert second["status"] == "queued"
    assert second["duplicate_count"] == 0
    assert flat["jobs"][0]["job_kind"] == "download"

from __future__ import annotations

import pytest

from video_download_control.capabilities import (
    DEFAULT_DOWNLOAD_CAPABILITIES,
    AdapterJobKind,
    AdapterRoute,
    AuthenticationMode,
    CapabilityStatus,
    DownloadCapability,
    PlatformCapabilityRegistry,
    ShortLinkStatus,
)
from video_download_control.domain import Platform, SourceType


def test_default_registry_declares_six_candidate_platforms() -> None:
    capabilities = DEFAULT_DOWNLOAD_CAPABILITIES.list(adapter="yt_dlp")

    assert {item.route.platform for item in capabilities} == set(Platform)
    assert all(item.status is CapabilityStatus.CANDIDATE for item in capabilities)
    assert all(
        item.authentication is AuthenticationMode.OPTIONAL_COOKIE
        for item in capabilities
    )
    assert AdapterRoute(
        Platform.BILIBILI, SourceType.BILIBILI_VIDEO
    ) in DEFAULT_DOWNLOAD_CAPABILITIES.claimable_routes(adapter="yt_dlp")


def test_disabled_capability_is_visible_but_not_claimable() -> None:
    route = AdapterRoute(Platform.INSTAGRAM, SourceType.INSTAGRAM_REEL)
    registry = PlatformCapabilityRegistry(
        (
            DownloadCapability(
                adapter="isolated_instagram",
                route=route,
                status=CapabilityStatus.DISABLED,
                authentication=AuthenticationMode.REQUIRED_COOKIE,
            ),
        )
    )

    assert registry.list(adapter="isolated_instagram")[0].route == route
    assert registry.claimable_routes(adapter="isolated_instagram") == frozenset()


def test_registry_rejects_duplicate_adapter_route() -> None:
    capability = DownloadCapability(
        adapter="yt_dlp",
        route=AdapterRoute(Platform.BILIBILI, SourceType.BILIBILI_VIDEO),
        status=CapabilityStatus.CANDIDATE,
        authentication=AuthenticationMode.OPTIONAL_COOKIE,
    )

    with pytest.raises(ValueError, match="duplicate"):
        PlatformCapabilityRegistry((capability, capability))


@pytest.mark.parametrize(
    ("platform", "source_type", "job_kind"),
    (
        (Platform.BILIBILI, SourceType.YOUTUBE_VIDEO, AdapterJobKind.DOWNLOAD),
        (Platform.INSTAGRAM, SourceType.SHORT_LINK, AdapterJobKind.DOWNLOAD),
        (Platform.TIKTOK, SourceType.TIKTOK_VIDEO, AdapterJobKind.DISCOVER),
    ),
)
def test_adapter_route_rejects_invalid_domain_combinations(
    platform: Platform,
    source_type: SourceType,
    job_kind: AdapterJobKind,
) -> None:
    with pytest.raises(ValueError, match="combination"):
        AdapterRoute(platform, source_type, job_kind)


def test_public_capability_record_contains_only_declared_fields() -> None:
    payload = DEFAULT_DOWNLOAD_CAPABILITIES.list(adapter="yt_dlp")[0].to_public_dict()

    assert set(payload) == {
        "platform",
        "source_type",
        "job_kind",
        "adapter",
        "status",
        "authentication",
        "adapter_version",
        "environment",
        "short_link_status",
    }


def test_verified_capability_cannot_be_promoted_by_static_declaration() -> None:
    with pytest.raises(ValueError, match="Stage 0 evidence"):
        DownloadCapability(
            adapter="yt_dlp",
            route=AdapterRoute(Platform.BILIBILI, SourceType.BILIBILI_VIDEO),
            status=CapabilityStatus.VERIFIED,
            authentication=AuthenticationMode.OPTIONAL_COOKIE,
            adapter_version="2026.08.19",
            environment="windows-x64-direct",
            short_link_status=ShortLinkStatus.GATED,
        )


def test_default_short_link_status_distinguishes_supported_gated_and_deferred() -> None:
    by_platform_and_type = {
        (item.route.platform, item.route.source_type): item.short_link_status
        for item in DEFAULT_DOWNLOAD_CAPABILITIES.list(adapter="yt_dlp")
    }

    assert by_platform_and_type[(Platform.YOUTUBE, SourceType.YOUTUBE_VIDEO)] is (
        ShortLinkStatus.SUPPORTED
    )
    assert by_platform_and_type[(Platform.BILIBILI, SourceType.BILIBILI_VIDEO)] is (
        ShortLinkStatus.GATED
    )
    assert by_platform_and_type[(Platform.TIKTOK, SourceType.TIKTOK_VIDEO)] is (
        ShortLinkStatus.DEFERRED
    )

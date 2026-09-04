"""Declared download routes and public validation status.

The registry describes what an adapter is allowed to claim.  ``candidate``
means the route is implemented but has not met the project's Stage 0 sample
gate; it must never be presented as platform-wide verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .domain import Platform, SourceType


class AdapterJobKind(StrEnum):
    DISCOVER = "discover"
    DOWNLOAD = "download"


class CapabilityStatus(StrEnum):
    DISABLED = "disabled"
    CANDIDATE = "candidate"
    VERIFIED = "verified"


class AuthenticationMode(StrEnum):
    NONE = "none"
    OPTIONAL_COOKIE = "optional_cookie"
    REQUIRED_COOKIE = "required_cookie"


class ShortLinkStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    SUPPORTED = "supported"
    GATED = "gated"
    DEFERRED = "deferred"


_ALLOWED_ROUTE_JOB_KINDS = {
    (Platform.X, SourceType.X_POST): frozenset(
        {AdapterJobKind.DISCOVER, AdapterJobKind.DOWNLOAD}
    ),
    (Platform.X, SourceType.X_ATTACHMENT): frozenset(
        {AdapterJobKind.DOWNLOAD}
    ),
    (Platform.YOUTUBE, SourceType.YOUTUBE_VIDEO): frozenset(
        {AdapterJobKind.DOWNLOAD}
    ),
    (Platform.YOUTUBE, SourceType.YOUTUBE_SHORT): frozenset(
        {AdapterJobKind.DOWNLOAD}
    ),
    (Platform.BILIBILI, SourceType.BILIBILI_VIDEO): frozenset(
        {AdapterJobKind.DOWNLOAD}
    ),
    (Platform.DOUYIN, SourceType.DOUYIN_VIDEO): frozenset(
        {AdapterJobKind.DOWNLOAD}
    ),
    (Platform.TIKTOK, SourceType.TIKTOK_VIDEO): frozenset(
        {AdapterJobKind.DOWNLOAD}
    ),
    (Platform.INSTAGRAM, SourceType.INSTAGRAM_REEL): frozenset(
        {AdapterJobKind.DOWNLOAD}
    ),
}


@dataclass(frozen=True, slots=True)
class AdapterRoute:
    """One explicit platform/source/job route supported by an adapter."""

    platform: Platform
    source_type: SourceType
    job_kind: AdapterJobKind = AdapterJobKind.DOWNLOAD

    def __post_init__(self) -> None:
        if not isinstance(self.platform, Platform):
            raise TypeError("adapter route platform must be a Platform")
        if not isinstance(self.source_type, SourceType):
            raise TypeError("adapter route source_type must be a SourceType")
        if not isinstance(self.job_kind, AdapterJobKind):
            raise TypeError("adapter route job_kind must be an AdapterJobKind")
        allowed_job_kinds = _ALLOWED_ROUTE_JOB_KINDS.get(
            (self.platform, self.source_type)
        )
        if allowed_job_kinds is None or self.job_kind not in allowed_job_kinds:
            raise ValueError("adapter route platform/source/job combination is invalid")


@dataclass(frozen=True, slots=True)
class DownloadCapability:
    adapter: str
    route: AdapterRoute
    status: CapabilityStatus
    authentication: AuthenticationMode
    adapter_version: str | None = None
    environment: str | None = None
    short_link_status: ShortLinkStatus = ShortLinkStatus.NOT_APPLICABLE

    def __post_init__(self) -> None:
        if not self.adapter or len(self.adapter) > 64:
            raise ValueError("capability adapter name is invalid")
        if not isinstance(self.route, AdapterRoute):
            raise TypeError("capability route must be an AdapterRoute")
        if not isinstance(self.status, CapabilityStatus):
            raise TypeError("capability status must be a CapabilityStatus")
        if not isinstance(self.authentication, AuthenticationMode):
            raise TypeError("capability authentication must be an AuthenticationMode")
        if self.adapter_version is not None and (
            not self.adapter_version or len(self.adapter_version) > 64
        ):
            raise ValueError("capability adapter version is invalid")
        if self.environment is not None and (
            not self.environment or len(self.environment) > 128
        ):
            raise ValueError("capability environment is invalid")
        if self.status is CapabilityStatus.VERIFIED:
            raise ValueError(
                "verified capability must be derived from Stage 0 evidence, not declared statically"
            )
        if not isinstance(self.short_link_status, ShortLinkStatus):
            raise TypeError("short-link status must be a ShortLinkStatus")

    def to_public_dict(self) -> dict[str, str | None]:
        return {
            "platform": self.route.platform.value,
            "source_type": self.route.source_type.value,
            "job_kind": self.route.job_kind.value,
            "adapter": self.adapter,
            "status": self.status.value,
            "authentication": self.authentication.value,
            "adapter_version": self.adapter_version,
            "environment": self.environment,
            "short_link_status": self.short_link_status.value,
        }


class PlatformCapabilityRegistry:
    """Immutable registry used for Worker routing and public introspection."""

    def __init__(self, capabilities: tuple[DownloadCapability, ...]) -> None:
        if not isinstance(capabilities, tuple):
            raise TypeError("capabilities must be a tuple")
        identities: set[tuple[str, AdapterRoute]] = set()
        for capability in capabilities:
            if not isinstance(capability, DownloadCapability):
                raise TypeError("registry entries must be DownloadCapability values")
            identity = (capability.adapter, capability.route)
            if identity in identities:
                raise ValueError("duplicate adapter capability route")
            identities.add(identity)
        self._capabilities = tuple(
            sorted(
                capabilities,
                key=lambda item: (
                    item.route.platform.value,
                    item.route.source_type.value,
                    item.route.job_kind.value,
                    item.adapter,
                ),
            )
        )

    def list(self, *, adapter: str | None = None) -> tuple[DownloadCapability, ...]:
        if adapter is None:
            return self._capabilities
        return tuple(item for item in self._capabilities if item.adapter == adapter)

    def claimable_routes(self, *, adapter: str) -> frozenset[AdapterRoute]:
        return frozenset(
            item.route
            for item in self._capabilities
            if item.adapter == adapter
            and item.status in {CapabilityStatus.CANDIDATE, CapabilityStatus.VERIFIED}
        )


def _yt_dlp_capability(
    platform: Platform,
    source_type: SourceType,
    *,
    short_link_status: ShortLinkStatus = ShortLinkStatus.NOT_APPLICABLE,
) -> DownloadCapability:
    return DownloadCapability(
        adapter="yt_dlp",
        route=AdapterRoute(platform, source_type),
        status=CapabilityStatus.CANDIDATE,
        authentication=AuthenticationMode.OPTIONAL_COOKIE,
        short_link_status=short_link_status,
    )


DEFAULT_DOWNLOAD_CAPABILITIES = PlatformCapabilityRegistry(
    (
        _yt_dlp_capability(
            Platform.X,
            SourceType.X_POST,
            short_link_status=ShortLinkStatus.GATED,
        ),
        _yt_dlp_capability(
            Platform.YOUTUBE,
            SourceType.YOUTUBE_VIDEO,
            short_link_status=ShortLinkStatus.SUPPORTED,
        ),
        _yt_dlp_capability(Platform.YOUTUBE, SourceType.YOUTUBE_SHORT),
        _yt_dlp_capability(
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
            short_link_status=ShortLinkStatus.GATED,
        ),
        _yt_dlp_capability(
            Platform.DOUYIN,
            SourceType.DOUYIN_VIDEO,
            short_link_status=ShortLinkStatus.GATED,
        ),
        _yt_dlp_capability(
            Platform.TIKTOK,
            SourceType.TIKTOK_VIDEO,
            short_link_status=ShortLinkStatus.GATED,
        ),
        _yt_dlp_capability(Platform.INSTAGRAM, SourceType.INSTAGRAM_REEL),
    )
)

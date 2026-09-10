from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .domain import ErrorCode, InputStatus, SourceType
from .credential_defaults import CredentialDefaults, CredentialMode, validate_credential_mode
from .normalization import (
    NormalizedURL,
    URLNormalizationError,
    extract_urls,
    normalize_url,
)
from .repository import BatchRepository
from .short_links import ShortLinkResolution


class BatchValidationError(ValueError):
    pass


class ShortLinkResolver(Protocol):
    def resolve(
        self,
        submitted_url: str,
        *,
        timeout_seconds: float,
    ) -> ShortLinkResolution: ...


@dataclass(slots=True)
class BatchService:
    repository: BatchRepository
    max_batch_urls: int
    route_policy_version: str
    x_graph_v2_enabled: bool = False
    short_link_resolver: ShortLinkResolver | None = None
    short_link_batch_timeout_seconds: float = 15.0
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    credential_defaults: CredentialDefaults | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        timeout = self.short_link_batch_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError(
                "short-link batch timeout must be greater than zero and at most 60 seconds"
            )

    def create_batch(
        self,
        *,
        name: str | None,
        raw_inputs: list[str],
        credential_mode: CredentialMode = "use_default",
    ) -> dict[str, Any]:
        validate_credential_mode(credential_mode)
        if not raw_inputs:
            raise BatchValidationError("至少需要一条输入")

        expanded: list[tuple[str, str | None]] = []
        for original_raw_text in raw_inputs:
            parse_text = original_raw_text.strip()
            urls = extract_urls(parse_text)
            if urls:
                expanded.extend((original_raw_text, url) for url in urls)
            else:
                expanded.append((original_raw_text, None))

        if len(expanded) > self.max_batch_urls:
            raise BatchValidationError(f"每个批次最多接受 {self.max_batch_urls} 个 URL")

        prepared: list[dict[str, Any]] = []
        first_ordinal_by_canonical: dict[str, int] = {}
        short_link_cache: dict[str, NormalizedURL] = {}
        short_link_failures: set[str] = set()
        short_link_deadline: float | None = None
        for ordinal, (raw_text, submitted_url) in enumerate(expanded, start=1):
            if not submitted_url:
                prepared.append(
                    {
                        "ordinal": ordinal,
                        "raw_text": raw_text,
                        "submitted_url": None,
                        "status": InputStatus.FAILED,
                        "error_code": ErrorCode.INVALID_URL,
                        "error_message": "输入中未找到 HTTP/HTTPS URL",
                    }
                )
                continue
            try:
                normalized = normalize_url(submitted_url)
            except URLNormalizationError as exc:
                prepared.append(
                    {
                        "ordinal": ordinal,
                        "raw_text": raw_text,
                        "submitted_url": submitted_url,
                        "status": InputStatus.FAILED,
                        "error_code": exc.code,
                        "error_message": str(exc),
                    }
                )
                continue

            if normalized.source_type == SourceType.SHORT_LINK:
                if self.short_link_resolver is None:
                    prepared.append(
                        {
                            "ordinal": ordinal,
                            "raw_text": raw_text,
                            "normalized": normalized,
                            "status": InputStatus.FAILED,
                            "error_code": ErrorCode.SHORT_LINK_RESOLUTION_REQUIRED,
                            "error_message": (
                                "短链尚未经过逐跳 DNS/IP 与目标域名安全验证，当前不会入队"
                            ),
                        }
                    )
                    continue

                short_link_key = normalized.canonical_url
                cached = short_link_cache.get(short_link_key)
                if cached is None and short_link_key not in short_link_failures:
                    now = self.monotonic()
                    if short_link_deadline is None:
                        short_link_deadline = (
                            now + self.short_link_batch_timeout_seconds
                        )
                    if now >= short_link_deadline:
                        short_link_failures.add(short_link_key)
                    else:
                        try:
                            resolution = self.short_link_resolver.resolve(
                                normalized.canonical_url,
                                timeout_seconds=short_link_deadline - now,
                            )
                            if not isinstance(resolution, ShortLinkResolution):
                                raise TypeError(
                                    "short-link resolver returned an invalid result"
                                )
                            resolved = resolution.normalized
                            if not isinstance(resolved, NormalizedURL):
                                raise TypeError(
                                    "short-link resolver returned an invalid target"
                                )
                            verified = normalize_url(resolved.canonical_url)
                            if (
                                resolution.platform is not normalized.platform
                                or verified.platform is not normalized.platform
                                or verified.source_type is SourceType.SHORT_LINK
                                or resolved.canonical_url != verified.canonical_url
                                or resolved.platform is not verified.platform
                                or resolved.source_type is not verified.source_type
                                or resolved.source_id != verified.source_id
                            ):
                                raise ValueError(
                                    "short-link resolver postcondition failed"
                                )
                            # Preserve only the user's submitted short URL.  The
                            # resolver's final Location can contain a replayable
                            # signature and must never enter the business DB/API.
                            cached = NormalizedURL(
                                submitted_url=short_link_key,
                                canonical_url=verified.canonical_url,
                                platform=verified.platform,
                                source_type=verified.source_type,
                                source_id=verified.source_id,
                            )
                        except Exception:  # noqa: BLE001 - resolver trust boundary
                            short_link_failures.add(short_link_key)
                        else:
                            if self.monotonic() > short_link_deadline:
                                short_link_failures.add(short_link_key)
                                cached = None
                            else:
                                short_link_cache[short_link_key] = cached
                if short_link_key in short_link_failures:
                    prepared.append(
                        {
                            "ordinal": ordinal,
                            "raw_text": raw_text,
                            "normalized": normalized,
                            "status": InputStatus.FAILED,
                            "error_code": ErrorCode.EGRESS_POLICY_BLOCKED,
                            "error_message": ("短链未通过受控展开策略，当前不会入队"),
                        }
                    )
                    continue
                if cached is None:
                    raise RuntimeError("short-link resolution cache is inconsistent")
                normalized = NormalizedURL(
                    submitted_url=submitted_url,
                    canonical_url=cached.canonical_url,
                    platform=cached.platform,
                    source_type=cached.source_type,
                    source_id=cached.source_id,
                )

            duplicate_of = first_ordinal_by_canonical.get(normalized.canonical_url)
            if duplicate_of is not None:
                prepared.append(
                    {
                        "ordinal": ordinal,
                        "raw_text": raw_text,
                        "normalized": normalized,
                        "status": InputStatus.DUPLICATE,
                        "duplicate_of_ordinal": duplicate_of,
                    }
                )
                continue

            first_ordinal_by_canonical[normalized.canonical_url] = ordinal
            prepared.append(
                {
                    "ordinal": ordinal,
                    "raw_text": raw_text,
                    "normalized": normalized,
                    "status": InputStatus.QUEUED,
                }
            )

        return self.repository.create_batch(
            name=name.strip() if name and name.strip() else None,
            inputs=prepared,
            route_policy_version=self.route_policy_version,
            enable_x_graph_v2=self.x_graph_v2_enabled,
            credential_mode=credential_mode,
            credential_defaults=self.credential_defaults,
        )

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self.repository.get_batch(batch_id)

    def list_batches(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.repository.list_batches(limit=limit)

    def find_batches_by_name(self, name: str) -> list[dict[str, Any]]:
        return self.repository.find_batches_by_name(name)

    def list_ready_assets_for_batch(
        self, batch_id: str
    ) -> list[dict[str, Any]] | None:
        return self.repository.list_ready_assets_for_batch(batch_id)

    def inspect_single_input_download(
        self, batch_id: str
    ) -> dict[str, Any] | None:
        return self.repository.inspect_single_input_download(batch_id)

    def get_ready_original_asset(self, asset_id: str) -> dict[str, Any] | None:
        return self.repository.get_ready_original_asset(asset_id)

    def get_ready_auxiliary_artifact(
        self, artifact_id: str
    ) -> dict[str, Any] | None:
        return self.repository.get_ready_auxiliary_artifact(artifact_id)

    def list_ready_captions_for_asset(
        self, asset_id: str
    ) -> list[dict[str, Any]]:
        return self.repository.list_ready_captions_for_asset(asset_id)

    def list_registered_thumbnails_for_asset(
        self, asset_id: str
    ) -> list[dict[str, Any]]:
        return self.repository.list_registered_thumbnails_for_asset(asset_id)

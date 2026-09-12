"""Pure upload metadata normalization shared by upload clients.

This module deliberately has no database, filesystem, account, or runtime
dependencies.  Those stateful checks remain in :mod:`uploads.service`.
"""

from __future__ import annotations

import json

from .contracts import UploadError


TITLE_LIMITS = {"bilibili": 80, "douyin": 30, "tencent": 100}
# Reserve the platform's minimum lead plus the real backend's two-hour upload
# timeout and five minutes for process/form overhead. Browser adapters perform
# a separate final-action check against the platform minimum itself.
SCHEDULE_LEAD_SECONDS = {
    "bilibili": 6 * 60 * 60 + 5 * 60,
    "douyin": 4 * 60 * 60 + 5 * 60,
    "tencent": 4 * 60 * 60 + 5 * 60,
}
DOUYIN_DECLARATIONS = frozenset(
    {
        "内容由AI生成",
        "内容为转载信息",
        "内容为个人观点或见解",
    }
)
TENCENT_CONTENT_LABELS = frozenset({"含AI生成内容"})
TENCENT_SCHEDULE_MAX_SECONDS = 28 * 24 * 60 * 60
PLATFORM_OPTION_KEYS = {
    "bilibili": frozenset(
        {"dynamic", "no_reprint", "close_comments", "close_danmu"}
    ),
    "douyin": frozenset({"declaration"}),
    "tencent": frozenset({"short_title", "content_label"}),
}
TARGET_OVERRIDE_KEYS = frozenset(
    {
        "account_id",
        "title",
        "description",
        "tags",
        "category_id",
        "mode",
        "copyright",
        "source_credit",
        "cover_landscape_asset_id",
        "cover_portrait_asset_id",
        "publish_at_unix",
        "publish_timezone_offset_minutes",
        "platform_options",
    }
)


def normalize_upload_text(
    value: object,
    maximum: int,
    *,
    required: bool = False,
) -> str:
    """Return trimmed metadata text under the upload-domain character rules."""

    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(
            ord(character) < 32 and character not in "\n\t\r"
            for character in value
        )
    ):
        raise UploadError("invalid_metadata")
    if required and not value.strip():
        raise UploadError("invalid_metadata")
    return value.strip()


def normalize_upload_tags(tags: object) -> list[str]:
    """Normalize the canonical, ordered upload tag list."""

    if not isinstance(tags, list) or len(tags) > 10:
        raise UploadError("invalid_tags")
    normalized = [
        normalize_upload_text(tag, 20, required=True) for tag in tags
    ]
    if len(set(normalized)) != len(normalized) or any(
        any(character in tag for character in ",，#＃\n\r\t")
        for tag in normalized
    ):
        raise UploadError("invalid_tags")
    return normalized


def normalize_platform_options(platform: str, value: object) -> dict:
    """Normalize one platform's optional submission parameters."""

    if value is None:
        value = {}
    if (
        not isinstance(value, dict)
        or set(value) - PLATFORM_OPTION_KEYS[platform]
    ):
        raise UploadError("unsupported_platform_option")
    if platform == "bilibili":
        dynamic_value = value.get("dynamic", "")
        dynamic = (
            ""
            if dynamic_value is None
            else normalize_upload_text(dynamic_value, 250)
        )
        options = {"dynamic": dynamic}
        for key in ("no_reprint", "close_comments", "close_danmu"):
            option = value.get(key, False)
            if type(option) is not bool:
                raise UploadError("invalid_platform_option")
            options[key] = option
    elif platform == "douyin":
        declaration = value.get("declaration")
        if declaration is not None:
            declaration = normalize_upload_text(
                declaration, 30, required=True
            )
            if declaration not in DOUYIN_DECLARATIONS:
                raise UploadError("unsupported_declaration")
        options = {"declaration": declaration}
    else:
        short_title = value.get("short_title")
        if short_title is not None:
            short_title = normalize_upload_text(short_title, 15, required=True)
            if len(short_title) < 7:
                raise UploadError("tencent_short_title_length")
        content_label = value.get("content_label")
        if content_label is not None:
            content_label = normalize_upload_text(
                content_label, 30, required=True
            )
            if content_label not in TENCENT_CONTENT_LABELS:
                raise UploadError("unsupported_content_label")
        options = {
            "short_title": short_title,
            "content_label": content_label,
        }
    if (
        len(
            json.dumps(
                options,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        > 4096
    ):
        raise UploadError("platform_options_too_large")
    return options


def cover_slot(platform: str, width: int, height: int) -> str | None:
    """Choose the target cover field for verified positive image dimensions."""
    if platform == "bilibili":
        return "cover_landscape_asset_id" if width >= height else None
    if platform == "douyin":
        return "cover_landscape_asset_id" if width >= height else "cover_portrait_asset_id"
    if platform == "tencent":
        ratio = width / height
        if abs(ratio - 4 / 3) <= 0.04:
            return "cover_landscape_asset_id"
        if abs(ratio - 3 / 4) <= 0.04:
            return "cover_portrait_asset_id"
    return None


def cover_slot_error(
    platform: str, *, landscape: bool, portrait: bool
) -> str | None:
    """Check slot presence before callers read media or inspect its dimensions."""
    if portrait and platform not in {"douyin", "tencent"}:
        return "portrait_cover_unsupported"
    if platform == "douyin" and landscape and portrait:
        return "multiple_covers_unsupported"
    return None


def cover_dimensions_error(
    platform: str,
    *,
    landscape: tuple[int, int] | None,
    portrait: tuple[int, int] | None,
) -> str | None:
    """Check a valid slot combination using verified positive dimensions only.

    The caller owns media presence, byte identity and error/transaction policy.
    Reusing cover_slot keeps Workflow selection and persisted job rules aligned.
    """
    code = {
        "bilibili": "bilibili_cover_orientation_invalid",
        "douyin": "douyin_cover_orientation_invalid",
        "tencent": "tencent_cover_ratio_invalid",
    }.get(platform)
    if code is not None and (
        landscape is not None and cover_slot(platform, *landscape) != "cover_landscape_asset_id"
        or portrait is not None and cover_slot(platform, *portrait) != "cover_portrait_asset_id"
    ):
        return code
    return None


def validate_publish_schedule(
    platform: str,
    publish_at_unix: object,
    offset_minutes: object,
    *,
    now: int,
    enforce_tencent_horizon: bool = True,
) -> tuple[int | None, int | None]:
    """Validate and return one canonical publication schedule."""

    if publish_at_unix is None:
        if offset_minutes is not None:
            raise UploadError("publish_timezone_without_time")
        return None, None
    if (
        type(publish_at_unix) is not int
        or not 1_700_000_000 <= publish_at_unix <= 4_102_444_800
        or type(offset_minutes) is not int
        or not -840 <= offset_minutes <= 840
    ):
        raise UploadError("invalid_publish_time")
    if publish_at_unix <= now + SCHEDULE_LEAD_SECONDS[platform]:
        raise UploadError("publish_time_too_soon")
    if publish_at_unix % 60:
        raise UploadError("publish_time_precision_unsupported")
    if platform == "tencent":
        if (publish_at_unix + offset_minutes * 60) % 3600:
            raise UploadError("tencent_schedule_requires_whole_hour")
        if (
            enforce_tencent_horizon
            and publish_at_unix > now + TENCENT_SCHEDULE_MAX_SECONDS
        ):
            raise UploadError("tencent_schedule_too_far")
    return publish_at_unix, offset_minutes


__all__ = [
    "cover_slot",
    "cover_slot_error",
    "cover_dimensions_error",
    "DOUYIN_DECLARATIONS",
    "PLATFORM_OPTION_KEYS",
    "SCHEDULE_LEAD_SECONDS",
    "TARGET_OVERRIDE_KEYS",
    "TENCENT_CONTENT_LABELS",
    "TENCENT_SCHEDULE_MAX_SECONDS",
    "TITLE_LIMITS",
    "normalize_platform_options",
    "normalize_upload_tags",
    "normalize_upload_text",
    "validate_publish_schedule",
]

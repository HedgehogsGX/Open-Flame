from __future__ import annotations

import pytest

from video_download_control.domain import ErrorCode, Platform, SourceType
from video_download_control.normalization import (
    URLNormalizationError,
    extract_urls,
    normalize_url,
)


@pytest.mark.parametrize(
    ("submitted", "canonical", "platform", "source_type", "source_id"),
    [
        (
            "https://twitter.com/example/status/1234567890?s=20",
            "https://x.com/i/status/1234567890",
            Platform.X,
            SourceType.X_POST,
            "1234567890",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?si=tracker",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_VIDEO,
            "dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com:443/watch?v=explicit-default",
            "https://www.youtube.com/watch?v=explicit-default",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_VIDEO,
            "explicit-default",
        ),
        (
            "http://www.youtube.com:80/watch?v=explicit-http-default",
            "https://www.youtube.com/watch?v=explicit-http-default",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_VIDEO,
            "explicit-http-default",
        ),
        (
            "https://www.youtube.com/shorts/abcdefghijk?feature=share",
            "https://www.youtube.com/shorts/abcdefghijk",
            Platform.YOUTUBE,
            SourceType.YOUTUBE_SHORT,
            "abcdefghijk",
        ),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333.1",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
            "BV1xx411c7mD",
        ),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD?p=1&spm_id_from=333.1",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
            "BV1xx411c7mD",
        ),
        (
            "https://www.douyin.com/video/7123456789012345678?previous_page=app_code_link",
            "https://www.douyin.com/video/7123456789012345678",
            Platform.DOUYIN,
            SourceType.DOUYIN_VIDEO,
            "7123456789012345678",
        ),
        (
            "https://www.tiktok.com/@Example.User/video/7461234567890123456?is_from_webapp=1",
            "https://www.tiktok.com/@example.user/video/7461234567890123456",
            Platform.TIKTOK,
            SourceType.TIKTOK_VIDEO,
            "7461234567890123456",
        ),
        (
            "https://m.instagram.com/reels/AbC_def-123/?igsh=tracking",
            "https://www.instagram.com/reel/AbC_def-123",
            Platform.INSTAGRAM,
            SourceType.INSTAGRAM_REEL,
            "AbC_def-123",
        ),
    ],
)
def test_normalizes_supported_mvp_links(
    submitted: str,
    canonical: str,
    platform: Platform,
    source_type: SourceType,
    source_id: str,
) -> None:
    result = normalize_url(submitted)
    assert result.canonical_url == canonical
    assert result.platform == platform
    assert result.source_type == source_type
    assert result.source_id == source_id


def test_extracts_urls_from_share_text_and_strips_punctuation() -> None:
    text = "复制链接 https://v.douyin.com/AbCdEf/，另见 https://b23.tv/xyz123。"
    assert extract_urls(text) == [
        "https://v.douyin.com/AbCdEf/",
        "https://b23.tv/xyz123",
    ]


def test_x_aliases_share_stable_canonical_identity() -> None:
    first = normalize_url("https://x.com/alice/status/1234567890")
    second = normalize_url("https://twitter.com/Bob/status/1234567890")
    assert first.canonical_url == "https://x.com/i/status/1234567890"
    assert second.canonical_url == first.canonical_url
    assert second.source_id == first.source_id


@pytest.mark.parametrize(
    ("url", "canonical", "source_id"),
    (
        (
            "https://x.com/example/status/0001234567890",
            "https://x.com/i/status/1234567890",
            "1234567890",
        ),
        (
            "https://www.bilibili.com/video/av000170001",
            "https://www.bilibili.com/video/av170001",
            "av170001",
        ),
        (
            "https://www.douyin.com/video/0007123456789012345678",
            "https://www.douyin.com/video/7123456789012345678",
            "7123456789012345678",
        ),
        (
            "https://www.tiktok.com/@Example/video/0007461234567890123456",
            "https://www.tiktok.com/@example/video/7461234567890123456",
            "7461234567890123456",
        ),
    ),
)
def test_numeric_source_ids_remove_leading_zero_aliases(
    url: str,
    canonical: str,
    source_id: str,
) -> None:
    result = normalize_url(url)

    assert result.canonical_url == canonical
    assert result.source_id == source_id


@pytest.mark.parametrize("host", ("vm.tiktok.com", "vt.tiktok.com"))
def test_normalizes_tiktok_share_short_links(host: str) -> None:
    result = normalize_url(f"http://{host}/ZShort123/?share_app_id=123#fragment")

    assert result.canonical_url == f"https://{host}/ZShort123"
    assert result.platform is Platform.TIKTOK
    assert result.source_type is SourceType.SHORT_LINK
    assert result.source_id is None


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://www.tiktok.com/@x/video/1", ErrorCode.UNSUPPORTED_LINK_TYPE),
        ("https://www.tiktok.com/@x", ErrorCode.UNSUPPORTED_LINK_TYPE),
        ("https://vm.tiktok.com/", ErrorCode.INVALID_URL),
        ("https://vt.tiktok.com/", ErrorCode.INVALID_URL),
        ("https://www.instagram.com/p/AbC_def-123/", ErrorCode.UNSUPPORTED_LINK_TYPE),
        ("https://www.instagram.com/example/", ErrorCode.UNSUPPORTED_LINK_TYPE),
        ("https://127.0.0.1/video/1", ErrorCode.UNSUPPORTED_PLATFORM),
        ("https://user:secret@www.youtube.com/watch?v=abc", ErrorCode.INVALID_URL),
        ("https://www.youtube.com:8443/watch?v=abc", ErrorCode.INVALID_URL),
        ("https://www.youtube.com:80/watch?v=abc", ErrorCode.INVALID_URL),
        ("http://www.youtube.com:443/watch?v=abc", ErrorCode.INVALID_URL),
        ("https://www.youtube.com/playlist?list=abc", ErrorCode.UNSUPPORTED_LINK_TYPE),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
            ErrorCode.UNSUPPORTED_LINK_TYPE,
        ),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD?p=first",
            ErrorCode.UNSUPPORTED_LINK_TYPE,
        ),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD?p=",
            ErrorCode.UNSUPPORTED_LINK_TYPE,
        ),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD?p=1&p=",
            ErrorCode.UNSUPPORTED_LINK_TYPE,
        ),
        ("https://x.com/example/status/0", ErrorCode.INVALID_URL),
        ("https://www.bilibili.com/video/av000", ErrorCode.INVALID_URL),
        ("https://www.douyin.com/video/0", ErrorCode.INVALID_URL),
        ("https://www.tiktok.com/@example/video/00000", ErrorCode.INVALID_URL),
    ],
)
def test_rejects_unsafe_or_out_of_scope_links(url: str, code: ErrorCode) -> None:
    with pytest.raises(URLNormalizationError) as exc_info:
        normalize_url(url)
    assert exc_info.value.code == code

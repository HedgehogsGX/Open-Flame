from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .domain import ErrorCode, Platform, SourceType


# Platform share URLs are ASCII. Restricting the match prevents adjacent CJK
# prose (which often has no whitespace before punctuation) from becoming part
# of the URL.
URL_PATTERN = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+", re.IGNORECASE
)
TRAILING_PUNCTUATION = ").,;:!?]}>'\"，。；：！？）】》」』"
class URLNormalizationError(ValueError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NormalizedURL:
    submitted_url: str
    canonical_url: str
    platform: Platform
    source_type: SourceType
    source_id: str | None


def extract_urls(text: str) -> list[str]:
    """Extract HTTP(S) URLs from a URL or platform share text."""
    return [match.group(0).rstrip(TRAILING_PUNCTUATION) for match in URL_PATTERN.finditer(text)]


def _clean_host(host: str | None) -> str:
    if not host:
        raise URLNormalizationError(ErrorCode.INVALID_URL, "URL 缺少域名")
    return host.rstrip(".").lower()


def _validate_common(parts) -> str:
    if parts.scheme.lower() not in {"http", "https"}:
        raise URLNormalizationError(
            ErrorCode.INVALID_URL, "只接受 HTTP/HTTPS URL"
        )
    if parts.username or parts.password:
        raise URLNormalizationError(
            ErrorCode.INVALID_URL, "URL 不得包含用户名或密码"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise URLNormalizationError(ErrorCode.INVALID_URL, "URL 端口无效") from exc
    default_port = 80 if parts.scheme.lower() == "http" else 443
    if port is not None and port != default_port:
        raise URLNormalizationError(
            ErrorCode.INVALID_URL, "平台 URL 的 scheme 与端口不匹配"
        )
    return _clean_host(parts.hostname)


def _clean_path(path: str) -> str:
    path = re.sub(r"/{2,}", "/", path or "/")
    return path.rstrip("/") or "/"


def _canonical_decimal_id(
    value: str,
    *,
    platform_name: str,
    minimum_digits: int = 1,
) -> str:
    canonical = value.lstrip("0")
    if not canonical or len(canonical) < minimum_digits:
        raise URLNormalizationError(
            ErrorCode.INVALID_URL,
            f"{platform_name} 标识必须是正十进制整数",
        )
    return canonical


def normalize_url(url: str) -> NormalizedURL:
    submitted = url.strip()
    try:
        parts = urlsplit(submitted)
    except ValueError as exc:
        raise URLNormalizationError(ErrorCode.INVALID_URL, "URL 格式无效") from exc
    host = _validate_common(parts)
    path = _clean_path(parts.path)

    if host in {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        match = re.fullmatch(
            r"/([^/]+)/status/([0-9]{1,32})(?:/[^/]*)?", path
        )
        if not match:
            raise URLNormalizationError(
                ErrorCode.UNSUPPORTED_LINK_TYPE,
                "MVP 仅接受 X 单条帖子 status 链接",
            )
        source_id = _canonical_decimal_id(
            match.group(2), platform_name="X"
        )
        # X usernames can change and aliases can point to the same post. The
        # stable /i/status form keeps canonical identity tied to the post ID.
        canonical = f"https://x.com/i/status/{source_id}"
        return NormalizedURL(
            submitted, canonical, Platform.X, SourceType.X_POST, source_id
        )

    if host == "t.co":
        if path == "/":
            raise URLNormalizationError(ErrorCode.INVALID_URL, "X 短链缺少标识")
        canonical = urlunsplit(("https", "t.co", path, "", ""))
        return NormalizedURL(
            submitted, canonical, Platform.X, SourceType.SHORT_LINK, None
        )

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
            if not video_id:
                raise URLNormalizationError(
                    ErrorCode.INVALID_URL, "YouTube watch URL 缺少 v 参数"
                )
            canonical = f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"
            return NormalizedURL(
                submitted,
                canonical,
                Platform.YOUTUBE,
                SourceType.YOUTUBE_VIDEO,
                video_id,
            )
        short_match = re.fullmatch(r"/shorts/([^/]+)", path)
        if short_match:
            video_id = short_match.group(1)
            canonical = f"https://www.youtube.com/shorts/{video_id}"
            return NormalizedURL(
                submitted,
                canonical,
                Platform.YOUTUBE,
                SourceType.YOUTUBE_SHORT,
                video_id,
            )
        raise URLNormalizationError(
            ErrorCode.UNSUPPORTED_LINK_TYPE,
            "MVP 仅接受 YouTube 单视频或 Shorts 链接",
        )

    if host == "youtu.be":
        video_id = path.removeprefix("/").split("/", 1)[0]
        if not video_id:
            raise URLNormalizationError(
                ErrorCode.INVALID_URL, "YouTube 短链缺少视频 ID"
            )
        canonical = f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"
        return NormalizedURL(
            submitted,
            canonical,
            Platform.YOUTUBE,
            SourceType.YOUTUBE_VIDEO,
            video_id,
        )

    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        match = re.fullmatch(
            r"/video/((?:BV[0-9A-Za-z]+)|(?:av[0-9]{1,32}))",
            path,
            re.IGNORECASE,
        )
        if not match:
            raise URLNormalizationError(
                ErrorCode.UNSUPPORTED_LINK_TYPE,
                "MVP 仅接受 Bilibili 普通投稿 BV/av 链接",
            )
        page_values = parse_qs(parts.query, keep_blank_values=True).get("p", [])
        if page_values and (
            len(page_values) != 1
            or not page_values[0].isdigit()
            or int(page_values[0]) != 1
        ):
            raise URLNormalizationError(
                ErrorCode.UNSUPPORTED_LINK_TYPE,
                "当前仅接受 Bilibili 投稿默认分 P；暂不支持选择其他分 P",
            )
        source_id = match.group(1)
        if source_id.lower().startswith("bv"):
            source_id = "BV" + source_id[2:]
        else:
            source_id = "av" + _canonical_decimal_id(
                source_id[2:], platform_name="Bilibili av"
            )
        canonical = f"https://www.bilibili.com/video/{source_id}"
        return NormalizedURL(
            submitted,
            canonical,
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
            source_id,
        )

    if host == "b23.tv":
        if path == "/":
            raise URLNormalizationError(ErrorCode.INVALID_URL, "Bilibili 短链缺少标识")
        canonical = urlunsplit(("https", "b23.tv", path, "", ""))
        return NormalizedURL(
            submitted,
            canonical,
            Platform.BILIBILI,
            SourceType.SHORT_LINK,
            None,
        )

    if host in {"douyin.com", "www.douyin.com"}:
        match = re.fullmatch(r"/video/([0-9]{1,32})", path)
        if not match:
            raise URLNormalizationError(
                ErrorCode.UNSUPPORTED_LINK_TYPE,
                "MVP 仅接受抖音单作品 video 链接",
            )
        source_id = _canonical_decimal_id(
            match.group(1), platform_name="Douyin"
        )
        canonical = f"https://www.douyin.com/video/{source_id}"
        return NormalizedURL(
            submitted,
            canonical,
            Platform.DOUYIN,
            SourceType.DOUYIN_VIDEO,
            source_id,
        )

    if host == "v.douyin.com":
        if path == "/":
            raise URLNormalizationError(ErrorCode.INVALID_URL, "抖音短链缺少标识")
        canonical = urlunsplit(("https", "v.douyin.com", path, "", ""))
        return NormalizedURL(
            submitted,
            canonical,
            Platform.DOUYIN,
            SourceType.SHORT_LINK,
            None,
        )

    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        if path == "/":
            raise URLNormalizationError(ErrorCode.INVALID_URL, "TikTok 短链缺少标识")
        canonical = urlunsplit(("https", host, path, "", ""))
        return NormalizedURL(
            submitted,
            canonical,
            Platform.TIKTOK,
            SourceType.SHORT_LINK,
            None,
        )

    if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        match = re.fullmatch(
            r"/@([A-Za-z0-9._]{1,32})/video/([0-9]{5,32})",
            path,
        )
        if not match:
            raise URLNormalizationError(
                ErrorCode.UNSUPPORTED_LINK_TYPE,
                "当前仅接受 TikTok 单视频 /@handle/video/{id} 链接",
            )
        handle = match.group(1).lower()
        source_id = _canonical_decimal_id(
            match.group(2),
            platform_name="TikTok",
            minimum_digits=5,
        )
        canonical = f"https://www.tiktok.com/@{handle}/video/{source_id}"
        return NormalizedURL(
            submitted,
            canonical,
            Platform.TIKTOK,
            SourceType.TIKTOK_VIDEO,
            source_id,
        )

    if host in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        match = re.fullmatch(r"/(?:reel|reels)/([A-Za-z0-9_-]{5,64})", path)
        if not match:
            raise URLNormalizationError(
                ErrorCode.UNSUPPORTED_LINK_TYPE,
                "当前仅接受 Instagram 单条 Reel 链接",
            )
        source_id = match.group(1)
        canonical = f"https://www.instagram.com/reel/{source_id}"
        return NormalizedURL(
            submitted,
            canonical,
            Platform.INSTAGRAM,
            SourceType.INSTAGRAM_REEL,
            source_id,
        )

    raise URLNormalizationError(
        ErrorCode.UNSUPPORTED_PLATFORM,
        f"不支持的平台域名：{host}",
    )

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol


PLATFORMS = {"bilibili": "Bilibili", "douyin": "抖音", "tencent": "视频号"}


def normalize_tencent_short_title(value: str) -> str:
    """Return the deterministic 7-15 character title accepted by 视频号."""

    allowed = "《》“”:+?%°"
    filtered = "".join(
        character if character.isalnum() or character in allowed
        else " " if character == "," else ""
        for character in value
    ).strip()[:15]
    if len(filtered) < 7:
        filler = "，精彩内容分享"
        filtered = (filtered + filler)[:7] if filtered else "精彩视频内容分享"
    return filtered


def is_tencent_short_title_output(value: object) -> bool:
    """Whether a value can be produced by the pinned upstream normalizer."""

    if not isinstance(value, str) or not 7 <= len(value) <= 15 or value != value.strip():
        return False
    allowed = "《》“”:+?%°"
    ordinary = lambda character: character.isalnum() or character in allowed or character == " "
    if "，" not in value:
        return all(ordinary(character) for character in value)
    if len(value) != 7 or value.count("，") != 1:
        return False
    comma = value.index("，")
    prefix = value[:comma]
    return (
        1 <= len(prefix) <= 6
        and prefix == prefix.strip()
        and all(ordinary(character) for character in prefix)
        and value[comma:] == "，精彩内容分享"[: 7 - comma]
    )


class UploadError(ValueError):
    """A fixed public error code; never include underlying paths or output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UploadRequest:
    job_id: str
    account_id: str
    platform: str
    file_path: Path
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: int | None = None
    mode: str = "publish"
    copyright: int = 1
    source_credit: str = ""
    cover_landscape_path: Path | None = None
    cover_portrait_path: Path | None = None
    publish_at_unix: int | None = None
    publish_timezone_offset_minutes: int | None = None
    dynamic: str = ""
    no_reprint: bool = False
    close_comments: bool = False
    close_danmu: bool = False
    declaration: str | None = None
    short_title: str | None = None
    content_label: str | None = None


@dataclass(frozen=True)
class BackendResult:
    # submitted is an upstream submission acknowledgement, not moderation approval.
    status: str
    code: str


class UploadBackend(Protocol):
    def inspect(self) -> dict: ...

    def login(self, platform: str, account_id: str, stop: Event) -> BackendResult: ...

    def check(self, platform: str, account_id: str, stop: Event) -> BackendResult: ...

    def upload(self, request: UploadRequest, stop: Event) -> BackendResult: ...

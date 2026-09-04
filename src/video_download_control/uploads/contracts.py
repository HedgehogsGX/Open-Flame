from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol


PLATFORMS = {"bilibili": "Bilibili", "douyin": "抖音", "tencent": "视频号"}


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

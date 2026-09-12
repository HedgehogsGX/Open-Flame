from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol


PLATFORMS = {"bilibili": "Bilibili", "douyin": "抖音", "tencent": "视频号"}
UPLOAD_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
UPLOAD_ATTEMPT_STATES = frozenset(
    {"reserved", "dispatch_may_have_started", "responded", "unknown", "reconciled"}
)
UPLOAD_RESULT_STATUSES = frozenset(
    {"submitted", "draft_saved", "failed", "unknown", "canceled"}
)
UPLOAD_EVIDENCE_KINDS = frozenset(
    {
        "process_exit_zero",
        "uploader_returned_after_final_action",
        "https_errcode_zero",
        "post_list_navigation",
    }
)
UPLOAD_RECONCILIATIONS = frozenset(
    {"not_accepted", "submission_acknowledged", "draft_saved"}
)
UPLOAD_SUCCESS_EVIDENCE = {
    ("bilibili", "publish", "submitted"): "process_exit_zero",
    ("douyin", "publish", "submitted"): "uploader_returned_after_final_action",
    ("tencent", "publish", "submitted"): "https_errcode_zero",
    ("tencent", "draft", "draft_saved"): "post_list_navigation",
}
CURRENT_UPLOAD_ADAPTER_IDENTITIES = {
    "bilibili": ("biliup", "v1.2.4"),
    "douyin": (
        "social-auto-upload",
        "0012d2c355f88f683cc38dde2a2db209e14091bc",
    ),
    "tencent": (
        "social-auto-upload",
        "0012d2c355f88f683cc38dde2a2db209e14091bc",
    ),
}
# Append-only receipt compatibility ledger. When a current pin changes, retain
# every previously released identity here and add the new tuple explicitly.
UPLOAD_ADAPTER_IDENTITY_HISTORY = {
    "bilibili": frozenset({("biliup", "v1.2.4")}),
    "douyin": frozenset(
        {
            (
                "social-auto-upload",
                "0012d2c355f88f683cc38dde2a2db209e14091bc",
            )
        }
    ),
    "tencent": frozenset(
        {
            (
                "social-auto-upload",
                "0012d2c355f88f683cc38dde2a2db209e14091bc",
            )
        }
    ),
}
if set(CURRENT_UPLOAD_ADAPTER_IDENTITIES) != set(UPLOAD_ADAPTER_IDENTITY_HISTORY) or any(
    identity not in UPLOAD_ADAPTER_IDENTITY_HISTORY[platform]
    for platform, identity in CURRENT_UPLOAD_ADAPTER_IDENTITIES.items()
):
    raise RuntimeError("current upload adapter identity is missing from history")


def upload_evidence_is_valid(
    platform: object,
    mode: object,
    status: object,
    evidence_kind: object,
) -> bool:
    """Validate the fixed acknowledgement boundary for one upload result."""

    expected = UPLOAD_SUCCESS_EVIDENCE.get((platform, mode, status))
    if evidence_kind is not None:
        return evidence_kind == expected
    return status not in {"submitted", "draft_saved"}


def upload_adapter_identity_matches(
    platform: object, adapter_name: object, adapter_revision: object
) -> bool:
    """Bind a persistent receipt to the exact pinned owned adapter."""

    return (adapter_name, adapter_revision) in UPLOAD_ADAPTER_IDENTITY_HISTORY.get(
        platform, ()
    )


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


def legacy_migrated_platform_options(platform: str, title: str) -> dict:
    """Return the explicit options assigned to a v1 request during migration."""

    if platform == "douyin":
        return {"declaration": "内容由AI生成"}
    if platform == "tencent":
        return {
            "content_label": "含AI生成内容",
            "short_title": normalize_tencent_short_title(title),
        }
    return {}


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
    source_sha256: str
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
    # A fixed local description of the acknowledgement boundary. Synthetic
    # backends may omit it; the owned runtime validates every real value.
    evidence_kind: str | None = None


class UploadBackend(Protocol):
    def inspect(self) -> dict: ...

    def receipt_identity(self, platform: str) -> dict[str, str]: ...

    def login(self, platform: str, account_id: str, stop: Event) -> BackendResult: ...

    def check(self, platform: str, account_id: str, stop: Event) -> BackendResult: ...

    def upload(self, request: UploadRequest, stop: Event) -> BackendResult: ...

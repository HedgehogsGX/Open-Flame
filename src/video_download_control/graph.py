"""Pure graph-v2 identities and discovery snapshots for X attachments.

This module deliberately contains no persistence, downloader, or public API
serialization code.  Identity- and target-key fields are internal inputs for
the repository/Worker boundary and are omitted from normal dataclass ``repr``
output so incidental logging does not disclose them.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

MIN_DISCOVERY_ITEMS = 1
MAX_DISCOVERY_ITEMS = 50
MAX_STABLE_KEY_CHARS = 256
MAX_SELECTOR_CHARS = 256
MAX_EXPECTED_MEDIA_KEY_CHARS = 256
MAX_MEDIA_KIND_CHARS = 32
X_ATTACHMENT_SOURCE_TYPE = "x_attachment"
X_ATTACHMENT_SOURCE_ID_PREFIX = "xatt:v1:"
X_ATTACHMENT_RELATION_TYPE = "attachment"

_MAX_X_POST_SOURCE_ID_CHARS = 64
_MEDIA_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")


class GraphValidationError(ValueError):
    """A bounded graph input violated the internal discovery contract."""


@dataclass(frozen=True, slots=True)
class XPostIdentity:
    """Stable product identity for one normalized parent X post."""

    source_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, str)
            or not 1 <= len(self.source_id) <= _MAX_X_POST_SOURCE_ID_CHARS
            or not self.source_id.isascii()
            or not self.source_id.isdecimal()
        ):
            raise GraphValidationError("X parent source identity is invalid")


@dataclass(frozen=True, slots=True)
class XAttachmentProbeItem:
    """Validated probe inputs for child identity, membership, and download."""

    stable_key: str = field(repr=False)
    selector_key: str = field(repr=False)
    expected_media_key: str = field(repr=False)
    media_kind: str

    def __post_init__(self) -> None:
        validate_stable_attachment_key(self.stable_key)
        validate_stable_selector(self.selector_key)
        validate_expected_media_key(self.expected_media_key)
        _validate_media_kind(self.media_kind)


@dataclass(frozen=True, slots=True)
class XAttachmentMember:
    """One ordered child SourceItem plus its exact internal download target."""

    ordinal: int
    source_id: str
    canonical_url: str = field(repr=False)
    stable_key: str = field(repr=False)
    selector_key: str = field(repr=False)
    expected_media_key: str = field(repr=False)
    media_kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool):
            raise GraphValidationError("attachment ordinal is invalid")
        if not 0 <= self.ordinal < MAX_DISCOVERY_ITEMS:
            raise GraphValidationError("attachment ordinal is outside the limit")
        if not _is_x_attachment_source_id(self.source_id):
            raise GraphValidationError("attachment source identity is invalid")
        validate_stable_attachment_key(self.stable_key)
        validate_stable_selector(self.selector_key)
        validate_expected_media_key(self.expected_media_key)
        _validate_media_kind(self.media_kind)
        if not isinstance(self.canonical_url, str):
            raise GraphValidationError("attachment canonical locator is invalid")
        parent_match = re.fullmatch(
            r"https://x\.com/i/status/([0-9]{1,64})#vdc-media=.*",
            self.canonical_url,
        )
        if parent_match is None:
            raise GraphValidationError("attachment canonical locator is invalid")
        parent = XPostIdentity(parent_match.group(1))
        if self.canonical_url != x_attachment_canonical_url(
            parent,
            f"https://x.com/i/status/{parent.source_id}",
            self.stable_key,
        ):
            raise GraphValidationError("attachment canonical locator is invalid")
        if self.source_id != x_attachment_source_id(parent, self.stable_key):
            raise GraphValidationError("attachment source identity is inconsistent")


@dataclass(frozen=True, slots=True)
class XAttachmentDiscovery:
    """An immutable ordered discovery result suitable for atomic fan-out."""

    members: tuple[XAttachmentMember, ...]
    snapshot_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.members, tuple) or any(
            not isinstance(member, XAttachmentMember) for member in self.members
        ):
            raise GraphValidationError("attachment discovery members are invalid")
        if not MIN_DISCOVERY_ITEMS <= len(self.members) <= MAX_DISCOVERY_ITEMS:
            raise GraphValidationError("attachment discovery count is outside the limit")
        if tuple(member.ordinal for member in self.members) != tuple(
            range(len(self.members))
        ):
            raise GraphValidationError("attachment discovery order is invalid")
        _validate_unique_member_keys(self.members)
        if self.snapshot_hash != x_attachment_snapshot_hash(self.members):
            raise GraphValidationError("attachment discovery snapshot is inconsistent")

    @property
    def expected_item_count(self) -> int:
        return len(self.members)


def validate_stable_attachment_key(stable_key: object) -> str:
    """Return an exact bounded identity key without echoing invalid input."""

    return _validate_plain_key(
        stable_key,
        max_chars=MAX_STABLE_KEY_CHARS,
        label="stable attachment key",
    )


def validate_stable_selector(selector_key: object) -> str:
    """Return an exact bounded fetch selector without echoing invalid input."""

    return _validate_plain_key(
        selector_key,
        max_chars=MAX_SELECTOR_CHARS,
        label="attachment selector",
    )


def validate_expected_media_key(expected_media_key: object) -> str:
    """Return the exact bounded post-download mapping key."""

    return _validate_plain_key(
        expected_media_key,
        max_chars=MAX_EXPECTED_MEDIA_KEY_CHARS,
        label="expected media key",
    )


def x_attachment_source_id(
    parent: XPostIdentity,
    stable_key: object,
) -> str:
    """Derive child identity from framed parent identity + stable key."""

    key = validate_stable_attachment_key(stable_key)
    parent_identity = json.dumps(
        ["x", "x_post", parent.source_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(
        parent_identity + b"\0" + key.encode("utf-8")
    ).hexdigest()
    return f"{X_ATTACHMENT_SOURCE_ID_PREFIX}{digest}"


def x_attachment_canonical_url(
    parent: XPostIdentity,
    parent_canonical_url: object,
    stable_key: object,
) -> str:
    """Create an internal, non-fetchable attachment locator under the parent URL."""

    key = validate_stable_attachment_key(stable_key)
    expected_parent_url = f"https://x.com/i/status/{parent.source_id}"
    if parent_canonical_url != expected_parent_url:
        raise GraphValidationError("X parent canonical URL is invalid")
    return f"{expected_parent_url}#vdc-media={quote(key, safe='')}"


def x_attachment_snapshot_hash(
    members: Sequence[XAttachmentMember],
) -> str:
    """Hash ordered membership using only ADR-approved stable fields."""

    canonical = x_attachment_members_json(members).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def x_attachment_members_json(
    members: Sequence[XAttachmentMember],
) -> str:
    """Serialize the exact ordered discovery-membership persistence payload."""

    if not MIN_DISCOVERY_ITEMS <= len(members) <= MAX_DISCOVERY_ITEMS:
        raise GraphValidationError("attachment discovery count is outside the limit")
    if tuple(member.ordinal for member in members) != tuple(range(len(members))):
        raise GraphValidationError("attachment discovery order is invalid")
    _validate_unique_member_keys(members)
    payload = [
        {
            "child_source_id": member.source_id,
            "media_kind": member.media_kind,
            "selector_key": member.selector_key,
        }
        for member in members
    ]
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_x_attachment_discovery(
    *,
    parent: XPostIdentity,
    parent_canonical_url: object,
    probe_items: Iterable[XAttachmentProbeItem],
) -> XAttachmentDiscovery:
    """Validate and derive at most 50 ordered X attachment graph members."""

    members: list[XAttachmentMember] = []
    stable_keys: set[str] = set()
    selectors: set[str] = set()
    for ordinal, probe_item in enumerate(probe_items):
        if ordinal >= MAX_DISCOVERY_ITEMS:
            raise GraphValidationError("attachment discovery count is outside the limit")
        if not isinstance(probe_item, XAttachmentProbeItem):
            raise GraphValidationError("attachment probe item is invalid")
        stable_key = validate_stable_attachment_key(probe_item.stable_key)
        selector = validate_stable_selector(probe_item.selector_key)
        expected_media_key = validate_expected_media_key(
            probe_item.expected_media_key
        )
        if stable_key in stable_keys:
            raise GraphValidationError("stable attachment keys must be unique")
        if selector in selectors:
            raise GraphValidationError("attachment selectors must be unique")
        stable_keys.add(stable_key)
        selectors.add(selector)
        members.append(
            XAttachmentMember(
                ordinal=ordinal,
                source_id=x_attachment_source_id(parent, stable_key),
                canonical_url=x_attachment_canonical_url(
                    parent,
                    parent_canonical_url,
                    stable_key,
                ),
                stable_key=stable_key,
                selector_key=selector,
                expected_media_key=expected_media_key,
                media_kind=probe_item.media_kind,
            )
        )
    if not members:
        raise GraphValidationError("attachment discovery count is outside the limit")
    frozen_members = tuple(members)
    return XAttachmentDiscovery(
        members=frozen_members,
        snapshot_hash=x_attachment_snapshot_hash(frozen_members),
    )


def _validate_media_kind(media_kind: object) -> str:
    if (
        not isinstance(media_kind, str)
        or not 1 <= len(media_kind) <= MAX_MEDIA_KIND_CHARS
        or _MEDIA_KIND.fullmatch(media_kind) is None
    ):
        raise GraphValidationError("attachment media kind is invalid")
    return media_kind


def _validate_plain_key(value: object, *, max_chars: int, label: str) -> str:
    if not isinstance(value, str):
        raise GraphValidationError(f"{label} must be text")
    if not 1 <= len(value) <= max_chars:
        raise GraphValidationError(f"{label} length is invalid")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise GraphValidationError(f"{label} contains unsafe characters")
    return value


def _validate_unique_member_keys(members: Sequence[XAttachmentMember]) -> None:
    stable_keys = tuple(member.stable_key for member in members)
    selectors = tuple(member.selector_key for member in members)
    if len(set(stable_keys)) != len(stable_keys):
        raise GraphValidationError("stable attachment keys must be unique")
    if len(set(selectors)) != len(selectors):
        raise GraphValidationError("attachment selectors must be unique")


def _is_x_attachment_source_id(source_id: object) -> bool:
    if not isinstance(source_id, str) or not source_id.startswith(
        X_ATTACHMENT_SOURCE_ID_PREFIX
    ):
        return False
    digest = source_id.removeprefix(X_ATTACHMENT_SOURCE_ID_PREFIX)
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


__all__ = [
    "MAX_DISCOVERY_ITEMS",
    "MAX_EXPECTED_MEDIA_KEY_CHARS",
    "MAX_MEDIA_KIND_CHARS",
    "MAX_SELECTOR_CHARS",
    "MAX_STABLE_KEY_CHARS",
    "MIN_DISCOVERY_ITEMS",
    "X_ATTACHMENT_RELATION_TYPE",
    "X_ATTACHMENT_SOURCE_ID_PREFIX",
    "X_ATTACHMENT_SOURCE_TYPE",
    "GraphValidationError",
    "XAttachmentDiscovery",
    "XAttachmentMember",
    "XAttachmentProbeItem",
    "XPostIdentity",
    "build_x_attachment_discovery",
    "validate_expected_media_key",
    "validate_stable_attachment_key",
    "validate_stable_selector",
    "x_attachment_canonical_url",
    "x_attachment_members_json",
    "x_attachment_snapshot_hash",
    "x_attachment_source_id",
]

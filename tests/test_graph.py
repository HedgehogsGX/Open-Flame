from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from video_download_control.graph import (
    MAX_DISCOVERY_ITEMS,
    MAX_EXPECTED_MEDIA_KEY_CHARS,
    MAX_SELECTOR_CHARS,
    MAX_STABLE_KEY_CHARS,
    GraphValidationError,
    XAttachmentProbeItem,
    XPostIdentity,
    build_x_attachment_discovery,
    validate_expected_media_key,
    validate_stable_attachment_key,
    validate_stable_selector,
    x_attachment_members_json,
    x_attachment_snapshot_hash,
    x_attachment_source_id,
)

PARENT = XPostIdentity("1234567890123456789")
PARENT_URL = "https://x.com/i/status/1234567890123456789"


def item(
    stable_key: str,
    media_kind: str = "video",
    *,
    selector_key: str | None = None,
    expected_media_key: str | None = None,
) -> XAttachmentProbeItem:
    return XAttachmentProbeItem(
        stable_key=stable_key,
        selector_key=stable_key if selector_key is None else selector_key,
        expected_media_key=(
            stable_key if expected_media_key is None else expected_media_key
        ),
        media_kind=media_kind,
    )


def test_build_discovery_preserves_order_and_derives_internal_targets() -> None:
    discovery = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable:one",
                selector_key="fetch-one",
                expected_media_key="media-one",
            ),
            item(
                "stable/two",
                "image",
                selector_key="fetch-two",
                expected_media_key="media-two",
            ),
        ),
    )

    assert discovery.expected_item_count == 2
    assert [member.ordinal for member in discovery.members] == [0, 1]
    assert [member.stable_key for member in discovery.members] == [
        "stable:one",
        "stable/two",
    ]
    assert [member.selector_key for member in discovery.members] == [
        "fetch-one",
        "fetch-two",
    ]
    assert [member.expected_media_key for member in discovery.members] == [
        "media-one",
        "media-two",
    ]
    assert discovery.members[0].canonical_url == (
        f"{PARENT_URL}#vdc-media=stable%3Aone"
    )
    assert discovery.members[1].canonical_url == (
        f"{PARENT_URL}#vdc-media=stable%2Ftwo"
    )
    assert discovery.members[0].source_id == x_attachment_source_id(
        PARENT,
        "stable:one",
    )
    assert all(member.source_id.startswith("xatt:v1:") for member in discovery.members)
    assert len(discovery.snapshot_hash) == 64


def test_source_id_uses_canonical_parent_identity_nul_and_stable_key() -> None:
    parent_frame = json.dumps(
        ["x", "x_post", PARENT.source_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    expected = "xatt:v1:" + hashlib.sha256(
        parent_frame + b"\0" + b"media-1"
    ).hexdigest()

    assert expected == (
        "xatt:v1:03154b6d3ee0866c04677f39d4dc3c2d"
        "7aeef726badafc3dccd893a750d35f82"
    )
    assert x_attachment_source_id(PARENT, "media-1") == expected
    assert x_attachment_source_id(PARENT, "media-1") != x_attachment_source_id(
        XPostIdentity("2234567890123456789"),
        "media-1",
    )
    assert x_attachment_source_id(PARENT, "media-1") != x_attachment_source_id(
        PARENT,
        "media-2",
    )


@pytest.mark.parametrize(
    ("validator", "limit"),
    [
        (validate_stable_attachment_key, MAX_STABLE_KEY_CHARS),
        (validate_stable_selector, MAX_SELECTOR_CHARS),
        (validate_expected_media_key, MAX_EXPECTED_MEDIA_KEY_CHARS),
    ],
)
def test_attachment_keys_accept_exact_bounded_plain_scalars(
    validator,
    limit: int,
) -> None:
    for value in ("x", "x" * limit, "媒体-一"):
        assert validator(value) == value


@pytest.mark.parametrize(
    ("validator", "limit"),
    [
        (validate_stable_attachment_key, MAX_STABLE_KEY_CHARS),
        (validate_stable_selector, MAX_SELECTOR_CHARS),
        (validate_expected_media_key, MAX_EXPECTED_MEDIA_KEY_CHARS),
    ],
)
def test_attachment_keys_reject_empty_unbounded_or_control_text(
    validator,
    limit: int,
) -> None:
    invalid_values = (
        "",
        "x" * (limit + 1),
        "unsafe\0key",
        "unsafe\nkey",
        "unsafe\x7fkey",
        "unsafe\u200bkey",
    )
    for value in invalid_values:
        with pytest.raises(GraphValidationError) as caught:
            validator(value)

        if value:
            assert value not in str(caught.value)


@pytest.mark.parametrize(
    "validator",
    [
        validate_stable_attachment_key,
        validate_stable_selector,
        validate_expected_media_key,
    ],
)
def test_attachment_keys_reject_non_text_without_echoing_it(validator) -> None:
    with pytest.raises(GraphValidationError, match="must be text") as caught:
        validator(123)

    assert "123" not in str(caught.value)


@pytest.mark.parametrize("source_id", ["", "not-a-post", "1" * 65, "１２３"])
def test_parent_identity_requires_bounded_ascii_decimal_source_id(
    source_id: str,
) -> None:
    with pytest.raises(GraphValidationError, match="parent source identity"):
        XPostIdentity(source_id)


@pytest.mark.parametrize(
    "parent_url",
    [
        "http://x.com/i/status/1234567890123456789",
        "https://twitter.com/i/status/1234567890123456789",
        "https://x.com/i/status/999",
        f"{PARENT_URL}?tracking=1",
        f"{PARENT_URL}#existing",
    ],
)
def test_child_locator_requires_the_exact_normalized_parent_url(
    parent_url: str,
) -> None:
    with pytest.raises(GraphValidationError, match="canonical URL"):
        build_x_attachment_discovery(
            parent=PARENT,
            parent_canonical_url=parent_url,
            probe_items=(item("media-1"),),
        )


def test_discovery_enforces_one_to_fifty_items_without_unbounded_consumption() -> None:
    with pytest.raises(GraphValidationError, match="count"):
        build_x_attachment_discovery(
            parent=PARENT,
            parent_canonical_url=PARENT_URL,
            probe_items=(),
        )

    consumed = 0

    def too_many_items():
        nonlocal consumed
        for index in range(MAX_DISCOVERY_ITEMS + 20):
            consumed += 1
            yield item(f"media-{index}")

    with pytest.raises(GraphValidationError, match="count"):
        build_x_attachment_discovery(
            parent=PARENT,
            parent_canonical_url=PARENT_URL,
            probe_items=too_many_items(),
        )

    assert consumed == MAX_DISCOVERY_ITEMS + 1


def test_discovery_accepts_exactly_fifty_items() -> None:
    discovery = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(item(f"media-{index}") for index in range(MAX_DISCOVERY_ITEMS)),
    )

    assert discovery.expected_item_count == MAX_DISCOVERY_ITEMS
    assert discovery.members[-1].ordinal == MAX_DISCOVERY_ITEMS - 1


def test_duplicate_stable_key_fails_closed_even_when_targets_differ() -> None:
    with pytest.raises(GraphValidationError, match="stable attachment keys"):
        build_x_attachment_discovery(
            parent=PARENT,
            parent_canonical_url=PARENT_URL,
            probe_items=(
                item(
                    "same",
                    selector_key="selector-one",
                    expected_media_key="media-one",
                ),
                item(
                    "same",
                    "image",
                    selector_key="selector-two",
                    expected_media_key="media-two",
                ),
            ),
        )


def test_duplicate_selector_fails_closed_even_when_identities_differ() -> None:
    with pytest.raises(GraphValidationError, match="attachment selectors"):
        build_x_attachment_discovery(
            parent=PARENT,
            parent_canonical_url=PARENT_URL,
            probe_items=(
                item(
                    "stable-one",
                    selector_key="same",
                    expected_media_key="media-one",
                ),
                item(
                    "stable-two",
                    "image",
                    selector_key="same",
                    expected_media_key="media-two",
                ),
            ),
        )


def test_expected_media_key_is_scoped_to_each_child_job_not_the_discovery() -> None:
    discovery = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable-one",
                selector_key="selector-one",
                expected_media_key="shared-output-id",
            ),
            item(
                "stable-two",
                selector_key="selector-two",
                expected_media_key="shared-output-id",
            ),
        ),
    )

    assert [member.expected_media_key for member in discovery.members] == [
        "shared-output-id",
        "shared-output-id",
    ]


def test_snapshot_is_ordered_and_contains_only_approved_membership_fields() -> None:
    discovery = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable-first",
                "video",
                selector_key="fetch-first",
                expected_media_key="output-first",
            ),
            item(
                "stable-second",
                "image",
                selector_key="fetch-second",
                expected_media_key="output-second",
            ),
        ),
    )
    payload = [
        {
            "child_source_id": member.source_id,
            "media_kind": member.media_kind,
            "selector_key": member.selector_key,
        }
        for member in discovery.members
    ]
    expected_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_hash = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    reordered = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable-second",
                "image",
                selector_key="fetch-second",
                expected_media_key="output-second",
            ),
            item(
                "stable-first",
                "video",
                selector_key="fetch-first",
                expected_media_key="output-first",
            ),
        ),
    )
    changed_kind = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable-first",
                "audio",
                selector_key="fetch-first",
                expected_media_key="output-first",
            ),
            item(
                "stable-second",
                "image",
                selector_key="fetch-second",
                expected_media_key="output-second",
            ),
        ),
    )
    changed_selector = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable-first",
                "video",
                selector_key="fetch-first-v2",
                expected_media_key="output-first",
            ),
            item(
                "stable-second",
                "image",
                selector_key="fetch-second",
                expected_media_key="output-second",
            ),
        ),
    )
    changed_expected_media_key = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable-first",
                "video",
                selector_key="fetch-first",
                expected_media_key="different-output",
            ),
            item(
                "stable-second",
                "image",
                selector_key="fetch-second",
                expected_media_key="output-second",
            ),
        ),
    )

    assert expected_hash == (
        "7d66dcf11f40f4fe9fde40c1efb7b21c"
        "b5b1bc2bba182cdd795831a92b51140c"
    )
    assert x_attachment_members_json(discovery.members) == expected_json
    assert discovery.snapshot_hash == expected_hash
    assert discovery.snapshot_hash == x_attachment_snapshot_hash(discovery.members)
    assert discovery.snapshot_hash != reordered.snapshot_hash
    assert discovery.snapshot_hash != changed_kind.snapshot_hash
    assert discovery.snapshot_hash != changed_selector.snapshot_hash
    assert discovery.snapshot_hash == changed_expected_media_key.snapshot_hash
    assert discovery.members[0].source_id == changed_selector.members[0].source_id
    assert discovery.members[0].canonical_url == changed_selector.members[0].canonical_url
    assert (
        discovery.members[0].source_id
        == changed_expected_media_key.members[0].source_id
    )
    assert (
        discovery.members[0].canonical_url
        == changed_expected_media_key.members[0].canonical_url
    )
    assert discovery == build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(
            item(
                "stable-first",
                "video",
                selector_key="fetch-first",
                expected_media_key="output-first",
            ),
            item(
                "stable-second",
                "image",
                selector_key="fetch-second",
                expected_media_key="output-second",
            ),
        ),
    )


def test_internal_target_dtos_are_frozen_and_repr_safe() -> None:
    stable_key = "stable-key-do-not-log"
    selector = "internal-selector-do-not-log"
    expected_media_key = "expected-media-key-do-not-log"
    probe_item = item(
        stable_key,
        selector_key=selector,
        expected_media_key=expected_media_key,
    )
    discovery = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(probe_item,),
    )

    for private_value in (stable_key, selector, expected_media_key):
        assert private_value not in repr(probe_item)
        assert private_value not in repr(discovery.members[0])
        assert private_value not in repr(discovery)
    with pytest.raises(FrozenInstanceError):
        discovery.members[0].ordinal = 2  # type: ignore[misc]

    with pytest.raises(GraphValidationError, match="members are invalid"):
        replace(discovery, members=list(discovery.members))  # type: ignore[arg-type]


def test_member_rejects_a_locator_or_identity_from_another_parent() -> None:
    discovery = build_x_attachment_discovery(
        parent=PARENT,
        parent_canonical_url=PARENT_URL,
        probe_items=(item("media-1"),),
    )
    member = discovery.members[0]

    with pytest.raises(GraphValidationError, match="source identity"):
        replace(
            member,
            canonical_url="https://x.com/i/status/999#vdc-media=media-1",
        )
    with pytest.raises(GraphValidationError, match="canonical locator"):
        replace(member, canonical_url=f"{PARENT_URL}#vdc-media=wrong")
    with pytest.raises(GraphValidationError, match="source identity"):
        replace(member, source_id="xatt:v1:" + ("0" * 64))


@pytest.mark.parametrize("media_kind", ["", "Video", "video-kind", "x" * 33])
def test_media_kind_must_be_a_bounded_canonical_token(media_kind: str) -> None:
    with pytest.raises(GraphValidationError, match="media kind"):
        item("media-1", media_kind)

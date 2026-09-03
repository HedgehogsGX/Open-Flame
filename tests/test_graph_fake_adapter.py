from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from video_download_control.adapters import (
    AdapterContext,
    AdapterFailure,
    AdapterNetworkMode,
    DownloadAdapter,
    DownloadRequest,
    ProbeRequest,
    ScriptedGraphFakeAdapter,
)
from video_download_control.domain import ErrorCode, Platform, SourceType
from video_download_control.graph import (
    MAX_DISCOVERY_ITEMS,
    GraphValidationError,
    XAttachmentProbeItem,
    XPostIdentity,
    build_x_attachment_discovery,
)

PARENT_URL = "https://x.com/i/status/1234567890123456789"
PRIVATE_KEYS = (
    "stable:key-one",
    "selector-key-one",
    "expected-media-one",
    "stable/key-two",
    "selector-key-two",
    "expected-media-two",
)
GRAPH_ITEMS = (
    XAttachmentProbeItem(
        stable_key=PRIVATE_KEYS[0],
        selector_key=PRIVATE_KEYS[1],
        expected_media_key=PRIVATE_KEYS[2],
        media_kind="video",
    ),
    XAttachmentProbeItem(
        stable_key=PRIVATE_KEYS[3],
        selector_key=PRIVATE_KEYS[4],
        expected_media_key=PRIVATE_KEYS[5],
        media_kind="image",
    ),
)


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class AlreadyCancelled:
    def is_cancelled(self) -> bool:
        return True


def context(tmp_path: Path) -> AdapterContext:
    return AdapterContext(
        worker_id="worker-1",
        attempt_id="attempt-1",
        temporary_dir=tmp_path,
    )


def probe_request(**changes: object) -> ProbeRequest:
    request = ProbeRequest(
        job_id="parent-job",
        canonical_url=PARENT_URL,
        platform=Platform.X,
        source_type=SourceType.X_POST,
        max_items=MAX_DISCOVERY_ITEMS,
    )
    return replace(request, **changes)


def download_request(tmp_path: Path, **changes: object) -> DownloadRequest:
    request = DownloadRequest(
        job_id="child-job",
        source_item_id="child-source-item",
        canonical_url=PARENT_URL,
        platform=Platform.X,
        source_type=SourceType.X_ATTACHMENT,
        output_dir=tmp_path / "output",
        selector_key=PRIVATE_KEYS[1],
        expected_media_keys=(PRIVATE_KEYS[2],),
    )
    return replace(request, **changes)


def assert_private_keys_redacted(value: object) -> None:
    rendered = repr(value)
    for private_key in PRIVATE_KEYS:
        assert private_key not in rendered


def test_graph_fake_is_offline_exact_selector_adapter() -> None:
    adapter = ScriptedGraphFakeAdapter(GRAPH_ITEMS)

    assert isinstance(adapter, DownloadAdapter)
    assert adapter.network_mode is AdapterNetworkMode.OFFLINE
    assert adapter.supports_exact_selector is True
    assert adapter.probe_call_count == 0
    assert adapter.download_call_count == 0
    assert_private_keys_redacted(adapter)


def test_parent_probe_returns_canonical_graph_membership(tmp_path: Path) -> None:
    adapter = ScriptedGraphFakeAdapter(GRAPH_ITEMS)
    result = adapter.probe(probe_request(), context(tmp_path))
    expected = build_x_attachment_discovery(
        parent=XPostIdentity("1234567890123456789"),
        parent_canonical_url=PARENT_URL,
        probe_items=GRAPH_ITEMS,
    )

    assert adapter.probe_call_count == 1
    assert result.expected_item_count == 2
    assert result.discovery_snapshot_hash == expected.snapshot_hash
    assert result.sanitized_source == {
        "platform": "x",
        "source_type": "x_post",
        "canonical_url": PARENT_URL,
    }
    assert [item.canonical_url for item in result.items] == [
        member.canonical_url for member in expected.members
    ]
    assert [item.source_id for item in result.items] == [
        member.source_id for member in expected.members
    ]
    assert [item.stable_key for item in result.items] == [
        member.stable_key for member in expected.members
    ]
    assert [item.selector_key for item in result.items] == [
        member.selector_key for member in expected.members
    ]
    assert [item.media_key for item in result.items] == [
        member.expected_media_key for member in expected.members
    ]
    assert [item.media_kind for item in result.items] == ["video", "image"]
    assert_private_keys_redacted(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"platform": Platform.YOUTUBE},
        {"source_type": SourceType.X_ATTACHMENT},
        {"canonical_url": "http://x.com/i/status/1234567890123456789"},
        {"canonical_url": f"{PARENT_URL}?tracking=1"},
        {"canonical_url": "https://twitter.com/i/status/1234567890123456789"},
        {"max_items": 1},
        {"max_items": MAX_DISCOVERY_ITEMS + 1},
        {"max_items": True},
    ],
)
def test_parent_probe_rejects_noncanonical_or_unbounded_requests(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    adapter = ScriptedGraphFakeAdapter(GRAPH_ITEMS)

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(probe_request(**changes), context(tmp_path))

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.diagnostic == "graph fake probe request is invalid"
    assert adapter.probe_call_count == 1
    assert_private_keys_redacted(caught.value)


def test_download_selects_exactly_one_original(tmp_path: Path) -> None:
    payload = b"one graph attachment\n"
    adapter = ScriptedGraphFakeAdapter(GRAPH_ITEMS, payload=payload)
    progress = []

    result = adapter.download(
        download_request(tmp_path),
        context(tmp_path),
        progress.append,
        NeverCancelled(),
    )

    assert adapter.download_call_count == 1
    assert len(result.files) == 1
    assert result.thumbnails == ()
    assert result.captions == ()
    produced = result.files[0]
    assert produced.path == tmp_path / "output" / "attachment.fake"
    assert produced.path.read_bytes() == payload
    assert produced.media_key == PRIVATE_KEYS[2]
    assert produced.media_kind == "video"
    assert produced.role == "original"
    assert produced.ordinal == 0
    assert [(update.phase, update.fraction) for update in progress] == [
        ("downloading", 0.1),
        ("downloading", 1.0),
    ]
    assert_private_keys_redacted(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"selector_key": None},
        {"selector_key": "unknown-selector"},
        {"expected_media_keys": ()},
        {"expected_media_keys": (PRIVATE_KEYS[2], PRIVATE_KEYS[5])},
        {"expected_media_keys": ("unknown-media-key",)},
        {
            "selector_key": PRIVATE_KEYS[4],
            "expected_media_keys": (PRIVATE_KEYS[2],),
        },
    ],
)
def test_download_rejects_missing_unknown_mismatched_or_multiple_targets(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    adapter = ScriptedGraphFakeAdapter(GRAPH_ITEMS)

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(
            download_request(tmp_path, **changes),
            context(tmp_path),
            lambda update: None,
            NeverCancelled(),
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.diagnostic == "graph fake download request is invalid"
    assert adapter.download_call_count == 1
    assert not (tmp_path / "output").exists()
    assert_private_keys_redacted(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"platform": Platform.YOUTUBE},
        {"source_type": SourceType.X_POST},
        {"canonical_url": f"{PARENT_URL}#vdc-media=not-a-fetch-url"},
    ],
)
def test_download_requires_parent_fetch_url_and_attachment_logical_type(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    adapter = ScriptedGraphFakeAdapter(GRAPH_ITEMS)

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(
            download_request(tmp_path, **changes),
            context(tmp_path),
            lambda update: None,
            NeverCancelled(),
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.diagnostic == "graph fake download request is invalid"
    assert adapter.download_call_count == 1


def test_cancelled_download_produces_no_original(tmp_path: Path) -> None:
    adapter = ScriptedGraphFakeAdapter(GRAPH_ITEMS)

    result = adapter.download(
        download_request(tmp_path),
        context(tmp_path),
        lambda update: None,
        AlreadyCancelled(),
    )

    assert result.files == ()
    assert adapter.download_call_count == 1
    assert not (tmp_path / "output").exists()


def test_constructor_uses_graph_validation_without_leaking_keys() -> None:
    duplicate_selector = (
        GRAPH_ITEMS[0],
        XAttachmentProbeItem(
            stable_key="another-private-stable-key",
            selector_key=PRIVATE_KEYS[1],
            expected_media_key="another-private-media-key",
            media_kind="image",
        ),
    )

    with pytest.raises(GraphValidationError) as caught:
        ScriptedGraphFakeAdapter(duplicate_selector)

    assert "attachment selectors must be unique" == str(caught.value)
    assert_private_keys_redacted(caught.value)


def test_constructor_stops_after_the_fifty_first_item() -> None:
    consumed = 0

    def too_many_items():
        nonlocal consumed
        for index in range(MAX_DISCOVERY_ITEMS + 20):
            consumed += 1
            yield XAttachmentProbeItem(
                stable_key=f"stable-{index}",
                selector_key=f"selector-{index}",
                expected_media_key=f"media-{index}",
                media_kind="video",
            )

    with pytest.raises(GraphValidationError, match="count"):
        ScriptedGraphFakeAdapter(too_many_items())

    assert consumed == MAX_DISCOVERY_ITEMS + 1

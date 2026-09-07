from __future__ import annotations

from pathlib import Path

import pytest

from video_download_control.editing.ai import (
    ProviderCapability,
    SpeechOptions,
    TranslationItem,
    TranslationRevision,
    default_capabilities,
)
from video_download_control.editing.timeline import (
    TimelineError,
    parse_srt,
    parse_webvtt,
    serialize_srt,
    serialize_webvtt,
)


def test_srt_and_webvtt_preserve_timeline_and_have_stable_ids():
    srt = b"1\n00:00:01,250 --> 00:00:02,500\nHello\n\n2\n00:00:03,000 --> 00:00:04,125\nWorld\n"
    cues = parse_srt(srt, language="en")
    assert [(cue.start_ms, cue.end_ms, cue.source_text) for cue in cues] == [
        (1250, 2500, "Hello"),
        (3000, 4125, "World"),
    ]
    assert parse_srt(serialize_srt(cues), language="en") == cues

    vtt = serialize_webvtt(cues)
    parsed = parse_webvtt(vtt, language="en")
    assert [(cue.start_ms, cue.end_ms, cue.source_text) for cue in parsed] == [
        (1250, 2500, "Hello"),
        (3000, 4125, "World"),
    ]


def test_translation_revision_requires_exact_cue_order():
    cues = parse_srt(b"1\n00:00:00,000 --> 00:00:01,000\nOne\n\n2\n00:00:01,000 --> 00:00:02,000\nTwo\n")
    good = TranslationRevision(
        source_language="en",
        target_language="zh-CN",
        provider_id="fixture",
        model_id="fixture-v1",
        items=tuple(TranslationItem(cue.id, text) for cue, text in zip(cues, ("一", "二"), strict=True)),
    )
    good.validate_against(cues)
    with pytest.raises(ValueError, match="preserve"):
        TranslationRevision(
            source_language="en",
            target_language="zh-CN",
            provider_id="fixture",
            model_id="fixture-v1",
            items=tuple(reversed(good.items)),
        ).validate_against(cues)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"1\n00:00:02,000 --> 00:00:01,000\nbackwards\n",
        b"1\nnot-a-time\ntext\n",
        b"\xff",
    ],
)
def test_invalid_subtitles_fail_closed(payload):
    with pytest.raises(TimelineError):
        parse_srt(payload)


def test_capability_inventory_does_not_claim_uninstalled_ai_models():
    blocked = default_capabilities(media_ready=False)
    assert {item.status for item in blocked} == {"blocked"}
    ready_media = default_capabilities(media_ready=True)
    by_operation = {item.operation: item for item in ready_media}
    assert by_operation["segment"].status == by_operation["cover"].status == "ready"
    assert by_operation["translate"].status == by_operation["dub"].status == "blocked"
    assert by_operation["translate"].requirements == ("ai_runtime", "model")
    assert all(item.data_egress == () for item in ready_media)


def test_provider_contracts_reject_unsafe_or_unreviewed_values(tmp_path: Path):
    with pytest.raises(ValueError, match="remote capabilities"):
        ProviderCapability(
            operation="translate",
            label="translate",
            status="blocked",
            execution="remote",
            description="fixture",
        )
    with pytest.raises(ValueError, match="timing policy"):
        SpeechOptions(voice_id="standard", language="zh-CN", rate=1.5)

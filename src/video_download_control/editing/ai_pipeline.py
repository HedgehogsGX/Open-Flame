"""Canonical, bounded persistence contracts for local AI editing work."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, Mapping, Sequence

from .ai import TranslationRevision
from .ai_authorization import AiOperationAuthorization, parse_operation_authorization
from .timeline import MAX_CUES, TimelineCue, TimelineError, serialize_webvtt


MAX_AI_REQUEST_BYTES = 256 * 1024
# Leave deterministic room inside the 4 MiB isolated-runtime request envelope
# for operation metadata and the separately bounded glossary.
MAX_TIMELINE_JSON_BYTES = 3 * 1024 * 1024
MAX_GLOSSARY_ITEMS = 200
MAX_TIMELINE_MILLISECONDS = 7 * 24 * 60 * 60 * 1000

_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_PROVIDER_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_CUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SPEAKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class AiPipelineError(ValueError):
    """Stored or incoming AI pipeline data is not canonical and bounded."""


@dataclass(frozen=True, slots=True)
class CanonicalAiRequest:
    project_id: str
    operation: Literal["transcribe", "translate"]
    provider_id: str
    model_id: str
    source_revision_id: str | None
    source_language: str | None
    target_language: str | None
    authorization: AiOperationAuthorization | None
    request: dict[str, Any]
    request_json: str
    request_sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalTimeline:
    language: str
    cues: tuple[TimelineCue, ...]
    cues_json: str
    cues_sha256: str


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AiPipelineError("invalid identifier")
    return value


def _provider_token(value: object) -> str:
    if not isinstance(value, str) or not _PROVIDER_TOKEN.fullmatch(value):
        raise AiPipelineError("invalid provider or model")
    return value


def _language(value: object) -> str:
    if not isinstance(value, str) or not _LANGUAGE.fullmatch(value):
        raise AiPipelineError("invalid language")
    return value


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise AiPipelineError("invalid text")
    result = value.strip()
    if not result or any(
        ord(character) < 32 and character not in "\n\t" for character in result
    ):
        raise AiPipelineError("invalid text")
    _utf8(result, "invalid text")
    return result


def _utf8(value: str, message: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError:
        raise AiPipelineError(message) from None


def _exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str]) -> None:
    keys = set(value)
    if keys - allowed or not required <= keys or any(not isinstance(key, str) for key in keys):
        raise AiPipelineError("invalid request fields")


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise AiPipelineError("invalid boolean")
    return value


def canonical_ai_request(
    *,
    project_id: str,
    operation: str,
    provider_id: str,
    model_id: str,
    source_revision_id: str | None,
    options: Mapping[str, Any],
    authorization: AiOperationAuthorization | Mapping[str, Any] | None = None,
) -> CanonicalAiRequest:
    """Return one exact request representation suitable for hashing and replay."""

    project_id = _identifier(project_id)
    provider_id = _provider_token(provider_id)
    model_id = _provider_token(model_id)
    if not isinstance(options, Mapping):
        raise AiPipelineError("invalid request options")

    source_language: str | None = None
    target_language: str | None = None
    if operation == "transcribe":
        if source_revision_id is not None:
            raise AiPipelineError("transcription cannot have a source revision")
        _exact_keys(
            options,
            {
                "language",
                "word_timestamps",
                "vad",
                "clip_start_ms",
                "clip_end_ms",
            },
            set(),
        )
        language_value = options.get("language")
        language = None if language_value is None else _language(language_value)
        normalized_options: dict[str, Any] = {
            "language": language,
            "vad": _boolean(options.get("vad", True)),
            "word_timestamps": _boolean(options.get("word_timestamps", True)),
        }
        clip_start = options.get("clip_start_ms")
        clip_end = options.get("clip_end_ms")
        if (clip_start is None) != (clip_end is None):
            raise AiPipelineError("incomplete transcription clip")
        if clip_start is not None:
            if (
                isinstance(clip_start, bool)
                or not isinstance(clip_start, int)
                or isinstance(clip_end, bool)
                or not isinstance(clip_end, int)
                or clip_start < 0
                or clip_end <= clip_start
                or clip_end > MAX_TIMELINE_MILLISECONDS
            ):
                raise AiPipelineError("invalid transcription clip")
            normalized_options.update(
                clip_start_ms=clip_start,
                clip_end_ms=clip_end,
            )
    elif operation == "translate":
        source_revision_id = _identifier(source_revision_id)
        _exact_keys(
            options,
            {"source_language", "target_language", "glossary"},
            {"source_language", "target_language"},
        )
        source_language = _language(options["source_language"])
        target_language = _language(options["target_language"])
        if source_language.casefold() == target_language.casefold():
            raise AiPipelineError("translation languages must differ")
        raw_glossary = options.get("glossary", [])
        if (
            not isinstance(raw_glossary, (list, tuple))
            or len(raw_glossary) > MAX_GLOSSARY_ITEMS
        ):
            raise AiPipelineError("invalid glossary")
        glossary: list[list[str]] = []
        seen_terms: set[str] = set()
        for item in raw_glossary:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise AiPipelineError("invalid glossary")
            source_text = _text(item[0], 500)
            target_text = _text(item[1], 500)
            if source_text in seen_terms:
                raise AiPipelineError("duplicate glossary term")
            seen_terms.add(source_text)
            glossary.append([source_text, target_text])
        normalized_options = {
            "glossary": glossary,
            "source_language": source_language,
            "target_language": target_language,
        }
    else:
        raise AiPipelineError("unsupported AI operation")

    normalized_authorization: AiOperationAuthorization | None = None
    if authorization is not None:
        try:
            normalized_authorization = parse_operation_authorization(authorization)
        except (TypeError, ValueError):
            raise AiPipelineError("invalid AI authorization") from None
        if (
            normalized_authorization.operation != operation
            or normalized_authorization.provider_id != provider_id
            or normalized_authorization.model_id != model_id
        ):
            raise AiPipelineError("AI authorization does not match request")

    request = {
        "model_id": model_id,
        "operation": operation,
        "options": normalized_options,
        "project_id": project_id,
        "provider_id": provider_id,
        "source_revision_id": source_revision_id,
    }
    if normalized_authorization is not None:
        request["authorization"] = normalized_authorization.to_dict()
    encoded = json.dumps(
        request, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    encoded_bytes = _utf8(encoded, "invalid AI request encoding")
    if len(encoded_bytes) > MAX_AI_REQUEST_BYTES:
        raise AiPipelineError("AI request is too large")
    return CanonicalAiRequest(
        project_id=project_id,
        operation=operation,
        provider_id=provider_id,
        model_id=model_id,
        source_revision_id=source_revision_id,
        source_language=source_language,
        target_language=target_language,
        authorization=normalized_authorization,
        request=request,
        request_json=encoded,
        request_sha256=sha256(encoded_bytes).hexdigest(),
    )


def decode_ai_request(value: object, expected_sha256: object) -> CanonicalAiRequest:
    if not isinstance(value, str) or not isinstance(expected_sha256, str):
        raise AiPipelineError("invalid stored AI request")
    value_bytes = _utf8(value, "invalid stored AI request")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or len(value_bytes) > MAX_AI_REQUEST_BYTES
    ):
        raise AiPipelineError("invalid stored AI request")
    try:
        raw = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        raise AiPipelineError("invalid stored AI request") from None
    if not isinstance(raw, dict):
        raise AiPipelineError("invalid stored AI request")
    required = {
        "model_id",
        "operation",
        "options",
        "project_id",
        "provider_id",
        "source_revision_id",
    }
    _exact_keys(raw, required | {"authorization"}, required)
    if "authorization" in raw and raw["authorization"] is None:
        raise AiPipelineError("invalid stored AI request")
    canonical = canonical_ai_request(
        project_id=raw["project_id"],
        operation=raw["operation"],
        provider_id=raw["provider_id"],
        model_id=raw["model_id"],
        source_revision_id=raw["source_revision_id"],
        options=raw["options"],
        authorization=raw.get("authorization"),
    )
    if canonical.request_json != value or canonical.request_sha256 != expected_sha256:
        raise AiPipelineError("stored AI request changed")
    return canonical


def canonical_timeline(
    cues: Sequence[TimelineCue], *, language: str
) -> CanonicalTimeline:
    language = _language(language)
    if isinstance(cues, (str, bytes, bytearray)):
        raise AiPipelineError("invalid timeline")
    try:
        values = tuple(cues)
    except TypeError:
        raise AiPipelineError("invalid timeline") from None
    if not values or len(values) > MAX_CUES:
        raise AiPipelineError("invalid timeline cue count")

    normalized: list[TimelineCue] = []
    seen_ids: set[str] = set()
    previous_start = -1
    for order, cue in enumerate(values):
        if not isinstance(cue, TimelineCue):
            raise AiPipelineError("invalid timeline cue")
        if (
            isinstance(cue.order, bool)
            or not isinstance(cue.order, int)
            or isinstance(cue.start_ms, bool)
            or not isinstance(cue.start_ms, int)
            or isinstance(cue.end_ms, bool)
            or not isinstance(cue.end_ms, int)
            or cue.order != order
            or not isinstance(cue.id, str)
            or not _CUE_ID.fullmatch(cue.id)
            or cue.id in seen_ids
            or cue.start_ms < previous_start
            or cue.end_ms > MAX_TIMELINE_MILLISECONDS
            or cue.source_language != language
            or (
                cue.speaker_id is not None
                and (
                    not isinstance(cue.speaker_id, str)
                    or not _SPEAKER_ID.fullmatch(cue.speaker_id)
                )
            )
        ):
            raise AiPipelineError("invalid timeline cue")
        try:
            normalized_cue = TimelineCue(
                id=cue.id,
                order=cue.order,
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                source_text=cue.source_text,
                source_language=cue.source_language,
                speaker_id=cue.speaker_id,
            )
        except (TimelineError, TypeError, ValueError):
            raise AiPipelineError("invalid timeline cue") from None
        _utf8(normalized_cue.source_text, "invalid timeline cue")
        seen_ids.add(cue.id)
        previous_start = cue.start_ms
        normalized.append(normalized_cue)

    payload = [
        {
            "end_ms": cue.end_ms,
            "id": cue.id,
            "order": cue.order,
            "source_language": cue.source_language,
            "source_text": cue.source_text,
            "speaker_id": cue.speaker_id,
            "start_ms": cue.start_ms,
        }
        for cue in normalized
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    encoded_bytes = _utf8(encoded, "invalid timeline encoding")
    if len(encoded_bytes) > MAX_TIMELINE_JSON_BYTES:
        raise AiPipelineError("timeline is too large")
    try:
        serialize_webvtt(tuple(normalized))
    except (TimelineError, UnicodeError):
        raise AiPipelineError("invalid timeline") from None
    return CanonicalTimeline(
        language=language,
        cues=tuple(normalized),
        cues_json=encoded,
        cues_sha256=sha256(encoded_bytes).hexdigest(),
    )


def decode_timeline(
    value: object, expected_sha256: object, *, language: object
) -> CanonicalTimeline:
    if not isinstance(value, str) or not isinstance(expected_sha256, str):
        raise AiPipelineError("invalid stored timeline")
    value_bytes = _utf8(value, "invalid stored timeline")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or len(value_bytes) > MAX_TIMELINE_JSON_BYTES
    ):
        raise AiPipelineError("invalid stored timeline")
    language = _language(language)
    try:
        raw = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        raise AiPipelineError("invalid stored timeline") from None
    if not isinstance(raw, list) or not raw:
        raise AiPipelineError("invalid stored timeline")
    cues: list[TimelineCue] = []
    required = {
        "end_ms",
        "id",
        "order",
        "source_language",
        "source_text",
        "speaker_id",
        "start_ms",
    }
    for item in raw:
        if not isinstance(item, dict):
            raise AiPipelineError("invalid stored timeline")
        _exact_keys(item, required, required)
        for field in ("order", "start_ms", "end_ms"):
            if isinstance(item[field], bool) or not isinstance(item[field], int):
                raise AiPipelineError("invalid stored timeline")
        try:
            cues.append(
                TimelineCue(
                    id=item["id"],
                    order=item["order"],
                    start_ms=item["start_ms"],
                    end_ms=item["end_ms"],
                    source_text=item["source_text"],
                    source_language=item["source_language"],
                    speaker_id=item["speaker_id"],
                )
            )
        except (TimelineError, TypeError, ValueError):
            raise AiPipelineError("invalid stored timeline") from None
    canonical = canonical_timeline(cues, language=language)
    if canonical.cues_json != value or canonical.cues_sha256 != expected_sha256:
        raise AiPipelineError("stored timeline changed")
    return canonical


def translation_timeline(
    parent: CanonicalTimeline,
    revision: TranslationRevision,
    *,
    provider_id: str,
    model_id: str,
    source_language: str,
    target_language: str,
) -> CanonicalTimeline:
    if not isinstance(revision, TranslationRevision):
        raise AiPipelineError("invalid translation result")
    if (
        revision.provider_id != provider_id
        or revision.model_id != model_id
        or revision.source_language != source_language
        or revision.target_language != target_language
        or parent.language != source_language
    ):
        raise AiPipelineError("translation result does not match its request")
    try:
        revision.validate_against(parent.cues)
    except ValueError:
        raise AiPipelineError("translation changed cue identity or order") from None
    by_id = {item.segment_id: item.target_text for item in revision.items}
    cues = tuple(
        TimelineCue(
            id=cue.id,
            order=cue.order,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            source_text=by_id[cue.id],
            source_language=target_language,
            speaker_id=cue.speaker_id,
        )
        for cue in parent.cues
    )
    return canonical_timeline(cues, language=target_language)


def validate_translation_timeline(
    parent: CanonicalTimeline, child: CanonicalTimeline
) -> None:
    if len(parent.cues) != len(child.cues):
        raise AiPipelineError("translation changed cue count")
    for source, translated in zip(parent.cues, child.cues, strict=True):
        if (
            source.id != translated.id
            or source.order != translated.order
            or source.start_ms != translated.start_ms
            or source.end_ms != translated.end_ms
            or source.speaker_id != translated.speaker_id
        ):
            raise AiPipelineError("translation changed cue identity or timing")

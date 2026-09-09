"""Standard-library OpenAI provider for the isolated Open-Flame AI runtime.

The module deliberately has no dependency on the main application or the
OpenAI SDK.  It sends only the payload required by each operation, never
follows redirects, bounds every file and response read, and exposes code-only
failures so provider responses and credentials cannot escape through errors.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn


_AUTH_ENVIRONMENT = "OPEN_FLAME_AI_OPENAI_API_KEY"
_API_ROOT = "https://api.openai.com"
_TRANSCRIPTION_ENDPOINT = "/v1/audio/transcriptions"
_TRANSLATION_ENDPOINT = "/v1/responses"
_SPEECH_ENDPOINT = "/v1/audio/speech"

_TRANSCRIPTION_MODEL = "gpt-transcribe"
_TIMESTAMP_TRANSCRIPTION_MODEL = "whisper-1"
_SPEECH_MODEL = "gpt-4o-mini-tts"

_MAX_MEDIA_BYTES = 25 * 1024 * 1024
_MAX_JSON_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_AUDIO_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_CUES = 10_000
_MAX_TRANSLATION_BATCH_CUES = 50
_MAX_CUE_TEXT = 4_096
_MAX_SPEECH_TEXT = 4_096
_MAX_TIMELINE_MS = 7 * 24 * 60 * 60 * 1_000
_MAX_TRANSLATION_PROGRESS_UPDATES = 250
_DEFAULT_TIMEOUT_SECONDS = 120.0
_MAX_TIMEOUT_SECONDS = 600.0

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,159}$")
_BCP47 = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_MEDIA_TYPES = {
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpga": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}

_STANDARD_VOICE_IDS = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)
_STANDARD_VOICE_LANGUAGES = (
    "en",
    "en-US",
    "en-GB",
    "zh",
    "zh-CN",
    "zh-TW",
)


class ProviderFailure(RuntimeError):
    """A stable, payload-free provider failure."""

    def __init__(self, code: str):
        self.code = code if _CODE.fullmatch(code) else "ai_provider_failed"
        super().__init__(self.code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fail(code: str) -> NoReturn:
    raise ProviderFailure(code) from None


def _raise_runtime_failure(code: str) -> NoReturn:
    """Raise the worker's own failure type when loaded as a runtime plugin.

    ``ai_worker.py`` currently recognizes its local ``WorkerFailure`` class.
    A direct import (for isolated provider checks) still receives the public
    ``ProviderFailure`` type from this module.
    """

    for module_name in (
        "__main__",
        "video_download_control.editing.ai_worker",
        "ai_worker",
    ):
        module = sys.modules.get(module_name)
        failure_type = getattr(module, "WorkerFailure", None) if module else None
        if isinstance(failure_type, type):
            raise failure_type(code) from None
    raise ProviderFailure(code) from None


def _mapping(value: object, code: str = "ai_provider_input_invalid") -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _fail(code)
    return value


def _text(value: object, maximum: int, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        _fail(code)
    normalized = value.strip()
    if not normalized:
        _fail(code)
    return normalized


def _timeout(context: Mapping[str, Any]) -> float:
    config = context.get("config", {})
    if config is None:
        config = {}
    config = _mapping(config, "ai_provider_config_invalid")
    raw = config.get(
        "request_timeout_seconds",
        config.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS),
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _fail("ai_provider_config_invalid")
    value = float(raw)
    if not math.isfinite(value) or not 1.0 <= value <= _MAX_TIMEOUT_SECONDS:
        _fail("ai_provider_config_invalid")
    return value


def _api_key() -> str:
    value = os.environ.get(_AUTH_ENVIRONMENT, "")
    if (
        not value
        or len(value) > 16_384
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        _fail("ai_provider_auth_missing")
    return value


def _response_status(response: object) -> int:
    value = getattr(response, "status", None)
    if value is None:
        getcode = getattr(response, "getcode", None)
        value = getcode() if callable(getcode) else None
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("ai_provider_response_invalid")
    return value


def _http_status_failure(status: object) -> NoReturn:
    if isinstance(status, bool) or not isinstance(status, int):
        _fail("ai_provider_request_failed")
    if 300 <= status <= 399:
        _fail("ai_provider_redirect_blocked")
    if status in {401, 403}:
        _fail("ai_provider_auth_failed")
    if status in {408, 504}:
        _fail("ai_provider_timeout")
    if status == 429:
        _fail("ai_provider_rate_limited")
    if status in {400, 404, 405, 409, 413, 415, 422}:
        _fail("ai_provider_request_rejected")
    if 500 <= status <= 599:
        _fail("ai_provider_unavailable")
    _fail("ai_provider_request_failed")


def _declared_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    raw = getter("Content-Length") if callable(getter) else None
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        _fail("ai_provider_response_invalid")
    value = int(raw)
    if value < 0:
        _fail("ai_provider_response_invalid")
    return value


def _post(
    endpoint: str,
    body: bytes,
    *,
    content_type: str,
    accept: str,
    maximum: int,
    timeout: float,
) -> bytes:
    key = _api_key()
    request = urllib.request.Request(
        _API_ROOT + endpoint,
        data=body,
        headers={
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + key,
            "Content-Type": content_type,
            "User-Agent": "Open-Flame-AI-Runtime/1",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = _response_status(response)
            if status != 200:
                _http_status_failure(status)
            declared = _declared_length(response)
            if declared is not None and declared > maximum:
                _fail("ai_provider_response_too_large")
            reader = getattr(response, "read", None)
            if not callable(reader):
                _fail("ai_provider_response_invalid")
            value = reader(maximum + 1)
    except ProviderFailure:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            exc.close()
        except OSError:
            pass
        _http_status_failure(status)
    except (TimeoutError, socket.timeout):
        _fail("ai_provider_timeout")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            _fail("ai_provider_timeout")
        _fail("ai_provider_unavailable")
    except OSError:
        _fail("ai_provider_unavailable")
    if not isinstance(value, bytes) or not value:
        _fail("ai_provider_response_invalid")
    if len(value) > maximum:
        _fail("ai_provider_response_too_large")
    return value


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("ai_provider_input_invalid")


def _json_response(value: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _fail("ai_provider_response_invalid")
    return _mapping(decoded, "ai_provider_response_invalid")


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _read_media(payload: Mapping[str, Any]) -> tuple[bytes, str, str]:
    path_value = payload.get("media_path")
    expected_size = payload.get("media_size")
    expected_digest = payload.get("media_sha256")
    if (
        not isinstance(path_value, str)
        or not path_value
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
        or not isinstance(expected_digest, str)
        or not _SHA256.fullmatch(expected_digest)
    ):
        _fail("ai_provider_input_invalid")
    if expected_size > _MAX_MEDIA_BYTES:
        _fail("ai_http_media_too_large")
    path = Path(path_value)
    if not path.is_absolute():
        _fail("ai_provider_input_invalid")
    suffix = path.suffix.lower()
    content_type = _MEDIA_TYPES.get(suffix)
    if content_type is None:
        _fail("ai_transcription_media_format_unsupported")
    handle = None
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or getattr(before, "st_file_attributes", 0) & reparse
            or before.st_nlink != 1
        ):
            _fail("ai_source_unavailable")
        if before.st_size > _MAX_MEDIA_BYTES:
            _fail("ai_http_media_too_large")
        if before.st_size != expected_size:
            _fail("ai_source_changed")
        handle = path.open("rb")
        opened = os.fstat(handle.fileno())
        if _signature(opened) != _signature(before):
            _fail("ai_source_changed")
        data = handle.read(_MAX_MEDIA_BYTES + 1)
        finished = os.fstat(handle.fileno())
        after = path.lstat()
        if (
            len(data) != expected_size
            or len(data) > _MAX_MEDIA_BYTES
            or _signature(finished) != _signature(before)
            or _signature(after) != _signature(before)
            or hashlib.sha256(data).hexdigest() != expected_digest
        ):
            _fail("ai_source_changed")
    except ProviderFailure:
        raise
    except OSError:
        _fail("ai_source_unavailable")
    finally:
        if handle is not None:
            handle.close()
    return data, "media" + suffix, content_type


def _multipart(
    fields: list[tuple[str, str]],
    *,
    file_name: str,
    file_type: str,
    file_data: bytes,
) -> tuple[bytes, str]:
    boundary = "----OpenFlame" + secrets.token_hex(16)
    marker = boundary.encode("ascii")
    while marker in file_data:
        boundary = "----OpenFlame" + secrets.token_hex(16)
        marker = boundary.encode("ascii")
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            (
                b"--" + marker + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            b"--" + marker + b"\r\n",
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{file_name}"\r\n'
            ).encode("ascii"),
            f"Content-Type: {file_type}\r\n\r\n".encode("ascii"),
            file_data,
            b"\r\n--" + marker + b"--\r\n",
        )
    )
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def _language_hint(options: Mapping[str, Any]) -> str | None:
    value = options.get("language")
    if value is None or value == "auto":
        return None
    if not isinstance(value, str) or not _BCP47.fullmatch(value):
        _fail("ai_provider_input_invalid")
    primary = value.split("-", 1)[0].lower()
    if not 2 <= len(primary) <= 3 or not primary.isalpha():
        _fail("ai_provider_input_invalid")
    return primary


def _detected_language(
    response: Mapping[str, Any],
    options: Mapping[str, Any],
) -> str:
    requested = options.get("language")
    if isinstance(requested, str) and requested != "auto" and _BCP47.fullmatch(requested):
        return requested
    languages = response.get("languages")
    if isinstance(languages, list):
        for item in languages:
            if isinstance(item, Mapping):
                candidate = item.get("code")
            else:
                candidate = item
            if isinstance(candidate, str) and _BCP47.fullmatch(candidate):
                return candidate
    candidate = response.get("language")
    if isinstance(candidate, str):
        aliases = {
            "chinese": "zh",
            "english": "en",
            "french": "fr",
            "german": "de",
            "italian": "it",
            "japanese": "ja",
            "korean": "ko",
            "portuguese": "pt",
            "spanish": "es",
        }
        normalized = aliases.get(candidate.strip().casefold(), candidate.strip())
        if _BCP47.fullmatch(normalized):
            return normalized
    return "und"


def _seconds_to_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    milliseconds = round(seconds * 1_000)
    if not 1 <= milliseconds <= _MAX_TIMELINE_MS:
        return None
    return milliseconds


def _coarse_duration_ms(response: Mapping[str, Any]) -> int | None:
    # A whole-file cue is valid only when the API supplies its duration.
    duration = _seconds_to_ms(response.get("duration"))
    if duration is not None:
        return duration
    usage = response.get("usage")
    if isinstance(usage, Mapping) and usage.get("type") in (None, "duration"):
        duration = _seconds_to_ms(usage.get("seconds"))
        if duration is not None:
            return duration
    return None


def _gpt_transcription_result(
    response: Mapping[str, Any],
    options: Mapping[str, Any],
) -> dict[str, object]:
    transcript = _text(
        response.get("text"), _MAX_CUE_TEXT, "ai_transcription_response_invalid"
    )
    duration_ms = _coarse_duration_ms(response)
    if duration_ms is None:
        _fail("ai_transcription_timing_unavailable")
    language = _detected_language(response, options)
    return {
        "language": language,
        "cues": [
            {
                "id": "coarse-000000",
                "order": 0,
                "start_ms": 0,
                "end_ms": duration_ms,
                "source_text": transcript,
                "source_language": language,
                "speaker_id": None,
            }
        ],
    }


def _whisper_transcription_result(
    response: Mapping[str, Any], options: Mapping[str, Any]
) -> dict[str, object]:
    raw_segments = response.get("segments")
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= _MAX_CUES:
        _fail("ai_transcription_response_invalid")
    language = _detected_language(response, options)
    cues: list[dict[str, object]] = []
    previous_end = 0
    for index, raw in enumerate(raw_segments):
        segment = _mapping(raw, "ai_transcription_response_invalid")
        start_value = segment.get("start")
        if isinstance(start_value, bool):
            _fail("ai_transcription_timing_invalid")
        start_ms = _seconds_to_ms(start_value)
        if start_value == 0 or start_value == 0.0:
            start_ms = 0
        end_ms = _seconds_to_ms(segment.get("end"))
        if (
            start_ms is None
            or end_ms is None
            or end_ms <= start_ms
            or start_ms < previous_end
        ):
            _fail("ai_transcription_timing_invalid")
        cues.append(
            {
                "id": f"segment-{index:06d}",
                "order": index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "source_text": _text(
                    segment.get("text"),
                    _MAX_CUE_TEXT,
                    "ai_transcription_response_invalid",
                ),
                "source_language": language,
                "speaker_id": None,
            }
        )
        previous_end = end_ms
    return {"language": language, "cues": cues}


def _transcribe(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    progress: Callable[[float, str], None],
) -> dict[str, object]:
    options = _mapping(payload.get("options"), "ai_provider_input_invalid")
    model_id = context.get("model_id")
    use_timestamps = model_id == _TIMESTAMP_TRANSCRIPTION_MODEL
    if model_id is not None and model_id not in (
        _TRANSCRIPTION_MODEL,
        _TIMESTAMP_TRANSCRIPTION_MODEL,
    ):
        _fail("ai_model_not_found")
    media, file_name, file_type = _read_media(payload)
    fields = [
        ("model", _TIMESTAMP_TRANSCRIPTION_MODEL if use_timestamps else _TRANSCRIPTION_MODEL),
        ("response_format", "verbose_json" if use_timestamps else "json"),
    ]
    language = _language_hint(options)
    if language is not None:
        fields.append(("language" if use_timestamps else "languages[]", language))
    if use_timestamps:
        fields.append(("timestamp_granularities[]", "segment"))
    body, content_type = _multipart(
        fields,
        file_name=file_name,
        file_type=file_type,
        file_data=media,
    )
    progress(0.10, "openai_transcription_uploading")
    response = _json_response(
        _post(
            _TRANSCRIPTION_ENDPOINT,
            body,
            content_type=content_type,
            accept="application/json",
            maximum=_MAX_JSON_RESPONSE_BYTES,
            timeout=_timeout(context),
        )
    )
    progress(0.90, "openai_transcription_received")
    if use_timestamps:
        return _whisper_transcription_result(response, options)
    return _gpt_transcription_result(response, options)


def _translation_cues(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cues = payload.get("cues")
    if not isinstance(cues, list) or not 1 <= len(cues) <= _MAX_CUES:
        _fail("ai_provider_input_invalid")
    validated: list[Mapping[str, Any]] = []
    ids: set[str] = set()
    previous_end = 0
    for index, raw in enumerate(cues):
        cue = _mapping(raw)
        cue_id = cue.get("id")
        order = cue.get("order")
        start_ms = cue.get("start_ms")
        end_ms = cue.get("end_ms")
        source_text = cue.get("source_text")
        if (
            not isinstance(cue_id, str)
            or not _TOKEN.fullmatch(cue_id)
            or cue_id in ids
            or isinstance(order, bool)
            or not isinstance(order, int)
            or order != index
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or not 0 <= start_ms <= _MAX_TIMELINE_MS
            or not 1 <= end_ms <= _MAX_TIMELINE_MS
            or start_ms < previous_end
            or end_ms <= start_ms
        ):
            _fail("ai_provider_input_invalid")
        _text(source_text, _MAX_CUE_TEXT, "ai_provider_input_invalid")
        ids.add(cue_id)
        previous_end = end_ms
        validated.append(cue)
    return validated


def _response_output_text(response: Mapping[str, Any]) -> str:
    status = response.get("status")
    if status is not None and status != "completed":
        _fail("ai_provider_response_incomplete")
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = response.get("output")
    if not isinstance(output, list):
        _fail("ai_provider_response_invalid")
    values: list[str] = []
    for raw_item in output:
        if not isinstance(raw_item, Mapping) or raw_item.get("type") != "message":
            continue
        content = raw_item.get("content")
        if not isinstance(content, list):
            _fail("ai_provider_response_invalid")
        for raw_part in content:
            if not isinstance(raw_part, Mapping):
                _fail("ai_provider_response_invalid")
            if raw_part.get("type") == "refusal":
                _fail("ai_provider_refused")
            if raw_part.get("type") == "output_text":
                text_value = raw_part.get("text")
                if not isinstance(text_value, str):
                    _fail("ai_provider_response_invalid")
                values.append(text_value)
    combined = "".join(values).strip()
    if not combined:
        _fail("ai_provider_response_invalid")
    return combined


def _translated_items(value: str, expected_ids: list[str]) -> list[dict[str, str]]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        _fail("ai_provider_response_invalid")
    wrapper = _mapping(decoded, "ai_provider_response_invalid")
    if set(wrapper) != {"items"}:
        _fail("ai_provider_response_invalid")
    raw_items = wrapper.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(expected_ids):
        _fail("ai_translation_alignment_invalid")
    items: list[dict[str, str]] = []
    received_ids: list[str] = []
    for raw in raw_items:
        item = _mapping(raw, "ai_provider_response_invalid")
        if set(item) != {"segment_id", "target_text"}:
            _fail("ai_provider_response_invalid")
        segment_id = item.get("segment_id")
        if not isinstance(segment_id, str) or not _TOKEN.fullmatch(segment_id):
            _fail("ai_translation_alignment_invalid")
        received_ids.append(segment_id)
        items.append(
            {
                "segment_id": segment_id,
                "target_text": _text(
                    item.get("target_text"),
                    _MAX_CUE_TEXT,
                    "ai_translation_response_invalid",
                ),
            }
        )
    if (
        len(received_ids) != len(set(received_ids))
        or received_ids != expected_ids
    ):
        _fail("ai_translation_alignment_invalid")
    return items


def _translation_request(
    *,
    model_id: str,
    cues: list[Mapping[str, Any]],
    source_language: str,
    target_language: str,
    glossary: list[Mapping[str, Any]],
) -> dict[str, object]:
    cue_ids = [str(cue["id"]) for cue in cues]
    return {
        "model": model_id,
        "store": False,
        "instructions": (
            "Translate every subtitle cue independently. Preserve meaning, punctuation, "
            "and line breaks. Follow the glossary when applicable. Return exactly one "
            "item per input cue, in input order, and preserve every segment_id exactly."
        ),
        "input": json.dumps(
            {
                "source_language": source_language,
                "target_language": target_language,
                "items": [
                    {
                        "segment_id": str(cue["id"]),
                        "source_text": cue["source_text"],
                    }
                    for cue in cues
                ],
                "glossary": glossary,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "open_flame_translation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "items": {
                            "type": "array",
                            "minItems": len(cues),
                            "maxItems": len(cues),
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "segment_id": {
                                        "type": "string",
                                        "enum": cue_ids,
                                    },
                                    "target_text": {"type": "string"},
                                },
                                "required": ["segment_id", "target_text"],
                            },
                        },
                    },
                    "required": ["items"],
                },
            }
        },
    }


def _translation_batch(
    *,
    model_id: str,
    cues: list[Mapping[str, Any]],
    start: int,
    source_language: str,
    target_language: str,
    glossary: list[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], bytes]:
    maximum = min(start + _MAX_TRANSLATION_BATCH_CUES, len(cues))
    lower = start + 1
    upper = maximum
    selected: tuple[list[Mapping[str, Any]], bytes] | None = None
    while lower <= upper:
        end = (lower + upper) // 2
        batch = cues[start:end]
        body = _json_bytes(
            _translation_request(
                model_id=model_id,
                cues=batch,
                source_language=source_language,
                target_language=target_language,
                glossary=glossary,
            )
        )
        if len(body) <= _MAX_JSON_REQUEST_BYTES:
            selected = batch, body
            lower = end + 1
        else:
            upper = end - 1
    if selected is None:
        _fail("ai_provider_request_too_large")
    return selected


def _translate(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    progress: Callable[[float, str], None],
) -> dict[str, object]:
    model_id = context.get("model_id")
    if not isinstance(model_id, str) or not _TOKEN.fullmatch(model_id):
        _fail("ai_model_not_found")
    source_language = payload.get("source_language")
    target_language = payload.get("target_language")
    if (
        not isinstance(source_language, str)
        or not _BCP47.fullmatch(source_language)
        or not isinstance(target_language, str)
        or not _BCP47.fullmatch(target_language)
    ):
        _fail("ai_provider_input_invalid")
    raw_glossary = payload.get("glossary")
    if not isinstance(raw_glossary, list) or len(raw_glossary) > 1_000:
        _fail("ai_provider_input_invalid")
    glossary: list[Mapping[str, Any]] = []
    for raw in raw_glossary:
        item = _mapping(raw)
        if set(item) != {"source", "target"}:
            _fail("ai_provider_input_invalid")
        _text(item.get("source"), 500, "ai_provider_input_invalid")
        _text(item.get("target"), 500, "ai_provider_input_invalid")
        glossary.append(item)
    cues = _translation_cues(payload)
    timeout = _timeout(context)
    items: list[dict[str, str]] = []
    progress(0.05, "openai_translation_starting")
    offset = 0
    while offset < len(cues):
        batch, body = _translation_batch(
            model_id=model_id,
            cues=cues,
            start=offset,
            source_language=source_language,
            target_language=target_language,
            glossary=glossary,
        )
        response = _json_response(
            _post(
                _TRANSLATION_ENDPOINT,
                body,
                content_type="application/json",
                accept="application/json",
                maximum=_MAX_JSON_RESPONSE_BYTES,
                timeout=timeout,
            )
        )
        expected_ids = [str(cue["id"]) for cue in batch]
        items.extend(
            _translated_items(_response_output_text(response), expected_ids)
        )
        completed = offset + len(batch)
        previous_bucket = offset * _MAX_TRANSLATION_PROGRESS_UPDATES // len(cues)
        current_bucket = completed * _MAX_TRANSLATION_PROGRESS_UPDATES // len(cues)
        if completed == len(cues) or current_bucket > previous_bucket:
            progress(
                0.05 + (0.90 * completed / len(cues)),
                "openai_translation_batch_complete",
            )
        offset = completed
    if [item["segment_id"] for item in items] != [str(cue["id"]) for cue in cues]:
        _fail("ai_translation_alignment_invalid")
    return {
        "source_language": source_language,
        "target_language": target_language,
        "items": items,
    }


def _speech_output_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("ai_provider_input_invalid")
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != ".wav":
        _fail("ai_provider_input_invalid")
    try:
        parent = path.parent
        info = parent.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse
        ):
            _fail("ai_audio_output_unavailable")
        if path.exists() or path.is_symlink():
            _fail("ai_audio_output_unavailable")
    except ProviderFailure:
        raise
    except OSError:
        _fail("ai_audio_output_unavailable")
    return path


def _write_audio(path: Path, value: bytes) -> None:
    if len(value) < 12 or value[:4] != b"RIFF" or value[8:12] != b"WAVE":
        _fail("ai_audio_output_invalid")
    created = False
    try:
        with path.open("xb") as handle:
            created = True
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _fail("ai_audio_output_unavailable")
    except OSError:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        _fail("ai_audio_output_unavailable")


def _synthesize(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    progress: Callable[[float, str], None],
) -> dict[str, object]:
    text = _text(payload.get("text"), _MAX_SPEECH_TEXT, "ai_speech_input_too_large")
    options = _mapping(payload.get("options"), "ai_provider_input_invalid")
    voice_id = options.get("voice_id")
    if voice_id not in _STANDARD_VOICE_IDS:
        _fail("ai_voice_not_allowed")
    language = options.get("language")
    if not isinstance(language, str) or not _BCP47.fullmatch(language):
        _fail("ai_provider_input_invalid")
    rate = options.get("rate")
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not 0.88 <= rate <= 1.12
    ):
        _fail("ai_provider_input_invalid")
    speed = float(rate)
    if not math.isfinite(speed):
        _fail("ai_provider_input_invalid")
    style = options.get("style")
    if style is not None:
        if not isinstance(style, str) or len(style) > 500 or any(
            ord(character) < 32 and character not in "\n\t" for character in style
        ):
            _fail("ai_provider_input_invalid")
        style = style.strip()
    output_path = _speech_output_path(payload.get("output_path"))
    request: dict[str, object] = {
        "model": _SPEECH_MODEL,
        "input": text,
        "voice": voice_id,
        "response_format": "wav",
        "speed": speed,
    }
    if style:
        request["instructions"] = style
    progress(0.10, "openai_speech_generating")
    audio = _post(
        _SPEECH_ENDPOINT,
        _json_bytes(request),
        content_type="application/json",
        accept="audio/wav, application/octet-stream",
        maximum=_MAX_AUDIO_RESPONSE_BYTES,
        timeout=_timeout(context),
    )
    _write_audio(output_path, audio)
    progress(0.95, "openai_speech_complete")
    return {}


def _health(progress: Callable[[float, str], None]) -> dict[str, object]:
    progress(0.90, "openai_provider_configured")
    return {
        "voices": [
            {
                "id": voice_id,
                "label": voice_id.capitalize(),
                "languages": list(_STANDARD_VOICE_LANGUAGES),
                "is_clone": False,
            }
            for voice_id in _STANDARD_VOICE_IDS
        ]
    }


def handle(
    operation: str,
    payload: Mapping[str, object],
    context: Mapping[str, object],
    progress: Callable[[float, str], None],
) -> Mapping[str, object]:
    """Execute one provider operation using the isolated runtime contract."""

    try:
        if operation not in {"health", "transcribe", "translate", "synthesize"}:
            _fail("ai_provider_operation_unsupported")
        payload_value = _mapping(payload)
        context_value = _mapping(context)
        if not callable(progress):
            _fail("ai_provider_input_invalid")
        if operation == "health":
            return _health(progress)
        if operation == "transcribe":
            return _transcribe(payload_value, context_value, progress)
        if operation == "translate":
            return _translate(payload_value, context_value, progress)
        return _synthesize(payload_value, context_value, progress)
    except ProviderFailure as exc:
        _raise_runtime_failure(exc.code)
    except Exception as exc:
        code = getattr(exc, "code", None)
        _raise_runtime_failure(
            code if isinstance(code, str) and _CODE.fullmatch(code) else "ai_provider_failed"
        )


__all__ = ["ProviderFailure", "handle"]

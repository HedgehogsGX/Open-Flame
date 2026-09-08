"""Small, dependency-free wire contract for the optional AI runtime.

The control process and the isolated AI worker exchange one bounded JSON
request, one bounded JSON result, and progress-only JSON Lines on stdout.
Free-form provider output is never part of this protocol.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping


PROTOCOL_SCHEMA = 1
RUNTIME_MANIFEST_SCHEMA = 1
RUNTIME_ID = "open-flame-ai-runtime"
RUNTIME_VERSION = "1"
OPERATIONS = frozenset({"health", "transcribe", "translate", "synthesize"})
TASK_OPERATIONS = frozenset({"transcribe", "translate", "synthesize"})
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_PROGRESS_LINE_BYTES = 1024
MAX_PROGRESS_LINES = 256
MAX_CUES = 10_000
MAX_CUE_TEXT = 8_000
MAX_TRANSLATION_ITEMS = 10_000
MAX_GLOSSARY_ITEMS = 1_000
MAX_GLOSSARY_TEXT = 500
MAX_SYNTHESIS_TEXT = 8_000

_ID = re.compile(r"^[0-9a-f]{32}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,159}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_BCP47 = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$"
)
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}$")


class AiProtocolError(ValueError):
    """Stable protocol failure that contains no provider or media payload."""

    def __init__(self, code: str):
        self.code = code if _CODE.fullmatch(code) else "ai_protocol_invalid"
        super().__init__(self.code)


def _exact(
    value: object,
    fields: set[str],
    code: str = "ai_protocol_invalid",
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AiProtocolError(code)
    if set(value) != fields:
        raise AiProtocolError(code)
    return value


def _token(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise AiProtocolError("ai_protocol_invalid")
    return value


def _text(value: object, maximum: int, *, required: bool = True) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise AiProtocolError("ai_protocol_invalid")
    normalized = value.strip()
    if required and not normalized:
        raise AiProtocolError("ai_protocol_invalid")
    return normalized


def _language(value: object, *, auto: bool = False, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if value == "auto" and auto:
        return "auto"
    if not isinstance(value, str) or not _BCP47.fullmatch(value):
        raise AiProtocolError("ai_protocol_invalid")
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AiProtocolError("ai_protocol_invalid")
    return value


def _number(value: object, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AiProtocolError("ai_protocol_invalid")
    converted = float(value)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        raise AiProtocolError("ai_protocol_invalid")
    return converted


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise AiProtocolError("ai_protocol_invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AiProtocolError("ai_protocol_invalid")
    return value


def relative_runtime_path(value: object) -> str:
    """Return one canonical, portable, traversal-free runtime path."""

    if not isinstance(value, str) or not value or len(value) > 600 or "\\" in value:
        raise AiProtocolError("ai_manifest_invalid")
    if value.startswith("/") or value.endswith("/"):
        raise AiProtocolError("ai_manifest_invalid")
    segments = value.split("/")
    if any(not _SAFE_PATH_SEGMENT.fullmatch(segment) for segment in segments):
        raise AiProtocolError("ai_manifest_invalid")
    return "/".join(segments)


def _absolute_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32_000
        or "\x00" in value
    ):
        raise AiProtocolError("ai_protocol_invalid")
    path = Path(value)
    if not path.is_absolute():
        raise AiProtocolError("ai_protocol_invalid")
    return str(path)


def _cue(value: object, expected_order: int) -> dict[str, object]:
    row = _exact(
        value,
        {"id", "order", "start_ms", "end_ms", "source_text", "source_language", "speaker_id"},
    )
    cue_id = _token(row["id"])
    order = _integer(row["order"], 0, MAX_CUES - 1)
    start = _integer(row["start_ms"], 0, 7 * 24 * 60 * 60 * 1000)
    end = _integer(row["end_ms"], 1, 7 * 24 * 60 * 60 * 1000)
    if order != expected_order or end <= start:
        raise AiProtocolError("ai_protocol_invalid")
    return {
        "id": cue_id,
        "order": order,
        "start_ms": start,
        "end_ms": end,
        "source_text": _text(row["source_text"], MAX_CUE_TEXT),
        "source_language": _language(row["source_language"]),
        "speaker_id": _token(row["speaker_id"], nullable=True),
    }


def _cues(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CUES:
        raise AiProtocolError("ai_protocol_invalid")
    cues = [_cue(item, index) for index, item in enumerate(value)]
    ids = [item["id"] for item in cues]
    if len(ids) != len(set(ids)):
        raise AiProtocolError("ai_protocol_invalid")
    previous_end = 0
    for cue in cues:
        if int(cue["start_ms"]) < previous_end:
            raise AiProtocolError("ai_protocol_invalid")
        previous_end = int(cue["end_ms"])
    return cues


def _transcription_payload(value: object) -> dict[str, object]:
    payload = _exact(value, {"media_path", "media_size", "media_sha256", "options"})
    options = _exact(payload["options"], {"language", "word_timestamps", "vad"})
    return {
        "media_path": _absolute_path(payload["media_path"]),
        "media_size": _integer(payload["media_size"], 1, 16 * 1024**3),
        "media_sha256": _digest(payload["media_sha256"]),
        "options": {
            "language": _language(options["language"], auto=True, nullable=True),
            "word_timestamps": _boolean(options["word_timestamps"]),
            "vad": _boolean(options["vad"]),
        },
    }


def _translation_payload(value: object) -> dict[str, object]:
    payload = _exact(
        value,
        {"cues", "source_language", "target_language", "glossary"},
    )
    raw_glossary = payload["glossary"]
    if not isinstance(raw_glossary, list) or len(raw_glossary) > MAX_GLOSSARY_ITEMS:
        raise AiProtocolError("ai_protocol_invalid")
    glossary: list[dict[str, str]] = []
    for item in raw_glossary:
        row = _exact(item, {"source", "target"})
        glossary.append(
            {
                "source": _text(row["source"], MAX_GLOSSARY_TEXT),
                "target": _text(row["target"], MAX_GLOSSARY_TEXT),
            }
        )
    return {
        "cues": _cues(payload["cues"]),
        "source_language": _language(payload["source_language"]),
        "target_language": _language(payload["target_language"]),
        "glossary": glossary,
    }


def _synthesis_payload(value: object) -> dict[str, object]:
    payload = _exact(value, {"text", "output_path", "options"})
    options = _exact(payload["options"], {"voice_id", "language", "rate", "style"})
    return {
        "text": _text(payload["text"], MAX_SYNTHESIS_TEXT),
        "output_path": _absolute_path(payload["output_path"]),
        "options": {
            "voice_id": _token(options["voice_id"]),
            "language": _language(options["language"]),
            "rate": _number(options["rate"], 0.88, 1.12),
            "style": None
            if options["style"] is None
            else _text(options["style"], 500, required=False),
        },
    }


def validate_request(value: object) -> dict[str, object]:
    request = _exact(
        value,
        {"schema", "request_id", "operation", "provider_id", "model_id", "payload"},
    )
    if request["schema"] != PROTOCOL_SCHEMA:
        raise AiProtocolError("ai_protocol_version_mismatch")
    request_id = request["request_id"]
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise AiProtocolError("ai_protocol_invalid")
    operation = request["operation"]
    if operation not in OPERATIONS:
        raise AiProtocolError("ai_operation_unsupported")
    if operation == "health":
        _exact(request["payload"], set())
        payload: dict[str, object] = {}
    elif operation == "transcribe":
        payload = _transcription_payload(request["payload"])
    elif operation == "translate":
        payload = _translation_payload(request["payload"])
    else:
        payload = _synthesis_payload(request["payload"])
    return {
        "schema": PROTOCOL_SCHEMA,
        "request_id": request_id,
        "operation": operation,
        "provider_id": _token(request["provider_id"]),
        "model_id": _token(request["model_id"], nullable=True),
        "payload": payload,
    }


def _voice(value: object) -> dict[str, object]:
    row = _exact(value, {"id", "label", "languages", "is_clone"})
    languages = row["languages"]
    if not isinstance(languages, list) or not 1 <= len(languages) <= 64:
        raise AiProtocolError("ai_protocol_invalid")
    return {
        "id": _token(row["id"]),
        "label": _text(row["label"], 120),
        "languages": [_language(item) for item in languages],
        "is_clone": _boolean(row["is_clone"]),
    }


def _health_result(value: object) -> dict[str, object]:
    payload = _exact(value, {"operations", "voices", "runtime"})
    operations = payload["operations"]
    if (
        not isinstance(operations, list)
        or not operations
        or len(operations) != len(set(operations))
        or any(item not in TASK_OPERATIONS for item in operations)
    ):
        raise AiProtocolError("ai_protocol_invalid")
    voices = payload["voices"]
    if not isinstance(voices, list) or len(voices) > 512:
        raise AiProtocolError("ai_protocol_invalid")
    runtime = _exact(payload["runtime"], {"implementation", "version", "bits"})
    version = runtime["version"]
    if (
        not isinstance(version, list)
        or len(version) != 3
        or any(type(item) is not int or item < 0 for item in version)
    ):
        raise AiProtocolError("ai_protocol_invalid")
    return {
        "operations": list(operations),
        "voices": [_voice(item) for item in voices],
        "runtime": {
            "implementation": _token(runtime["implementation"]),
            "version": version,
            "bits": _integer(runtime["bits"], 32, 64),
        },
    }


def _transcription_result(value: object) -> dict[str, object]:
    payload = _exact(value, {"language", "cues"})
    return {"language": _language(payload["language"]), "cues": _cues(payload["cues"])}


def _translation_result(value: object) -> dict[str, object]:
    payload = _exact(value, {"source_language", "target_language", "items"})
    items = payload["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_TRANSLATION_ITEMS:
        raise AiProtocolError("ai_protocol_invalid")
    normalized: list[dict[str, str]] = []
    for item in items:
        row = _exact(item, {"segment_id", "target_text"})
        normalized.append(
            {
                "segment_id": _token(row["segment_id"]),
                "target_text": _text(row["target_text"], MAX_CUE_TEXT),
            }
        )
    ids = [item["segment_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise AiProtocolError("ai_protocol_invalid")
    return {
        "source_language": _language(payload["source_language"]),
        "target_language": _language(payload["target_language"]),
        "items": normalized,
    }


def _synthesis_result(value: object) -> dict[str, object]:
    payload = _exact(value, {"audio"})
    audio = _exact(
        payload["audio"],
        {"path", "size", "sha256", "duration_ms", "sample_rate", "channels", "container"},
    )
    if audio["container"] != "wav":
        raise AiProtocolError("ai_protocol_invalid")
    return {
        "audio": {
            "path": _absolute_path(audio["path"]),
            "size": _integer(audio["size"], 1, 512 * 1024**2),
            "sha256": _digest(audio["sha256"]),
            "duration_ms": _integer(audio["duration_ms"], 1, 24 * 60 * 60 * 1000),
            "sample_rate": _integer(audio["sample_rate"], 8_000, 192_000),
            "channels": _integer(audio["channels"], 1, 2),
            "container": "wav",
        }
    }


def validate_result(
    value: object,
    *,
    expected_request_id: str | None = None,
    expected_operation: str | None = None,
) -> dict[str, object]:
    result = _exact(
        value,
        {
            "schema",
            "request_id",
            "operation",
            "status",
            "code",
            "provider_id",
            "model_id",
            "payload",
        },
    )
    if result["schema"] != PROTOCOL_SCHEMA:
        raise AiProtocolError("ai_protocol_version_mismatch")
    request_id = result["request_id"]
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise AiProtocolError("ai_protocol_invalid")
    operation = result["operation"]
    if operation not in OPERATIONS:
        raise AiProtocolError("ai_protocol_invalid")
    if expected_request_id is not None and request_id != expected_request_id:
        raise AiProtocolError("ai_result_mismatch")
    if expected_operation is not None and operation != expected_operation:
        raise AiProtocolError("ai_result_mismatch")
    provider_id = _token(result["provider_id"], nullable=True)
    model_id = _token(result["model_id"], nullable=True)
    status = result["status"]
    if status == "error":
        if not isinstance(result["code"], str) or not _CODE.fullmatch(result["code"]):
            raise AiProtocolError("ai_protocol_invalid")
        _exact(result["payload"], set())
        return {
            "schema": PROTOCOL_SCHEMA,
            "request_id": request_id,
            "operation": operation,
            "status": "error",
            "code": result["code"],
            "provider_id": provider_id,
            "model_id": model_id,
            "payload": {},
        }
    if status != "ok" or result["code"] != "":
        raise AiProtocolError("ai_protocol_invalid")
    if operation == "health":
        payload = _health_result(result["payload"])
    elif operation == "transcribe":
        payload = _transcription_result(result["payload"])
    elif operation == "translate":
        payload = _translation_result(result["payload"])
    else:
        payload = _synthesis_result(result["payload"])
    return {
        "schema": PROTOCOL_SCHEMA,
        "request_id": request_id,
        "operation": operation,
        "status": "ok",
        "code": "",
        "provider_id": provider_id,
        "model_id": model_id,
        "payload": payload,
    }


def validate_progress(
    value: object,
    *,
    expected_request_id: str | None = None,
) -> dict[str, object]:
    progress = _exact(value, {"schema", "type", "request_id", "fraction", "code"})
    if progress["schema"] != PROTOCOL_SCHEMA or progress["type"] != "progress":
        raise AiProtocolError("ai_progress_invalid")
    request_id = progress["request_id"]
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise AiProtocolError("ai_progress_invalid")
    if expected_request_id is not None and request_id != expected_request_id:
        raise AiProtocolError("ai_progress_invalid")
    code = progress["code"]
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise AiProtocolError("ai_progress_invalid")
    return {
        "schema": PROTOCOL_SCHEMA,
        "type": "progress",
        "request_id": request_id,
        "fraction": _number(progress["fraction"], 0.0, 1.0),
        "code": code,
    }


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AiProtocolError("ai_protocol_invalid") from exc


def read_json_file(path: Path, *, maximum: int) -> object:
    """Read a stable, single-link regular UTF-8 JSON file within a byte cap."""

    path = Path(path)
    handle = None
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or getattr(before, "st_file_attributes", 0) & reparse
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
        ):
            raise AiProtocolError("ai_protocol_file_invalid")
        handle = path.open("rb")
        opened = os.fstat(handle.fileno())
        if _signature(opened) != _signature(before):
            raise AiProtocolError("ai_protocol_file_changed")
        payload = handle.read(maximum + 1)
        finished = os.fstat(handle.fileno())
        after = path.lstat()
        if (
            len(payload) != before.st_size
            or len(payload) > maximum
            or _signature(finished) != _signature(before)
            or _signature(after) != _signature(before)
        ):
            raise AiProtocolError("ai_protocol_file_changed")
        try:
            return json.loads(payload.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AiProtocolError("ai_protocol_invalid") from exc
    except AiProtocolError:
        raise
    except OSError as exc:
        raise AiProtocolError("ai_protocol_file_unavailable") from exc
    finally:
        if handle is not None:
            handle.close()


def write_json_atomic(path: Path, value: object, *, maximum: int) -> None:
    payload = canonical_json_bytes(value)
    if not payload or len(payload) > maximum:
        raise AiProtocolError("ai_protocol_too_large")
    destination = Path(path)
    descriptor = None
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(raw)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.exists() or destination.is_symlink():
            raise AiProtocolError("ai_protocol_output_exists")
        os.link(temporary, destination)
    except AiProtocolError:
        raise
    except OSError as exc:
        raise AiProtocolError("ai_protocol_file_unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def file_sha256(path: Path, *, maximum: int) -> tuple[int, str]:
    path = Path(path)
    try:
        before = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or getattr(before, "st_file_attributes", 0) & reparse
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
        ):
            raise AiProtocolError("ai_artifact_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if _signature(opened) != _signature(before):
                raise AiProtocolError("ai_artifact_changed")
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            finished = os.fstat(handle.fileno())
        after = path.lstat()
        if _signature(finished) != _signature(before) or _signature(after) != _signature(before):
            raise AiProtocolError("ai_artifact_changed")
        return before.st_size, digest.hexdigest()
    except AiProtocolError:
        raise
    except OSError as exc:
        raise AiProtocolError("ai_artifact_unavailable") from exc


def validate_entrypoint(value: object) -> str:
    if not isinstance(value, str) or not _ENTRYPOINT.fullmatch(value):
        raise AiProtocolError("ai_manifest_invalid")
    return value


def validate_environment_name(value: object) -> str:
    if not isinstance(value, str) or not _ENVIRONMENT_NAME.fullmatch(value):
        raise AiProtocolError("ai_manifest_invalid")
    return value


__all__ = [
    "AiProtocolError",
    "MAX_PROGRESS_LINE_BYTES",
    "MAX_PROGRESS_LINES",
    "MAX_REQUEST_BYTES",
    "MAX_RESULT_BYTES",
    "OPERATIONS",
    "PROTOCOL_SCHEMA",
    "RUNTIME_ID",
    "RUNTIME_MANIFEST_SCHEMA",
    "RUNTIME_VERSION",
    "TASK_OPERATIONS",
    "canonical_json_bytes",
    "file_sha256",
    "read_json_file",
    "relative_runtime_path",
    "validate_entrypoint",
    "validate_environment_name",
    "validate_progress",
    "validate_request",
    "validate_result",
    "write_json_atomic",
]

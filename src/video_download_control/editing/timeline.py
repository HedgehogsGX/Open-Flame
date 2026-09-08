"""Strict subtitle timeline parsing and deterministic serialization."""
from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_CUES = 10_000
MAX_CUE_TEXT = 4_096
_SRT_TIME = re.compile(
    r"^(?P<h>\d{2,3}):(?P<m>[0-5]\d):(?P<s>[0-5]\d),(?P<ms>\d{3})$"
)
_VTT_TIME = re.compile(
    r"^(?:(?P<h>\d{1,3}):)?(?P<m>[0-5]?\d):(?P<s>[0-5]\d)\.(?P<ms>\d{3})$"
)


class TimelineError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TimelineCue:
    id: str
    order: int
    start_ms: int
    end_ms: int
    source_text: str
    source_language: str = "und"
    speaker_id: str | None = None

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 64:
            raise TimelineError("invalid cue id")
        if self.order < 0 or self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise TimelineError("invalid cue timing")
        if not self.source_text.strip() or len(self.source_text) > MAX_CUE_TEXT:
            raise TimelineError("invalid cue text")
        if any(ord(character) < 32 and character not in "\n\t" for character in self.source_text):
            raise TimelineError("invalid cue text")
        if self.speaker_id is not None and (
            not self.speaker_id or len(self.speaker_id) > 64
        ):
            raise TimelineError("invalid speaker id")


def _decode(payload: bytes) -> str:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SUBTITLE_BYTES:
        raise TimelineError("subtitle size is invalid")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TimelineError("subtitles must be UTF-8") from None
    if "\x00" in text:
        raise TimelineError("subtitle contains a null byte")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _milliseconds(match: re.Match[str]) -> int:
    groups = match.groupdict()
    return (
        int(groups.get("h") or 0) * 3_600_000
        + int(groups["m"]) * 60_000
        + int(groups["s"]) * 1_000
        + int(groups["ms"])
    )


def _cue_id(order: int, start_ms: int, end_ms: int, text: str) -> str:
    digest = sha256(f"{order}\0{start_ms}\0{end_ms}\0{text}".encode()).hexdigest()
    return digest[:32]


def _validate_order(cues: list[TimelineCue]) -> tuple[TimelineCue, ...]:
    if not cues or len(cues) > MAX_CUES:
        raise TimelineError("subtitle cue count is invalid")
    previous_start = -1
    for cue in cues:
        if cue.start_ms < previous_start:
            raise TimelineError("subtitle cues are out of order")
        previous_start = cue.start_ms
    return tuple(cues)


def parse_srt(payload: bytes, *, language: str = "und") -> tuple[TimelineCue, ...]:
    text = _decode(payload).strip()
    blocks = re.split(r"\n{2,}", text)
    cues: list[TimelineCue] = []
    for order, block in enumerate(blocks):
        lines = block.splitlines()
        if len(lines) < 2:
            raise TimelineError("invalid SRT cue")
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if len(lines) < 2 or " --> " not in lines[0]:
            raise TimelineError("invalid SRT timing")
        parts = lines[0].split(" --> ")
        if len(parts) != 2:
            raise TimelineError("invalid SRT timing")
        start = _SRT_TIME.fullmatch(parts[0].strip())
        end = _SRT_TIME.fullmatch(parts[1].strip())
        if start is None or end is None:
            raise TimelineError("invalid SRT timing")
        source_text = "\n".join(lines[1:]).strip()
        start_ms, end_ms = _milliseconds(start), _milliseconds(end)
        cues.append(
            TimelineCue(
                id=_cue_id(order, start_ms, end_ms, source_text),
                order=order,
                start_ms=start_ms,
                end_ms=end_ms,
                source_text=source_text,
                source_language=language,
            )
        )
    return _validate_order(cues)


def parse_webvtt(payload: bytes, *, language: str = "und") -> tuple[TimelineCue, ...]:
    text = _decode(payload)
    lines = text.splitlines()
    if not lines or not lines[0].lstrip("\ufeff").startswith("WEBVTT"):
        raise TimelineError("invalid WebVTT header")
    blocks = re.split(r"\n{2,}", "\n".join(lines[1:]).strip())
    cues: list[TimelineCue] = []
    for block in blocks:
        if not block.strip() or block.startswith(("NOTE", "STYLE", "REGION")):
            continue
        cue_lines = block.splitlines()
        timing_index = 0 if " --> " in cue_lines[0] else 1
        if len(cue_lines) <= timing_index + 1 or " --> " not in cue_lines[timing_index]:
            raise TimelineError("invalid WebVTT cue")
        parts = cue_lines[timing_index].split(" --> ")
        if len(parts) != 2:
            raise TimelineError("invalid WebVTT timing")
        start = _VTT_TIME.fullmatch(parts[0].strip())
        end_token = parts[1].strip().split(maxsplit=1)[0]
        end = _VTT_TIME.fullmatch(end_token)
        if start is None or end is None:
            raise TimelineError("invalid WebVTT timing")
        order = len(cues)
        source_text = "\n".join(cue_lines[timing_index + 1 :]).strip()
        start_ms, end_ms = _milliseconds(start), _milliseconds(end)
        cues.append(
            TimelineCue(
                id=_cue_id(order, start_ms, end_ms, source_text),
                order=order,
                start_ms=start_ms,
                end_ms=end_ms,
                source_text=source_text,
                source_language=language,
            )
        )
    return _validate_order(cues)


def parse_subtitles(
    payload: bytes, kind: Literal["srt", "vtt"], *, language: str = "und"
) -> tuple[TimelineCue, ...]:
    return parse_srt(payload, language=language) if kind == "srt" else parse_webvtt(
        payload, language=language
    )


def _format_time(milliseconds: int, separator: str) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def serialize_srt(cues: tuple[TimelineCue, ...], texts: dict[str, str] | None = None) -> bytes:
    rows: list[str] = []
    for index, cue in enumerate(_validate_order(list(cues)), start=1):
        text = cue.source_text if texts is None else texts.get(cue.id, "")
        if not text.strip() or len(text) > MAX_CUE_TEXT:
            raise TimelineError("translated subtitle text is missing or invalid")
        rows.extend(
            [
                str(index),
                f"{_format_time(cue.start_ms, ',')} --> {_format_time(cue.end_ms, ',')}",
                text,
                "",
            ]
        )
    return "\n".join(rows).encode("utf-8")


def serialize_webvtt(
    cues: tuple[TimelineCue, ...], texts: dict[str, str] | None = None
) -> bytes:
    rows = ["WEBVTT", ""]
    for cue in _validate_order(list(cues)):
        text = cue.source_text if texts is None else texts.get(cue.id, "")
        if not text.strip() or len(text) > MAX_CUE_TEXT:
            raise TimelineError("translated subtitle text is missing or invalid")
        rows.extend(
            [
                f"{_format_time(cue.start_ms, '.')} --> {_format_time(cue.end_ms, '.')}",
                text,
                "",
            ]
        )
    return "\n".join(rows).encode("utf-8")

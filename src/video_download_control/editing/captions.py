"""On-screen Chinese caption policy: punctuation, wrapping, checks and ASS.

A caption is read at a glance and its own boundary already ends the thought, so
sentence punctuation mostly adds noise on screen.  This module rewrites approved
Chinese text for display only.  The stored translation revision keeps its natural
punctuation, because that is what a reviewer reads and what speech synthesis needs
for prosody; only the caption files carry the on-screen style.

Nothing here touches the filesystem, a provider or the clock, so a caption file is
a pure function of the approved cues and one style.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .timeline import MAX_CUE_TEXT, TimelineCue


class CaptionError(ValueError):
    """A caption could not be written in the on-screen style."""


CAPTION_COMMA = "，"
# Marks that become a candidate pause.  The ASCII members only count when they
# are not inside a token such as 3.5, 12:30 or example.com.
_CAPTION_BREAKS = "。．｡，、；：！？…‥—–～〜"
_CAPTION_ASCII_BREAKS = ".,;:!?"
# Marks that carry no pause of their own and are simply removed.
_CAPTION_DROPPED = "“”‘’＂「」『』《》〈〉（）()【】〔〕｛｝"
# Where a wrapped line may end, when the caption is longer than one line.
_WRAP_BREAKS = frozenset("。！？；：，、,.!?;:")
# Every Chinese variant reads the same on screen, so the policy is not limited
# to Simplified.  A macrolanguage tag is matched on its primary subtag only.
_CHINESE_PRIMARY = frozenset({"zh", "cmn", "yue", "wuu", "hak", "nan", "gan", "hsn"})

DEFAULT_MIN_COMMA_RUN = 6
DEFAULT_MAX_LINE_CHARS = 22
DEFAULT_MAX_READING_SPEED = 12.0
# Comfortable rather than maximum: the speed a caption is held *for*, while
# ``max_reading_speed`` is only the speed a caption is warned about.
DEFAULT_TARGET_READING_SPEED = 9.0
# Forced alignment ends a word at its last detected phoneme, so a caption cut at
# that instant leaves the screen while the word is still audible.
DEFAULT_LEAD_OUT_MS = 250
DEFAULT_MIN_DURATION_MS = 1_000
# Held captions still separate, so two consecutive lines never look like one.
DEFAULT_MIN_GAP_MS = 100
MAX_DISPLAY_MS = 24 * 60 * 60 * 1000
MAX_LINE_CHARS_LIMIT = 96
MAX_FONT_SIZE = 512
MAX_PLAY_RESOLUTION = 16384
MAX_CAPTION_ISSUES = 5_000

_ASS_HEADER_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
    "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding"
)
_ASS_EVENT_FORMAT = (
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text"
)


def is_chinese_language(tag: object) -> bool:
    """True when a BCP 47 tag names any Chinese variant."""

    if not isinstance(tag, str) or not tag:
        return False
    return tag.split("-", 1)[0].casefold() in _CHINESE_PRIMARY


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    """The on-screen caption style and the thresholds its checks use."""

    font_name: str = "Microsoft YaHei"
    font_size: int = 48
    play_res_x: int = 1920
    play_res_y: int = 1080
    margin_horizontal: int = 60
    margin_vertical: int = 48
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS
    min_comma_run: int = DEFAULT_MIN_COMMA_RUN
    max_reading_speed: float = DEFAULT_MAX_READING_SPEED
    target_reading_speed: float = DEFAULT_TARGET_READING_SPEED
    lead_out_ms: int = DEFAULT_LEAD_OUT_MS
    min_duration_ms: int = DEFAULT_MIN_DURATION_MS
    min_gap_ms: int = DEFAULT_MIN_GAP_MS

    def __post_init__(self) -> None:
        name = self.font_name
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 120
            or any(character in name for character in ",\r\n{}\\")
            or any(ord(character) < 32 for character in name)
        ):
            raise CaptionError("invalid caption font name")
        for value, maximum in (
            (self.font_size, MAX_FONT_SIZE),
            (self.play_res_x, MAX_PLAY_RESOLUTION),
            (self.play_res_y, MAX_PLAY_RESOLUTION),
            (self.max_line_chars, MAX_LINE_CHARS_LIMIT),
            (self.min_comma_run, MAX_LINE_CHARS_LIMIT),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise CaptionError("invalid caption style")
        for margin in (self.margin_horizontal, self.margin_vertical):
            if (
                isinstance(margin, bool)
                or not isinstance(margin, int)
                or not 0 <= margin <= MAX_PLAY_RESOLUTION
            ):
                raise CaptionError("invalid caption style")
        for speed in (self.max_reading_speed, self.target_reading_speed):
            if (
                isinstance(speed, bool)
                or not isinstance(speed, (int, float))
                or not 0 < float(speed) <= 1000
            ):
                raise CaptionError("invalid caption style")
        for milliseconds in (self.lead_out_ms, self.min_duration_ms, self.min_gap_ms):
            if (
                isinstance(milliseconds, bool)
                or not isinstance(milliseconds, int)
                or not 0 <= milliseconds <= MAX_DISPLAY_MS
            ):
                raise CaptionError("invalid caption style")


CHINESE_CAPTION_STYLE = CaptionStyle()


def _intra_token(text: str, index: int) -> bool:
    """True when an ASCII mark belongs to a token such as 3.5, 12:30 or K.O."""

    before = text[index - 1] if index > 0 else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return (
        bool(before and after)
        and before.isascii()
        and after.isascii()
        and before.isalnum()
        and after.isalnum()
    )


def _is_caption_break(text: str, index: int) -> bool:
    character = text[index]
    if character in _CAPTION_BREAKS:
        return True
    return character in _CAPTION_ASCII_BREAKS and not _intra_token(text, index)


def normalize_caption_text(
    text: str, *, min_comma_run: int = DEFAULT_MIN_COMMA_RUN
) -> str:
    """Rewrite caption text in the on-screen punctuation style.

    Sentence marks and paired quotes or brackets are removed; a removed mark
    becomes a comma only at the first pause with enough text on both sides of it
    to be worth one, at most once per line.  Existing line breaks and marks
    inside a token are preserved, and text that would normalize to nothing is
    returned unchanged.
    """

    if not isinstance(text, str):
        raise CaptionError("caption text must be a string")
    if isinstance(min_comma_run, bool) or not isinstance(min_comma_run, int) or min_comma_run < 1:
        raise CaptionError("invalid caption style")
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = "".join(
            character for character in line if character not in _CAPTION_DROPPED
        )
        pieces: list[str] = []
        buffer: list[str] = []
        for index in range(len(cleaned)):
            if _is_caption_break(cleaned, index):
                pieces.append("".join(buffer).strip())
                buffer = []
                continue
            buffer.append(cleaned[index])
        pieces.append("".join(buffer).strip())
        pieces = [piece for piece in pieces if piece]
        if not pieces:
            lines.append("")
            continue
        result = pieces[0]
        for index, piece in enumerate(pieces[1:], 1):
            since = len(result) - (result.rfind(CAPTION_COMMA) + 1)
            tail = sum(len(remaining) for remaining in pieces[index:])
            if (
                CAPTION_COMMA not in result
                and since >= min_comma_run
                and tail >= min_comma_run
            ):
                result += CAPTION_COMMA + piece
            elif (
                result[-1].isascii()
                and result[-1].isalnum()
                and piece[0].isascii()
                and piece[0].isalnum()
            ):
                # Two Latin tokens must not be glued into one word.
                result += " " + piece
            else:
                result += piece
        lines.append(result)
    normalized = "\n".join(lines).strip("\n")
    return normalized if normalized.strip() else text


def stray_caption_punctuation(text: str) -> str:
    """Marks in a caption that the on-screen style does not allow."""

    if not isinstance(text, str):
        raise CaptionError("caption text must be a string")
    found = {character for character in text if character in _CAPTION_DROPPED}
    found.update(
        character
        for index, character in enumerate(text)
        if character != CAPTION_COMMA and _is_caption_break(text, index)
    )
    return "".join(sorted(found))


def wrap_caption_text(text: str, max_line_chars: int) -> str:
    """Break a caption at the last pause that fits, as the caption file shows it."""

    if not isinstance(text, str):
        raise CaptionError("caption text must be a string")
    if (
        isinstance(max_line_chars, bool)
        or not isinstance(max_line_chars, int)
        or not 1 <= max_line_chars <= MAX_LINE_CHARS_LIMIT
    ):
        raise CaptionError("invalid caption style")
    lines: list[str] = []
    for source_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        remaining = source_line
        wrapped: list[str] = []
        while len(remaining) > max_line_chars:
            cut = max_line_chars
            for position in range(max_line_chars, 0, -1):
                if remaining[position - 1] in _WRAP_BREAKS:
                    cut = position
                    break
            piece, remaining = remaining[:cut], remaining[cut:]
            # A line break already marks the pause its comma stood for, and a
            # line never begins with one.
            if piece.endswith(CAPTION_COMMA):
                piece = piece[:-1]
            while remaining.startswith(CAPTION_COMMA):
                remaining = remaining[1:]
            if piece:
                wrapped.append(piece)
        # Keep a genuinely blank source line, but do not add one of our own.
        if remaining or not wrapped:
            wrapped.append(remaining)
        lines.extend(wrapped)
    return "\n".join(lines)


def caption_display_text(text: str, style: CaptionStyle) -> str:
    """Return the exact text a caption file shows for one approved cue."""

    if not isinstance(style, CaptionStyle):
        raise CaptionError("invalid caption style")
    normalized = normalize_caption_text(text, min_comma_run=style.min_comma_run)
    wrapped = wrap_caption_text(normalized, style.max_line_chars)
    # Wrapping only adds line breaks.  A cue long enough for them to push it past
    # the timeline limit is kept unwrapped rather than silently truncated.
    if len(wrapped) > MAX_CUE_TEXT:
        return normalized if len(normalized) <= MAX_CUE_TEXT else text
    return wrapped


def caption_display_texts(
    cues: tuple[TimelineCue, ...], style: CaptionStyle
) -> dict[str, str]:
    """Map every cue id to the text its caption files show."""

    return {cue.id: caption_display_text(cue.source_text, style) for cue in cues}


def hold_captions(
    cues: tuple[TimelineCue, ...],
    texts: dict[str, str],
    style: CaptionStyle,
    *,
    limit_ms: int,
) -> tuple[TimelineCue, ...]:
    """Hold each caption long enough to finish reading it, without overlapping.

    Forced alignment ends a word at its last detected phoneme, so a caption that
    ends exactly there leaves the screen while the speaker is still finishing the
    word, and a translated caption is often longer to read than the original was
    to say.  This extends the *displayed* end only: a start is never moved, an end
    is never shortened, a caption never reaches the next one, and nothing here
    changes the approved cue times that speech synthesis is aligned to.
    """

    if not isinstance(style, CaptionStyle):
        raise CaptionError("invalid caption style")
    if isinstance(limit_ms, bool) or not isinstance(limit_ms, int) or limit_ms < 0:
        raise CaptionError("invalid caption window")
    held: list[TimelineCue] = []
    for index, cue in enumerate(cues):
        displayed = texts.get(cue.id, cue.source_text)
        characters = len(displayed.replace("\n", ""))
        readable_ms = int(
            characters * 1000 / style.target_reading_speed + 0.5
        )
        wanted = max(
            cue.end_ms + style.lead_out_ms,
            cue.start_ms + style.min_duration_ms,
            cue.start_ms + readable_ms,
        )
        following = cues[index + 1].start_ms if index + 1 < len(cues) else limit_ms
        # An already overlapping or touching pair keeps whatever it had; the
        # ceiling can never pull an end backwards.
        ceiling = max(cue.end_ms, following - style.min_gap_ms)
        end_ms = max(cue.end_ms, min(wanted, ceiling, max(cue.end_ms, limit_ms)))
        if end_ms == cue.end_ms:
            held.append(cue)
            continue
        held.append(
            TimelineCue(
                id=cue.id,
                order=cue.order,
                start_ms=cue.start_ms,
                end_ms=end_ms,
                source_text=cue.source_text,
                source_language=cue.source_language,
                speaker_id=cue.speaker_id,
            )
        )
    return tuple(held)


@dataclass(frozen=True, slots=True)
class CaptionIssue:
    """One readability or style observation about a single caption."""

    cue_id: str
    order: int
    code: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "cue_id": self.cue_id,
            "order": self.order,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CaptionReport:
    """What the checks observed about one caption file.

    These are observations, never a verdict on translation accuracy, and they do
    not stop a render: a caption that reads fast or keeps an unusual mark is
    still written, and the report says so.
    """

    cue_count: int
    language: str
    issues: tuple[CaptionIssue, ...] = ()

    def to_dict(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.code] = counts.get(issue.code, 0) + 1
        return {
            "cue_count": self.cue_count,
            "language": self.language,
            "issue_count": len(self.issues),
            "issue_counts": dict(sorted(counts.items())),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def check_captions(
    cues: tuple[TimelineCue, ...],
    texts: dict[str, str],
    style: CaptionStyle,
    *,
    language: str,
) -> CaptionReport:
    """Check the displayed captions for readability and on-screen style."""

    if not isinstance(style, CaptionStyle):
        raise CaptionError("invalid caption style")
    issues: list[CaptionIssue] = []
    previous_end_ms: int | None = None

    def record(cue: TimelineCue, code: str, detail: str) -> None:
        if len(issues) < MAX_CAPTION_ISSUES:
            issues.append(
                CaptionIssue(cue_id=cue.id, order=cue.order, code=code, detail=detail)
            )

    for cue in cues:
        if previous_end_ms is not None and cue.start_ms < previous_end_ms:
            record(
                cue,
                "caption_overlaps_previous",
                f"starts {previous_end_ms - cue.start_ms} ms before the previous caption ends",
            )
        previous_end_ms = cue.end_ms
        displayed = texts.get(cue.id, cue.source_text)
        if not displayed.strip():
            record(cue, "caption_empty", "caption has no text to display")
            continue
        lines = displayed.split("\n")
        longest = max(len(line) for line in lines)
        if longest > style.max_line_chars:
            record(
                cue,
                "caption_line_too_long",
                f"line is {longest} characters after wrapping, over {style.max_line_chars}",
            )
        duration_ms = cue.end_ms - cue.start_ms
        characters = len(displayed.replace("\n", ""))
        speed = characters * 1000 / duration_ms
        if speed > style.max_reading_speed:
            record(
                cue,
                "caption_reading_speed_high",
                f"{speed:.1f} characters per second, over {style.max_reading_speed:g}",
            )
        stray = stray_caption_punctuation(displayed)
        if stray:
            record(
                cue,
                "caption_punctuation_not_allowed",
                f"captions use no {stray}",
            )
        commas = max(line.count(CAPTION_COMMA) for line in lines)
        if commas > 1:
            record(
                cue,
                "caption_multiple_commas",
                f"{commas} commas on one caption line",
            )
    return CaptionReport(
        cue_count=len(cues), language=language, issues=tuple(issues)
    )


def serialize_caption_report(report: CaptionReport) -> bytes:
    """Serialize one report deterministically."""

    if not isinstance(report, CaptionReport):
        raise CaptionError("invalid caption report")
    payload = json.dumps(
        report.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    return (payload + "\n").encode("utf-8")


def _ass_escape(value: str) -> str:
    # Fullwidth punctuation prevents caption text from becoming an ASS override.
    value = value.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\n", "\\N")


def _ass_time(milliseconds: int) -> str:
    centiseconds = max(0, (int(milliseconds) + 5) // 10)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, hundredths = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def serialize_ass(
    cues: tuple[TimelineCue, ...],
    texts: dict[str, str],
    style: CaptionStyle,
) -> bytes:
    """Serialize approved cues as one styled Advanced SubStation Alpha file."""

    if not isinstance(style, CaptionStyle):
        raise CaptionError("invalid caption style")
    rows = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {style.play_res_x}",
        f"PlayResY: {style.play_res_y}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        _ASS_HEADER_FORMAT,
        (
            f"Style: Default,{style.font_name},{style.font_size},"
            "&H00FFFFFF,&H000000FF,&H00111111,&H99000000,0,0,0,0,100,100,0,0,1,2,1,2,"
            f"{style.margin_horizontal},{style.margin_horizontal},{style.margin_vertical},1"
        ),
        "",
        "[Events]",
        _ASS_EVENT_FORMAT,
    ]
    previous_start_ms = -1
    for cue in cues:
        if cue.start_ms < previous_start_ms:
            raise CaptionError("captions are out of order")
        previous_start_ms = cue.start_ms
        text = texts.get(cue.id, cue.source_text)
        if not text.strip() or len(text) > MAX_CUE_TEXT:
            raise CaptionError("caption text is missing or invalid")
        rows.append(
            f"Dialogue: 0,{_ass_time(cue.start_ms)},{_ass_time(cue.end_ms)},"
            f"Default,,0,0,0,,{_ass_escape(text)}"
        )
    return ("\n".join(rows) + "\n").encode("utf-8")

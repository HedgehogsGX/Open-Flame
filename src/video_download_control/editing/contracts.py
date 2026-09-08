"""Public contracts for deterministic, non-destructive media editing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Mapping, Protocol, Sequence

from .ai_authorization import AiOperationAuthorization, parse_operation_authorization


class EditingError(ValueError):
    """A stable public error code that never contains a filesystem path."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SegmentSpec:
    start_ms: int
    end_ms: int
    label: str


@dataclass(frozen=True)
class CoverSpec:
    timestamp_ms: int
    aspect_ratio: str = "source"
    title: str = ""
    subtitle: str = ""


@dataclass(frozen=True)
class TranslationSpec:
    enabled: bool = False
    source_language: str = "auto"
    target_language: str = ""
    provider: str = ""
    model: str = ""
    state: str = "disabled"


@dataclass(frozen=True)
class DubbingSpec:
    enabled: bool = False
    language: str = ""
    provider: str = ""
    model: str = ""
    voice: str = ""
    state: str = "disabled"
    replace_original_audio: bool = False
    authorization: AiOperationAuthorization | None = None


@dataclass(frozen=True)
class EditRecipe:
    segments: tuple[SegmentSpec, ...] = ()
    cover: CoverSpec | None = None
    translation: TranslationSpec = TranslationSpec()
    dubbing: DubbingSpec = DubbingSpec()

    def to_dict(self) -> dict[str, Any]:
        dubbing: dict[str, Any] = {
            "enabled": self.dubbing.enabled,
            "language": self.dubbing.language,
            "provider": self.dubbing.provider,
            "model": self.dubbing.model,
            "voice": self.dubbing.voice,
            "state": self.dubbing.state,
            "replace_original_audio": self.dubbing.replace_original_audio,
        }
        if self.dubbing.authorization is not None:
            dubbing["authorization"] = self.dubbing.authorization.to_dict()
        return {
            "segments": [
                {"start_ms": item.start_ms, "end_ms": item.end_ms, "label": item.label}
                for item in self.segments
            ],
            "cover": None
            if self.cover is None
            else {
                "timestamp_ms": self.cover.timestamp_ms,
                "aspect_ratio": self.cover.aspect_ratio,
                "title": self.cover.title,
                "subtitle": self.cover.subtitle,
            },
            "translation": {
                "enabled": self.translation.enabled,
                "source_language": self.translation.source_language,
                "target_language": self.translation.target_language,
                "provider": self.translation.provider,
                "model": self.translation.model,
                "state": self.translation.state,
            },
            "dubbing": dubbing,
        }


@dataclass(frozen=True)
class RenderAsset:
    kind: str
    path: Path
    name: str
    mime_type: str
    ordinal: int = 0
    size_bytes: int = 0
    sha256: str = ""
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    container: str = ""
    video_codec: str | None = None
    audio_codec: str | None = None


@dataclass(frozen=True)
class RenderResult:
    status: str
    code: str
    assets: tuple[RenderAsset, ...] = ()


class MediaProcessor(Protocol):
    def render(
        self,
        source: Path,
        output_dir: Path,
        recipe: EditRecipe,
        *,
        cancel_event: Event | None = None,
        expected_source_size: int | None = None,
        expected_source_sha256: str | None = None,
    ) -> RenderResult: ...


def recipe_from_mapping(value: EditRecipe | Mapping[str, Any]) -> EditRecipe:
    """Parse the small recipe surface and reject unknown or weakly typed values."""

    if isinstance(value, EditRecipe):
        return value
    if not isinstance(value, Mapping):
        raise EditingError("invalid_recipe")
    _exact_keys(value, {"segments", "cover", "translation", "dubbing"})

    raw_segments = value.get("segments", ())
    if (not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes))
            or len(raw_segments) > 100):
        raise EditingError("invalid_segments")
    segments: list[SegmentSpec] = []
    previous_end = 0
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, Mapping):
            raise EditingError("invalid_segments")
        _exact_keys(raw, {"start_ms", "end_ms", "label"})
        start = _integer(raw.get("start_ms"), 0, 7 * 24 * 60 * 60 * 1000)
        end = _integer(raw.get("end_ms"), 1, 7 * 24 * 60 * 60 * 1000)
        label = _text(raw.get("label", f"segment-{index + 1}"), 120, required=True)
        if end <= start or end - start < 100 or start < previous_end:
            raise EditingError("invalid_segments")
        segments.append(SegmentSpec(start, end, label))
        previous_end = end

    raw_cover = value.get("cover")
    cover = None
    if raw_cover is not None:
        if not isinstance(raw_cover, Mapping):
            raise EditingError("invalid_cover")
        _exact_keys(raw_cover, {"timestamp_ms", "aspect_ratio", "title", "subtitle"})
        aspect = raw_cover.get("aspect_ratio", "source")
        if aspect not in {"source", "16:9", "4:3", "3:4", "9:16", "1:1"}:
            raise EditingError("invalid_cover")
        cover = CoverSpec(
            timestamp_ms=_integer(raw_cover.get("timestamp_ms"), 0, 7 * 24 * 60 * 60 * 1000),
            aspect_ratio=aspect,
            title=_text(raw_cover.get("title", ""), 120),
            subtitle=_text(raw_cover.get("subtitle", ""), 240),
        )

    translation = _translation(value.get("translation", {}))
    dubbing = _dubbing(value.get("dubbing", {}))
    if not segments and cover is None and not translation.enabled and not dubbing.enabled:
        raise EditingError("empty_recipe")
    return EditRecipe(tuple(segments), cover, translation, dubbing)


def _translation(value: object) -> TranslationSpec:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise EditingError("invalid_translation")
    _exact_keys(value, {"enabled", "source_language", "target_language", "provider", "model", "state"})
    enabled = _boolean(value.get("enabled", False))
    state = value.get("state", "needs_review" if enabled else "disabled")
    if state not in {"disabled", "needs_review", "ready", "blocked"}:
        raise EditingError("invalid_translation")
    result = TranslationSpec(
        enabled=enabled,
        source_language=_token(value.get("source_language", "auto"), 32),
        target_language=_token(value.get("target_language", ""), 32, required=enabled),
        provider=_token(value.get("provider", ""), 64, required=enabled),
        model=_token(value.get("model", ""), 120),
        state=state,
    )
    if enabled == (state == "disabled"):
        raise EditingError("invalid_translation")
    return result


def _dubbing(value: object) -> DubbingSpec:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise EditingError("invalid_dubbing")
    _exact_keys(value, {"enabled", "language", "provider", "model", "voice", "state", "replace_original_audio", "authorization"})
    enabled = _boolean(value.get("enabled", False))
    state = value.get("state", "needs_review" if enabled else "disabled")
    if state not in {"disabled", "needs_review", "ready", "blocked"}:
        raise EditingError("invalid_dubbing")
    raw_authorization = value.get("authorization")
    authorization: AiOperationAuthorization | None = None
    if raw_authorization is not None:
        try:
            authorization = parse_operation_authorization(raw_authorization)
        except (TypeError, ValueError):
            raise EditingError("invalid_dubbing") from None
    result = DubbingSpec(
        enabled=enabled,
        language=_token(value.get("language", ""), 32, required=enabled),
        provider=_token(value.get("provider", ""), 64, required=enabled),
        model=_token(value.get("model", ""), 120),
        voice=_token(value.get("voice", ""), 120, required=enabled),
        state=state,
        replace_original_audio=_boolean(value.get("replace_original_audio", False)),
        authorization=authorization,
    )
    if (
        enabled == (state == "disabled")
        or authorization is not None
        and (
            not enabled
            or authorization.operation != "synthesize"
            or authorization.provider_id != result.provider
            or authorization.model_id != result.model
        )
    ):
        raise EditingError("invalid_dubbing")
    return result


def _exact_keys(value: Mapping[str, Any], allowed: set[str]) -> None:
    if any(not isinstance(key, str) for key in value) or set(value) - allowed:
        raise EditingError("invalid_recipe")


def _integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise EditingError("invalid_recipe")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise EditingError("invalid_recipe")
    return value


def _text(value: object, maximum: int, *, required: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(character) < 32 and character not in "\n\t\r" for character in value)):
        raise EditingError("invalid_recipe")
    result = value.strip()
    if required and not result:
        raise EditingError("invalid_recipe")
    return result


def _token(value: object, maximum: int, *, required: bool = False) -> str:
    result = _text(value, maximum, required=required)
    if result and any(not (character.isalnum() or character in "-_.:") for character in result):
        raise EditingError("invalid_recipe")
    return result

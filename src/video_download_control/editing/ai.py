"""Provider-neutral contracts for transcription, translation and speech.

The core application deliberately ships without a networked AI provider.  An
isolated AI runtime can implement these protocols later without changing edit
recipes, timeline revisions, or the upload confirmation boundary.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .timeline import TimelineCue

CapabilityStatus = Literal["ready", "blocked", "unverified", "unsupported"]
ExecutionLocation = Literal["local", "remote"]
_BCP47 = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    operation: Literal["segment", "cover", "transcribe", "translate", "dub"]
    label: str
    status: CapabilityStatus
    execution: ExecutionLocation
    description: str
    requirements: tuple[str, ...] = ()
    provider_id: str | None = None
    model_id: str | None = None
    data_egress: tuple[str, ...] = ()
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "ready" and self.requirements:
            raise ValueError("a ready capability cannot have unmet requirements")
        if self.execution == "remote" and not self.provider_id:
            raise ValueError("remote capabilities require a provider id")
        if self.execution == "local" and self.data_egress:
            raise ValueError("local capabilities cannot declare data egress")

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "label": self.label,
            "status": self.status,
            "execution": self.execution,
            "description": self.description,
            "requirements": list(self.requirements),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "data_egress": list(self.data_egress),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class TranscriptionOptions:
    language: str | None = None
    word_timestamps: bool = True
    vad: bool = True

    def __post_init__(self) -> None:
        if self.language is not None and not _BCP47.fullmatch(self.language):
            raise ValueError("invalid transcription language")


@dataclass(frozen=True, slots=True)
class TranslationItem:
    segment_id: str
    target_text: str

    def __post_init__(self) -> None:
        if not self.segment_id or len(self.segment_id) > 64:
            raise ValueError("invalid translation segment id")
        if not self.target_text.strip() or len(self.target_text) > 8_000:
            raise ValueError("invalid translated text")
        if any(ord(character) < 32 and character not in "\n\t" for character in self.target_text):
            raise ValueError("invalid translated text")


@dataclass(frozen=True, slots=True)
class TranslationRevision:
    source_language: str
    target_language: str
    provider_id: str
    model_id: str
    items: tuple[TranslationItem, ...]

    def __post_init__(self) -> None:
        if not _BCP47.fullmatch(self.source_language) or not _BCP47.fullmatch(
            self.target_language
        ):
            raise ValueError("invalid translation language")
        ids = [item.segment_id for item in self.items]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("translation ids must be non-empty and unique")

    def validate_against(self, cues: Sequence[TimelineCue]) -> None:
        expected = [cue.id for cue in cues]
        received = [item.segment_id for item in self.items]
        if received != expected:
            raise ValueError("translation must preserve cue ids and order")


@dataclass(frozen=True, slots=True)
class Voice:
    id: str
    label: str
    languages: tuple[str, ...]
    is_clone: bool = False

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 120 or not self.label or len(self.label) > 120:
            raise ValueError("invalid voice")
        if not self.languages or any(not _BCP47.fullmatch(item) for item in self.languages):
            raise ValueError("invalid voice languages")


@dataclass(frozen=True, slots=True)
class SpeechOptions:
    voice_id: str
    language: str
    rate: float = 1.0
    style: str | None = None

    def __post_init__(self) -> None:
        if not self.voice_id or len(self.voice_id) > 120:
            raise ValueError("invalid voice id")
        if not _BCP47.fullmatch(self.language):
            raise ValueError("invalid speech language")
        if not 0.88 <= self.rate <= 1.12:
            raise ValueError("speech rate must remain within the timing policy")
        if self.style is not None and len(self.style) > 500:
            raise ValueError("speech style is too long")


@dataclass(frozen=True, slots=True)
class SpeechClip:
    path: Path
    duration_ms: int
    sample_rate: int
    channels: int
    provider_id: str
    model_id: str

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or self.duration_ms <= 0:
            raise ValueError("invalid speech clip")
        if self.sample_rate not in {24_000, 44_100, 48_000} or self.channels not in {1, 2}:
            raise ValueError("unsupported speech audio format")


ProgressCallback = Callable[[float, str], None]
CancelCallback = Callable[[], bool]


@runtime_checkable
class TranscriptionProvider(Protocol):
    def capability(self) -> ProviderCapability: ...

    def transcribe(
        self,
        media: Path,
        options: TranscriptionOptions,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> tuple[TimelineCue, ...]: ...


@runtime_checkable
class TranslationProvider(Protocol):
    def capability(self) -> ProviderCapability: ...

    def translate(
        self,
        cues: Sequence[TimelineCue],
        *,
        source_language: str,
        target_language: str,
        glossary: Sequence[tuple[str, str]],
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> TranslationRevision: ...


@runtime_checkable
class SpeechProvider(Protocol):
    def capability(self) -> ProviderCapability: ...

    def voices(self, language: str) -> tuple[Voice, ...]: ...

    def synthesize(
        self,
        text: str,
        output: Path,
        options: SpeechOptions,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> SpeechClip: ...


def default_capabilities(*, media_ready: bool) -> tuple[ProviderCapability, ...]:
    """Return honest local capability state without probing or installing models."""
    local_status: CapabilityStatus = "ready" if media_ready else "blocked"
    local_reason = None if media_ready else "verified_ffmpeg_required"
    local_requirements = () if media_ready else ("verified_ffmpeg",)
    return (
        ProviderCapability(
            operation="segment",
            label="视频分段",
            status=local_status,
            execution="local",
            description="按多个起止时间生成独立视频派生文件。",
            requirements=local_requirements,
            reason_code=local_reason,
        ),
        ProviderCapability(
            operation="cover",
            label="封面制作",
            status=local_status,
            execution="local",
            description="本地抽帧、居中裁切、缩放并可选叠加标题。",
            requirements=local_requirements,
            reason_code=local_reason,
        ),
        ProviderCapability(
            operation="transcribe",
            label="自动听写",
            status="blocked",
            execution="local",
            description="计划使用隔离的 faster-whisper 运行时生成时间轴。",
            requirements=("ai_runtime", "model"),
            provider_id="local-faster-whisper",
            model_id=None,
            reason_code="ai_runtime_not_installed",
        ),
        ProviderCapability(
            operation="translate",
            label="自动翻译",
            status="blocked",
            execution="local",
            description="首批计划用固定版本本地模型验收中文与 English 互译。",
            requirements=("ai_runtime", "model"),
            provider_id="local-m2m100",
            model_id="facebook/m2m100_418M",
            reason_code="ai_runtime_not_installed",
        ),
        ProviderCapability(
            operation="dub",
            label="自动 AI 配音",
            status="blocked",
            execution="local",
            description="首批只使用标准音色；声音克隆保持禁用。",
            requirements=("ai_runtime", "model", "voice_review"),
            provider_id="local-kokoro",
            model_id="Kokoro-82M",
            reason_code="ai_runtime_not_installed",
        ),
    )

"""Composition of approved AI timelines with deterministic media rendering.

The AI provider only creates bounded per-cue PCM WAV files.  This module
validates those files, streams them into one timeline-aligned PCM track, and
hands the track to :class:`MediaProcessor` for the only FFmpeg operation.
Original media is never modified.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from ..subprocess_runner import CommandCancelled
from .ai import (
    ProviderCapability,
    SpeechClip,
    SpeechOptions,
    SpeechProvider,
    Voice,
)
from .ai_authorization import (
    AiAuthorizationError,
    enforce_synthesis_budget,
    require_authorization_match,
)
from .ai_bridge import AiBridgeError
from .ai_pipeline import AiPipelineError, canonical_timeline
from .contracts import (
    DubbingSpec,
    EditRecipe,
    EditingError,
    RenderAsset,
    RenderResult,
    TranslationSpec,
    recipe_from_mapping,
)
from .media import (
    EDITING_RESERVE_BYTES,
    MAX_OUTPUT_BYTES,
    SEGMENT_DURATION_TOLERANCE_MS,
    MediaProcessor,
)
from .timeline import TimelineCue, TimelineError, serialize_webvtt


MAX_CAPTION_BYTES = 8 * 1024 * 1024
MAX_CUE_WAV_BYTES = 64 * 1024 * 1024
MAX_SYNTHESIZED_BYTES = 512 * 1024 * 1024
MAX_PCM_TRACK_BYTES = 2 * 1024 * 1024 * 1024
MAX_CUE_AUDIO_DURATION_MS = 5 * 60 * 1000
MAX_DUBBING_DURATION_MS = 24 * 60 * 60 * 1000
_STREAM_BYTES = 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_SPEECH_CHECKPOINT_NAME = re.compile(
    r"^cue-([0-9]{6})-([0-9a-f]{64})-([0-9a-f]{64})\.wav$"
)
_SPEECH_CHECKPOINT_PART = re.compile(
    r"^(cue-[0-9]{6}-[0-9a-f]{64}-[0-9a-f]{64}\.wav)\."
    r"[a-z0-9_]{8}\.part$"
)

ProgressCallback = Callable[[float, str], None]
SpeechCheckpointLookup = Callable[[int, str, str, str, str], tuple[str, ...]]
SpeechCheckpointRecord = Callable[[int, str, str, str, str, str], None]


@dataclass(frozen=True, slots=True)
class SpeechCheckpointBinding:
    """Narrow service callbacks plus one retry-lineage cache directory."""

    directory: Path
    lookup: SpeechCheckpointLookup
    record: SpeechCheckpointRecord

    def __post_init__(self) -> None:
        if not isinstance(self.directory, Path):
            raise TypeError("speech checkpoint directory must be a Path")
        if not callable(self.lookup) or not callable(self.record):
            raise TypeError("speech checkpoint callbacks must be callable")


@dataclass(frozen=True, slots=True)
class _FileSignature:
    device: int
    inode: int
    size: int
    modified_ns: int
    attributes: int


@dataclass(frozen=True, slots=True)
class _WaveInfo:
    path: Path
    signature: _FileSignature
    size: int
    frames: int
    sample_rate: int
    channels: int
    sample_width: int
    duration_ms: int
    sha256: str

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width


def _signature(info: os.stat_result) -> _FileSignature:
    return _FileSignature(
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
        attributes=getattr(info, "st_file_attributes", 0),
    )


def _plain_directory(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or value.is_symlink():
        raise EditingError("invalid_output_directory")
    try:
        path = value.resolve(strict=True)
        info = value.lstat()
    except OSError as exc:
        raise EditingError("invalid_output_directory") from exc
    if (
        path != value
        or not stat.S_ISDIR(info.st_mode)
        or (_REPARSE_POINT and info.st_file_attributes & _REPARSE_POINT)
    ):
        raise EditingError("invalid_output_directory")
    return path


def _plain_file(path: Path, maximum: int, code: str) -> _FileSignature:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise EditingError(code)
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError as exc:
        raise EditingError(code) from exc
    if (
        resolved != path
        or not stat.S_ISREG(info.st_mode)
        or (_REPARSE_POINT and info.st_file_attributes & _REPARSE_POINT)
        or info.st_nlink != 1
        or not 0 < info.st_size <= maximum
    ):
        raise EditingError(code)
    return _signature(info)


def _assert_signature(path: Path, expected: _FileSignature, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EditingError(code) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or (_REPARSE_POINT and info.st_file_attributes & _REPARSE_POINT)
        or info.st_nlink != 1
        or _signature(info) != expected
    ):
        raise EditingError(code)


def _file_snapshot(
    path: Path, maximum: int, code: str
) -> tuple[io.BytesIO, _FileSignature, str]:
    """Copy a bounded ordinary file into memory while hashing the same bytes."""

    signature = _plain_file(path, maximum, code)
    digest = hashlib.sha256()
    snapshot = io.BytesIO()
    total = 0
    try:
        with path.open("rb") as handle:
            if _signature(os.fstat(handle.fileno())) != signature:
                raise EditingError(code)
            while chunk := handle.read(_STREAM_BYTES):
                total += len(chunk)
                if total > maximum:
                    raise EditingError(code)
                digest.update(chunk)
                snapshot.write(chunk)
            finished = _signature(os.fstat(handle.fileno()))
    except EditingError:
        snapshot.close()
        raise
    except (MemoryError, OSError) as exc:
        snapshot.close()
        raise EditingError(code) from exc
    if total != signature.size or finished != signature:
        snapshot.close()
        raise EditingError(code)
    try:
        _assert_signature(path, signature, code)
    except BaseException:
        snapshot.close()
        raise
    snapshot.seek(0)
    return snapshot, signature, digest.hexdigest()


def _verified_wave_file(path: Path, cue: TimelineCue, code: str) -> _WaveInfo:
    """Validate WAV metadata and PCM against one immutable byte snapshot."""

    snapshot, signature, digest = _file_snapshot(
        path, MAX_CUE_WAV_BYTES, code
    )
    try:
        with wave.open(snapshot, "rb") as stream:
            channels = stream.getnchannels()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            sample_width = stream.getsampwidth()
            compression = stream.getcomptype()
            if (
                channels not in {1, 2}
                or sample_rate not in {24_000, 44_100, 48_000}
                or frames <= 0
                or sample_width not in {2, 3, 4}
                or compression != "NONE"
            ):
                raise EditingError(code)
            frame_bytes = channels * sample_width
            if frames > signature.size // frame_bytes:
                raise EditingError(code)
            remaining_frames = frames
            while remaining_frames:
                requested = min(remaining_frames, 65_536)
                payload = stream.readframes(requested)
                if not payload or len(payload) % frame_bytes:
                    raise EditingError(code)
                received = len(payload) // frame_bytes
                if received > requested:
                    raise EditingError(code)
                remaining_frames -= received
    except EditingError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise EditingError(code) from exc
    finally:
        snapshot.close()
    duration_ms = max(1, round(frames * 1000 / sample_rate))
    cue_duration = cue.end_ms - cue.start_ms
    allowed_duration = min(
        MAX_CUE_AUDIO_DURATION_MS,
        max(15_000, cue_duration * 4),
    )
    if duration_ms > allowed_duration:
        raise EditingError(code)
    return _WaveInfo(
        path=path,
        signature=signature,
        size=signature.size,
        frames=frames,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        duration_ms=duration_ms,
        sha256=digest,
    )


def _inspect_wave_file(path: Path, cue: TimelineCue, code: str) -> _WaveInfo:
    return _verified_wave_file(path, cue, code)


def _inspect_hashed_wave_file(
    path: Path, cue: TimelineCue, code: str
) -> tuple[_WaveInfo, str]:
    info = _verified_wave_file(path, cue, code)
    return info, info.sha256


def _inspect_wave(path: Path, clip: SpeechClip, cue: TimelineCue) -> _WaveInfo:
    info = _inspect_wave_file(path, cue, "ai_speech_output_invalid")
    if (
        clip.path != path
        or clip.duration_ms != info.duration_ms
        or clip.sample_rate != info.sample_rate
        or clip.channels != info.channels
    ):
        raise EditingError("ai_speech_output_invalid")
    _assert_signature(path, info.signature, "ai_speech_output_changed")
    return info


def _speech_checkpoint_key(
    cue: TimelineCue,
    cue_ordinal: int,
    spec: DubbingSpec,
    authorization_sha256: str,
) -> str:
    """Bind one reusable WAV to every immutable synthesis input."""

    try:
        payload = json.dumps(
            {
                "schema_version": 1,
                "authorization_sha256": authorization_sha256,
                "provider": spec.provider,
                "model": spec.model,
                "voice": spec.voice,
                "language": spec.language,
                "rate": spec.rate,
                "cue": {
                    "id": cue.id,
                    "ordinal": cue_ordinal,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "source_text": cue.source_text,
                    "source_language": cue.source_language,
                    "speaker_id": cue.speaker_id,
                },
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise EditingError("ai_speech_checkpoint_invalid") from exc
    return hashlib.sha256(payload).hexdigest()


class _SpeechCheckpointCache:
    """Content-addressed WAV checkpoints scoped to one render retry lineage."""

    def __init__(self, binding: SpeechCheckpointBinding):
        if not isinstance(binding, SpeechCheckpointBinding):
            raise EditingError("ai_speech_checkpoint_invalid")
        self.binding = binding
        self.directory = _plain_directory(binding.directory)

    @staticmethod
    def _prefix(cue_ordinal: int, request_key: str) -> str:
        if (
            isinstance(cue_ordinal, bool)
            or not isinstance(cue_ordinal, int)
            or not 0 <= cue_ordinal <= 999_999
            or not isinstance(request_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_key) is None
        ):
            raise EditingError("ai_speech_checkpoint_invalid")
        return f"cue-{cue_ordinal:06d}-{request_key}-"

    def load(
        self,
        *,
        cue: TimelineCue,
        cue_ordinal: int,
        request_key: str,
        invocation_fingerprint: str,
        authorization_sha256: str,
        execution: str,
    ) -> _WaveInfo | None:
        prefix = self._prefix(cue_ordinal, request_key)
        if (
            re.fullmatch(r"[0-9a-f]{64}", invocation_fingerprint or "") is None
            or re.fullmatch(r"[0-9a-f]{64}", authorization_sha256 or "") is None
            or execution not in {"local", "remote"}
        ):
            raise EditingError("ai_speech_checkpoint_invalid")
        expected_digests = self.binding.lookup(
            cue_ordinal,
            request_key,
            invocation_fingerprint,
            authorization_sha256,
            execution,
        )
        if (
            not isinstance(expected_digests, tuple)
            or len(expected_digests) > 1_000
            or any(
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in expected_digests
            )
            or len(set(expected_digests)) != len(expected_digests)
        ):
            raise EditingError("ai_speech_checkpoint_invalid")
        names = self._names(prefix)
        for name in names:
            if _SPEECH_CHECKPOINT_PART.fullmatch(name) is not None:
                self._recover_or_discard_part(self.directory / name, name)

        # Recovery can reduce a target's link count from two to one.  Scan
        # again before deciding whether an ordinary target is safe to remove.
        names = self._names(prefix)
        expected = set(expected_digests)
        for name in names:
            match = _SPEECH_CHECKPOINT_NAME.fullmatch(name)
            if (
                match is not None
                and int(match.group(1)) == cue_ordinal
                and match.group(2) == request_key
                and match.group(3) in expected
            ):
                continue
            self._discard_plain_file(self.directory / name)

        for expected_digest in expected_digests:
            path = self.directory / f"{prefix}{expected_digest}.wav"
            try:
                info, digest = _inspect_hashed_wave_file(
                    path, cue, "ai_speech_checkpoint_invalid"
                )
                if info.size <= 0 or digest != expected_digest:
                    raise EditingError("ai_speech_checkpoint_invalid")
                return info
            except EditingError:
                # A registered cache entry is only an optimisation. A missing
                # or corrupt ordinary file is removed so this retry can create
                # a fresh checkpoint under its own immutable manifest row.
                self._discard_plain_file(path)
        return None

    def _names(self, prefix: str) -> tuple[str, ...]:
        try:
            _plain_directory(self.directory)
            with os.scandir(self.directory) as entries:
                return tuple(
                    sorted(entry.name for entry in entries if entry.name.startswith(prefix))
                )
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc

    def _recover_or_discard_part(self, path: Path, name: str) -> None:
        """Finish the only safe hard-link crash state, otherwise fail closed."""

        match = _SPEECH_CHECKPOINT_PART.fullmatch(name)
        if match is None:
            return
        target = self.directory / match.group(1)
        try:
            part_info = path.lstat()
            target_info = target.lstat()
        except FileNotFoundError:
            self._discard_plain_file(path)
            return
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc
        if (
            not stat.S_ISLNK(part_info.st_mode)
            and stat.S_ISREG(part_info.st_mode)
            and not (_REPARSE_POINT and part_info.st_file_attributes & _REPARSE_POINT)
            and not stat.S_ISLNK(target_info.st_mode)
            and stat.S_ISREG(target_info.st_mode)
            and not (
                _REPARSE_POINT
                and target_info.st_file_attributes & _REPARSE_POINT
            )
            and part_info.st_nlink == 2
            and target_info.st_nlink == 2
            and part_info.st_dev == target_info.st_dev
            and part_info.st_ino == target_info.st_ino
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise EditingError("editing_storage_unavailable") from exc
            return
        self._discard_plain_file(path)

    def _discard_plain_file(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or (_REPARSE_POINT and info.st_file_attributes & _REPARSE_POINT)
            or info.st_nlink != 1
        ):
            return
        try:
            if path.resolve(strict=True).parent != self.directory:
                return
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc

    def discard(self, info: _WaveInfo) -> None:
        """Remove one loaded entry that fails track-level validation."""

        if (
            not isinstance(info, _WaveInfo)
            or info.path.parent != self.directory
            or _SPEECH_CHECKPOINT_NAME.fullmatch(info.path.name) is None
        ):
            raise EditingError("ai_speech_checkpoint_invalid")
        try:
            _assert_signature(
                info.path, info.signature, "ai_speech_checkpoint_invalid"
            )
        except EditingError:
            return
        self._discard_plain_file(info.path)

    def _copy_atomic(
        self,
        info: _WaveInfo,
        target: Path,
        *,
        cue: TimelineCue,
        digest: str,
    ) -> bool:
        """Create a verified checkpoint without replacing an existing path."""

        descriptor: int | None = None
        temporary: Path | None = None
        created = False
        try:
            descriptor, raw = tempfile.mkstemp(
                prefix=f"{target.name}.",
                suffix=".part",
                dir=self.directory,
            )
            temporary = Path(raw)
            with info.path.open("rb") as source:
                if _signature(os.fstat(source.fileno())) != info.signature:
                    raise EditingError("ai_speech_output_changed")
                with os.fdopen(descriptor, "wb") as destination:
                    descriptor = None
                    source_digest = hashlib.sha256()
                    remaining = info.size
                    while remaining:
                        chunk = source.read(min(_STREAM_BYTES, remaining))
                        if not chunk:
                            raise EditingError("ai_speech_output_changed")
                        destination.write(chunk)
                        source_digest.update(chunk)
                        remaining -= len(chunk)
                    if source.read(1):
                        raise EditingError("ai_speech_output_changed")
                    destination.flush()
                    os.fsync(destination.fileno())
                    if _signature(os.fstat(source.fileno())) != info.signature:
                        raise EditingError("ai_speech_output_changed")
                    if source_digest.hexdigest() != info.sha256:
                        raise EditingError("ai_speech_output_changed")
            copied_size, copied_digest = _hash_file(
                temporary,
                MAX_CUE_WAV_BYTES,
                "ai_speech_output_changed",
            )
            if copied_size != info.size or copied_digest != digest:
                raise EditingError("ai_speech_output_changed")
            try:
                os.link(temporary, target)
                created = True
            except FileExistsError:
                created = False
        except EditingError:
            raise
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        try:
            cached, cached_digest = _inspect_hashed_wave_file(
                target, cue, "ai_speech_checkpoint_conflict"
            )
        except EditingError:
            if created:
                self._discard_plain_file(target)
            raise
        if cached.size != info.size or cached_digest != digest:
            if created:
                self._discard_plain_file(target)
            raise EditingError("ai_speech_checkpoint_conflict")
        return created

    def persist(
        self,
        info: _WaveInfo,
        *,
        cue: TimelineCue,
        cue_ordinal: int,
        request_key: str,
        invocation_fingerprint: str,
        authorization_sha256: str,
        execution: str,
    ) -> _WaveInfo:
        prefix = self._prefix(cue_ordinal, request_key)
        existing = self.load(
            cue=cue,
            cue_ordinal=cue_ordinal,
            request_key=request_key,
            invocation_fingerprint=invocation_fingerprint,
            authorization_sha256=authorization_sha256,
            execution=execution,
        )
        size, digest = info.size, info.sha256
        if existing is not None:
            if existing.size != size or existing.sha256 != digest:
                raise EditingError("ai_speech_checkpoint_conflict")
            return info
        target = self.directory / f"{prefix}{digest}.wav"
        _assert_signature(info.path, info.signature, "ai_speech_output_changed")
        created = self._copy_atomic(info, target, cue=cue, digest=digest)
        try:
            self.binding.record(
                cue_ordinal,
                request_key,
                invocation_fingerprint,
                authorization_sha256,
                execution,
                digest,
            )
            recorded = self.binding.lookup(
                cue_ordinal,
                request_key,
                invocation_fingerprint,
                authorization_sha256,
                execution,
            )
            if not recorded or recorded[0] != digest:
                raise EditingError("ai_speech_checkpoint_invalid")
        except BaseException:
            if created:
                self._discard_plain_file(target)
            raise
        return info


def _write_zeros(stream: wave.Wave_write, frames: int, frame_bytes: int) -> None:
    chunk_frames = max(1, _STREAM_BYTES // frame_bytes)
    zero_chunk = bytes(chunk_frames * frame_bytes)
    remaining = frames
    while remaining:
        count = min(remaining, chunk_frames)
        stream.writeframesraw(zero_chunk[: count * frame_bytes])
        remaining -= count


def _copy_frames(
    destination: wave.Wave_write,
    info: _WaveInfo,
    frame_limit: int,
) -> int:
    if info.frames > frame_limit:
        raise EditingError("ai_speech_timing_overflow")
    expected = info.frames
    copied = 0
    snapshot: io.BytesIO | None = None
    try:
        snapshot, signature, digest = _file_snapshot(
            info.path,
            MAX_CUE_WAV_BYTES,
            "ai_speech_output_changed",
        )
        if signature != info.signature or digest != info.sha256:
            raise EditingError("ai_speech_output_changed")
        with wave.open(snapshot, "rb") as source:
            if (
                source.getnchannels() != info.channels
                or source.getframerate() != info.sample_rate
                or source.getnframes() != info.frames
                or source.getsampwidth() != info.sample_width
                or source.getcomptype() != "NONE"
            ):
                raise EditingError("ai_speech_output_changed")
            while copied < expected:
                requested = min(expected - copied, 65_536)
                payload = source.readframes(requested)
                if not payload or len(payload) % info.frame_bytes:
                    raise EditingError("ai_speech_output_invalid")
                frames = len(payload) // info.frame_bytes
                if frames > requested:
                    raise EditingError("ai_speech_output_invalid")
                destination.writeframesraw(payload)
                copied += frames
    except EditingError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise EditingError("ai_speech_output_invalid") from exc
    finally:
        if snapshot is not None:
            snapshot.close()
    if copied != expected:
        raise EditingError("ai_speech_output_changed")
    return copied


def _window_cues(
    cues: tuple[TimelineCue, ...], start_ms: int, end_ms: int
) -> tuple[TimelineCue, ...]:
    """Return cues intersecting one output window, clamped and rebased to zero."""

    selected: list[TimelineCue] = []
    for cue in cues:
        clipped_start = max(cue.start_ms, start_ms)
        clipped_end = min(cue.end_ms, end_ms)
        if clipped_end <= clipped_start:
            continue
        if cue.start_ms < start_ms or cue.end_ms > end_ms:
            # Cue text is atomic.  Silently clipping only its timestamps would
            # move words from outside the selected media into this segment.
            raise EditingError("ai_segment_boundary_splits_cue")
        selected.append(
            TimelineCue(
                id=cue.id,
                order=len(selected),
                start_ms=cue.start_ms - start_ms,
                end_ms=cue.end_ms - start_ms,
                source_text=cue.source_text,
                source_language=cue.source_language,
                speaker_id=cue.speaker_id,
            )
        )
    return tuple(selected)


def _hash_file(path: Path, maximum: int, code: str) -> tuple[int, str]:
    signature = _plain_file(path, maximum, code)
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            if _signature(os.fstat(handle.fileno())) != signature:
                raise EditingError(code)
            while chunk := handle.read(_STREAM_BYTES):
                total += len(chunk)
                if total > maximum:
                    raise EditingError(code)
                digest.update(chunk)
            finished = _signature(os.fstat(handle.fileno()))
    except EditingError:
        raise
    except OSError as exc:
        raise EditingError(code) from exc
    if total != signature.size or finished != signature:
        raise EditingError(code)
    _assert_signature(path, signature, code)
    return total, digest.hexdigest()


def _write_exclusive(path: Path, payload: bytes) -> tuple[int, str]:
    if not payload or len(payload) > MAX_CAPTION_BYTES:
        raise EditingError("ai_caption_too_large")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise EditingError("edit_output_exists") from None
    except OSError as exc:
        raise EditingError("media_processing_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        return _hash_file(path, MAX_CAPTION_BYTES, "ai_caption_invalid")
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _remove_scratch(path: Path, parent: Path) -> None:
    """Best-effort cleanup that never follows links created by a provider."""

    try:
        candidate = Path(os.path.abspath(path))
        if candidate.parent != Path(os.path.abspath(parent)):
            return
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            _REPARSE_POINT and info.st_file_attributes & _REPARSE_POINT
        ):
            candidate.unlink(missing_ok=True)
            return
        if not stat.S_ISDIR(info.st_mode):
            candidate.unlink(missing_ok=True)
            return
        for entry in os.scandir(candidate):
            child = Path(entry.path)
            child_info = child.lstat()
            if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(
                child_info.st_mode
            ) and not (
                _REPARSE_POINT
                and child_info.st_file_attributes & _REPARSE_POINT
            ):
                _remove_tree_contents(child)
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        candidate.rmdir()
    except OSError:
        pass


def _remove_tree_contents(path: Path) -> None:
    for entry in os.scandir(path):
        child = Path(entry.path)
        try:
            info = child.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not (
                _REPARSE_POINT and info.st_file_attributes & _REPARSE_POINT
            ):
                _remove_tree_contents(child)
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        except OSError:
            continue


class AiRenderProcessor:
    """Render ordinary edits plus approved captions and standard-voice dubbing."""

    def __init__(self, media_processor: MediaProcessor) -> None:
        if not isinstance(media_processor, MediaProcessor):
            raise TypeError("media_processor must be a MediaProcessor")
        self.media_processor = media_processor

    def render(
        self,
        source: Path,
        output_dir: Path,
        recipe: EditRecipe,
        *,
        timeline: Sequence[TimelineCue] | None,
        speech_provider: SpeechProvider | None = None,
        cancel_event: Event | None = None,
        progress: ProgressCallback = lambda _fraction, _code: None,
        expected_source_size: int | None = None,
        expected_source_sha256: str | None = None,
        speech_checkpoint_binding: SpeechCheckpointBinding | None = None,
    ) -> RenderResult:
        """Render one approved AI recipe without changing the source media."""

        if cancel_event is not None and not isinstance(cancel_event, Event):
            raise TypeError("cancel_event must be a threading.Event")
        if not callable(progress):
            raise TypeError("progress must be callable")
        try:
            normalized = recipe_from_mapping(recipe.to_dict())
        except (AttributeError, EditingError) as exc:
            raise EditingError("invalid_edit_recipe") from exc
        translation = normalized.translation
        dubbing = normalized.dubbing
        if translation.enabled and translation.state != "ready":
            raise EditingError("ai_translation_not_approved")
        if dubbing.enabled and dubbing.state != "ready":
            raise EditingError("ai_dubbing_not_approved")
        if translation.enabled and dubbing.enabled and (
            translation.target_language.casefold() != dubbing.language.casefold()
        ):
            raise EditingError("ai_timeline_language_mismatch")
        if speech_checkpoint_binding is not None and not dubbing.enabled:
            raise EditingError("ai_speech_checkpoint_not_expected")

        destination = _plain_directory(output_dir)
        ordinary = EditRecipe(
            segments=normalized.segments,
            cover=normalized.cover,
            translation=TranslationSpec(),
            dubbing=DubbingSpec(),
        )
        ai_enabled = translation.enabled or dubbing.enabled
        if not ai_enabled:
            return self.media_processor.render(
                source,
                destination,
                ordinary,
                cancel_event=cancel_event,
                expected_source_size=expected_source_size,
                expected_source_sha256=expected_source_sha256,
            )

        language = (
            translation.target_language if translation.enabled else dubbing.language
        )
        if timeline is None:
            raise EditingError("ai_timeline_required")
        try:
            canonical = canonical_timeline(timeline, language=language)
        except AiPipelineError as exc:
            raise EditingError("ai_timeline_invalid") from exc
        cue_ordinals = {cue.id: cue.order for cue in canonical.cues}
        if len(cue_ordinals) != len(canonical.cues):
            raise EditingError("ai_timeline_invalid")

        output_count = len(ordinary.segments) if ordinary.segments else 1
        caption_paths = tuple(
            destination
            / (
                "caption.vtt"
                if output_count == 1
                else f"caption-{ordinal:03d}.vtt"
            )
            for ordinal in range(1, output_count + 1)
        )
        dubbed_paths = tuple(
            destination / f"dubbed-video-{ordinal:03d}.mp4"
            for ordinal in range(1, output_count + 1)
        )
        planned = (
            (caption_paths if translation.enabled else ())
            + (dubbed_paths if dubbing.enabled else ())
        )
        if any(path.exists() or path.is_symlink() for path in planned):
            raise EditingError("edit_output_exists")

        owned: list[Path] = []
        scratch: Path | None = None
        speech_checkpoints: _SpeechCheckpointCache | None = None
        authorization_sha256: str | None = None
        authorization_execution: str | None = None
        try:
            self._check_cancelled(cancel_event)
            source_probe = self.media_processor.probe(
                source,
                cancel_event=cancel_event,
                expected_source_size=expected_source_size,
                expected_source_sha256=expected_source_sha256,
            )
            if canonical.cues[-1].end_ms > source_probe.duration_ms:
                raise EditingError("ai_timeline_invalid")
            windows = (
                tuple(
                    (segment.start_ms, segment.end_ms)
                    for segment in ordinary.segments
                )
                if ordinary.segments
                else ((0, source_probe.duration_ms),)
            )
            if any(
                end_ms > source_probe.duration_ms
                or (
                    dubbing.enabled
                    and end_ms - start_ms > MAX_DUBBING_DURATION_MS
                )
                for start_ms, end_ms in windows
            ):
                raise EditingError("ai_timeline_invalid")
            windowed_cues = tuple(
                _window_cues(canonical.cues, start_ms, end_ms)
                for start_ms, end_ms in windows
            )
            if dubbing.enabled:
                # Bind the approved recipe to the authorization carried by the
                # verified provider instance, then account for every cue that
                # will cause a synthesis request. The provider rechecks the
                # current runtime before ``voices()`` can load a plugin or
                # contact a remote service.
                if speech_provider is None or not isinstance(
                    speech_provider, SpeechProvider
                ):
                    raise EditingError("ai_speech_provider_required")
                try:
                    provider_authorization = getattr(
                        speech_provider, "expected_authorization", None
                    )
                    authorization = require_authorization_match(
                        dubbing.authorization,
                        provider_authorization,
                    )
                    synthesis_cues = tuple(
                        cue for cue_group in windowed_cues for cue in cue_group
                    )
                    if (
                        len({cue.id for cue in synthesis_cues})
                        != len(synthesis_cues)
                        or any(cue.id not in cue_ordinals for cue in synthesis_cues)
                    ):
                        raise EditingError("ai_timeline_invalid")
                    enforce_synthesis_budget(
                        authorization,
                        synthesis_cues,
                    )
                    authorization_sha256 = authorization.sha256
                    authorization_execution = authorization.execution
                    if speech_checkpoint_binding is not None:
                        speech_checkpoints = _SpeechCheckpointCache(
                            speech_checkpoint_binding
                        )
                except (AiAuthorizationError, AiBridgeError) as exc:
                    raise EditingError(exc.code) from exc

            outputs: list[RenderAsset] = []
            completed_bytes = 0
            if ordinary.segments or ordinary.cover is not None:
                base = self.media_processor.render(
                    source,
                    destination,
                    ordinary,
                    cancel_event=cancel_event,
                    expected_source_size=expected_source_size,
                    expected_source_sha256=expected_source_sha256,
                )
                if base.status == "canceled":
                    return base
                if base.status != "ready":
                    raise EditingError("media_processing_failed")
                outputs.extend(base.assets)
                owned.extend(asset.path for asset in base.assets)
                completed_bytes = sum(asset.size_bytes for asset in base.assets)

            if translation.enabled:
                for ordinal, (caption_path, cues, window) in enumerate(
                    zip(caption_paths, windowed_cues, windows, strict=True), start=1
                ):
                    try:
                        caption = serialize_webvtt(cues) if cues else b"WEBVTT\n\n"
                    except (TimelineError, UnicodeError) as exc:
                        raise EditingError("ai_caption_invalid") from exc
                    size, digest = _write_exclusive(caption_path, caption)
                    owned.append(caption_path)
                    completed_bytes += size
                    if completed_bytes > MAX_OUTPUT_BYTES:
                        raise EditingError("media_output_too_large")
                    outputs.append(
                        RenderAsset(
                            kind="caption",
                            path=caption_path,
                            name=caption_path.name,
                            mime_type="text/vtt",
                            ordinal=ordinal,
                            size_bytes=size,
                            sha256=digest,
                            duration_ms=window[1] - window[0],
                            container="webvtt",
                        )
                    )

            if dubbing.enabled:
                provider, voice = self._standard_voice(speech_provider, dubbing)
                scratch = Path(
                    tempfile.mkdtemp(prefix=".ai-render-", dir=destination)
                )
                segment_assets = sorted(
                    (asset for asset in outputs if asset.kind == "segment"),
                    key=lambda asset: asset.ordinal,
                )
                if ordinary.segments and len(segment_assets) != len(ordinary.segments):
                    raise EditingError("media_processing_failed")
                bases = (
                    tuple(
                        (
                            asset.path,
                            None,
                            None,
                            windowed_cues[index],
                            (
                                ordinary.segments[index].end_ms
                                - ordinary.segments[index].start_ms
                                + SEGMENT_DURATION_TOLERANCE_MS
                            ),
                        )
                        for index, asset in enumerate(segment_assets)
                    )
                    if ordinary.segments
                    else (
                        (
                            source,
                            expected_source_size,
                            expected_source_sha256,
                            windowed_cues[0],
                            source_probe.duration_ms + SEGMENT_DURATION_TOLERANCE_MS,
                        ),
                    )
                )
                for ordinal, (base_path, size, digest, cues, duration_ms) in enumerate(
                    bases, start=1
                ):
                    self._check_cancelled(cancel_event)
                    track_dir = scratch / f"track-{ordinal:03d}"
                    track_dir.mkdir(mode=0o700)

                    def track_progress(
                        fraction: float, code: str, *, item: int = ordinal - 1
                    ) -> None:
                        self._progress(
                            progress,
                            (item + fraction) / len(bases) * 0.85,
                            code,
                        )

                    try:
                        track, track_size, track_digest = self._build_track(
                            track_dir,
                            cues,
                            cue_ordinals,
                            duration_ms,
                            provider,
                            voice,
                            dubbing,
                            cancel_event,
                            track_progress,
                            speech_checkpoints,
                            authorization_sha256,
                            authorization_execution,
                        )
                        if track_size <= 0 or len(track_digest) != 64:
                            raise EditingError("ai_audio_track_invalid")
                        rendered = self.media_processor.render_dubbed_video(
                            base_path,
                            track,
                            dubbed_paths[ordinal - 1],
                            track_offset_ms=0,
                            replace_original_audio=dubbing.replace_original_audio,
                            cancel_event=cancel_event,
                            expected_source_size=size,
                            expected_source_sha256=digest,
                        )
                    finally:
                        _remove_scratch(track_dir, scratch)
                    rendered = replace(
                        rendered,
                        ordinal=ordinal,
                        name=dubbed_paths[ordinal - 1].name,
                    )
                    owned.append(rendered.path)
                    completed_bytes += rendered.size_bytes
                    if completed_bytes > MAX_OUTPUT_BYTES:
                        raise EditingError("media_output_too_large")
                    outputs.append(rendered)

            self._check_cancelled(cancel_event)
            self.media_processor.probe(
                source,
                cancel_event=cancel_event,
                expected_source_size=expected_source_size,
                expected_source_sha256=expected_source_sha256,
            )
            self._progress(progress, 1.0, "ai_render_complete")
            return RenderResult(
                status="ready",
                code="ai_render_complete",
                assets=tuple(outputs),
            )
        except CommandCancelled:
            self._cleanup_files(owned)
            return RenderResult(status="canceled", code="canceled")
        except EditingError:
            self._cleanup_files(owned)
            raise
        except Exception as exc:
            self._cleanup_files(owned)
            if cancel_event is not None and cancel_event.is_set():
                return RenderResult(status="canceled", code="canceled")
            raise EditingError("ai_render_failed") from exc
        finally:
            if scratch is not None:
                _remove_scratch(scratch, destination)

    @staticmethod
    def _standard_voice(
        speech_provider: SpeechProvider | None,
        spec: DubbingSpec,
    ) -> tuple[SpeechProvider, Voice]:
        if speech_provider is None or not isinstance(speech_provider, SpeechProvider):
            raise EditingError("ai_speech_provider_required")
        try:
            capability = speech_provider.capability()
            raw_voices = speech_provider.voices(spec.language)
        except (AiAuthorizationError, AiBridgeError) as exc:
            raise EditingError(exc.code) from exc
        except Exception:
            raise EditingError("ai_speech_provider_unavailable") from None
        if not isinstance(raw_voices, tuple) or len(raw_voices) > 512:
            raise EditingError("ai_speech_provider_mismatch")
        voices = raw_voices
        if (
            not isinstance(capability, ProviderCapability)
            or capability.operation != "dub"
            or capability.status in {"blocked", "unsupported"}
            or capability.provider_id != spec.provider
            or (capability.model_id or "") != spec.model
            or any(not isinstance(item, Voice) for item in voices)
            or len({item.id for item in voices}) != len(voices)
        ):
            raise EditingError("ai_speech_provider_mismatch")
        matches = [
            item
            for item in voices
            if item.id == spec.voice
            and spec.language.casefold()
            in {language.casefold() for language in item.languages}
        ]
        if len(matches) != 1 or matches[0].is_clone:
            raise EditingError("ai_voice_not_allowed")
        return speech_provider, matches[0]

    def _build_track(
        self,
        scratch: Path,
        cues: tuple[TimelineCue, ...],
        cue_ordinals: dict[str, int],
        duration_ms: int,
        provider: SpeechProvider,
        voice: Voice,
        spec: DubbingSpec,
        cancel_event: Event | None,
        progress: ProgressCallback,
        speech_checkpoints: _SpeechCheckpointCache | None = None,
        authorization_sha256: str | None = None,
        authorization_execution: str | None = None,
    ) -> tuple[Path, int, str]:
        clips: list[_WaveInfo] = []
        total_bytes = 0
        # A silent/B-roll segment remains a valid output.  Use a small,
        # deterministic PCM format without calling the speech provider.
        expected_format: tuple[int, int, int] | None = (
            (24_000, 1, 2) if not cues else None
        )
        for index, cue in enumerate(cues):
            self._check_cancelled(cancel_event)
            try:
                cue_ordinal = cue_ordinals[cue.id]
            except KeyError:
                raise EditingError("ai_timeline_invalid") from None
            if (
                isinstance(cue_ordinal, bool)
                or not isinstance(cue_ordinal, int)
                or cue_ordinal < 0
            ):
                raise EditingError("ai_timeline_invalid")
            path = scratch / f"cue-{index + 1:05d}.wav"
            request_key = None
            invocation_fingerprint = None
            info = None
            options = SpeechOptions(
                voice_id=voice.id,
                language=spec.language,
                rate=spec.rate,
                style=None,
            )
            if speech_checkpoints is not None:
                if (
                    not isinstance(authorization_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", authorization_sha256) is None
                    or authorization_execution not in {"local", "remote"}
                ):
                    raise EditingError("ai_speech_checkpoint_invalid")
                request_key = _speech_checkpoint_key(
                    cue,
                    cue_ordinal,
                    spec,
                    authorization_sha256,
                )
                fingerprint = getattr(provider, "request_fingerprint", None)
                if not callable(fingerprint):
                    raise EditingError("ai_speech_checkpoint_invalid")
                try:
                    invocation_fingerprint = fingerprint(cue.source_text, options)
                except (AiAuthorizationError, AiBridgeError) as exc:
                    raise EditingError(exc.code) from exc
                except Exception as exc:
                    raise EditingError("ai_speech_checkpoint_invalid") from exc
                info = speech_checkpoints.load(
                    cue=cue,
                    cue_ordinal=cue_ordinal,
                    request_key=request_key,
                    invocation_fingerprint=invocation_fingerprint,
                    authorization_sha256=authorization_sha256,
                    execution=authorization_execution,
                )
            progress_calls = 0
            last_progress = 0.0

            def cue_progress(fraction: float, _code: str, *, item: int = index) -> None:
                nonlocal progress_calls, last_progress
                if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
                    raise EditingError("ai_progress_invalid")
                value = float(fraction)
                if (
                    not 0.0 <= value <= 1.0
                    or value < last_progress
                    or progress_calls >= 256
                ):
                    raise EditingError("ai_progress_invalid")
                progress_calls += 1
                last_progress = value
                self._progress(
                    progress,
                    (item + value) / len(cues) * 0.75,
                    "ai_speech_rendering",
                )

            reused = info is not None
            while True:
                if info is None:
                    try:
                        cancelled = (
                            (lambda: False)
                            if cancel_event is None
                            else cancel_event.is_set
                        )
                        synthesize_ledgered = getattr(
                            provider, "synthesize_ledgered", None
                        )
                        if callable(synthesize_ledgered):
                            clip = synthesize_ledgered(
                                cue.source_text,
                                path,
                                options,
                                ordinal=cue_ordinal,
                                progress=cue_progress,
                                cancelled=cancelled,
                            )
                        else:
                            clip = provider.synthesize(
                                cue.source_text,
                                path,
                                options,
                                progress=cue_progress,
                                cancelled=cancelled,
                            )
                    except EditingError:
                        raise
                    except (AiAuthorizationError, AiBridgeError) as exc:
                        raise EditingError(exc.code) from exc
                    except Exception:
                        if cancel_event is not None and cancel_event.is_set():
                            raise CommandCancelled(
                                "AI speech rendering was cancelled"
                            ) from None
                        raise EditingError("ai_speech_failed") from None
                    if not isinstance(clip, SpeechClip):
                        raise EditingError("ai_speech_output_invalid")
                    if clip.provider_id != spec.provider or clip.model_id != spec.model:
                        raise EditingError("ai_speech_provider_mismatch")
                    info = _inspect_wave(path, clip, cue)

                current_format = (
                    info.sample_rate,
                    info.channels,
                    info.sample_width,
                )
                rejection = ""
                if expected_format is None:
                    total_frames = (duration_ms * info.sample_rate + 999) // 1000
                    planned_size = 44 + total_frames * info.frame_bytes
                    if planned_size > MAX_PCM_TRACK_BYTES:
                        rejection = "ai_audio_track_too_large"
                elif current_format != expected_format:
                    rejection = "ai_speech_format_changed"
                if not rejection and total_bytes + info.size > MAX_SYNTHESIZED_BYTES:
                    rejection = "ai_speech_output_too_large"
                next_start = (
                    cues[index + 1].start_ms
                    if index + 1 < len(cues)
                    else cue.end_ms
                )
                start_frame = cue.start_ms * info.sample_rate // 1000
                slot_end_ms = min(cue.end_ms, next_start, duration_ms)
                slot_end_frame = slot_end_ms * info.sample_rate // 1000
                if (
                    not rejection
                    and info.frames > max(0, slot_end_frame - start_frame)
                ):
                    rejection = "ai_speech_timing_overflow"
                if rejection:
                    if reused and speech_checkpoints is not None:
                        speech_checkpoints.discard(info)
                        info = None
                        reused = False
                        continue
                    raise EditingError(rejection)
                break

            if reused:
                self._progress(
                    progress,
                    (index + 1) / len(cues) * 0.75,
                    "ai_speech_checkpoint_reused",
                )
            elif speech_checkpoints is not None:
                if request_key is None or invocation_fingerprint is None:
                    raise EditingError("ai_speech_checkpoint_invalid")
                info = speech_checkpoints.persist(
                    info,
                    cue=cue,
                    cue_ordinal=cue_ordinal,
                    request_key=request_key,
                    invocation_fingerprint=invocation_fingerprint,
                    authorization_sha256=authorization_sha256,
                    execution=authorization_execution,
                )
            if expected_format is None:
                expected_format = current_format
                try:
                    free = shutil.disk_usage(scratch).free
                except OSError as exc:
                    raise EditingError("editing_storage_unavailable") from exc
                if free < planned_size + EDITING_RESERVE_BYTES:
                    raise EditingError("editing_storage_full")
            total_bytes += info.size
            clips.append(info)

        assert expected_format is not None
        sample_rate, channels, sample_width = expected_format
        total_frames = (duration_ms * sample_rate + 999) // 1000
        planned_size = 44 + total_frames * channels * sample_width
        try:
            free = shutil.disk_usage(scratch).free
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc
        if free < planned_size + EDITING_RESERVE_BYTES:
            raise EditingError("editing_storage_full")
        track = scratch / "timeline.wav"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                track,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                with wave.open(handle, "wb") as destination:
                    destination.setnchannels(channels)
                    destination.setsampwidth(sample_width)
                    destination.setframerate(sample_rate)
                    current_frame = 0
                    for index, (cue, info) in enumerate(zip(cues, clips, strict=True)):
                        self._check_cancelled(cancel_event)
                        start_frame = cue.start_ms * sample_rate // 1000
                        next_start = (
                            cues[index + 1].start_ms
                            if index + 1 < len(cues)
                            else cue.end_ms
                        )
                        slot_end_ms = min(cue.end_ms, next_start, duration_ms)
                        slot_end_frame = slot_end_ms * sample_rate // 1000
                        if current_frame > start_frame:
                            raise EditingError("ai_timeline_overlap_invalid")
                        _write_zeros(
                            destination,
                            start_frame - current_frame,
                            channels * sample_width,
                        )
                        current_frame = start_frame
                        copied = _copy_frames(
                            destination,
                            info,
                            max(0, slot_end_frame - start_frame),
                        )
                        current_frame += copied
                    if current_frame > total_frames:
                        raise EditingError("ai_audio_track_invalid")
                    _write_zeros(
                        destination,
                        total_frames - current_frame,
                        channels * sample_width,
                    )
                handle.flush()
                os.fsync(handle.fileno())
        except EditingError:
            raise
        except (OSError, wave.Error) as exc:
            raise EditingError("ai_audio_track_invalid") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        size, digest = _hash_file(
            track,
            MAX_PCM_TRACK_BYTES,
            "ai_audio_track_invalid",
        )
        self._progress(progress, 0.8, "ai_audio_track_ready")
        return track, size, digest

    @staticmethod
    def _progress(progress: ProgressCallback, fraction: float, code: str) -> None:
        try:
            progress(fraction, code)
        except EditingError:
            raise
        except Exception as exc:
            raise EditingError("ai_progress_callback_failed") from exc

    @staticmethod
    def _check_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CommandCancelled("AI rendering was cancelled")

    @staticmethod
    def _cleanup_files(paths: Sequence[Path]) -> None:
        for path in reversed(tuple(paths)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["AiRenderProcessor", "SpeechCheckpointBinding"]

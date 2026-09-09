"""Deterministic local segment and cover rendering for editing plans.

The processor only accepts already-local media.  It never invokes a shell,
never derives command arguments or filenames from user text, and never
modifies the source file.  Callers are expected to supply a managed staging
directory and publish the returned files through the editing asset store.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import sys
import time
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from ..subprocess_runner import (
    CommandCancelled,
    CommandResult,
    CommandSpec,
    SecureSubprocessRunner,
    SubprocessExecutionError,
    SubprocessPolicyError,
)
from .contracts import (
    EditRecipe,
    EditingError,
    RenderAsset,
    RenderResult,
    SegmentSpec,
    recipe_from_mapping,
)


MAX_SOURCE_BYTES = 16 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
EDITING_RESERVE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DECODED_BYTES = 64 * 1024 * 1024
MAX_COVER_FONT_BYTES = 32 * 1024 * 1024
MAX_TRANSCRIPTION_AUDIO_BYTES = 25 * 1024 * 1024
MAX_TRANSCRIPTION_DURATION_MS = 90 * 60 * 1000
SEGMENT_DURATION_TOLERANCE_MS = 500
PCM_TRACK_DURATION_TOLERANCE_MS = 10

_COVER_DIMENSIONS = {
    "16:9": (1280, 720),
    "4:3": (1200, 900),
    "3:4": (900, 1200),
    "9:16": (720, 1280),
    "1:1": (1080, 1080),
}
_INPUT_DEMUXER_BY_SUFFIX = {
    ".mp4": "mov",
    ".mov": "mov",
    ".m4v": "mov",
    ".mkv": "matroska",
    ".webm": "matroska",
}
_FORMAT_NAMES_BY_DEMUXER = {
    "mov": frozenset({"mov,mp4,m4a,3gp,3g2,mj2"}),
    "matroska": frozenset({"matroska,webm"}),
}
_IMAGE_MODE_BYTES_PER_PIXEL = {
    "1": 1,
    "L": 1,
    "P": 1,
    "LA": 2,
    "I": 4,
    "F": 4,
    "RGB": 3,
    "RGBA": 4,
    "CMYK": 4,
    "YCbCr": 3,
}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_VIDEO_BITRATE_BITS_PER_SECOND = 4_000_000
_AUDIO_BITRATE_BITS_PER_SECOND = 192_000
_SEGMENT_FIXED_HEADROOM_BYTES = 4 * 1024 * 1024
_RESOURCE_POLL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """Allow-listed media properties returned by ffprobe."""

    duration_ms: int
    width: int
    height: int
    container: str
    video_codec: str
    audio_codec: str | None


@dataclass(frozen=True, slots=True)
class _FileSignature:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class _CoverFontSource:
    """One immutable font snapshot reused for every cover font size."""

    payload: bytes | None


class _RenderResourceGuard:
    """Bound one active FFmpeg output without confusing it with user cancel."""

    def __init__(
        self,
        *,
        cancel_event: Event | None,
        destination: Path,
        target: Path,
        completed_bytes: int,
        file_limit_bytes: int,
    ) -> None:
        self.cancel_event = cancel_event
        self.destination = destination
        self.target = target
        self.completed_bytes = completed_bytes
        self.file_limit_bytes = file_limit_bytes
        self.resource_code: str | None = None
        self._last_resource_check = 0.0

    def is_set(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        now = time.monotonic()
        if now - self._last_resource_check < _RESOURCE_POLL_SECONDS:
            return False
        self._last_resource_check = now
        self.resource_code = self._resource_failure()
        return self.resource_code is not None

    def _resource_failure(self) -> str | None:
        size = 0
        try:
            info = self.target.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return "editing_storage_unavailable"
        else:
            if (
                self.target.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or (_REPARSE_POINT and getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
                or info.st_nlink != 1
            ):
                return "media_processing_failed"
            size = info.st_size
        if (
            size > self.file_limit_bytes
            or self.completed_bytes + size > MAX_OUTPUT_BYTES
        ):
            return "media_output_too_large"
        try:
            if shutil.disk_usage(self.destination).free <= EDITING_RESERVE_BYTES:
                return "editing_storage_full"
        except OSError:
            return "editing_storage_unavailable"
        return None


class MediaProcessor:
    """Render deterministic local editing operations with pinned FFmpeg tools."""

    def __init__(
        self,
        tool_root: Path,
        runner: SecureSubprocessRunner | None = None,
    ) -> None:
        if not isinstance(tool_root, Path) or not tool_root.is_absolute():
            raise EditingError("invalid_tool_root")
        self.tool_root = tool_root.resolve()
        self.ffmpeg_directory = self.tool_root / "ffmpeg" / "bin"
        self.ffmpeg_executable = self.ffmpeg_directory / "ffmpeg.exe"
        self.ffprobe_executable = self.ffmpeg_directory / "ffprobe.exe"
        self.runner = runner or SecureSubprocessRunner(
            allowed_executable_roots=(self.ffmpeg_directory,),
        )

    def probe(
        self,
        source: Path,
        *,
        cancel_event: Event | None = None,
        expected_source_size: int | None = None,
        expected_source_sha256: str | None = None,
    ) -> MediaProbe:
        """Probe one immutable local video with bounded ffprobe output."""

        self._validate_cancel_event(cancel_event)
        self._check_cancelled(cancel_event)
        path, signature = self._plain_file(source, source=True)
        if path.is_relative_to(self.tool_root):
            raise EditingError("invalid_source_media")
        self._verify_expected_source(
            path,
            signature,
            expected_size=expected_source_size,
            expected_sha256=expected_source_sha256,
            cancel_event=cancel_event,
        )
        probe = self._probe_path(path, cancel_event, source=True)
        self._assert_signature(path, signature, source=True)
        self._check_cancelled(cancel_event)
        return probe

    def prepare_transcription_audio(
        self,
        source: Path,
        target: Path,
        *,
        cancel_event: Event | None = None,
        expected_source_size: int | None = None,
        expected_source_sha256: str | None = None,
        clip_start_ms: int | None = None,
        clip_end_ms: int | None = None,
    ) -> Path:
        """Create a bounded mono AAC copy for a remote transcription provider."""

        self._validate_cancel_event(cancel_event)
        if (clip_start_ms is None) != (clip_end_ms is None) or (
            clip_start_ms is not None
            and (
                isinstance(clip_start_ms, bool)
                or not isinstance(clip_start_ms, int)
                or isinstance(clip_end_ms, bool)
                or not isinstance(clip_end_ms, int)
                or clip_start_ms < 0
                or clip_end_ms <= clip_start_ms
            )
        ):
            raise EditingError("invalid_transcription_clip")
        if (
            not isinstance(target, Path)
            or not target.is_absolute()
            or target.suffix.lower() != ".m4a"
        ):
            raise EditingError("invalid_transcription_output")
        destination = self._plain_directory(target.parent)
        if target.parent != destination or destination.is_relative_to(self.tool_root):
            raise EditingError("invalid_output_directory")
        if target.exists() or target.is_symlink():
            raise EditingError("edit_output_exists")

        try:
            self._check_cancelled(cancel_event)
            source_path, source_signature = self._plain_file(source, source=True)
            if source_path.is_relative_to(self.tool_root):
                raise EditingError("invalid_source_media")
            self._verify_expected_source(
                source_path,
                source_signature,
                expected_size=expected_source_size,
                expected_sha256=expected_source_sha256,
                cancel_event=cancel_event,
            )
            source_probe = self._probe_path(
                source_path,
                cancel_event,
                source=True,
                expected_signature=source_signature,
            )
            if source_probe.audio_codec is None:
                raise EditingError("ai_transcription_audio_unavailable")
            selected_duration_ms = (
                source_probe.duration_ms
                if clip_start_ms is None
                else clip_end_ms - clip_start_ms
            )
            if (
                clip_start_ms is not None
                and (
                    clip_start_ms >= source_probe.duration_ms
                    or clip_end_ms > source_probe.duration_ms
                )
            ):
                raise EditingError("invalid_transcription_clip")
            if selected_duration_ms > MAX_TRANSCRIPTION_DURATION_MS:
                # One remote request must contain the complete audio.  Fail before
                # encoding instead of ever accepting a size-limited partial file.
                raise EditingError("ai_transcription_media_too_large")
            self._require_capacity(
                destination,
                MAX_TRANSCRIPTION_AUDIO_BYTES,
                promotion_copy=False,
            )
            guard = _RenderResourceGuard(
                cancel_event=cancel_event,
                destination=destination,
                target=target,
                completed_bytes=0,
                file_limit_bytes=MAX_TRANSCRIPTION_AUDIO_BYTES,
            )
            clip_arguments: tuple[str, ...] = ()
            if clip_start_ms is not None:
                clip_arguments = (
                    "-ss",
                    self._seconds(clip_start_ms),
                    "-t",
                    self._seconds(selected_duration_ms),
                )
            result = self._run(
                CommandSpec(
                    executable=self.ffmpeg_executable,
                    arguments=(
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-n",
                        "-f",
                        self._input_demuxer(source_path, source=True),
                        "-protocol_whitelist",
                        "file",
                        "-i",
                        str(source_path),
                        *clip_arguments,
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-sn",
                        "-dn",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "32k",
                        "-ac",
                        "1",
                        "-ar",
                        "24000",
                        "-map_metadata",
                        "-1",
                        "-movflags",
                        "+faststart",
                        "-f",
                        "ipod",
                        str(target),
                    ),
                    cwd=destination,
                    timeout_seconds=60 * 60,
                    stdout_limit_bytes=64 * 1024,
                    stderr_limit_bytes=1024 * 1024,
                ),
                cancel_event,
                resource_guard=guard,
            )
            if result.returncode != 0:
                raise EditingError("media_processing_failed")
            self._assert_signature(source_path, source_signature, source=True)
            _, output_signature = self._plain_file(target, source=False)
            if output_signature.size > MAX_TRANSCRIPTION_AUDIO_BYTES:
                raise EditingError("ai_transcription_media_too_large")
            self._digest_file(target, expected_signature=output_signature)
            return target
        except EditingError as error:
            self._cleanup((target,))
            if error.code == "media_output_too_large":
                raise EditingError("ai_transcription_media_too_large") from None
            raise
        except BaseException:
            self._cleanup((target,))
            raise

    def render(
        self,
        source: Path,
        output_dir: Path,
        recipe: EditRecipe,
        *,
        cancel_event: Event | None = None,
        expected_source_size: int | None = None,
        expected_source_sha256: str | None = None,
        full_video_output: bool = False,
    ) -> RenderResult:
        """Render all local operations or leave no completed output behind."""

        if type(full_video_output) is not bool:
            raise EditingError("invalid_edit_recipe")
        self._validate_cancel_event(cancel_event)
        normalized = self._validated_recipe(
            recipe,
            allow_empty=full_video_output,
        )
        if full_video_output and normalized.segments:
            raise EditingError("invalid_edit_recipe")
        try:
            self._check_cancelled(cancel_event)
            cover_font = self._preflight_cover_font(normalized)
            self._check_cancelled(cancel_event)
            source_path, source_signature = self._plain_file(source, source=True)
            self._verify_expected_source(
                source_path,
                source_signature,
                expected_size=expected_source_size,
                expected_sha256=expected_source_sha256,
                cancel_event=cancel_event,
            )
        except CommandCancelled:
            return RenderResult(status="canceled", code="canceled")
        destination = self._plain_directory(output_dir)
        if destination.is_relative_to(self.tool_root):
            raise EditingError("invalid_output_directory")
        if source_path.is_relative_to(self.tool_root):
            raise EditingError("invalid_source_media")

        planned_video_count = (
            len(normalized.segments)
            if normalized.segments
            else int(full_video_output)
        )
        planned = [
            destination / f"segment-{index:03d}.mp4"
            for index in range(1, planned_video_count + 1)
        ]
        if normalized.cover is not None:
            planned.append(destination / "cover.png")
        if any(path.exists() or path.is_symlink() for path in planned):
            raise EditingError("edit_output_exists")

        owned_paths: list[Path] = []
        outputs: list[RenderAsset] = []
        try:
            self._check_cancelled(cancel_event)
            source_probe = self._probe_path(
                source_path,
                cancel_event,
                source=True,
                expected_signature=source_signature,
            )
            self._assert_signature(source_path, source_signature, source=True)
            self._validate_recipe_against_source(normalized, source_probe)
            render_segments = normalized.segments
            if full_video_output:
                render_segments = (
                    SegmentSpec(0, source_probe.duration_ms, "whole-video"),
                )
            render_recipe = EditRecipe(
                segments=render_segments,
                cover=normalized.cover,
                translation=normalized.translation,
                dubbing=normalized.dubbing,
            )
            estimated_bytes = self._estimated_plan_bytes(render_recipe, source_probe)
            self._require_capacity(destination, estimated_bytes)
            completed_bytes = 0

            for ordinal, segment in enumerate(render_segments, start=1):
                target = destination / f"segment-{ordinal:03d}.mp4"
                owned_paths.append(target)
                file_limit = min(
                    self._segment_output_limit(
                        segment.end_ms - segment.start_ms,
                        audio=source_probe.audio_codec is not None,
                    ),
                    MAX_OUTPUT_BYTES - completed_bytes,
                )
                if file_limit <= 0:
                    raise EditingError("media_output_too_large")
                self._render_segment(
                    source_path,
                    target,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    cancel_event=cancel_event,
                    completed_bytes=completed_bytes,
                    file_limit_bytes=file_limit,
                )
                self._assert_signature(source_path, source_signature, source=True)
                _, output_signature = self._plain_file(target, source=False)
                output_probe = self._probe_path(
                    target,
                    cancel_event,
                    source=False,
                    expected_signature=output_signature,
                )
                expected_duration = segment.end_ms - segment.start_ms
                if (
                    "mp4" not in output_probe.container.split(",")
                    or output_probe.video_codec != "h264"
                    or output_probe.audio_codec
                    != ("aac" if source_probe.audio_codec is not None else None)
                    or abs(output_probe.duration_ms - expected_duration)
                    > SEGMENT_DURATION_TOLERANCE_MS
                ):
                    raise EditingError("media_processing_failed")
                size, digest = self._digest_file(
                    target,
                    expected_signature=output_signature,
                )
                if size > file_limit or completed_bytes + size > MAX_OUTPUT_BYTES:
                    raise EditingError("media_output_too_large")
                completed_bytes += size
                outputs.append(
                    RenderAsset(
                        kind="segment",
                        path=target,
                        # Public/storage names are fixed and never contain the
                        # user-authored label.  The recipe remains the source
                        # of display metadata.
                        name=f"segment-{ordinal:03d}.mp4",
                        mime_type="video/mp4",
                        ordinal=ordinal,
                        size_bytes=size,
                        sha256=digest,
                        duration_ms=output_probe.duration_ms,
                        width=output_probe.width,
                        height=output_probe.height,
                        container=output_probe.container,
                        video_codec=output_probe.video_codec,
                        audio_codec=output_probe.audio_codec,
                    )
                )

            if normalized.cover is not None:
                cover_target = destination / "cover.png"
                owned_paths.append(cover_target)
                dimensions = self._cover_dimensions(
                    normalized.cover.aspect_ratio,
                    source_probe,
                )
                self._render_cover(
                    source_path,
                    cover_target,
                    at_ms=normalized.cover.timestamp_ms,
                    width=dimensions[0],
                    height=dimensions[1],
                    title=normalized.cover.title,
                    subtitle=normalized.cover.subtitle,
                    font_source=cover_font,
                    cancel_event=cancel_event,
                    completed_bytes=completed_bytes,
                )
                self._assert_signature(source_path, source_signature, source=True)
                width, height, size, digest = self._verify_png(cover_target)
                if (width, height) != dimensions:
                    raise EditingError("media_processing_failed")
                if completed_bytes + size > MAX_OUTPUT_BYTES:
                    raise EditingError("media_output_too_large")
                completed_bytes += size
                outputs.append(
                    RenderAsset(
                        kind="cover",
                        path=cover_target,
                        name="cover.png",
                        mime_type="image/png",
                        ordinal=1,
                        size_bytes=size,
                        sha256=digest,
                        duration_ms=None,
                        width=width,
                        height=height,
                        container="png",
                        video_codec="png",
                        audio_codec=None,
                    )
                )

            self._check_cancelled(cancel_event)
            self._assert_signature(source_path, source_signature, source=True)
            return RenderResult(
                status="ready",
                code="render_complete",
                assets=tuple(outputs),
            )
        except CommandCancelled:
            self._cleanup(owned_paths)
            return RenderResult(status="canceled", code="canceled")
        except BaseException:
            self._cleanup(owned_paths)
            raise

    def render_dubbed_video(
        self,
        source: Path,
        audio_track: Path,
        target: Path,
        *,
        track_offset_ms: int = 0,
        replace_original_audio: bool = False,
        cancel_event: Event | None = None,
        expected_source_size: int | None = None,
        expected_source_sha256: str | None = None,
    ) -> RenderAsset:
        """Render one H.264/AAC MP4 using a verified PCM WAV timeline track.

        The caller owns timeline construction.  This method owns the media
        boundary: it revalidates both inputs, runs only the pinned FFmpeg via
        ``SecureSubprocessRunner``, verifies the derived file, and removes a
        partial target on every failure or cancellation.
        """

        self._validate_cancel_event(cancel_event)
        if (
            isinstance(track_offset_ms, bool)
            or not isinstance(track_offset_ms, int)
            or track_offset_ms < 0
            or track_offset_ms > 7 * 24 * 60 * 60 * 1000
            or type(replace_original_audio) is not bool
            or not isinstance(target, Path)
            or not target.is_absolute()
            or target.suffix.lower() != ".mp4"
        ):
            raise EditingError("invalid_dubbing_output")
        destination = self._plain_directory(target.parent)
        if target.parent != destination or destination.is_relative_to(self.tool_root):
            raise EditingError("invalid_output_directory")
        if target.exists() or target.is_symlink():
            raise EditingError("edit_output_exists")

        try:
            self._check_cancelled(cancel_event)
            source_path, source_signature = self._plain_file(source, source=True)
            track_path, track_signature = self._plain_file(audio_track, source=False)
            if (
                source_path in {track_path, target}
                or track_path == target
                or track_path.suffix.lower() != ".wav"
                or source_path.is_relative_to(self.tool_root)
                or track_path.is_relative_to(self.tool_root)
            ):
                raise EditingError("invalid_dubbing_input")
            self._verify_expected_source(
                source_path,
                source_signature,
                expected_size=expected_source_size,
                expected_sha256=expected_source_sha256,
                cancel_event=cancel_event,
            )
            source_probe = self._probe_path(
                source_path,
                cancel_event,
                source=True,
                expected_signature=source_signature,
            )
            track_duration_ms = self._probe_pcm_wave(
                track_path,
                expected_signature=track_signature,
            )
            if (
                track_offset_ms + source_probe.duration_ms
                > track_duration_ms + PCM_TRACK_DURATION_TOLERANCE_MS
            ):
                raise EditingError("invalid_dubbing_track")
            self._digest_file(track_path, expected_signature=track_signature)
            file_limit = self._segment_output_limit(
                source_probe.duration_ms,
                audio=True,
            )
            self._require_capacity(destination, file_limit)
            guard = _RenderResourceGuard(
                cancel_event=cancel_event,
                destination=destination,
                target=target,
                completed_bytes=0,
                file_limit_bytes=file_limit,
            )
            arguments = [
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-f",
                self._input_demuxer(source_path, source=True),
                "-protocol_whitelist",
                "file",
                "-i",
                str(source_path),
                "-ss",
                self._seconds(track_offset_ms),
                "-f",
                "wav",
                "-protocol_whitelist",
                "file",
                "-i",
                str(track_path),
            ]
            if not replace_original_audio and source_probe.audio_codec is not None:
                arguments.extend(
                    (
                        "-filter_complex",
                        "[0:a:0]volume=0.22[original];"
                        "[original][1:a:0]amix=inputs=2:duration=longest:"
                        "dropout_transition=0:normalize=0,alimiter=limit=0.95[a]",
                        "-map",
                        "0:V:0",
                        "-map",
                        "[a]",
                    )
                )
            else:
                arguments.extend(("-map", "0:V:0", "-map", "1:a:0"))
            arguments.extend(
                (
                    "-t",
                    self._seconds(source_probe.duration_ms),
                    "-sn",
                    "-dn",
                    "-c:v",
                    "libopenh264",
                    "-b:v",
                    "4M",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    "-map_metadata",
                    "-1",
                    "-fs",
                    str(file_limit),
                    str(target),
                )
            )
            result = self._run(
                CommandSpec(
                    executable=self.ffmpeg_executable,
                    arguments=tuple(arguments),
                    cwd=destination,
                    timeout_seconds=60 * 60,
                    stdout_limit_bytes=64 * 1024,
                    stderr_limit_bytes=1024 * 1024,
                ),
                cancel_event,
                resource_guard=guard,
            )
            if result.returncode != 0:
                raise EditingError("media_processing_failed")
            self._assert_signature(source_path, source_signature, source=True)
            self._assert_signature(track_path, track_signature, source=False)
            _, output_signature = self._plain_file(target, source=False)
            output_probe = self._probe_path(
                target,
                cancel_event,
                source=False,
                expected_signature=output_signature,
            )
            if (
                "mp4" not in output_probe.container.split(",")
                or output_probe.video_codec != "h264"
                or output_probe.audio_codec != "aac"
                or abs(output_probe.duration_ms - source_probe.duration_ms)
                > SEGMENT_DURATION_TOLERANCE_MS
            ):
                raise EditingError("media_processing_failed")
            size, digest = self._digest_file(
                target,
                expected_signature=output_signature,
            )
            if size > file_limit:
                raise EditingError("media_output_too_large")
            return RenderAsset(
                kind="dubbed_video",
                path=target,
                name=target.name,
                mime_type="video/mp4",
                ordinal=1,
                size_bytes=size,
                sha256=digest,
                duration_ms=output_probe.duration_ms,
                width=output_probe.width,
                height=output_probe.height,
                container=output_probe.container,
                video_codec=output_probe.video_codec,
                audio_codec=output_probe.audio_codec,
            )
        except BaseException:
            self._cleanup((target,))
            raise

    @staticmethod
    def _validated_recipe(
        recipe: EditRecipe,
        *,
        allow_empty: bool = False,
    ) -> EditRecipe:
        if type(recipe) is not EditRecipe:
            raise EditingError("invalid_edit_recipe")
        try:
            normalized = recipe_from_mapping(recipe.to_dict())
        except EditingError as exc:
            raise EditingError("invalid_edit_recipe") from exc
        if normalized.translation.enabled or normalized.dubbing.enabled:
            raise EditingError("capability_unavailable")
        if not allow_empty and not normalized.segments and normalized.cover is None:
            raise EditingError("invalid_edit_recipe")
        return normalized

    @staticmethod
    def _validate_recipe_against_source(
        recipe: EditRecipe,
        source: MediaProbe,
    ) -> None:
        if any(segment.end_ms > source.duration_ms for segment in recipe.segments):
            raise EditingError("invalid_edit_recipe")
        if recipe.cover is not None and recipe.cover.timestamp_ms >= source.duration_ms:
            raise EditingError("invalid_edit_recipe")

    def _render_segment(
        self,
        source: Path,
        target: Path,
        *,
        start_ms: int,
        end_ms: int,
        cancel_event: Event | None,
        completed_bytes: int,
        file_limit_bytes: int,
    ) -> None:
        guard = _RenderResourceGuard(
            cancel_event=cancel_event,
            destination=target.parent,
            target=target,
            completed_bytes=completed_bytes,
            file_limit_bytes=file_limit_bytes,
        )
        result = self._run(
            CommandSpec(
                executable=self.ffmpeg_executable,
                arguments=(
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-n",
                    "-f",
                    self._input_demuxer(source, source=True),
                    "-protocol_whitelist",
                    "file",
                    "-i",
                    str(source),
                    "-ss",
                    self._seconds(start_ms),
                    "-t",
                    self._seconds(end_ms - start_ms),
                    "-map",
                    "0:V:0",
                    "-map",
                    "0:a:0?",
                    "-sn",
                    "-dn",
                    "-c:v",
                    "libopenh264",
                    "-b:v",
                    "4M",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    "-map_metadata",
                    "-1",
                    "-fs",
                    str(file_limit_bytes),
                    str(target),
                ),
                cwd=target.parent,
                timeout_seconds=60 * 60,
                stdout_limit_bytes=64 * 1024,
                stderr_limit_bytes=1024 * 1024,
            ),
            cancel_event,
            resource_guard=guard,
        )
        if result.returncode != 0:
            raise EditingError("media_processing_failed")
        self._plain_file(target, source=False)

    def _render_cover(
        self,
        source: Path,
        target: Path,
        *,
        at_ms: int,
        width: int,
        height: int,
        title: str,
        subtitle: str,
        font_source: _CoverFontSource | None,
        cancel_event: Event | None,
        completed_bytes: int,
    ) -> None:
        frame = target.parent / f".cover-frame-{uuid4().hex}.png"
        try:
            guard = _RenderResourceGuard(
                cancel_event=cancel_event,
                destination=target.parent,
                target=frame,
                completed_bytes=completed_bytes,
                file_limit_bytes=MAX_IMAGE_BYTES,
            )
            result = self._run(
                CommandSpec(
                    executable=self.ffmpeg_executable,
                    arguments=(
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-n",
                        "-f",
                        self._input_demuxer(source, source=True),
                        "-protocol_whitelist",
                        "file",
                        "-i",
                        str(source),
                        "-ss",
                        self._seconds(at_ms),
                        "-map",
                        "0:V:0",
                        "-frames:v",
                        "1",
                        "-an",
                        "-sn",
                        "-dn",
                        "-c:v",
                        "png",
                        "-f",
                        "image2",
                        "-update",
                        "1",
                        "-fs",
                        str(MAX_IMAGE_BYTES),
                        str(frame),
                    ),
                    cwd=target.parent,
                    timeout_seconds=10 * 60,
                    stdout_limit_bytes=64 * 1024,
                    stderr_limit_bytes=512 * 1024,
                ),
                cancel_event,
                resource_guard=guard,
            )
            if result.returncode != 0:
                raise EditingError("media_processing_failed")
            self._check_cancelled(cancel_event)
            payload = self._read_verified_file(frame, MAX_IMAGE_BYTES)
            rendered = self._compose_cover(
                payload,
                width=width,
                height=height,
                title=title,
                subtitle=subtitle,
                font_source=font_source,
            )
            self._check_cancelled(cancel_event)
            if len(rendered) > MAX_IMAGE_BYTES:
                raise EditingError("media_output_too_large")
            self._require_capacity(target.parent, len(rendered), promotion_copy=False)
            self._write_exclusive(target, rendered)
        except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as exc:
            if isinstance(exc, EditingError):
                raise
            raise EditingError("media_processing_failed") from exc
        finally:
            self._cleanup((frame,))

    @staticmethod
    def _input_demuxer(path: Path, *, source: bool) -> str:
        try:
            return _INPUT_DEMUXER_BY_SUFFIX[path.suffix.lower()]
        except (AttributeError, KeyError):
            raise EditingError(
                "invalid_source_media" if source else "media_processing_failed"
            ) from None

    def _probe_path(
        self,
        path: Path,
        cancel_event: Event | None,
        *,
        source: bool,
        expected_signature: _FileSignature | None = None,
    ) -> MediaProbe:
        _, signature = self._plain_file(path, source=source)
        demuxer = self._input_demuxer(path, source=source)
        if expected_signature is not None and signature != expected_signature:
            raise EditingError(
                "source_asset_changed" if source else "media_processing_failed"
            )
        result = self._run(
            CommandSpec(
                executable=self.ffprobe_executable,
                arguments=(
                    "-v",
                    "error",
                    "-f",
                    demuxer,
                    "-protocol_whitelist",
                    "file",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ),
                cwd=path.parent,
                timeout_seconds=30,
                stdout_limit_bytes=2 * 1024 * 1024,
                stderr_limit_bytes=64 * 1024,
            ),
            cancel_event,
        )
        if result.returncode != 0:
            raise EditingError("media_processing_failed")
        probe = self._parse_probe(result.stdout)
        if probe.container not in _FORMAT_NAMES_BY_DEMUXER[demuxer]:
            raise EditingError("media_processing_failed")
        self._assert_signature(path, signature, source=source)
        return probe

    @classmethod
    def _probe_pcm_wave(
        cls,
        path: Path,
        *,
        expected_signature: _FileSignature,
    ) -> int:
        _, signature = cls._plain_file(path, source=False)
        if signature != expected_signature:
            raise EditingError("invalid_dubbing_track")
        try:
            with path.open("rb") as handle:
                if cls._signature(os.fstat(handle.fileno())) != signature:
                    raise EditingError("invalid_dubbing_track")
                with wave.open(handle, "rb") as stream:
                    channels = stream.getnchannels()
                    sample_rate = stream.getframerate()
                    frames = stream.getnframes()
                    sample_width = stream.getsampwidth()
                    compression = stream.getcomptype()
                finished = cls._signature(os.fstat(handle.fileno()))
        except EditingError:
            raise
        except (EOFError, OSError, wave.Error) as exc:
            raise EditingError("invalid_dubbing_track") from exc
        if (
            finished != signature
            or channels not in {1, 2}
            or sample_rate not in {24_000, 44_100, 48_000}
            or frames <= 0
            or sample_width not in {2, 3, 4}
            or compression != "NONE"
            or signature.size != 44 + frames * channels * sample_width
        ):
            raise EditingError("invalid_dubbing_track")
        cls._assert_signature(path, signature, source=False)
        return max(1, round(frames * 1000 / sample_rate))

    def _run(
        self,
        spec: CommandSpec,
        cancel_event: Event | None,
        *,
        resource_guard: _RenderResourceGuard | None = None,
    ) -> CommandResult:
        self._check_cancelled(cancel_event)
        try:
            return self.runner.run(
                spec,
                is_cancelled=(
                    resource_guard.is_set
                    if resource_guard is not None
                    else None if cancel_event is None else cancel_event.is_set
                ),
            )
        except CommandCancelled:
            if resource_guard is not None and resource_guard.resource_code is not None:
                raise EditingError(resource_guard.resource_code) from None
            raise
        except (SubprocessExecutionError, SubprocessPolicyError) as exc:
            raise EditingError("media_processing_failed") from exc

    @staticmethod
    def _segment_output_limit(duration_ms: int, *, audio: bool) -> int:
        bitrate = _VIDEO_BITRATE_BITS_PER_SECOND
        if audio:
            bitrate += _AUDIO_BITRATE_BITS_PER_SECOND
        encoded = math.ceil(duration_ms * bitrate / 8000)
        return min(
            MAX_OUTPUT_BYTES,
            encoded * 2 + _SEGMENT_FIXED_HEADROOM_BYTES,
        )

    @classmethod
    def _estimated_plan_bytes(cls, recipe: EditRecipe, source: MediaProbe) -> int:
        total = sum(
            cls._segment_output_limit(
                segment.end_ms - segment.start_ms,
                audio=source.audio_codec is not None,
            )
            for segment in recipe.segments
        )
        if recipe.cover is not None:
            total += MAX_IMAGE_BYTES
        if total <= 0 or total > MAX_OUTPUT_BYTES:
            raise EditingError("media_output_too_large")
        return total

    @staticmethod
    def _require_capacity(
        destination: Path,
        output_bytes: int,
        *,
        promotion_copy: bool = True,
    ) -> None:
        multiplier = 2 if promotion_copy else 1
        required = output_bytes * multiplier + EDITING_RESERVE_BYTES
        try:
            free = shutil.disk_usage(destination).free
        except OSError as exc:
            raise EditingError("editing_storage_unavailable") from exc
        if free < required:
            raise EditingError("editing_storage_full")

    @classmethod
    def _verify_expected_source(
        cls,
        path: Path,
        signature: _FileSignature,
        *,
        expected_size: int | None,
        expected_sha256: str | None,
        cancel_event: Event | None,
    ) -> None:
        if expected_size is None and expected_sha256 is None:
            return
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or expected_size > MAX_SOURCE_BYTES
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
            or signature.size != expected_size
        ):
            raise EditingError("source_asset_changed")
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as handle:
                opened = cls._signature(os.fstat(handle.fileno()))
                if opened != signature:
                    raise EditingError("source_asset_changed")
                while chunk := handle.read(1024 * 1024):
                    cls._check_cancelled(cancel_event)
                    total += len(chunk)
                    if total > expected_size:
                        raise EditingError("source_asset_changed")
                    digest.update(chunk)
                finished = cls._signature(os.fstat(handle.fileno()))
        except EditingError:
            raise
        except OSError as exc:
            raise EditingError("source_asset_changed") from exc
        if (
            total != expected_size
            or digest.hexdigest() != expected_sha256
            or opened != signature
            or finished != signature
        ):
            raise EditingError("source_asset_changed")
        cls._assert_signature(path, signature, source=True)

    @classmethod
    def _parse_probe(cls, payload: bytes) -> MediaProbe:
        try:
            document = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EditingError("media_processing_failed") from exc
        if not isinstance(document, dict):
            raise EditingError("media_processing_failed")
        streams = document.get("streams")
        format_info = document.get("format")
        if not isinstance(streams, list) or not isinstance(format_info, dict):
            raise EditingError("media_processing_failed")
        video = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict)
                and stream.get("codec_type") == "video"
                and not (
                    isinstance(stream.get("disposition"), dict)
                    and stream["disposition"].get("attached_pic") == 1
                )
            ),
            None,
        )
        audio = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "audio"
            ),
            None,
        )
        if video is None:
            raise EditingError("media_processing_failed")
        duration = cls._positive_number(format_info.get("duration"))
        if duration is None:
            duration = cls._positive_number(video.get("duration"))
        width = cls._positive_integer(video.get("width"))
        height = cls._positive_integer(video.get("height"))
        container = cls._safe_token(format_info.get("format_name"))
        video_codec = cls._safe_token(video.get("codec_name"))
        audio_codec = (
            cls._safe_token(audio.get("codec_name")) if audio is not None else None
        )
        if (
            None in {duration, width, height, container, video_codec}
            or (audio is not None and audio_codec is None)
        ):
            raise EditingError("media_processing_failed")
        duration_ms = round(float(duration) * 1000)
        if (
            duration_ms <= 0
            or duration_ms > 7 * 24 * 60 * 60 * 1000
            or width is None
            or height is None
            or width > 32768
            or height > 32768
            or width * height > MAX_IMAGE_PIXELS
        ):
            raise EditingError("media_processing_failed")
        assert width is not None and height is not None
        assert container is not None and video_codec is not None
        return MediaProbe(
            duration_ms=duration_ms,
            width=width,
            height=height,
            container=container,
            video_codec=video_codec,
            audio_codec=audio_codec,
        )

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) and result > 0 else None

    @staticmethod
    def _positive_integer(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.isascii() and value.isdigit():
            parsed = int(value)
            return parsed if parsed > 0 else None
        return None

    @staticmethod
    def _safe_token(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not stripped or len(stripped) > 128:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in stripped):
            return None
        return stripped

    @classmethod
    def _preflight_cover_font(cls, recipe: EditRecipe) -> _CoverFontSource | None:
        cover = recipe.cover
        if cover is None:
            return None
        text = (
            cover.title
            if not cover.subtitle
            else f"{cover.title}\n{cover.subtitle}"
            if cover.title
            else cover.subtitle
        )
        if not text:
            return None
        if all(character == "\n" or " " <= character <= "~" for character in text):
            return _CoverFontSource(payload=None)
        if any(character != "\n" and not character.isprintable() for character in text):
            raise EditingError("cover_glyph_unsupported")

        loadable_font_found = False
        for path in cls._cover_font_candidates():
            payload = cls._read_cover_font_candidate(path)
            if payload is None:
                continue
            source = _CoverFontSource(payload=payload)
            try:
                font = cls._cover_font_at_size(source, 32)
            except EditingError:
                continue
            loadable_font_found = True
            if cls._font_supports_text(font, text):
                return source
        raise EditingError(
            "cover_glyph_unsupported"
            if loadable_font_found
            else "cover_font_unavailable"
        )

    @staticmethod
    def _cover_font_candidates() -> tuple[Path, ...]:
        """Return only fixed OS-owned font paths; never search or accept input."""

        if os.name == "nt":
            return (
                Path(r"C:\Windows\Fonts\msyh.ttc"),
                Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
                Path(r"C:\Windows\Fonts\Deng.ttf"),
                Path(r"C:\Windows\Fonts\simhei.ttf"),
            )
        if sys.platform.startswith("linux"):
            return (
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
                Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            )
        if sys.platform == "darwin":
            return (
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/System/Library/Fonts/STHeiti Light.ttc"),
            )
        return ()

    @classmethod
    def _read_cover_font_candidate(cls, path: Path) -> bytes | None:
        if not path.is_absolute() or path.is_symlink():
            return None
        try:
            resolved = path.resolve(strict=True)
            info = path.lstat()
            expected = cls._signature(info)
            if (
                resolved != path
                or not stat.S_ISREG(info.st_mode)
                or (
                    _REPARSE_POINT
                    and getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
                )
                or info.st_size <= 0
                or info.st_size > MAX_COVER_FONT_BYTES
            ):
                return None
            with path.open("rb") as handle:
                opened = cls._signature(os.fstat(handle.fileno()))
                if opened != expected:
                    return None
                payload = handle.read(MAX_COVER_FONT_BYTES + 1)
                finished = cls._signature(os.fstat(handle.fileno()))
        except OSError:
            return None
        if (
            opened != finished
            or len(payload) != expected.size
            or len(payload) > MAX_COVER_FONT_BYTES
        ):
            return None
        return payload

    @staticmethod
    def _cover_font_at_size(
        source: _CoverFontSource,
        font_size: int,
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        try:
            if source.payload is None:
                return ImageFont.load_default(size=font_size)
            return ImageFont.truetype(BytesIO(source.payload), size=font_size)
        except (OSError, ValueError) as exc:
            raise EditingError("cover_font_unavailable") from exc

    @classmethod
    def _font_supports_text(
        cls,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        text: str,
    ) -> bool:
        try:
            missing = cls._glyph_signature(font, "\U0010ffff")
            for character in text:
                if character in {" ", "\n"}:
                    continue
                if cls._glyph_signature(font, character) == missing:
                    return False
        except (OSError, OverflowError, UnicodeError, ValueError):
            return False
        return True

    @staticmethod
    def _glyph_signature(
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        character: str,
    ) -> tuple[tuple[int, int], bytes]:
        mask = font.getmask(character)
        return mask.size, bytes(mask)

    @classmethod
    def _compose_cover(
        cls,
        payload: bytes,
        *,
        width: int,
        height: int,
        title: str,
        subtitle: str,
        font_source: _CoverFontSource | None,
    ) -> bytes:
        try:
            with Image.open(BytesIO(payload)) as opened:
                if opened.format != "PNG" or getattr(opened, "n_frames", 1) != 1:
                    raise EditingError("media_processing_failed")
                orientation = opened.getexif().get(274, 1)
                source_width, source_height = opened.size
                bytes_per_pixel = _IMAGE_MODE_BYTES_PER_PIXEL.get(opened.mode)
                if (
                    type(orientation) is not int
                    or orientation != 1
                    or source_width < 1
                    or source_height < 1
                    or source_width * source_height > MAX_IMAGE_PIXELS
                    or bytes_per_pixel is None
                    or source_width * source_height * bytes_per_pixel
                    > MAX_IMAGE_DECODED_BYTES
                ):
                    raise EditingError("media_processing_failed")
                opened.load()
                image = opened.convert("RGB")
        except EditingError:
            raise
        except (
            OSError,
            SyntaxError,
            ValueError,
            Image.DecompressionBombError,
            UnidentifiedImageError,
        ) as exc:
            raise EditingError("media_processing_failed") from exc

        if source_width * height > source_height * width:
            crop_width = max(1, source_height * width // height)
            left = (source_width - crop_width) // 2
            box = (left, 0, left + crop_width, source_height)
        else:
            crop_height = max(1, source_width * height // width)
            top = (source_height - crop_height) // 2
            box = (0, top, source_width, top + crop_height)
        image = image.crop(box).resize(
            (width, height),
            resample=Image.Resampling.LANCZOS,
        )
        if title or subtitle:
            cls._draw_cover_text(
                image,
                title=title,
                subtitle=subtitle,
                font_source=font_source,
            )
        result = BytesIO()
        image.save(result, format="PNG", optimize=False, compress_level=9)
        encoded = result.getvalue()
        if not encoded or len(encoded) > MAX_IMAGE_BYTES:
            raise EditingError("media_processing_failed")
        return encoded

    @classmethod
    def _draw_cover_text(
        cls,
        image: Image.Image,
        *,
        title: str,
        subtitle: str,
        font_source: _CoverFontSource | None,
    ) -> None:
        if font_source is None:
            raise EditingError("cover_font_unavailable")
        draw = ImageDraw.Draw(image)
        margin = max(12, min(image.size) // 24)
        max_width = image.width - margin * 2
        max_height = max(1, image.height * 2 // 5)
        start_size = max(14, min(64, image.height // 12))
        selected: tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, str, int] | None = None
        combined = title if not subtitle else f"{title}\n{subtitle}" if title else subtitle
        for font_size in range(start_size, 7, -2):
            font = cls._cover_font_at_size(font_source, font_size)
            lines = cls._wrap_text(draw, combined, font, max_width)
            if len(lines) > 4:
                continue
            rendered = "\n".join(lines)
            bounds = draw.multiline_textbbox((0, 0), rendered, font=font, spacing=4)
            text_height = bounds[3] - bounds[1]
            if text_height <= max_height:
                selected = font, rendered, text_height
                break
        if selected is None:
            raise EditingError("invalid_edit_recipe")
        font, rendered, text_height = selected
        spacing = 4
        bounds = draw.multiline_textbbox(
            (0, 0), rendered, font=font, spacing=spacing, align="center"
        )
        text_width = bounds[2] - bounds[0]
        padding = max(8, margin // 2)
        left = max(margin // 2, (image.width - text_width) // 2 - padding)
        right = min(image.width - margin // 2, left + text_width + padding * 2)
        bottom = image.height - margin
        top = bottom - text_height - padding * 2
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=max(4, padding),
            fill=(0, 0, 0),
        )
        draw.multiline_text(
            ((left + right) // 2, top + padding),
            rendered,
            font=font,
            fill=(255, 255, 255),
            anchor="ma",
            spacing=spacing,
            align="center",
        )

    @staticmethod
    def _wrap_text(
        draw: ImageDraw.ImageDraw,
        value: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        output: list[str] = []
        for paragraph in value.splitlines() or [""]:
            line = ""
            for character in paragraph:
                candidate = line + character
                bounds = draw.textbbox((0, 0), candidate, font=font)
                if line and bounds[2] - bounds[0] > max_width:
                    output.append(line)
                    line = character
                else:
                    line = candidate
            output.append(line)
        return output

    @classmethod
    def _verify_png(cls, path: Path) -> tuple[int, int, int, str]:
        payload = cls._read_verified_file(path, MAX_IMAGE_BYTES)
        try:
            with Image.open(BytesIO(payload)) as image:
                if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                    raise EditingError("media_processing_failed")
                width, height = image.size
                bytes_per_pixel = _IMAGE_MODE_BYTES_PER_PIXEL.get(image.mode)
                orientation = image.getexif().get(274, 1)
                if (
                    type(orientation) is not int
                    or orientation != 1
                    or width < 1
                    or height < 1
                    or width * height > MAX_IMAGE_PIXELS
                    or bytes_per_pixel is None
                    or width * height * bytes_per_pixel > MAX_IMAGE_DECODED_BYTES
                ):
                    raise EditingError("media_processing_failed")
                image.load()
                return width, height, len(payload), hashlib.sha256(payload).hexdigest()
        except EditingError:
            raise
        except (
            OSError,
            SyntaxError,
            ValueError,
            Image.DecompressionBombError,
            UnidentifiedImageError,
        ) as exc:
            raise EditingError("media_processing_failed") from exc

    @staticmethod
    def _cover_dimensions(aspect_ratio: str, source: MediaProbe) -> tuple[int, int]:
        if aspect_ratio in _COVER_DIMENSIONS:
            return _COVER_DIMENSIONS[aspect_ratio]
        if aspect_ratio != "source":
            raise EditingError("invalid_edit_recipe")
        scale = min(1.0, 1920 / source.width, 1080 / source.height)
        width = max(1, round(source.width * scale))
        height = max(1, round(source.height * scale))
        return width, height

    @classmethod
    def _plain_file(
        cls,
        value: Path,
        *,
        source: bool,
    ) -> tuple[Path, _FileSignature]:
        code = "source_not_found" if source else "media_processing_failed"
        if not isinstance(value, Path) or not value.is_absolute():
            raise EditingError(code)
        if value.is_symlink():
            raise EditingError(code)
        try:
            path = value.resolve(strict=True)
            info = value.lstat()
        except OSError as exc:
            raise EditingError(code) from exc
        if (
            path != value
            or not stat.S_ISREG(info.st_mode)
            or (_REPARSE_POINT and getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
            or info.st_nlink != 1
            or info.st_size <= 0
            or info.st_size > (MAX_SOURCE_BYTES if source else MAX_OUTPUT_BYTES)
        ):
            raise EditingError(code)
        return path, cls._signature(info)

    @classmethod
    def _plain_directory(cls, value: Path) -> Path:
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
            or (_REPARSE_POINT and getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
        ):
            raise EditingError("invalid_output_directory")
        return path

    @classmethod
    def _assert_signature(
        cls,
        path: Path,
        expected: _FileSignature,
        *,
        source: bool,
    ) -> None:
        code = "source_asset_changed" if source else "media_processing_failed"
        try:
            info = path.lstat()
        except OSError as exc:
            raise EditingError(code) from exc
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or (_REPARSE_POINT and getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
            or info.st_nlink != 1
            or cls._signature(info) != expected
        ):
            raise EditingError(code)

    @staticmethod
    def _signature(info: os.stat_result) -> _FileSignature:
        return _FileSignature(
            device=info.st_dev,
            inode=info.st_ino,
            size=info.st_size,
            modified_ns=info.st_mtime_ns,
        )

    @classmethod
    def _read_verified_file(cls, path: Path, maximum: int) -> bytes:
        _, expected = cls._plain_file(path, source=False)
        if expected.size > maximum:
            raise EditingError("media_processing_failed")
        try:
            with path.open("rb") as handle:
                opened = cls._signature(os.fstat(handle.fileno()))
                if opened != expected:
                    raise EditingError("media_processing_failed")
                payload = handle.read(maximum + 1)
                finished = cls._signature(os.fstat(handle.fileno()))
        except EditingError:
            raise
        except OSError as exc:
            raise EditingError("media_processing_failed") from exc
        if len(payload) != expected.size or len(payload) > maximum or finished != opened:
            raise EditingError("media_processing_failed")
        cls._assert_signature(path, expected, source=False)
        return payload

    @classmethod
    def _digest_file(
        cls,
        path: Path,
        *,
        expected_signature: _FileSignature | None = None,
    ) -> tuple[int, str]:
        _, expected = cls._plain_file(path, source=False)
        if expected_signature is not None and expected != expected_signature:
            raise EditingError("media_processing_failed")
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as handle:
                opened = cls._signature(os.fstat(handle.fileno()))
                if opened != expected:
                    raise EditingError("media_processing_failed")
                while chunk := handle.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        raise EditingError("media_processing_failed")
                    digest.update(chunk)
                finished = cls._signature(os.fstat(handle.fileno()))
        except EditingError:
            raise
        except OSError as exc:
            raise EditingError("media_processing_failed") from exc
        if total != expected.size or finished != opened:
            raise EditingError("media_processing_failed")
        cls._assert_signature(path, expected, source=False)
        return total, digest.hexdigest()

    @staticmethod
    def _write_exclusive(path: Path, payload: bytes) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise EditingError("media_processing_failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _seconds(milliseconds: int) -> str:
        return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"

    @staticmethod
    def _validate_cancel_event(cancel_event: Event | None) -> None:
        if cancel_event is not None and not isinstance(cancel_event, Event):
            raise TypeError("cancel_event must be a threading.Event")

    @staticmethod
    def _check_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CommandCancelled("media processing was cancelled")

    @staticmethod
    def _cleanup(paths: Iterable[Path]) -> None:
        for path in reversed(tuple(paths)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

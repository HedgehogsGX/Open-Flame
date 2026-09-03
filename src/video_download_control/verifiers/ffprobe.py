from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ..assets import AssetValidationError, VerificationResult
from ..subprocess_runner import (
    CommandSpec,
    SecureSubprocessRunner,
    SubprocessExecutionError,
)


class FfprobeVerifier:
    """Verify media readability and basic structure without transcoding."""

    name = "ffprobe"

    def __init__(
        self,
        *,
        executable: Path,
        runner: SecureSubprocessRunner,
        expected_version: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not expected_version.strip():
            raise ValueError("expected_version is required")
        self.executable = executable
        self.runner = runner
        self.version = expected_version.strip()
        self.timeout_seconds = timeout_seconds

    def validate_runtime(self) -> None:
        """Fail closed when the installed ffprobe does not match the pin."""
        try:
            result = self.runner.run(
                CommandSpec(
                    executable=self.executable,
                    arguments=("-version",),
                    timeout_seconds=10,
                    stdout_limit_bytes=64 * 1024,
                    stderr_limit_bytes=16 * 1024,
                )
            )
        except SubprocessExecutionError as exc:
            raise AssetValidationError("ffprobe runtime check failed") from exc
        if result.returncode != 0:
            raise AssetValidationError("ffprobe runtime check failed")
        first_line = result.stdout.decode("utf-8", "replace").splitlines()[:1]
        version_fields = first_line[0].split() if first_line else []
        if len(version_fields) < 3 or version_fields[:2] != ["ffprobe", "version"] or (
            version_fields[2] != self.version
        ):
            raise AssetValidationError("ffprobe version does not match configured pin")

    def verify(self, path: Path, media_kind: str) -> VerificationResult:
        if media_kind not in {"video", "audio", "image"}:
            raise AssetValidationError("unsupported media kind for ffprobe")
        try:
            result = self.runner.run(
                CommandSpec(
                    executable=self.executable,
                    arguments=(
                        "-v",
                        "error",
                        "-print_format",
                        "json",
                        "-show_format",
                        "-show_streams",
                        str(path),
                    ),
                    cwd=path.parent,
                    timeout_seconds=self.timeout_seconds,
                    stdout_limit_bytes=2 * 1024 * 1024,
                    stderr_limit_bytes=64 * 1024,
                )
            )
        except SubprocessExecutionError as exc:
            raise AssetValidationError("ffprobe execution failed") from exc
        if result.returncode != 0:
            raise AssetValidationError("ffprobe rejected media")
        try:
            document = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssetValidationError("ffprobe returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise AssetValidationError("ffprobe returned an invalid document")
        streams = document.get("streams")
        format_info = document.get("format")
        if not isinstance(streams, list) or not isinstance(format_info, dict):
            raise AssetValidationError("ffprobe output lacks streams or format")

        required_type = "audio" if media_kind == "audio" else "video"
        primary = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict)
                and stream.get("codec_type") == required_type
            ),
            None,
        )
        if primary is None:
            raise AssetValidationError(
                f"ffprobe did not find the required {required_type} stream"
            )
        container = self._safe_string(format_info.get("format_name"))
        codec = self._safe_string(primary.get("codec_name"))
        if container is None or codec is None:
            raise AssetValidationError("ffprobe output lacks container or codec")

        duration = self._positive_float(format_info.get("duration"))
        if duration is None:
            duration = self._positive_float(primary.get("duration"))
        width = self._positive_int(primary.get("width"))
        height = self._positive_int(primary.get("height"))
        if media_kind == "image":
            duration = None
        return VerificationResult(
            media_kind=media_kind,
            container=container,
            codec=codec,
            duration_seconds=duration,
            width=width,
            height=height,
        )

    @staticmethod
    def _safe_string(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()[:128]

    @staticmethod
    def _positive_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

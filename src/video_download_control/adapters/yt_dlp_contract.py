from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from ..domain import Platform
from .base import DownloadRequest, ProbeRequest

FORBIDDEN_YT_DLP_OPTIONS = frozenset(
    {
        "--exec",
        "--external-downloader",
        "--external-downloader-args",
        "--config-locations",
        "--load-info-json",
        "--batch-file",
        "--paths-from-file",
    }
)

_DOWNLOAD_PROGRESS_TEMPLATE = (
    # Emit only constant presence bits for the yt-dlp info fields.  Main media
    # transfers inherit both ``id`` and ``format_id``; subtitle downloader
    # dictionaries inherit neither.  The actual identifiers never cross the
    # control-line boundary.
    "download:VDC_PROGRESS|%(info.id&1|0)s|"
    "%(info.format_id&1|0)s|%(progress.status)j|"
    "%(progress.downloaded_bytes)j|%(progress.total_bytes)j|"
    "%(progress.total_bytes_estimate)j|%(progress.progress_idx)j|"
    "%(progress.max_progress)j"
)
_POSTPROCESS_PROGRESS_TEMPLATE = "postprocess:VDC_PHASE|postprocessing"


class YtDlpContractError(ValueError):
    pass


class YtDlpJsRuntimeName(StrEnum):
    """JavaScript runtimes supported by the pinned yt-dlp command surface."""

    DENO = "deno"
    NODE = "node"
    BUN = "bun"
    QUICKJS = "quickjs"


@dataclass(frozen=True, slots=True)
class YtDlpJsRuntime:
    """One explicit local JavaScript runtime made available to yt-dlp."""

    name: YtDlpJsRuntimeName
    executable: Path = field(repr=False)

    def __post_init__(self) -> None:
        try:
            normalized_name = YtDlpJsRuntimeName(self.name)
        except (TypeError, ValueError) as exc:
            raise YtDlpContractError(
                "yt-dlp JavaScript runtime name is unsupported"
            ) from exc
        object.__setattr__(self, "name", normalized_name)
        executable = self.executable
        if (
            not isinstance(executable, Path)
            or not executable.is_absolute()
            or Path(os.path.abspath(executable)) != executable
        ):
            raise YtDlpContractError(
                "yt-dlp JavaScript runtime must be an absolute regular file"
            )
        try:
            info = executable.lstat()
        except OSError as exc:
            raise YtDlpContractError(
                "yt-dlp JavaScript runtime must be an absolute regular file"
            ) from exc
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or (reparse and attributes & reparse)
        ):
            raise YtDlpContractError(
                "yt-dlp JavaScript runtime must be an absolute regular file"
            )


@dataclass(frozen=True, slots=True)
class ControlledEgressEndpoint:
    """Configuration for an independently enforced HTTP egress proxy.

    Possessing this value is not an isolation proof. The eventual container
    launcher must additionally remove every direct network path.
    """

    url: str = field(repr=False)
    policy_version: str

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.url)
            port = parts.port
        except ValueError as exc:
            raise YtDlpContractError(
                "controlled egress endpoint port is invalid"
            ) from exc
        if parts.scheme != "http" or not parts.hostname or port is None:
            raise YtDlpContractError(
                "controlled egress endpoint must be an explicit HTTP host and port"
            )
        if parts.username or parts.password or parts.query or parts.fragment:
            raise YtDlpContractError(
                "controlled egress endpoint may not contain secrets"
            )
        if parts.path not in {"", "/"}:
            raise YtDlpContractError(
                "controlled egress endpoint may not contain a path"
            )
        try:
            endpoint_ip = ipaddress.ip_address(parts.hostname)
        except ValueError as exc:
            raise YtDlpContractError(
                "controlled egress endpoint must use a loopback IP literal"
            ) from exc
        if not endpoint_ip.is_loopback:
            raise YtDlpContractError(
                "controlled egress endpoint must use a loopback IP literal"
            )
        if not self.policy_version.strip():
            raise YtDlpContractError("egress policy version is required")


@dataclass(frozen=True, slots=True)
class DirectEgress:
    """Explicit local route that tells yt-dlp to bypass configured proxies.

    This is a routing declaration, not an isolation claim.  The Worker still
    requires a separately injected execution guard before it can claim work.
    """


@dataclass(frozen=True, slots=True)
class CookieMount:
    platform: Platform
    path: Path = field(repr=False)


@dataclass(frozen=True, slots=True)
class YtDlpCommand:
    executable: Path = field(repr=False)
    arguments: tuple[str, ...] = field(repr=False)
    expected_version: str
    mapping_path: Path | None = field(default=None, repr=False)


class YtDlpCommandFactory:
    """Build fixed yt-dlp invocations; it never launches a process."""

    def __init__(
        self,
        *,
        executable: Path,
        ffmpeg_directory: Path,
        expected_version: str,
        egress: ControlledEgressEndpoint | DirectEgress,
        zipimport_entrypoint: Path | None = None,
        js_runtime: YtDlpJsRuntime | None = None,
        max_height: int = 1080,
        max_file_bytes: int = 8 * 1024 * 1024 * 1024,
        socket_timeout_seconds: int = 20,
    ) -> None:
        if not executable.is_absolute() or not ffmpeg_directory.is_absolute():
            raise YtDlpContractError("tool paths must be absolute")
        if executable.is_symlink() or not executable.is_file():
            raise YtDlpContractError("yt-dlp executable must be a regular file")
        if ffmpeg_directory.is_symlink() or not ffmpeg_directory.is_dir():
            raise YtDlpContractError("ffmpeg directory must be a regular directory")
        if zipimport_entrypoint is not None:
            if (
                not zipimport_entrypoint.is_absolute()
                or zipimport_entrypoint.is_symlink()
                or not zipimport_entrypoint.is_file()
            ):
                raise YtDlpContractError(
                    "yt-dlp zipimport entrypoint must be an absolute regular file"
                )
            try:
                entrypoint_info = zipimport_entrypoint.stat()
            except OSError as exc:
                raise YtDlpContractError(
                    "yt-dlp zipimport entrypoint must be an absolute regular file"
                ) from exc
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            attributes = getattr(entrypoint_info, "st_file_attributes", 0)
            if not stat.S_ISREG(entrypoint_info.st_mode) or (
                reparse and attributes & reparse
            ):
                raise YtDlpContractError(
                    "yt-dlp zipimport entrypoint must be an absolute regular file"
                )
        if not expected_version.strip():
            raise YtDlpContractError("yt-dlp version pin is required")
        if js_runtime is not None and not isinstance(js_runtime, YtDlpJsRuntime):
            raise YtDlpContractError(
                "yt-dlp JavaScript runtime configuration is invalid"
            )
        if max_height < 144 or max_height > 2160:
            raise YtDlpContractError("max_height is outside the supported policy range")
        if max_file_bytes < 1:
            raise YtDlpContractError("max_file_bytes must be positive")
        if socket_timeout_seconds < 1 or socket_timeout_seconds > 120:
            raise YtDlpContractError("socket timeout is outside the policy range")
        self.executable = executable
        self.zipimport_entrypoint = zipimport_entrypoint
        self.js_runtime = js_runtime
        self.ffmpeg_directory = ffmpeg_directory
        self.expected_version = expected_version.strip()
        if not isinstance(egress, (ControlledEgressEndpoint, DirectEgress)):
            raise YtDlpContractError("yt-dlp egress configuration is invalid")
        self.egress = egress
        self.max_height = max_height
        self.max_file_bytes = max_file_bytes
        self.socket_timeout_seconds = socket_timeout_seconds

    def version_command(self) -> YtDlpCommand:
        return self._command(("--ignore-config", "--version"))

    def probe_command(
        self,
        request: ProbeRequest,
        *,
        temporary_root: Path,
        cookie: CookieMount | None = None,
    ) -> YtDlpCommand:
        root = self._controlled_directory(temporary_root)
        arguments = [
            *self._base_arguments(root, request.platform, cookie),
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
            "--",
            request.canonical_url,
        ]
        return self._command(tuple(arguments))

    def download_command(
        self,
        request: DownloadRequest,
        *,
        temporary_root: Path,
        cookie: CookieMount | None = None,
    ) -> YtDlpCommand:
        root = self._controlled_directory(temporary_root)
        output = self._controlled_directory(request.output_dir)
        if not output.is_relative_to(root):
            raise YtDlpContractError(
                "download output must stay inside the attempt root"
            )
        mapping_path = root / "yt-dlp-after-move.jsonl"
        # Prefer direct media URLs over HLS when both are available.  The
        # pinned YouTube extractor otherwise ranks an HLS variant first for
        # some videos; its merged timestamps can decode with duplicate-DTS
        # diagnostics even though an equivalent direct stream is clean.  The
        # final branches retain HLS as a compatibility fallback while keeping
        # the configured height ceiling.  An unqualified final ``/b`` would
        # silently bypass that ceiling when a platform exposes no smaller
        # rendition (common for vertical short-form video).
        format_selector = (
            f"bv*[height<={self.max_height}][protocol!*=m3u8]"
            f"+ba[protocol!*=m3u8]/"
            f"b[height<={self.max_height}][protocol!*=m3u8]/"
            f"bv*[height<={self.max_height}]+ba/"
            f"b[height<={self.max_height}]"
        )
        arguments = [
            *self._base_arguments(root, request.platform, cookie),
            "--format",
            format_selector,
            "--paths",
            str(output),
            "--output",
            "%(id)s.%(ext)s",
            "--output",
            "thumbnail:%(id)s.thumbnail.%(ext)s",
            "--output",
            "subtitle:%(id)s.caption.%(language)s.%(ext)s",
            "--no-overwrites",
            "--write-thumbnail",
            # TikTok can name a valid JPEG thumbnail with the ambiguous
            # .image extension. Normalize only that case through the pinned
            # lossless PNG converter; ordinary thumbnails and media stay as-is.
            "--convert-thumbnails",
            "image>png",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            # Exact full-match codes bound captions to four. Broad patterns
            # can exceed the asset layer's per-media sidecar limit.
            "en,zh,zh-Hans,zh-Hant",
            "--merge-output-format",
            "mkv",
            "--print-to-file",
            "after_move:%(.{id,filepath})j",
            self._output_template_literal(mapping_path),
            "--progress",
            "--newline",
            "--progress-delta",
            "0.5",
            "--progress-template",
            _DOWNLOAD_PROGRESS_TEMPLATE,
            "--progress-template",
            _POSTPROCESS_PROGRESS_TEMPLATE,
            "--",
            request.canonical_url,
        ]
        return self._command(tuple(arguments), mapping_path=mapping_path)

    def _base_arguments(
        self,
        temporary_root: Path,
        platform: Platform,
        cookie: CookieMount | None,
    ) -> list[str]:
        cache = temporary_root / "cache"
        arguments = [
            "--ignore-config",
            "--encoding",
            "utf-8",
            "--no-plugin-dirs",
            "--no-remote-components",
            "--no-js-runtimes",
            "--abort-on-error",
            "--no-playlist",
            "--no-write-playlist-metafiles",
            "--no-write-info-json",
        ]
        proxy_url = (
            self.egress.url
            if isinstance(self.egress, ControlledEgressEndpoint)
            else ""
        )
        arguments.extend(("--proxy", proxy_url))
        arguments.extend(
            (
                "--socket-timeout",
                str(self.socket_timeout_seconds),
                "--retries",
                "0",
                "--fragment-retries",
                "0",
                "--extractor-retries",
                "0",
                "--file-access-retries",
                "0",
                "--concurrent-fragments",
                "1",
                "--max-filesize",
                str(self.max_file_bytes),
                "--cache-dir",
                str(cache),
                "--ffmpeg-location",
                str(self.ffmpeg_directory),
            )
        )
        if self.js_runtime is not None:
            arguments.extend(
                (
                    "--js-runtimes",
                    f"{self.js_runtime.name.value}:{self.js_runtime.executable}",
                )
            )
        if cookie is not None:
            cookie_path = self._validated_cookie(
                cookie, platform, temporary_root=temporary_root
            )
            arguments.extend(("--cookies", str(cookie_path)))
        return arguments

    def _command(
        self,
        arguments: tuple[str, ...],
        *,
        mapping_path: Path | None = None,
    ) -> YtDlpCommand:
        command_arguments = (
            (str(self.zipimport_entrypoint), *arguments)
            if self.zipimport_entrypoint is not None
            else arguments
        )
        present_options = {item for item in command_arguments if item.startswith("--")}
        forbidden = present_options & FORBIDDEN_YT_DLP_OPTIONS
        if forbidden:
            raise YtDlpContractError("forbidden yt-dlp option in generated command")
        return YtDlpCommand(
            executable=self.executable,
            arguments=command_arguments,
            expected_version=self.expected_version,
            mapping_path=mapping_path,
        )

    @staticmethod
    def _output_template_literal(path: Path) -> str:
        # --print-to-file FILE follows output-template syntax. Percent signs
        # in the attempt path must therefore be escaped as literals.
        return str(path).replace("%", "%%")

    @staticmethod
    def _controlled_directory(path: Path) -> Path:
        if not path.is_absolute():
            raise YtDlpContractError("attempt directories must be absolute")
        if path.is_symlink():
            raise YtDlpContractError("attempt directory symlinks are not accepted")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise YtDlpContractError("attempt directory does not exist") from exc
        if not resolved.is_dir():
            raise YtDlpContractError("attempt path must be a directory")
        return resolved

    @staticmethod
    def _validated_cookie(
        cookie: CookieMount,
        platform: Platform,
        *,
        temporary_root: Path,
    ) -> Path:
        if cookie.platform != platform:
            raise YtDlpContractError("cookie mount platform does not match the request")
        if not cookie.path.is_absolute() or cookie.path.is_symlink():
            raise YtDlpContractError("cookie mount must be an absolute regular file")
        try:
            resolved = cookie.path.resolve(strict=True)
        except OSError as exc:
            raise YtDlpContractError("cookie mount does not exist") from exc
        info = resolved.stat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if not resolved.is_file() or (reparse and attributes & reparse):
            raise YtDlpContractError("cookie mount must be a regular file")
        if not resolved.is_relative_to(temporary_root):
            raise YtDlpContractError(
                "cookie must be an attempt-private copy, not the mounted source"
            )
        if info.st_nlink != 1:
            raise YtDlpContractError("cookie copy may not be hard-linked")
        if os.name == "posix" and info.st_mode & 0o077:
            raise YtDlpContractError("cookie copy permissions are too broad")
        return resolved

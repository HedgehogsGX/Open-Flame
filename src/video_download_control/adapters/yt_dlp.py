"""Production-candidate yt-dlp adapter behind the controlled-egress boundary.

The adapter intentionally owns no retry, fallback, credential storage, or
network-isolation policy.  It executes commands produced by the fixed
``YtDlpCommandFactory`` contract through ``SecureSubprocessRunner`` and maps
all tool output to the small, sanitized adapter DTO surface.

This module is deliberately not registered by the Worker CLI.  Enabling it
requires a deployment-owned ``NetworkExecutionGuard`` and real-platform
validation outside this component.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..capabilities import DEFAULT_DOWNLOAD_CAPABILITIES
from ..domain import ErrorCode, Platform
from ..subprocess_runner import (
    CommandCancelled,
    CommandOutputLimitExceeded,
    CommandProcessError,
    CommandResult,
    CommandSpec,
    CommandTimedOut,
    SecureSubprocessRunner,
    SubprocessPolicyError,
)
from .base import (
    AdapterContext,
    AdapterFailure,
    AdapterNetworkMode,
    CancellationToken,
    DownloadRequest,
    DownloadResult,
    ProbeItem,
    ProbeRequest,
    ProbeResult,
    ProducedFile,
    ProgressReporter,
    ProgressUpdate,
)
from .yt_dlp_contract import (
    CookieMount,
    DirectEgress,
    YtDlpCommand,
    YtDlpCommandFactory,
    YtDlpContractError,
)


CookieResolver = Callable[[Platform, str, AdapterContext], CookieMount]


_CAPTION_EXTENSIONS = frozenset(
    {".ass", ".json3", ".lrc", ".smi", ".srt", ".srv1", ".srv2", ".srv3", ".ssa", ".ttml", ".vtt"}
)
_IMAGE_EXTENSIONS = frozenset(
    {".avif", ".bmp", ".gif", ".jpe", ".jpeg", ".jpg", ".png", ".webp"}
)
_AUDIO_EXTENSIONS = frozenset(
    {".aac", ".alac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".wma"}
)
_VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogv",
        ".ts",
        ".webm",
        ".wmv",
    }
)
_KNOWN_OUTPUT_EXTENSIONS = (
    _CAPTION_EXTENSIONS | _IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS
)
_MAX_DOWNLOAD_ITEMS = 50
_DEFAULT_MAPPING_LIMIT_BYTES = 256 * 1024
_MAX_MAPPING_LIMIT_BYTES = 1024 * 1024
_MAX_PROGRESS_LINE_BYTES = 256
_MAX_PROGRESS_BYTE_COUNT = (2**63) - 1
_MAX_MEDIA_TRANSFERS = 2
_MAX_DOWNLOAD_PROGRESS_FRACTION = 0.98
_POSTPROCESS_PROGRESS_FRACTION = 0.99
_DOWNLOAD_PROGRESS_PREFIX = b"VDC_PROGRESS|"
_POSTPROCESS_PROGRESS_LINE = b"VDC_PHASE|postprocessing"

_RATE_LIMIT_MARKERS = (
    "http error 429",
    "too many requests",
    "rate limit",
    "rate-limit",
)
_BILIBILI_THROTTLE_MARKERS = (
    "http error 412",
    "code -412",
    "request was banned",
)
_DRM_MARKERS = ("drm protected", "drm-protected", "this video is drm")
_GEO_MARKERS = (
    "geo restricted",
    "geo-restricted",
    "not available in your country",
    "not available from your location",
)
_PRIVATE_MARKERS = (
    "private account",
    "private video",
    "protected tweet",
    "protected post",
)
_AUTH_MARKERS = (
    "authentication required",
    "confirm you're not a bot",
    "cookies are needed",
    "fresh cookies",
    "login required",
    "not a bot",
    "sign in to",
    "use --cookies",
)
_FORMAT_UNAVAILABLE_MARKERS = ("requested format is not available",)
_UNAVAILABLE_MARKERS = (
    "content is unavailable",
    "has been deleted",
    "has been removed",
    "is no longer available",
    "post not found",
    "status is not available",
    "tweet not found",
    "video unavailable",
)
_UNSUPPORTED_MARKERS = ("no suitable extractor", "unsupported url")
_NETWORK_MARKERS = (
    "connection refused",
    "connection reset",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "network is unreachable",
    "proxy error",
    "remote end closed connection",
    "temporary failure in name resolution",
    "timed out",
    "tunnel connection failed",
    "unable to download webpage",
)


@dataclass(frozen=True, slots=True)
class _MappedOriginal:
    media_key: str
    path: Path
    suffix: str


@dataclass(frozen=True, slots=True)
class _ControlFile:
    path: Path
    device: int
    inode: int


class _YtDlpProgressTracker:
    """Reduce fixed yt-dlp control lines to a conservative stage estimate.

    yt-dlp reports progress per transfer, not per logical item.  A subtitle or
    the first half of a separate video/audio pair can therefore reach 100%
    while more network work remains.  Sidecars are ignored; sequential main
    media transfers occupy two conservative slots; explicitly indexed parallel
    transfers are aggregated across their slots.
    """

    def __init__(self, reporter: ProgressReporter) -> None:
        self._reporter = reporter
        self._last_fraction = 0.0
        self._mode: str | None = None
        self._sequential_slot = 0
        self._sequential_active = False
        self._sequential_finished_pending = False
        self._sequential_exhausted = False
        self._indexed_max: int | None = None
        self._indexed_fractions: dict[int, float] = {}
        self.entered_postprocessing = False

    def __call__(self, line: bytes) -> None:
        if len(line) > _MAX_PROGRESS_LINE_BYTES:
            return
        if line == _POSTPROCESS_PROGRESS_LINE:
            if self.entered_postprocessing:
                return
            self.entered_postprocessing = True
            self._last_fraction = max(
                self._last_fraction,
                _POSTPROCESS_PROGRESS_FRACTION,
            )
            self._reporter(
                ProgressUpdate(
                    phase="postprocessing",
                    fraction=self._last_fraction,
                )
            )
            return
        if self.entered_postprocessing:
            return
        if not line.startswith(_DOWNLOAD_PROGRESS_PREFIX):
            return
        fields = line.split(b"|")
        if len(fields) != 9:
            return
        # Both values are fixed literals generated by yt-dlp's conditional
        # template syntax.  Requiring both makes unexpected dictionaries fail
        # closed while never exposing an actual media identifier.
        if fields[1] != b"1" or fields[2] != b"1":
            return
        try:
            status = json.loads(fields[3].decode("ascii", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return

        indexed = self._progress_indices(fields[7], fields[8])
        if indexed is False:
            return
        if status == "finished":
            self._observe_finished(indexed=indexed)
            return
        if status != "downloading":
            return
        downloaded = _progress_byte_count(fields[4])
        total = _progress_total_count(fields[5])
        estimate = _progress_total_count(fields[6])
        if downloaded is None:
            return
        if not self._activate_transfer(indexed=indexed):
            return
        selected_total = total if total is not None else estimate
        if selected_total is None:
            self._reporter(
                ProgressUpdate(
                    phase="downloading",
                    downloaded_bytes=downloaded,
                )
            )
            return
        if selected_total <= 0:
            return
        transfer_fraction = min(
            downloaded / selected_total,
            1.0,
        )
        if indexed is None:
            fraction = _MAX_DOWNLOAD_PROGRESS_FRACTION * (
                self._sequential_slot + transfer_fraction
            ) / _MAX_MEDIA_TRANSFERS
        else:
            index, maximum = indexed
            previous = self._indexed_fractions.get(index, 0.0)
            if transfer_fraction <= previous:
                return
            self._indexed_fractions[index] = transfer_fraction
            fraction = _MAX_DOWNLOAD_PROGRESS_FRACTION * (
                sum(self._indexed_fractions.values()) / maximum
            )
        if fraction <= self._last_fraction:
            return
        self._last_fraction = fraction
        self._reporter(
            ProgressUpdate(
                phase="downloading",
                fraction=fraction,
                downloaded_bytes=downloaded,
                # yt-dlp's fragment estimate may be fractional.  It is valid
                # for the ratio but must not be exposed as an exact byte total.
                total_bytes=(total if isinstance(total, int) else None),
            )
        )

    @staticmethod
    def _progress_indices(
        raw_index: bytes,
        raw_maximum: bytes,
    ) -> tuple[int, int] | None | bool:
        """Return indexed coordinates, None for sequential, False if invalid."""

        if raw_index == b"NA" and raw_maximum == b"NA":
            return None
        index = _progress_byte_count(raw_index)
        maximum = _progress_byte_count(raw_maximum)
        if (
            index is None
            or maximum is None
            or maximum < 1
            or maximum > _MAX_MEDIA_TRANSFERS
            or index >= maximum
        ):
            return False
        return index, maximum

    def _activate_transfer(
        self,
        *,
        indexed: tuple[int, int] | None,
    ) -> bool:
        if indexed is not None:
            _index, maximum = indexed
            if self._mode is None:
                self._mode = "indexed"
                self._indexed_max = maximum
            return self._mode == "indexed" and self._indexed_max == maximum

        if self._mode is None:
            self._mode = "sequential"
        if self._mode != "sequential" or self._sequential_exhausted:
            return False
        if self._sequential_finished_pending:
            self._sequential_finished_pending = False
            if self._sequential_slot + 1 >= _MAX_MEDIA_TRANSFERS:
                self._sequential_exhausted = True
                return False
            self._sequential_slot += 1
        self._sequential_active = True
        return True

    def _observe_finished(
        self,
        *,
        indexed: tuple[int, int] | None,
    ) -> None:
        if indexed is not None:
            return
        if self._mode != "sequential" or not self._sequential_active:
            return
        self._sequential_active = False
        self._sequential_finished_pending = True


class YtDlpAdapter:
    """One-attempt yt-dlp implementation suitable for guarded integration.

    ``version`` is the exact configured runtime pin.  Every probe and download
    revalidates the executable version before the actual command so a replaced
    or drifted binary fails closed.
    """

    name = "yt_dlp"
    network_mode = AdapterNetworkMode.CONTROLLED_EGRESS
    # Real X attachment selection remains disabled until an authorized Stage 0
    # sample proves that the pinned extractor downloads exactly one sibling.
    supports_exact_selector = False
    supported_routes = DEFAULT_DOWNLOAD_CAPABILITIES.claimable_routes(
        adapter="yt_dlp"
    )

    def __init__(
        self,
        *,
        factory: YtDlpCommandFactory,
        runner: SecureSubprocessRunner,
        cookie_resolver: CookieResolver | None = None,
        version_timeout_seconds: float = 10.0,
        probe_timeout_seconds: float = 90.0,
        download_timeout_seconds: float = 30.0 * 60.0,
        probe_stdout_limit_bytes: int = 2 * 1024 * 1024,
        tool_output_limit_bytes: int = 1024 * 1024,
        mapping_limit_bytes: int = _DEFAULT_MAPPING_LIMIT_BYTES,
    ) -> None:
        self._factory = factory
        self.network_mode = (
            AdapterNetworkMode.DIRECT_EGRESS
            if isinstance(factory.egress, DirectEgress)
            else AdapterNetworkMode.CONTROLLED_EGRESS
        )
        self._runner = runner
        self._cookie_resolver = cookie_resolver
        self._version_timeout_seconds = _positive_finite(
            version_timeout_seconds, "version timeout"
        )
        self._probe_timeout_seconds = _positive_finite(
            probe_timeout_seconds, "probe timeout"
        )
        self._download_timeout_seconds = _positive_finite(
            download_timeout_seconds, "download timeout"
        )
        self._probe_stdout_limit_bytes = _positive_int(
            probe_stdout_limit_bytes, "probe stdout limit"
        )
        self._tool_output_limit_bytes = _positive_int(
            tool_output_limit_bytes, "tool output limit"
        )
        self._mapping_limit_bytes = _bounded_positive_int(
            mapping_limit_bytes,
            "download mapping limit",
            maximum=_MAX_MAPPING_LIMIT_BYTES,
        )
        self.version = factory.expected_version

    def probe(self, request: ProbeRequest, context: AdapterContext) -> ProbeResult:
        self._validate_request_policy(
            max_height=request.max_height,
            max_items=request.max_items,
        )
        self._validate_runtime(context)
        cookie = self._resolve_cookie(request.platform, request.credential_ref, context)
        try:
            command = self._factory.probe_command(
                request,
                temporary_root=context.temporary_dir,
                cookie=cookie,
            )
        except YtDlpContractError as exc:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp probe request violated the fixed command policy",
            ) from exc
        result = self._execute(
            command,
            context=context,
            timeout_seconds=self._probe_timeout_seconds,
            stdout_limit_bytes=self._probe_stdout_limit_bytes,
            cancellation=None,
            operation="probe",
            platform=request.platform,
        )
        return _parse_probe_result(
            request,
            result.stdout,
            max_bytes=self._probe_stdout_limit_bytes,
        )

    def download(
        self,
        request: DownloadRequest,
        context: AdapterContext,
        progress: ProgressReporter,
        cancellation: CancellationToken,
    ) -> DownloadResult:
        self._validate_request_policy(max_height=request.max_height)
        self._validate_expected_media_keys(request.expected_media_keys)
        self._validate_runtime(context, cancellation=cancellation)
        cookie = self._resolve_cookie(request.platform, request.credential_ref, context)
        try:
            command = self._factory.download_command(
                request,
                temporary_root=context.temporary_dir,
                cookie=cookie,
            )
        except YtDlpContractError as exc:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp download request violated the fixed command policy",
            ) from exc
        mapping_file = _prepare_mapping_file(
            command,
            context=context,
            output_dir=request.output_dir,
        )
        progress_tracker = _YtDlpProgressTracker(progress)
        try:
            progress(ProgressUpdate(phase="downloading", fraction=0.0))
            self._execute(
                command,
                context=context,
                timeout_seconds=self._download_timeout_seconds,
                stdout_limit_bytes=self._tool_output_limit_bytes,
                cancellation=cancellation,
                operation="download",
                platform=request.platform,
                stdout_line_observer=progress_tracker,
            )
            mappings = _read_download_mapping(
                mapping_file,
                expected_media_keys=request.expected_media_keys,
                output_dir=request.output_dir,
                max_bytes=self._mapping_limit_bytes,
            )
        finally:
            _remove_mapping_file(mapping_file)
        classified = _classify_output(request, mappings)
        try:
            total_bytes = sum(
                item.path.stat().st_size
                for group in (
                    classified.files,
                    classified.thumbnails,
                    classified.captions,
                )
                for item in group
            )
        except OSError as exc:
            raise AdapterFailure(
                ErrorCode.STORAGE_ERROR,
                "yt-dlp output sizes could not be inspected",
            ) from exc
        progress(
            ProgressUpdate(
                phase=(
                    "postprocessing"
                    if progress_tracker.entered_postprocessing
                    else "downloading"
                ),
                fraction=1.0,
                downloaded_bytes=total_bytes,
                total_bytes=total_bytes,
            )
        )
        return classified

    def _validate_runtime(
        self,
        context: AdapterContext,
        *,
        cancellation: CancellationToken | None = None,
    ) -> None:
        try:
            command = self._factory.version_command()
        except YtDlpContractError as exc:
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                "yt-dlp runtime command violated the fixed command policy",
            ) from exc
        result = self._execute(
            command,
            context=context,
            timeout_seconds=self._version_timeout_seconds,
            stdout_limit_bytes=256,
            cancellation=cancellation,
            operation="version",
        )
        try:
            observed = result.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                "yt-dlp runtime returned an invalid version response",
            ) from exc
        if observed != command.expected_version:
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                "yt-dlp runtime version does not match the configured pin",
            )

    def _execute(
        self,
        command: YtDlpCommand,
        *,
        context: AdapterContext,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        cancellation: CancellationToken | None,
        operation: str,
        platform: Platform | None = None,
        stdout_line_observer: Callable[[bytes], None] | None = None,
    ) -> CommandResult:
        effective_timeout = _effective_timeout(context, timeout_seconds)
        observer_failure: BaseException | None = None
        cancellation_failure: BaseException | None = None

        def observe_stdout(line: bytes) -> None:
            nonlocal observer_failure
            assert stdout_line_observer is not None
            try:
                stdout_line_observer(line)
            except BaseException as exc:
                observer_failure = exc
                raise

        def check_cancellation() -> bool:
            nonlocal cancellation_failure
            assert cancellation is not None
            try:
                return cancellation.is_cancelled()
            except BaseException as exc:
                cancellation_failure = exc
                raise

        def is_callback_failure(exc: BaseException) -> bool:
            return exc is observer_failure or exc is cancellation_failure

        spec = CommandSpec(
            executable=command.executable,
            arguments=command.arguments,
            cwd=context.temporary_dir,
            environment={},
            timeout_seconds=effective_timeout,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=self._tool_output_limit_bytes,
            stdout_line_observer=(
                observe_stdout if stdout_line_observer is not None else None
            ),
        )
        try:
            result = self._runner.run(
                spec,
                is_cancelled=(
                    check_cancellation if cancellation is not None else None
                ),
            )
        except CommandCancelled as exc:
            if is_callback_failure(exc):
                raise
            # ErrorCode currently has no cancellation value.  WORKER_LOST is
            # the stable interruption code; the repository's cancel request
            # still wins when the Worker finalizes this attempt.
            raise AdapterFailure(
                ErrorCode.WORKER_LOST,
                f"yt-dlp {operation} was interrupted",
            ) from exc
        except CommandTimedOut as exc:
            if is_callback_failure(exc):
                raise
            raise AdapterFailure(
                ErrorCode.WORKER_TIMEOUT,
                f"yt-dlp {operation} exceeded its deadline",
            ) from exc
        except CommandOutputLimitExceeded as exc:
            if is_callback_failure(exc):
                raise
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                f"yt-dlp {operation} exceeded the output policy limit",
            ) from exc
        except (CommandProcessError, SubprocessPolicyError) as exc:
            if is_callback_failure(exc):
                raise
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                f"yt-dlp {operation} could not be executed under policy",
            ) from exc
        if result.returncode != 0:
            if operation == "version":
                raise AdapterFailure(
                    ErrorCode.EXTRACTOR_BROKEN,
                    "yt-dlp runtime version check failed",
                )
            code, diagnostic = _classify_exit(
                result.stderr,
                operation=operation,
                platform=platform,
            )
            raise AdapterFailure(code, diagnostic)
        return result

    def _resolve_cookie(
        self,
        platform: Platform,
        credential_ref: str | None,
        context: AdapterContext,
    ) -> CookieMount | None:
        if credential_ref is None:
            return None
        if self._cookie_resolver is None:
            raise AdapterFailure(
                ErrorCode.AUTHENTICATION_REQUIRED,
                "credential reference cannot be resolved by this adapter instance",
            )
        try:
            cookie = self._cookie_resolver(platform, credential_ref, context)
        except AdapterFailure:
            raise
        except Exception as exc:
            raise AdapterFailure(
                ErrorCode.AUTHENTICATION_REQUIRED,
                "credential reference could not be prepared",
            ) from exc
        if not isinstance(cookie, CookieMount):
            raise AdapterFailure(
                ErrorCode.AUTHENTICATION_REQUIRED,
                "credential resolver returned an invalid mount",
            )
        return cookie

    def _validate_request_policy(
        self,
        *,
        max_height: int,
        max_items: int | None = None,
    ) -> None:
        if max_height != self._factory.max_height:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "request height does not match the fixed yt-dlp policy",
            )
        if max_items is not None and (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items < 1
            or max_items > _MAX_DOWNLOAD_ITEMS
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "request item limit is invalid",
            )

    @staticmethod
    def _validate_expected_media_keys(expected: tuple[str, ...]) -> None:
        if (
            not isinstance(expected, tuple)
            or not 1 <= len(expected) <= _MAX_DOWNLOAD_ITEMS
            or any(not isinstance(media_key, str) for media_key in expected)
            or any(
                _safe_text(media_key, max_chars=256) != media_key
                for media_key in expected
            )
            or len(set(expected)) != len(expected)
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "download expectations must contain 1 to 50 unique media keys",
            )


def _positive_finite(value: float, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be a finite positive number")
    return float(value)


def _positive_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _bounded_positive_int(value: int, label: str, *, maximum: int) -> int:
    accepted = _positive_int(value, label)
    if accepted > maximum:
        raise ValueError(f"{label} exceeds the fixed upper bound")
    return accepted


def _progress_byte_count(raw: bytes) -> int | None:
    if raw == b"NA":
        return None
    try:
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_PROGRESS_BYTE_COUNT
    ):
        return None
    return value


def _progress_total_count(raw: bytes) -> int | float | None:
    """Parse an exact/estimated yt-dlp total without trusting arbitrary JSON."""

    if raw == b"NA":
        return None
    try:
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0 or value > _MAX_PROGRESS_BYTE_COUNT:
            return None
        return value
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or value < 0
            or value > _MAX_PROGRESS_BYTE_COUNT
        ):
            return None
        return value
    return None


def _effective_timeout(context: AdapterContext, configured: float) -> float:
    if context.deadline_at is None:
        return configured
    deadline = context.deadline_at
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo is not None else datetime.now()
    remaining = (deadline - now).total_seconds()
    if remaining <= 0:
        raise AdapterFailure(
            ErrorCode.WORKER_TIMEOUT,
            "yt-dlp operation deadline had already expired",
        )
    return min(configured, remaining)


def _classify_exit(
    stderr: bytes,
    *,
    operation: str,
    platform: Platform | None = None,
) -> tuple[ErrorCode, str]:
    # stderr is used only as an in-memory classifier.  None of it is reflected
    # in diagnostics because it may contain signed URLs, cookie values, or
    # platform response bodies.
    lowered = stderr.decode("utf-8", errors="replace").lower()
    if platform is Platform.BILIBILI and any(
        marker in lowered for marker in _BILIBILI_THROTTLE_MARKERS
    ):
        # Bilibili uses HTTP/API 412 for request blocking.  Treat it as
        # throttling so the Worker applies its longer backoff and platform
        # cooldown instead of immediately repeating a generic network retry.
        return (
            ErrorCode.RATE_LIMITED,
            f"yt-dlp {operation} failed due to platform request throttling",
        )
    classifications = (
        (_RATE_LIMIT_MARKERS, ErrorCode.RATE_LIMITED, "rate limiting"),
        (_DRM_MARKERS, ErrorCode.DRM_PROTECTED, "DRM-protected content"),
        (_GEO_MARKERS, ErrorCode.GEO_RESTRICTED, "geo-restricted content"),
        (_PRIVATE_MARKERS, ErrorCode.PRIVATE_CONTENT, "private content"),
        (_AUTH_MARKERS, ErrorCode.AUTHENTICATION_REQUIRED, "authentication"),
        (
            _FORMAT_UNAVAILABLE_MARKERS,
            ErrorCode.CONTENT_UNAVAILABLE,
            "no media format within policy",
        ),
        (_UNAVAILABLE_MARKERS, ErrorCode.CONTENT_UNAVAILABLE, "unavailable content"),
        (_UNSUPPORTED_MARKERS, ErrorCode.ADAPTER_UNSUPPORTED, "an unsupported URL"),
        (_NETWORK_MARKERS, ErrorCode.NETWORK_ERROR, "a network failure"),
    )
    for markers, code, reason in classifications:
        if any(marker in lowered for marker in markers):
            return code, f"yt-dlp {operation} failed due to {reason}"
    return ErrorCode.EXTRACTOR_BROKEN, f"yt-dlp {operation} failed unexpectedly"


def _parse_probe_result(
    request: ProbeRequest,
    payload: bytes,
    *,
    max_bytes: int,
) -> ProbeResult:
    if len(payload) > max_bytes:
        # A conforming SecureSubprocessRunner raises before returning this
        # shape.  Retain the check so injected runners cannot bypass the
        # parser's memory boundary.
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp probe exceeded the bounded JSON limit",
        )
    try:
        decoded = payload.decode("utf-8", errors="strict")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp probe returned invalid bounded JSON",
        ) from exc
    if not isinstance(raw, dict):
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp probe JSON root is not an object",
        )

    raw_entries = raw.get("entries")
    if raw_entries is None:
        entries: list[Mapping[str, Any]] = [raw]
    elif isinstance(raw_entries, list) and all(
        isinstance(entry, dict) for entry in raw_entries
    ):
        entries = raw_entries
    else:
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp probe returned an invalid item collection",
        )
    if not entries:
        raise AdapterFailure(
            ErrorCode.CONTENT_UNAVAILABLE,
            "yt-dlp probe returned no downloadable items",
        )
    if len(entries) > request.max_items:
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp probe exceeded the requested item limit",
        )

    items: list[ProbeItem] = []
    media_keys: set[str] = set()
    for ordinal, entry in enumerate(entries):
        source_id = _safe_text(entry.get("id"), max_chars=256)
        if source_id is None:
            source_id = _derived_media_key(request.canonical_url, ordinal)
        if source_id in media_keys:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp probe returned duplicate media identifiers",
            )
        media_keys.add(source_id)
        media_kind = _media_kind_from_info(entry)
        item_metadata = _safe_probe_metadata(entry)
        items.append(
            ProbeItem(
                canonical_url=request.canonical_url,
                media_key=source_id,
                media_kind=media_kind,
                source_id=source_id,
                title=_safe_text(entry.get("title"), max_chars=512),
                metadata=item_metadata,
            )
        )

    sanitized_source: dict[str, object] = {
        "platform": request.platform.value,
        "source_type": request.source_type.value,
        "canonical_url": request.canonical_url,
        "item_count": len(items),
    }
    for key in (
        "title",
        "uploader",
        "uploader_id",
        "channel",
        "channel_id",
        "extractor_key",
        "live_status",
    ):
        if (value := _safe_text(raw.get(key), max_chars=512)) is not None:
            sanitized_source[key] = value
    for key in ("duration", "timestamp", "width", "height"):
        if (value := _safe_number(raw.get(key))) is not None:
            sanitized_source[key] = value

    snapshot = {
        "canonical_url": request.canonical_url,
        "items": [
            {
                "media_key": item.media_key,
                "media_kind": item.media_kind,
                "source_id": item.source_id,
                "title": item.title,
                "metadata": dict(item.metadata),
            }
            for item in items
        ],
    }
    discovery_hash = hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ProbeResult(
        sanitized_source=sanitized_source,
        items=tuple(items),
        expected_item_count=len(items),
        discovery_snapshot_hash=discovery_hash,
    )


def _safe_probe_metadata(info: Mapping[str, Any]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in (
        "uploader",
        "uploader_id",
        "channel",
        "channel_id",
        "extractor_key",
        "live_status",
        "ext",
    ):
        if (value := _safe_text(info.get(key), max_chars=256)) is not None:
            metadata[key] = value
    for key in ("duration", "timestamp", "width", "height"):
        if (value := _safe_number(info.get(key))) is not None:
            metadata[key] = value
    return metadata


def _safe_text(value: object, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in value
    ).strip()
    return cleaned[:max_chars] if cleaned else None


def _safe_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0 or value > 10**12:
        return None
    return value


def _derived_media_key(canonical_url: str, ordinal: int) -> str:
    identity = hashlib.sha256(
        f"{canonical_url}\0{ordinal}".encode("utf-8")
    ).hexdigest()
    return f"derived-{identity[:24]}"


def _media_kind_from_info(info: Mapping[str, Any]) -> str:
    ext = _safe_text(info.get("ext"), max_chars=16)
    if ext is not None and f".{ext.lower().lstrip('.')}" in _IMAGE_EXTENSIONS:
        return "image"
    vcodec = _safe_text(info.get("vcodec"), max_chars=64)
    acodec = _safe_text(info.get("acodec"), max_chars=64)
    if vcodec == "none" and acodec not in {None, "none"}:
        return "audio"
    return "video"


def _prepare_mapping_file(
    command: YtDlpCommand,
    *,
    context: AdapterContext,
    output_dir: Path,
) -> _ControlFile:
    mapping_path = command.mapping_path
    if mapping_path is None or not mapping_path.is_absolute():
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp download command omitted its controlled mapping path",
        )
    try:
        attempt_root = context.temporary_dir.resolve(strict=True)
        resolved_output = output_dir.resolve(strict=True)
        mapping_parent = mapping_path.parent.resolve(strict=True)
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp mapping directory could not be inspected",
        ) from exc
    if (
        context.temporary_dir.is_symlink()
        or not attempt_root.is_dir()
        or mapping_parent != attempt_root
        or mapping_path.is_relative_to(resolved_output)
    ):
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp mapping path is outside its attempt-private control area",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor: int | None = None
    try:
        descriptor = os.open(mapping_path, flags, 0o600)
    except FileExistsError as exc:
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp mapping file already exists for this attempt",
        ) from exc
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp mapping file could not be created",
        ) from exc
    try:
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp mapping file was not created as a private regular file",
            )
        if os.name == "posix" and stat.S_IMODE(created.st_mode) & 0o077:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp mapping file permissions are too broad",
            )
        os.fsync(descriptor)
    except OSError:
        failure: AdapterFailure | None = AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp mapping file could not be initialized",
        )
    except AdapterFailure as exc:
        failure = exc
    else:
        failure = None
    finally:
        os.close(descriptor)
    if failure is not None:
        try:
            mapping_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise failure
    return _ControlFile(
        path=mapping_path,
        device=created.st_dev,
        inode=created.st_ino,
    )


def _remove_mapping_file(control_file: _ControlFile) -> None:
    mapping_path = control_file.path
    try:
        current = mapping_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp mapping file identity could not be checked",
        ) from exc
    if (current.st_dev, current.st_ino) != (
        control_file.device,
        control_file.inode,
    ):
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp mapping file identity changed before cleanup",
        )
    try:
        mapping_path.unlink(missing_ok=True)
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp mapping file could not be removed",
        ) from exc


def _read_download_mapping(
    control_file: _ControlFile,
    *,
    expected_media_keys: tuple[str, ...],
    output_dir: Path,
    max_bytes: int,
) -> tuple[_MappedOriginal, ...]:
    mapping_path = control_file.path
    descriptor: int | None = None
    try:
        before = mapping_path.lstat()
        if (before.st_dev, before.st_ino) != (
            control_file.device,
            control_file.inode,
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp mapping file identity changed after execution",
            )
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(before, "st_file_attributes", 0)
        if (
            mapping_path.is_symlink()
            or (reparse and attributes & reparse)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp mapping is not a private regular file",
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(mapping_path, flags)
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino)
            != (control_file.device, control_file.inode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (os.name == "posix" and stat.S_IMODE(opened.st_mode) & 0o077)
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp mapping changed while it was being opened",
            )
        if opened.st_size > max_bytes:
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                "yt-dlp output mapping exceeded its byte limit",
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or len(payload) != opened.st_size
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp mapping changed while it was being read",
            )
        if after.st_size > max_bytes:
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                "yt-dlp output mapping exceeded its byte limit",
            )
    except AdapterFailure:
        raise
    except FileNotFoundError as exc:
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp completed without its required output mapping",
        ) from exc
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp mapping file could not be inspected",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > max_bytes:
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp output mapping exceeded its byte limit",
        )
    lines = payload.splitlines()
    if not lines or len(lines) > _MAX_DOWNLOAD_ITEMS or any(not line for line in lines):
        raise AdapterFailure(
            ErrorCode.EXTRACTOR_BROKEN,
            "yt-dlp output mapping has an invalid record count",
        )

    try:
        resolved_output = output_dir.resolve(strict=True)
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp output directory could not be inspected",
        ) from exc
    by_media_key: dict[str, _MappedOriginal] = {}
    mapped_paths: set[Path] = set()
    for line in lines:
        try:
            record = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                "yt-dlp output mapping contains invalid JSONL",
            ) from exc
        if not isinstance(record, dict) or set(record) != {"id", "filepath"}:
            raise AdapterFailure(
                ErrorCode.EXTRACTOR_BROKEN,
                "yt-dlp output mapping contains an invalid record shape",
            )
        media_key = record["id"]
        filepath = record["filepath"]
        if (
            _safe_text(media_key, max_chars=256) != media_key
            or _safe_text(filepath, max_chars=4096) != filepath
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp output mapping contains invalid scalar values",
            )
        mapped_path = Path(filepath)
        if not mapped_path.is_absolute():
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp output mapping contains a non-absolute path",
            )
        try:
            resolved_path = mapped_path.resolve(strict=True)
        except OSError as exc:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp output mapping references a missing original",
            ) from exc
        suffix = resolved_path.suffix.lower()
        if (
            resolved_path.parent != resolved_output
            or suffix
            not in (_VIDEO_EXTENSIONS | _AUDIO_EXTENSIONS | _IMAGE_EXTENSIONS)
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp output mapping references an invalid original",
            )
        _validate_regular_output_entry(resolved_path)
        if media_key in by_media_key or resolved_path in mapped_paths:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp output mapping contains a duplicate identifier or path",
            )
        by_media_key[media_key] = _MappedOriginal(
            media_key=media_key,
            path=resolved_path,
            suffix=suffix,
        )
        mapped_paths.add(resolved_path)

    if set(by_media_key) != set(expected_media_keys):
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp output mapping does not match the probed media set",
        )
    return tuple(by_media_key[media_key] for media_key in expected_media_keys)


def _validate_regular_output_entry(path: Path) -> None:
    try:
        info = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            path.is_symlink()
            or (reparse and attributes & reparse)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp produced a non-regular or linked output entry",
            )
    except AdapterFailure:
        raise
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp output entry could not be inspected",
        ) from exc


def _classify_output(
    request: DownloadRequest,
    mappings: tuple[_MappedOriginal, ...],
) -> DownloadResult:
    output = request.output_dir
    if not output.is_absolute() or output.is_symlink():
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp output directory is not a controlled absolute directory",
        )
    try:
        resolved_output = output.resolve(strict=True)
        children = sorted(resolved_output.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise AdapterFailure(
            ErrorCode.STORAGE_ERROR,
            "yt-dlp output directory could not be inspected",
        ) from exc
    if not resolved_output.is_dir():
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp output path is not a directory",
        )

    candidates: dict[Path, str] = {}
    for child in children:
        _validate_regular_output_entry(child)
        try:
            resolved_child = child.resolve(strict=True)
        except OSError as exc:
            raise AdapterFailure(
                ErrorCode.STORAGE_ERROR,
                "yt-dlp output entry could not be resolved",
            ) from exc
        if resolved_child.parent != resolved_output:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp produced an output outside the controlled directory",
            )
        suffix = resolved_child.suffix.lower()
        if suffix not in _KNOWN_OUTPUT_EXTENSIONS:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp produced an unexpected output file type",
            )
        candidates[resolved_child] = suffix

    mapped_paths = {mapping.path for mapping in mappings}
    if len(mapped_paths) != len(mappings) or not mapped_paths.issubset(candidates):
        raise AdapterFailure(
            ErrorCode.VALIDATION_FAILED,
            "yt-dlp original mapping does not match the output directory",
        )
    produced_originals = tuple(
        ProducedFile(
            path=mapping.path,
            media_key=mapping.media_key,
            media_kind=(
                "video"
                if mapping.suffix in _VIDEO_EXTENSIONS
                else "audio"
                if mapping.suffix in _AUDIO_EXTENSIONS
                else "image"
            ),
            role="original",
            ordinal=ordinal,
        )
        for ordinal, mapping in enumerate(mappings)
    )

    thumbnails: list[ProducedFile] = []
    captions: list[ProducedFile] = []
    for path, suffix in candidates.items():
        if path in mapped_paths:
            continue
        if suffix in _IMAGE_EXTENSIONS:
            marker = ".thumbnail."
            role = "thumbnail"
            media_kind = "image"
        elif suffix in _CAPTION_EXTENSIONS:
            marker = ".caption."
            role = "caption"
            media_kind = "text"
        else:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp produced an unowned original media file",
            )
        owners = [
            mapping
            for mapping in mappings
            if path.name.startswith(f"{mapping.path.stem}{marker}")
        ]
        if len(owners) != 1:
            raise AdapterFailure(
                ErrorCode.VALIDATION_FAILED,
                "yt-dlp produced an unowned or ambiguous sidecar file",
            )
        target = thumbnails if role == "thumbnail" else captions
        target.append(
            ProducedFile(
                path=path,
                media_key=owners[0].media_key,
                media_kind=media_kind,
                role=role,
                ordinal=len(target),
            )
        )
    return DownloadResult(
        files=produced_originals,
        thumbnails=tuple(thumbnails),
        captions=tuple(captions),
    )

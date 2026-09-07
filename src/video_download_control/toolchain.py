"""Pinned, project-local Windows media toolchain bootstrap and verification.

This module installs only reviewed artifacts named by the checked-in lock.  It
does not add anything to PATH and it never performs a media-platform request.
Successful installation proves local binary integrity and an offline codec
smoke test; it deliberately does not claim that an isolated real Worker exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import urlsplit
from uuid import uuid4

from .subprocess_runner import CommandSpec, SecureSubprocessRunner

LOCK_PATH: Final = Path(__file__).with_name("toolchains") / "windows-x64.json"
LOCK_MAX_BYTES: Final = 256 * 1024
DOWNLOAD_CHUNK_BYTES: Final = 1024 * 1024
DOWNLOAD_SOCKET_TIMEOUT_SECONDS: Final = 30.0
DOWNLOAD_TOTAL_TIMEOUT_SECONDS: Final = 600.0
MAX_ARCHIVE_MEMBER_BYTES: Final = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES: Final = 4 * 1024 * 1024 * 1024
SMOKE_FILENAME: Final = "smoke-result.json"
LOCK_COPY_FILENAME: Final = "bundle-lock.json"
REDISTRIBUTION_BLOCKED_STATUS: Final = (
    "blocked_pending_third_party_source_and_notice_audit"
)
_HEX: Final = frozenset("0123456789abcdef")
_WINDOWS_INVALID_FILENAME_CHARACTERS: Final = frozenset('<>:"|?*')
_WINDOWS_RESERVED_BASENAMES: Final = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_DOWNLOAD_FINAL_HOSTS: Final = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


class ToolchainError(RuntimeError):
    """Fail-closed toolchain error with a stable, non-secret reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class Artifact:
    cache_name: str
    url: str
    size: int
    sha256: str
    install_path: str | None = None


@dataclass(frozen=True, slots=True)
class LockedFile:
    install_path: str
    size: int
    sha256: str
    archive_path: str | None = None


@dataclass(frozen=True, slots=True)
class YtDlpLock:
    version: str
    execution: str
    entrypoint: str
    license_expression: str
    artifact: Artifact
    source_artifact: Artifact
    checksums_artifact: Artifact
    signature_artifact: Artifact
    license: LockedFile


@dataclass(frozen=True, slots=True)
class FfmpegLock:
    version: str
    archive_root: str
    license_expression: str
    configuration_sha256: str
    required_configuration_flags: tuple[str, ...]
    forbidden_configuration_flags: tuple[str, ...]
    artifact: Artifact
    source_project_url: str
    build_project_url: str
    payload: tuple[LockedFile, ...]
    license: LockedFile


@dataclass(frozen=True, slots=True)
class ToolchainLock:
    schema_version: int
    bundle_id: str
    target_system: str
    target_machines: tuple[str, ...]
    yt_dlp: YtDlpLock
    ffmpeg: FfmpegLock
    network_download_enabled: bool
    isolated_worker_ready: bool
    platform_download_verified: bool
    redistribution_status: str
    raw_bytes: bytes


@dataclass(frozen=True, slots=True)
class VerifiedToolchain:
    bundle_id: str
    yt_dlp_version: str
    ffmpeg_version: str
    ffprobe_version: str
    offline_smoke_passed: bool


@dataclass(frozen=True, slots=True)
class ToolchainStatus:
    state: str
    detail_code: str
    yt_dlp_version: str | None
    ffmpeg_version: str | None
    ffprobe_version: str | None
    offline_smoke_passed: bool
    isolated_worker_ready: bool
    platform_download_verified: bool
    redistribution_status: str
    network_download_enabled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "detail_code": self.detail_code,
            "yt_dlp_version": self.yt_dlp_version,
            "ffmpeg_version": self.ffmpeg_version,
            "ffprobe_version": self.ffprobe_version,
            "offline_smoke_passed": self.offline_smoke_passed,
            "isolated_worker_ready": self.isolated_worker_ready,
            "platform_download_verified": self.platform_download_verified,
            "redistribution_status": self.redistribution_status,
            "network_download_enabled": self.network_download_enabled,
        }


DownloadFunction = Callable[[Artifact, Path], None]


def _fail(code: str, detail: str) -> ToolchainError:
    return ToolchainError(code, detail)


def _exact_keys(
    value: object, required: set[str], *, label: str
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != required:
        raise _fail("lock_schema_invalid", f"{label} fields do not match the schema")
    return value


def _bounded_text(value: object, *, label: str, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise _fail("lock_schema_invalid", f"{label} must be bounded text")
    return value


def _sha256_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise _fail("lock_schema_invalid", f"{label} must be lowercase SHA-256")
    return value


def _positive_size(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail("lock_schema_invalid", f"{label} must be a positive integer")
    return value


def _windows_filename_component_is_safe(component: str) -> bool:
    try:
        utf16_units = len(component.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return False
    basename = component.split(".", 1)[0].rstrip(" .").casefold()
    return bool(
        component
        and component not in {".", ".."}
        and not component.endswith((" ", "."))
        and utf16_units <= 255
        and not any(
            ord(character) < 32
            or character in _WINDOWS_INVALID_FILENAME_CHARACTERS
            for character in component
        )
        and basename not in _WINDOWS_RESERVED_BASENAMES
    )


def _relative_path(value: object, *, label: str) -> str:
    text = _bounded_text(value, label=label)
    if "\\" in text:
        raise _fail("lock_schema_invalid", f"{label} must use POSIX separators")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(not _windows_filename_component_is_safe(part) for part in path.parts)
    ):
        raise _fail("lock_schema_invalid", f"{label} must be a safe relative path")
    return path.as_posix()


def _https_url(value: object, *, label: str, download: bool = False) -> str:
    text = _bounded_text(value, label=label)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as error:
        raise _fail(
            "lock_schema_invalid", f"{label} must be a fixed HTTPS URL"
        ) from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _fail("lock_schema_invalid", f"{label} must be a fixed HTTPS URL")
    if download and (
        parsed.hostname != "github.com" or "/latest/" in parsed.path.lower()
    ):
        raise _fail(
            "lock_schema_invalid", f"{label} must be a fixed GitHub release URL"
        )
    return text


def _artifact(value: object, *, label: str, installed: bool) -> Artifact:
    required = {"cache_name", "url", "size", "sha256"}
    if installed:
        required.add("install_path")
    document = _exact_keys(value, required, label=label)
    cache_name = _relative_path(document["cache_name"], label=f"{label}.cache_name")
    if len(PurePosixPath(cache_name).parts) != 1:
        raise _fail(
            "lock_schema_invalid",
            f"{label}.cache_name must be a safe Windows file name",
        )
    return Artifact(
        cache_name=cache_name,
        url=_https_url(document["url"], label=f"{label}.url", download=True),
        size=_positive_size(document["size"], label=f"{label}.size"),
        sha256=_sha256_text(document["sha256"], label=f"{label}.sha256"),
        install_path=(
            _relative_path(document["install_path"], label=f"{label}.install_path")
            if installed
            else None
        ),
    )


def _locked_file(value: object, *, label: str, archived: bool) -> LockedFile:
    required = {"install_path", "size", "sha256"}
    if archived:
        required.add("archive_path")
    document = _exact_keys(value, required, label=label)
    return LockedFile(
        install_path=_relative_path(
            document["install_path"], label=f"{label}.install_path"
        ),
        size=_positive_size(document["size"], label=f"{label}.size"),
        sha256=_sha256_text(document["sha256"], label=f"{label}.sha256"),
        archive_path=(
            _relative_path(document["archive_path"], label=f"{label}.archive_path")
            if archived
            else None
        ),
    )


def _text_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise _fail("lock_schema_invalid", f"{label} must be a bounded list")
    result = tuple(
        _bounded_text(item, label=f"{label}[]", maximum=256) for item in value
    )
    if len(set(result)) != len(result):
        raise _fail("lock_schema_invalid", f"{label} contains duplicates")
    return result


def load_toolchain_lock(path: Path = LOCK_PATH) -> ToolchainLock:
    """Load and strictly validate one trusted lock document."""

    # Package managers such as uv may hard-link immutable wheel resources from
    # their content-addressed cache. Runtime artifacts remain single-link, but
    # rejecting that normal package layout would make a clean wheel unusable.
    _plain_file(
        path,
        code="lock_unavailable",
        maximum=LOCK_MAX_BYTES,
        require_single_link=False,
    )
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _fail(
            "lock_schema_invalid", "toolchain lock must be UTF-8 JSON"
        ) from error
    root = _exact_keys(
        parsed,
        {"schema_version", "bundle_id", "target", "yt_dlp", "ffmpeg", "policy"},
        label="lock",
    )
    if root["schema_version"] != 1:
        raise _fail("lock_schema_invalid", "unsupported toolchain lock schema")
    target = _exact_keys(root["target"], {"system", "machines"}, label="target")
    machines = _text_tuple(target["machines"], label="target.machines")
    target_system = _bounded_text(target["system"], label="target.system")
    if target_system != "Windows" or set(machines) != {"AMD64", "x86_64"}:
        raise _fail(
            "lock_schema_invalid", "only the reviewed Windows x64 target is approved"
        )

    yt = _exact_keys(
        root["yt_dlp"],
        {
            "version",
            "execution",
            "entrypoint",
            "license_expression",
            "artifact",
            "source_artifact",
            "checksums_artifact",
            "signature_artifact",
            "license",
        },
        label="yt_dlp",
    )
    yt_lock = YtDlpLock(
        version=_bounded_text(yt["version"], label="yt_dlp.version"),
        execution=_bounded_text(yt["execution"], label="yt_dlp.execution"),
        entrypoint=_relative_path(yt["entrypoint"], label="yt_dlp.entrypoint"),
        license_expression=_bounded_text(
            yt["license_expression"], label="yt_dlp.license_expression"
        ),
        artifact=_artifact(yt["artifact"], label="yt_dlp.artifact", installed=False),
        source_artifact=_artifact(
            yt["source_artifact"], label="yt_dlp.source_artifact", installed=True
        ),
        checksums_artifact=_artifact(
            yt["checksums_artifact"],
            label="yt_dlp.checksums_artifact",
            installed=True,
        ),
        signature_artifact=_artifact(
            yt["signature_artifact"],
            label="yt_dlp.signature_artifact",
            installed=True,
        ),
        license=_locked_file(yt["license"], label="yt_dlp.license", archived=True),
    )
    if yt_lock.execution != "python-zipimport":
        raise _fail("lock_schema_invalid", "yt-dlp execution mode is not approved")
    if yt_lock.license_expression != "Unlicense AND MIT AND ISC":
        raise _fail("lock_schema_invalid", "yt-dlp license expression is not approved")

    ff = _exact_keys(
        root["ffmpeg"],
        {
            "version",
            "archive_root",
            "license_expression",
            "configuration_sha256",
            "required_configuration_flags",
            "forbidden_configuration_flags",
            "artifact",
            "source_project_url",
            "build_project_url",
            "payload",
            "license",
        },
        label="ffmpeg",
    )
    payload_value = ff["payload"]
    if not isinstance(payload_value, list) or not 2 <= len(payload_value) <= 64:
        raise _fail("lock_schema_invalid", "ffmpeg.payload must be a bounded list")
    payload = tuple(
        _locked_file(item, label=f"ffmpeg.payload[{index}]", archived=True)
        for index, item in enumerate(payload_value)
    )
    ff_lock = FfmpegLock(
        version=_bounded_text(ff["version"], label="ffmpeg.version"),
        archive_root=_relative_path(ff["archive_root"], label="ffmpeg.archive_root"),
        license_expression=_bounded_text(
            ff["license_expression"], label="ffmpeg.license_expression"
        ),
        configuration_sha256=_sha256_text(
            ff["configuration_sha256"], label="ffmpeg.configuration_sha256"
        ),
        required_configuration_flags=_text_tuple(
            ff["required_configuration_flags"],
            label="ffmpeg.required_configuration_flags",
        ),
        forbidden_configuration_flags=_text_tuple(
            ff["forbidden_configuration_flags"],
            label="ffmpeg.forbidden_configuration_flags",
        ),
        artifact=_artifact(ff["artifact"], label="ffmpeg.artifact", installed=False),
        source_project_url=_https_url(
            ff["source_project_url"], label="ffmpeg.source_project_url"
        ),
        build_project_url=_https_url(
            ff["build_project_url"], label="ffmpeg.build_project_url"
        ),
        payload=payload,
        license=_locked_file(ff["license"], label="ffmpeg.license", archived=True),
    )
    if ff_lock.license_expression != "LGPL-3.0-or-later":
        raise _fail("lock_schema_invalid", "FFmpeg license expression is not approved")
    install_paths = [item.install_path.casefold() for item in payload]
    archive_paths = [str(item.archive_path).casefold() for item in payload]
    if len(set(install_paths)) != len(install_paths) or len(set(archive_paths)) != len(
        archive_paths
    ):
        raise _fail("lock_schema_invalid", "ffmpeg payload paths contain duplicates")
    if not {"ffmpeg/bin/ffmpeg.exe", "ffmpeg/bin/ffprobe.exe"}.issubset(install_paths):
        raise _fail("lock_schema_invalid", "ffmpeg payload omits required executables")

    policy = _exact_keys(
        root["policy"],
        {
            "network_download_enabled",
            "isolated_worker_ready",
            "platform_download_verified",
            "redistribution_status",
        },
        label="policy",
    )
    boolean_values = (
        policy["network_download_enabled"],
        policy["isolated_worker_ready"],
        policy["platform_download_verified"],
    )
    if any(not isinstance(item, bool) for item in boolean_values) or any(
        boolean_values
    ):
        raise _fail("lock_schema_invalid", "bootstrap policy must remain fail-closed")
    redistribution_status = _bounded_text(
        policy["redistribution_status"], label="policy.redistribution_status"
    )
    if redistribution_status != REDISTRIBUTION_BLOCKED_STATUS:
        raise _fail(
            "lock_schema_invalid", "redistribution policy must remain fail-closed"
        )
    return ToolchainLock(
        schema_version=1,
        bundle_id=_bounded_text(root["bundle_id"], label="bundle_id"),
        target_system=target_system,
        target_machines=machines,
        yt_dlp=yt_lock,
        ffmpeg=ff_lock,
        network_download_enabled=False,
        isolated_worker_ready=False,
        platform_download_verified=False,
        redistribution_status=redistribution_status,
        raw_bytes=raw,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and getattr(info, "st_file_attributes", 0) & reparse)


def _plain_file(
    path: Path,
    *,
    code: str = "bundle_invalid",
    size: int | None = None,
    sha256: str | None = None,
    maximum: int | None = None,
    require_single_link: bool = True,
) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise _fail(code, "required file is unavailable") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or _is_reparse(info)
        or (require_single_link and info.st_nlink != 1)
    ):
        raise _fail(code, "required file is not a plain single-link file")
    if size is not None and info.st_size != size:
        raise _fail(code, "required file size does not match the lock")
    if maximum is not None and not 0 < info.st_size <= maximum:
        raise _fail(code, "required file exceeds its size boundary")
    if sha256 is not None and _sha256_file(path) != sha256:
        raise _fail(code, "required file hash does not match the lock")


def _require_absolute_normalized(path: Path, *, label: str) -> Path:
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise _fail("path_invalid", f"{label} must be an absolute normalized path")
    if os.name == "nt":
        drive = path.drive
        if (
            len(drive) != 2
            or drive[1] != ":"
            or not drive[0].isalpha()
            or any(
                not _windows_filename_component_is_safe(part)
                for part in path.parts[1:]
            )
        ):
            raise _fail(
                "path_invalid",
                f"{label} must be on a local drive with safe Windows path segments",
            )
    if path == Path(path.anchor):
        raise _fail("path_invalid", f"{label} may not be a filesystem root")
    return path


def _plain_directory(path: Path, *, code: str = "bundle_invalid") -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise _fail(code, "required directory is unavailable") from error
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise _fail(code, "required directory is not a plain directory")


def _verify_existing_ancestors(path: Path) -> None:
    existing: list[Path] = []
    cursor = path
    while not cursor.exists():
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    while True:
        existing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for ancestor in existing:
        _plain_directory(ancestor, code="path_invalid")


def _platform_matches(
    lock: ToolchainLock,
    *,
    system_name: str | None = None,
    machine_name: str | None = None,
) -> None:
    actual_system = system_name or platform.system()
    actual_machine = machine_name or platform.machine()
    if (
        actual_system != lock.target_system
        or actual_machine not in lock.target_machines
    ):
        raise _fail("platform_mismatch", "toolchain target does not match this host")


def _validate_download_hop(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise _fail(
            "download_redirect_rejected", "artifact redirect was rejected"
        ) from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DOWNLOAD_FINAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise _fail("download_redirect_rejected", "artifact redirect was rejected")


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # HTTPRedirectHandler resolves Location before calling this method.  The
        # check therefore runs before urllib constructs or sends the next hop.
        _validate_download_hop(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _remaining_download_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _fail("download_timeout", "artifact download exceeded its deadline")
    return min(DOWNLOAD_SOCKET_TIMEOUT_SECONDS, remaining)


def _set_response_socket_timeout(response, timeout: float) -> None:
    # urllib exposes no total-deadline API.  Its HTTPS response uses this socket
    # path on supported CPython versions; read1() plus a shrinking socket timeout
    # prevents a byte-at-a-time peer from extending the transfer indefinitely.
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    socket = getattr(raw, "_sock", None)
    if socket is not None:
        socket.settimeout(timeout)


def _download_artifact(artifact: Artifact, destination: Path) -> None:
    deadline = time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT_SECONDS
    request = urllib.request.Request(
        artifact.url,
        headers={
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "video-download-control-toolchain/0.27.0",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _ValidatedRedirectHandler()
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with opener.open(
            request, timeout=_remaining_download_timeout(deadline)
        ) as response:
            _validate_download_hop(response.geturl())
            _remaining_download_timeout(deadline)
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) != artifact.size:
                raise _fail("download_size_mismatch", "artifact size header mismatched")
            reader = getattr(response, "read1", None)
            if reader is None:
                reader = response.read
            with destination.open("xb") as out:
                while True:
                    _set_response_socket_timeout(
                        response, _remaining_download_timeout(deadline)
                    )
                    chunk = reader(DOWNLOAD_CHUNK_BYTES)
                    _remaining_download_timeout(deadline)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > artifact.size:
                        raise _fail(
                            "download_size_mismatch", "artifact exceeded locked size"
                        )
                    digest.update(chunk)
                    out.write(chunk)
    except ToolchainError:
        raise
    except TimeoutError as error:
        raise _fail(
            "download_timeout", "artifact download exceeded its deadline"
        ) from error
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise _fail("download_failed", "artifact download failed") from error
    if total != artifact.size:
        raise _fail("download_size_mismatch", "artifact size mismatched")
    if digest.hexdigest() != artifact.sha256:
        raise _fail("download_hash_mismatch", "artifact hash mismatched")


def _copy_cached_artifact(artifact: Artifact, destination: Path, cache: Path) -> None:
    source = cache / artifact.cache_name
    _plain_file(
        source,
        code="artifact_cache_invalid",
        size=artifact.size,
        sha256=artifact.sha256,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as read_stream, destination.open("xb") as write_stream:
        shutil.copyfileobj(read_stream, write_stream, DOWNLOAD_CHUNK_BYTES)
    _plain_file(destination, size=artifact.size, sha256=artifact.sha256)


def _materialize(
    artifact: Artifact,
    destination: Path,
    *,
    cache: Path | None,
    downloader: DownloadFunction,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if cache is not None:
        _copy_cached_artifact(artifact, destination, cache)
    else:
        downloader(artifact, destination)
        _plain_file(destination, size=artifact.size, sha256=artifact.sha256)


def _safe_archive_name(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise _fail("archive_invalid", "archive member path is unsafe")
    normalized_name = name[:-1] if name.endswith("/") else name
    member = PurePosixPath(normalized_name)
    if (
        member.is_absolute()
        or not member.parts
        or member.as_posix() != normalized_name
        or any(part in {"", ".", ".."} for part in member.parts)
        or any(not _windows_filename_component_is_safe(part) for part in member.parts)
    ):
        raise _fail("archive_invalid", "archive member path is unsafe")
    return member


def _copy_bounded_stream(
    source, destination: Path, *, expected_size: int, expected_hash: str
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with destination.open("xb") as out:
        while chunk := source.read(DOWNLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > expected_size:
                raise _fail("archive_invalid", "archive member exceeded locked size")
            digest.update(chunk)
            out.write(chunk)
    if total != expected_size or digest.hexdigest() != expected_hash:
        raise _fail("archive_invalid", "archive member did not match the lock")


def _extract_ffmpeg(archive: Path, root: Path, lock: FfmpegLock) -> None:
    expected: dict[str, LockedFile] = {}
    for item in (*lock.payload, lock.license):
        assert item.archive_path is not None
        expected[f"{lock.archive_root}/{item.archive_path}"] = item
    seen_names: set[str] = set()
    found: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as bundle:
            total_uncompressed = 0
            for member in bundle.infolist():
                safe_name = _safe_archive_name(member.filename).as_posix()
                folded = safe_name.casefold()
                if folded in seen_names:
                    raise _fail("archive_invalid", "archive contains duplicate paths")
                seen_names.add(folded)
                total_uncompressed += member.file_size
                if (
                    member.flag_bits & 0x1
                    or member.file_size > MAX_ARCHIVE_MEMBER_BYTES
                    or total_uncompressed > MAX_ARCHIVE_TOTAL_BYTES
                ):
                    raise _fail("archive_invalid", "archive exceeds extraction policy")
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                if unix_mode and stat.S_ISLNK(unix_mode):
                    raise _fail("archive_invalid", "archive links are not accepted")
                item = expected.get(safe_name)
                if item is None:
                    continue
                if member.is_dir() or member.file_size != item.size:
                    raise _fail("archive_invalid", "required archive member is invalid")
                with bundle.open(member, "r") as source:
                    _copy_bounded_stream(
                        source,
                        root / Path(item.install_path),
                        expected_size=item.size,
                        expected_hash=item.sha256,
                    )
                found.add(safe_name)
    except (OSError, zipfile.BadZipFile) as error:
        raise _fail("archive_invalid", "FFmpeg archive is invalid") from error
    if found != set(expected):
        raise _fail("archive_invalid", "FFmpeg archive omitted a required member")


def _extract_yt_dlp_license(source_archive: Path, root: Path, item: LockedFile) -> None:
    assert item.archive_path is not None
    try:
        with tarfile.open(source_archive, "r:gz") as bundle:
            member = bundle.getmember(item.archive_path)
            if not member.isfile() or member.size != item.size:
                raise _fail("archive_invalid", "yt-dlp license member is invalid")
            source = bundle.extractfile(member)
            if source is None:
                raise _fail("archive_invalid", "yt-dlp license member is unavailable")
            with source:
                _copy_bounded_stream(
                    source,
                    root / Path(item.install_path),
                    expected_size=item.size,
                    expected_hash=item.sha256,
                )
    except (KeyError, OSError, tarfile.TarError) as error:
        raise _fail("archive_invalid", "yt-dlp source archive is invalid") from error


def _checksum_evidence_matches(root: Path, lock: ToolchainLock) -> None:
    path = root / Path(lock.yt_dlp.checksums_artifact.install_path or "")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise _fail("bundle_invalid", "yt-dlp checksum evidence is invalid") from error
    expected = f"{lock.yt_dlp.artifact.sha256}  yt-dlp"
    if lines.count(expected) != 1:
        raise _fail("bundle_invalid", "yt-dlp checksum evidence does not match")


def _walk_plain_files(root: Path) -> set[str]:
    files: set[str] = set()

    def visit(directory: Path) -> None:
        _plain_directory(directory)
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise _fail(
                "bundle_invalid", "toolchain directory cannot be inspected"
            ) from error
        for entry in entries:
            path = Path(entry.path)
            # CPython's Windows DirEntry.stat() can report st_nlink=0 for a
            # freshly created file, while Path.stat() returns the real link
            # count. Use the latter so the single-link check remains useful.
            info = path.stat(follow_symlinks=False)
            if entry.is_symlink() or _is_reparse(info):
                raise _fail("bundle_invalid", "toolchain tree contains a link")
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                files.add(path.relative_to(root).as_posix())
            else:
                raise _fail("bundle_invalid", "toolchain tree contains a special file")

    visit(root)
    return files


def _expected_files(lock: ToolchainLock) -> dict[str, tuple[int, str]]:
    result = {
        lock.yt_dlp.entrypoint: (
            lock.yt_dlp.artifact.size,
            lock.yt_dlp.artifact.sha256,
        ),
        lock.yt_dlp.source_artifact.install_path or "": (
            lock.yt_dlp.source_artifact.size,
            lock.yt_dlp.source_artifact.sha256,
        ),
        lock.yt_dlp.checksums_artifact.install_path or "": (
            lock.yt_dlp.checksums_artifact.size,
            lock.yt_dlp.checksums_artifact.sha256,
        ),
        lock.yt_dlp.signature_artifact.install_path or "": (
            lock.yt_dlp.signature_artifact.size,
            lock.yt_dlp.signature_artifact.sha256,
        ),
        lock.yt_dlp.license.install_path: (
            lock.yt_dlp.license.size,
            lock.yt_dlp.license.sha256,
        ),
        lock.ffmpeg.license.install_path: (
            lock.ffmpeg.license.size,
            lock.ffmpeg.license.sha256,
        ),
    }
    for item in lock.ffmpeg.payload:
        result[item.install_path] = (item.size, item.sha256)
    return result


def _smoke_document(lock: ToolchainLock) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bundle_id": lock.bundle_id,
        "status": "passed",
        "streams": ["audio", "video"],
        "network_access": "not_exercised",
    }


def _smoke_marker_valid(root: Path, lock: ToolchainLock) -> bool:
    marker = root / SMOKE_FILENAME
    if not marker.exists():
        return False
    _plain_file(marker, maximum=4096)
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _fail(
            "smoke_marker_invalid", "offline smoke marker is invalid"
        ) from error
    if document != _smoke_document(lock):
        raise _fail("smoke_marker_invalid", "offline smoke marker does not match")
    return True


def _run_version_checks(
    root: Path, lock: ToolchainLock, python_executable: Path
) -> None:
    ffmpeg_root = root / "ffmpeg" / "bin"
    runner = SecureSubprocessRunner(
        allowed_executable_roots=[python_executable.parent, ffmpeg_root]
    )
    yt_result = runner.run(
        CommandSpec(
            executable=python_executable,
            arguments=(
                "-I",
                str(root / Path(lock.yt_dlp.entrypoint)),
                "--ignore-config",
                "--version",
            ),
            cwd=root,
            timeout_seconds=15,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=64 * 1024,
        )
    )
    if (
        yt_result.returncode != 0
        or yt_result.stdout.decode("utf-8", errors="replace").strip()
        != lock.yt_dlp.version
    ):
        raise _fail("version_mismatch", "yt-dlp version check failed")

    for executable_name in ("ffmpeg.exe", "ffprobe.exe"):
        result = runner.run(
            CommandSpec(
                executable=ffmpeg_root / executable_name,
                arguments=("-version",),
                cwd=root,
                timeout_seconds=15,
                stdout_limit_bytes=256 * 1024,
                stderr_limit_bytes=64 * 1024,
            )
        )
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        tool_name = executable_name.removesuffix(".exe")
        if (
            result.returncode != 0
            or not lines
            or not lines[0].startswith(f"{tool_name} version {lock.ffmpeg.version} ")
        ):
            raise _fail("version_mismatch", f"{tool_name} version check failed")
        configuration = next(
            (
                line.removeprefix("configuration:").strip()
                for line in lines
                if line.startswith("configuration:")
            ),
            None,
        )
        if (
            configuration is None
            or hashlib.sha256(configuration.encode()).hexdigest()
            != lock.ffmpeg.configuration_sha256
        ):
            raise _fail("configuration_mismatch", "FFmpeg configuration check failed")
        flags = set(configuration.split())
        if not set(lock.ffmpeg.required_configuration_flags).issubset(flags) or set(
            lock.ffmpeg.forbidden_configuration_flags
        ).intersection(flags):
            raise _fail(
                "configuration_mismatch", "FFmpeg license flags are not approved"
            )


def verify_toolchain(
    tool_root: Path,
    *,
    execute_version_checks: bool = True,
    python_executable: Path | None = None,
    lock: ToolchainLock | None = None,
    validate_smoke_marker: bool = True,
) -> VerifiedToolchain:
    """Verify the complete managed file set, hashes, policy and tool versions."""

    active_lock = lock or load_toolchain_lock()
    _platform_matches(active_lock)
    root = _require_absolute_normalized(tool_root, label="tool root")
    _plain_directory(root)
    _verify_existing_ancestors(root)
    installed_lock = root / LOCK_COPY_FILENAME
    _plain_file(installed_lock, maximum=LOCK_MAX_BYTES)
    if installed_lock.read_bytes() != active_lock.raw_bytes:
        raise _fail("lock_mismatch", "installed lock differs from the application lock")

    expected = _expected_files(active_lock)
    allowed = set(expected) | {LOCK_COPY_FILENAME}
    marker_exists = (root / SMOKE_FILENAME).exists()
    if marker_exists:
        allowed.add(SMOKE_FILENAME)
    if _walk_plain_files(root) != allowed:
        raise _fail("bundle_invalid", "toolchain managed file set does not match")
    for relative, (size, digest) in expected.items():
        _plain_file(root / Path(relative), size=size, sha256=digest)
    _checksum_evidence_matches(root, active_lock)
    smoke_passed = (
        _smoke_marker_valid(root, active_lock)
        if validate_smoke_marker and marker_exists
        else False
    )
    if execute_version_checks:
        executable = (python_executable or Path(sys.executable)).resolve(strict=True)
        _run_version_checks(root, active_lock, executable)
    return VerifiedToolchain(
        bundle_id=active_lock.bundle_id,
        yt_dlp_version=active_lock.yt_dlp.version,
        ffmpeg_version=active_lock.ffmpeg.version,
        ffprobe_version=active_lock.ffmpeg.version,
        offline_smoke_passed=smoke_passed,
    )


def run_offline_smoke(
    tool_root: Path,
    *,
    python_executable: Path | None = None,
    lock: ToolchainLock | None = None,
) -> VerifiedToolchain:
    """Run yt-dlp version plus synthetic FFmpeg/ffprobe checks without network."""

    active_lock = lock or load_toolchain_lock()
    executable = (python_executable or Path(sys.executable)).resolve(strict=True)
    root = _require_absolute_normalized(tool_root, label="tool root")
    verify_toolchain(
        root,
        execute_version_checks=True,
        python_executable=executable,
        lock=active_lock,
        validate_smoke_marker=False,
    )
    temporary = Path(tempfile.mkdtemp(prefix="vdc-tool-smoke-", dir=root.parent))
    pending: Path | None = None
    try:
        media = temporary / "synthetic.mkv"
        ffmpeg_root = root / "ffmpeg" / "bin"
        runner = SecureSubprocessRunner(allowed_executable_roots=[ffmpeg_root])
        encoded = runner.run(
            CommandSpec(
                executable=ffmpeg_root / "ffmpeg.exe",
                arguments=(
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=160x90:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-shortest",
                    "-c:v",
                    "mpeg4",
                    "-c:a",
                    "aac",
                    "-y",
                    str(media),
                ),
                cwd=temporary,
                timeout_seconds=30,
                stdout_limit_bytes=128 * 1024,
                stderr_limit_bytes=128 * 1024,
            )
        )
        if encoded.returncode != 0 or not media.is_file() or media.stat().st_size < 1:
            raise _fail("offline_smoke_failed", "FFmpeg synthetic encode failed")
        probed = runner.run(
            CommandSpec(
                executable=ffmpeg_root / "ffprobe.exe",
                arguments=(
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    str(media),
                ),
                cwd=temporary,
                timeout_seconds=15,
                stdout_limit_bytes=1024 * 1024,
                stderr_limit_bytes=128 * 1024,
            )
        )
        try:
            probe = json.loads(probed.stdout.decode("utf-8"))
            streams = sorted(
                {
                    item.get("codec_type")
                    for item in probe["streams"]
                    if isinstance(item, dict)
                }
            )
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise _fail(
                "offline_smoke_failed", "ffprobe smoke output is invalid"
            ) from error
        if probed.returncode != 0 or streams != ["audio", "video"]:
            raise _fail("offline_smoke_failed", "ffprobe did not find audio and video")

        marker = root / SMOKE_FILENAME
        pending = root / f".{SMOKE_FILENAME}.{uuid4().hex}.tmp"
        pending.write_text(
            json.dumps(_smoke_document(active_lock), sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(pending, marker)
    finally:
        if (
            pending is not None
            and pending.parent == root
            and pending.name.startswith(f".{SMOKE_FILENAME}.")
        ):
            pending.unlink(missing_ok=True)
        if temporary.parent == root.parent and temporary.name.startswith(
            "vdc-tool-smoke-"
        ):
            shutil.rmtree(temporary, ignore_errors=True)
    return verify_toolchain(
        root,
        execute_version_checks=False,
        python_executable=executable,
        lock=active_lock,
    )


def install_toolchain(
    tool_root: Path,
    *,
    artifact_cache: Path | None = None,
    python_executable: Path | None = None,
    lock: ToolchainLock | None = None,
    downloader: DownloadFunction = _download_artifact,
) -> ToolchainStatus:
    """Atomically install and offline-smoke the fixed Windows tool bundle."""

    active_lock = lock or load_toolchain_lock()
    _platform_matches(active_lock)
    root = _require_absolute_normalized(tool_root, label="tool root")
    _verify_existing_ancestors(root)
    if root.exists():
        raise _fail("target_exists", "tool root already exists; overwrite is refused")
    root.parent.mkdir(parents=True, exist_ok=True)
    _plain_directory(root.parent, code="path_invalid")
    cache: Path | None = None
    if artifact_cache is not None:
        cache = _require_absolute_normalized(artifact_cache, label="artifact cache")
        _plain_directory(cache, code="artifact_cache_invalid")
        _verify_existing_ancestors(cache)

    staging = root.parent / f".vdc-toolchain-staging-{uuid4().hex}"
    staging.mkdir()
    try:
        (staging / LOCK_COPY_FILENAME).write_bytes(active_lock.raw_bytes)
        _materialize(
            active_lock.yt_dlp.artifact,
            staging / Path(active_lock.yt_dlp.entrypoint),
            cache=cache,
            downloader=downloader,
        )
        for artifact in (
            active_lock.yt_dlp.source_artifact,
            active_lock.yt_dlp.checksums_artifact,
            active_lock.yt_dlp.signature_artifact,
        ):
            assert artifact.install_path is not None
            _materialize(
                artifact,
                staging / Path(artifact.install_path),
                cache=cache,
                downloader=downloader,
            )
        _extract_yt_dlp_license(
            staging / Path(active_lock.yt_dlp.source_artifact.install_path or ""),
            staging,
            active_lock.yt_dlp.license,
        )
        ffmpeg_archive = staging / ".ffmpeg-download.zip"
        _materialize(
            active_lock.ffmpeg.artifact,
            ffmpeg_archive,
            cache=cache,
            downloader=downloader,
        )
        _extract_ffmpeg(ffmpeg_archive, staging, active_lock.ffmpeg)
        ffmpeg_archive.unlink()
        run_offline_smoke(
            staging,
            python_executable=python_executable,
            lock=active_lock,
        )
        staging.replace(root)
    except BaseException:
        if staging.parent == root.parent and staging.name.startswith(
            ".vdc-toolchain-staging-"
        ):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return inspect_toolchain(root, lock=active_lock)


def inspect_toolchain(
    tool_root: Path | None, *, lock: ToolchainLock | None = None
) -> ToolchainStatus:
    """Return a bounded UI/API status without executing any installed tool."""

    active_lock = lock or load_toolchain_lock()
    if tool_root is None:
        return ToolchainStatus(
            state="unconfigured",
            detail_code="tool_root_unconfigured",
            yt_dlp_version=None,
            ffmpeg_version=None,
            ffprobe_version=None,
            offline_smoke_passed=False,
            isolated_worker_ready=False,
            platform_download_verified=False,
            redistribution_status=active_lock.redistribution_status,
            network_download_enabled=False,
        )
    try:
        verified = verify_toolchain(
            tool_root, execute_version_checks=False, lock=active_lock
        )
        state = "ready" if verified.offline_smoke_passed else "invalid"
        detail_code = (
            "ok" if verified.offline_smoke_passed else "offline_smoke_required"
        )
        return ToolchainStatus(
            state=state,
            detail_code=detail_code,
            yt_dlp_version=verified.yt_dlp_version,
            ffmpeg_version=verified.ffmpeg_version,
            ffprobe_version=verified.ffprobe_version,
            offline_smoke_passed=verified.offline_smoke_passed,
            isolated_worker_ready=False,
            platform_download_verified=False,
            redistribution_status=active_lock.redistribution_status,
            network_download_enabled=False,
        )
    except ToolchainError as error:
        return ToolchainStatus(
            state="invalid",
            detail_code=error.code,
            yt_dlp_version=None,
            ffmpeg_version=None,
            ffprobe_version=None,
            offline_smoke_passed=False,
            isolated_worker_ready=False,
            platform_download_verified=False,
            redistribution_status=active_lock.redistribution_status,
            network_download_enabled=False,
        )
    except OSError:
        return ToolchainStatus(
            state="invalid",
            detail_code="toolchain_unavailable",
            yt_dlp_version=None,
            ffmpeg_version=None,
            ffprobe_version=None,
            offline_smoke_passed=False,
            isolated_worker_ready=False,
            platform_download_verified=False,
            redistribution_status=active_lock.redistribution_status,
            network_download_enabled=False,
        )

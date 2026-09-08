"""Offline builder for the optional Windows x64 AI runtime.

The builder accepts an already downloaded CPython 3.12 or 3.13 embeddable
archive.  It does not contain a downloader and never calls a remote AI
provider.  Every archive member is extracted manually into a new sibling
staging directory, and the finished runtime is published only after its
complete manifest has been written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator
from uuid import uuid4

from ..subprocess_runner import CommandSpec, SecureSubprocessRunner
from .ai_protocol import (
    PROTOCOL_SCHEMA,
    RUNTIME_ID,
    RUNTIME_MANIFEST_SCHEMA,
    RUNTIME_VERSION,
    AiProtocolError,
    canonical_json_bytes,
    relative_runtime_path,
)
from .ai_runtime import AiRuntime, AiRuntimeError, load_ai_runtime


PLATFORM = "windows-x64"
PYTHON_EXECUTABLE = "python.exe"
WORKER_FILE = "ai_worker.py"
PROTOCOL_FILE = "ai_protocol.py"
OPENAI_PROVIDER_FILE = "openai_provider.py"
MANIFEST_FILE = "manifest.json"
OPENAI_PROVIDER_ID = "openai"
OPENAI_AUTH_ENV = "OPEN_FLAME_AI_OPENAI_API_KEY"
OPENAI_RUNTIME_REVISION = "open-flame-openai-runtime-v1"
# Translation accepts at most 10,000 cues in batches of 50.  Keep the bridge
# deadline above the 200 bounded HTTP request timeouts plus process overhead;
# each individual request remains limited to 120 seconds by provider config.
OPENAI_OPERATION_TIMEOUT_SECONDS = 25_200.0
OPENAI_REQUEST_TIMEOUT_SECONDS = 120
OPENAI_MODEL_TERMS = (
    "OpenAI API cloud service under OpenAI API Terms; "
    "model weights are not bundled or redistributed"
)
OPENAI_STANDARD_VOICE_IDS = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)
OPENAI_MODELS = (
    ("whisper-1", ("transcribe",)),
    ("gpt-5.6-luna", ("translate",)),
    ("gpt-4o-mini-tts", ("synthesize",)),
)

MAX_PYTHON_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4_096
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_SOURCE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
PYTHON_CHECK_TIMEOUT_SECONDS = 30.0
PYTHON_CHECK_OUTPUT_BYTES = 4 * 1024

_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_STAGING_PREFIX = ".open-flame-ai-runtime-staging-"
_MANIFEST_TEMP_PREFIX = ".open-flame-ai-runtime-manifest-"
_WINDOWS_INVALID_FILENAME_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
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
    }
)
_RESERVED_RUNTIME_PATHS = frozenset(
    {
        MANIFEST_FILE.casefold(),
        WORKER_FILE.casefold(),
        PROTOCOL_FILE.casefold(),
        OPENAI_PROVIDER_FILE.casefold(),
    }
)
_ALLOWED_COMPRESSIONS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


class AiRuntimeBuildError(RuntimeError):
    """Stable, path-free failure raised by the offline builder."""

    _CODES = frozenset(
        {
            "ai_runtime_build_arguments_invalid",
            "ai_runtime_build_archive_changed",
            "ai_runtime_build_archive_hash_mismatch",
            "ai_runtime_build_archive_invalid",
            "ai_runtime_build_archive_too_large",
            "ai_runtime_build_failed",
            "ai_runtime_build_output_exists",
            "ai_runtime_build_output_invalid",
            "ai_runtime_build_platform_unsupported",
            "ai_runtime_build_python_invalid",
            "ai_runtime_build_source_invalid",
            "ai_runtime_build_validation_failed",
        }
    )

    def __init__(self, code: str):
        self.code = code if code in self._CODES else "ai_runtime_build_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _ArchiveEntry:
    info: zipfile.ZipInfo
    relative_path: str
    is_directory: bool


def _is_reparse(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and getattr(info, "st_file_attributes", 0) & reparse)


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
    )


def _windows_component_is_safe(value: str) -> bool:
    if (
        not value
        or value[-1] in {" ", "."}
        or any(character in _WINDOWS_INVALID_FILENAME_CHARACTERS for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        return False
    basename = value.split(".", 1)[0].casefold()
    return basename not in _WINDOWS_RESERVED_BASENAMES


def _is_python_cache_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    filename = parts[-1].casefold()
    return any(part.casefold() == "__pycache__" for part in parts) or filename.endswith(
        (".pyc", ".pyo")
    )


def _absolute_normalized_path(value: str | os.PathLike[str], *, output: bool) -> Path:
    try:
        text = os.fspath(value)
    except TypeError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_arguments_invalid") from exc
    if not isinstance(text, str) or not text or "\x00" in text:
        raise AiRuntimeBuildError("ai_runtime_build_arguments_invalid")
    path = Path(text)
    code = "ai_runtime_build_output_invalid" if output else "ai_runtime_build_archive_invalid"
    if (
        not path.is_absolute()
        or path != Path(os.path.normpath(text))
        or path == Path(path.anchor)
    ):
        raise AiRuntimeBuildError(code)
    if os.name == "nt":
        drive = path.drive
        if (
            len(drive) != 2
            or drive[1] != ":"
            or not drive[0].isalpha()
            or any(not _windows_component_is_safe(part) for part in path.parts[1:])
        ):
            raise AiRuntimeBuildError(code)
    return path


def _plain_directory(path: Path, *, code: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AiRuntimeBuildError(code) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
    ):
        raise AiRuntimeBuildError(code)
    try:
        if path.resolve(strict=True) != path:
            raise AiRuntimeBuildError(code)
    except (OSError, RuntimeError) as exc:
        raise AiRuntimeBuildError(code) from exc
    return info


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_output_invalid") from exc
    return True


def _prepare_output_parent(output: Path) -> None:
    missing: list[Path] = []
    cursor = output.parent
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise AiRuntimeBuildError("ai_runtime_build_output_invalid")
            cursor = parent
        except OSError as exc:
            raise AiRuntimeBuildError("ai_runtime_build_output_invalid") from exc

    ancestor = cursor
    while True:
        _plain_directory(ancestor, code="ai_runtime_build_output_invalid")
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except OSError as exc:
            raise AiRuntimeBuildError("ai_runtime_build_output_invalid") from exc
        _plain_directory(directory, code="ai_runtime_build_output_invalid")
    _plain_directory(output.parent, code="ai_runtime_build_output_invalid")


def _plain_file_info(
    path: Path,
    *,
    code: str,
    maximum: int | None = None,
    allow_empty: bool = True,
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AiRuntimeBuildError(code) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
        or info.st_nlink != 1
        or (not allow_empty and info.st_size == 0)
        or info.st_size < 0
        or (maximum is not None and info.st_size > maximum)
    ):
        raise AiRuntimeBuildError(code)
    return info


@contextmanager
def _verified_archive(
    path: Path, expected_sha256: str
) -> Iterator[tuple[BinaryIO, int]]:
    before = _plain_file_info(
        path,
        code="ai_runtime_build_archive_invalid",
        allow_empty=False,
    )
    if before.st_size > MAX_PYTHON_ARCHIVE_BYTES:
        raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
    try:
        if path.resolve(strict=True) != path:
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
        handle = path.open("rb")
    except AiRuntimeBuildError:
        raise
    except (OSError, RuntimeError) as exc:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid") from exc
    try:
        opened = os.fstat(handle.fileno())
        if _signature(opened) != _signature(before):
            raise AiRuntimeBuildError("ai_runtime_build_archive_changed")
        digest = hashlib.sha256()
        total = 0
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_PYTHON_ARCHIVE_BYTES:
                raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
            digest.update(chunk)
        finished = os.fstat(handle.fileno())
        try:
            after = path.lstat()
        except OSError as exc:
            raise AiRuntimeBuildError("ai_runtime_build_archive_changed") from exc
        if (
            total != before.st_size
            or _signature(finished) != _signature(before)
            or _signature(after) != _signature(before)
        ):
            raise AiRuntimeBuildError("ai_runtime_build_archive_changed")
        if digest.hexdigest() != expected_sha256:
            raise AiRuntimeBuildError("ai_runtime_build_archive_hash_mismatch")
        handle.seek(0)
        yield handle, before.st_size
        final_opened = os.fstat(handle.fileno())
        try:
            final_path = path.lstat()
        except OSError as exc:
            raise AiRuntimeBuildError("ai_runtime_build_archive_changed") from exc
        if (
            _signature(final_opened) != _signature(before)
            or _signature(final_path) != _signature(before)
        ):
            raise AiRuntimeBuildError("ai_runtime_build_archive_changed")
    except AiRuntimeBuildError:
        raise
    except OSError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_archive_changed") from exc
    finally:
        handle.close()


def _archive_relative_path(name: str) -> tuple[str, bool]:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    is_directory = name.endswith("/")
    normalized = name[:-1] if is_directory else name
    if not normalized or normalized.endswith("/"):
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    try:
        relative = relative_runtime_path(normalized)
    except AiProtocolError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid") from exc
    if relative != normalized:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    parts = PurePosixPath(relative).parts
    if (
        any(not _windows_component_is_safe(part) for part in parts)
        or _is_python_cache_path(relative)
    ):
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    return relative, is_directory


def _archive_entries(bundle: zipfile.ZipFile) -> tuple[_ArchiveEntry, ...]:
    try:
        members = bundle.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid") from exc
    if not members:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")

    entries: list[_ArchiveEntry] = []
    member_paths: set[str] = set()
    spellings: dict[str, str] = {}
    path_types: dict[str, bool] = {}
    total = 0
    python_found = False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

    for member in members:
        original_name = getattr(member, "orig_filename", None)
        if (
            not isinstance(original_name, str)
            or "\x00" in original_name
            or original_name != member.filename
        ):
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
        relative, is_directory = _archive_relative_path(member.filename)
        folded = relative.casefold()
        if folded in member_paths:
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
        previous_spelling = spellings.get(folded)
        if previous_spelling is not None and previous_spelling != relative:
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
        if folded in _RESERVED_RUNTIME_PATHS:
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
        if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
        if (
            member.flag_bits & 0x1
            or member.compress_type not in _ALLOWED_COMPRESSIONS
            or member.file_size < 0
            or member.compress_size < 0
            or (reparse_attribute and member.external_attr & reparse_attribute)
        ):
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")

        unix_mode = (member.external_attr >> 16) & 0xFFFF
        unix_type = stat.S_IFMT(unix_mode)
        if unix_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
        if (
            (is_directory and unix_type not in {0, stat.S_IFDIR})
            or (not is_directory and unix_type == stat.S_IFDIR)
            or (is_directory and member.file_size != 0)
        ):
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")

        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            prefix_folded = prefix.casefold()
            previous = spellings.get(prefix_folded)
            if previous is not None and previous != prefix:
                raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
            if path_types.get(prefix_folded) is False:
                raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
            spellings.setdefault(prefix_folded, prefix)
            path_types.setdefault(prefix_folded, True)

        existing_type = path_types.get(folded)
        if existing_type is not None and existing_type != is_directory:
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
        spellings[folded] = relative
        path_types[folded] = is_directory
        member_paths.add(folded)
        if not is_directory:
            total += member.file_size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
            if relative == PYTHON_EXECUTABLE:
                python_found = True
        entries.append(_ArchiveEntry(member, relative, is_directory))

    if not python_found:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    return tuple(entries)


def _destination(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        if not path.parent.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    except (OSError, RuntimeError) as exc:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid") from exc
    return path


def _copy_archive_member(source: BinaryIO, destination: Path, expected_size: int) -> None:
    total = 0
    try:
        with destination.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > expected_size or total > MAX_ARCHIVE_MEMBER_BYTES:
                    raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except AiRuntimeBuildError:
        raise
    except OSError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid") from exc
    if total != expected_size:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid")
    _plain_file_info(destination, code="ai_runtime_build_archive_invalid")


def _extract_archive(stream: BinaryIO, root: Path) -> None:
    try:
        with zipfile.ZipFile(stream, "r") as bundle:
            entries = _archive_entries(bundle)
            directories: set[str] = set()
            for entry in entries:
                parts = PurePosixPath(entry.relative_path).parts
                limit = len(parts) if entry.is_directory else len(parts) - 1
                for index in range(1, limit + 1):
                    directories.add("/".join(parts[:index]))
            for relative in sorted(
                directories,
                key=lambda value: (len(PurePosixPath(value).parts), value.casefold()),
            ):
                destination = root.joinpath(*PurePosixPath(relative).parts)
                try:
                    destination.mkdir()
                except FileExistsError:
                    _plain_directory(
                        destination, code="ai_runtime_build_archive_invalid"
                    )
                except OSError as exc:
                    raise AiRuntimeBuildError("ai_runtime_build_archive_invalid") from exc
                _plain_directory(destination, code="ai_runtime_build_archive_invalid")

            for entry in entries:
                if entry.is_directory:
                    continue
                destination = _destination(root, entry.relative_path)
                with bundle.open(entry.info, "r") as source:
                    _copy_archive_member(source, destination, entry.info.file_size)
    except AiRuntimeBuildError:
        raise
    except (
        EOFError,
        OSError,
        RuntimeError,
        ValueError,
        NotImplementedError,
        zipfile.BadZipFile,
        zlib.error,
    ) as exc:
        raise AiRuntimeBuildError("ai_runtime_build_archive_invalid") from exc


def _copy_package_file(source: Path, destination: Path) -> None:
    before = _plain_file_info(
        source,
        code="ai_runtime_build_source_invalid",
        maximum=MAX_PACKAGE_SOURCE_BYTES,
        allow_empty=False,
    )
    digest_size = 0
    try:
        with source.open("rb") as source_handle, destination.open("xb") as output:
            if _signature(os.fstat(source_handle.fileno())) != _signature(before):
                raise AiRuntimeBuildError("ai_runtime_build_source_invalid")
            while chunk := source_handle.read(1024 * 1024):
                digest_size += len(chunk)
                if digest_size > MAX_PACKAGE_SOURCE_BYTES:
                    raise AiRuntimeBuildError("ai_runtime_build_source_invalid")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            finished = os.fstat(source_handle.fileno())
        after = source.lstat()
    except AiRuntimeBuildError:
        raise
    except OSError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_source_invalid") from exc
    if (
        digest_size != before.st_size
        or _signature(finished) != _signature(before)
        or _signature(after) != _signature(before)
    ):
        raise AiRuntimeBuildError("ai_runtime_build_source_invalid")
    _plain_file_info(destination, code="ai_runtime_build_source_invalid")


def _verify_python(root: Path) -> tuple[int, int, int]:
    executable = root / PYTHON_EXECUTABLE
    before = _plain_file_info(
        executable,
        code="ai_runtime_build_python_invalid",
        allow_empty=False,
    )
    expected_voices = repr(OPENAI_STANDARD_VOICE_IDS)
    script = (
        "import importlib,os,struct,sys;"
        "r=os.path.dirname(sys.executable);"
        "p=importlib.import_module('openai_provider');"
        "w=importlib.import_module('ai_worker');"
        "q=importlib.import_module('ai_protocol');"
        "h=p.handle('health',{},{},lambda *_:None);"
        "f=lambda m,n:callable(getattr(m,n,None)) and "
        "os.path.normcase(os.path.realpath(m.__file__))=="
        "os.path.normcase(os.path.realpath(os.path.join(r,m.__name__+'.py')));"
        "print('%s|%d|%d|%d|%d|%d|%d|%d|%d|%d'%"
        "(sys.implementation.name,sys.version_info.major,sys.version_info.minor,"
        "sys.version_info.micro,struct.calcsize('P')*8,sys.flags.isolated,"
        "f(p,'handle'),f(w,'main'),q.PROTOCOL_SCHEMA,"
        "isinstance(h,dict) and tuple(v.get('id') for v in "
        f"h.get('voices',()))=={expected_voices}))"
    )
    try:
        result = SecureSubprocessRunner(allowed_executable_roots=(root,)).run(
            CommandSpec(
                executable=executable,
                arguments=("-I", "-B", "-c", script),
                cwd=root,
                timeout_seconds=PYTHON_CHECK_TIMEOUT_SECONDS,
                stdout_limit_bytes=PYTHON_CHECK_OUTPUT_BYTES,
                stderr_limit_bytes=PYTHON_CHECK_OUTPUT_BYTES,
            )
        )
    except Exception as exc:
        raise AiRuntimeBuildError("ai_runtime_build_python_invalid") from exc
    after = _plain_file_info(
        executable,
        code="ai_runtime_build_python_invalid",
        allow_empty=False,
    )
    try:
        fields = result.stdout.strip().decode("ascii").split("|")
        implementation = fields[0]
        version = tuple(int(value) for value in fields[1:4])
        bits = int(fields[4])
        isolated = int(fields[5])
        provider_ready = int(fields[6])
        worker_ready = int(fields[7])
        protocol_schema = int(fields[8])
        provider_health_ready = int(fields[9])
    except (IndexError, UnicodeError, ValueError) as exc:
        raise AiRuntimeBuildError("ai_runtime_build_python_invalid") from exc
    if (
        result.returncode != 0
        or result.stderr
        or _signature(after) != _signature(before)
        or len(fields) != 10
        or implementation != "cpython"
        or version[:2] not in {(3, 12), (3, 13)}
        or version[2] < 0
        or bits != 64
        or isolated != 1
        or provider_ready != 1
        or worker_ready != 1
        or protocol_schema != PROTOCOL_SCHEMA
        or provider_health_ready != 1
    ):
        raise AiRuntimeBuildError("ai_runtime_build_python_invalid")
    return version


def _hash_runtime_file(path: Path) -> tuple[int, str]:
    before = _plain_file_info(path, code="ai_runtime_build_validation_failed")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            if _signature(os.fstat(handle.fileno())) != _signature(before):
                raise AiRuntimeBuildError("ai_runtime_build_validation_failed")
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
                digest.update(chunk)
            finished = os.fstat(handle.fileno())
        after = path.lstat()
    except AiRuntimeBuildError:
        raise
    except OSError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_validation_failed") from exc
    if (
        total != before.st_size
        or _signature(finished) != _signature(before)
        or _signature(after) != _signature(before)
    ):
        raise AiRuntimeBuildError("ai_runtime_build_validation_failed")
    return total, digest.hexdigest()


def _artifact_inventory(root: Path) -> dict[str, dict[str, object]]:
    artifacts: dict[str, dict[str, object]] = {}
    total = 0
    count = 0
    def walk_error(error: OSError) -> None:
        raise AiRuntimeBuildError("ai_runtime_build_validation_failed") from error

    for current, directory_names, file_names in os.walk(
        root, topdown=True, onerror=walk_error, followlinks=False
    ):
        current_path = Path(current)
        _plain_directory(current_path, code="ai_runtime_build_validation_failed")
        for directory_name in directory_names:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS + len(_RESERVED_RUNTIME_PATHS):
                raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
            try:
                directory_relative = (
                    (current_path / directory_name).relative_to(root).as_posix()
                )
                directory_relative = relative_runtime_path(directory_relative)
            except (AiProtocolError, ValueError) as exc:
                raise AiRuntimeBuildError("ai_runtime_build_validation_failed") from exc
            if _is_python_cache_path(directory_relative):
                raise AiRuntimeBuildError("ai_runtime_build_validation_failed")
            _plain_directory(
                current_path / directory_name,
                code="ai_runtime_build_validation_failed",
            )
        for file_name in file_names:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS + len(_RESERVED_RUNTIME_PATHS):
                raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
            path = current_path / file_name
            try:
                relative = path.relative_to(root).as_posix()
                canonical = relative_runtime_path(relative)
            except (AiProtocolError, ValueError) as exc:
                raise AiRuntimeBuildError("ai_runtime_build_validation_failed") from exc
            if (
                canonical == MANIFEST_FILE
                or canonical in artifacts
                or _is_python_cache_path(canonical)
            ):
                raise AiRuntimeBuildError("ai_runtime_build_validation_failed")
            size, digest = _hash_runtime_file(path)
            total += size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise AiRuntimeBuildError("ai_runtime_build_archive_too_large")
            artifacts[canonical] = {"size": size, "sha256": digest}
    return {relative: artifacts[relative] for relative in sorted(artifacts)}


def _manifest(
    version: tuple[int, int, int], artifacts: dict[str, dict[str, object]]
) -> dict[str, object]:
    model_ids = [model_id for model_id, _operations in OPENAI_MODELS]
    return {
        "schema": RUNTIME_MANIFEST_SCHEMA,
        "protocol_schema": PROTOCOL_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "runtime_version": RUNTIME_VERSION,
        "platform": PLATFORM,
        "python": {
            "path": PYTHON_EXECUTABLE,
            "implementation": "cpython",
            "version": list(version),
            "bits": 64,
        },
        "worker": WORKER_FILE,
        "protocol": PROTOCOL_FILE,
        "providers": [
            {
                "id": OPENAI_PROVIDER_ID,
                "kind": "remote_plugin",
                "operations": ["transcribe", "translate", "synthesize"],
                "model_ids": model_ids,
                "entrypoint": "openai_provider:handle",
                "endpoint": None,
                "auth_env": OPENAI_AUTH_ENV,
                "timeout_seconds": OPENAI_OPERATION_TIMEOUT_SECONDS,
                "data_egress": ["audio", "text"],
                "artifact_paths": [OPENAI_PROVIDER_FILE],
                "config": {
                    "request_timeout_seconds": OPENAI_REQUEST_TIMEOUT_SECONDS,
                    "standard_voice_ids": list(OPENAI_STANDARD_VOICE_IDS),
                },
            }
        ],
        "models": [
            {
                "id": model_id,
                "operations": list(operations),
                "revision": OPENAI_RUNTIME_REVISION,
                "license": OPENAI_MODEL_TERMS,
                "artifact_paths": [OPENAI_PROVIDER_FILE],
            }
            for model_id, operations in OPENAI_MODELS
        ],
        "artifacts": artifacts,
    }


def _write_manifest_atomic(root: Path, manifest: dict[str, object]) -> None:
    try:
        payload = canonical_json_bytes(manifest)
    except AiProtocolError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_validation_failed") from exc
    if not payload or len(payload) > MAX_MANIFEST_BYTES:
        raise AiRuntimeBuildError("ai_runtime_build_validation_failed")
    temporary = root / f"{_MANIFEST_TEMP_PREFIX}{uuid4().hex}.tmp"
    target = root / MANIFEST_FILE
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise AiRuntimeBuildError("ai_runtime_build_validation_failed") from exc


def _cleanup_staging(path: Path, parent: Path) -> None:
    try:
        if path.parent != parent or not path.name.startswith(_STAGING_PREFIX):
            return
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
        ):
            return
        resolved_parent = parent.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if resolved.parent != resolved_parent or resolved.name != path.name:
            return
        shutil.rmtree(path)
    except (OSError, RuntimeError):
        return


def _rollback_published(
    output: Path,
    staging: Path,
    parent: Path,
    published_signature: tuple[int, int, int, int, int, int] | None,
) -> None:
    if published_signature is None:
        return
    try:
        current = output.lstat()
        if (
            _signature(current) != published_signature
            or not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or _is_reparse(current)
            or staging.exists()
        ):
            return
        os.rename(output, staging)
    except OSError:
        return
    _cleanup_staging(staging, parent)


def _require_windows_x64() -> None:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise AiRuntimeBuildError("ai_runtime_build_platform_unsupported")


def build_ai_runtime(
    python_embed_zip: str | os.PathLike[str],
    python_sha256: str,
    output: str | os.PathLike[str],
) -> AiRuntime:
    """Build a new CPython 3.12/3.13 Windows x64 runtime and validate it."""

    if not isinstance(python_sha256, str) or not _SHA256.fullmatch(python_sha256):
        raise AiRuntimeBuildError("ai_runtime_build_arguments_invalid")
    expected_sha256 = python_sha256.casefold()
    archive = _absolute_normalized_path(python_embed_zip, output=False)
    runtime_root = _absolute_normalized_path(output, output=True)

    staging: Path | None = None
    parent = runtime_root.parent
    published_signature: tuple[int, int, int, int, int, int] | None = None
    published = False
    success = False
    try:
        with _verified_archive(archive, expected_sha256) as (
            archive_stream,
            _archive_size,
        ):
            if _path_exists(runtime_root):
                raise AiRuntimeBuildError("ai_runtime_build_output_exists")
            _require_windows_x64()
            _prepare_output_parent(runtime_root)
            if _path_exists(runtime_root):
                raise AiRuntimeBuildError("ai_runtime_build_output_exists")
            staging = parent / f"{_STAGING_PREFIX}{uuid4().hex}"
            try:
                staging.mkdir()
            except OSError as exc:
                raise AiRuntimeBuildError("ai_runtime_build_output_invalid") from exc
            _plain_directory(staging, code="ai_runtime_build_output_invalid")
            _extract_archive(archive_stream, staging)

        assert staging is not None
        package_root = Path(__file__).resolve(strict=True).parent
        for name in (WORKER_FILE, PROTOCOL_FILE, OPENAI_PROVIDER_FILE):
            _copy_package_file(package_root / name, staging / name)

        python_version = _verify_python(staging)
        artifacts = _artifact_inventory(staging)
        required = {PYTHON_EXECUTABLE, WORKER_FILE, PROTOCOL_FILE, OPENAI_PROVIDER_FILE}
        if not required.issubset(artifacts):
            raise AiRuntimeBuildError("ai_runtime_build_validation_failed")
        _write_manifest_atomic(staging, _manifest(python_version, artifacts))

        if _path_exists(runtime_root):
            raise AiRuntimeBuildError("ai_runtime_build_output_exists")
        published_signature = _signature(
            _plain_directory(staging, code="ai_runtime_build_validation_failed")
        )
        try:
            os.rename(staging, runtime_root)
        except FileExistsError as exc:
            raise AiRuntimeBuildError("ai_runtime_build_output_exists") from exc
        except OSError as exc:
            raise AiRuntimeBuildError("ai_runtime_build_output_invalid") from exc
        published = True
        if _signature(
            _plain_directory(runtime_root, code="ai_runtime_build_validation_failed")
        ) != published_signature:
            raise AiRuntimeBuildError("ai_runtime_build_validation_failed")
        try:
            runtime = load_ai_runtime(runtime_root)
        except AiRuntimeError as exc:
            raise AiRuntimeBuildError("ai_runtime_build_validation_failed") from exc
        success = True
        return runtime
    except AiRuntimeBuildError:
        raise
    except Exception as exc:
        raise AiRuntimeBuildError("ai_runtime_build_failed") from exc
    finally:
        if not success and staging is not None:
            if published:
                _rollback_published(
                    runtime_root,
                    staging,
                    parent,
                    published_signature,
                )
            else:
                _cleanup_staging(staging, parent)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise AiRuntimeBuildError("ai_runtime_build_arguments_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="open-flame-ai-runtime-builder",
        description=(
            "Build an Open-Flame AI runtime from a local CPython 3.12/3.13 "
            "Windows x64 embeddable ZIP."
        ),
    )
    parser.add_argument("--python-embed-zip", required=True)
    parser.add_argument("--python-sha256", required=True)
    parser.add_argument("--output", required=True)
    return parser


def _emit(value: dict[str, object], *, error: bool = False) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr if error else sys.stdout,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        runtime = build_ai_runtime(
            arguments.python_embed_zip,
            arguments.python_sha256,
            arguments.output,
        )
    except AiRuntimeBuildError as exc:
        _emit(
            {
                "ready": False,
                "code": exc.code,
                "runtime_id": RUNTIME_ID,
                "runtime_version": RUNTIME_VERSION,
                "protocol_schema": PROTOCOL_SCHEMA,
            },
            error=True,
        )
        return 2

    status = runtime.status()
    status.update(
        {
            "output": str(runtime.root),
            "manifest_sha256": runtime.manifest_sha256,
            "python": {
                "implementation": runtime.python_identity["implementation"],
                "version": list(runtime.python_identity["version"]),
                "bits": runtime.python_identity["bits"],
            },
        }
    )
    _emit(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["AiRuntimeBuildError", "build_ai_runtime", "main"]

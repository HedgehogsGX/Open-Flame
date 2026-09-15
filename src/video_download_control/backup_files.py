"""Shared file operations for domain-owned backup and restore workflows.

Callers own formats, source selection, database consistency and recovery policy.
This module owns plain paths, file identity, copying, bounded reads, durability,
and SQLite snapshots of the source already selected by a caller.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

from .managed_files import (
    close_binary_on_error,
    discard_created_file,
    fdopen_owned_binary,
)

COPY_CHUNK_BYTES = 1024 * 1024


class BackupRestoreError(RuntimeError):
    """A bounded, operator-safe backup/restore diagnostic."""


@dataclass(frozen=True, slots=True)
class BackupFileEntry:
    path: str
    size_bytes: int
    sha256: str


@contextmanager
def _owned_sqlite_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Close each acquired connection once without replacing an earlier error."""

    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        yield connection
    except BaseException:
        with suppress(BaseException):
            connection.close()
        raise
    else:
        connection.close()


def sqlite_online_backup(source: Path, destination: Path) -> None:
    with (
        _owned_sqlite_connection(source) as source_connection,
        _owned_sqlite_connection(destination) as destination_connection,
    ):
        source_connection.execute("PRAGMA query_only = ON")
        source_connection.backup(destination_connection)
        destination_connection.commit()
        journal_mode = destination_connection.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            raise BackupRestoreError("database snapshot journal mode is unsafe")
        quick = destination_connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or quick[0] != "ok":
            raise BackupRestoreError("database snapshot quick_check failed")
    _sync_file(destination)


def scan_backup_files(root: Path) -> tuple[str, ...]:
    files: list[str] = []

    def walk(directory: Path, relative: PurePosixPath) -> None:
        assert_existing_ancestors_no_links(directory)
        for entry in sorted(os.scandir(directory), key=lambda item: item.name.casefold()):
            path = Path(entry.path)
            info = safe_lstat(path)
            if is_link_or_reparse(path, info):
                raise BackupRestoreError("backup contains a link or reparse point")
            child = relative / entry.name
            if stat.S_ISDIR(info.st_mode):
                walk(path, child)
            elif stat.S_ISREG(info.st_mode):
                files.append(child.as_posix())
            else:
                raise BackupRestoreError("backup contains a special file")

    walk(root, PurePosixPath())
    return tuple(files)


@contextmanager
def _owned_binary_reader(descriptor: int) -> Iterator[BinaryIO]:
    """Transfer a raw descriptor once, preserving any earlier failure on close."""

    handle = fdopen_owned_binary(descriptor, "rb")
    with close_binary_on_error(handle):
        yield handle
    handle.close()


def copy_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> tuple[str, int]:
    assert_existing_ancestors_no_links(source)
    initial = safe_lstat(source)
    if is_link_or_reparse(source, initial) or not stat.S_ISREG(initial.st_mode):
        raise BackupRestoreError("copy source is not a plain regular file")
    if initial.st_nlink != 1:
        raise BackupRestoreError("copy source has multiple filesystem links")
    if expected_size is not None and initial.st_size != expected_size:
        raise BackupRestoreError("copy source size mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    copied = 0
    created: os.stat_result | None = None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise BackupRestoreError("copy source could not be opened safely") from exc
    try:
        with _owned_binary_reader(descriptor) as source_handle:
            opened = os.fstat(source_handle.fileno())
            if not _same_file_identity(initial, opened):
                raise BackupRestoreError("copy source changed before reading")
            try:
                target_handle = destination.open("xb")
            except OSError as exc:
                raise BackupRestoreError("copy destination could not be created") from exc
            with close_binary_on_error(target_handle):
                created = os.fstat(target_handle.fileno())
                while True:
                    chunk = source_handle.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if expected_size is not None and copied > expected_size:
                        raise BackupRestoreError("copy source grew during reading")
                    target_handle.write(chunk)
                    digest.update(chunk)
                final_source = os.fstat(source_handle.fileno())
                if not _same_file_identity(initial, final_source) or copied != initial.st_size:
                    raise BackupRestoreError("copy source changed during reading")
                target_handle.flush()
                os.fsync(target_handle.fileno())
            target_handle.close()
        value = digest.hexdigest()
        if expected_sha256 is not None and value != expected_sha256:
            raise BackupRestoreError("copy source hash mismatch")
        return value, copied
    except BackupRestoreError:
        discard_created_file(destination, created)
        raise
    except OSError as exc:
        discard_created_file(destination, created)
        raise BackupRestoreError("file copy failed") from exc
    except BaseException:
        discard_created_file(destination, created)
        raise


def entry_for_file(path: Path, *, relative_path: str) -> BackupFileEntry:
    info = safe_lstat(path)
    return BackupFileEntry(
        path=validated_relative_path(relative_path),
        size_bytes=info.st_size,
        sha256=sha256_regular_file(path),
    )


def verify_regular_file(
    path: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    assert_existing_ancestors_no_links(path)
    info = safe_lstat(path)
    if is_link_or_reparse(path, info) or not stat.S_ISREG(info.st_mode):
        raise BackupRestoreError("verified path is not a plain regular file")
    if info.st_nlink != 1:
        raise BackupRestoreError("verified file has multiple filesystem links")
    if expected_size is not None and info.st_size != expected_size:
        raise BackupRestoreError("verified file size mismatch")
    if expected_sha256 is not None and sha256_regular_file(path) != expected_sha256:
        raise BackupRestoreError("verified file hash mismatch")


def sha256_regular_file(path: Path) -> str:
    assert_existing_ancestors_no_links(path)
    initial = safe_lstat(path)
    if is_link_or_reparse(path, initial) or not stat.S_ISREG(initial.st_mode):
        raise BackupRestoreError("hash source is not a plain regular file")
    if initial.st_nlink != 1:
        raise BackupRestoreError("hash source has multiple filesystem links")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        digest = hashlib.sha256()
        size = 0
        descriptor = os.open(path, flags)
        with _owned_binary_reader(descriptor) as handle:
            opened = os.fstat(handle.fileno())
            if not _same_file_identity(initial, opened):
                raise BackupRestoreError("hash source changed before reading")
            for chunk in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
                size += len(chunk)
                digest.update(chunk)
            final = os.fstat(handle.fileno())
            if not _same_file_identity(initial, final) or size != initial.st_size:
                raise BackupRestoreError("hash source changed during reading")
        return digest.hexdigest()
    except BackupRestoreError:
        raise
    except OSError as exc:
        raise BackupRestoreError("hash source could not be read") from exc


def read_bounded_regular_file(path: Path, max_bytes: int) -> bytes:
    assert_existing_ancestors_no_links(path)
    info = safe_lstat(path)
    if (
        is_link_or_reparse(path, info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > max_bytes
    ):
        raise BackupRestoreError("backup control file is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with _owned_binary_reader(descriptor) as handle:
            opened = os.fstat(handle.fileno())
            if not _same_file_identity(info, opened):
                raise BackupRestoreError(
                    "backup control file changed before reading"
                )
            value = handle.read(max_bytes + 1)
            final = os.fstat(handle.fileno())
            if not _same_file_identity(info, final):
                raise BackupRestoreError(
                    "backup control file changed during reading"
                )
    except OSError as exc:
        raise BackupRestoreError("backup control file could not be read") from exc
    if len(value) != info.st_size or len(value) > max_bytes:
        raise BackupRestoreError("backup control file changed during reading")
    return value


def read_json_mapping(path: Path, max_bytes: int) -> Mapping[str, Any]:
    raw = read_bounded_regular_file(path, max_bytes)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupRestoreError("JSON control file is invalid") from exc
    if not isinstance(value, dict):
        raise BackupRestoreError("JSON control file must be an object")
    return value


def write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")
    _write_bytes_exclusive(path, encoded)


def write_text_exclusive(path: Path, value: str) -> None:
    _write_bytes_exclusive(path, value.encode("ascii"))


def _write_bytes_exclusive(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BackupRestoreError("backup control file could not be written") from exc


def _sync_file(path: Path) -> None:
    try:
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BackupRestoreError("backup file durability sync failed") from exc


def sync_tree(root: Path) -> None:
    directories = [Path(current) for current, _, _ in os.walk(root, topdown=False)]
    for directory in directories:
        _sync_directory(directory)


def _sync_directory(path: Path) -> None:
    try:
        if os.name == "nt":
            _sync_windows_directory(path)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise BackupRestoreError("durability target is not a directory")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BackupRestoreError:
        raise
    except OSError as exc:
        raise BackupRestoreError("directory durability sync failed") from exc


def _sync_windows_directory(path: Path) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush_file_buffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def validated_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BackupRestoreError("relative path is invalid")
    if "\\" in value or "\x00" in value:
        raise BackupRestoreError("relative path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupRestoreError("relative path escapes its root")
    normalized = path.as_posix()
    if normalized != value or ":" in path.parts[0]:
        raise BackupRestoreError("relative path is not canonical")
    return normalized


def require_existing_directory(path: Path, *, label: str) -> Path:
    absolute = require_absolute_non_root(path, label=label)
    return _canonical_existing_entry(absolute, label=label, directory=True)


def require_existing_regular_file(path: Path, *, label: str) -> Path:
    absolute = require_absolute_non_root(path, label=label)
    return _canonical_existing_entry(absolute, label=label, directory=False)


def require_new_target(path: Path, *, label: str) -> Path:
    absolute = require_absolute_non_root(path, label=label)
    if os.path.lexists(absolute):
        raise BackupRestoreError(f"{label} already exists")
    parent = absolute.parent
    if not os.path.lexists(parent):
        raise BackupRestoreError(f"{label} parent does not exist")
    assert_existing_ancestors_no_links(parent)
    parent_info = safe_lstat(parent)
    if not stat.S_ISDIR(parent_info.st_mode):
        raise BackupRestoreError(f"{label} parent does not exist")
    canonical_parent = _canonical_existing_entry(
        parent,
        label=f"{label} parent",
        directory=True,
    )
    canonical_target = canonical_parent / absolute.name
    if os.path.lexists(absolute) or os.path.lexists(canonical_target):
        raise BackupRestoreError(f"{label} already exists")
    return canonical_target


def _canonical_existing_entry(
    absolute: Path,
    *,
    label: str,
    directory: bool,
) -> Path:
    assert_existing_ancestors_no_links(absolute)
    initial = safe_lstat(absolute)
    _validate_plain_entry(absolute, initial, label=label, directory=directory)
    try:
        canonical = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BackupRestoreError("required filesystem entry is unavailable") from exc

    for candidate in (absolute, canonical):
        assert_existing_ancestors_no_links(candidate)
        current = safe_lstat(candidate)
        _validate_plain_entry(candidate, current, label=label, directory=directory)
        if not _same_path_identity(initial, current):
            raise BackupRestoreError(
                "required filesystem entry changed during validation"
            )
    return canonical


def _validate_plain_entry(
    path: Path,
    info: os.stat_result,
    *,
    label: str,
    directory: bool,
) -> None:
    if directory:
        valid = stat.S_ISDIR(info.st_mode)
        kind = "directory"
    else:
        valid = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        kind = "regular file"
    if is_link_or_reparse(path, info) or not valid:
        raise BackupRestoreError(f"{label} is not a plain {kind}")


def require_absolute_non_root(path: Path, *, label: str) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise BackupRestoreError(f"{label} must be an explicit absolute path")
    absolute = Path(os.path.abspath(path))
    if absolute == Path(absolute.anchor):
        raise BackupRestoreError(f"{label} must not be a filesystem root")
    return absolute


def assert_existing_ancestors_no_links(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            continue
        info = safe_lstat(current)
        if is_link_or_reparse(current, info):
            raise BackupRestoreError("path contains a link or reparse point")


def paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _same_path_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise BackupRestoreError("required filesystem entry is unavailable") from exc


def is_link_or_reparse(path: Path, info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(reparse and attributes & reparse)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(second.st_mode)
        and second.st_nlink == 1
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def cleanup_stage(stage: Path) -> None:
    if not os.path.lexists(stage):
        return
    with suppress(OSError):
        info = stage.lstat()
        if is_link_or_reparse(stage, info):
            if stat.S_ISDIR(info.st_mode):
                stage.rmdir()
            else:
                stage.unlink()
            return
        shutil.rmtree(stage)


def publish_directory(stage: Path, target: Path) -> None:
    try:
        os.replace(stage, target)
    except OSError as exc:
        raise BackupRestoreError("atomic directory publish failed") from exc
    try:
        _sync_directory(target.parent)
    except BackupRestoreError:
        cleanup_stage(target)
        with suppress(BackupRestoreError):
            _sync_directory(target.parent)
        raise

"""Small identity and lstat checks for managed filesystem entries.

Callers keep ownership of domain limits, error mapping, and transactions.
File helpers preserve handle ownership and restrict failed-copy cleanup to
the plain entry created by that operation.
"""
from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, TypeAlias


FileSignature: TypeAlias = tuple[int, int, int, int]
DEFAULT_HASH_CHUNK_BYTES = 1024 * 1024


class UnsafeManagedPath(RuntimeError):
    """A filesystem entry is not an allowed plain file or directory."""


class ManagedFileChanged(RuntimeError):
    """An opened or final handle no longer has the accepted identity."""


class ManagedFileSizeExceeded(RuntimeError):
    """A file grew beyond the caller's hard read limit."""


@dataclass(frozen=True, slots=True)
class OpenedManagedFile:
    """A matching binary handle whose ownership transfers to the caller."""

    handle: BinaryIO
    info: os.stat_result


@dataclass(frozen=True, slots=True)
class BoundedSha256:
    """The digest and optional per-chunk manifest read from one open handle."""

    size: int
    hexdigest: str
    chunk_digests: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class BoundedBytesSnapshot:
    """Immutable bytes and their digest captured in one bounded read."""

    payload: bytes
    size: int
    hexdigest: str


def file_signature(info: os.stat_result) -> FileSignature:
    """Return the stable fields used to detect replacement or mutation."""

    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def is_plain_entry(info: os.stat_result, *, directory: bool = False) -> bool:
    """Classify a stat result without following or consulting its path."""

    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return bool(
        expected
        and not stat.S_ISLNK(info.st_mode)
        and not (getattr(info, "st_file_attributes", 0) & reparse)
        and (directory or info.st_nlink == 1)
    )


def lstat_plain(path: Path, *, directory: bool = False) -> os.stat_result:
    """Return lstat metadata for one plain entry, leaving I/O errors untouched."""

    info = path.lstat()
    if not is_plain_entry(info, directory=directory):
        raise UnsafeManagedPath
    return info


def discard_created_file(path: Path, created: os.stat_result | None) -> None:
    """Best-effort failure cleanup of an exclusively created, still-owned file."""

    if created is None:
        return
    try:
        current = path.lstat()
        if (is_plain_entry(current)
                and current.st_dev == created.st_dev
                and current.st_ino == created.st_ino):
            path.unlink()
    except BaseException:
        # Cleanup must not replace the operation's original failure.
        pass


def require_matching_fstat(
    handle: BinaryIO, *, expected: FileSignature
) -> os.stat_result:
    """Return current handle metadata only when its identity still matches."""

    info = os.fstat(handle.fileno())
    if file_signature(info) != expected:
        raise ManagedFileChanged
    return info


@contextmanager
def close_binary_on_error(handle: BinaryIO) -> Iterator[BinaryIO]:
    """Keep ownership on success; close on failure without replacing its cause."""

    try:
        yield handle
    except BaseException:
        try:
            handle.close()
        except BaseException:
            pass
        raise


def open_matching_binary(
    path: Path, *, expected: FileSignature
) -> OpenedManagedFile:
    """Open a binary file and transfer ownership only after an identity match."""

    handle = path.open("rb")
    with close_binary_on_error(handle):
        info = require_matching_fstat(handle, expected=expected)
    return OpenedManagedFile(handle=handle, info=info)


def _consume_open_binary(
    handle: BinaryIO,
    *,
    maximum: int,
    chunk_size: int,
    collect_chunk_digests: bool,
    snapshot: BytesIO | None,
) -> BoundedSha256:
    if maximum < 0 or chunk_size < 1:
        raise ValueError("invalid managed file read limit")
    digest = hashlib.sha256()
    chunk_digests: list[bytes] = []
    total = 0
    while chunk := handle.read(min(chunk_size, maximum - total + 1)):
        total += len(chunk)
        if total > maximum:
            raise ManagedFileSizeExceeded
        digest.update(chunk)
        if snapshot is not None:
            snapshot.write(chunk)
        if collect_chunk_digests:
            chunk_digests.append(hashlib.sha256(chunk).digest())
    return BoundedSha256(
        size=total,
        hexdigest=digest.hexdigest(),
        chunk_digests=tuple(chunk_digests),
    )


def hash_open_binary(
    handle: BinaryIO,
    *,
    maximum: int,
    chunk_size: int = DEFAULT_HASH_CHUNK_BYTES,
    collect_chunk_digests: bool = False,
) -> BoundedSha256:
    """Hash a blocking file handle with one growth sentinel; never seek or close."""

    return _consume_open_binary(
        handle,
        maximum=maximum,
        chunk_size=chunk_size,
        collect_chunk_digests=collect_chunk_digests,
        snapshot=None,
    )


def snapshot_open_binary(
    handle: BinaryIO,
    *,
    maximum: int,
    chunk_size: int = DEFAULT_HASH_CHUNK_BYTES,
) -> BoundedBytesSnapshot:
    """Capture immutable bytes and their hash in one pass; never seek or close."""

    with BytesIO() as payload:
        hashed = _consume_open_binary(
            handle,
            maximum=maximum,
            chunk_size=chunk_size,
            collect_chunk_digests=False,
            snapshot=payload,
        )
        captured = payload.getvalue()
    return BoundedBytesSnapshot(
        payload=captured,
        size=hashed.size,
        hexdigest=hashed.hexdigest,
    )

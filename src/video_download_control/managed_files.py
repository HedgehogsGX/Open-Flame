"""Small identity and lstat checks for managed filesystem entries.

Callers keep ownership of safe opening, limits, error mapping, and transactional
cleanup.  The only path access here is the explicit, non-following lstat call.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TypeAlias


FileSignature: TypeAlias = tuple[int, int, int, int]


class UnsafeManagedPath(RuntimeError):
    """A filesystem entry is not an allowed plain file or directory."""


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

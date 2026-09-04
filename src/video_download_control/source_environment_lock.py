"""Coordinate source-launcher readers with the source installer's writer."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat
from collections.abc import Iterator


class SourceEnvironmentBusy(OSError):
    """The source environment is unavailable or another operation owns it."""


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


@contextmanager
def source_environment_lock(repository: Path, *, exclusive: bool) -> Iterator[None]:
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    overlapped = _Overlapped()
    path = repository / ".open-flame-setup.lock"
    descriptor = None
    locked = False
    try:
        if path.is_symlink():
            raise SourceEnvironmentBusy("source environment unavailable")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_BINARY, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise SourceEnvironmentBusy("source environment unavailable")
        handle = msvcrt.get_osfhandle(descriptor)
        # CRT LK_NBRLCK is the same exclusive lock as LK_NBLCK on Windows.
        # LockFileEx without EXCLUSIVE_LOCK is the actual shared-reader API.
        if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
            raise SourceEnvironmentBusy("source environment unavailable")
        locked = True
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise SourceEnvironmentBusy("source environment busy or unavailable") from None
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        yield
    finally:
        try:
            if locked:
                kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        finally:
            os.close(descriptor)


__all__ = ["SourceEnvironmentBusy", "source_environment_lock"]

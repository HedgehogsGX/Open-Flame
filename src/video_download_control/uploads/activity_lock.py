"""Cross-process reader/writer lock for one upload data root.

Current application and service activity takes a shared lease.  Offline backup
and restore take an exclusive lease, which gives those operations one durable
quiescence boundary without relying on a racy process-status check.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat
import threading


class UploadActivityBusy(OSError):
    """The upload root activity lease is busy or cannot be trusted."""


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def activity_lock_path(root: Path) -> Path:
    """Return the persistent lock adjacent to ``root``.

    Keeping the coordination file beside the data root lets the application
    hold a lifecycle lease without eagerly creating the upload database, and
    lets restore lock a destination before its atomic directory publication.
    """

    normalized = canonical_activity_root(root)
    return normalized.parent / f".{normalized.name}.activity.lock"


def _is_link_or_reparse(path: Path, info: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or path.is_symlink()
    )


def _validate_parent(path: Path) -> None:
    parent = path.parent
    for ancestor in reversed((parent, *parent.parents)):
        try:
            info = ancestor.lstat()
        except OSError as exc:
            raise UploadActivityBusy("upload activity lock is unavailable") from exc
        if _is_link_or_reparse(ancestor, info) or not stat.S_ISDIR(info.st_mode):
            raise UploadActivityBusy("upload activity lock parent is unsafe")


def canonical_activity_root(root: Path) -> Path:
    """Canonicalize an existing root or the existing parent of a new root."""

    normalized = Path(os.path.abspath(root))
    if not normalized.name:
        raise UploadActivityBusy("upload activity root is invalid")
    _validate_parent(normalized)
    try:
        info = normalized.lstat()
    except FileNotFoundError:
        try:
            canonical_parent = normalized.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UploadActivityBusy("upload activity root is unavailable") from exc
        return canonical_parent / normalized.name
    except OSError as exc:
        raise UploadActivityBusy("upload activity root is unavailable") from exc
    if _is_link_or_reparse(normalized, info):
        raise UploadActivityBusy("upload activity root is unsafe")
    try:
        return normalized.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UploadActivityBusy("upload activity root is unavailable") from exc


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_nlink == second.st_nlink
        and first.st_size == second.st_size
    )


def _validate_open_file(descriptor: int, path: Path) -> None:
    opened = os.fstat(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise UploadActivityBusy("upload activity lock is unavailable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size != 0
        or _is_link_or_reparse(path, current)
        or not _same_identity(opened, current)
    ):
        raise UploadActivityBusy("upload activity lock is unsafe")


def _open_lock_file(path: Path) -> int:
    _validate_parent(path)
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise UploadActivityBusy("upload activity lock is unavailable") from exc
    try:
        os.set_inheritable(descriptor, False)
        _validate_open_file(descriptor, path)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _windows_lock(descriptor: int, *, exclusive: bool):
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    overlapped = _Overlapped()
    handle = msvcrt.get_osfhandle(descriptor)
    flags = 1 | (2 if exclusive else 0)  # FAIL_IMMEDIATELY | EXCLUSIVE_LOCK
    if not kernel.LockFileEx(
        handle, flags, 0, 1, 0, ctypes.byref(overlapped)
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "upload activity lock is busy")
    return kernel, handle, overlapped


def _platform_lock(descriptor: int, *, exclusive: bool):
    if os.name == "nt":
        return _windows_lock(descriptor, exclusive=exclusive)
    import fcntl

    fcntl.flock(
        descriptor,
        (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
    )
    return None


def _platform_unlock(descriptor: int, token) -> None:
    if os.name == "nt":
        kernel, handle, overlapped = token
        kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


class UploadActivityLease:
    """One held shared or exclusive activity lease."""

    def __init__(self, descriptor: int, token) -> None:
        self._descriptor: int | None = descriptor
        self._token = token
        self._release_guard = threading.Lock()

    @classmethod
    def acquire(cls, root: Path, *, exclusive: bool) -> UploadActivityLease:
        path = activity_lock_path(root)
        descriptor = _open_lock_file(path)
        token = None
        locked = False
        try:
            token = _platform_lock(descriptor, exclusive=exclusive)
            locked = True
            _validate_open_file(descriptor, path)
            if activity_lock_path(root) != path:
                raise UploadActivityBusy("upload activity root changed while locking")
            return cls(descriptor, token)
        except BaseException as exc:
            if locked:
                with suppress(OSError):
                    _platform_unlock(descriptor, token)
            os.close(descriptor)
            if isinstance(exc, UploadActivityBusy):
                raise
            if isinstance(exc, OSError):
                raise UploadActivityBusy("upload activity lock is busy") from exc
            raise

    def release(self) -> None:
        with self._release_guard:
            descriptor = self._descriptor
            if descriptor is None:
                return
            self._descriptor = None
            try:
                with suppress(OSError):
                    _platform_unlock(descriptor, self._token)
            finally:
                os.close(descriptor)

    def __enter__(self) -> UploadActivityLease:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


@contextmanager
def upload_activity_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    lease = UploadActivityLease.acquire(root, exclusive=exclusive)
    try:
        yield
    finally:
        lease.release()


__all__ = [
    "UploadActivityBusy",
    "UploadActivityLease",
    "activity_lock_path",
    "canonical_activity_root",
    "upload_activity_lock",
]

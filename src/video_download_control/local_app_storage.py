"""Shared Windows application-root validation, creation and lifetime lock."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import stat


class LocalAppStorageError(OSError):
    """The local application root or its lock is unsafe or unavailable."""


class LocalAppLockUnavailable(LocalAppStorageError):
    """The application-root lifetime lock is held or unavailable."""


def validate_windows_local_path(path: object) -> Path:
    """Reject aliases that can escape lexical Windows path comparisons."""

    try:
        if not isinstance(path, Path):
            raise ValueError
        text = str(path)
        if (
            not path.is_absolute()
            or Path(os.path.abspath(path)) != path
            or path == Path(path.anchor)
            or text.startswith(("\\\\", "\\\\?\\", "\\\\.\\"))
            or "\x00" in text
        ):
            raise ValueError
        reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
        reserved.update(f"COM{index}" for index in range(1, 10))
        reserved.update(f"LPT{index}" for index in range(1, 10))
        for component in path.parts[1:]:
            if (
                not component
                or component in {".", ".."}
                or component.endswith((" ", "."))
                or ":" in component
                or any(ord(character) < 32 for character in component)
                or component.split(".", 1)[0].upper() in reserved
            ):
                raise ValueError
        return path
    except (OSError, TypeError, ValueError):
        raise LocalAppStorageError("local application path is invalid") from None


def default_local_app_root(
    environment: Mapping[str, str],
) -> Path:
    raw = next(
        (value for key, value in environment.items() if key.upper() == "LOCALAPPDATA"),
        None,
    )
    if not isinstance(raw, str) or not raw:
        raise LocalAppStorageError("local application root is unavailable")
    return validate_windows_local_path(Path(raw)) / "Open-Flame" / "video-download-control"


def plain_directory_info(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise LocalAppStorageError("local application storage is unavailable") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(reparse and attributes & reparse)
    ):
        raise LocalAppStorageError("local application storage is unavailable")
    return info


def ensure_plain_directory_tree(path: Path) -> None:
    current = Path(path.anchor)
    try:
        plain_directory_info(current)
        for part in path.parts[1:]:
            current /= part
            try:
                plain_directory_info(current)
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                plain_directory_info(current)
    except LocalAppStorageError:
        raise
    except OSError as exc:
        raise LocalAppStorageError("local application storage is unavailable") from exc


def reject_existing_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LocalAppStorageError("local application storage is unavailable") from exc
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or bool(reparse and attributes & reparse):
            raise LocalAppStorageError("local application storage is unavailable")


@contextmanager
def exclusive_local_app(app_root: Path) -> Iterator[None]:
    """Hold the application-root byte-range lock for one bounded operation."""

    lock_path = app_root / ".local-app.lock"
    descriptor: int | None = None
    locked = False
    try:
        reject_existing_link_components(lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        info = os.fstat(descriptor)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or bool(reparse and attributes & reparse)
        ):
            raise OSError
        if info.st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        locked = True
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise LocalAppLockUnavailable("local application lock unavailable") from None
    try:
        yield
    finally:
        if descriptor is not None:
            if locked:
                try:
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass


__all__ = [
    "LocalAppLockUnavailable",
    "LocalAppStorageError",
    "default_local_app_root",
    "ensure_plain_directory_tree",
    "exclusive_local_app",
    "plain_directory_info",
    "reject_existing_link_components",
    "validate_windows_local_path",
]

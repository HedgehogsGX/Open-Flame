"""Attempt-private Cookie preparation shared by supported real Workers.

The configured files are deployment-owned, read-only inputs.  A downloader
never receives those paths directly: every resolver call copies one source
through an already-open descriptor into a new 0600 file below the immutable
Attempt directory.  Errors intentionally contain neither credential
references, paths, nor file contents.
"""

from __future__ import annotations

import hmac
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from .adapters.base import AdapterContext
from .adapters.yt_dlp_contract import CookieMount
from .domain import Platform

DEFAULT_MAX_COOKIE_BYTES = 8 * 1024 * 1024
_CREDENTIAL_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_GENERIC_PREPARATION_ERROR = "credential cookie could not be prepared safely"


class CookiePreparationError(RuntimeError):
    """A deliberately secret-free credential preparation failure."""


@dataclass(frozen=True, slots=True)
class CookieSource:
    """One opaque credential reference mapped to one platform-owned file."""

    platform: Platform
    credential_ref: str = field(repr=False)
    path: Path = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.platform, Platform):
            raise TypeError("cookie source platform must be explicit")
        if not _CREDENTIAL_REFERENCE.fullmatch(self.credential_ref):
            raise ValueError("credential reference must be a short opaque token")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("cookie source path must be absolute")
        if Path(os.path.abspath(self.path)) != self.path:
            raise ValueError("cookie source path must be normalized")


class AttemptCookieResolver:
    """Copy configured cookies into a fresh file for each adapter operation.

    Only one credential profile per platform is accepted by each Worker process.
    The Worker passes only the claim-validated opaque ``secret_ref`` as
    ``credential_ref``; database IDs, mounted paths and cookie contents never
    cross the adapter request boundary.
    """

    def __init__(
        self,
        sources: tuple[CookieSource, ...] = (),
        *,
        max_cookie_bytes: int = DEFAULT_MAX_COOKIE_BYTES,
    ) -> None:
        if (
            isinstance(max_cookie_bytes, bool)
            or not isinstance(max_cookie_bytes, int)
            or not 1 <= max_cookie_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("cookie byte limit must be between 1 and 67108864")
        by_platform: dict[Platform, CookieSource] = {}
        for source in sources:
            if not isinstance(source, CookieSource):
                raise TypeError("cookie sources must use CookieSource")
            if source.platform in by_platform:
                raise ValueError("only one cookie source is allowed per platform")
            by_platform[source.platform] = source
        self._sources: Mapping[Platform, CookieSource] = MappingProxyType(by_platform)
        self.max_cookie_bytes = max_cookie_bytes

    @property
    def configured_platforms(self) -> tuple[Platform, ...]:
        return tuple(sorted(self._sources, key=lambda item: item.value))

    def __repr__(self) -> str:
        platforms = ",".join(item.value for item in self.configured_platforms)
        return (
            "AttemptCookieResolver("
            f"configured_platforms=({platforms}), "
            f"max_cookie_bytes={self.max_cookie_bytes})"
        )

    def validate_sources(self) -> None:
        """Fail startup without copying or exposing a configured source."""

        failed = False
        try:
            self._validate_sources()
        except Exception:  # noqa: BLE001 - public boundary removes context
            failed = True
        if failed:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

    def _validate_sources(self) -> None:
        opened_identities: set[tuple[int, int]] = set()
        for source in self._sources.values():
            descriptor = -1
            try:
                before = self._source_lstat(source.path)
                descriptor = self._open_source(source.path)
                opened = os.fstat(descriptor)
                self._validate_source_info(opened)
                self._require_same_identity(before, opened)
                if opened.st_size <= 0 or opened.st_size > self.max_cookie_bytes:
                    raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
                after = self._source_lstat(source.path)
                self._require_stable_source(before, opened, after)
                opened_identity = (opened.st_dev, opened.st_ino)
                if opened_identity in opened_identities:
                    raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
                opened_identities.add(opened_identity)
            except CookiePreparationError:
                raise
            except (OSError, ValueError, TypeError) as exc:
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def __call__(
        self,
        platform: Platform,
        credential_ref: str,
        context: AdapterContext,
    ) -> CookieMount:
        prepared: CookieMount | None = None
        try:
            prepared = self._prepare(platform, credential_ref, context)
        except Exception:  # noqa: BLE001 - public boundary removes context
            prepared = None
        if prepared is None:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
        return prepared

    def _prepare(
        self,
        platform: Platform,
        credential_ref: str,
        context: AdapterContext,
    ) -> CookieMount:
        source = self._sources.get(platform)
        if (
            source is None
            or not isinstance(credential_ref, str)
            or not hmac.compare_digest(source.credential_ref, credential_ref)
        ):
            raise CookiePreparationError("credential reference is not available")

        source_descriptor = -1
        target_descriptor = -1
        directory_descriptor = -1
        target_path: Path | None = None
        target_identity: tuple[int, int] | None = None
        target_filename: str | None = None
        cleanup_owned_target = False
        try:
            attempt_root = self._validated_attempt_root(context.temporary_dir)
            before = self._source_lstat(source.path)
            source_descriptor = self._open_source(source.path)
            opened = os.fstat(source_descriptor)
            self._validate_source_info(opened)
            self._require_same_identity(before, opened)
            if opened.st_size <= 0 or opened.st_size > self.max_cookie_bytes:
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

            secrets_root, secrets_info = self._prepare_secrets_root(attempt_root)
            target_filename = f"{platform.value}-{uuid4().hex}.cookies.txt"
            target_path = secrets_root / target_filename
            (
                target_descriptor,
                directory_descriptor,
            ) = self._create_private_target(
                secrets_root,
                target_filename,
                expected_directory=secrets_info,
            )
            target_opened = os.fstat(target_descriptor)
            target_identity = (target_opened.st_dev, target_opened.st_ino)
            self._validate_target_info(target_opened, expected_size=0)

            copied = self._copy_bounded(
                source_descriptor,
                target_descriptor,
                maximum=self.max_cookie_bytes,
            )
            if copied != opened.st_size:
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
            os.fsync(target_descriptor)

            final_source = os.fstat(source_descriptor)
            after = self._source_lstat(source.path)
            self._require_stable_source(before, final_source, after)
            final_target = os.fstat(target_descriptor)
            self._validate_target_info(final_target, expected_size=copied)
            self._require_target_path_identity(target_path, final_target)
            if directory_descriptor >= 0:
                final_directory = os.fstat(directory_descriptor)
                self._validate_directory_info(final_directory, private=True)
                self._require_same_identity(secrets_info, final_directory)
                self._require_directory_path_identity(
                    secrets_root,
                    final_directory,
                    private=True,
                )
                os.fsync(directory_descriptor)

            return CookieMount(platform=platform, path=target_path)
        except CookiePreparationError:
            cleanup_owned_target = True
            raise
        except (OSError, ValueError, TypeError) as exc:
            cleanup_owned_target = True
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        finally:
            if (
                cleanup_owned_target
                and directory_descriptor >= 0
                and target_filename is not None
                and target_identity is not None
            ):
                self._remove_owned_target_at(
                    directory_descriptor,
                    target_filename,
                    target_identity,
                )
            for descriptor in (
                target_descriptor,
                directory_descriptor,
                source_descriptor,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if cleanup_owned_target:
                self._remove_owned_target(target_path, target_identity)

    @staticmethod
    def _validated_attempt_root(path: Path) -> Path:
        if not isinstance(path, Path) or not path.is_absolute():
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
        AttemptCookieResolver._reject_link_components(path)
        try:
            resolved = path.resolve(strict=True)
            info = resolved.lstat()
        except OSError as exc:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        if not stat.S_ISDIR(info.st_mode) or AttemptCookieResolver._is_reparse(info):
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
        AttemptCookieResolver._validate_directory_info(info, private=False)
        parent = resolved.parent
        AttemptCookieResolver._reject_link_components(parent)
        try:
            parent_info = parent.lstat()
        except OSError as exc:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        AttemptCookieResolver._validate_directory_info(parent_info, private=False)
        AttemptCookieResolver._validate_owned_ancestor_chain(parent)
        return resolved

    @staticmethod
    def _prepare_secrets_root(attempt_root: Path) -> tuple[Path, os.stat_result]:
        secrets_root = attempt_root / "secrets"
        try:
            secrets_root.mkdir(mode=0o700, exist_ok=True)
            info = secrets_root.lstat()
        except OSError as exc:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        AttemptCookieResolver._validate_directory_info(info, private=True)
        try:
            resolved = secrets_root.resolve(strict=True)
            resolved_info = resolved.lstat()
        except OSError as exc:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        if resolved.parent != attempt_root:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
        AttemptCookieResolver._validate_directory_info(resolved_info, private=True)
        AttemptCookieResolver._require_same_identity(info, resolved_info)
        return resolved, resolved_info

    @staticmethod
    def _reject_link_components(path: Path) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except OSError as exc:
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
            if stat.S_ISLNK(info.st_mode) or AttemptCookieResolver._is_reparse(info):
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

    @staticmethod
    def _source_lstat(path: Path):
        AttemptCookieResolver._reject_link_components(path)
        AttemptCookieResolver._validate_owned_ancestor_chain(path.parent)
        try:
            info = path.lstat()
        except OSError as exc:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        AttemptCookieResolver._validate_source_info(info)
        return info

    @staticmethod
    def _validate_owned_ancestor_chain(path: Path) -> None:
        """Reject replaceable POSIX ancestors while allowing root sticky temp roots."""

        if os.name != "posix":
            return
        effective_uid = os.geteuid()
        current = path
        while True:
            try:
                info = current.lstat()
            except OSError as exc:
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
            mode = info.st_mode
            if (
                not stat.S_ISDIR(mode)
                or stat.S_ISLNK(mode)
                or AttemptCookieResolver._is_reparse(info)
                or info.st_uid not in {0, effective_uid}
            ):
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
            permissions = stat.S_IMODE(mode)
            writable_by_others = bool(permissions & 0o022)
            root_sticky = info.st_uid == 0 and bool(permissions & stat.S_ISVTX)
            if writable_by_others and not root_sticky:
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
            if current.parent == current:
                return
            current = current.parent

    @staticmethod
    def _validate_source_info(info: object) -> None:
        mode = getattr(info, "st_mode", 0)
        if (
            not stat.S_ISREG(mode)
            or stat.S_ISLNK(mode)
            or AttemptCookieResolver._is_reparse(info)
            or getattr(info, "st_nlink", 0) != 1
            or mode & 0o222
        ):
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

    @staticmethod
    def _open_source(path: Path) -> int:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        return os.open(path, flags)

    @staticmethod
    def _create_private_target(
        secrets_root: Path,
        filename: str,
        *,
        expected_directory: object,
    ) -> tuple[int, int]:
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_BINARY", 0)
        directory_descriptor = -1
        if os.name == "posix":
            directory_flags = os.O_RDONLY
            directory_flags |= getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_descriptor = os.open(secrets_root, directory_flags)
            try:
                opened_directory = os.fstat(directory_descriptor)
                AttemptCookieResolver._validate_directory_info(
                    opened_directory,
                    private=True,
                )
                AttemptCookieResolver._require_same_identity(
                    expected_directory,
                    opened_directory,
                )
                AttemptCookieResolver._require_directory_path_identity(
                    secrets_root,
                    opened_directory,
                    private=True,
                )
                target_descriptor = os.open(
                    filename,
                    file_flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except Exception:
                os.close(directory_descriptor)
                raise
        else:
            target_descriptor = os.open(
                secrets_root / filename,
                file_flags,
                0o600,
            )
        try:
            os.fchmod(target_descriptor, 0o600)
        except AttributeError:
            pass
        return target_descriptor, directory_descriptor

    @staticmethod
    def _validate_directory_info(info: object, *, private: bool) -> None:
        mode = getattr(info, "st_mode", 0)
        if (
            not stat.S_ISDIR(mode)
            or stat.S_ISLNK(mode)
            or AttemptCookieResolver._is_reparse(info)
        ):
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
        if os.name == "posix":
            if getattr(info, "st_uid", -1) != os.geteuid():
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
            permissions = stat.S_IMODE(mode)
            if (private and permissions != 0o700) or (
                not private and permissions & 0o022
            ):
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

    @staticmethod
    def _require_directory_path_identity(
        path: Path,
        opened: object,
        *,
        private: bool,
    ) -> None:
        try:
            observed = path.lstat()
        except OSError as exc:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        AttemptCookieResolver._validate_directory_info(observed, private=private)
        AttemptCookieResolver._require_same_identity(opened, observed)

    @staticmethod
    def _copy_bounded(source: int, target: int, *, maximum: int) -> int:
        copied = 0
        while True:
            chunk = os.read(source, min(64 * 1024, maximum - copied + 1))
            if not chunk:
                return copied
            copied += len(chunk)
            if copied > maximum:
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
            view = memoryview(chunk)
            while view:
                written = os.write(target, view)
                if written <= 0:
                    raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
                view = view[written:]

    @staticmethod
    def _validate_target_info(info: object, *, expected_size: int) -> None:
        mode = getattr(info, "st_mode", 0)
        if (
            not stat.S_ISREG(mode)
            or stat.S_ISLNK(mode)
            or AttemptCookieResolver._is_reparse(info)
            or getattr(info, "st_nlink", 0) != 1
            or getattr(info, "st_size", -1) != expected_size
        ):
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)
        if os.name == "posix" and mode & 0o777 != 0o600:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

    @staticmethod
    def _require_same_identity(first: object, second: object) -> None:
        if getattr(first, "st_dev", None) != getattr(second, "st_dev", None) or getattr(
            first, "st_ino", None
        ) != getattr(second, "st_ino", None):
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

    @staticmethod
    def _require_stable_source(
        before: object,
        opened: object,
        after: object,
    ) -> None:
        AttemptCookieResolver._validate_source_info(opened)
        AttemptCookieResolver._validate_source_info(after)
        AttemptCookieResolver._require_same_identity(before, opened)
        AttemptCookieResolver._require_same_identity(opened, after)
        stable_fields = ["st_size", "st_mtime_ns"]
        # Windows may expose different creation-time precision for lstat and
        # fstat on the same handle.  The candidate runtime is Linux, where
        # ctime gives an additional mutation signal.
        if os.name == "posix":
            stable_fields.append("st_ctime_ns")
        for field_name in stable_fields:
            if not (
                getattr(before, field_name, None)
                == getattr(opened, field_name, None)
                == getattr(after, field_name, None)
            ):
                raise CookiePreparationError(_GENERIC_PREPARATION_ERROR)

    @staticmethod
    def _require_target_path_identity(path: Path, opened: object) -> None:
        try:
            observed = path.lstat()
        except OSError as exc:
            raise CookiePreparationError(_GENERIC_PREPARATION_ERROR) from exc
        AttemptCookieResolver._validate_target_info(
            observed,
            expected_size=getattr(opened, "st_size", -1),
        )
        AttemptCookieResolver._require_same_identity(opened, observed)

    @staticmethod
    def _remove_owned_target(
        path: Path | None,
        identity: tuple[int, int] | None,
    ) -> None:
        if path is None or identity is None:
            return
        try:
            observed = path.lstat()
            if (observed.st_dev, observed.st_ino) == identity:
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _remove_owned_target_at(
        directory_descriptor: int,
        filename: str,
        identity: tuple[int, int],
    ) -> None:
        """Remove only the file created through the already-attested directory FD."""

        try:
            observed = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (observed.st_dev, observed.st_ino) == identity:
                os.unlink(filename, dir_fd=directory_descriptor)
        except OSError:
            pass

    @staticmethod
    def _is_reparse(info: object) -> bool:
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        return bool(reparse and attributes & reparse)


__all__ = [
    "DEFAULT_MAX_COOKIE_BYTES",
    "AttemptCookieResolver",
    "CookiePreparationError",
    "CookieSource",
]

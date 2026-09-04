"""Strict, bounded Cookie-source configuration for local process hand-off.

The JSON file contains only platform, opaque credential reference and source
path mappings.  It is opened through a descriptor and revalidated before use;
public failures intentionally omit the file path, references and contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .candidate_cookies import AttemptCookieResolver, CookieSource
from .domain import Platform

COOKIE_SOURCE_CONFIG_SCHEMA_VERSION = 2
DEFAULT_MAX_COOKIE_SOURCE_CONFIG_BYTES = 64 * 1024
_GENERIC_CONFIG_ERROR = "cookie source configuration is invalid"


class CookieSourceConfigError(ValueError):
    """A deliberately path- and credential-free configuration failure."""


@dataclass(frozen=True, slots=True)
class CookieSourceConfig:
    """One stable configuration snapshot safe to pass over private IPC."""

    path: Path = field(repr=False)
    sources: tuple[CookieSource, ...] = field(repr=False)
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str = field(repr=False)
    default_cookie_platforms: tuple[Platform, ...] = ()


def load_cookie_source_config(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_COOKIE_SOURCE_CONFIG_BYTES,
) -> CookieSourceConfig:
    """Load a strict JSON mapping without leaking private diagnostics."""

    loaded: CookieSourceConfig | None = None
    try:
        loaded = _load_cookie_source_config(path, max_bytes=max_bytes)
    except Exception:  # noqa: BLE001 - public boundary deliberately removes context
        loaded = None
    if loaded is None:
        raise CookieSourceConfigError(_GENERIC_CONFIG_ERROR) from None
    return loaded


def revalidate_cookie_source_config(config: CookieSourceConfig) -> None:
    """Require the file and parsed mappings to match the captured snapshot."""

    valid = False
    try:
        if not isinstance(config, CookieSourceConfig):
            raise TypeError
        observed = _load_cookie_source_config(
            config.path,
            max_bytes=max(config.size, 1),
        )
        valid = observed == config
    except Exception:  # noqa: BLE001 - public boundary deliberately removes context
        valid = False
    if not valid:
        raise CookieSourceConfigError(_GENERIC_CONFIG_ERROR) from None


def _load_cookie_source_config(path: Path, *, max_bytes: int) -> CookieSourceConfig:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= 1024 * 1024
    ):
        raise ValueError
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or Path(os.path.abspath(path)) != path
    ):
        raise ValueError

    descriptor = -1
    try:
        _reject_link_components(path)
        before = path.lstat()
        _validate_file_info(before)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
        )
        opened = os.fstat(descriptor)
        _validate_file_info(opened)
        _require_same_identity(before, opened)
        if not 1 <= opened.st_size <= max_bytes:
            raise ValueError
        payload = _read_bounded(descriptor, maximum=max_bytes)
        final_opened = os.fstat(descriptor)
        after = path.lstat()
        _require_stable(before, opened, final_opened, after)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    document = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
    )
    if not isinstance(document, dict):
        raise ValueError
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError
    expected_keys = {"schema_version", "cookie_sources"}
    if schema_version == 2:
        expected_keys.add("default_cookie_platforms")
    if set(document) != expected_keys:
        raise ValueError
    entries = document["cookie_sources"]
    if (
        not isinstance(entries, list)
        or len(entries) > len(Platform)
    ):
        raise ValueError

    sources: list[CookieSource] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "platform",
            "credential_ref",
            "path",
        }:
            raise ValueError
        if not all(isinstance(entry[key], str) for key in entry):
            raise ValueError
        source_path = Path(entry["path"])
        sources.append(
            CookieSource(
                platform=Platform(entry["platform"]),
                credential_ref=entry["credential_ref"],
                path=source_path,
            )
        )
    normalized_sources = tuple(sorted(sources, key=lambda item: item.platform.value))
    AttemptCookieResolver(normalized_sources)
    if any(source.path == path for source in normalized_sources):
        raise ValueError
    default_entries = document.get("default_cookie_platforms", [])
    if (
        not isinstance(default_entries, list)
        or len(default_entries) > len(Platform)
        or not all(isinstance(value, str) for value in default_entries)
    ):
        raise ValueError
    default_platforms = tuple(Platform(value) for value in default_entries)
    if (
        len(default_platforms) != len(set(default_platforms))
        or not set(default_platforms) <= {source.platform for source in normalized_sources}
    ):
        raise ValueError
    return CookieSourceConfig(
        path=path,
        sources=normalized_sources,
        device=opened.st_dev,
        inode=opened.st_ino,
        size=opened.st_size,
        modified_ns=opened.st_mtime_ns,
        sha256=hashlib.sha256(payload).hexdigest(),
        default_cookie_platforms=tuple(sorted(default_platforms, key=lambda item: item.value)),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise ValueError


def _validate_file_info(info: object) -> None:
    mode = getattr(info, "st_mode", 0)
    if (
        not stat.S_ISREG(mode)
        or stat.S_ISLNK(mode)
        or _is_reparse(info)
        or getattr(info, "st_nlink", 0) != 1
        or mode & 0o222
    ):
        raise ValueError


def _read_bounded(descriptor: int, *, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum:
            raise ValueError
        chunks.append(chunk)


def _require_same_identity(first: object, second: object) -> None:
    if (
        getattr(first, "st_dev", None) != getattr(second, "st_dev", None)
        or getattr(first, "st_ino", None) != getattr(second, "st_ino", None)
    ):
        raise ValueError


def _require_stable(*states: object) -> None:
    first = states[0]
    for state in states:
        _validate_file_info(state)
        _require_same_identity(first, state)
        if (
            getattr(state, "st_size", None) != getattr(first, "st_size", None)
            or getattr(state, "st_mtime_ns", None)
            != getattr(first, "st_mtime_ns", None)
        ):
            raise ValueError


def _is_reparse(info: object) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(reparse and attributes & reparse)


__all__ = [
    "COOKIE_SOURCE_CONFIG_SCHEMA_VERSION",
    "DEFAULT_MAX_COOKIE_SOURCE_CONFIG_BYTES",
    "CookieSourceConfig",
    "CookieSourceConfigError",
    "load_cookie_source_config",
    "revalidate_cookie_source_config",
]

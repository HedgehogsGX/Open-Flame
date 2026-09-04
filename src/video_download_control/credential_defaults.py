"""Explicit, run-scoped Cookie defaults with transaction-local Job binding.

Only profile IDs are persisted on Jobs; no database row stores an automatic
default. A new application run must opt in again through its private config.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from .candidate_cookies import AttemptCookieResolver, DEFAULT_MAX_COOKIE_BYTES
from .cookie_source_config import CookieSourceConfig, revalidate_cookie_source_config
from .credentials import validate_opaque_reference
from .database import Database
from .domain import Platform


CredentialMode = Literal["use_default", "anonymous"]
_ERROR = "configured credential defaults are unavailable"
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")


class CredentialDefaultsError(ValueError):
    """Fixed public failure; never carries profile identity or source details."""


@dataclass(frozen=True, slots=True)
class _CredentialBinding:
    platform: Platform
    profile_id: str = field(repr=False)
    credential_ref: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CredentialDefaults:
    run_id: str
    bindings: tuple[_CredentialBinding, ...] = field(repr=False)
    config: CookieSourceConfig = field(repr=False)
    max_cookie_bytes: int = field(default=DEFAULT_MAX_COOKIE_BYTES, repr=False)

    @property
    def platforms(self) -> tuple[Platform, ...]:
        return tuple(binding.platform for binding in self.bindings)


def validate_credential_mode(mode: object, *, allow_preserve: bool = False) -> None:
    if (mode is None and allow_preserve) or mode in ("use_default", "anonymous"):
        return
    raise CredentialDefaultsError(_ERROR)


def _now_text(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError
    return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_profile(row: sqlite3.Row, *, platform: Platform, ref: str, now: datetime) -> None:
    if row["platform"] != platform.value or row["secret_ref"] != ref:
        raise ValueError
    validate_opaque_reference(row["secret_ref"])
    if row["disabled_at"] is not None:
        raise ValueError
    expiry = row["expires_at"]
    if expiry is not None:
        if not isinstance(expiry, str) or not 1 <= len(expiry) <= 64:
            raise ValueError
        parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if parsed.tzinfo is None or _now_text(parsed) != expiry or parsed <= now:
            raise ValueError


def _revalidate(defaults: CredentialDefaults) -> None:
    if (
        not isinstance(defaults, CredentialDefaults)
        or not isinstance(defaults.run_id, str)
        or _RUN_ID.fullmatch(defaults.run_id) is None
        or not isinstance(defaults.bindings, tuple)
    ):
        raise ValueError
    revalidate_cookie_source_config(defaults.config)
    source_refs = {source.platform: source.credential_ref for source in defaults.config.sources}
    if defaults.platforms != defaults.config.default_cookie_platforms:
        raise ValueError
    for binding in defaults.bindings:
        if source_refs.get(binding.platform) != binding.credential_ref:
            raise ValueError
    if defaults.bindings:
        AttemptCookieResolver(
            defaults.config.sources,
            max_cookie_bytes=defaults.max_cookie_bytes,
        ).validate_sources()


def revalidate_credential_defaults(defaults: CredentialDefaults | None) -> None:
    if defaults is None:
        return
    failed = False
    try:
        _revalidate(defaults)
    except Exception:
        failed = True
    if failed:
        raise CredentialDefaultsError(_ERROR)


def prepare_credential_defaults(
    database: Database,
    config: CookieSourceConfig,
    *,
    run_id: str,
    now: datetime | None = None,
    max_cookie_bytes: int = DEFAULT_MAX_COOKIE_BYTES,
) -> CredentialDefaults:
    """Resolve/register explicitly opted-in profiles; never revive old ones."""
    prepared: CredentialDefaults | None = None
    try:
        moment = datetime.now(UTC) if now is None else now
        now_text = _now_text(moment)
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise ValueError
        revalidate_cookie_source_config(config)
        selected = {
            source.platform: source
            for source in config.sources
            if source.platform in config.default_cookie_platforms
        }
        if not selected:
            prepared = CredentialDefaults(run_id, (), config, max_cookie_bytes)
            _revalidate(prepared)
        else:
            AttemptCookieResolver(config.sources, max_cookie_bytes=max_cookie_bytes).validate_sources()
            bindings: list[_CredentialBinding] = []
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                revalidate_cookie_source_config(config)
                for platform in config.default_cookie_platforms:
                    source = selected[platform]
                    rows = connection.execute(
                        """
                        SELECT id, platform, secret_ref, expires_at, disabled_at
                        FROM credential_profiles WHERE platform = ? AND secret_ref = ?
                        ORDER BY id
                        """,
                        (platform.value, source.credential_ref),
                    ).fetchall()
                    if len(rows) > 1:
                        raise ValueError
                    if rows:
                        row = rows[0]
                        _validate_profile(row, platform=platform, ref=source.credential_ref, now=moment)
                        profile_id = row["id"]
                    else:
                        profile_id = str(uuid4())
                        connection.execute(
                            """
                            INSERT INTO credential_profiles(
                                id, platform, name, secret_ref, expires_at,
                                last_verified_at, created_at, disabled_at
                            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)
                            """,
                            (
                                profile_id, platform.value,
                                f"Local app default {platform.value} {uuid4().hex[:12]}",
                                source.credential_ref, now_text,
                            ),
                        )
                    bindings.append(_CredentialBinding(platform, profile_id, source.credential_ref))
                prepared = CredentialDefaults(run_id, tuple(bindings), config, max_cookie_bytes)
                _revalidate(prepared)
    except Exception:
        prepared = None
    if prepared is None:
        raise CredentialDefaultsError(_ERROR)
    return prepared


def resolve_default_profile_locked(
    connection: sqlite3.Connection,
    *,
    defaults: CredentialDefaults | None,
    platform: Platform,
    now: datetime,
) -> str | None:
    """Resolve on the caller's write transaction, never a second connection."""
    if defaults is None:
        return None
    failed = False
    profile_id: str | None = None
    try:
        if not connection.in_transaction or not isinstance(platform, Platform):
            raise ValueError
        _now_text(now)
        binding = next((item for item in defaults.bindings if item.platform == platform), None)
        if binding is not None:
            row = connection.execute(
                """
                SELECT id, platform, secret_ref, expires_at, disabled_at
                FROM credential_profiles WHERE id = ?
                """,
                (binding.profile_id,),
            ).fetchone()
            if row is None:
                raise ValueError
            _validate_profile(row, platform=platform, ref=binding.credential_ref, now=now)
            profile_id = row["id"]
    except Exception:
        failed = True
    if failed:
        raise CredentialDefaultsError(_ERROR)
    return profile_id


__all__ = [
    "CredentialDefaults", "CredentialDefaultsError", "CredentialMode",
    "prepare_credential_defaults", "revalidate_credential_defaults",
    "resolve_default_profile_locked", "validate_credential_mode",
]

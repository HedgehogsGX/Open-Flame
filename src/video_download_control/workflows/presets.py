"""Small, secret-free reusable workflow presets.

Presets are parameter templates, not execution records.  They deliberately
keep no source URL, credential, account session revision, or full AI
authorization.  A workflow created from a preset must provide a fresh,
runtime-bound profile and its authorization digests must still match the
saved intent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

from ..editing.contracts import recipe_from_mapping
from ..uploads.contracts import UploadError
from ..uploads.service import (
    UploadService,
    _PLATFORM_OPTION_KEYS,
    _TARGET_OVERRIDE_KEYS,
    _text as _upload_text,
)
from .service import WorkflowError, _profile as _workflow_profile


_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^\S(?:.*\S)?$")
_PRESETS_FILE = "presets.json"
_SCHEMA = 1
_LOCK = RLock()
_MAX_RECORDS = 50
_MAX_PROFILE_BYTES = 128 * 1024
_MAX_STORAGE_BYTES = 8 * 1024 * 1024
_PROFILE_KEYS = frozenset({
    "download_credential_mode", "edit_recipe", "ai", "upload",
    "auto_confirm_edit", "auto_confirm_upload",
})
_AI_KEYS = frozenset({
    "transcription_provider", "transcription_model",
    "transcription_authorization_sha256", "translation_authorization_sha256",
})
_SYNTHESIS_DIGEST = "synthesis_authorization_sha256"


class WorkflowPresetError(ValueError):
    _CODES = frozenset(
        {
            "workflow_preset_invalid",
            "workflow_preset_not_found",
            "workflow_preset_conflict",
            "workflow_preset_authorization_changed",
            "workflow_preset_storage_unavailable",
        }
    )

    def __init__(self, code: str):
        self.code = code if code in self._CODES else "workflow_preset_invalid"
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise WorkflowPresetError("workflow_preset_invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise WorkflowPresetError("workflow_preset_invalid")
    return value


def _name(value: object) -> str:
    if isinstance(value, str):
        value = value.strip()
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or not _SAFE_NAME.fullmatch(value):
        raise WorkflowPresetError("workflow_preset_invalid")
    if any(ord(char) < 32 for char in value):
        raise WorkflowPresetError("workflow_preset_invalid")
    return value.strip()


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowPresetError("workflow_preset_invalid") from exc


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowPresetError("workflow_preset_storage_unavailable")
        result[key] = value
    return result


def _upload_overrides(upload: dict[str, Any]) -> None:
    """Validate metadata without opening an account or media database."""
    try:
        upload["tags"] = UploadService._normalize_tags(upload["tags"])
        normalized = []
        for raw in upload["target_overrides"]:
            if set(raw) - _TARGET_OVERRIDE_KEYS or set(raw) == {"account_id"}:
                raise WorkflowPresetError("workflow_preset_invalid")
            item = dict(raw)
            for key, maximum in (("title", 100), ("description", 2000), ("source_credit", 200)):
                if key in item:
                    item[key] = _upload_text(item[key], maximum, required=key == "title")
            if "tags" in item:
                item["tags"] = UploadService._normalize_tags(item["tags"])
            for key, maximum in (("category_id", 10000), ("copyright", 2)):
                if key in item and (type(item[key]) is not int or not 1 <= item[key] <= maximum):
                    raise WorkflowPresetError("workflow_preset_invalid")
            if "mode" in item and item["mode"] not in {"draft", "publish"}:
                raise WorkflowPresetError("workflow_preset_invalid")
            for key in ("cover_landscape_asset_id", "cover_portrait_asset_id"):
                if item.get(key) is not None:
                    _id(item[key])
            # Only generic bounds are checked here. Creation still runs normal
            # upload preflight for current time, platform, accounts and covers.
            UploadService._validate_schedule(
                "bilibili", item.get("publish_at_unix"),
                item.get("publish_timezone_offset_minutes"), now=0,
            )
            options = item.get("platform_options")
            if options is not None:
                if not isinstance(options, dict):
                    raise WorkflowPresetError("workflow_preset_invalid")
                platform = next((
                    platform for platform, keys in _PLATFORM_OPTION_KEYS.items()
                    if set(options) <= keys
                ), None)
                if platform is None:
                    raise WorkflowPresetError("workflow_preset_invalid")
                parsed = UploadService._normalize_platform_options(platform, options)
                item["platform_options"] = {key: parsed[key] for key in options}
            normalized.append(item)
        upload["target_overrides"] = normalized
    except UploadError as exc:
        raise WorkflowPresetError("workflow_preset_invalid") from exc


def _current_profile(
    profile: Mapping[str, Any], *, template_only: bool = False,
) -> dict[str, Any]:
    """Use the execution contract before discarding authorization/session data."""
    try:
        candidate = profile
        if template_only and isinstance(profile, Mapping):
            if type(profile.get("ai_data_egress_accepted")) is not bool:
                raise WorkflowPresetError("workflow_preset_invalid")
            candidate = dict(profile)
            raw_ai = profile.get("ai")
            authorizations = []
            if isinstance(raw_ai, Mapping):
                authorizations.extend(raw_ai.get(key) for key in (
                    "transcription_authorization", "translation_authorization",
                ))
            dubbing = recipe_from_mapping(profile.get("edit_recipe")).dubbing.authorization
            if dubbing is not None:
                authorizations.append(dubbing.to_dict())
            # Saving is a local operation. Derive this temporary parsing flag
            # from the full definitions; normal materialization still requires
            # the actual current execution consent supplied by the caller.
            candidate["ai_data_egress_accepted"] = any(
                isinstance(item, Mapping) and item.get("execution") == "remote"
                for item in authorizations
            )
        # Bound workflow profiles may be saved; session bindings never enter
        # the preset or the next workflow request.
        upload = profile.get("upload") if isinstance(profile, Mapping) else None
        bound = isinstance(upload, Mapping) and "account_bindings" in upload
        normalized, _, _ = _workflow_profile(
            candidate, bound_accounts=bound, require_ai_authorization=True,
        )
        normalized["upload"].pop("account_bindings", None)
        _upload_overrides(normalized["upload"])
        return normalized
    except (WorkflowError, TypeError, ValueError, RecursionError) as exc:
        raise WorkflowPresetError("workflow_preset_invalid") from exc


def _secret_free_profile(
    profile: Mapping[str, Any], *, template_only: bool = False,
) -> dict[str, Any]:
    """Extract fields only from a validated current execution profile."""
    normalized = _current_profile(profile, template_only=template_only)
    reusable = {key: normalized[key] for key in _PROFILE_KEYS}
    dubbing = reusable["edit_recipe"]["dubbing"]
    if reusable["ai"] is not None:
        reusable["ai"] = {key: reusable["ai"][key] for key in _AI_KEYS}
        authorization = recipe_from_mapping(normalized["edit_recipe"]).dubbing.authorization
        reusable["ai"][_SYNTHESIS_DIGEST] = authorization.sha256
    dubbing.pop("authorization", None)
    return reusable


def _stored_profile(profile: object) -> dict[str, Any]:
    """Validate digest-only templates, including readable historical AI intent."""
    if not isinstance(profile, Mapping) or set(profile) != _PROFILE_KEYS:
        raise WorkflowPresetError("workflow_preset_invalid")
    encoded = _canonical(profile)
    if len(encoded.encode("utf-8")) > _MAX_PROFILE_BYTES:
        raise WorkflowPresetError("workflow_preset_invalid")
    original = json.loads(encoded)
    candidate = json.loads(encoded)
    raw_ai = candidate["ai"]
    if raw_ai is not None:
        if not isinstance(raw_ai, dict) or set(raw_ai) not in {
            _AI_KEYS, _AI_KEYS | {_SYNTHESIS_DIGEST},
        }:
            raise WorkflowPresetError("workflow_preset_invalid")
        if _SYNTHESIS_DIGEST in raw_ai:
            _digest(raw_ai.pop(_SYNTHESIS_DIGEST))
    raw_recipe = candidate["edit_recipe"]
    if (
        not isinstance(raw_recipe, dict)
        or not isinstance(raw_recipe.get("dubbing"), dict)
        or "authorization" in raw_recipe["dubbing"]
    ):
        raise WorkflowPresetError("workflow_preset_invalid")
    # Digest-only historical AI profiles conservatively require egress. This
    # temporary flag validates the template; it is never saved or executed.
    candidate["ai_data_egress_accepted"] = raw_ai is not None
    try:
        normalized, _, _ = _workflow_profile(candidate)
        _upload_overrides(normalized["upload"])
    except (WorkflowError, TypeError, ValueError, RecursionError) as exc:
        raise WorkflowPresetError("workflow_preset_invalid") from exc
    return original


def _record(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "id", "name", "profile", "profile_sha256", "revision", "created_at", "updated_at",
    }:
        raise WorkflowPresetError("workflow_preset_invalid")
    profile = _stored_profile(value["profile"])
    digest = _digest(value.get("profile_sha256"))
    if not hmac.compare_digest(digest, hashlib.sha256(_canonical(profile).encode("utf-8")).hexdigest()):
        raise WorkflowPresetError("workflow_preset_invalid")
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= 2**31 - 1:
        raise WorkflowPresetError("workflow_preset_invalid")
    try:
        timestamps = []
        for key in ("created_at", "updated_at"):
            raw = value[key]
            if not isinstance(raw, str) or len(raw) > 40 or not raw.endswith("Z"):
                raise ValueError
            timestamp = datetime.fromisoformat(raw)
            if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                raise ValueError
            timestamps.append(timestamp)
        if timestamps[0] > timestamps[1]:
            raise ValueError
    except ValueError as exc:
        raise WorkflowPresetError("workflow_preset_invalid") from exc
    return {
        "id": _id(value.get("id")), "name": _name(value.get("name")),
        "profile": profile, "profile_sha256": digest,
        "revision": value["revision"], "created_at": value["created_at"],
        "updated_at": value["updated_at"],
    }


class WorkflowPresetStore:
    """Atomic, bounded JSON storage for one local control-plane process."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / _PRESETS_FILE

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("rb") as handle:
                encoded = handle.read(_MAX_STORAGE_BYTES + 1)
            if len(encoded) > _MAX_STORAGE_BYTES:
                raise WorkflowPresetError("workflow_preset_storage_unavailable")
            value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_json_object)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise WorkflowPresetError("workflow_preset_storage_unavailable") from exc
        if (
            not isinstance(value, dict) or set(value) != {"schema", "presets"}
            or type(value["schema"]) is not int or value["schema"] != _SCHEMA
            or not isinstance(value["presets"], list) or len(value["presets"]) > _MAX_RECORDS
        ):
            raise WorkflowPresetError("workflow_preset_storage_unavailable")
        try:
            records = [_record(item) for item in value["presets"]]
            if len({item["id"] for item in records}) != len(records):
                raise WorkflowPresetError("workflow_preset_invalid")
            return records
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise WorkflowPresetError("workflow_preset_storage_unavailable") from exc

    def _write(self, records: list[Mapping[str, Any]]) -> None:
        payload = _canonical({"schema": _SCHEMA, "presets": records})
        if len(payload.encode("utf-8")) > _MAX_STORAGE_BYTES:
            raise WorkflowPresetError("workflow_preset_conflict")
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".presets-", suffix=".tmp", dir=self.root)
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except (OSError, UnicodeError) as exc:
            raise WorkflowPresetError("workflow_preset_storage_unavailable") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def list(self) -> list[dict[str, Any]]:
        with _LOCK:
            return self._read()

    def get(self, preset_id: str) -> dict[str, Any]:
        preset_id = _id(preset_id)
        with _LOCK:
            for record in self._read():
                if record["id"] == preset_id:
                    return record
        raise WorkflowPresetError("workflow_preset_not_found")

    def create(self, name: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        reusable = _secret_free_profile(profile, template_only=True)
        now = _now()
        record = _record({
            "id": uuid4().hex,
            "name": _name(name),
            "profile": reusable,
            "profile_sha256": hashlib.sha256(_canonical(reusable).encode("utf-8")).hexdigest(),
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        })
        with _LOCK:
            records = self._read()
            for existing in records:
                if existing["name"] == record["name"] and existing["profile_sha256"] == record["profile_sha256"]:
                    return existing
            records.insert(0, record)
            if len(records) > _MAX_RECORDS:
                raise WorkflowPresetError("workflow_preset_conflict")
            self._write(records)
        return record

    def materialize(self, preset_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        preset = self.get(preset_id)
        normalized = _current_profile(profile)
        current = _secret_free_profile(profile)
        saved = preset["profile"]
        if saved["ai"] != current["ai"]:
            raise WorkflowPresetError("workflow_preset_authorization_changed")
        materialized = normalized
        recipe = json.loads(_canonical(saved["edit_recipe"]))
        current_dubbing = normalized["edit_recipe"]["dubbing"]
        if "authorization" in current_dubbing:
            recipe["dubbing"]["authorization"] = current_dubbing["authorization"]
        materialized["edit_recipe"] = recipe
        materialized["upload"] = saved["upload"]
        # Confirmation and credential choices belong to this invocation. A
        # saved template must never silently elevate the current request.
        return _current_profile(materialized)


__all__ = ["WorkflowPresetError", "WorkflowPresetStore"]

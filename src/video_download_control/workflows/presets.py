"""Small, secret-free reusable workflow presets.

Presets are parameter templates, not execution records.  They deliberately
keep no source URL, credential, account session revision, or full AI
authorization.  A workflow created from a preset must provide a fresh,
runtime-bound profile and its authorization digests must still match the
saved intent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

from ..editing.contracts import EditingError, recipe_from_mapping


_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^\S(?:.*\S)?$")
_PRESETS_FILE = "presets.json"
_SCHEMA = 1
_LOCK = RLock()


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
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or not _SAFE_NAME.fullmatch(value):
        raise WorkflowPresetError("workflow_preset_invalid")
    if any(ord(char) < 32 for char in value):
        raise WorkflowPresetError("workflow_preset_invalid")
    return value.strip()


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowPresetError("workflow_preset_invalid") from exc


def _secret_free_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Extract reusable fields and discard execution-only authorization data."""

    if not isinstance(profile, Mapping):
        raise WorkflowPresetError("workflow_preset_invalid")
    recipe = profile.get("edit_recipe")
    try:
        normalized_recipe = recipe_from_mapping(recipe).to_dict()
    except EditingError as exc:
        raise WorkflowPresetError("workflow_preset_invalid") from exc
    dubbing = normalized_recipe.get("dubbing")
    if isinstance(dubbing, dict):
        dubbing.pop("authorization", None)
    raw_ai = profile.get("ai")
    if raw_ai is None:
        ai = None
    elif isinstance(raw_ai, Mapping):
        required = {
            "transcription_provider",
            "transcription_model",
            "transcription_authorization_sha256",
            "translation_authorization_sha256",
        }
        allowed_ai = required | {
            "transcription_authorization",
            "translation_authorization",
        }
        if set(raw_ai) not in {frozenset(required), frozenset(allowed_ai)}:
            raise WorkflowPresetError("workflow_preset_invalid")
        ai = {
            key: raw_ai[key]
            for key in (
                "transcription_provider",
                "transcription_model",
                "transcription_authorization_sha256",
                "translation_authorization_sha256",
            )
        }
        _digest(ai["transcription_authorization_sha256"])
        _digest(ai["translation_authorization_sha256"])
    else:
        raise WorkflowPresetError("workflow_preset_invalid")
    upload = profile.get("upload")
    if not isinstance(upload, Mapping):
        raise WorkflowPresetError("workflow_preset_invalid")
    allowed_upload = {
        "account_ids", "title", "description", "tags", "category_id",
        "mode", "copyright", "source_credit", "target_overrides",
    }
    if set(upload) not in {
        frozenset(allowed_upload),
        frozenset(allowed_upload | {"account_bindings"}),
    }:
        raise WorkflowPresetError("workflow_preset_invalid")
    accounts = upload.get("account_ids")
    if not isinstance(accounts, list) or len(accounts) > 3 or any(
        not isinstance(item, str) or _ID.fullmatch(item) is None for item in accounts
    ):
        raise WorkflowPresetError("workflow_preset_invalid")
    return {
        "download_credential_mode": profile.get("download_credential_mode"),
        "edit_recipe": normalized_recipe,
        "ai": ai,
        "upload": json.loads(_canonical({key: upload[key] for key in allowed_upload})),
        "auto_confirm_edit": profile.get("auto_confirm_edit"),
        "auto_confirm_upload": profile.get("auto_confirm_upload"),
    }


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _id(value.get("id")),
        "name": _name(value.get("name")),
        "profile": _secret_free_profile(value["profile"]),
        "profile_sha256": _digest(value.get("profile_sha256")),
        "revision": value.get("revision"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
    }


def _profile_digest(profile: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(_secret_free_profile(profile)).encode("utf-8")).hexdigest()


class WorkflowPresetStore:
    """Atomic, bounded JSON storage for a small number of local presets."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / _PRESETS_FILE

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowPresetError("workflow_preset_storage_unavailable") from exc
        if not isinstance(value, dict) or value.get("schema") != _SCHEMA or not isinstance(value.get("presets"), list):
            raise WorkflowPresetError("workflow_preset_storage_unavailable")
        try:
            return [_record(item) for item in value["presets"]]
        except (KeyError, TypeError, WorkflowPresetError) as exc:
            raise WorkflowPresetError("workflow_preset_storage_unavailable") from exc

    def _write(self, records: list[Mapping[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = _canonical({"schema": _SCHEMA, "presets": records})
        descriptor, temporary_name = tempfile.mkstemp(prefix=".presets-", suffix=".tmp", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except (OSError, UnicodeError) as exc:
            raise WorkflowPresetError("workflow_preset_storage_unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)

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
        reusable = _secret_free_profile(profile)
        now = _now()
        record = {
            "id": uuid4().hex,
            "name": _name(name),
            "profile": reusable,
            "profile_sha256": hashlib.sha256(_canonical(reusable).encode("utf-8")).hexdigest(),
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        with _LOCK:
            records = self._read()
            records.insert(0, record)
            if len(records) > 50:
                raise WorkflowPresetError("workflow_preset_conflict")
            self._write(records)
        return _record(record)

    def materialize(self, preset_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
        preset = self.get(preset_id)
        raw_recipe = profile.get("edit_recipe")
        current_dubbing_authorization = None
        if isinstance(raw_recipe, Mapping) and isinstance(raw_recipe.get("dubbing"), Mapping):
            current_dubbing_authorization = raw_recipe["dubbing"].get("authorization")
        current = _secret_free_profile(profile)
        saved = preset["profile"]
        if saved["ai"] != current["ai"]:
            raise WorkflowPresetError("workflow_preset_authorization_changed")
        materialized = dict(profile)
        materialized["download_credential_mode"] = saved["download_credential_mode"]
        recipe = json.loads(_canonical(saved["edit_recipe"]))
        if isinstance(recipe.get("dubbing"), dict) and current_dubbing_authorization is not None:
            recipe["dubbing"]["authorization"] = current_dubbing_authorization
        materialized["edit_recipe"] = recipe
        materialized["upload"] = saved["upload"]
        materialized["auto_confirm_edit"] = saved["auto_confirm_edit"]
        materialized["auto_confirm_upload"] = saved["auto_confirm_upload"]
        return materialized


__all__ = ["WorkflowPresetError", "WorkflowPresetStore"]

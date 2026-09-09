"""Durable, restart-safe orchestration across download, edit and upload domains."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from ..editing.ai_authorization import (
    AiAuthorizationError,
    AiOperationAuthorization,
    parse_operation_authorization,
)
from ..editing.contracts import EditingError, recipe_from_mapping
from .contracts import (
    CancellationSnapshot,
    MAX_WORKFLOW_ACCOUNTS,
    MAX_WORKFLOW_SEGMENTS,
    MAX_WORKFLOW_UPLOAD_JOBS,
    UploadSnapshot,
    WorkflowDomainAdapter,
    workflow_outputs_match_state,
)
from .schema import SCHEMA_VERSION, WorkflowSchemaError, ensure_workflow_schema


_REQUEST_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HEX_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_AI_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_PROGRESS_LIMIT = MAX_WORKFLOW_SEGMENTS * 2 + 12
_CANCELLATION_REQUESTED = "workflow_cancellation_requested"
_SERVICE_LOCKS_GUARD = RLock()
_SERVICE_LOCKS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_ACTIVE_STATES = {
    "created",
    "downloading",
    "preparing_edit",
    "awaiting_ai_review",
    "awaiting_edit_confirmation",
    "rendering",
    "preparing_upload",
    "awaiting_upload_confirmation",
    "uploading",
}
_CANCELLABLE_STATES = _ACTIVE_STATES | {"attention_required"}
_AI_LEDGER_REVIEW_CODES = frozenset(
    {
        "ai_remote_result_unknown",
        "ai_remote_retry_blocked",
        "ai_remote_reconciliation_required",
        "ai_remote_accepted_without_result",
        "ai_remote_abandoned",
    }
)
_AUTO_AI_REVIEW_CODES = frozenset(
    {
        "",
        "ai_transcription_confirmation_required",
        "ai_transcription_review_required",
        "ai_translation_confirmation_required",
        "ai_translation_review_required",
        "ai_restart_confirmation_required",
    }
)
_AUTO_EDIT_REVIEW_CODES = frozenset(
    {
        "",
        "edit_confirmation_required",
        "edit_review_confirmation_required",
        "explicit_confirmation_required",
        "restart_confirmation_required",
        "edit_restart_confirmation_required",
    }
)
_AUTO_UPLOAD_REVIEW_CODES = frozenset({"", "upload_restart_confirmation_required"})
_PREFLIGHT_RETRY_CODES = frozenset(
    {
        "download_worker_unobserved",
        "download_runtime_unavailable",
        "download_queue_paused",
        "download_worker_stale",
        "download_worker_not_ready",
        "download_network_disabled",
        "processor_not_configured",
        "ai_runtime_missing",
        "ai_runtime_invalid",
        "ai_runtime_changed",
        "ai_runtime_unsupported",
        "ai_provider_not_found",
        "ai_provider_operation_unsupported",
        "ai_model_not_found",
        "ai_model_operation_unsupported",
        "ai_provider_auth_missing",
        "ai_provider_auth_environment_invalid",
        "ai_authorization_binding_required",
        "ai_authorization_changed",
        "ai_authorization_invalid",
        "ai_voice_not_allowed",
        "runtime_missing",
        "runtime_invalid",
        "runtime_busy",
        "runtime_upgrade_required",
        "runtime_unavailable",
        "unsupported_platform",
        "scheduler_owned_by_other_instance",
        "scheduler_database_unavailable",
        "scheduler_failed",
        "upload_scheduler_not_ready",
        "uploader_stopped",
        "upload_activity_busy",
        "account_not_found",
        "account_disconnected",
        "account_not_ready",
        "account_session_changed",
    }
)
_PREFLIGHT_WAIT_CODES = frozenset(
    {
        "download_worker_unobserved",
        "download_runtime_unavailable",
        "download_queue_paused",
        "download_worker_stale",
        "download_worker_not_ready",
        "download_network_disabled",
        "runtime_busy",
        "upload_activity_busy",
        "account_not_ready",
    }
)


class WorkflowError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def default_workflow_root(data_root: Path) -> Path:
    data_root = Path(data_root)
    return data_root.with_name(data_root.name + "-workflows")


def _download_identifier(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical(value: Mapping[str, Any]) -> tuple[str, str]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise WorkflowError("invalid_workflow_profile") from None
    if len(encoded.encode("utf-8")) > 128 * 1024:
        raise WorkflowError("workflow_profile_too_large")
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: object, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise WorkflowError("invalid_workflow_profile")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in value):
        raise WorkflowError("invalid_workflow_profile")
    result = value.strip()
    if required and not result:
        raise WorkflowError("invalid_workflow_profile")
    return result


def _bound_ai_authorization(
    value: object,
    *,
    operation: str,
    provider: str,
    model: str,
    expected_sha256: str,
) -> AiOperationAuthorization:
    try:
        authorization = parse_operation_authorization(value)
    except AiAuthorizationError:
        raise WorkflowError("invalid_workflow_profile") from None
    if (
        authorization is None
        or authorization.operation != operation
        or authorization.provider_id != provider
        or authorization.model_id != model
        or not hmac.compare_digest(authorization.sha256, expected_sha256)
    ):
        raise WorkflowError("invalid_workflow_profile")
    return authorization


def _profile(
    value: Mapping[str, Any],
    *,
    bound_accounts: bool = False,
    require_ai_authorization: bool = False,
) -> tuple[dict[str, Any], str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "edit_recipe",
        "ai",
        "upload",
        "download_credential_mode",
        "auto_confirm_edit",
        "auto_confirm_upload",
        "ai_data_egress_accepted",
    }:
        raise WorkflowError("invalid_workflow_profile")
    auto_edit = value.get("auto_confirm_edit")
    auto_upload = value.get("auto_confirm_upload")
    if not isinstance(auto_edit, bool) or not isinstance(auto_upload, bool):
        raise WorkflowError("invalid_workflow_profile")
    if auto_upload and not auto_edit:
        raise WorkflowError("upload_automation_requires_edit_automation")
    ai_data_egress_accepted = value.get("ai_data_egress_accepted")
    if not isinstance(ai_data_egress_accepted, bool):
        raise WorkflowError("invalid_workflow_profile")
    credential_mode = value.get("download_credential_mode")
    if credential_mode not in {"anonymous", "use_default"}:
        raise WorkflowError("invalid_workflow_profile")
    raw_recipe = value.get("edit_recipe")
    try:
        parsed_recipe = recipe_from_mapping(raw_recipe)
        recipe = parsed_recipe.to_dict()
    except EditingError as exc:
        raise WorkflowError(exc.code) from None
    if parsed_recipe.translation.revision_id is not None:
        raise WorkflowError("invalid_workflow_profile")
    if len(parsed_recipe.segments) > MAX_WORKFLOW_SEGMENTS or (
        not parsed_recipe.dubbing.enabled and not parsed_recipe.segments
    ):
        raise WorkflowError("workflow_output_count_invalid")
    if (
        (parsed_recipe.translation.enabled or parsed_recipe.dubbing.enabled)
        and len(parsed_recipe.segments) > 1
        and any(
            left.end_ms != right.start_ms
            for left, right in zip(
                parsed_recipe.segments, parsed_recipe.segments[1:], strict=False
            )
        )
    ):
        raise WorkflowError("workflow_ai_segments_must_be_contiguous")
    ai_enabled = parsed_recipe.translation.enabled or parsed_recipe.dubbing.enabled
    if parsed_recipe.translation.enabled and not parsed_recipe.dubbing.enabled:
        raise WorkflowError("workflow_translation_requires_dubbing")
    raw_ai = value.get("ai")
    if ai_enabled:
        runtime_tokens = [
            parsed_recipe.translation.provider,
            parsed_recipe.translation.model,
        ]
        if parsed_recipe.dubbing.enabled:
            runtime_tokens.extend(
                (parsed_recipe.dubbing.provider, parsed_recipe.dubbing.model)
            )
        if any(_AI_TOKEN.fullmatch(item) is None for item in runtime_tokens):
            raise WorkflowError("invalid_workflow_profile")
        legacy_ai_keys = {
            "transcription_provider",
            "transcription_model",
        }
        digest_ai_keys = legacy_ai_keys | {
            "transcription_authorization_sha256",
            "translation_authorization_sha256",
        }
        bound_ai_keys = digest_ai_keys | {
            "transcription_authorization",
            "translation_authorization",
        }
        raw_ai_keys = frozenset(raw_ai) if isinstance(raw_ai, Mapping) else frozenset()
        if (
            not isinstance(raw_ai, Mapping)
            or raw_ai_keys
            not in {
                frozenset(legacy_ai_keys),
                frozenset(digest_ai_keys),
                frozenset(bound_ai_keys),
            }
            or (require_ai_authorization and raw_ai_keys != frozenset(bound_ai_keys))
        ):
            raise WorkflowError("invalid_workflow_profile")
        transcription_provider = raw_ai.get("transcription_provider")
        transcription_model = raw_ai.get("transcription_model")
        if (
            not isinstance(transcription_provider, str)
            or _AI_TOKEN.fullmatch(transcription_provider) is None
            or not isinstance(transcription_model, str)
            or _AI_TOKEN.fullmatch(transcription_model) is None
        ):
            raise WorkflowError("invalid_workflow_profile")
        if parsed_recipe.dubbing.enabled and not parsed_recipe.translation.enabled:
            raise WorkflowError("workflow_dubbing_requires_translation")
        if (
            parsed_recipe.translation.source_language.casefold()
            != "auto"
            and parsed_recipe.translation.source_language.casefold()
            == parsed_recipe.translation.target_language.casefold()
        ):
            raise WorkflowError("workflow_translation_languages_match")
        normalized_ai: dict[str, Any] | None = {
            "transcription_provider": transcription_provider,
            "transcription_model": transcription_model,
        }
        if raw_ai_keys in {frozenset(digest_ai_keys), frozenset(bound_ai_keys)}:
            for field in (
                "transcription_authorization_sha256",
                "translation_authorization_sha256",
            ):
                digest = raw_ai.get(field)
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise WorkflowError("invalid_workflow_profile")
                normalized_ai[field] = digest
        if raw_ai_keys == frozenset(bound_ai_keys):
            transcription_authorization = _bound_ai_authorization(
                raw_ai.get("transcription_authorization"),
                operation="transcribe",
                provider=transcription_provider,
                model=transcription_model,
                expected_sha256=normalized_ai["transcription_authorization_sha256"],
            )
            translation_authorization = _bound_ai_authorization(
                raw_ai.get("translation_authorization"),
                operation="translate",
                provider=parsed_recipe.translation.provider,
                model=parsed_recipe.translation.model,
                expected_sha256=normalized_ai["translation_authorization_sha256"],
            )
            normalized_ai["transcription_authorization"] = (
                transcription_authorization.to_dict()
            )
            normalized_ai["translation_authorization"] = (
                translation_authorization.to_dict()
            )
            authorizations = [transcription_authorization, translation_authorization]
            if parsed_recipe.dubbing.authorization is not None:
                authorizations.append(parsed_recipe.dubbing.authorization)
            ai_requires_data_egress = any(
                authorization.execution == "remote" for authorization in authorizations
            )
        else:
            # Historical profiles did not persist enough data to prove local
            # execution. Preserve their original conservative egress contract.
            ai_requires_data_egress = True
        if require_ai_authorization and (
            not parsed_recipe.dubbing.enabled
            or parsed_recipe.dubbing.authorization is None
        ):
            raise WorkflowError("ai_authorization_required")
    else:
        if raw_ai is not None:
            raise WorkflowError("invalid_workflow_profile")
        normalized_ai = None
        ai_requires_data_egress = False
    if ai_requires_data_egress and not ai_data_egress_accepted:
        raise WorkflowError("ai_data_egress_confirmation_required")
    if not ai_requires_data_egress and ai_data_egress_accepted:
        raise WorkflowError("invalid_workflow_profile")
    raw_upload = value.get("upload")
    if not isinstance(raw_upload, Mapping):
        raise WorkflowError("invalid_workflow_profile")
    required_upload = {
        "account_ids",
        "title",
        "description",
        "tags",
        "category_id",
        "mode",
        "copyright",
        "source_credit",
        "target_overrides",
    }
    expected_upload = required_upload | ({"account_bindings"} if bound_accounts else set())
    if set(raw_upload) != expected_upload:
        raise WorkflowError("invalid_workflow_profile")
    account_ids = raw_upload.get("account_ids")
    if (
        not isinstance(account_ids, Sequence)
        or isinstance(account_ids, (str, bytes))
        or not 1 <= len(account_ids) <= MAX_WORKFLOW_ACCOUNTS
        or any(not isinstance(item, str) or not _HEX_IDENTIFIER.fullmatch(item) for item in account_ids)
        or len(set(account_ids)) != len(account_ids)
    ):
        raise WorkflowError("invalid_workflow_profile")
    tags = raw_upload.get("tags")
    if (
        not isinstance(tags, Sequence)
        or isinstance(tags, (str, bytes))
        or len(tags) > 10
        or any(not isinstance(item, str) or not item.strip() or len(item) > 40 for item in tags)
    ):
        raise WorkflowError("invalid_workflow_profile")
    normalized_tags = [_text(item, 20, required=True) for item in tags]
    if len(normalized_tags) != len(set(normalized_tags)):
        raise WorkflowError("invalid_workflow_profile")
    mode = raw_upload.get("mode")
    if mode not in {"draft", "publish"}:
        raise WorkflowError("invalid_workflow_profile")
    category_id = raw_upload.get("category_id")
    copyright_value = raw_upload.get("copyright")
    if category_id is not None and (
        isinstance(category_id, bool)
        or not isinstance(category_id, int)
        or not 1 <= category_id <= 10_000
    ):
        raise WorkflowError("invalid_workflow_profile")
    if copyright_value is not None and (
        isinstance(copyright_value, bool)
        or not isinstance(copyright_value, int)
        or copyright_value not in {1, 2}
    ):
        raise WorkflowError("invalid_workflow_profile")
    target_overrides = raw_upload.get("target_overrides")
    if (
        not isinstance(target_overrides, Sequence)
        or isinstance(target_overrides, (str, bytes))
        or len(target_overrides) > len(account_ids)
        or any(not isinstance(item, Mapping) for item in target_overrides)
    ):
        raise WorkflowError("invalid_workflow_profile")
    override_accounts: list[str] = []
    for item in target_overrides:
        account_id = item.get("account_id")
        if (
            not isinstance(account_id, str)
            or not _HEX_IDENTIFIER.fullmatch(account_id)
            or account_id not in account_ids
            or account_id in override_accounts
        ):
            raise WorkflowError("invalid_workflow_profile")
        override_accounts.append(account_id)
    normalized_bindings: list[dict[str, str]] = []
    if bound_accounts:
        raw_bindings = raw_upload.get("account_bindings")
        if (
            not isinstance(raw_bindings, Sequence)
            or isinstance(raw_bindings, (str, bytes))
            or len(raw_bindings) != len(account_ids)
        ):
            raise WorkflowError("invalid_workflow_profile")
        seen_bindings: set[str] = set()
        for item in raw_bindings:
            if not isinstance(item, Mapping) or set(item) != {
                "account_id",
                "platform",
                "session_revision",
            }:
                raise WorkflowError("invalid_workflow_profile")
            account_id = item.get("account_id")
            platform = item.get("platform")
            session_revision = item.get("session_revision")
            if (
                not isinstance(account_id, str)
                or account_id not in account_ids
                or account_id in seen_bindings
                or platform not in {"bilibili", "douyin", "tencent"}
                or not isinstance(session_revision, str)
                or not _HEX_IDENTIFIER.fullmatch(session_revision)
            ):
                raise WorkflowError("invalid_workflow_profile")
            seen_bindings.add(account_id)
            normalized_bindings.append(
                {
                    "account_id": account_id,
                    "platform": platform,
                    "session_revision": session_revision,
                }
            )
    normalized: dict[str, Any] = {
        "download_credential_mode": credential_mode,
        "edit_recipe": recipe,
        "ai": normalized_ai,
        "upload": {
            "account_ids": list(account_ids),
            "title": _text(raw_upload.get("title"), 100, required=True),
            "description": _text(raw_upload.get("description"), 2000),
            "tags": normalized_tags,
            "category_id": category_id,
            "mode": mode,
            "copyright": copyright_value,
            "source_credit": _text(raw_upload.get("source_credit"), 200),
            "target_overrides": [dict(item) for item in target_overrides],
        },
        "auto_confirm_edit": auto_edit,
        "auto_confirm_upload": auto_upload,
        "ai_data_egress_accepted": ai_data_egress_accepted,
    }
    if bound_accounts:
        normalized["upload"]["account_bindings"] = normalized_bindings
    encoded, digest = _canonical(normalized)
    return normalized, encoded, digest


def _workflow_outputs(
    value: object,
    profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate the ordered edit-to-upload fan-out stored by Workflow Schema 2."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) > MAX_WORKFLOW_SEGMENTS
    ):
        raise WorkflowError("workflow_data_invalid")
    recipe = profile.get("edit_recipe")
    upload = profile.get("upload")
    if not isinstance(recipe, Mapping) or not isinstance(upload, Mapping):
        raise WorkflowError("workflow_data_invalid")
    segments = recipe.get("segments")
    account_ids = upload.get("account_ids")
    bindings = upload.get("account_bindings")
    if (
        not isinstance(segments, list)
        or not isinstance(account_ids, list)
        or not isinstance(bindings, list)
    ):
        raise WorkflowError("workflow_data_invalid")
    expected_outputs = len(segments) or 1
    if value and len(value) != expected_outputs:
        raise WorkflowError("workflow_data_invalid")
    platforms = {
        item.get("account_id"): item.get("platform")
        for item in bindings
        if isinstance(item, Mapping)
    }
    if len(platforms) != len(account_ids) or set(platforms) != set(account_ids):
        raise WorkflowError("workflow_data_invalid")

    normalized: list[dict[str, Any]] = []
    edit_ids: set[str] = set()
    source_ids: set[str] = set()
    job_ids: set[str] = set()
    prepared: list[bool] = []
    for ordinal, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping) or set(raw) != {
            "segment_ordinal",
            "edit_output_id",
            "upload_source_id",
            "targets",
        }:
            raise WorkflowError("workflow_data_invalid")
        edit_output_id = raw.get("edit_output_id")
        source_id = raw.get("upload_source_id")
        targets = raw.get("targets")
        if (
            raw.get("segment_ordinal") != ordinal
            or not isinstance(edit_output_id, str)
            or not _HEX_IDENTIFIER.fullmatch(edit_output_id)
            or edit_output_id in edit_ids
            or source_id is not None
            and (
                not isinstance(source_id, str)
                or not _HEX_IDENTIFIER.fullmatch(source_id)
                or source_id in source_ids
            )
            or not isinstance(targets, Sequence)
            or isinstance(targets, (str, bytes))
        ):
            raise WorkflowError("workflow_data_invalid")
        edit_ids.add(edit_output_id)
        if source_id is not None:
            source_ids.add(source_id)
        is_prepared = source_id is not None
        prepared.append(is_prepared)
        if (not is_prepared and targets) or (
            is_prepared and len(targets) != len(account_ids)
        ):
            raise WorkflowError("workflow_data_invalid")
        normalized_targets: list[dict[str, str]] = []
        if is_prepared:
            for account_id, target in zip(account_ids, targets, strict=True):
                if (
                    not isinstance(target, Mapping)
                    or set(target) != {"account_id", "platform", "job_id"}
                    or target.get("account_id") != account_id
                    or target.get("platform") != platforms[account_id]
                ):
                    raise WorkflowError("workflow_data_invalid")
                job_id = target.get("job_id")
                if (
                    not isinstance(job_id, str)
                    or not _HEX_IDENTIFIER.fullmatch(job_id)
                    or job_id in job_ids
                ):
                    raise WorkflowError("workflow_data_invalid")
                job_ids.add(job_id)
                normalized_targets.append(
                    {
                        "account_id": account_id,
                        "platform": platforms[account_id],
                        "job_id": job_id,
                    }
                )
        normalized.append(
            {
                "segment_ordinal": ordinal,
                "edit_output_id": edit_output_id,
                "upload_source_id": source_id,
                "targets": normalized_targets,
            }
        )
    if any(prepared[index] and not prepared[index - 1] for index in range(1, len(prepared))):
        raise WorkflowError("workflow_data_invalid")
    if len(job_ids) > MAX_WORKFLOW_UPLOAD_JOBS:
        raise WorkflowError("workflow_data_invalid")
    return normalized


def _migration_profile_is_valid(value: Mapping[str, object]) -> bool:
    """Apply the current complete reader contract before a Schema 1 migration."""

    try:
        _profile(value, bound_accounts=True)
    except WorkflowError:
        return False
    return True


def _output_ids_from_snapshot(snapshot: object) -> tuple[str, ...]:
    raw = getattr(snapshot, "output_ids", ())
    primary = getattr(snapshot, "output_id", None)
    if not raw:
        raw = () if primary is None else (primary,)
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or not 1 <= len(raw) <= MAX_WORKFLOW_SEGMENTS
        or any(not isinstance(item, str) or not _HEX_IDENTIFIER.fullmatch(item) for item in raw)
        or len(set(raw)) != len(raw)
        or (primary is not None and (len(raw) != 1 or primary != raw[0]))
    ):
        raise WorkflowError("workflow_domain_data_invalid")
    return tuple(raw)


def _unprepared_outputs(output_ids: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {
            "segment_ordinal": ordinal,
            "edit_output_id": output_id,
            "upload_source_id": None,
            "targets": [],
        }
        for ordinal, output_id in enumerate(output_ids, start=1)
    ]


def _service_lock(database_path: Path) -> Any:
    """Share one in-process mutation lock for every view of a workflow store."""

    try:
        key = os.path.normcase(str(database_path.resolve(strict=False)))
    except (OSError, RuntimeError):
        raise WorkflowError("workflow_database_unavailable") from None
    with _SERVICE_LOCKS_GUARD:
        lock = _SERVICE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _SERVICE_LOCKS[key] = lock
        return lock


class WorkflowService:
    """Own one small state machine; domain media and secrets stay in adapters."""

    def __init__(self, root: Path, adapter: WorkflowDomainAdapter) -> None:
        self.root = Path(root)
        self.database_path = self.root / "workflows.sqlite3"
        self.adapter = adapter
        self._lock = _service_lock(self.database_path)
        with self._lock:
            try:
                ensure_workflow_schema(
                    self.database_path,
                    legacy_profile_validator=_migration_profile_is_valid,
                )
            except WorkflowSchemaError:
                raise WorkflowError("workflow_database_unavailable") from None

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _preflight(
        self,
        profile: Mapping[str, Any],
        *,
        expected_account_bindings: Sequence[Mapping[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        recipe = profile["edit_recipe"]
        cover = recipe.get("cover")
        upload = dict(profile["upload"])
        upload.pop("account_bindings", None)
        raw_bindings = self.adapter.preflight(
            recipe,
            profile["ai"],
            upload,
            cover_aspect_ratio=(
                None if cover is None else cover.get("aspect_ratio")
            ),
            expected_account_bindings=expected_account_bindings,
        )
        if (
            not isinstance(raw_bindings, Sequence)
            or isinstance(raw_bindings, (str, bytes))
            or any(not isinstance(item, Mapping) for item in raw_bindings)
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        bindings = [dict(item) for item in raw_bindings]
        if expected_account_bindings is not None and bindings != [
            dict(item) for item in expected_account_bindings
        ]:
            raise WorkflowError("account_session_changed")
        return bindings

    def create(
        self,
        *,
        source_url: str,
        name: str,
        profile: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not _REQUEST_KEY.fullmatch(
            idempotency_key
        ):
            raise WorkflowError("invalid_idempotency_key")
        source_url = _text(source_url, 4096, required=True)
        if not source_url.startswith(("https://", "http://")):
            raise WorkflowError("invalid_source_url")
        name = _text(name, 160, required=True)
        normalized, _encoded, intent_profile_digest = _profile(
            profile, require_ai_authorization=True
        )
        request_payload = {
            "source_url": source_url,
            "name": name,
            "profile_sha256": intent_profile_digest,
        }
        _, request_digest = _canonical(request_payload)
        with self._lock:
            with self._db() as db:
                prior = db.execute(
                    "SELECT * FROM workflows WHERE request_key=?", (idempotency_key,)
                ).fetchone()
                if prior is not None:
                    if prior["request_digest"] != request_digest:
                        raise WorkflowError("idempotency_conflict")
                    return self._public(prior)
        # Runtime integrity may require a cold scan. It has no workflow-domain
        # side effects, so do not hold the global mutation lock while it runs.
        # The request key is checked again in the insertion transaction below.
        normalized["upload"]["account_bindings"] = self._preflight(normalized)
        normalized, encoded, profile_digest = _profile(
            normalized,
            bound_accounts=True,
            require_ai_authorization=True,
        )
        with self._lock:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute(
                    "SELECT * FROM workflows WHERE request_key=?", (idempotency_key,)
                ).fetchone()
                if prior is not None:
                    if prior["request_digest"] != request_digest:
                        raise WorkflowError("idempotency_conflict")
                    return self._public(prior)
                workflow_id = uuid4().hex
                now = _now()
                db.execute(
                """INSERT INTO workflows(
 id,request_key,request_digest,source_url,name,profile_json,profile_sha256,
 state,code,batch_name,upload_job_ids_json,auto_confirm_edit,
 auto_confirm_upload,revision,created_at,updated_at)
 VALUES(?,?,?,?,?,?,?,'created','',?,'[]',?,?,1,?,?)""",
                (
                    workflow_id,
                    idempotency_key,
                    request_digest,
                    source_url,
                    name,
                    encoded,
                    profile_digest,
                    f"Open-Flame workflow {workflow_id}",
                    int(normalized["auto_confirm_edit"]),
                    int(normalized["auto_confirm_upload"]),
                    now,
                    now,
                ),
                )
                self._event(db, workflow_id, None, "created", "", now)
                return self._by_id(db, workflow_id)

    def get(self, workflow_id: str) -> dict[str, Any]:
        workflow_id = self._identifier(workflow_id)
        with self._db() as db:
            return self._by_id(db, workflow_id)

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WorkflowError("invalid_limit")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM workflows ORDER BY created_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def active_page(
        self,
        after: tuple[str, str] | None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one cursor page so every durable active workflow is reconciled."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WorkflowError("invalid_limit")
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 2
            or not all(isinstance(item, str) and item for item in after)
        ):
            raise WorkflowError("workflow_data_invalid")
        states = tuple(sorted(_ACTIVE_STATES))
        placeholders = ",".join("?" for _ in states)

        def fetch(cursor: tuple[str, str] | None) -> list[sqlite3.Row]:
            query = (
                f"SELECT * FROM workflows WHERE (state IN ({placeholders}) OR "
                "(state='attention_required' AND code=?))"
            )
            values: list[object] = [*states, _CANCELLATION_REQUESTED]
            if cursor is not None:
                query += " AND (created_at>? OR (created_at=? AND id>?))"
                values.extend((cursor[0], cursor[0], cursor[1]))
            query += " ORDER BY created_at,id LIMIT ?"
            values.append(limit)
            with self._db() as db:
                return db.execute(query, values).fetchall()

        rows = fetch(after)
        if not rows and after is not None:
            rows = fetch(None)
        return [self._public(row) for row in rows]

    def events(self, workflow_id: str) -> list[dict[str, Any]]:
        workflow_id = self._identifier(workflow_id)
        with self._db() as db:
            if db.execute("SELECT 1 FROM workflows WHERE id=?", (workflow_id,)).fetchone() is None:
                raise WorkflowError("workflow_not_found")
            rows = db.execute(
                "SELECT sequence,from_state,to_state,code,created_at "
                "FROM workflow_events WHERE workflow_id=? ORDER BY sequence",
                (workflow_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def advance(self, workflow_id: str) -> dict[str, Any]:
        """Advance synchronous seams until the workflow must wait for a domain."""

        workflow_id = self._identifier(workflow_id)
        with self._lock:
            for _ in range(_WORKFLOW_PROGRESS_LIMIT):
                record = self.get(workflow_id)
                state = record["state"]
                if record["code"] == _CANCELLATION_REQUESTED:
                    progressed = self._advance_once(record)
                    if not progressed:
                        return self.get(workflow_id)
                    continue
                if (
                    state == "attention_required"
                    and record["outputs"]
                    and any(
                        item["upload_source_id"] is None
                        for item in record["outputs"]
                    )
                ):
                    # Upload preparation only creates local drafts. An explicit
                    # advance may safely replay the first unfinished segment by
                    # its stable idempotency key; already checkpointed segments
                    # stay drafts until the complete fan-out is confirmed.
                    self._transition(
                        workflow_id, "preparing_upload", "", expected=record
                    )
                    continue
                if state == "attention_required" and record["upload_job_ids"]:
                    snapshot = self._inspect_upload(record)
                    self._sync_upload_job_ids(record, snapshot)
                    if snapshot.status == "ready":
                        self._record_upload_outcome(record, snapshot)
                        return self.get(workflow_id)
                    if snapshot.status == "waiting":
                        self._transition(
                            workflow_id,
                            (
                                "awaiting_upload_confirmation"
                                if snapshot.code
                                else "uploading"
                            ),
                            snapshot.code,
                            expected=record,
                        )
                        continue
                    return self.get(workflow_id)
                if (
                    state == "attention_required"
                    and record["batch_id"] is None
                    and record["code"] in _PREFLIGHT_RETRY_CODES
                ):
                    # Environment setup, credentials, or a selected account may
                    # have been repaired after the zero-side-effect preflight.
                    # Only an explicit advance reaches inactive attention rows.
                    self._transition(workflow_id, "created", "", expected=record)
                    continue
                if (
                    state == "attention_required"
                    and record["code"] in _AI_LEDGER_REVIEW_CODES
                    and record["edit_project_id"]
                ):
                    if record["edit_plan_id"]:
                        snapshot = self.adapter.inspect_edit(record["edit_plan_id"])
                        if snapshot.status in {"failed", "attention"}:
                            code = snapshot.code or "edit_attention_required"
                            if code != record["code"]:
                                return self._attention(
                                    workflow_id, code, expected=record
                                )
                            return record
                        if snapshot.status == "waiting":
                            self._transition(
                                workflow_id,
                                "awaiting_edit_confirmation",
                                snapshot.code or "",
                                expected=record,
                            )
                            continue
                        if snapshot.status == "ready":
                            if not self._record_edit_outputs(record, snapshot):
                                return self.get(workflow_id)
                            self._transition(
                                workflow_id,
                                "preparing_upload",
                                "",
                                expected=record,
                            )
                            continue
                        return self._attention(
                            workflow_id,
                            "workflow_domain_data_invalid",
                            expected=record,
                        )
                    if record["profile"]["ai"] is None:
                        return self._attention(
                            workflow_id,
                            "workflow_domain_data_invalid",
                            expected=record,
                        )
                    snapshot = self.adapter.advance_ai(
                        workflow_id,
                        record["edit_project_id"],
                        record["profile"]["edit_recipe"],
                        record["profile"]["ai"],
                        authorize=False,
                        explicit=False,
                    )
                    if snapshot.status in {"failed", "attention"}:
                        code = snapshot.code or "ai_review_required"
                        if code != record["code"]:
                            return self._attention(
                                workflow_id, code, expected=record
                            )
                        return record
                    if snapshot.status in {"waiting", "review"}:
                        self._transition(
                            workflow_id,
                            "awaiting_ai_review",
                            snapshot.code or "",
                            expected=record,
                        )
                        continue
                    if (
                        snapshot.status != "ready"
                        or not snapshot.plan_id
                        or snapshot.draft_version is None
                    ):
                        return self._attention(
                            workflow_id,
                            "workflow_domain_data_invalid",
                            expected=record,
                        )
                    record = self._set_refs(
                        workflow_id,
                        expected=record,
                        edit_draft_version=snapshot.draft_version,
                        edit_plan_id=snapshot.plan_id,
                    )
                    self._transition(
                        workflow_id,
                        "awaiting_edit_confirmation",
                        "",
                        expected=record,
                    )
                    continue
                if state not in _ACTIVE_STATES:
                    return record
                progressed = self._advance_once(record)
                if not progressed:
                    return self.get(workflow_id)
            record = self.get(workflow_id)
            if record["code"] == _CANCELLATION_REQUESTED:
                return record
            return self._attention(
                workflow_id, "workflow_progress_limit", expected=record
            )

    def cancel(self, workflow_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Persist cancellation intent, then reconcile the furthest domain seam."""

        workflow_id = self._identifier(workflow_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise WorkflowError("invalid_revision")
        with self._lock:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM workflows WHERE id=?", (workflow_id,)
                ).fetchone()
                if row is None:
                    raise WorkflowError("workflow_not_found")
                record = self._public(row)
                if record["state"] == "canceled":
                    return record
                if record["state"] == "completed":
                    raise WorkflowError("workflow_state_conflict")
                if record["code"] != _CANCELLATION_REQUESTED:
                    if record["revision"] != expected_revision:
                        raise WorkflowError("workflow_revision_conflict")
                    if record["state"] not in _CANCELLABLE_STATES:
                        raise WorkflowError("workflow_state_conflict")
                    now = _now()
                    changed = db.execute(
                        "UPDATE workflows SET code=?,revision=revision+1,updated_at=?,"
                        "finished_at=? WHERE id=? AND revision=? AND state=? AND code=?",
                        (
                            _CANCELLATION_REQUESTED,
                            now,
                            now if record["state"] == "attention_required" else None,
                            workflow_id,
                            record["revision"],
                            record["state"],
                            record["code"],
                        ),
                    ).rowcount
                    if changed != 1:
                        raise WorkflowError("workflow_revision_conflict")
                    self._event(
                        db,
                        workflow_id,
                        record["state"],
                        record["state"],
                        _CANCELLATION_REQUESTED,
                        now,
                    )
                elif record["state"] not in _CANCELLABLE_STATES:
                    raise WorkflowError("workflow_state_conflict")
            return self.advance(workflow_id)

    def confirm_edit(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        expected_profile_sha256: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._expected(
                workflow_id, expected_revision, expected_profile_sha256
            )
            if record["profile"]["ai"] is not None and expected_profile_sha256 is None:
                raise WorkflowError("workflow_profile_confirmation_required")
            if record["state"] != "awaiting_edit_confirmation" or not record["edit_plan_id"]:
                raise WorkflowError("workflow_state_conflict")
            try:
                self.adapter.confirm_edit(record["edit_plan_id"])
            except WorkflowError as error:
                if error.code in _AI_LEDGER_REVIEW_CODES:
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            self._transition(workflow_id, "rendering", "", expected=record)
            return self.advance(workflow_id)

    def confirm_ai(
        self,
        workflow_id: str,
        *,
        expected_revision: int,
        expected_profile_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Authorize exactly the AI action or result review currently waiting."""

        with self._lock:
            record = self._expected(
                workflow_id, expected_revision, expected_profile_sha256
            )
            if expected_profile_sha256 is None:
                raise WorkflowError("workflow_profile_confirmation_required")
            if record["state"] != "awaiting_ai_review" or not record["edit_project_id"]:
                raise WorkflowError("workflow_state_conflict")
            try:
                snapshot = self.adapter.advance_ai(
                    workflow_id,
                    record["edit_project_id"],
                    record["profile"]["edit_recipe"],
                    record["profile"]["ai"],
                    authorize=True,
                    explicit=True,
                )
            except WorkflowError as error:
                if error.code in _AI_LEDGER_REVIEW_CODES:
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            if snapshot.status in {"failed", "attention"}:
                return self._attention(
                    workflow_id,
                    snapshot.code or "ai_review_required",
                    expected=record,
                )
            if snapshot.status == "ready":
                if not snapshot.plan_id or snapshot.draft_version is None:
                    return self._attention(
                        workflow_id, "workflow_data_invalid", expected=record
                    )
                record = self._set_refs(
                    workflow_id,
                    expected=record,
                    edit_draft_version=snapshot.draft_version,
                    edit_plan_id=snapshot.plan_id,
                )
                self._transition(
                    workflow_id,
                    "awaiting_edit_confirmation",
                    "",
                    expected=record,
                )
            else:
                self._transition(
                    workflow_id,
                    "awaiting_ai_review",
                    snapshot.code,
                    expected=record,
                )
            return self.advance(workflow_id)

    def confirm_upload(self, workflow_id: str, *, expected_revision: int) -> dict[str, Any]:
        with self._lock:
            record = self._expected(workflow_id, expected_revision)
            if record["state"] != "awaiting_upload_confirmation":
                raise WorkflowError("workflow_state_conflict")
            original_job_ids = tuple(record["upload_job_ids"])
            snapshot = self._inspect_upload(record)
            self._sync_upload_job_ids(record, snapshot)
            if snapshot.job_ids is not None and snapshot.job_ids != original_job_ids:
                return self.get(workflow_id)
            if snapshot.status in {"failed", "attention"}:
                return self._attention(
                    workflow_id,
                    snapshot.code or "upload_attention_required",
                    expected=record,
                )
            if snapshot.status == "ready":
                self._record_upload_outcome(record, snapshot)
                return self.get(workflow_id)
            try:
                self.adapter.confirm_uploads(
                    record["upload_job_ids"],
                    record["profile"]["upload"]["account_bindings"],
                )
            except WorkflowError as error:
                if error.code == "account_session_changed":
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            self._transition(workflow_id, "uploading", "", expected=record)
            return self.advance(workflow_id)

    def retry(self, workflow_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Create an explicitly reviewable successor for failed AI or rendering."""

        with self._lock:
            record = self._expected(workflow_id, expected_revision)
            if record["state"] != "attention_required" or not record["edit_project_id"]:
                raise WorkflowError("workflow_retry_not_available")
            if record["code"] in _AI_LEDGER_REVIEW_CODES:
                raise WorkflowError("workflow_retry_not_available")
            if record["edit_plan_id"] is not None:
                if record["edit_output_ids"] or record["upload_job_ids"]:
                    raise WorkflowError("workflow_retry_not_available")
                snapshot = self.adapter.inspect_edit(record["edit_plan_id"])
                if snapshot.status == "waiting" and snapshot.code in {
                    "explicit_confirmation_required",
                    "restart_confirmation_required",
                }:
                    self._transition(
                        workflow_id,
                        "awaiting_edit_confirmation",
                        "render_retry_confirmation_required",
                        expected=record,
                    )
                    return self.get(workflow_id)
                try:
                    plan_id = self.adapter.retry_edit(
                        workflow_id, record["edit_plan_id"]
                    )
                except WorkflowError as error:
                    if error.code in _AI_LEDGER_REVIEW_CODES:
                        return self._attention(
                            workflow_id, error.code, expected=record
                        )
                    raise
                record = self._set_refs(
                    workflow_id, expected=record, edit_plan_id=plan_id
                )
                self._transition(
                    workflow_id,
                    "awaiting_edit_confirmation",
                    "render_retry_confirmation_required",
                    expected=record,
                )
                return self.get(workflow_id)
            if record["profile"]["ai"] is None:
                raise WorkflowError("workflow_retry_not_available")
            try:
                self.adapter.retry_ai(
                    workflow_id,
                    record["edit_project_id"],
                    record["profile"]["edit_recipe"],
                    record["profile"]["ai"],
                )
            except WorkflowError as error:
                if error.code in _AI_LEDGER_REVIEW_CODES:
                    return self._attention(
                        workflow_id, error.code, expected=record
                    )
                raise
            self._transition(
                workflow_id,
                "awaiting_ai_review",
                "ai_retry_confirmation_required",
                expected=record,
            )
            return self.get(workflow_id)

    def require_attention(
        self,
        workflow_id: str,
        code: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            code = "workflow_failed"
        with self._lock:
            record = (
                self.get(self._identifier(workflow_id))
                if expected_revision is None
                else self._expected(workflow_id, expected_revision)
            )
            if record["code"] == _CANCELLATION_REQUESTED:
                return record
            if record["state"] not in _ACTIVE_STATES:
                return record
            return self._attention(record["id"], code, expected=record)

    def _advance_once(self, record: dict[str, Any]) -> bool:
        workflow_id, state = record["id"], record["state"]
        if record["code"] == _CANCELLATION_REQUESTED:
            try:
                snapshot = self._cancel_furthest_domain(record)
            except WorkflowError as error:
                self._attention(
                    workflow_id,
                    error.code or "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            if snapshot is None or snapshot.status == "stopped":
                self._transition(
                    workflow_id,
                    "canceled",
                    "workflow_canceled",
                    expected=record,
                )
                return True
            if snapshot.status == "waiting":
                return False
            if snapshot.status == "attention":
                self._attention(
                    workflow_id,
                    snapshot.code or "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            if snapshot.status == "upload_completed":
                return self._record_upload_outcome(
                    record,
                    UploadSnapshot("ready", outcome=snapshot.outcome),
                )
            self._attention(
                workflow_id, "workflow_domain_data_invalid", expected=record
            )
            return False
        if state == "created":
            try:
                self._preflight(
                    record["profile"],
                    expected_account_bindings=record["profile"]["upload"][
                        "account_bindings"
                    ],
                )
            except WorkflowError as error:
                if error.code in _PREFLIGHT_WAIT_CODES:
                    self._transition(
                        workflow_id, "created", error.code, expected=record
                    )
                else:
                    self._attention(workflow_id, error.code, expected=record)
                return False
            batch_id = self.adapter.create_download(
                workflow_id,
                record["source_url"],
                record["profile"]["download_credential_mode"],
            )
            record = self._set_refs(
                workflow_id, expected=record, batch_id=batch_id
            )
            self._transition(workflow_id, "downloading", "", expected=record)
            return True
        if state == "downloading":
            if not record["batch_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            snapshot = self.adapter.inspect_download(record["batch_id"])
            if snapshot.status == "waiting":
                return False
            if snapshot.status != "ready" or not snapshot.asset_id:
                self._attention(
                    workflow_id,
                    snapshot.code or "download_attention_required",
                    expected=record,
                )
                return False
            record = self._set_refs(
                workflow_id,
                expected=record,
                download_asset_id=snapshot.asset_id,
            )
            self._transition(
                workflow_id, "preparing_edit", "", expected=record
            )
            return True
        if state == "preparing_edit":
            if not record["download_asset_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            prepared = self.adapter.prepare_edit(
                workflow_id,
                record["download_asset_id"],
                record["profile"]["edit_recipe"],
            )
            record = self._set_refs(
                workflow_id,
                expected=record,
                edit_project_id=prepared.project_id,
                edit_draft_version=prepared.draft_version,
                edit_plan_id=prepared.plan_id,
            )
            next_state = (
                "awaiting_ai_review"
                if prepared.awaiting_ai_review
                else "awaiting_edit_confirmation"
            )
            self._transition(workflow_id, next_state, "", expected=record)
            return True
        if state == "awaiting_ai_review":
            if not record["edit_project_id"] or record["profile"]["ai"] is None:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            if record["code"] and (
                not record["auto_confirm_edit"]
                or record["code"] not in _AUTO_AI_REVIEW_CODES
            ):
                return False
            snapshot = self.adapter.advance_ai(
                workflow_id,
                record["edit_project_id"],
                record["profile"]["edit_recipe"],
                record["profile"]["ai"],
                authorize=record["auto_confirm_edit"],
                explicit=False,
            )
            if snapshot.status in {"waiting", "review"}:
                if snapshot.code != record["code"]:
                    self._transition(
                        workflow_id,
                        "awaiting_ai_review",
                        snapshot.code,
                        expected=record,
                    )
                return False
            if snapshot.status != "ready":
                self._attention(
                    workflow_id,
                    snapshot.code or "ai_review_required",
                    expected=record,
                )
                return False
            if not snapshot.plan_id or snapshot.draft_version is None:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            record = self._set_refs(
                workflow_id,
                expected=record,
                edit_draft_version=snapshot.draft_version,
                edit_plan_id=snapshot.plan_id,
            )
            self._transition(
                workflow_id,
                "awaiting_edit_confirmation",
                "",
                expected=record,
            )
            return True
        if state == "awaiting_edit_confirmation":
            if not record["edit_plan_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            snapshot = self.adapter.inspect_edit(record["edit_plan_id"])
            if snapshot.status == "waiting":
                if record["code"] not in _AUTO_EDIT_REVIEW_CODES:
                    return False
                code = snapshot.code or record["code"]
                if code and code != record["code"]:
                    record = self._transition(
                        workflow_id, state, code, expected=record
                    )
                if (
                    not record["auto_confirm_edit"]
                    or code not in _AUTO_EDIT_REVIEW_CODES
                ):
                    return False
            if snapshot.status == "failed" or snapshot.status == "attention":
                self._attention(
                    workflow_id,
                    snapshot.code or "edit_attention_required",
                    expected=record,
                )
                return False
            if snapshot.status == "ready":
                if not self._record_edit_outputs(record, snapshot):
                    return False
                self._transition(
                    workflow_id, "preparing_upload", "", expected=record
                )
                return True
            if snapshot.status != "waiting":
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            self.adapter.confirm_edit(record["edit_plan_id"])
            self._transition(workflow_id, "rendering", "", expected=record)
            return True
        if state == "rendering":
            if not record["edit_plan_id"]:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            snapshot = self.adapter.inspect_edit(record["edit_plan_id"])
            if snapshot.status == "waiting":
                if snapshot.code:
                    self._transition(
                        workflow_id,
                        "awaiting_edit_confirmation",
                        (
                            "edit_restart_confirmation_required"
                            if snapshot.code == "restart_confirmation_required"
                            else snapshot.code
                        ),
                        expected=record,
                    )
                return False
            if snapshot.status != "ready":
                self._attention(
                    workflow_id,
                    snapshot.code or "edit_attention_required",
                    expected=record,
                )
                return False
            if not self._record_edit_outputs(record, snapshot):
                return False
            self._transition(
                workflow_id, "preparing_upload", "", expected=record
            )
            return True
        if state == "preparing_upload":
            outputs = record["outputs"]
            if not outputs:
                self._attention(
                    workflow_id, "workflow_data_invalid", expected=record
                )
                return False
            pending = next(
                (item for item in outputs if item["upload_source_id"] is None), None
            )
            if pending is None:
                self._transition(
                    workflow_id,
                    "awaiting_upload_confirmation",
                    "",
                    expected=record,
                )
                return True
            try:
                prepared = self.adapter.prepare_upload(
                    workflow_id,
                    pending["edit_output_id"],
                    record["edit_cover_id"],
                    record["profile"]["upload"],
                    segment_ordinal=pending["segment_ordinal"],
                )
            except WorkflowError as error:
                self._attention(workflow_id, error.code, expected=record)
                return False
            job_ids = tuple(prepared.job_ids)
            account_ids = record["profile"]["upload"]["account_ids"]
            bindings = record["profile"]["upload"]["account_bindings"]
            platforms = {item["account_id"]: item["platform"] for item in bindings}
            if len(job_ids) != len(account_ids):
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            replacement = [dict(item) for item in outputs]
            index = pending["segment_ordinal"] - 1
            replacement[index] = {
                **pending,
                "upload_source_id": prepared.source_id,
                "targets": [
                    {
                        "account_id": account_id,
                        "platform": platforms[account_id],
                        "job_id": job_id,
                    }
                    for account_id, job_id in zip(account_ids, job_ids, strict=True)
                ],
            }
            if record["upload_cover_id"] not in {None, prepared.cover_id}:
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            self._set_refs(
                workflow_id,
                expected=record,
                outputs=replacement,
                upload_cover_id=prepared.cover_id,
            )
            return True
        if state == "awaiting_upload_confirmation":
            snapshot = self._inspect_upload(record)
            self._sync_upload_job_ids(record, snapshot)
            if snapshot.status == "failed" or snapshot.status == "attention":
                self._attention(
                    workflow_id,
                    snapshot.code or "upload_attention_required",
                    expected=record,
                )
                return False
            if snapshot.status == "ready":
                return self._record_upload_outcome(record, snapshot)
            if snapshot.status != "waiting":
                self._attention(
                    workflow_id,
                    "workflow_domain_data_invalid",
                    expected=record,
                )
                return False
            if record["code"] not in _AUTO_UPLOAD_REVIEW_CODES:
                return False
            if snapshot.code and snapshot.code != record["code"]:
                record = self._transition(
                    workflow_id, state, snapshot.code, expected=record
                )
            if (
                not record["auto_confirm_upload"]
                or snapshot.code not in _AUTO_UPLOAD_REVIEW_CODES
            ):
                return False
            self.adapter.confirm_uploads(
                record["upload_job_ids"],
                record["profile"]["upload"]["account_bindings"],
            )
            self._transition(workflow_id, "uploading", "", expected=record)
            return True
        if state == "uploading":
            snapshot = self._inspect_upload(record)
            self._sync_upload_job_ids(record, snapshot)
            if snapshot.status == "waiting":
                if snapshot.code:
                    self._transition(
                        workflow_id,
                        "awaiting_upload_confirmation",
                        snapshot.code,
                        expected=record,
                    )
                return False
            if snapshot.status != "ready":
                self._attention(
                    workflow_id,
                    snapshot.code or "upload_attention_required",
                    expected=record,
                )
                return False
            return self._record_upload_outcome(record, snapshot)
        return False

    def _cancel_furthest_domain(
        self, record: Mapping[str, Any]
    ) -> CancellationSnapshot | None:
        pending_upload = next(
            (
                output
                for output in record["outputs"]
                if output["upload_source_id"] is None
            ),
            None,
        )
        if pending_upload is not None:
            snapshot = self._cancel_via_discovery(
                "cancel_uploads_for_workflow",
                "upload_discovery_unavailable",
                record["id"],
                pending_upload["edit_output_id"],
                pending_upload["segment_ordinal"],
                record["upload_job_ids"],
                expected_targets=self._upload_targets(record),
                expected_account_ids=record["profile"]["upload"]["account_ids"],
                expected_account_bindings=record["profile"]["upload"][
                    "account_bindings"
                ],
                expected_upload=record["profile"]["upload"],
                expected_cover_id=record["edit_cover_id"],
            )
            if snapshot.status == "upload_completed":
                return CancellationSnapshot(
                    "attention", code="upload_partially_completed"
                )
            return snapshot
        if record["upload_job_ids"]:
            return self.adapter.cancel_uploads(
                record["upload_job_ids"],
                expected_targets=self._upload_targets(record),
                expected_account_bindings=record["profile"]["upload"][
                    "account_bindings"
                ],
            )
        if record["edit_plan_id"]:
            if not record["edit_project_id"] or not record["download_asset_id"]:
                return CancellationSnapshot(
                    "attention", code="workflow_domain_data_invalid"
                )
            return self.adapter.cancel_edit(
                record["edit_plan_id"],
                expected_project_id=record["edit_project_id"],
                expected_name=f"Open-Flame workflow {record['id']}",
                expected_source_asset_id=record["download_asset_id"],
            )
        if record["edit_project_id"]:
            if not record["download_asset_id"]:
                return CancellationSnapshot(
                    "attention", code="workflow_domain_data_invalid"
                )
            return self._cancel_via_discovery(
                "cancel_edit_for_workflow",
                "edit_discovery_unavailable",
                record["id"],
                expected_project_id=record["edit_project_id"],
                expected_name=f"Open-Flame workflow {record['id']}",
                expected_source_asset_id=record["download_asset_id"],
                expected_recipe=record["profile"]["edit_recipe"],
            )
        if record["download_asset_id"]:
            return self._cancel_via_discovery(
                "cancel_edit_for_workflow",
                "edit_discovery_unavailable",
                record["id"],
                expected_project_id=None,
                expected_name=f"Open-Flame workflow {record['id']}",
                expected_source_asset_id=record["download_asset_id"],
                expected_recipe=record["profile"]["edit_recipe"],
            )
        if record["batch_id"]:
            return self.adapter.cancel_download(
                record["batch_id"],
                expected_name=record["batch_name"],
                expected_source_url=record["source_url"],
            )
        if not record["download_asset_id"]:
            return self._cancel_via_discovery(
                "cancel_download_for_workflow",
                "download_discovery_unavailable",
                record["id"],
                expected_name=record["batch_name"],
                expected_source_url=record["source_url"],
            )
        return CancellationSnapshot(
            "attention", code="workflow_domain_data_invalid"
        )

    def _cancel_via_discovery(
        self,
        method_name: str,
        unavailable_code: str,
        *args: Any,
        **kwargs: Any,
    ) -> CancellationSnapshot:
        method = getattr(self.adapter, method_name, None)
        if not callable(method):
            return CancellationSnapshot("attention", code=unavailable_code)
        return method(*args, **kwargs)

    def _record_upload_outcome(
        self, record: Mapping[str, Any], snapshot: UploadSnapshot
    ) -> bool:
        """Finish with the exact acknowledgement observed by the upload domain."""

        codes = {
            "submitted": "submission_acknowledged",
            "draft_saved": "platform_draft_saved",
            "mixed": "submission_and_draft_acknowledged",
        }
        code = codes.get(snapshot.outcome or "")
        if code is None:
            self._attention(
                record["id"], "upload_outcome_missing", expected=record
            )
            return False
        self._transition(record["id"], "completed", code, expected=record)
        return True

    def _record_edit_outputs(
        self, record: dict[str, Any], snapshot: object
    ) -> bool:
        try:
            output_ids = _output_ids_from_snapshot(snapshot)
            updated = self._set_refs(
                record["id"],
                expected=record,
                outputs=_unprepared_outputs(output_ids),
                edit_cover_id=getattr(snapshot, "cover_id", None),
            )
        except WorkflowError:
            self._attention(
                record["id"],
                "workflow_domain_data_invalid",
                expected=record,
            )
            return False
        record.clear()
        record.update(updated)
        return True

    @staticmethod
    def _upload_targets(record: Mapping[str, Any]) -> list[dict[str, str]]:
        return [
            {
                "job_id": target["job_id"],
                "source_id": output["upload_source_id"],
                "account_id": target["account_id"],
                "platform": target["platform"],
            }
            for output in record["outputs"]
            for target in output["targets"]
        ]

    def _inspect_upload(self, record: Mapping[str, Any]) -> UploadSnapshot:
        expected_targets = self._upload_targets(record)
        if len(expected_targets) != len(record["upload_job_ids"]):
            raise WorkflowError("workflow_data_invalid")
        return self.adapter.inspect_upload(
            record["upload_job_ids"], expected_targets=expected_targets
        )

    def _sync_upload_job_ids(
        self, record: dict[str, Any], snapshot: UploadSnapshot
    ) -> None:
        if snapshot.job_ids is None:
            return
        current = tuple(record["upload_job_ids"])
        if snapshot.job_ids != current:
            if (
                not isinstance(snapshot.job_ids, Sequence)
                or isinstance(snapshot.job_ids, (str, bytes))
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            replacement = tuple(snapshot.job_ids)
            if (
                len(replacement) != len(current)
                or any(
                    not isinstance(item, str)
                    or not _HEX_IDENTIFIER.fullmatch(item)
                    for item in replacement
                )
                or len(set(replacement)) != len(replacement)
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            iterator = iter(replacement)
            outputs: list[dict[str, Any]] = []
            for output in record["outputs"]:
                targets = [
                    {**target, "job_id": next(iterator)}
                    for target in output["targets"]
                ]
                outputs.append({**output, "targets": targets})
            try:
                next(iterator)
            except StopIteration:
                pass
            else:
                raise WorkflowError("workflow_domain_data_invalid")
            updated = self._set_refs(
                record["id"], expected=record, outputs=outputs
            )
            record.clear()
            record.update(updated)

    def _identifier(self, value: str) -> str:
        if not isinstance(value, str) or not _HEX_IDENTIFIER.fullmatch(value):
            raise WorkflowError("workflow_not_found")
        return value

    def _expected(
        self,
        workflow_id: str,
        revision: int,
        expected_profile_sha256: str | None = None,
    ) -> dict[str, Any]:
        workflow_id = self._identifier(workflow_id)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise WorkflowError("invalid_revision")
        record = self.get(workflow_id)
        if record["code"] == _CANCELLATION_REQUESTED:
            raise WorkflowError("workflow_state_conflict")
        if record["revision"] != revision:
            raise WorkflowError("workflow_revision_conflict")
        if expected_profile_sha256 is not None:
            if (
                not isinstance(expected_profile_sha256, str)
                or not _SHA256.fullmatch(expected_profile_sha256)
            ):
                raise WorkflowError("invalid_workflow_profile_confirmation")
            if not hmac.compare_digest(
                record["profile_sha256"], expected_profile_sha256
            ):
                raise WorkflowError("workflow_profile_changed")
        return record

    @staticmethod
    def _mutation_identity(
        workflow_id: str, expected: Mapping[str, Any]
    ) -> tuple[int, str, str]:
        try:
            expected_id = expected["id"]
            revision = expected["revision"]
            state = expected["state"]
            code = expected["code"]
        except (KeyError, TypeError):
            raise WorkflowError("workflow_data_invalid") from None
        if (
            expected_id != workflow_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(state, str)
            or not isinstance(code, str)
        ):
            raise WorkflowError("workflow_data_invalid")
        return revision, state, code

    def _set_refs(
        self,
        workflow_id: str,
        *,
        expected: Mapping[str, Any] | None = None,
        **values: object,
    ) -> dict[str, Any]:
        allowed = {
            "batch_id",
            "download_asset_id",
            "edit_project_id",
            "edit_draft_version",
            "edit_plan_id",
            "edit_cover_id",
            "upload_cover_id",
            "outputs",
        }
        if not values or set(values) - allowed:
            raise WorkflowError("workflow_data_invalid")
        download_identifier_fields = {
            "batch_id",
            "download_asset_id",
        }
        identifier_fields = {
            "edit_project_id",
            "edit_plan_id",
            "edit_cover_id",
            "upload_cover_id",
        }
        for name in download_identifier_fields & values.keys():
            value = values[name]
            if value is not None and not _download_identifier(value):
                raise WorkflowError("workflow_data_invalid")
        for name in identifier_fields & values.keys():
            value = values[name]
            if value is not None and (
                not isinstance(value, str) or not _HEX_IDENTIFIER.fullmatch(value)
            ):
                raise WorkflowError("workflow_data_invalid")
        if "edit_draft_version" in values:
            version = values["edit_draft_version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise WorkflowError("workflow_data_invalid")
        assignments: list[str] = []
        parameters: list[object] = []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError("workflow_not_found")
            identity = (
                (row["revision"], row["state"], row["code"])
                if expected is None
                else self._mutation_identity(workflow_id, expected)
            )
            if (row["revision"], row["state"], row["code"]) != identity:
                raise WorkflowError("workflow_revision_conflict")
            if "outputs" in values:
                try:
                    profile = json.loads(row["profile_json"])
                except (TypeError, ValueError):
                    raise WorkflowError("workflow_data_invalid") from None
                normalized_profile, _, _ = _profile(profile, bound_accounts=True)
                values["outputs"] = _workflow_outputs(
                    values["outputs"], normalized_profile
                )
            for name, value in values.items():
                column = "outputs_json" if name == "outputs" else name
                if name == "outputs":
                    value = json.dumps(
                        value,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                assignments.append(f"{column}=?")
                parameters.append(value)
            assignments.extend(("revision=revision+1", "updated_at=?"))
            parameters.extend((_now(), workflow_id, *identity))
            changed = db.execute(
                f"UPDATE workflows SET {','.join(assignments)} "
                "WHERE id=? AND revision=? AND state=? AND code=?",
                parameters,
            ).rowcount
            if changed != 1:
                raise WorkflowError("workflow_revision_conflict")
            try:
                return self._by_id(db, workflow_id)
            except WorkflowError:
                # Private fixture/setup callers may intentionally assemble a
                # valid state across two writes. Production callers always
                # supply ``expected`` and may not observe an invalid midpoint.
                if expected is not None:
                    raise
                return {
                    "id": workflow_id,
                    "revision": identity[0] + 1,
                    "state": identity[1],
                    "code": identity[2],
                }

    def _transition(
        self,
        workflow_id: str,
        state: str,
        code: str,
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM workflows WHERE id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError("workflow_not_found")
            identity = (
                (row["revision"], row["state"], row["code"])
                if expected is None
                else self._mutation_identity(workflow_id, expected)
            )
            if (row["revision"], row["state"], row["code"]) != identity:
                raise WorkflowError("workflow_revision_conflict")
            previous = row["state"]
            if previous == state and row["code"] == code:
                return self._public(row)
            finished = now if state in {"completed", "attention_required", "canceled"} else None
            changed = db.execute(
                "UPDATE workflows SET state=?,code=?,revision=revision+1,updated_at=?,"
                "finished_at=? WHERE id=? AND revision=? AND state=? AND code=?",
                (state, code, now, finished, workflow_id, *identity),
            ).rowcount
            if changed != 1:
                raise WorkflowError("workflow_revision_conflict")
            self._event(db, workflow_id, previous, state, code, now)
            return self._by_id(db, workflow_id)

    def _attention(
        self,
        workflow_id: str,
        code: str,
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            workflow_id,
            "attention_required",
            code,
            expected=expected,
        )

    def _event(
        self,
        db: sqlite3.Connection,
        workflow_id: str,
        previous: str | None,
        state: str,
        code: str,
        now: str,
    ) -> None:
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM workflow_events WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO workflow_events(workflow_id,sequence,from_state,to_state,code,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (workflow_id, sequence, previous, state, code, now),
        )

    def _by_id(self, db: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise WorkflowError("workflow_not_found")
        return self._public(row)

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            profile = json.loads(row["profile_json"])
            legacy_job_ids = json.loads(row["upload_job_ids_json"])
            raw_outputs = json.loads(row["outputs_json"])
        except (TypeError, ValueError):
            raise WorkflowError("workflow_data_invalid") from None
        if not isinstance(profile, dict) or legacy_job_ids != []:
            raise WorkflowError("workflow_data_invalid")
        normalized_profile, encoded, profile_digest = _profile(
            profile, bound_accounts=True
        )
        if (
            encoded != row["profile_json"]
            or profile_digest != row["profile_sha256"]
            or row["auto_confirm_edit"]
            != int(normalized_profile["auto_confirm_edit"])
            or row["auto_confirm_upload"]
            != int(normalized_profile["auto_confirm_upload"])
        ):
            raise WorkflowError("workflow_data_invalid")
        outputs = _workflow_outputs(raw_outputs, normalized_profile)
        outputs_json = json.dumps(
            outputs,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if (
            outputs_json != row["outputs_json"]
            or row["edit_output_id"] is not None
            or row["upload_source_id"] is not None
        ):
            raise WorkflowError("workflow_data_invalid")
        expected_output_count = len(normalized_profile["edit_recipe"]["segments"]) or 1
        if not workflow_outputs_match_state(
            row["state"], outputs, expected_output_count
        ):
            raise WorkflowError("workflow_data_invalid")
        for field in ("batch_id", "download_asset_id"):
            if row[field] is not None and not _download_identifier(row[field]):
                raise WorkflowError("workflow_data_invalid")
        for field in (
            "edit_project_id",
            "edit_plan_id",
            "edit_cover_id",
            "upload_cover_id",
        ):
            value = row[field]
            if value is not None and (
                not isinstance(value, str) or not _HEX_IDENTIFIER.fullmatch(value)
            ):
                raise WorkflowError("workflow_data_invalid")
        public = dict(row)
        public.pop("request_key")
        public.pop("request_digest")
        public.pop("profile_json")
        public.pop("upload_job_ids_json")
        public.pop("outputs_json")
        edit_output_ids = [item["edit_output_id"] for item in outputs]
        upload_source_ids = [
            item["upload_source_id"]
            for item in outputs
            if item["upload_source_id"] is not None
        ]
        upload_job_ids = [
            target["job_id"]
            for item in outputs
            for target in item["targets"]
        ]
        public["profile"] = normalized_profile
        public["outputs"] = outputs
        public["edit_output_ids"] = edit_output_ids
        public["upload_source_ids"] = upload_source_ids
        public["upload_job_ids"] = upload_job_ids
        public["edit_output_id"] = (
            edit_output_ids[0] if len(edit_output_ids) == 1 else None
        )
        public["upload_source_id"] = (
            upload_source_ids[0] if len(upload_source_ids) == 1 else None
        )
        public["edit_output_count"] = len(edit_output_ids)
        public["upload_job_count"] = len(upload_job_ids)
        public["auto_confirm_edit"] = normalized_profile["auto_confirm_edit"]
        public["auto_confirm_upload"] = normalized_profile["auto_confirm_upload"]
        public["schema_version"] = SCHEMA_VERSION
        return public

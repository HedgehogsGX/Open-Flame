"""Canonical, state-free workflow profile and checkpoint contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..editing.ai_authorization import (
    AiAuthorizationError,
    AiOperationAuthorization,
    parse_operation_authorization,
)
from ..editing.contracts import EditingError, recipe_from_mapping
from ..uploads.contracts import UploadError
from ..uploads.identity import normalize_account_bindings
from .contracts import (
    MAX_WORKFLOW_ACCOUNTS,
    MAX_WORKFLOW_SEGMENTS,
    MAX_WORKFLOW_UPLOAD_JOBS,
    WorkflowError,
)


_AI_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,199}$")
_HEX_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_workflow_mapping(value: Mapping[str, Any]) -> tuple[str, str]:
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


def normalize_workflow_text(value: object, maximum: int, *, required: bool = False) -> str:
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


def normalize_workflow_profile(
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
    if len(parsed_recipe.segments) > MAX_WORKFLOW_SEGMENTS:
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
        transcription_mode_key = "transcription_mode"
        transcription_modes = {"ai", "prefer_source_caption"}
        raw_ai_keys = frozenset(raw_ai) if isinstance(raw_ai, Mapping) else frozenset()
        raw_ai_core_keys = raw_ai_keys - {transcription_mode_key}
        if (
            not isinstance(raw_ai, Mapping)
            or raw_ai_core_keys
            not in {
                frozenset(legacy_ai_keys),
                frozenset(digest_ai_keys),
                frozenset(bound_ai_keys),
            }
            or (
                require_ai_authorization
                and raw_ai_core_keys != frozenset(bound_ai_keys)
            )
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
        if transcription_mode_key in raw_ai_keys:
            transcription_mode = raw_ai.get(transcription_mode_key)
            if (
                not isinstance(transcription_mode, str)
                or transcription_mode not in transcription_modes
            ):
                raise WorkflowError("invalid_workflow_profile")
            normalized_ai[transcription_mode_key] = transcription_mode
        if raw_ai_core_keys in {
            frozenset(digest_ai_keys),
            frozenset(bound_ai_keys),
        }:
            for field in (
                "transcription_authorization_sha256",
                "translation_authorization_sha256",
            ):
                digest = raw_ai.get(field)
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise WorkflowError("invalid_workflow_profile")
                normalized_ai[field] = digest
        if raw_ai_core_keys == frozenset(bound_ai_keys):
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
    raw_upload_keys = frozenset(raw_upload)
    if raw_upload_keys not in {frozenset(expected_upload), frozenset(expected_upload | {"title_mode"})}:
        raise WorkflowError("invalid_workflow_profile")
    title_mode = raw_upload.get("title_mode", "explicit")
    if not isinstance(title_mode, str) or title_mode not in {"explicit", "source"}:
        raise WorkflowError("invalid_workflow_profile")
    normalized_title = normalize_workflow_text(
        raw_upload.get("title"),
        100,
        required=title_mode == "explicit",
    )
    if title_mode == "source" and normalized_title:
        # The source title is intentionally unknown until the download is
        # ready.  Reject hidden placeholders so they cannot change request
        # identity while being ignored during execution.
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
    normalized_tags = [normalize_workflow_text(item, 20, required=True) for item in tags]
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
        if title_mode == "source" and "title" in item:
            override_title = normalize_workflow_text(item.get("title"), 100, required=True)
            if override_title != item.get("title"):
                # The frozen title shown by Workflow must exactly match the
                # upload-domain value.  New source profiles therefore reject
                # a target title that would be normalized later.
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
        try:
            normalized_bindings = list(normalize_account_bindings(raw_bindings))
        except UploadError:
            raise WorkflowError("invalid_workflow_profile") from None
        if {binding["account_id"] for binding in normalized_bindings} != set(
            account_ids
        ):
            raise WorkflowError("invalid_workflow_profile")
    normalized: dict[str, Any] = {
        "download_credential_mode": credential_mode,
        "edit_recipe": recipe,
        "ai": normalized_ai,
        "upload": {
            "account_ids": list(account_ids),
            "title": normalized_title,
            "description": normalize_workflow_text(raw_upload.get("description"), 2000),
            "tags": normalized_tags,
            "category_id": category_id,
            "mode": mode,
            "copyright": copyright_value,
            "source_credit": normalize_workflow_text(raw_upload.get("source_credit"), 200),
            "target_overrides": [dict(item) for item in target_overrides],
        },
        "auto_confirm_edit": auto_edit,
        "auto_confirm_upload": auto_upload,
        "ai_data_egress_accepted": ai_data_egress_accepted,
    }
    if bound_accounts:
        normalized["upload"]["account_bindings"] = normalized_bindings
    # Keep existing explicit-title profiles byte-for-byte canonical.  The
    # source mode is opt-in and is frozen separately once download metadata is
    # available.
    if title_mode == "source":
        normalized["upload"]["title_mode"] = "source"
    encoded, digest = canonical_workflow_mapping(normalized)
    return normalized, encoded, digest


def normalize_resolved_upload(
    value: object,
    profile: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Validate one concrete upload snapshot against its immutable profile."""

    if not isinstance(value, Mapping):
        raise WorkflowError("workflow_data_invalid")
    candidate = dict(profile)
    candidate["upload"] = dict(value)
    normalized, _, _ = normalize_workflow_profile(candidate, bound_accounts=True)
    upload = normalized["upload"]
    if "title_mode" in upload:
        raise WorkflowError("workflow_data_invalid")
    profile_upload = profile.get("upload")
    immutable_fields = (
        "account_ids",
        "description",
        "tags",
        "category_id",
        "mode",
        "copyright",
        "source_credit",
        "account_bindings",
    )
    if (
        not isinstance(profile_upload, Mapping)
        or profile_upload.get("title_mode") != "source"
        or any(upload.get(field) != profile_upload.get(field) for field in immutable_fields)
    ):
        raise WorkflowError("workflow_data_invalid")

    account_ids = upload["account_ids"]
    source_overrides = profile_upload.get("target_overrides")
    resolved_overrides = upload.get("target_overrides")
    if (
        not isinstance(source_overrides, Sequence)
        or isinstance(source_overrides, (str, bytes))
        or not isinstance(resolved_overrides, Sequence)
        or isinstance(resolved_overrides, (str, bytes))
        or [item.get("account_id") for item in resolved_overrides]
        != account_ids
    ):
        raise WorkflowError("workflow_data_invalid")
    source_by_account = {
        item.get("account_id"): item
        for item in source_overrides
        if isinstance(item, Mapping)
    }
    if len(source_by_account) != len(source_overrides):
        raise WorkflowError("workflow_data_invalid")
    for resolved_override in resolved_overrides:
        if not isinstance(resolved_override, Mapping):
            raise WorkflowError("workflow_data_invalid")
        account_id = resolved_override.get("account_id")
        source_override = source_by_account.get(account_id, {"account_id": account_id})
        try:
            resolved_title = normalize_workflow_text(
                resolved_override.get("title"), 100, required=True
            )
        except WorkflowError:
            raise WorkflowError("workflow_data_invalid") from None
        if (
            set(resolved_override) != set(source_override) | {"title"}
            or any(
                resolved_override.get(field) != field_value
                for field, field_value in source_override.items()
            )
            or resolved_title != resolved_override.get("title")
        ):
            raise WorkflowError("workflow_data_invalid")
    encoded, _ = canonical_workflow_mapping(upload)
    return upload, encoded


def normalize_workflow_outputs(
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

__all__ = [
    "canonical_workflow_mapping",
    "normalize_resolved_upload",
    "normalize_workflow_outputs",
    "normalize_workflow_profile",
    "normalize_workflow_text",
]

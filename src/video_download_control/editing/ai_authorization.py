"""Exact, hash-bound authorization and hard budgets for optional AI calls.

The authorization value is deliberately small and contains no endpoint,
credential, or provider response.  It records the exact runtime and model
identity a user reviewed together with operation-specific ceilings.  Every
consumer should compare the persisted value with a freshly constructed value
immediately before a remote or local inference call.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .ai_protocol import (
    PROTOCOL_SCHEMA,
    RUNTIME_ID,
    RUNTIME_VERSION,
    TASK_OPERATIONS,
    AiProtocolError,
    canonical_json_bytes,
    operation_data_egress,
)
from .ai_runtime import AiRuntime, RuntimeModel, RuntimeProvider


AUTHORIZATION_SCHEMA = 1
TRANSLATION_CUES_PER_REQUEST_UNIT = 50
_REMOTE_PROVIDER_KINDS = frozenset({"http", "remote_plugin"})
_PROVIDER_KINDS = _REMOTE_PROVIDER_KINDS | {"plugin"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,159}$")
_AUTHORIZATION_KEYS = frozenset(
    {
        "schema_version",
        "runtime_id",
        "runtime_version",
        "protocol_schema",
        "manifest_sha256",
        "operation",
        "provider_id",
        "provider_kind",
        "model_id",
        "model_revision",
        "execution",
        "data_egress",
        "limits",
    }
)
_LIMIT_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "transcribe": frozenset(
            {"max_requests", "max_audio_ms", "max_audio_bytes"}
        ),
        "translate": frozenset(
            {"max_requests", "max_cues", "max_input_characters"}
        ),
        "synthesize": frozenset(
            {"max_requests", "max_cues", "max_input_characters"}
        ),
    }
)

# These exact ceilings are part of the core release, not provider configuration.
# Keeping one canonical limit set makes the authorization shown by the UI the
# same value reconstructed at every later boundary. A runtime manifest cannot
# silently raise or replace these limits.
CORE_OPERATION_LIMITS: Mapping[str, Mapping[str, int]] = MappingProxyType(
    {
        "transcribe": MappingProxyType(
            {
                "max_requests": 1,
                "max_audio_ms": 30 * 60 * 1000,
                "max_audio_bytes": 25 * 1024 * 1024,
            }
        ),
        "translate": MappingProxyType(
            {
                "max_requests": 20,
                "max_cues": 1_000,
                "max_input_characters": 60_000,
            }
        ),
        "synthesize": MappingProxyType(
            {
                "max_requests": 600,
                "max_cues": 600,
                "max_input_characters": 60_000,
            }
        ),
    }
)


class AiAuthorizationError(ValueError):
    """Stable authorization/budget failure without user or provider content."""

    _CODES = frozenset(
        {
            "ai_authorization_invalid",
            "ai_authorization_binding_required",
            "ai_authorization_changed",
            "ai_budget_invalid",
            "ai_budget_exceeded",
        }
    )

    def __init__(self, code: str):
        self.code = code if code in self._CODES else "ai_authorization_invalid"
        super().__init__(self.code)


def _exact_mapping(value: object, keys: frozenset[str]) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or any(not isinstance(key, str) for key in value)
        or set(value) != keys
    ):
        raise AiAuthorizationError("ai_authorization_invalid")
    return value


def _token(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise AiAuthorizationError("ai_authorization_invalid")
    return value


def _bounded_text(
    value: object, *, maximum: int, optional: bool = False
) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AiAuthorizationError("ai_authorization_invalid")
    normalized = value.strip()
    if not normalized:
        raise AiAuthorizationError("ai_authorization_invalid")
    return normalized


def _positive_integer(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AiAuthorizationError(code)
    return value


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AiAuthorizationError("ai_budget_invalid")
    return value


def _validated_limits(operation: str, value: object) -> Mapping[str, int]:
    keys = _LIMIT_KEYS.get(operation)
    core = CORE_OPERATION_LIMITS.get(operation)
    if keys is None or core is None:
        raise AiAuthorizationError("ai_authorization_invalid")
    row = _exact_mapping(value, keys)
    normalized: dict[str, int] = {}
    for key in sorted(keys):
        limit = _positive_integer(row[key], "ai_authorization_invalid")
        if limit != core[key]:
            raise AiAuthorizationError("ai_authorization_invalid")
        normalized[key] = limit
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class AiOperationAuthorization:
    """One immutable operation consent bound to runtime, model, and budgets."""

    schema_version: int
    runtime_id: str
    runtime_version: str
    protocol_schema: int
    manifest_sha256: str
    operation: str
    provider_id: str
    provider_kind: str
    model_id: str | None
    model_revision: str | None
    execution: str
    data_egress: tuple[str, ...]
    limits: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.schema_version != AUTHORIZATION_SCHEMA:
            raise AiAuthorizationError("ai_authorization_invalid")
        runtime_id = _token(self.runtime_id)
        runtime_version = _token(self.runtime_version)
        provider_id = _token(self.provider_id)
        model_id = _token(self.model_id, optional=True)
        model_revision = _bounded_text(
            self.model_revision, maximum=240, optional=True
        )
        if (
            isinstance(self.protocol_schema, bool)
            or not isinstance(self.protocol_schema, int)
            or self.protocol_schema <= 0
            or not isinstance(self.manifest_sha256, str)
            or not _SHA256.fullmatch(self.manifest_sha256)
            or not isinstance(self.operation, str)
            or self.operation not in TASK_OPERATIONS
            or not isinstance(self.provider_kind, str)
            or self.provider_kind not in _PROVIDER_KINDS
            or not isinstance(self.execution, str)
            or self.execution not in {"local", "remote"}
            or (model_id is None) != (model_revision is None)
        ):
            raise AiAuthorizationError("ai_authorization_invalid")
        try:
            expected_egress = (
                tuple(operation_data_egress(self.operation))
                if self.provider_kind in _REMOTE_PROVIDER_KINDS
                else ()
            )
        except AiProtocolError as exc:
            raise AiAuthorizationError("ai_authorization_invalid") from exc
        if (
            not isinstance(self.data_egress, tuple)
            or self.data_egress != expected_egress
            or self.execution
            != ("remote" if self.provider_kind in _REMOTE_PROVIDER_KINDS else "local")
        ):
            raise AiAuthorizationError("ai_authorization_invalid")
        limits = _validated_limits(self.operation, self.limits)
        object.__setattr__(self, "runtime_id", runtime_id)
        object.__setattr__(self, "runtime_version", runtime_version)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "model_revision", model_revision)
        object.__setattr__(self, "limits", limits)

    def to_dict(self) -> dict[str, object]:
        """Return the exact JSON-safe value whose bytes are hash-bound."""

        return {
            "schema_version": self.schema_version,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "protocol_schema": self.protocol_schema,
            "manifest_sha256": self.manifest_sha256,
            "operation": self.operation,
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "execution": self.execution,
            "data_egress": list(self.data_egress),
            "limits": {key: self.limits[key] for key in sorted(self.limits)},
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()


def parse_operation_authorization(value: object) -> AiOperationAuthorization | None:
    """Parse one exact value; ``None`` denotes an unbound legacy record."""

    if value is None:
        return None
    if isinstance(value, AiOperationAuthorization):
        value = value.to_dict()
    row = _exact_mapping(value, _AUTHORIZATION_KEYS)
    data_egress = row["data_egress"]
    if (
        not isinstance(data_egress, list)
        or any(not isinstance(item, str) for item in data_egress)
        or len(set(data_egress)) != len(data_egress)
    ):
        raise AiAuthorizationError("ai_authorization_invalid")
    return AiOperationAuthorization(
        schema_version=row["schema_version"],  # type: ignore[arg-type]
        runtime_id=row["runtime_id"],  # type: ignore[arg-type]
        runtime_version=row["runtime_version"],  # type: ignore[arg-type]
        protocol_schema=row["protocol_schema"],  # type: ignore[arg-type]
        manifest_sha256=row["manifest_sha256"],  # type: ignore[arg-type]
        operation=row["operation"],  # type: ignore[arg-type]
        provider_id=row["provider_id"],  # type: ignore[arg-type]
        provider_kind=row["provider_kind"],  # type: ignore[arg-type]
        model_id=row["model_id"],  # type: ignore[arg-type]
        model_revision=row["model_revision"],  # type: ignore[arg-type]
        execution=row["execution"],  # type: ignore[arg-type]
        data_egress=tuple(data_egress),
        limits=row["limits"],  # type: ignore[arg-type]
    )


def build_operation_authorization(
    runtime: AiRuntime,
    provider: RuntimeProvider,
    model: RuntimeModel | None,
    operation: str,
    data_egress: Sequence[str] | None = None,
    *,
    limits: Mapping[str, int] | None = None,
) -> AiOperationAuthorization:
    """Construct the authorization for one already verified runtime operation."""

    if (
        not isinstance(runtime, AiRuntime)
        or not isinstance(provider, RuntimeProvider)
        or (model is not None and not isinstance(model, RuntimeModel))
        or not isinstance(provider.kind, str)
        or provider.kind not in _PROVIDER_KINDS
        or not isinstance(operation, str)
        or operation not in TASK_OPERATIONS
        or operation not in provider.operations
        or (model is not None and operation not in model.operations)
        or (model is None and bool(provider.model_ids))
        or (model is not None and model.id not in provider.model_ids)
    ):
        raise AiAuthorizationError("ai_authorization_invalid")
    try:
        expected_egress = (
            tuple(operation_data_egress(operation))
            if provider.kind in _REMOTE_PROVIDER_KINDS
            else ()
        )
    except AiProtocolError as exc:
        raise AiAuthorizationError("ai_authorization_invalid") from exc
    try:
        supplied_egress = (
            None if data_egress is None else tuple(data_egress)
        )
    except TypeError as exc:
        raise AiAuthorizationError("ai_authorization_invalid") from exc
    if (
        not set(expected_egress).issubset(provider.data_egress)
        or supplied_egress is not None
        and supplied_egress != expected_egress
    ):
        raise AiAuthorizationError("ai_authorization_invalid")
    selected_limits: Mapping[str, int] = (
        CORE_OPERATION_LIMITS[operation] if limits is None else limits
    )
    return AiOperationAuthorization(
        schema_version=AUTHORIZATION_SCHEMA,
        runtime_id=RUNTIME_ID,
        runtime_version=RUNTIME_VERSION,
        protocol_schema=PROTOCOL_SCHEMA,
        manifest_sha256=runtime.manifest_sha256,
        operation=operation,
        provider_id=provider.id,
        provider_kind=provider.kind,
        model_id=None if model is None else model.id,
        model_revision=None if model is None else model.revision,
        execution="remote" if provider.kind in _REMOTE_PROVIDER_KINDS else "local",
        data_egress=expected_egress,
        limits=selected_limits,
    )


def authorization_sha256(value: object) -> str:
    authorization = parse_operation_authorization(value)
    if authorization is None:
        raise AiAuthorizationError("ai_authorization_binding_required")
    return authorization.sha256


def authorization_matches(left: object, right: object) -> bool:
    """Return false for absent/invalid values without weakening fail-closed callers."""

    try:
        first = parse_operation_authorization(left)
        second = parse_operation_authorization(right)
    except AiAuthorizationError:
        return False
    return (
        first is not None
        and second is not None
        and hmac.compare_digest(first.sha256, second.sha256)
    )


def require_authorization_match(
    stored: object,
    current: object,
    *,
    expected_sha256: str | None = None,
) -> AiOperationAuthorization:
    """Fail closed unless stored, current, and optional UI digest are identical."""

    persisted = parse_operation_authorization(stored)
    fresh = parse_operation_authorization(current)
    if persisted is None or fresh is None:
        raise AiAuthorizationError("ai_authorization_binding_required")
    if (
        expected_sha256 is not None
        and (
            not isinstance(expected_sha256, str)
            or not _SHA256.fullmatch(expected_sha256)
            or not hmac.compare_digest(expected_sha256, persisted.sha256)
        )
    ):
        raise AiAuthorizationError("ai_authorization_changed")
    if not hmac.compare_digest(persisted.sha256, fresh.sha256):
        raise AiAuthorizationError("ai_authorization_changed")
    return persisted


def _required_authorization(value: object, operation: str) -> AiOperationAuthorization:
    authorization = parse_operation_authorization(value)
    if authorization is None:
        raise AiAuthorizationError("ai_authorization_binding_required")
    if authorization.operation != operation:
        raise AiAuthorizationError("ai_authorization_invalid")
    return authorization


def _cue_characters(cues: Sequence[object]) -> tuple[int, int]:
    if isinstance(cues, (str, bytes, bytearray)) or not isinstance(cues, Sequence):
        raise AiAuthorizationError("ai_budget_invalid")
    total = 0
    for cue in cues:
        if isinstance(cue, Mapping):
            text = cue.get("source_text")
        else:
            text = getattr(cue, "source_text", None)
        if not isinstance(text, str) or not text.strip():
            raise AiAuthorizationError("ai_budget_invalid")
        total += len(text)
    return len(cues), total


def enforce_transcription_budget(
    authorization: object,
    *,
    duration_ms: int,
    audio_bytes: int,
    request_count: int = 1,
) -> dict[str, int | str]:
    """Check complete transcription work before the first provider request."""

    value = _required_authorization(authorization, "transcribe")
    duration = _nonnegative_integer(duration_ms)
    size = _nonnegative_integer(audio_bytes)
    requests = _positive_integer(request_count, "ai_budget_invalid")
    if (
        duration > value.limits["max_audio_ms"]
        or size > value.limits["max_audio_bytes"]
        or requests > value.limits["max_requests"]
    ):
        raise AiAuthorizationError("ai_budget_exceeded")
    return {
        "operation": "transcribe",
        "requests": requests,
        "audio_ms": duration,
        "audio_bytes": size,
    }


def _enforce_text_budget(
    authorization: object,
    operation: str,
    cues: Sequence[object],
    *,
    request_count: int | None,
    additional_characters: int,
) -> dict[str, int | str]:
    value = _required_authorization(authorization, operation)
    cue_count, characters = _cue_characters(cues)
    extra = _nonnegative_integer(additional_characters)
    characters += extra
    requests = (
        estimate_request_units(operation, cue_count)
        if request_count is None
        else _nonnegative_integer(request_count)
    )
    if cue_count == 0:
        if operation != "synthesize" or requests != 0 or characters != 0:
            raise AiAuthorizationError("ai_budget_invalid")
    elif requests == 0:
        raise AiAuthorizationError("ai_budget_invalid")
    if (
        cue_count > value.limits["max_cues"]
        or characters > value.limits["max_input_characters"]
        or requests > value.limits["max_requests"]
    ):
        raise AiAuthorizationError("ai_budget_exceeded")
    return {
        "operation": operation,
        "requests": requests,
        "cues": cue_count,
        "input_characters": characters,
    }


def enforce_translation_budget(
    authorization: object,
    cues: Sequence[object],
    *,
    request_count: int | None = None,
    additional_characters: int = 0,
) -> dict[str, int | str]:
    """Check aggregate translation text and core-side request units."""

    return _enforce_text_budget(
        authorization,
        "translate",
        cues,
        request_count=request_count,
        additional_characters=additional_characters,
    )


def enforce_synthesis_budget(
    authorization: object,
    cues: Sequence[object],
    *,
    request_count: int | None = None,
    additional_characters: int = 0,
) -> dict[str, int | str]:
    """Check aggregate standard-voice text before synthesizing the first cue."""

    return _enforce_text_budget(
        authorization,
        "synthesize",
        cues,
        request_count=request_count,
        additional_characters=additional_characters,
    )


# Speech is the product-facing name; synthesis is the runtime protocol name.
enforce_speech_budget = enforce_synthesis_budget


def estimate_request_units(operation: str, cue_count: int) -> int:
    """Estimate core call units; this is not a provider billing ledger."""

    count = _nonnegative_integer(cue_count)
    if operation == "translate":
        return (count + TRANSLATION_CUES_PER_REQUEST_UNIT - 1) // (
            TRANSLATION_CUES_PER_REQUEST_UNIT
        )
    if operation == "synthesize":
        return count
    if operation == "transcribe":
        return 1 if count else 0
    raise AiAuthorizationError("ai_budget_invalid")


__all__ = [
    "AUTHORIZATION_SCHEMA",
    "CORE_OPERATION_LIMITS",
    "TRANSLATION_CUES_PER_REQUEST_UNIT",
    "AiAuthorizationError",
    "AiOperationAuthorization",
    "authorization_matches",
    "authorization_sha256",
    "build_operation_authorization",
    "enforce_speech_budget",
    "enforce_synthesis_budget",
    "enforce_transcription_budget",
    "enforce_translation_budget",
    "estimate_request_units",
    "parse_operation_authorization",
    "require_authorization_match",
]

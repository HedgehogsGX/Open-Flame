"""Pure upload job and account identity contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .contracts import PLATFORMS, UploadError


ACCOUNT_BINDING_KEYS = frozenset(
    {"account_id", "platform", "session_revision"}
)
UPLOAD_TARGET_KEYS = frozenset(
    {"job_id", "source_id", "account_id", "platform"}
)
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise UploadError("invalid_identifier")
    return value


def _normalize_account_binding(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != ACCOUNT_BINDING_KEYS:
        raise UploadError("invalid_account_bindings")
    account_id = _identifier(value.get("account_id"))
    session_revision = _identifier(value.get("session_revision"))
    platform = value.get("platform")
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise UploadError("invalid_account_bindings")
    return {
        "account_id": account_id,
        "platform": platform,
        "session_revision": session_revision,
    }


def normalize_account_bindings(value: object) -> tuple[dict[str, str], ...]:
    """Decode unique bindings while preserving their supplied order."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise UploadError("invalid_account_bindings")
    if any(
        not isinstance(item, Mapping) or set(item) != ACCOUNT_BINDING_KEYS
        for item in value
    ):
        raise UploadError("invalid_account_bindings")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        binding = _normalize_account_binding(item)
        if binding["account_id"] in seen:
            raise UploadError("invalid_account_bindings")
        seen.add(binding["account_id"])
        normalized.append(binding)
    return tuple(normalized)


def _normalize_upload_target(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != UPLOAD_TARGET_KEYS:
        raise UploadError("invalid_job_batch")
    try:
        job_id = _identifier(value.get("job_id"))
        source_id = _identifier(value.get("source_id"))
        account_id = _identifier(value.get("account_id"))
    except UploadError:
        raise UploadError("invalid_job_batch") from None
    platform = value.get("platform")
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise UploadError("invalid_job_batch")
    return {
        "job_id": job_id,
        "source_id": source_id,
        "account_id": account_id,
        "platform": platform,
    }


def normalize_upload_targets(
    job_ids: object,
    expected_targets: object,
    *,
    maximum_jobs: int = 30,
) -> tuple[dict[str, str], ...]:
    """Decode ordered upload slots aligned with their supplied root IDs."""

    if (
        not isinstance(job_ids, Sequence)
        or isinstance(job_ids, (str, bytes))
        or not 1 <= len(job_ids) <= maximum_jobs
        or not isinstance(expected_targets, Sequence)
        or isinstance(expected_targets, (str, bytes))
        or len(expected_targets) != len(job_ids)
    ):
        raise UploadError("invalid_job_batch")
    normalized_ids: list[str] = []
    for value in job_ids:
        try:
            normalized_ids.append(_identifier(value))
        except UploadError:
            raise UploadError("invalid_job_batch") from None
    normalized_targets = tuple(
        _normalize_upload_target(target) for target in expected_targets
    )
    if any(
        target["job_id"] != normalized_ids[index]
        for index, target in enumerate(normalized_targets)
    ):
        raise UploadError("invalid_job_batch")
    return normalized_targets


def normalize_upload_job_batch(
    job_ids: object,
    expected_targets: object = None,
    *,
    maximum_jobs: int = 30,
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...] | None]:
    """Decode an ordered upload root-ID batch and its optional target slots."""

    if (
        not isinstance(job_ids, Sequence)
        or isinstance(job_ids, (str, bytes))
        or not 1 <= len(job_ids) <= maximum_jobs
    ):
        raise UploadError("invalid_job_batch")
    normalized_ids: list[str] = []
    seen: set[str] = set()
    for value in job_ids:
        try:
            job_id = _identifier(value)
        except UploadError:
            raise UploadError("invalid_job_batch") from None
        if job_id in seen:
            raise UploadError("invalid_job_batch")
        seen.add(job_id)
        normalized_ids.append(job_id)

    if expected_targets is None:
        return tuple(normalized_ids), None
    normalized_targets = normalize_upload_targets(
        normalized_ids,
        expected_targets,
        maximum_jobs=maximum_jobs,
    )
    return tuple(normalized_ids), normalized_targets


def bind_current_upload_target(
    job: object,
    expected_target: object,
) -> dict[str, str]:
    """Bind an expected slot to its current retry leaf after identity checks."""

    expected = _normalize_upload_target(expected_target)
    if not isinstance(job, Mapping):
        raise UploadError("invalid_job_batch")
    try:
        current_id = _identifier(job.get("id"))
    except UploadError:
        raise UploadError("invalid_job_batch") from None
    if any(
        job.get(field) != expected[field]
        for field in ("source_id", "account_id", "platform")
    ):
        raise UploadError("invalid_job_batch")
    return {**expected, "job_id": current_id}


__all__ = [
    "ACCOUNT_BINDING_KEYS",
    "UPLOAD_TARGET_KEYS",
    "bind_current_upload_target",
    "normalize_account_bindings",
    "normalize_upload_job_batch",
    "normalize_upload_targets",
]

"""Fail-closed adapters for the local download, edit, and upload stores."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from uuid import UUID

from ..credential_defaults import CredentialDefaultsError
from ..domain import ErrorCode
from ..editing.contracts import EditingError, recipe_from_mapping
from ..service import BatchService, BatchValidationError
from ..uploads.contracts import UploadError
from ..uploads.metadata import cover_slot
from ..uploads.identity import (
    bind_current_upload_target,
    normalize_account_bindings,
    normalize_upload_job_batch,
    normalize_upload_targets,
)
from .contracts import (
    AiSnapshot,
    CancellationSnapshot,
    DownloadSnapshot,
    EditPrepared,
    EditSnapshot,
    MAX_WORKFLOW_ACCOUNTS,
    MAX_WORKFLOW_SEGMENTS,
    UploadPrepared,
    UploadSnapshot,
    workflow_upload_request,
    workflow_upload_request_key,
)
from .contracts import WorkflowError

if TYPE_CHECKING:
    from ..editing.manager import EditingManager


_HEX_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_DOWNLOAD_STATES = {
    "queued",
    "failed",
    "duplicate",
    "ready",
    "partial_success",
    "canceled",
}
_DOWNLOAD_JOB_STATES = {
    "queued",
    "probing",
    "downloading",
    "postprocessing",
    "verifying",
    "ready",
    "failed",
    "canceled",
}
_DOWNLOAD_ACTIVE_JOB_STATES = {
    "queued",
    "probing",
    "downloading",
    "postprocessing",
    "verifying",
}
_DOWNLOAD_FAILURE_CODES = frozenset(code.value for code in ErrorCode)
_EDIT_WAITING_STATES = {"review", "queued", "running", "canceling"}
_UPLOAD_WAITING_STATES = {"draft", "queued", "running"}
_UPLOAD_SUCCESS_STATES = {"submitted", "draft_saved"}
_AI_UNCERTAIN_CODES = {
    "ai_remote_result_unknown",
    "ai_remote_retry_blocked",
    "ai_remote_reconciliation_required",
    "ai_remote_accepted_without_result",
    "ai_remote_abandoned",
}
_UPLOAD_INPUT_KEYS = {
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
_UPLOAD_KEYS = _UPLOAD_INPUT_KEYS | {"account_bindings"}
_UPLOAD_TITLE_MODE_KEY = "title_mode"
_UPLOAD_PREFER_DOWNLOAD_COVER_KEY = "prefer_download_cover"
_SOURCE_METADATA_ERROR = "workflow_source_metadata_unavailable"
_COVER_KEYS = {"cover_landscape_asset_id", "cover_portrait_asset_id"}
_TRANSCRIPTION_MODE_KEY = "transcription_mode"
_TRANSCRIPTION_MODES = frozenset({"ai", "prefer_source_caption"})
_SOURCE_CAPTION_MODEL = re.compile(
    r"^source-caption-v1:([0-9a-f-]{36}):([0-9a-f]{64})$"
)
_SOURCE_CAPTION_MIME_TYPES = {
    "application/x-subrip": "srt",
    "text/vtt": "vtt",
}
_CAPTION_LANGUAGE = re.compile(
    r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"
)
_LEGACY_AI_KEYS = frozenset({"transcription_provider", "transcription_model"})
_DIGEST_AI_KEYS = _LEGACY_AI_KEYS | frozenset({
    "transcription_authorization_sha256",
    "translation_authorization_sha256",
})
_BOUND_AI_KEYS = _DIGEST_AI_KEYS | frozenset({
    "transcription_authorization",
    "translation_authorization",
})


class UploadManager(Protocol):
    """The small lazy-manager seam needed by the workflow adapter."""

    def get(self) -> Any: ...


class DownloadControl(Protocol):
    """The one download mutation needed by workflow cancellation."""

    def request_cancel_input(self, input_record_id: str, *, now: datetime) -> object: ...


DownloadAssetResolver = Callable[[str], tuple[Path, str]]
DownloadCaptionResolver = Callable[[str], Mapping[str, Any]]
DownloadCoverResolver = Callable[[str], Mapping[str, Any] | None]
DownloadRuntimeProbe = Callable[[], Mapping[str, Any]]


def _domain_failure(error: object, fallback: str) -> NoReturn:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
        code = fallback
    raise WorkflowError(code) from None


def _hex_identifier(value: object) -> str:
    if not isinstance(value, str) or _HEX_IDENTIFIER.fullmatch(value) is None:
        raise WorkflowError("workflow_domain_data_invalid")
    return value


def _download_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise WorkflowError("workflow_domain_data_invalid")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (AttributeError, ValueError):
        raise WorkflowError("workflow_domain_data_invalid") from None
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WorkflowError("workflow_domain_data_invalid")
    return value


def _managed_import_id(
    workflow_id: str,
    origin_kind: str,
    origin_id: str,
    expected_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "origin_id": origin_id,
            "origin_kind": origin_kind,
            "request_key": f"wf-{workflow_id}-{origin_kind}-import-v1",
            "sha256": expected_sha256,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()[:32]


def _record_id(record: object) -> str:
    if not isinstance(record, Mapping):
        raise WorkflowError("workflow_domain_data_invalid")
    return _hex_identifier(record.get("id"))


def _ai_shape(ai: Mapping[str, Any]) -> tuple[frozenset[object], str]:
    """Return the stable AI contract keys and its optional caption policy."""

    try:
        keys = frozenset(ai)
    except TypeError:
        raise WorkflowError("workflow_domain_data_invalid") from None
    mode = ai.get(_TRANSCRIPTION_MODE_KEY, "ai")
    if not isinstance(mode, str) or mode not in _TRANSCRIPTION_MODES:
        raise WorkflowError("workflow_domain_data_invalid")
    if _TRANSCRIPTION_MODE_KEY in keys:
        keys = keys - {_TRANSCRIPTION_MODE_KEY}
    return keys, mode


def _validate_retry_successors(
    record_ids: Sequence[str],
    successors: Mapping[str, str],
    *,
    error_code: str,
) -> None:
    """Reject cycles in a retry forest without limiting legitimate history."""

    resolved: set[str] = set()
    for start in record_ids:
        current = start
        path: set[str] = set()
        while current in successors and current not in resolved:
            if current in path:
                raise WorkflowError(error_code)
            path.add(current)
            current = successors[current]
        resolved.update(path)


@dataclass(slots=True)
class LocalWorkflowAdapter:
    """Connect the workflow state machine without sharing media ownership."""

    batch_service: BatchService
    editing_manager: EditingManager
    upload_manager: UploadManager
    download_asset_resolver: DownloadAssetResolver
    download_runtime_probe: DownloadRuntimeProbe | None = None
    download_control: DownloadControl | None = None
    download_caption_resolver: DownloadCaptionResolver | None = None
    download_cover_resolver: DownloadCoverResolver | None = None

    def preflight(
        self,
        recipe: Mapping[str, Any],
        ai: Mapping[str, Any] | None,
        upload: Mapping[str, Any],
        *,
        cover_aspect_ratio: str | None,
        expected_account_bindings: Sequence[Mapping[str, str]] | None = None,
    ) -> Sequence[Mapping[str, str]]:
        """Check every local execution dependency before download side effects."""

        self._validate_download_runtime()
        if self.editing_manager.media_ready is not True:
            raise WorkflowError("processor_not_configured")
        raw_overrides = (
            upload.get("target_overrides", ()) if isinstance(upload, Mapping) else ()
        )
        if (
            cover_aspect_ratio is not None
            and isinstance(raw_overrides, Sequence)
            and not isinstance(raw_overrides, (str, bytes))
            and any(
                isinstance(item, Mapping)
                and any(item.get(key) is not None for key in _COVER_KEYS)
                for item in raw_overrides
            )
        ):
            raise WorkflowError("upload_cover_override_conflict")
        bindings = self.validate_upload(
            upload,
            cover_aspect_ratio=cover_aspect_ratio,
            expected_account_bindings=expected_account_bindings,
        )
        if ai is not None:
            self._validate_ai(recipe, ai)
        return bindings

    def _validate_download_runtime(self) -> None:
        if self.download_runtime_probe is None:
            raise WorkflowError("download_worker_unobserved")
        try:
            status = self.download_runtime_probe()
        except Exception:
            raise WorkflowError("download_runtime_unavailable") from None
        if not isinstance(status, Mapping):
            raise WorkflowError("download_runtime_unavailable")
        if status.get("mode") != "managed_direct":
            raise WorkflowError("download_worker_unobserved")
        state = status.get("state")
        if state == "paused" or status.get("queue_paused") is True:
            raise WorkflowError("download_queue_paused")
        if state == "stale":
            raise WorkflowError("download_worker_stale")
        if state != "online":
            raise WorkflowError("download_worker_not_ready")
        if status.get("network_download_enabled") is not True:
            raise WorkflowError("download_network_disabled")
        age = status.get("heartbeat_age_seconds")
        timeout = status.get("heartbeat_timeout_seconds")
        if (
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(age)
            or not math.isfinite(timeout)
            or age < 0
            or timeout <= 0
            or age >= timeout
        ):
            raise WorkflowError("download_worker_stale")

    def _validate_ai(
        self,
        recipe: Mapping[str, Any],
        ai: Mapping[str, Any],
    ) -> None:
        try:
            normalized = recipe_from_mapping(recipe)
        except EditingError as error:
            _domain_failure(error, "invalid_recipe")
        ai_keys, _transcription_mode = _ai_shape(ai)
        if (
            not normalized.translation.enabled
            or not normalized.dubbing.enabled
            or normalized.dubbing.authorization is None
            or ai_keys != _BOUND_AI_KEYS
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        operations = (
            (
                "transcribe",
                ai.get("transcription_provider"),
                ai.get("transcription_model"),
                ai.get("transcription_authorization"),
                ai.get("transcription_authorization_sha256"),
                None,
            ),
            (
                "translate",
                normalized.translation.provider,
                normalized.translation.model,
                ai.get("translation_authorization"),
                ai.get("translation_authorization_sha256"),
                None,
            ),
            (
                "synthesize",
                normalized.dubbing.provider,
                normalized.dubbing.model,
                normalized.dubbing.authorization,
                normalized.dubbing.authorization.sha256,
                normalized.dubbing.voice,
            ),
        )
        try:
            for operation, provider, model, authorization, digest, voice in operations:
                if not isinstance(provider, str) or not isinstance(model, str):
                    raise WorkflowError("workflow_domain_data_invalid")
                if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                    raise WorkflowError("workflow_domain_data_invalid")
                self.editing_manager.validate_ai_operation(
                    operation,
                    provider,
                    model,
                    authorization,
                    expected_authorization_sha256=digest,
                    voice_id=voice,
                )
        except EditingError as error:
            _domain_failure(error, "ai_operation_blocked")

    def validate_upload(
        self,
        upload: Mapping[str, Any],
        *,
        cover_aspect_ratio: str | None,
        expected_account_bindings: Sequence[Mapping[str, str]] | None = None,
    ) -> Sequence[Mapping[str, str]]:
        if not isinstance(upload, Mapping) or set(upload) not in {
            frozenset(_UPLOAD_INPUT_KEYS),
            frozenset(_UPLOAD_INPUT_KEYS | {_UPLOAD_TITLE_MODE_KEY}),
            frozenset(
                _UPLOAD_INPUT_KEYS | {_UPLOAD_PREFER_DOWNLOAD_COVER_KEY}
            ),
            frozenset(
                _UPLOAD_INPUT_KEYS
                | {_UPLOAD_TITLE_MODE_KEY, _UPLOAD_PREFER_DOWNLOAD_COVER_KEY}
            ),
        }:
            raise WorkflowError("workflow_domain_data_invalid")
        title_mode = upload.get(_UPLOAD_TITLE_MODE_KEY, "explicit")
        if not isinstance(title_mode, str) or title_mode not in {"explicit", "source"}:
            raise WorkflowError("workflow_domain_data_invalid")
        prefer_download_cover = upload.get(
            _UPLOAD_PREFER_DOWNLOAD_COVER_KEY, False
        )
        if (
            type(prefer_download_cover) is not bool
            or (
                _UPLOAD_PREFER_DOWNLOAD_COVER_KEY in upload
                and prefer_download_cover is not True
            )
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        if expected_account_bindings is not None and (
            not isinstance(expected_account_bindings, Sequence)
            or isinstance(expected_account_bindings, (str, bytes))
            or any(
                not isinstance(binding, Mapping)
                for binding in expected_account_bindings
            )
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        try:
            options = dict(upload)
            options.pop(_UPLOAD_TITLE_MODE_KEY, None)
            options.pop(_UPLOAD_PREFER_DOWNLOAD_COVER_KEY, None)
            if title_mode == "source":
                # Source metadata is unavailable before the download.  A
                # bounded placeholder lets the upload domain validate account,
                # platform, runtime, cover, and all other metadata now; the
                # concrete source title is frozen by ``resolve_upload`` before
                # any upload source or job is created.
                options["title"] = "source"
            if expected_account_bindings is not None:
                options["expected_account_bindings"] = [
                    dict(binding) for binding in expected_account_bindings
                ]
            targets = self.upload_manager.get().preflight_jobs(**options)
        except UploadError as error:
            _domain_failure(error, "upload_metadata_invalid")
        bindings: list[Mapping[str, str]] = []
        for target in targets:
            binding = target.get("account_binding")
            if not isinstance(binding, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            bindings.append(binding)
        if cover_aspect_ratio is None:
            return bindings
        dimensions = {
            "16:9": (16, 9),
            "4:3": (4, 3),
            "3:4": (3, 4),
            "9:16": (9, 16),
            "1:1": (1, 1),
        }.get(cover_aspect_ratio)
        if dimensions is None:
            raise WorkflowError("workflow_cover_incompatible")
        for target in targets:
            platform = target.get("platform")
            if (
                not isinstance(platform, str)
                or cover_slot(platform, *dimensions) is None
            ):
                raise WorkflowError("workflow_cover_incompatible")
        return bindings

    def create_download(
        self, workflow_id: str, source_url: str, credential_mode: str
    ) -> str:
        workflow_id = _hex_identifier(workflow_id)
        if credential_mode not in {"anonymous", "use_default"}:
            raise WorkflowError("invalid_workflow_profile")
        batch_name = f"Open-Flame workflow {workflow_id}"
        try:
            matches = self.batch_service.find_batches_by_name(batch_name)
            if len(matches) > 1:
                raise WorkflowError("download_batch_conflict")
            if matches:
                return self._matching_batch_id(matches[0], batch_name, source_url)
            batch = self.batch_service.create_batch(
                name=batch_name,
                raw_inputs=[source_url],
                credential_mode=credential_mode,
            )
        except BatchValidationError:
            raise WorkflowError("download_request_invalid") from None
        except CredentialDefaultsError:
            raise WorkflowError("download_credentials_unavailable") from None
        return self._matching_batch_id(batch, batch_name, source_url)

    def inspect_download(self, batch_id: str) -> DownloadSnapshot:
        batch_id = _download_identifier(batch_id)
        observation = self.batch_service.inspect_single_input_download(batch_id)
        if observation is None:
            return DownloadSnapshot("attention", code="download_batch_not_found")
        if not isinstance(observation, Mapping):
            return DownloadSnapshot("attention", code="download_state_invalid")
        batch = observation.get("batch")
        if not isinstance(batch, Mapping):
            return DownloadSnapshot("attention", code="download_state_invalid")
        if batch.get("id") != batch_id:
            return DownloadSnapshot("attention", code="download_state_invalid")
        state = batch.get("status")
        if state not in _DOWNLOAD_STATES:
            return DownloadSnapshot("attention", code="download_state_unknown")
        jobs = batch.get("jobs")
        if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
            return DownloadSnapshot("attention", code="download_state_invalid")
        job_states: list[str] = []
        for job in jobs:
            if not isinstance(job, Mapping) or job.get("status") not in _DOWNLOAD_JOB_STATES:
                return DownloadSnapshot("attention", code="download_state_unknown")
            job_states.append(job["status"])

        assets = observation.get("assets")
        if not isinstance(assets, list):
            return DownloadSnapshot("attention", code="download_state_invalid")
        if len(assets) > 1:
            return DownloadSnapshot("attention", code="download_multiple_assets")

        if state == "duplicate":
            owner = observation.get("duplicate_owner")
            if not isinstance(owner, Mapping) or owner.get("valid") is not True:
                return DownloadSnapshot("attention", code="download_state_invalid")
            if owner.get("job_kind") != "download":
                return DownloadSnapshot("attention", code="download_state_invalid")
            input_state = owner.get("input_status")
            job_state = owner.get("job_status")
            input_error = owner.get("input_error_code")
            input_error_message = owner.get("input_error_message")
            job_error = owner.get("job_error_code")
            if job_state not in _DOWNLOAD_JOB_STATES:
                return DownloadSnapshot("attention", code="download_state_unknown")
            if input_state == "queued":
                if (
                    job_state not in _DOWNLOAD_ACTIVE_JOB_STATES
                    or input_error is not None
                    or input_error_message is not None
                    or job_error is not None
                    or assets
                ):
                    return DownloadSnapshot(
                        "attention", code="download_state_invalid"
                    )
                return DownloadSnapshot("waiting")
            if input_state == "ready":
                if (
                    job_state != "ready"
                    or input_error is not None
                    or input_error_message is not None
                    or job_error is not None
                    or not assets
                ):
                    return DownloadSnapshot(
                        "attention", code="download_state_invalid"
                    )
            elif input_state == "failed":
                if (
                    job_state != "failed"
                    or not isinstance(input_error, str)
                    or input_error != job_error
                    or input_error not in _DOWNLOAD_FAILURE_CODES
                ):
                    return DownloadSnapshot(
                        "attention", code="download_state_invalid"
                    )
                if assets:
                    return DownloadSnapshot(
                        "attention", code="download_state_invalid"
                    )
                return DownloadSnapshot("failed", code=input_error)
            elif input_state == "canceled":
                if (
                    job_state != "canceled"
                    or input_error is not None
                    or input_error_message is not None
                    or job_error is not None
                    or assets
                ):
                    return DownloadSnapshot(
                        "attention", code="download_state_invalid"
                    )
                return DownloadSnapshot("failed", code="download_canceled")
            else:
                return DownloadSnapshot("attention", code="download_state_invalid")

        if assets:
            asset = assets[0]
            if not isinstance(asset, Mapping):
                return DownloadSnapshot("attention", code="download_state_invalid")
            try:
                asset_id = _download_identifier(asset.get("asset_id"))
            except WorkflowError:
                return DownloadSnapshot("attention", code="download_state_invalid")
            if asset.get("media_kind") != "video":
                return DownloadSnapshot("attention", code="download_asset_not_video")
            return DownloadSnapshot(
                "ready",
                asset_id=asset_id,
                source_title=self._download_source_text(
                    asset.get("source_title"), maximum=1024
                ),
            )

        if state == "queued":
            if not any(
                job_state in _DOWNLOAD_ACTIVE_JOB_STATES
                for job_state in job_states
            ):
                return DownloadSnapshot("attention", code="download_state_invalid")
            return DownloadSnapshot("waiting")
        return DownloadSnapshot("failed", code="download_no_ready_video")

    def cancel_download(
        self,
        batch_id: str,
        *,
        expected_name: str,
        expected_source_url: str,
    ) -> CancellationSnapshot:
        """Request cancellation for the workflow batch's sole input."""

        batch_id = _download_identifier(batch_id)
        if (
            not isinstance(expected_name, str)
            or not expected_name
            or not isinstance(expected_source_url, str)
            or not expected_source_url
        ):
            return CancellationSnapshot(
                "attention", code="download_batch_mismatch"
            )
        batch = self.batch_service.get_batch(batch_id)
        if not isinstance(batch, Mapping):
            return CancellationSnapshot("attention", code="download_batch_not_found")
        if (
            batch.get("id") != batch_id
            or batch.get("name") != expected_name
        ):
            return CancellationSnapshot(
                "attention", code="download_batch_mismatch"
            )
        inputs = batch.get("inputs")
        if (
            not isinstance(inputs, Sequence)
            or isinstance(inputs, (str, bytes))
            or len(inputs) != 1
            or not isinstance(inputs[0], Mapping)
        ):
            return CancellationSnapshot("attention", code="download_input_set_invalid")
        try:
            input_id = _download_identifier(inputs[0].get("id"))
        except WorkflowError:
            return CancellationSnapshot("attention", code="download_input_set_invalid")
        if (
            inputs[0].get("batch_id") != batch_id
            or inputs[0].get("raw_text") != expected_source_url
        ):
            return CancellationSnapshot("attention", code="download_input_set_invalid")
        if self.download_control is None:
            return CancellationSnapshot(
                "attention", code="download_cancellation_unavailable"
            )
        try:
            result = self.download_control.request_cancel_input(
                input_id, now=datetime.now(UTC)
            )
        except Exception:
            return CancellationSnapshot(
                "attention", code="download_cancellation_failed"
            )
        if result is None:
            return CancellationSnapshot("attention", code="download_input_not_found")

        refreshed = self.batch_service.get_batch(batch_id)
        if not isinstance(refreshed, Mapping):
            return CancellationSnapshot("attention", code="download_batch_not_found")
        if (
            refreshed.get("id") != batch_id
            or refreshed.get("name") != expected_name
        ):
            return CancellationSnapshot(
                "attention", code="download_batch_mismatch"
            )
        refreshed_inputs = refreshed.get("inputs")
        jobs = refreshed.get("jobs")
        if (
            not isinstance(refreshed_inputs, Sequence)
            or isinstance(refreshed_inputs, (str, bytes))
            or len(refreshed_inputs) != 1
            or not isinstance(refreshed_inputs[0], Mapping)
            or refreshed_inputs[0].get("id") != input_id
            or refreshed_inputs[0].get("batch_id") != batch_id
            or refreshed_inputs[0].get("raw_text") != expected_source_url
            or not isinstance(jobs, Sequence)
            or isinstance(jobs, (str, bytes))
        ):
            return CancellationSnapshot("attention", code="download_input_set_invalid")
        states: list[str] = []
        for job in jobs:
            if (
                not isinstance(job, Mapping)
                or job.get("input_record_id") != input_id
                or job.get("status") not in _DOWNLOAD_JOB_STATES
            ):
                return CancellationSnapshot(
                    "attention", code="download_state_unknown"
                )
            states.append(job["status"])
        if any(
            state in {"probing", "downloading", "postprocessing", "verifying"}
            for state in states
        ):
            return CancellationSnapshot(
                "waiting", code="download_cancellation_pending"
            )
        if any(state == "queued" for state in states):
            return CancellationSnapshot(
                "attention", code="download_cancellation_failed"
            )
        input_state = refreshed_inputs[0].get("status")
        if input_state not in _DOWNLOAD_STATES:
            return CancellationSnapshot("attention", code="download_state_unknown")
        return CancellationSnapshot("stopped")

    def cancel_download_for_workflow(
        self,
        workflow_id: str,
        *,
        expected_name: str,
        expected_source_url: str,
    ) -> CancellationSnapshot:
        """Discover and stop a workflow batch without creating a replacement."""

        workflow_id = _hex_identifier(workflow_id)
        if (
            expected_name != f"Open-Flame workflow {workflow_id}"
            or not isinstance(expected_source_url, str)
            or not expected_source_url
        ):
            return CancellationSnapshot(
                "attention", code="download_batch_mismatch"
            )
        try:
            matches = self.batch_service.find_batches_by_name(expected_name)
        except Exception:
            return CancellationSnapshot(
                "attention", code="download_discovery_failed"
            )
        if not isinstance(matches, Sequence) or isinstance(matches, (str, bytes)):
            return CancellationSnapshot(
                "attention", code="download_discovery_failed"
            )
        if not matches:
            return CancellationSnapshot("stopped")
        if len(matches) != 1:
            return CancellationSnapshot(
                "attention", code="download_batch_conflict"
            )
        try:
            batch_id = self._matching_batch_id(
                matches[0], expected_name, expected_source_url
            )
        except WorkflowError as error:
            return CancellationSnapshot("attention", code=error.code)
        return self.cancel_download(
            batch_id,
            expected_name=expected_name,
            expected_source_url=expected_source_url,
        )

    def prepare_edit(
        self,
        workflow_id: str,
        asset_id: str,
        recipe: Mapping[str, Any],
    ) -> EditPrepared:
        workflow_id = _hex_identifier(workflow_id)
        asset_id = _download_identifier(asset_id)
        try:
            normalized_recipe = recipe_from_mapping(recipe)
        except EditingError as error:
            _domain_failure(error, "invalid_recipe")
        try:
            path, expected_sha256 = self.download_asset_resolver(asset_id)
        except Exception:
            raise WorkflowError("download_asset_unavailable") from None
        path = Path(path)
        expected_sha256 = _sha256(expected_sha256)
        name = f"download-{asset_id}{path.suffix.lower()}"
        try:
            source = self.editing_manager.invoke(
                "import_source",
                path,
                name,
                expected_sha256,
                source_asset_id=asset_id,
                idempotency_key=f"wf-{workflow_id}-edit-source",
            )
            source_id = _record_id(source)
            if source.get("source_asset_id") != asset_id:
                raise WorkflowError("workflow_domain_data_invalid")
            project = self.editing_manager.invoke(
                "create_project",
                source_id,
                f"Open-Flame workflow {workflow_id}",
                f"wf-{workflow_id}-edit-project",
                recipe=normalized_recipe,
            )
            project_id = _record_id(project)
            if project.get("source_id") != source_id:
                raise WorkflowError("workflow_domain_data_invalid")
            draft_version = project.get("current_version")
            if (
                isinstance(draft_version, bool)
                or not isinstance(draft_version, int)
                or draft_version < 1
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            ai_enabled = (
                normalized_recipe.translation.enabled
                or normalized_recipe.dubbing.enabled
            )
            plan_id = None
            if not ai_enabled:
                plan = self.editing_manager.invoke(
                    "create_plan",
                    project_id,
                    draft_version,
                    f"wf-{workflow_id}-edit-plan",
                )
                plan_id = _record_id(plan)
                if (
                    plan.get("project_id") != project_id
                    or plan.get("draft_version") != draft_version
                ):
                    raise WorkflowError("workflow_domain_data_invalid")
        except EditingError as error:
            _domain_failure(error, "editing_failed")
        return EditPrepared(
            project_id=project_id,
            draft_version=draft_version,
            plan_id=plan_id,
            awaiting_ai_review=ai_enabled,
        )

    def advance_ai(
        self,
        workflow_id: str,
        project_id: str,
        recipe: Mapping[str, Any],
        ai: Mapping[str, Any],
        *,
        authorize: bool,
        explicit: bool,
    ) -> AiSnapshot:
        workflow_id = _hex_identifier(workflow_id)
        project_id = _hex_identifier(project_id)
        if type(authorize) is not bool or type(explicit) is not bool:
            raise WorkflowError("workflow_domain_data_invalid")
        try:
            normalized = recipe_from_mapping(recipe)
        except EditingError as error:
            _domain_failure(error, "invalid_recipe")
        if (
            not normalized.translation.enabled
            or not isinstance(ai, Mapping)
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        ai_keys, transcription_mode = _ai_shape(ai)
        if ai_keys in {_LEGACY_AI_KEYS, _DIGEST_AI_KEYS}:
            return AiSnapshot("attention", code="ai_authorization_required")
        if ai_keys != _BOUND_AI_KEYS:
            raise WorkflowError("workflow_domain_data_invalid")
        transcription_provider = ai.get("transcription_provider")
        transcription_model = ai.get("transcription_model")
        if not isinstance(transcription_provider, str) or not isinstance(
            transcription_model, str
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        transcription_authorization_sha256 = ai.get(
            "transcription_authorization_sha256"
        )
        translation_authorization_sha256 = ai.get(
            "translation_authorization_sha256"
        )
        if not isinstance(transcription_authorization_sha256, str) or not isinstance(
            translation_authorization_sha256, str
        ):
            raise WorkflowError("workflow_domain_data_invalid")

        language = normalized.translation.source_language
        authorization = [authorize]
        clip_options = self._transcription_clip_options(normalized)
        try:
            transcript: Mapping[str, Any] | AiSnapshot | None = None
            if transcription_mode == "prefer_source_caption":
                transcript = self._preferred_source_caption(
                    workflow_id,
                    project_id,
                    language,
                    normalized.translation.target_language,
                    clip_options,
                    segment_boundaries_ms=tuple(
                        segment.end_ms for segment in normalized.segments[:-1]
                    ),
                    authorization=authorization,
                    explicit=explicit,
                )
                if isinstance(transcript, AiSnapshot):
                    return transcript
            if transcript is None:
                transcription = self.editing_manager.create_ai_task(
                    project_id,
                    "transcribe",
                    transcription_provider,
                    transcription_model,
                    {
                        "language": None if language.casefold() == "auto" else language,
                        "word_timestamps": True,
                        "vad": True,
                        **clip_options,
                    },
                    f"wf-{workflow_id}-transcribe",
                    expected_authorization_sha256=transcription_authorization_sha256,
                )
                transcript = self._advance_ai_task(
                    transcription,
                    expected_project_id=project_id,
                    authorization=authorization,
                    explicit=explicit,
                    confirmation_code="ai_transcription_confirmation_required",
                    review_code="ai_transcription_review_required",
                )
                if isinstance(transcript, AiSnapshot):
                    return transcript

            source_language = transcript.get("language")
            source_revision_id = _hex_identifier(transcript.get("id"))
            if not isinstance(source_language, str):
                raise WorkflowError("workflow_domain_data_invalid")
            target_language = normalized.translation.target_language
            if source_language.casefold() == target_language.casefold():
                raise WorkflowError("workflow_translation_languages_match")
            translation = self.editing_manager.create_ai_task(
                project_id,
                "translate",
                normalized.translation.provider,
                normalized.translation.model,
                {
                    "source_language": source_language,
                    "target_language": target_language,
                    "glossary": [],
                },
                f"wf-{workflow_id}-translate",
                source_revision_id=source_revision_id,
                expected_authorization_sha256=translation_authorization_sha256,
            )
            translated = self._advance_ai_task(
                translation,
                expected_project_id=project_id,
                authorization=authorization,
                explicit=explicit,
                confirmation_code="ai_translation_confirmation_required",
                review_code="ai_translation_review_required",
            )
            if isinstance(translated, AiSnapshot):
                return translated

            translated_revision_id = _record_id(translated)
            ready_recipe = normalized.to_dict()
            ready_recipe["translation"]["source_language"] = source_language
            ready_recipe["translation"]["state"] = "ready"
            ready_recipe["translation"]["revision_id"] = translated_revision_id
            if normalized.dubbing.enabled:
                ready_recipe["dubbing"]["state"] = "ready"
            legacy_ready_recipe = {
                **ready_recipe,
                "translation": {
                    key: value
                    for key, value in ready_recipe["translation"].items()
                    if key != "revision_id"
                },
            }
            draft = self.editing_manager.invoke("draft", project_id)
            if not isinstance(draft, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            draft_version = draft.get("version")
            if (
                isinstance(draft_version, bool)
                or not isinstance(draft_version, int)
                or draft_version < 1
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            if draft.get("recipe") not in (ready_recipe, legacy_ready_recipe):
                if draft.get("recipe") != normalized.to_dict():
                    raise WorkflowError("workflow_edit_draft_changed")
                draft = self.editing_manager.invoke(
                    "update_draft",
                    project_id,
                    draft_version,
                    ready_recipe,
                    f"wf-{workflow_id}-ai-ready-draft",
                )
                draft_version = draft.get("version")
                if (
                    isinstance(draft_version, bool)
                    or not isinstance(draft_version, int)
                    or draft_version < 1
                    or draft.get("recipe") != ready_recipe
                ):
                    raise WorkflowError("workflow_domain_data_invalid")
            plan = self.editing_manager.invoke(
                "create_plan",
                project_id,
                draft_version,
                f"wf-{workflow_id}-edit-plan",
                timeline_revision_id=translated_revision_id,
            )
            plan_id = _record_id(plan)
            if (
                plan.get("project_id") != project_id
                or plan.get("draft_version") != draft_version
                or plan.get("timeline_revision_id") != translated_revision_id
                or plan.get("recipe") != draft.get("recipe")
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            return AiSnapshot(
                "ready", draft_version=draft_version, plan_id=plan_id
            )
        except EditingError as error:
            _domain_failure(error, "ai_pipeline_failed")

    def _preferred_source_caption(
        self,
        workflow_id: str,
        project_id: str,
        source_language: str,
        target_language: str,
        clip_options: Mapping[str, int],
        *,
        segment_boundaries_ms: Sequence[int],
        authorization: list[bool],
        explicit: bool,
    ) -> Mapping[str, Any] | AiSnapshot | None:
        """Reuse one verified download caption before creating a transcription task."""

        try:
            project = self.editing_manager.invoke("project", project_id)
        except EditingError as error:
            _domain_failure(error, "ai_pipeline_failed")
        if not isinstance(project, Mapping) or project.get("id") != project_id:
            raise WorkflowError("workflow_domain_data_invalid")
        source_asset_id = _download_identifier(project.get("source_asset_id"))
        timeline = self._existing_source_caption(project_id)
        newly_imported = False
        if timeline is None:
            if self._transcription_fallback_started(project_id):
                return None
            candidate = self._select_source_caption(
                source_asset_id, source_language, target_language
            )
            if candidate is None:
                return None
            if self.download_caption_resolver is None:
                raise WorkflowError("download_caption_unavailable")
            try:
                resolved = self.download_caption_resolver(candidate["artifact_id"])
            except Exception:
                raise WorkflowError("download_caption_unavailable") from None
            if not isinstance(resolved, Mapping):
                raise WorkflowError("download_caption_unavailable")
            payload = resolved.get("payload")
            if not isinstance(payload, bytes) or any(
                resolved.get(key) != candidate[key]
                for key in (
                    "artifact_id",
                    "asset_id",
                    "artifact_path",
                    "mime_type",
                    "language",
                    "sha256",
                    "origin",
                    "tool_name",
                    "tool_version",
                )
            ):
                raise WorkflowError("download_caption_unavailable")
            try:
                timeline = self.editing_manager.invoke(
                    "import_transcription_timeline",
                    project_id,
                    payload,
                    candidate["language"],
                    candidate["mime_type"],
                    candidate["sha256"],
                    candidate["asset_id"],
                    candidate["artifact_id"],
                    f"wf-{workflow_id}-source-caption",
                    clip_start_ms=clip_options.get("clip_start_ms"),
                    clip_end_ms=clip_options.get("clip_end_ms"),
                    segment_boundaries_ms=segment_boundaries_ms,
                )
            except EditingError as error:
                if error.code == "source_caption_not_usable":
                    return None
                _domain_failure(error, "download_caption_unavailable")
            if not isinstance(timeline, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            newly_imported = True

        state = timeline.get("state")
        if state == "rejected":
            # Rejection is an explicit signal to use the already-authorized AI
            # transcription fallback on the next advancement.
            return None
        revision_id = _record_id(timeline)
        if timeline.get("project_id") != project_id:
            raise WorkflowError("workflow_domain_data_invalid")
        if state == "review":
            if newly_imported or not explicit or not authorization[0]:
                return AiSnapshot(
                    "review", code="ai_source_caption_review_required"
                )
            review_version = timeline.get("review_version")
            if review_version != 0 or isinstance(review_version, bool):
                raise WorkflowError("workflow_domain_data_invalid")
            timeline = self.editing_manager.invoke(
                "review_timeline", revision_id, "approved", review_version
            )
            authorization[0] = False
            state = timeline.get("state")
        if state != "approved" or timeline.get("id") != revision_id:
            raise WorkflowError("workflow_domain_data_invalid")
        return timeline

    def _existing_source_caption(
        self, project_id: str
    ) -> Mapping[str, Any] | None:
        try:
            timelines = self.editing_manager.invoke(
                "timelines", project_id=project_id
            )
        except EditingError as error:
            _domain_failure(error, "ai_pipeline_failed")
        if not isinstance(timelines, list):
            raise WorkflowError("workflow_domain_data_invalid")
        matches: list[Mapping[str, Any]] = []
        for timeline in timelines:
            if not isinstance(timeline, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            if timeline.get("provider") != "download":
                continue
            model = timeline.get("model")
            if (
                timeline.get("project_id") != project_id
                or timeline.get("kind") != "transcription"
                or timeline.get("parent_id") is not None
                or not isinstance(model, str)
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            match = _SOURCE_CAPTION_MODEL.fullmatch(model)
            if match is None:
                raise WorkflowError("workflow_domain_data_invalid")
            try:
                if str(UUID(match.group(1))) != match.group(1):
                    raise ValueError
            except (AttributeError, ValueError):
                raise WorkflowError("workflow_domain_data_invalid") from None
            matches.append(timeline)
        if len(matches) > 1:
            raise WorkflowError("workflow_domain_data_invalid")
        return matches[0] if matches else None

    def _transcription_fallback_started(self, project_id: str) -> bool:
        """Keep the source-caption decision stable after AI fallback begins."""

        try:
            tasks = self.editing_manager.invoke("ai_tasks", project_id=project_id)
        except EditingError as error:
            _domain_failure(error, "ai_pipeline_failed")
        if not isinstance(tasks, list):
            raise WorkflowError("workflow_domain_data_invalid")
        seen: set[str] = set()
        fallback_started = False
        for task in tasks:
            if not isinstance(task, Mapping) or task.get("project_id") != project_id:
                raise WorkflowError("workflow_domain_data_invalid")
            task_id = _record_id(task)
            if task_id in seen:
                raise WorkflowError("workflow_domain_data_invalid")
            seen.add(task_id)
            operation = task.get("operation")
            if operation not in {"transcribe", "translate"}:
                raise WorkflowError("workflow_domain_data_invalid")
            fallback_started = fallback_started or operation == "transcribe"
        return fallback_started

    def _select_source_caption(
        self, asset_id: str, source_language: str, target_language: str
    ) -> dict[str, Any] | None:
        """Select a single unambiguous SRT/VTT artifact from this workflow."""

        asset_id = _download_identifier(asset_id)
        try:
            artifacts = self.batch_service.list_ready_captions_for_asset(asset_id)
        except Exception:
            raise WorkflowError("download_caption_unavailable") from None
        if not isinstance(artifacts, list):
            raise WorkflowError("download_caption_unavailable")

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise WorkflowError("download_caption_unavailable")
            if (
                artifact.get("kind") != "caption"
                or artifact.get("asset_id") != asset_id
                or artifact.get("origin") != "platform"
            ):
                raise WorkflowError("download_caption_unavailable")
            artifact_id = _download_identifier(artifact.get("artifact_id"))
            if artifact_id in seen:
                raise WorkflowError("download_caption_unavailable")
            seen.add(artifact_id)
            mime_type = artifact.get("mime_type")
            language = artifact.get("language")
            digest = artifact.get("sha256")
            artifact_path = artifact.get("artifact_path")
            if (
                not isinstance(mime_type, str)
                or not isinstance(language, str)
                or _CAPTION_LANGUAGE.fullmatch(language) is None
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                or not isinstance(artifact_path, str)
                or not artifact_path
            ):
                raise WorkflowError("download_caption_unavailable")
            if mime_type not in _SOURCE_CAPTION_MIME_TYPES:
                continue
            candidates.append(
                {
                    "artifact_id": artifact_id,
                    "asset_id": asset_id,
                    "artifact_path": artifact_path,
                    "mime_type": mime_type,
                    "language": language,
                    "sha256": digest,
                    "origin": artifact.get("origin"),
                    "tool_name": artifact.get("tool_name"),
                    "tool_version": artifact.get("tool_version"),
                }
            )
        if not candidates:
            return None

        desired = source_language.replace("_", "-").casefold()
        if desired == "auto":
            target = target_language.replace("_", "-").casefold()
            if target in {"zh", "zh-cn", "zh-hans", "zh-sg"}:
                desired = "en"
            elif target == "en" or target.startswith("en-"):
                desired = "zh-cn"
            else:
                eligible = [
                    candidate
                    for candidate in candidates
                    if self._caption_language_rank(target, candidate["language"])
                    is None
                ]
                return eligible[0] if len(eligible) == 1 else None
        ranked: list[tuple[int, dict[str, Any]]] = []
        for candidate in candidates:
            rank = self._caption_language_rank(desired, candidate["language"])
            if rank is not None:
                ranked.append((rank, candidate))
        if not ranked:
            return None
        best = min(rank for rank, _candidate in ranked)
        winners = [candidate for rank, candidate in ranked if rank == best]
        return winners[0] if len(winners) == 1 else None

    @staticmethod
    def _caption_language_rank(desired: str, available: str) -> int | None:
        normalized = available.replace("_", "-").casefold()
        if normalized == desired:
            return 0
        equivalents = {
            "zh-cn": ("zh-hans", "zh"),
            "zh-sg": ("zh-hans", "zh"),
            "zh-hans": ("zh-cn", "zh-sg", "zh"),
            "zh-tw": ("zh-hant", "zh"),
            "zh-hk": ("zh-hant", "zh"),
            "zh-mo": ("zh-hant", "zh"),
            "zh-hant": ("zh-tw", "zh-hk", "zh-mo", "zh"),
        }.get(desired)
        if equivalents is not None and normalized in equivalents:
            return equivalents.index(normalized) + 1
        if "-" not in desired and normalized.startswith(f"{desired}-"):
            return 1
        return None

    def _advance_ai_task(
        self,
        task: Mapping[str, Any],
        *,
        expected_project_id: str | None = None,
        authorization: list[bool],
        explicit: bool,
        confirmation_code: str,
        review_code: str,
    ) -> Mapping[str, Any] | AiSnapshot:
        if not isinstance(task, Mapping):
            raise WorkflowError("workflow_domain_data_invalid")
        task = self._latest_ai_task(
            task, expected_project_id=expected_project_id
        )
        task_id = _record_id(task)
        state = task.get("state")
        if self._ai_task_retry_blocked(task):
            return AiSnapshot("attention", code="ai_remote_retry_blocked")
        if state == "review":
            reason = task.get("code")
            # A retry remains an explicit decision even when its last queued
            # attempt was revoked by restart recovery.  Check lineage before
            # the generic restart code so an automatic workflow cannot mistake
            # a recovered retry for its originally pre-authorized task.
            if task.get("retry_of") is not None and not explicit:
                return AiSnapshot("review", code="ai_retry_confirmation_required")
            if (
                reason == "restart_confirmation_required"
                and not explicit
                and not authorization[0]
            ):
                return AiSnapshot("review", code="ai_restart_confirmation_required")
            if not explicit and reason not in {
                "explicit_confirmation_required",
                "restart_confirmation_required",
            }:
                return AiSnapshot(
                    "review",
                    code=(
                        reason
                        if isinstance(reason, str) and _SAFE_CODE.fullmatch(reason)
                        else "ai_review_required"
                    ),
                )
            if not authorization[0]:
                return AiSnapshot("review", code=confirmation_code)
            request_sha256 = task.get("request_sha256")
            authorization_sha256 = task.get("authorization_sha256")
            task_authorization = task.get("authorization")
            if (
                not isinstance(request_sha256, str)
                or not isinstance(authorization_sha256, str)
                or not isinstance(task_authorization, Mapping)
                or task_authorization.get("execution") not in {"local", "remote"}
            ):
                return AiSnapshot("attention", code="ai_authorization_required")
            task = self.editing_manager.confirm_ai_task(
                task_id,
                expected_request_sha256=request_sha256,
                expected_authorization_sha256=authorization_sha256,
                ai_data_egress_accepted=(
                    task_authorization["execution"] == "remote"
                ),
            )
            authorization[0] = False
            state = task.get("state")
        if state in {"queued", "running", "canceling"}:
            return AiSnapshot("waiting")
        if state in {"failed", "canceled"}:
            code = task.get("code")
            return AiSnapshot(
                "failed",
                code=(
                    code
                    if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
                    else "ai_task_failed"
                ),
            )
        if state != "succeeded":
            return AiSnapshot("attention", code="ai_task_state_unknown")
        revision_id = _hex_identifier(task.get("result_revision_id"))
        timeline = self.editing_manager.invoke("timeline", revision_id)
        if not isinstance(timeline, Mapping):
            raise WorkflowError("workflow_domain_data_invalid")
        timeline_state = timeline.get("state")
        if timeline_state == "review":
            if not authorization[0]:
                return AiSnapshot("review", code=review_code)
            review_version = timeline.get("review_version")
            if (
                isinstance(review_version, bool)
                or not isinstance(review_version, int)
                or review_version != 0
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            timeline = self.editing_manager.invoke(
                "review_timeline", revision_id, "approved", review_version
            )
            authorization[0] = False
            timeline_state = timeline.get("state")
        if timeline_state == "rejected":
            return AiSnapshot("failed", code="ai_timeline_rejected")
        if timeline_state != "approved" or timeline.get("id") != revision_id:
            return AiSnapshot("attention", code="ai_timeline_state_unknown")
        return timeline

    def _latest_ai_task(
        self,
        task: Mapping[str, Any],
        *,
        expected_project_id: str | None = None,
    ) -> Mapping[str, Any]:
        error_code = "ai_task_set_invalid"
        try:
            task_id = _record_id(task)
            project_id = _hex_identifier(task.get("project_id"))
            if expected_project_id is not None:
                expected_project_id = _hex_identifier(expected_project_id)
        except WorkflowError:
            raise WorkflowError(error_code) from None
        if expected_project_id is not None and project_id != expected_project_id:
            raise WorkflowError(error_code)
        rows = self.editing_manager.invoke("ai_tasks", project_id=project_id)
        if not isinstance(rows, list):
            raise WorkflowError(error_code)

        records: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping) or row.get("project_id") != project_id:
                raise WorkflowError(error_code)
            try:
                current_id = _record_id(row)
            except WorkflowError:
                raise WorkflowError(error_code) from None
            if current_id in records:
                raise WorkflowError(error_code)
            records[current_id] = row

        start = records.get(task_id)
        if start is None or any(
            start.get(field) != task.get(field)
            for field in (
                "project_id",
                "operation",
                "source_revision_id",
                "request_sha256",
            )
        ):
            raise WorkflowError(error_code)

        successors: dict[str, str] = {}
        for current_id, row in records.items():
            retry_of = row.get("retry_of")
            if retry_of is None:
                continue
            try:
                parent_id = _hex_identifier(retry_of)
            except WorkflowError:
                raise WorkflowError(error_code) from None
            if (
                parent_id not in records
                or parent_id == current_id
                or parent_id in successors
            ):
                raise WorkflowError(error_code)
            parent = records[parent_id]
            if any(
                row.get(field) != parent.get(field)
                for field in (
                    "operation",
                    "source_revision_id",
                    "request_sha256",
                )
            ):
                raise WorkflowError(error_code)
            successors[parent_id] = current_id

        _validate_retry_successors(
            tuple(records), successors, error_code=error_code
        )
        current_id = task_id
        while current_id in successors:
            current_id = successors[current_id]
        return records[current_id]

    def _ai_task_retry_blocked(self, task: Mapping[str, Any]) -> bool:
        """Read the full project-level recursive block set, independent of paging."""

        task_id = _record_id(task)
        project_id = _hex_identifier(task.get("project_id"))
        try:
            ledger = self.editing_manager.invoke(
                "ai_invocations", project_id=project_id, offset=0, limit=1
            )
        except EditingError as error:
            _domain_failure(error, "ai_pipeline_failed")
        if not isinstance(ledger, Mapping):
            raise WorkflowError("workflow_domain_data_invalid")
        summary = ledger.get("summary")
        if not isinstance(summary, Mapping):
            raise WorkflowError("workflow_domain_data_invalid")
        blocked = summary.get("retry_blocked_ai_task_ids")
        if not isinstance(blocked, list) or any(
            not isinstance(value, str) or _HEX_IDENTIFIER.fullmatch(value) is None
            for value in blocked
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        return task_id in blocked

    def retry_ai(
        self,
        workflow_id: str,
        project_id: str,
        recipe: Mapping[str, Any],
        ai: Mapping[str, Any],
    ) -> None:
        """Create one reviewable successor for the failed workflow AI step."""

        workflow_id = _hex_identifier(workflow_id)
        project_id = _hex_identifier(project_id)
        try:
            normalized = recipe_from_mapping(recipe)
        except EditingError as error:
            _domain_failure(error, "invalid_recipe")
        if (
            not normalized.translation.enabled
            or not isinstance(ai, Mapping)
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        ai_keys, transcription_mode = _ai_shape(ai)
        if ai_keys in {_LEGACY_AI_KEYS, _DIGEST_AI_KEYS}:
            raise WorkflowError("ai_authorization_required")
        if ai_keys != _BOUND_AI_KEYS:
            raise WorkflowError("workflow_domain_data_invalid")
        provider = ai.get("transcription_provider")
        model = ai.get("transcription_model")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise WorkflowError("workflow_domain_data_invalid")
        transcription_authorization_sha256 = ai.get(
            "transcription_authorization_sha256"
        )
        translation_authorization_sha256 = ai.get(
            "translation_authorization_sha256"
        )
        if not isinstance(transcription_authorization_sha256, str) or not isinstance(
            translation_authorization_sha256, str
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        try:
            clip_options = self._transcription_clip_options(normalized)
            source: Mapping[str, Any] | None = None
            if transcription_mode == "prefer_source_caption":
                source = self._existing_source_caption(project_id)
                if source is not None and source.get("state") == "rejected":
                    source = None
                elif source is not None and source.get("state") != "approved":
                    raise WorkflowError("workflow_ai_retry_not_available")
            if source is None:
                transcription = self.editing_manager.create_ai_task(
                    project_id,
                    "transcribe",
                    provider,
                    model,
                    {
                        "language": (
                            None
                            if normalized.translation.source_language.casefold() == "auto"
                            else normalized.translation.source_language
                        ),
                        "word_timestamps": True,
                        "vad": True,
                        **clip_options,
                    },
                    f"wf-{workflow_id}-transcribe",
                    expected_authorization_sha256=transcription_authorization_sha256,
                )
                current = self._latest_ai_task(
                    transcription, expected_project_id=project_id
                )
                if self._ai_task_retry_blocked(current):
                    raise WorkflowError("ai_remote_retry_blocked")
                if current.get("state") in {"review", "queued", "running", "canceling"}:
                    return
                if current.get("state") in {"failed", "canceled"}:
                    self.editing_manager.invoke(
                        "retry_ai_task",
                        _record_id(current),
                        f"wf-{workflow_id}-retry-{_record_id(current)}",
                    )
                    return
                if current.get("state") != "succeeded":
                    raise WorkflowError("workflow_ai_retry_not_available")
                source_revision_id = _hex_identifier(
                    current.get("result_revision_id")
                )
                source = self.editing_manager.invoke(
                    "timeline", source_revision_id
                )
                if isinstance(source, Mapping) and source.get("state") == "rejected":
                    self.editing_manager.invoke(
                        "retry_ai_task",
                        _record_id(current),
                        f"wf-{workflow_id}-retry-{_record_id(current)}",
                    )
                    return
                if not isinstance(source, Mapping) or source.get("state") != "approved":
                    raise WorkflowError("workflow_ai_retry_not_available")
            source_revision_id = _record_id(source)
            source_language = source.get("language")
            if not isinstance(source_language, str):
                raise WorkflowError("workflow_domain_data_invalid")
            translation = self.editing_manager.create_ai_task(
                project_id,
                "translate",
                normalized.translation.provider,
                normalized.translation.model,
                {
                    "source_language": source_language,
                    "target_language": normalized.translation.target_language,
                    "glossary": [],
                },
                f"wf-{workflow_id}-translate",
                source_revision_id=source_revision_id,
                expected_authorization_sha256=translation_authorization_sha256,
            )
            current = self._latest_ai_task(
                translation, expected_project_id=project_id
            )
            if self._ai_task_retry_blocked(current):
                raise WorkflowError("ai_remote_retry_blocked")
            if current.get("state") in {"review", "queued", "running", "canceling"}:
                return
            if current.get("state") == "succeeded":
                result_revision_id = _hex_identifier(current.get("result_revision_id"))
                result = self.editing_manager.invoke("timeline", result_revision_id)
                if not isinstance(result, Mapping) or result.get("state") != "rejected":
                    raise WorkflowError("workflow_ai_retry_not_available")
            elif current.get("state") not in {"failed", "canceled"}:
                raise WorkflowError("workflow_ai_retry_not_available")
            self.editing_manager.invoke(
                "retry_ai_task",
                _record_id(current),
                f"wf-{workflow_id}-retry-{_record_id(current)}",
            )
        except EditingError as error:
            _domain_failure(error, "ai_pipeline_failed")

    def _workflow_edit_project(
        self,
        project_id: str,
        *,
        expected_name: str,
        expected_source_asset_id: str,
    ) -> Mapping[str, Any]:
        project_id = _hex_identifier(project_id)
        expected_source_asset_id = _download_identifier(expected_source_asset_id)
        if not isinstance(expected_name, str) or not expected_name:
            raise WorkflowError("edit_project_mismatch")
        try:
            project = self.editing_manager.invoke("project", project_id)
        except EditingError as error:
            _domain_failure(error, "editing_failed")
        if (
            not isinstance(project, Mapping)
            or project.get("id") != project_id
            or project.get("name") != expected_name
            or project.get("source_asset_id") != expected_source_asset_id
        ):
            raise WorkflowError("edit_project_mismatch")
        return project

    def cancel_ai(
        self,
        project_id: str,
        *,
        expected_name: str,
        expected_source_asset_id: str,
    ) -> CancellationSnapshot:
        """Atomically stop every AI leaf owned by a workflow editing project."""

        project_id = _hex_identifier(project_id)
        try:
            self._workflow_edit_project(
                project_id,
                expected_name=expected_name,
                expected_source_asset_id=expected_source_asset_id,
            )
        except WorkflowError as error:
            return CancellationSnapshot("attention", code=error.code)
        try:
            canceled_tasks = self.editing_manager.cancel_ai_project_tasks(project_id)
        except EditingError as error:
            if error.code == "ai_task_retry_lineage_changed":
                return CancellationSnapshot(
                    "waiting", code="ai_task_retry_lineage_changed"
                )
            if error.code in {"ai_task_set_invalid", "editing_data_invalid"}:
                return CancellationSnapshot("attention", code="ai_task_set_invalid")
            if error.code in _AI_UNCERTAIN_CODES:
                return CancellationSnapshot(
                    "attention", code=error.code
                )
            _domain_failure(error, "ai_pipeline_failed")
        return self._ai_cancellation_snapshot(project_id, canceled_tasks)

    @staticmethod
    def _ai_cancellation_snapshot(
        project_id: str, canceled_tasks: object
    ) -> CancellationSnapshot:
        if not isinstance(canceled_tasks, list):
            return CancellationSnapshot("attention", code="ai_task_set_invalid")
        result_ids: set[str] = set()
        for canceled in canceled_tasks:
            if not isinstance(canceled, Mapping) or canceled.get("project_id") != project_id:
                return CancellationSnapshot("attention", code="ai_task_set_invalid")
            try:
                task_id = _record_id(canceled)
            except WorkflowError:
                return CancellationSnapshot("attention", code="ai_task_set_invalid")
            if task_id in result_ids:
                return CancellationSnapshot("attention", code="ai_task_set_invalid")
            result_ids.add(task_id)
        allowed_states = {
            "review", "queued", "running", "canceling",
            "succeeded", "failed", "canceled",
        }
        uncertain_codes = {
            task.get("code")
            for task in canceled_tasks
            if task.get("code") in _AI_UNCERTAIN_CODES
        }
        states = {task.get("state") for task in canceled_tasks}
        if any(state not in allowed_states for state in states):
            return CancellationSnapshot("attention", code="ai_task_state_unknown")
        if uncertain_codes:
            code = (
                next(iter(uncertain_codes))
                if len(uncertain_codes) == 1
                else "ai_remote_result_unknown"
            )
            return CancellationSnapshot("attention", code=code)
        if states & {"running", "canceling"}:
            return CancellationSnapshot("waiting", code="ai_cancellation_pending")
        if not states or states <= {"succeeded", "failed", "canceled"}:
            return CancellationSnapshot("stopped")
        return CancellationSnapshot("attention", code="ai_task_state_unknown")

    def cancel_edit_for_workflow(
        self,
        workflow_id: str,
        *,
        expected_project_id: str | None,
        expected_name: str,
        expected_source_asset_id: str,
        expected_recipe: Mapping[str, Any],
    ) -> CancellationSnapshot:
        """Atomically discover and stop workflow editing artifacts."""

        workflow_id = _hex_identifier(workflow_id)
        try:
            expected_source_asset_id = _download_identifier(
                expected_source_asset_id
            )
            if expected_project_id is not None:
                expected_project_id = _hex_identifier(expected_project_id)
        except WorkflowError as error:
            return CancellationSnapshot("attention", code=error.code)
        if expected_name != f"Open-Flame workflow {workflow_id}":
            return CancellationSnapshot("attention", code="edit_project_mismatch")
        try:
            normalized_recipe = recipe_from_mapping(expected_recipe).to_dict()
        except EditingError:
            return CancellationSnapshot("attention", code="edit_request_invalid")
        try:
            artifacts = self.editing_manager.cancel_workflow_request_artifacts(
                f"wf-{workflow_id}-edit-project",
                f"wf-{workflow_id}-edit-plan",
                expected_project_id=expected_project_id,
                expected_name=expected_name,
                expected_source_asset_id=expected_source_asset_id,
                expected_recipe=normalized_recipe,
            )
        except EditingError as error:
            if error.code in {
                "editing_request_invalid",
                "invalid_idempotency_key",
                "invalid_metadata",
                "project_not_found",
                "plan_not_found",
            }:
                return CancellationSnapshot("attention", code="edit_request_invalid")
            if error.code in {"edit_project_mismatch", "invalid_source_asset_id"}:
                return CancellationSnapshot("attention", code="edit_project_mismatch")
            if error.code in {"edit_plan_set_invalid", "editing_data_invalid"}:
                return CancellationSnapshot("attention", code="edit_plan_set_invalid")
            if error.code == "edit_plan_mismatch":
                return CancellationSnapshot("attention", code="edit_plan_mismatch")
            if error.code == "render_retry_lineage_changed":
                return CancellationSnapshot(
                    "waiting", code="render_retry_lineage_changed"
                )
            if error.code in _AI_UNCERTAIN_CODES:
                return CancellationSnapshot("attention", code=error.code)
            _domain_failure(error, "editing_failed")
        if not isinstance(artifacts, Mapping) or set(artifacts) != {
            "kind", "project", "plan", "ai_tasks",
        }:
            return CancellationSnapshot("attention", code="edit_request_invalid")
        kind = artifacts.get("kind")
        project = artifacts.get("project")
        plan = artifacts.get("plan")
        tasks = artifacts.get("ai_tasks")
        if kind == "none":
            if project is not None or plan is not None or tasks != []:
                return CancellationSnapshot("attention", code="edit_request_invalid")
            return CancellationSnapshot("stopped")
        if not isinstance(project, Mapping):
            return CancellationSnapshot("attention", code="edit_request_invalid")
        try:
            project_id = _record_id(project)
        except WorkflowError:
            return CancellationSnapshot("attention", code="edit_request_invalid")
        if (
            (expected_project_id is not None and project_id != expected_project_id)
            or project.get("name") != expected_name
            or project.get("source_asset_id") != expected_source_asset_id
        ):
            return CancellationSnapshot("attention", code="edit_project_mismatch")
        if kind == "ai":
            if plan is not None:
                return CancellationSnapshot("attention", code="edit_request_invalid")
            return self._ai_cancellation_snapshot(project_id, tasks)
        if (
            kind != "plan"
            or tasks != []
            or not isinstance(plan, Mapping)
            or plan.get("project_id") != project_id
        ):
            return CancellationSnapshot("attention", code="edit_request_invalid")
        try:
            plan_id = _record_id(plan)
        except WorkflowError:
            return CancellationSnapshot("attention", code="edit_request_invalid")
        if plan.get("code") in _AI_UNCERTAIN_CODES:
            return CancellationSnapshot("attention", code=plan["code"])
        state = plan.get("state")
        if state in {"running", "canceling"}:
            return CancellationSnapshot("waiting", code="edit_cancellation_pending")
        if state in {"ready", "failed", "canceled"}:
            return CancellationSnapshot("stopped")
        return CancellationSnapshot("attention", code="edit_state_unknown")

    def inspect_edit(self, plan_id: str) -> EditSnapshot:
        plan_id = _hex_identifier(plan_id)
        try:
            plan = self.editing_manager.invoke("plan", plan_id)
        except EditingError as error:
            _domain_failure(error, "editing_failed")
        if not isinstance(plan, Mapping):
            return EditSnapshot("attention", code="edit_state_invalid")
        state = plan.get("state")
        if state == "review":
            # Retry successors always keep their separate confirmation gate.
            # The editing domain uses the same restart recovery code for every
            # queued row, so lineage must take precedence here.
            code = (
                "render_retry_confirmation_required"
                if plan.get("retry_of") is not None
                else plan.get("code")
            )
            if code == "":
                code = "edit_review_confirmation_required"
            elif not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
                return EditSnapshot("attention", code="edit_state_invalid")
            return EditSnapshot(
                "waiting",
                code=code,
                needs_confirmation=True,
            )
        if state in _EDIT_WAITING_STATES:
            return EditSnapshot("waiting", needs_confirmation=False)
        if state in {"failed", "canceled"}:
            code = plan.get("code")
            if not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
                code = "edit_failed"
            return EditSnapshot("failed", code=code)
        if state != "ready":
            return EditSnapshot("attention", code="edit_state_unknown")

        try:
            recipe = recipe_from_mapping(plan.get("recipe"))
        except EditingError:
            return EditSnapshot("attention", code="edit_output_shape_invalid")
        expected_video_count = len(recipe.segments) or 1
        if not 1 <= expected_video_count <= MAX_WORKFLOW_SEGMENTS:
            return EditSnapshot("attention", code="edit_output_shape_invalid")

        assets = plan.get("assets")
        if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
            return EditSnapshot("attention", code="edit_output_shape_invalid")
        segments: dict[int, str] = {}
        dubbed_videos: dict[int, str] = {}
        covers: dict[int, str] = {}
        captions: dict[int, str] = {}
        audio: dict[int, str] = {}
        assets_by_kind = {
            "segment": segments,
            "dubbed_video": dubbed_videos,
            "cover": covers,
            "caption": captions,
            "audio": audio,
        }
        all_ids: set[str] = set()
        for asset in assets:
            if not isinstance(asset, Mapping):
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            kind = asset.get("kind")
            if kind not in assets_by_kind or asset.get("plan_id") != plan_id:
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            try:
                asset_id = _hex_identifier(asset.get("id"))
            except WorkflowError:
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            ordinal = asset.get("ordinal")
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal < 1
                or asset_id in all_ids
            ):
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            all_ids.add(asset_id)
            target = assets_by_kind[kind]
            if ordinal in target:
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            target[ordinal] = asset_id

        expected_ordinals = set(range(1, expected_video_count + 1))
        if recipe.dubbing.enabled:
            if set(dubbed_videos) != expected_ordinals:
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            if recipe.segments and set(segments) != expected_ordinals:
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            if not recipe.segments and segments:
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            selected = dubbed_videos
        else:
            if dubbed_videos or set(segments) != expected_ordinals:
                return EditSnapshot("attention", code="edit_output_shape_invalid")
            selected = segments

        expected_cover_ordinals = {1} if recipe.cover is not None else set()
        if set(covers) != expected_cover_ordinals:
            return EditSnapshot("attention", code="edit_output_shape_invalid")
        expected_caption_ordinals = (
            expected_ordinals if recipe.translation.enabled else set()
        )
        if set(captions) != expected_caption_ordinals or audio:
            return EditSnapshot("attention", code="edit_output_shape_invalid")
        videos = tuple(
            selected[ordinal] for ordinal in range(1, expected_video_count + 1)
        )
        return EditSnapshot(
            "ready",
            output_id=videos[0] if len(videos) == 1 else None,
            cover_id=covers.get(1),
            output_ids=videos,
        )

    def confirm_edit(self, plan_id: str) -> None:
        plan_id = _hex_identifier(plan_id)
        try:
            plan = self.editing_manager.invoke("plan", plan_id)
            if not isinstance(plan, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            recipe_sha256 = plan.get("recipe_sha256")
            if not isinstance(recipe_sha256, str):
                raise WorkflowError("workflow_domain_data_invalid")
            recipe_value = plan.get("recipe")
            if not isinstance(recipe_value, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            parsed_recipe = recipe_from_mapping(recipe_value)
            authorization = parsed_recipe.dubbing.authorization
            if parsed_recipe.dubbing.enabled and authorization is None:
                raise WorkflowError("ai_authorization_required")
            self.editing_manager.confirm_plan(
                plan_id,
                expected_recipe_sha256=recipe_sha256,
                expected_authorization_sha256=(
                    None if authorization is None else authorization.sha256
                ),
                ai_data_egress_accepted=(
                    authorization is not None and authorization.execution == "remote"
                ),
            )
        except EditingError as error:
            _domain_failure(error, "editing_failed")

    def _latest_edit_plan(
        self, plan_id: str, project_id: str
    ) -> Mapping[str, Any]:
        try:
            plans = self.editing_manager.invoke("plans", project_id=project_id)
        except EditingError as error:
            _domain_failure(error, "editing_failed")
        if not isinstance(plans, list):
            raise WorkflowError("edit_plan_set_invalid")
        records: dict[str, Mapping[str, Any]] = {}
        successors: dict[str, str] = {}
        for plan in plans:
            if not isinstance(plan, Mapping) or plan.get("project_id") != project_id:
                raise WorkflowError("edit_plan_set_invalid")
            try:
                current_id = _record_id(plan)
            except WorkflowError:
                raise WorkflowError("edit_plan_set_invalid") from None
            if current_id in records:
                raise WorkflowError("edit_plan_set_invalid")
            records[current_id] = plan
        if plan_id not in records:
            raise WorkflowError("edit_plan_mismatch")
        for current_id, plan in records.items():
            retry_of = plan.get("retry_of")
            if retry_of is None:
                continue
            try:
                parent_id = _hex_identifier(retry_of)
            except WorkflowError:
                raise WorkflowError("edit_plan_set_invalid") from None
            if (
                parent_id not in records
                or parent_id in successors
                or parent_id == current_id
            ):
                raise WorkflowError("edit_plan_set_invalid")
            parent = records[parent_id]
            if any(
                plan.get(field) != parent.get(field)
                for field in (
                    "draft_version",
                    "recipe_sha256",
                    "timeline_revision_id",
                )
            ):
                raise WorkflowError("edit_plan_set_invalid")
            successors[parent_id] = current_id
        _validate_retry_successors(
            tuple(records), successors, error_code="edit_plan_set_invalid"
        )
        current_id = plan_id
        while current_id in successors:
            current_id = successors[current_id]
        return records[current_id]

    def cancel_edit(
        self,
        plan_id: str,
        *,
        expected_project_id: str,
        expected_name: str,
        expected_source_asset_id: str,
    ) -> CancellationSnapshot:
        """Cancel one workflow-owned render plan without hiding uncertainty."""

        plan_id = _hex_identifier(plan_id)
        expected_project_id = _hex_identifier(expected_project_id)
        try:
            self._workflow_edit_project(
                expected_project_id,
                expected_name=expected_name,
                expected_source_asset_id=expected_source_asset_id,
            )
            plan = self._latest_edit_plan(plan_id, expected_project_id)
        except WorkflowError as error:
            return CancellationSnapshot("attention", code=error.code)
        leaf_id = _record_id(plan)
        if plan.get("code") in _AI_UNCERTAIN_CODES:
            return CancellationSnapshot("attention", code=plan["code"])
        try:
            plan = self.editing_manager.cancel(leaf_id)
        except EditingError as error:
            if error.code == "render_retry_lineage_changed":
                return CancellationSnapshot(
                    "waiting", code="render_retry_lineage_changed"
                )
            if error.code in _AI_UNCERTAIN_CODES:
                return CancellationSnapshot(
                    "attention", code=error.code
                )
            _domain_failure(error, "editing_failed")
        if (
            not isinstance(plan, Mapping)
            or plan.get("id") != leaf_id
            or plan.get("project_id") != expected_project_id
        ):
            return CancellationSnapshot("attention", code="edit_state_invalid")
        if plan.get("code") in _AI_UNCERTAIN_CODES:
            return CancellationSnapshot("attention", code=plan["code"])
        state = plan.get("state")
        if state in {"running", "canceling"}:
            return CancellationSnapshot("waiting", code="edit_cancellation_pending")
        if state in {"ready", "failed", "canceled"}:
            return CancellationSnapshot("stopped")
        return CancellationSnapshot("attention", code="edit_state_unknown")

    def retry_edit(self, workflow_id: str, plan_id: str) -> str:
        workflow_id = _hex_identifier(workflow_id)
        plan_id = _hex_identifier(plan_id)
        try:
            plan = self.editing_manager.invoke(
                "retry_plan",
                plan_id,
                f"wf-{workflow_id}-retry-plan-{plan_id}",
            )
        except EditingError as error:
            _domain_failure(error, "editing_failed")
        new_id = _record_id(plan)
        if plan.get("retry_of") != plan_id or plan.get("state") != "review":
            raise WorkflowError("workflow_domain_data_invalid")
        return new_id

    def select_upload_cover(
        self,
        workflow_id: str,
        output_id: str,
        cover_id: str,
        upload: Mapping[str, Any],
        *,
        segment_ordinal: int,
        download_asset_id: str,
    ) -> str:
        """Choose a deterministic cover without mutating the Upload domain."""

        selected = self._prepare_upload(
            workflow_id,
            output_id,
            cover_id,
            upload,
            segment_ordinal=segment_ordinal,
            download_asset_id=download_asset_id,
            expected_upload_cover_id=None,
            select_cover_only=True,
        )
        if selected.cover_id is None or selected.job_ids:
            raise WorkflowError("workflow_domain_data_invalid")
        return selected.cover_id

    def prepare_upload(
        self,
        workflow_id: str,
        output_id: str,
        cover_id: str | None,
        upload: Mapping[str, Any],
        *,
        segment_ordinal: int = 1,
        download_asset_id: str | None = None,
        expected_upload_cover_id: str | None = None,
    ) -> UploadPrepared:
        return self._prepare_upload(
            workflow_id,
            output_id,
            cover_id,
            upload,
            segment_ordinal=segment_ordinal,
            download_asset_id=download_asset_id,
            expected_upload_cover_id=expected_upload_cover_id,
            select_cover_only=False,
        )

    def _prepare_upload(
        self,
        workflow_id: str,
        output_id: str,
        cover_id: str | None,
        upload: Mapping[str, Any],
        *,
        segment_ordinal: int = 1,
        download_asset_id: str | None = None,
        expected_upload_cover_id: str | None = None,
        select_cover_only: bool,
    ) -> UploadPrepared:
        workflow_id = _hex_identifier(workflow_id)
        output_id = _hex_identifier(output_id)
        if (
            isinstance(segment_ordinal, bool)
            or not isinstance(segment_ordinal, int)
            or not 1 <= segment_ordinal <= MAX_WORKFLOW_SEGMENTS
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        if cover_id is not None:
            cover_id = _hex_identifier(cover_id)
        if not isinstance(upload, Mapping) or set(upload) not in {
            frozenset(_UPLOAD_KEYS),
            frozenset(_UPLOAD_KEYS | {_UPLOAD_PREFER_DOWNLOAD_COVER_KEY}),
        }:
            raise WorkflowError("workflow_domain_data_invalid")
        prefer_download_cover = upload.get(
            _UPLOAD_PREFER_DOWNLOAD_COVER_KEY, False
        )
        if (
            type(prefer_download_cover) is not bool
            or (
                _UPLOAD_PREFER_DOWNLOAD_COVER_KEY in upload
                and prefer_download_cover is not True
            )
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        if prefer_download_cover:
            if cover_id is None or download_asset_id is None:
                raise WorkflowError("workflow_domain_data_invalid")
            download_asset_id = _download_identifier(download_asset_id)
        elif download_asset_id is not None:
            raise WorkflowError("workflow_domain_data_invalid")
        if expected_upload_cover_id is not None:
            expected_upload_cover_id = _hex_identifier(expected_upload_cover_id)
        if (
            prefer_download_cover
            and not select_cover_only
            and expected_upload_cover_id is None
        ):
            raise WorkflowError("workflow_domain_data_invalid")

        try:
            output_path, output_sha256, output_name = (
                self.editing_manager.resolve_output(output_id)
            )
            output_path = Path(output_path)
            output_sha256 = _sha256(output_sha256)

            cover_identity: tuple[Path, str, str] | None = None
            if cover_id is not None:
                cover_path, cover_sha256, cover_name = (
                    self.editing_manager.resolve_cover(cover_id)
                )
                cover_identity = (
                    Path(cover_path),
                    _sha256(cover_sha256),
                    cover_name,
                )

            account_ids = upload.get("account_ids")
            if (
                not isinstance(account_ids, list)
                or not 1 <= len(account_ids) <= MAX_WORKFLOW_ACCOUNTS
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            normalized_account_ids = [_hex_identifier(item) for item in account_ids]
            if (
                normalized_account_ids != account_ids
                or len(set(normalized_account_ids)) != len(normalized_account_ids)
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            bindings = upload.get("account_bindings")
            if (
                not isinstance(bindings, Sequence)
                or isinstance(bindings, (str, bytes))
                or any(not isinstance(binding, Mapping) for binding in bindings)
            ):
                raise WorkflowError("workflow_domain_data_invalid")

            service = self.upload_manager.get()
            accounts = service.accounts()
            platforms = self._account_platforms(accounts, account_ids)
            # Reject malformed or ambiguously ordered account overrides before
            # importing the shared cover or any video source.
            self._upload_overrides(
                upload.get("target_overrides"),
                account_ids,
                platforms,
                None,
                None,
            )

            source_id = _managed_import_id(
                workflow_id, "edit_video", output_id, output_sha256
            )
            request_key = workflow_upload_request_key(workflow_id, segment_ordinal)
            edit_imported_cover_id = (
                _managed_import_id(
                    workflow_id,
                    "edit_cover",
                    cover_id,
                    cover_identity[1],
                )
                if cover_id is not None and cover_identity is not None
                else None
            )

            imported_cover_id = None
            selected_cover: tuple[Path, str, str, str] | None = None
            source_cover_identity: tuple[str, Path, str, str] | None = None
            cover_record: Mapping[str, Any] | None = None
            force_edit_fallback = False
            overrides: list[dict[str, Any]]
            persisted_jobs = (
                service.jobs_for_request(request_key)
                if prefer_download_cover
                else None
            )
            if prefer_download_cover and persisted_jobs == []:
                raise WorkflowError("workflow_domain_data_invalid")
            if prefer_download_cover and persisted_jobs is not None:
                imported_cover_id = self._workflow_request_cover_id(
                    persisted_jobs,
                    source_id=source_id,
                    account_ids=account_ids,
                    platforms=platforms,
                )
            elif prefer_download_cover and expected_upload_cover_id is not None:
                imported_cover_id = expected_upload_cover_id

            if imported_cover_id is not None:
                cover_record = self._frozen_workflow_cover_copy(
                    service,
                    workflow_id,
                    imported_cover_id,
                    edit_cover_id=edit_imported_cover_id,
                    edit_cover_sha256=(
                        cover_identity[1] if cover_identity is not None else None
                    ),
                    edit_cover_name=(
                        cover_identity[2] if cover_identity is not None else None
                    ),
                    require_media=persisted_jobs is None,
                    allow_missing=persisted_jobs is None,
                )
                if cover_record is None:
                    force_edit_fallback = (
                        imported_cover_id == edit_imported_cover_id
                    )
                    imported_cover_id = None
                else:
                    overrides = self._upload_overrides(
                        upload.get("target_overrides"),
                        account_ids,
                        platforms,
                        cover_record,
                        imported_cover_id,
                    )

            if imported_cover_id is None and not force_edit_fallback:
                source_cover_identity = (
                    self._download_cover_identity(download_asset_id)
                    if prefer_download_cover
                    else None
                )

            if imported_cover_id is None and source_cover_identity is not None:
                artifact_id, source_path, source_sha256, source_name = (
                    source_cover_identity
                )
                source_imported_cover_id = _managed_import_id(
                    workflow_id,
                    "download_cover",
                    artifact_id,
                    source_sha256,
                )
                source_cover_record = service.inspect_managed_cover_import(
                    source_path,
                    source_name,
                    expected_sha256=source_sha256,
                    managed_id=source_imported_cover_id,
                )
                if not isinstance(source_cover_record, Mapping):
                    raise WorkflowError("workflow_domain_data_invalid")
                if _record_id(source_cover_record) != source_imported_cover_id:
                    raise WorkflowError("workflow_domain_data_invalid")
                try:
                    overrides = self._upload_overrides(
                        upload.get("target_overrides"),
                        account_ids,
                        platforms,
                        source_cover_record,
                        source_imported_cover_id,
                    )
                except WorkflowError as error:
                    if error.code != "workflow_cover_incompatible":
                        raise
                else:
                    imported_cover_id = source_imported_cover_id
                    selected_cover = (
                        source_path,
                        source_name,
                        source_sha256,
                        source_imported_cover_id,
                    )

            if imported_cover_id is None and selected_cover is None:
                if cover_id is not None and cover_identity is not None:
                    cover_path, cover_sha256, cover_name = cover_identity
                    imported_cover_id = edit_imported_cover_id
                    if imported_cover_id is None:
                        raise WorkflowError("workflow_domain_data_invalid")
                    if prefer_download_cover:
                        cover_record = service.inspect_managed_cover_import(
                            cover_path,
                            cover_name,
                            expected_sha256=cover_sha256,
                            managed_id=imported_cover_id,
                        )
                        if not isinstance(cover_record, Mapping):
                            raise WorkflowError("workflow_domain_data_invalid")
                        if _record_id(cover_record) != imported_cover_id:
                            raise WorkflowError("workflow_domain_data_invalid")
                        selected_cover = (
                            cover_path,
                            cover_name,
                            cover_sha256,
                            imported_cover_id,
                        )
                    else:
                        imported = service.import_cover(
                            cover_path,
                            cover_name,
                            expected_sha256=cover_sha256,
                            managed_id=imported_cover_id,
                        )
                        if not isinstance(imported, Mapping):
                            raise WorkflowError("workflow_domain_data_invalid")
                        if _record_id(imported) != imported_cover_id:
                            raise WorkflowError("workflow_domain_data_invalid")
                        cover_record = imported
                else:
                    cover_record = None
                overrides = self._upload_overrides(
                    upload.get("target_overrides"),
                    account_ids,
                    platforms,
                    cover_record,
                    imported_cover_id,
                )

            if expected_upload_cover_id not in {None, imported_cover_id}:
                raise WorkflowError("workflow_domain_data_invalid")

            if select_cover_only:
                if not prefer_download_cover or imported_cover_id is None:
                    raise WorkflowError("workflow_domain_data_invalid")
                return UploadPrepared(
                    source_id=source_id,
                    cover_id=imported_cover_id,
                    job_ids=(),
                )

            if selected_cover is not None:
                selected_path, selected_name, selected_sha256, selected_id = (
                    selected_cover
                )
                imported = service.import_cover(
                    selected_path,
                    selected_name,
                    expected_sha256=selected_sha256,
                    managed_id=selected_id,
                )
                if (
                    not isinstance(imported, Mapping)
                    or _record_id(imported) != selected_id
                ):
                    raise WorkflowError("workflow_domain_data_invalid")

            source = service.import_source(
                output_path,
                output_name,
                expected_sha256=output_sha256,
                managed_id=source_id,
            )
            if _record_id(source) != source_id:
                raise WorkflowError("workflow_domain_data_invalid")
            request = workflow_upload_request(source_id, account_ids, upload, overrides)
            # Keep the execution boundary's tag list separate from frozen intent.
            request["tags"] = list(upload["tags"])
            jobs = service.create_jobs(
                **request,
                expected_account_bindings=[dict(binding) for binding in bindings],
                idempotency_key=request_key,
            )
        except EditingError as error:
            _domain_failure(error, "editing_failed")
        except UploadError as error:
            _domain_failure(error, "upload_failed")

        if not isinstance(jobs, list) or len(jobs) != len(account_ids):
            raise WorkflowError("workflow_domain_data_invalid")
        job_ids: list[str] = []
        for job, account_id in zip(jobs, account_ids, strict=True):
            if not isinstance(job, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            job_id = _record_id(job)
            try:
                bind_current_upload_target(
                    job,
                    {
                        "job_id": job_id,
                        "source_id": source_id,
                        "account_id": account_id,
                        "platform": platforms[account_id],
                    },
                )
            except UploadError:
                raise WorkflowError("workflow_domain_data_invalid") from None
            if job_id in job_ids:
                raise WorkflowError("workflow_domain_data_invalid")
            job_ids.append(job_id)
        return UploadPrepared(
            source_id=source_id,
            cover_id=imported_cover_id,
            job_ids=tuple(job_ids),
        )

    def _download_cover_identity(
        self, download_asset_id: str
    ) -> tuple[str, Path, str, str] | None:
        """Resolve one registered source cover without accepting arbitrary files."""

        resolver = self.download_cover_resolver
        if resolver is None:
            raise WorkflowError("workflow_source_cover_unavailable")
        try:
            resolved = resolver(download_asset_id)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("workflow_source_cover_unavailable") from None
        if resolved is None:
            return None
        if not isinstance(resolved, Mapping) or set(resolved) != {
            "artifact_id",
            "asset_id",
            "path",
            "sha256",
            "name",
        }:
            raise WorkflowError("workflow_source_cover_unavailable")
        try:
            artifact_id = _download_identifier(resolved.get("artifact_id"))
            asset_id = _download_identifier(resolved.get("asset_id"))
            expected_sha256 = _sha256(resolved.get("sha256"))
        except WorkflowError:
            raise WorkflowError("workflow_source_cover_unavailable") from None
        path = resolved.get("path")
        name = resolved.get("name")
        suffix = Path(name).suffix.lower() if isinstance(name, str) else ""
        if (
            asset_id != download_asset_id
            or not isinstance(path, Path)
            or not isinstance(name, str)
            or suffix not in {".jpg", ".jpeg", ".png", ".webp"}
            or name != f"download-cover-{artifact_id}{suffix}"
        ):
            raise WorkflowError("workflow_source_cover_unavailable")
        return artifact_id, path, expected_sha256, name

    @staticmethod
    def _workflow_request_cover_id(
        jobs: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        account_ids: Sequence[str],
        platforms: Mapping[str, str],
    ) -> str:
        """Recover one frozen cover from an immutable Upload fan-out."""

        if (
            not isinstance(jobs, Sequence)
            or isinstance(jobs, (str, bytes))
            or len(jobs) != len(account_ids)
        ):
            raise WorkflowError("upload_request_mismatch")
        observed_cover_ids: set[str] = set()
        for job, account_id in zip(jobs, account_ids, strict=True):
            if not isinstance(job, Mapping):
                raise WorkflowError("upload_request_mismatch")
            try:
                job_id = _record_id(job)
                bind_current_upload_target(
                    job,
                    {
                        "job_id": job_id,
                        "source_id": source_id,
                        "account_id": account_id,
                        "platform": platforms[account_id],
                    },
                )
                cover_ids = [
                    _hex_identifier(job.get(key))
                    for key in _COVER_KEYS
                    if job.get(key) is not None
                ]
            except (KeyError, UploadError, WorkflowError):
                raise WorkflowError("upload_request_mismatch") from None
            if len(cover_ids) != 1:
                raise WorkflowError("upload_request_mismatch")
            observed_cover_ids.add(cover_ids[0])
        if len(observed_cover_ids) != 1:
            raise WorkflowError("upload_request_mismatch")
        return observed_cover_ids.pop()

    @staticmethod
    def _frozen_workflow_cover_copy(
        service: Any,
        workflow_id: str,
        cover_id: str,
        *,
        edit_cover_id: str | None,
        edit_cover_sha256: str | None,
        edit_cover_name: str | None,
        require_media: bool = True,
        allow_missing: bool = False,
    ) -> Mapping[str, Any] | None:
        """Validate a frozen Upload cover against its deterministic owner."""

        canonical_cover_id = _hex_identifier(cover_id)
        if canonical_cover_id != edit_cover_id:
            return LocalWorkflowAdapter._workflow_source_cover_copy(
                service,
                workflow_id,
                canonical_cover_id,
                require_media=require_media,
                allow_missing=allow_missing,
            )
        if edit_cover_sha256 is None or edit_cover_name is None:
            raise WorkflowError("upload_request_mismatch")
        try:
            cover = service.cover(canonical_cover_id)
            if require_media:
                service.cover_content(canonical_cover_id)
        except UploadError as error:
            if allow_missing and error.code == "cover_not_found":
                return None
            raise WorkflowError("upload_request_mismatch") from None
        if (
            not isinstance(cover, Mapping)
            or cover.get("id") != canonical_cover_id
            or cover.get("name") != edit_cover_name
            or cover.get("sha256") != edit_cover_sha256
            or (
                require_media
                and cover.get("media_present") is not True
            )
        ):
            raise WorkflowError("upload_request_mismatch")
        return cover

    @staticmethod
    def _workflow_source_cover_copy(
        service: Any,
        workflow_id: str,
        cover_id: str | None,
        *,
        require_media: bool = True,
        allow_missing: bool = False,
    ) -> Mapping[str, Any] | None:
        """Validate the frozen Upload copy without consulting mutable Download rows."""

        try:
            canonical_cover_id = _hex_identifier(cover_id)
            cover = service.cover(canonical_cover_id)
            if require_media:
                service.cover_content(canonical_cover_id)
        except UploadError as error:
            if allow_missing and error.code == "cover_not_found":
                return None
            raise WorkflowError("upload_request_mismatch") from None
        except WorkflowError:
            raise WorkflowError("upload_request_mismatch") from None
        if (
            not isinstance(cover, Mapping)
            or cover.get("id") != canonical_cover_id
            or (
                require_media
                and cover.get("media_present") is not True
            )
        ):
            raise WorkflowError("upload_request_mismatch")
        name = cover.get("name")
        match = (
            re.fullmatch(
                r"download-cover-([0-9a-f-]{36})\.(?:jpg|jpeg|png|webp)",
                name,
            )
            if isinstance(name, str)
            else None
        )
        try:
            artifact_id = _download_identifier(
                None if match is None else match.group(1)
            )
            cover_sha256 = _sha256(cover.get("sha256"))
        except WorkflowError:
            raise WorkflowError("upload_request_mismatch") from None
        if (
            _managed_import_id(
                workflow_id,
                "download_cover",
                artifact_id,
                cover_sha256,
            )
            != canonical_cover_id
        ):
            raise WorkflowError("upload_request_mismatch")
        return cover

    def resolve_upload(
        self,
        upload: Mapping[str, Any],
        download: DownloadSnapshot,
    ) -> dict[str, Any]:
        """Freeze source-title mode into the legacy upload mapping shape."""

        if not isinstance(upload, Mapping) or set(upload) not in {
            frozenset(_UPLOAD_KEYS),
            frozenset(_UPLOAD_KEYS | {_UPLOAD_TITLE_MODE_KEY}),
            frozenset(
                _UPLOAD_KEYS | {_UPLOAD_PREFER_DOWNLOAD_COVER_KEY}
            ),
            frozenset(
                _UPLOAD_KEYS
                | {_UPLOAD_TITLE_MODE_KEY, _UPLOAD_PREFER_DOWNLOAD_COVER_KEY}
            ),
        }:
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        title_mode = upload.get(_UPLOAD_TITLE_MODE_KEY, "explicit")
        if not isinstance(title_mode, str):
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        if (
            _UPLOAD_PREFER_DOWNLOAD_COVER_KEY in upload
            and upload.get(_UPLOAD_PREFER_DOWNLOAD_COVER_KEY) is not True
        ):
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        if title_mode == "explicit":
            concrete = dict(upload)
            concrete.pop(_UPLOAD_TITLE_MODE_KEY, None)
            return concrete
        if title_mode != "source":
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        if (
            not isinstance(download, DownloadSnapshot)
            or download.status != "ready"
            or not isinstance(download.asset_id, str)
        ):
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        source_title = self._download_source_text(
            download.source_title, maximum=1024
        )
        if source_title is None:
            raise WorkflowError(_SOURCE_METADATA_ERROR)

        account_ids = upload.get("account_ids")
        bindings = upload.get("account_bindings")
        raw_overrides = upload.get("target_overrides")
        if (
            not isinstance(account_ids, list)
            or not 1 <= len(account_ids) <= MAX_WORKFLOW_ACCOUNTS
            or not isinstance(bindings, Sequence)
            or isinstance(bindings, (str, bytes))
            or len(bindings) != len(account_ids)
            or not isinstance(raw_overrides, Sequence)
            or isinstance(raw_overrides, (str, bytes))
        ):
            raise WorkflowError(_SOURCE_METADATA_ERROR)

        platforms: dict[str, str] = {}
        try:
            normalized_account_ids = [_hex_identifier(value) for value in account_ids]
            if (
                normalized_account_ids != account_ids
                or len(set(normalized_account_ids)) != len(normalized_account_ids)
            ):
                raise WorkflowError(_SOURCE_METADATA_ERROR)
            normalized_bindings = normalize_account_bindings(bindings)
            platforms = {
                binding["account_id"]: binding["platform"]
                for binding in normalized_bindings
            }
        except (UploadError, WorkflowError):
            raise WorkflowError(_SOURCE_METADATA_ERROR) from None
        if set(platforms) != set(normalized_account_ids):
            raise WorkflowError(_SOURCE_METADATA_ERROR)

        title_limits = self._upload_title_limits(set(platforms.values()))
        overrides: dict[str, dict[str, Any]] = {}
        for raw in raw_overrides:
            if not isinstance(raw, Mapping):
                raise WorkflowError(_SOURCE_METADATA_ERROR)
            try:
                account_id = _hex_identifier(raw.get("account_id"))
            except WorkflowError:
                raise WorkflowError(_SOURCE_METADATA_ERROR) from None
            if account_id not in account_ids or account_id in overrides:
                raise WorkflowError(_SOURCE_METADATA_ERROR)
            overrides[account_id] = dict(raw)

        for account_id in account_ids:
            override = overrides.setdefault(account_id, {"account_id": account_id})
            if "title" not in override:
                override["title"] = self._ellipsis_title(
                    source_title,
                    title_limits[platforms[account_id]],
                )

        concrete = dict(upload)
        concrete.pop(_UPLOAD_TITLE_MODE_KEY, None)
        concrete["title"] = self._ellipsis_title(source_title, 100)
        concrete["target_overrides"] = [overrides[value] for value in account_ids]
        return concrete

    def _upload_title_limits(self, platforms: set[str]) -> dict[str, int]:
        try:
            status = self.upload_manager.get().status()
        except Exception:
            raise WorkflowError(_SOURCE_METADATA_ERROR) from None
        if not isinstance(status, Mapping):
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        capabilities = status.get("platforms")
        if not isinstance(capabilities, Sequence) or isinstance(
            capabilities, (str, bytes)
        ):
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        limits: dict[str, int] = {}
        for capability in capabilities:
            if not isinstance(capability, Mapping):
                continue
            platform = capability.get("id")
            if platform not in platforms:
                continue
            limit = capability.get("title_limit")
            if (
                platform in limits
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 100
            ):
                raise WorkflowError(_SOURCE_METADATA_ERROR)
            limits[platform] = limit
        if set(limits) != platforms:
            raise WorkflowError(_SOURCE_METADATA_ERROR)
        return limits

    @staticmethod
    def _ellipsis_title(value: str, maximum: int) -> str:
        if len(value) <= maximum:
            return value
        if maximum == 1:
            return "…"
        return value[: maximum - 1] + "…"

    @staticmethod
    def _download_source_text(value: object, *, maximum: int) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > maximum
            or any(ord(character) < 32 for character in normalized)
            or "\x7f" in normalized
        ):
            return None
        return normalized

    @staticmethod
    def _transcription_clip_options(recipe: object) -> dict[str, int]:
        segments = getattr(recipe, "segments", None)
        if not isinstance(segments, tuple):
            raise WorkflowError("workflow_domain_data_invalid")
        if not segments:
            return {}
        if len(segments) > MAX_WORKFLOW_SEGMENTS:
            raise WorkflowError("workflow_domain_data_invalid")
        for previous, current in zip(segments, segments[1:]):
            if previous.end_ms != current.start_ms:
                # The transcribe protocol currently accepts one continuous
                # clip.  Refuse a bounding clip with gaps because it would send
                # audio outside the user's selected segments.
                raise WorkflowError("workflow_ai_segments_must_be_contiguous")
        return {
            "clip_start_ms": segments[0].start_ms,
            "clip_end_ms": segments[-1].end_ms,
        }

    def inspect_upload(
        self,
        job_ids: Sequence[str],
        expected_targets: Sequence[Mapping[str, str]] | None = None,
    ) -> UploadSnapshot:
        if (
            not isinstance(job_ids, Sequence)
            or isinstance(job_ids, (str, bytes))
            or not job_ids
        ):
            return UploadSnapshot("attention", code="upload_job_set_invalid")
        normalized_ids = [_hex_identifier(job_id) for job_id in job_ids]
        normalized_targets: list[dict[str, str]] | None = None
        if expected_targets is not None:
            try:
                normalized_targets = list(
                    normalize_upload_targets(
                        normalized_ids,
                        expected_targets,
                        maximum_jobs=(
                            MAX_WORKFLOW_SEGMENTS * MAX_WORKFLOW_ACCOUNTS
                        ),
                    )
                )
            except UploadError:
                return UploadSnapshot("attention", code="upload_job_set_invalid")
        try:
            jobs = self.upload_manager.get().latest_jobs_by_ids(normalized_ids)
        except UploadError as error:
            _domain_failure(error, "upload_failed")
        if not isinstance(jobs, list) or len(jobs) != len(normalized_ids):
            return UploadSnapshot("attention", code="upload_job_not_found")
        states: list[str] = []
        seen: set[str] = set()
        leaf_ids: list[str] = []
        for index, job in enumerate(jobs):
            if not isinstance(job, Mapping):
                return UploadSnapshot("attention", code="upload_job_set_invalid")
            job_id = _hex_identifier(job.get("id"))
            if job_id in seen:
                return UploadSnapshot("attention", code="upload_job_set_invalid")
            if normalized_targets is not None:
                try:
                    bind_current_upload_target(job, normalized_targets[index])
                except UploadError:
                    return UploadSnapshot(
                        "attention", code="upload_job_set_invalid"
                    )
            seen.add(job_id)
            leaf_ids.append(job_id)
        current_ids = tuple(leaf_ids)
        for job in jobs:
            state = job.get("state")
            if state == "unknown":
                return UploadSnapshot(
                    "attention",
                    code="upload_result_unknown",
                    job_ids=current_ids,
                )
            if (
                state not in _UPLOAD_WAITING_STATES
                and state not in _UPLOAD_SUCCESS_STATES
                and state not in {"failed", "canceled"}
            ):
                return UploadSnapshot(
                    "attention", code="upload_state_unknown", job_ids=tuple(leaf_ids)
                )
            states.append(state)
        job_codes = {
            job.get("code")
            for job in jobs
            if isinstance(job, Mapping) and isinstance(job.get("code"), str)
        }
        if job_codes.intersection(
            {
                "account_invalid",
                "account_missing",
                "account_invalid_confirmation_revoked",
            }
        ):
            return UploadSnapshot(
                "attention",
                code="upload_account_invalid",
                job_ids=current_ids,
            )
        if any(state in {"failed", "canceled"} for state in states):
            return UploadSnapshot(
                "failed", code="upload_job_failed", job_ids=current_ids
            )
        if all(state in _UPLOAD_SUCCESS_STATES for state in states):
            outcomes: set[str] = set()
            for job in jobs:
                state = job.get("state")
                mode = job.get("mode")
                if state == "submitted" and mode == "publish":
                    outcomes.add("submitted")
                elif state == "draft_saved" and mode == "draft":
                    outcomes.add("draft_saved")
                else:
                    return UploadSnapshot(
                        "attention",
                        code="upload_outcome_invalid",
                        job_ids=current_ids,
                    )
            if len(outcomes) != 1:
                return UploadSnapshot("ready", job_ids=current_ids, outcome="mixed")
            if "submitted" in outcomes:
                return UploadSnapshot(
                    "ready", job_ids=current_ids, outcome="submitted"
                )
            return UploadSnapshot(
                "ready", job_ids=current_ids, outcome="draft_saved"
            )
        if any(state == "draft" for state in states):
            draft_reasons = [
                job.get("code")
                for job in jobs
                if isinstance(job, Mapping) and job.get("state") == "draft"
            ]
            if any(
                not isinstance(reason, str)
                or (reason and _SAFE_CODE.fullmatch(reason) is None)
                for reason in draft_reasons
            ):
                return UploadSnapshot(
                    "attention", code="upload_job_set_invalid", job_ids=current_ids
                )
            codes = {
                reason for reason in draft_reasons if reason
            }
            if "account_session_changed" in codes:
                return UploadSnapshot(
                    "attention", code="account_session_changed", job_ids=current_ids
                )
            # A recovered queued retry also carries the generic restart code.
            # Lineage wins so automatic restart continuation remains limited to
            # the workflow's original, pre-authorized upload jobs.
            retry_drafts = [
                job
                for job in jobs
                if job.get("state") == "draft" and job.get("retry_of") is not None
            ]
            original_drafts = [
                job
                for job in jobs
                if job.get("state") == "draft" and job.get("retry_of") is None
            ]
            if retry_drafts:
                return UploadSnapshot(
                    "waiting",
                    code=(
                        "upload_retry_mixed_confirmation_required"
                        if original_drafts
                        else "upload_retry_confirmation_required"
                    ),
                    job_ids=current_ids,
                    needs_confirmation=True,
                )
            if codes == {"restart_confirmation_required"}:
                return UploadSnapshot(
                    "waiting",
                    code="upload_restart_confirmation_required",
                    job_ids=current_ids,
                    needs_confirmation=True,
                )
            if codes:
                review_code = next(iter(codes))
                return UploadSnapshot(
                    "waiting",
                    code=(
                        review_code
                        if len(codes) == 1
                        else "upload_review_confirmation_required"
                    ),
                    job_ids=current_ids,
                    needs_confirmation=True,
                )
            return UploadSnapshot(
                "waiting", job_ids=current_ids, needs_confirmation=True
            )
        return UploadSnapshot(
            "waiting", job_ids=current_ids, needs_confirmation=False
        )

    def confirm_uploads(
        self,
        job_ids: Sequence[str],
        account_bindings: Sequence[Mapping[str, str]],
    ) -> None:
        normalized = tuple(_hex_identifier(job_id) for job_id in job_ids)
        if (
            not normalized
            or not isinstance(account_bindings, Sequence)
            or isinstance(account_bindings, (str, bytes))
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        try:
            confirmed = self.upload_manager.get().confirm_many(
                normalized,
                expected_account_bindings=[dict(item) for item in account_bindings],
            )
        except UploadError as error:
            _domain_failure(error, "upload_failed")
        if (
            not isinstance(confirmed, list)
            or [row.get("id") for row in confirmed] != list(normalized)
        ):
            raise WorkflowError("workflow_domain_data_invalid")

    def retry_uploads(
        self,
        job_ids: Sequence[str],
        *,
        expected_targets: Sequence[Mapping[str, str]],
        account_bindings: Sequence[Mapping[str, str]],
        expected_request_keys: Sequence[str],
        expected_upload: Mapping[str, Any],
        expected_upload_cover_id: str | None,
    ) -> UploadSnapshot:
        """Atomically replace only failed upload leaves with reviewable drafts."""

        try:
            normalized_ids, normalized_targets = normalize_upload_job_batch(
                job_ids,
                expected_targets,
                maximum_jobs=MAX_WORKFLOW_SEGMENTS * MAX_WORKFLOW_ACCOUNTS,
            )
        except UploadError:
            return UploadSnapshot("attention", code="upload_job_set_invalid")
        if normalized_targets is None:
            return UploadSnapshot("attention", code="upload_job_set_invalid")
        if (
            not isinstance(expected_request_keys, Sequence)
            or isinstance(expected_request_keys, (str, bytes))
            or len(expected_request_keys) != len(normalized_ids)
            or any(not isinstance(key, str) for key in expected_request_keys)
        ):
            return UploadSnapshot("attention", code="upload_job_set_invalid")
        if (
            not isinstance(account_bindings, Sequence)
            or isinstance(account_bindings, (str, bytes))
            or any(not isinstance(binding, Mapping) for binding in account_bindings)
        ):
            return UploadSnapshot("attention", code="upload_job_set_invalid")
        if not isinstance(expected_upload, Mapping) or set(expected_upload) not in {
            frozenset(_UPLOAD_KEYS),
            frozenset(_UPLOAD_KEYS | {_UPLOAD_PREFER_DOWNLOAD_COVER_KEY}),
        }:
            return UploadSnapshot("attention", code="upload_request_invalid")
        try:
            normalized_bindings = list(
                normalize_account_bindings(
                    [dict(item) for item in account_bindings]
                )
            )
            profile_bindings = list(
                normalize_account_bindings(expected_upload.get("account_bindings"))
            )
        except UploadError:
            return UploadSnapshot("attention", code="upload_request_invalid")
        account_ids = expected_upload.get("account_ids")
        if (
            not isinstance(account_ids, list)
            or not account_ids
            or any(not isinstance(account_id, str) for account_id in account_ids)
            or len(set(account_ids)) != len(account_ids)
            or {
                binding["account_id"]: binding for binding in normalized_bindings
            }
            != {binding["account_id"]: binding for binding in profile_bindings}
            or {binding["account_id"] for binding in normalized_bindings}
            != set(account_ids)
        ):
            return UploadSnapshot("attention", code="upload_request_mismatch")
        platforms = {
            binding["account_id"]: binding["platform"]
            for binding in normalized_bindings
        }
        try:
            service = self.upload_manager.get()
        except UploadError as error:
            _domain_failure(error, "upload_failed")
        cover_record: Mapping[str, Any] | None = None
        if expected_upload_cover_id is not None:
            try:
                expected_upload_cover_id = _hex_identifier(
                    expected_upload_cover_id
                )
                cover_record = service.cover(expected_upload_cover_id)
                if (
                    not isinstance(cover_record, Mapping)
                    or _record_id(cover_record) != expected_upload_cover_id
                ):
                    return UploadSnapshot(
                        "attention", code="upload_request_mismatch"
                    )
            except (UploadError, WorkflowError):
                return UploadSnapshot("attention", code="upload_request_mismatch")
        elif expected_upload.get(_UPLOAD_PREFER_DOWNLOAD_COVER_KEY) is True:
            return UploadSnapshot("attention", code="upload_request_mismatch")
        try:
            overrides = self._upload_overrides(
                expected_upload.get("target_overrides"),
                account_ids,
                platforms,
                cover_record,
                expected_upload_cover_id,
            )
        except WorkflowError:
            return UploadSnapshot("attention", code="upload_request_mismatch")

        grouped_targets: dict[str, list[Mapping[str, str]]] = {}
        for request_key, target in zip(
            expected_request_keys, normalized_targets, strict=True
        ):
            grouped_targets.setdefault(request_key, []).append(target)
        expected_requests: dict[str, dict[str, object]] = {}
        source_ids: set[str] = set()
        for request_key, targets in grouped_targets.items():
            target_source_ids = {target["source_id"] for target in targets}
            if (
                [target["account_id"] for target in targets] != account_ids
                or any(
                    target["platform"] != platforms[target["account_id"]]
                    for target in targets
                )
                or len(target_source_ids) != 1
            ):
                return UploadSnapshot("attention", code="upload_job_set_invalid")
            source_id = next(iter(target_source_ids))
            if source_id in source_ids:
                return UploadSnapshot("attention", code="upload_job_set_invalid")
            source_ids.add(source_id)
            expected_requests[request_key] = workflow_upload_request(
                source_id, account_ids, expected_upload, overrides
            )

        try:
            retried = service.retry_many(
                normalized_ids,
                expected_targets=normalized_targets,
                expected_account_bindings=normalized_bindings,
                expected_request_keys=list(expected_request_keys),
                expected_requests=expected_requests,
            )
        except UploadError as error:
            if error.code in {"job_retry_lineage_changed", "retry_not_allowed"}:
                return self.inspect_upload(
                    normalized_ids,
                    expected_targets=normalized_targets,
                )
            if error.code == "verify_remote_result_first":
                return UploadSnapshot("attention", code="upload_result_unknown")
            if error.code == "account_session_changed":
                return UploadSnapshot("attention", code="account_session_changed")
            if error.code in {"upload_request_invalid", "upload_request_mismatch"}:
                return UploadSnapshot("attention", code=error.code)
            if error.code in {"job_retry_lineage_invalid", "invalid_job_batch"}:
                return UploadSnapshot("attention", code="upload_job_set_invalid")
            _domain_failure(error, "upload_failed")
        if not isinstance(retried, list) or len(retried) != len(normalized_ids):
            raise WorkflowError("workflow_domain_data_invalid")
        replacement_ids: list[str] = []
        rebound_targets: list[dict[str, str]] = []
        for row, target in zip(retried, normalized_targets, strict=True):
            if not isinstance(row, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            replacement_id = _record_id(row)
            try:
                bound = bind_current_upload_target(row, target)
            except UploadError:
                raise WorkflowError("workflow_domain_data_invalid") from None
            if bound["job_id"] != replacement_id:
                raise WorkflowError("workflow_domain_data_invalid")
            replacement_ids.append(replacement_id)
            rebound_targets.append(bound)
        return self.inspect_upload(
            replacement_ids,
            expected_targets=rebound_targets,
        )

    def cancel_uploads(
        self,
        job_ids: Sequence[str],
        *,
        expected_targets: Sequence[Mapping[str, str]],
        expected_account_bindings: Sequence[Mapping[str, str]],
    ) -> CancellationSnapshot:
        """Cancel a workflow fan-out only while every persisted identity matches."""

        if (
            not isinstance(expected_account_bindings, Sequence)
            or isinstance(expected_account_bindings, (str, bytes))
            or not 1 <= len(expected_account_bindings) <= MAX_WORKFLOW_ACCOUNTS
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        try:
            normalized_ids, normalized_targets = normalize_upload_job_batch(
                job_ids,
                expected_targets,
                maximum_jobs=MAX_WORKFLOW_SEGMENTS * MAX_WORKFLOW_ACCOUNTS,
            )
        except UploadError:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if normalized_targets is None:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")

        target_accounts: dict[str, str] = {}
        for target in normalized_targets:
            previous_platform = target_accounts.setdefault(
                target["account_id"], target["platform"]
            )
            if previous_platform != target["platform"]:
                return CancellationSnapshot(
                    "attention", code="upload_job_set_invalid"
                )
        if len(target_accounts) > MAX_WORKFLOW_ACCOUNTS:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        try:
            normalized_bindings = list(
                normalize_account_bindings(expected_account_bindings)
            )
        except UploadError:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        seen_accounts = {binding["account_id"] for binding in normalized_bindings}
        if any(
            target_accounts.get(binding["account_id"]) != binding["platform"]
            for binding in normalized_bindings
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if seen_accounts != set(target_accounts):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")

        try:
            service = self.upload_manager.get()
            jobs = service.latest_jobs_by_ids(list(normalized_ids))
        except UploadError as error:
            if error.code in {"job_not_found", "invalid_job_ids"}:
                return CancellationSnapshot("attention", code="upload_job_not_found")
            _domain_failure(error, "upload_failed")
        if not isinstance(jobs, list) or len(jobs) != len(normalized_ids):
            return CancellationSnapshot("attention", code="upload_job_not_found")
        leaf_targets: list[dict[str, str]] = []
        leaf_ids: set[str] = set()
        for job, expected in zip(jobs, normalized_targets, strict=True):
            try:
                leaf_target = bind_current_upload_target(job, expected)
            except UploadError:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            leaf_id = leaf_target["job_id"]
            if leaf_id in leaf_ids:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            leaf_ids.add(leaf_id)
            leaf_targets.append(leaf_target)

        try:
            canceled = service.cancel_many(
                [target["job_id"] for target in leaf_targets],
                expected_targets=leaf_targets,
                expected_account_bindings=normalized_bindings,
            )
        except UploadError as error:
            if error.code in {
                "invalid_job_batch", "invalid_account_bindings", "job_not_found",
            }:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            if error.code == "job_retry_lineage_changed":
                return CancellationSnapshot(
                    "waiting", code="upload_retry_lineage_changed"
                )
            _domain_failure(error, "upload_failed")
        if (
            not isinstance(canceled, list)
            or len(canceled) != len(leaf_targets)
            or any(
                not isinstance(job, Mapping)
                or job.get("id") != expected["job_id"]
                or any(
                    job.get(field) != expected[field]
                    for field in ("source_id", "account_id", "platform")
                )
                for job, expected in zip(canceled, leaf_targets, strict=True)
            )
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        result = self._upload_cancellation_aggregate(
            canceled, cancellation_requested=True
        )
        return result or CancellationSnapshot(
            "attention", code="upload_cancellation_failed"
        )

    def cancel_uploads_for_workflow(
        self,
        workflow_id: str,
        output_id: str,
        segment_ordinal: int,
        existing_job_ids: Sequence[str],
        *,
        expected_targets: Sequence[Mapping[str, str]],
        expected_account_ids: Sequence[str],
        expected_account_bindings: Sequence[Mapping[str, str]],
        expected_upload: Mapping[str, Any],
        expected_cover_id: str | None,
        expected_download_asset_id: str | None = None,
        expected_upload_cover_id: str | None = None,
    ) -> CancellationSnapshot:
        """Discover an uncheckpointed upload fan-out and cancel it with prior roots."""

        try:
            workflow_id = _hex_identifier(workflow_id)
            output_id = _hex_identifier(output_id)
        except WorkflowError:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if (
            isinstance(segment_ordinal, bool)
            or not isinstance(segment_ordinal, int)
            or not 1 <= segment_ordinal <= MAX_WORKFLOW_SEGMENTS
            or not isinstance(existing_job_ids, Sequence)
            or isinstance(existing_job_ids, (str, bytes))
            or not isinstance(expected_targets, Sequence)
            or isinstance(expected_targets, (str, bytes))
            or len(existing_job_ids) != len(expected_targets)
            or not isinstance(expected_account_ids, Sequence)
            or isinstance(expected_account_ids, (str, bytes))
            or not 1 <= len(expected_account_ids) <= MAX_WORKFLOW_ACCOUNTS
            or not isinstance(expected_account_bindings, Sequence)
            or isinstance(expected_account_bindings, (str, bytes))
            or not isinstance(expected_upload, Mapping)
            or set(expected_upload) not in {
                frozenset(_UPLOAD_KEYS),
                frozenset(
                    _UPLOAD_KEYS | {_UPLOAD_PREFER_DOWNLOAD_COVER_KEY}
                ),
            }
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        prefer_download_cover = expected_upload.get(
            _UPLOAD_PREFER_DOWNLOAD_COVER_KEY, False
        )
        if (
            type(prefer_download_cover) is not bool
            or (
                _UPLOAD_PREFER_DOWNLOAD_COVER_KEY in expected_upload
                and prefer_download_cover is not True
            )
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if prefer_download_cover:
            if expected_cover_id is None or expected_download_asset_id is None:
                return CancellationSnapshot(
                    "attention", code="upload_job_set_invalid"
                )
            try:
                expected_cover_id = _hex_identifier(expected_cover_id)
                expected_download_asset_id = _download_identifier(
                    expected_download_asset_id
                )
            except WorkflowError:
                return CancellationSnapshot(
                    "attention", code="upload_job_set_invalid"
                )
        elif (
            expected_download_asset_id is not None
            or expected_upload_cover_id is not None
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if expected_upload_cover_id is not None:
            try:
                expected_upload_cover_id = _hex_identifier(
                    expected_upload_cover_id
                )
            except WorkflowError:
                return CancellationSnapshot(
                    "attention", code="upload_job_set_invalid"
                )
        try:
            account_ids = [_hex_identifier(value) for value in expected_account_ids]
        except WorkflowError:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if len(set(account_ids)) != len(account_ids):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        try:
            normalized_bindings = normalize_account_bindings(
                expected_account_bindings
            )
        except UploadError:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        binding_records = {
            binding["account_id"]: binding for binding in normalized_bindings
        }
        if set(binding_records) != set(account_ids):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        binding_platforms = {
            account_id: binding["platform"]
            for account_id, binding in binding_records.items()
        }
        profile_bindings = expected_upload.get("account_bindings")
        if not isinstance(profile_bindings, Sequence) or isinstance(
            profile_bindings, (str, bytes)
        ):
            return CancellationSnapshot("attention", code="upload_request_mismatch")
        try:
            profile_binding_records = {
                binding["account_id"]: binding
                for binding in normalize_account_bindings(profile_bindings)
            }
        except UploadError:
            return CancellationSnapshot("attention", code="upload_request_mismatch")
        if (
            expected_upload.get("account_ids") != account_ids
            or profile_binding_records != binding_records
        ):
            return CancellationSnapshot("attention", code="upload_request_mismatch")

        request_key = workflow_upload_request_key(workflow_id, segment_ordinal)
        try:
            service = self.upload_manager.get()
        except UploadError as error:
            _domain_failure(error, "upload_failed")

        claimed_jobs: list[dict] | None = None
        if prefer_download_cover:
            try:
                claimed_jobs = service.claim_or_read_workflow_cancellation(
                    request_key
                )
            except UploadError as error:
                if error.code in {
                    "invalid_idempotency_key",
                    "upload_request_invalid",
                    "job_not_found",
                }:
                    return CancellationSnapshot(
                        "attention", code="upload_request_invalid"
                    )
                if error.code in {
                    "upload_request_mismatch",
                    "idempotency_conflict",
                }:
                    return CancellationSnapshot(
                        "attention", code="upload_request_mismatch"
                    )
                _domain_failure(error, "upload_failed")
            if not claimed_jobs:
                if not existing_job_ids:
                    return CancellationSnapshot("stopped")
                return self.cancel_uploads(
                    existing_job_ids,
                    expected_targets=expected_targets,
                    expected_account_bindings=expected_account_bindings,
                )

        try:
            output = self.editing_manager.invoke("asset", output_id)
        except EditingError as error:
            if error.code in {"asset_not_found", "invalid_identifier"}:
                return CancellationSnapshot("attention", code="edit_output_mismatch")
            _domain_failure(error, "editing_failed")
        if (
            not isinstance(output, Mapping)
            or output.get("id") != output_id
            or output.get("kind") not in {"segment", "dubbed_video"}
        ):
            return CancellationSnapshot("attention", code="edit_output_mismatch")
        try:
            output_sha256 = _sha256(output.get("sha256"))
        except WorkflowError:
            return CancellationSnapshot("attention", code="edit_output_mismatch")
        source_id = _managed_import_id(
            workflow_id, "edit_video", output_id, output_sha256
        )

        edit_cover_record: Mapping[str, Any] | None = None
        edit_cover_sha256 = None
        edit_cover_name = None
        edit_imported_cover_id = None
        if expected_cover_id is not None:
            try:
                expected_cover_id = _hex_identifier(expected_cover_id)
            except WorkflowError:
                return CancellationSnapshot("attention", code="edit_output_mismatch")
            try:
                candidate = self.editing_manager.invoke("asset", expected_cover_id)
            except EditingError as error:
                if error.code in {"asset_not_found", "invalid_identifier"}:
                    return CancellationSnapshot(
                        "attention", code="edit_output_mismatch"
                    )
                _domain_failure(error, "editing_failed")
            if (
                not isinstance(candidate, Mapping)
                or candidate.get("id") != expected_cover_id
                or candidate.get("kind") != "cover"
            ):
                return CancellationSnapshot("attention", code="edit_output_mismatch")
            try:
                cover_sha256 = _sha256(candidate.get("sha256"))
            except WorkflowError:
                return CancellationSnapshot("attention", code="edit_output_mismatch")
            cover_name = candidate.get("name")
            if not isinstance(cover_name, str):
                return CancellationSnapshot("attention", code="edit_output_mismatch")
            edit_cover_record = candidate
            edit_cover_sha256 = cover_sha256
            edit_cover_name = cover_name
            edit_imported_cover_id = _managed_import_id(
                workflow_id,
                "edit_cover",
                expected_cover_id,
                cover_sha256,
            )

        try:
            cover_record = edit_cover_record
            imported_cover_id = edit_imported_cover_id
            if prefer_download_cover:
                if not claimed_jobs:
                    raise WorkflowError("upload_request_mismatch")
                imported_cover_id = self._workflow_request_cover_id(
                    claimed_jobs,
                    source_id=source_id,
                    account_ids=account_ids,
                    platforms=binding_platforms,
                )
                cover_record = self._frozen_workflow_cover_copy(
                    service,
                    workflow_id,
                    imported_cover_id,
                    edit_cover_id=edit_imported_cover_id,
                    edit_cover_sha256=edit_cover_sha256,
                    edit_cover_name=edit_cover_name,
                    require_media=False,
                )
                if expected_upload_cover_id not in {None, imported_cover_id}:
                    return CancellationSnapshot(
                        "attention", code="upload_request_mismatch"
                    )
                overrides = self._upload_overrides(
                    expected_upload.get("target_overrides"),
                    account_ids,
                    binding_platforms,
                    cover_record,
                    imported_cover_id,
                )
                request_jobs = service.claim_workflow_request_for_cancellation(
                    request_key,
                    expected_request=workflow_upload_request(
                        source_id, account_ids, expected_upload, overrides
                    ),
                )
            else:
                overrides = self._upload_overrides(
                    expected_upload.get("target_overrides"),
                    account_ids,
                    binding_platforms,
                    cover_record,
                    imported_cover_id,
                )
                request_jobs = service.claim_workflow_request_for_cancellation(
                    request_key,
                    expected_request=workflow_upload_request(
                        source_id, account_ids, expected_upload, overrides
                    ),
                )
        except WorkflowError as error:
            if error.code in {
                "workflow_source_cover_unavailable",
                "workflow_source_cover_ambiguous",
            }:
                return CancellationSnapshot("attention", code=error.code)
            return CancellationSnapshot("attention", code="upload_request_mismatch")
        except UploadError as error:
            if error.code in {
                "invalid_idempotency_key", "upload_request_invalid", "job_not_found",
            }:
                return CancellationSnapshot("attention", code="upload_request_invalid")
            if error.code in {"upload_request_mismatch", "idempotency_conflict"}:
                return CancellationSnapshot("attention", code="upload_request_mismatch")
            _domain_failure(error, "upload_failed")

        combined_ids = list(existing_job_ids)
        combined_targets = list(expected_targets)
        if request_jobs is not None:
            if not isinstance(request_jobs, list) or len(request_jobs) != len(account_ids):
                return CancellationSnapshot("attention", code="upload_request_invalid")
            discovered_ids: set[str] = set()
            for account_id, job in zip(account_ids, request_jobs, strict=True):
                if not isinstance(job, Mapping):
                    return CancellationSnapshot("attention", code="upload_request_invalid")
                try:
                    job_id = _record_id(job)
                except WorkflowError:
                    return CancellationSnapshot("attention", code="upload_request_invalid")
                if job_id in discovered_ids or job_id in combined_ids:
                    return CancellationSnapshot("attention", code="upload_request_mismatch")
                try:
                    discovered_target = bind_current_upload_target(
                        job,
                        {
                            "job_id": job_id,
                            "source_id": source_id,
                            "account_id": account_id,
                            "platform": binding_platforms[account_id],
                        },
                    )
                except UploadError:
                    return CancellationSnapshot(
                        "attention", code="upload_request_mismatch"
                    )
                discovered_ids.add(job_id)
                combined_ids.append(job_id)
                combined_targets.append(discovered_target)
        if not combined_ids:
            return CancellationSnapshot("stopped")
        return self.cancel_uploads(
            combined_ids,
            expected_targets=combined_targets,
            expected_account_bindings=expected_account_bindings,
        )

    @staticmethod
    def _upload_cancellation_aggregate(
        jobs: Sequence[Mapping[str, Any]],
        *,
        cancellation_requested: bool = False,
    ) -> CancellationSnapshot | None:
        states = [job.get("state") for job in jobs]
        allowed = {
            "draft", "queued", "running", "canceling", "submitted",
            "draft_saved", "failed", "canceled", "unknown",
        }
        if any(state not in allowed for state in states):
            return CancellationSnapshot("attention", code="upload_state_unknown")
        if cancellation_requested and any(
            state in {"running", "canceling"} for state in states
        ):
            return CancellationSnapshot(
                "waiting", code="upload_cancellation_pending"
            )
        if "unknown" in states:
            return CancellationSnapshot("attention", code="upload_result_unknown")
        succeeded = [state in _UPLOAD_SUCCESS_STATES for state in states]
        if any(succeeded) and not all(succeeded):
            return CancellationSnapshot(
                "attention", code="upload_partially_completed"
            )
        if all(succeeded):
            outcomes: set[str] = set()
            for job in jobs:
                if job.get("state") == "submitted" and job.get("mode") == "publish":
                    outcomes.add("submitted")
                elif job.get("state") == "draft_saved" and job.get("mode") == "draft":
                    outcomes.add("draft_saved")
                else:
                    return CancellationSnapshot(
                        "attention", code="upload_outcome_invalid"
                    )
            outcome = next(iter(outcomes)) if len(outcomes) == 1 else "mixed"
            return CancellationSnapshot("upload_completed", outcome=outcome)
        if all(state in {"failed", "canceled"} for state in states):
            return CancellationSnapshot("stopped")
        return None

    @staticmethod
    def _matching_batch_id(
        batch: object, expected_name: str, source_url: str
    ) -> str:
        if not isinstance(batch, Mapping) or batch.get("name") != expected_name:
            raise WorkflowError("download_batch_conflict")
        inputs = batch.get("inputs")
        if (
            not isinstance(inputs, Sequence)
            or isinstance(inputs, (str, bytes))
            or len(inputs) != 1
            or not isinstance(inputs[0], Mapping)
            or inputs[0].get("raw_text") != source_url
        ):
            raise WorkflowError("download_batch_conflict")
        return _download_identifier(batch.get("id"))

    @staticmethod
    def _account_platforms(
        accounts: object, account_ids: Sequence[str]
    ) -> dict[str, str]:
        if not isinstance(accounts, Sequence) or isinstance(accounts, (str, bytes)):
            raise WorkflowError("workflow_domain_data_invalid")
        wanted = set(account_ids)
        platforms: dict[str, str] = {}
        for account in accounts:
            if not isinstance(account, Mapping) or account.get("id") not in wanted:
                continue
            account_id = _hex_identifier(account.get("id"))
            platform = account.get("platform")
            if platform not in {"bilibili", "douyin", "tencent"}:
                raise WorkflowError("upload_platform_not_supported")
            if account_id in platforms:
                raise WorkflowError("workflow_domain_data_invalid")
            platforms[account_id] = platform
        if set(platforms) != wanted:
            raise WorkflowError("account_not_found")
        return platforms

    @staticmethod
    def _upload_overrides(
        raw_overrides: object,
        account_ids: Sequence[str],
        platforms: Mapping[str, str],
        cover: Mapping[str, Any] | None,
        cover_id: str | None,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(raw_overrides, Sequence)
            or isinstance(raw_overrides, (str, bytes))
        ):
            raise WorkflowError("workflow_domain_data_invalid")
        by_account: dict[str, dict[str, Any]] = {}
        for raw in raw_overrides:
            if not isinstance(raw, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            account_id = _hex_identifier(raw.get("account_id"))
            if account_id not in account_ids or account_id in by_account:
                raise WorkflowError("workflow_domain_data_invalid")
            by_account[account_id] = dict(raw)

        if cover is not None and cover_id is not None:
            width, height = cover.get("width"), cover.get("height")
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or width < 1
                or isinstance(height, bool)
                or not isinstance(height, int)
                or height < 1
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            for account_id in account_ids:
                slot = cover_slot(
                    platforms[account_id], width, height
                )
                if slot is None:
                    raise WorkflowError("workflow_cover_incompatible")
                override = by_account.setdefault(
                    account_id, {"account_id": account_id}
                )
                existing_covers = {
                    key: override[key]
                    for key in _COVER_KEYS
                    if override.get(key) is not None
                }
                if existing_covers and existing_covers != {slot: cover_id}:
                    raise WorkflowError("upload_cover_override_conflict")
                override[slot] = cover_id
        return [
            by_account[account_id]
            for account_id in account_ids
            if account_id in by_account
        ]



__all__ = ["LocalWorkflowAdapter"]

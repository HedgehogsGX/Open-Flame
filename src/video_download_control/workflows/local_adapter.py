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
from ..editing.contracts import EditingError, recipe_from_mapping
from ..service import BatchService, BatchValidationError
from ..uploads.contracts import UploadError
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
)
from .service import WorkflowError

if TYPE_CHECKING:
    from ..editing.api import EditingManager


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
_COVER_KEYS = {"cover_landscape_asset_id", "cover_portrait_asset_id"}
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
        if (
            not normalized.translation.enabled
            or not normalized.dubbing.enabled
            or normalized.dubbing.authorization is None
            or set(ai) != _BOUND_AI_KEYS
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
        if not isinstance(upload, Mapping) or set(upload) != _UPLOAD_INPUT_KEYS:
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
                or self._cover_slot(platform, *dimensions) is None
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
        batch = self.batch_service.get_batch(batch_id)
        if not isinstance(batch, Mapping):
            return DownloadSnapshot("attention", code="download_batch_not_found")
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

        assets = self.batch_service.list_ready_assets_for_batch(batch_id)
        if assets is None:
            return DownloadSnapshot("attention", code="download_batch_not_found")
        if not isinstance(assets, list):
            return DownloadSnapshot("attention", code="download_state_invalid")
        if len(assets) > 1:
            return DownloadSnapshot("attention", code="download_multiple_assets")
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
            return DownloadSnapshot("ready", asset_id=asset_id)

        if state == "queued":
            if not any(
                job_state
                in {"queued", "probing", "downloading", "postprocessing", "verifying"}
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
        ai_keys = set(ai)
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
                authorization=authorization,
                explicit=explicit,
                confirmation_code="ai_translation_confirmation_required",
                review_code="ai_translation_review_required",
            )
            if isinstance(translated, AiSnapshot):
                return translated

            ready_recipe = normalized.to_dict()
            ready_recipe["translation"]["source_language"] = source_language
            ready_recipe["translation"]["state"] = "ready"
            if normalized.dubbing.enabled:
                ready_recipe["dubbing"]["state"] = "ready"
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
            if draft.get("recipe") != ready_recipe:
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
                timeline_revision_id=_record_id(translated),
            )
            plan_id = _record_id(plan)
            if (
                plan.get("project_id") != project_id
                or plan.get("draft_version") != draft_version
                or plan.get("timeline_revision_id") != _record_id(translated)
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            return AiSnapshot(
                "ready", draft_version=draft_version, plan_id=plan_id
            )
        except EditingError as error:
            _domain_failure(error, "ai_pipeline_failed")

    def _advance_ai_task(
        self,
        task: Mapping[str, Any],
        *,
        authorization: list[bool],
        explicit: bool,
        confirmation_code: str,
        review_code: str,
    ) -> Mapping[str, Any] | AiSnapshot:
        if not isinstance(task, Mapping):
            raise WorkflowError("workflow_domain_data_invalid")
        task = self._latest_ai_task(task)
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

    def _latest_ai_task(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        task_id = _record_id(task)
        project_id = _hex_identifier(task.get("project_id"))
        rows = self.editing_manager.invoke("ai_tasks", project_id=project_id)
        if not isinstance(rows, list):
            raise WorkflowError("workflow_domain_data_invalid")
        successors: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise WorkflowError("workflow_domain_data_invalid")
            retry_of = row.get("retry_of")
            if retry_of is not None:
                retry_of = _hex_identifier(retry_of)
                if retry_of in successors:
                    raise WorkflowError("workflow_domain_data_invalid")
                successors[retry_of] = row
        current = task
        seen: set[str] = set()
        while task_id in successors:
            if task_id in seen:
                raise WorkflowError("workflow_domain_data_invalid")
            seen.add(task_id)
            current = successors[task_id]
            task_id = _record_id(current)
        return current

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
        ai_keys = set(ai)
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
            current = self._latest_ai_task(transcription)
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
            source_revision_id = _hex_identifier(current.get("result_revision_id"))
            source = self.editing_manager.invoke("timeline", source_revision_id)
            if isinstance(source, Mapping) and source.get("state") == "rejected":
                self.editing_manager.invoke(
                    "retry_ai_task",
                    _record_id(current),
                    f"wf-{workflow_id}-retry-{_record_id(current)}",
                )
                return
            if not isinstance(source, Mapping) or source.get("state") != "approved":
                raise WorkflowError("workflow_ai_retry_not_available")
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
            current = self._latest_ai_task(translation)
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
            )
        if state in _EDIT_WAITING_STATES:
            return EditSnapshot("waiting")
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
        expected_video_count = len(recipe.segments) or (
            1 if recipe.dubbing.enabled else 0
        )
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

    def prepare_upload(
        self,
        workflow_id: str,
        output_id: str,
        cover_id: str | None,
        upload: Mapping[str, Any],
        *,
        segment_ordinal: int = 1,
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
        if not isinstance(upload, Mapping) or set(upload) != _UPLOAD_KEYS:
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

            imported_cover_id = None
            cover_record: Mapping[str, Any] | None = None
            if cover_id is not None and cover_identity is not None:
                cover_path, cover_sha256, cover_name = cover_identity
                imported = service.import_cover(
                    cover_path,
                    cover_name,
                    expected_sha256=cover_sha256,
                    managed_id=_managed_import_id(
                        workflow_id, "edit_cover", cover_id, cover_sha256
                    ),
                )
                if not isinstance(imported, Mapping):
                    raise WorkflowError("workflow_domain_data_invalid")
                imported_cover_id = _record_id(imported)
                cover_record = imported

            overrides = self._upload_overrides(
                upload.get("target_overrides"),
                account_ids,
                platforms,
                cover_record,
                imported_cover_id,
            )

            source = service.import_source(
                output_path,
                output_name,
                expected_sha256=output_sha256,
                managed_id=_managed_import_id(
                    workflow_id, "edit_video", output_id, output_sha256
                ),
            )
            source_id = _record_id(source)
            jobs = service.create_jobs(
                source_id=source_id,
                account_ids=list(account_ids),
                title=upload["title"],
                description=upload["description"],
                tags=list(upload["tags"]),
                category_id=upload["category_id"],
                mode=upload["mode"],
                copyright=upload["copyright"],
                source_credit=upload["source_credit"],
                target_overrides=overrides,
                expected_account_bindings=[dict(binding) for binding in bindings],
                idempotency_key=(
                    f"wf-{workflow_id}-upload-jobs"
                    if segment_ordinal == 1
                    else f"wf-{workflow_id}-upload-jobs-{segment_ordinal:03d}"
                ),
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
            if (
                job_id in job_ids
                or job.get("source_id") != source_id
                or job.get("account_id") != account_id
                or job.get("platform") != platforms[account_id]
            ):
                raise WorkflowError("workflow_domain_data_invalid")
            job_ids.append(job_id)
        return UploadPrepared(
            source_id=source_id,
            cover_id=imported_cover_id,
            job_ids=tuple(job_ids),
        )

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
            if (
                not isinstance(expected_targets, Sequence)
                or isinstance(expected_targets, (str, bytes))
                or len(expected_targets) != len(normalized_ids)
                or len(expected_targets)
                > MAX_WORKFLOW_SEGMENTS * MAX_WORKFLOW_ACCOUNTS
            ):
                return UploadSnapshot("attention", code="upload_job_set_invalid")
            normalized_targets = []
            for index, target in enumerate(expected_targets):
                if not isinstance(target, Mapping) or set(target) != {
                    "job_id",
                    "source_id",
                    "account_id",
                    "platform",
                }:
                    return UploadSnapshot("attention", code="upload_job_set_invalid")
                try:
                    job_id = _hex_identifier(target.get("job_id"))
                    source_id = _hex_identifier(target.get("source_id"))
                    account_id = _hex_identifier(target.get("account_id"))
                except WorkflowError:
                    return UploadSnapshot("attention", code="upload_job_set_invalid")
                platform = target.get("platform")
                if (
                    job_id != normalized_ids[index]
                    or not isinstance(platform, str)
                    or platform not in {"bilibili", "douyin", "tencent"}
                ):
                    return UploadSnapshot("attention", code="upload_job_set_invalid")
                normalized_targets.append(
                    {
                        "job_id": job_id,
                        "source_id": source_id,
                        "account_id": account_id,
                        "platform": platform,
                    }
                )
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
                expected = normalized_targets[index]
                if any(
                    job.get(field) != expected[field]
                    for field in ("source_id", "account_id", "platform")
                ):
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
            if any(job.get("retry_of") is not None for job in jobs):
                return UploadSnapshot(
                    "waiting",
                    code="upload_retry_confirmation_required",
                    job_ids=current_ids,
                )
            if codes == {"restart_confirmation_required"}:
                return UploadSnapshot(
                    "waiting",
                    code="upload_restart_confirmation_required",
                    job_ids=current_ids,
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
                )
        return UploadSnapshot("waiting", job_ids=current_ids)

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

    def cancel_uploads(
        self,
        job_ids: Sequence[str],
        *,
        expected_targets: Sequence[Mapping[str, str]],
        expected_account_bindings: Sequence[Mapping[str, str]],
    ) -> CancellationSnapshot:
        """Cancel a workflow fan-out only while every persisted identity matches."""

        if (
            not isinstance(job_ids, Sequence)
            or isinstance(job_ids, (str, bytes))
            or not 1 <= len(job_ids) <= MAX_WORKFLOW_SEGMENTS * MAX_WORKFLOW_ACCOUNTS
            or not isinstance(expected_targets, Sequence)
            or isinstance(expected_targets, (str, bytes))
            or len(expected_targets) != len(job_ids)
            or not isinstance(expected_account_bindings, Sequence)
            or isinstance(expected_account_bindings, (str, bytes))
            or not 1 <= len(expected_account_bindings) <= MAX_WORKFLOW_ACCOUNTS
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        try:
            normalized_ids = [_hex_identifier(job_id) for job_id in job_ids]
        except WorkflowError:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if len(set(normalized_ids)) != len(normalized_ids):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")

        normalized_targets: list[dict[str, str]] = []
        for index, target in enumerate(expected_targets):
            if not isinstance(target, Mapping) or set(target) != {
                "job_id", "source_id", "account_id", "platform",
            }:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            try:
                normalized = {
                    "job_id": _hex_identifier(target.get("job_id")),
                    "source_id": _hex_identifier(target.get("source_id")),
                    "account_id": _hex_identifier(target.get("account_id")),
                    "platform": target.get("platform"),
                }
            except WorkflowError:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            if (
                normalized["job_id"] != normalized_ids[index]
                or normalized["platform"] not in {"bilibili", "douyin", "tencent"}
            ):
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            normalized_targets.append(normalized)

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
        normalized_bindings: list[dict[str, str]] = []
        seen_accounts: set[str] = set()
        for binding in expected_account_bindings:
            if not isinstance(binding, Mapping) or set(binding) != {
                "account_id", "platform", "session_revision",
            }:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            try:
                account_id = _hex_identifier(binding.get("account_id"))
                session_revision = _hex_identifier(binding.get("session_revision"))
            except WorkflowError:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            platform = binding.get("platform")
            if (
                account_id in seen_accounts
                or platform not in {"bilibili", "douyin", "tencent"}
                or target_accounts.get(account_id) != platform
            ):
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            seen_accounts.add(account_id)
            normalized_bindings.append(
                {
                    "account_id": account_id,
                    "platform": platform,
                    "session_revision": session_revision,
                }
            )
        if seen_accounts != set(target_accounts):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")

        try:
            service = self.upload_manager.get()
            jobs = service.latest_jobs_by_ids(normalized_ids)
        except UploadError as error:
            if error.code in {"job_not_found", "invalid_job_ids"}:
                return CancellationSnapshot("attention", code="upload_job_not_found")
            _domain_failure(error, "upload_failed")
        if not isinstance(jobs, list) or len(jobs) != len(normalized_ids):
            return CancellationSnapshot("attention", code="upload_job_not_found")
        leaf_targets: list[dict[str, str]] = []
        leaf_ids: set[str] = set()
        for job, expected in zip(jobs, normalized_targets, strict=True):
            if not isinstance(job, Mapping):
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            try:
                leaf_id = _record_id(job)
            except WorkflowError:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            if leaf_id in leaf_ids or any(
                job.get(field) != expected[field]
                for field in ("source_id", "account_id", "platform")
            ):
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            leaf_ids.add(leaf_id)
            leaf_targets.append({**expected, "job_id": leaf_id})

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
            or set(expected_upload) != _UPLOAD_KEYS
        ):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        try:
            account_ids = [_hex_identifier(value) for value in expected_account_ids]
        except WorkflowError:
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        if len(set(account_ids)) != len(account_ids):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        binding_platforms: dict[str, str] = {}
        binding_records: dict[str, dict[str, str]] = {}
        for binding in expected_account_bindings:
            if not isinstance(binding, Mapping) or set(binding) != {
                "account_id", "platform", "session_revision",
            }:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            try:
                account_id = _hex_identifier(binding.get("account_id"))
                session_revision = _hex_identifier(binding.get("session_revision"))
            except WorkflowError:
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            platform = binding.get("platform")
            if (
                account_id not in account_ids
                or account_id in binding_platforms
                or platform not in {"bilibili", "douyin", "tencent"}
            ):
                return CancellationSnapshot("attention", code="upload_job_set_invalid")
            binding_platforms[account_id] = platform
            binding_records[account_id] = {
                "account_id": account_id,
                "platform": platform,
                "session_revision": session_revision,
            }
        if set(binding_platforms) != set(account_ids):
            return CancellationSnapshot("attention", code="upload_job_set_invalid")
        profile_bindings = expected_upload.get("account_bindings")
        if not isinstance(profile_bindings, Sequence) or isinstance(
            profile_bindings, (str, bytes)
        ):
            return CancellationSnapshot("attention", code="upload_request_mismatch")
        try:
            profile_binding_records = {
                _hex_identifier(binding.get("account_id")): dict(binding)
                for binding in profile_bindings
                if isinstance(binding, Mapping)
            }
        except WorkflowError:
            return CancellationSnapshot("attention", code="upload_request_mismatch")
        if (
            expected_upload.get("account_ids") != account_ids
            or profile_binding_records != binding_records
        ):
            return CancellationSnapshot("attention", code="upload_request_mismatch")

        request_key = (
            f"wf-{workflow_id}-upload-jobs"
            if segment_ordinal == 1
            else f"wf-{workflow_id}-upload-jobs-{segment_ordinal:03d}"
        )
        try:
            service = self.upload_manager.get()
            output = self.editing_manager.invoke("asset", output_id)
        except UploadError as error:
            _domain_failure(error, "upload_failed")
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

        cover_record: Mapping[str, Any] | None = None
        imported_cover_id = None
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
            cover_record = candidate
            imported_cover_id = _managed_import_id(
                workflow_id,
                "edit_cover",
                expected_cover_id,
                cover_sha256,
            )

        try:
            overrides = self._upload_overrides(
                expected_upload.get("target_overrides"),
                account_ids,
                binding_platforms,
                cover_record,
                imported_cover_id,
            )
            request_jobs = service.claim_workflow_request_for_cancellation(
                request_key,
                expected_request={
                    "source_id": source_id,
                    "account_ids": account_ids,
                    "title": expected_upload.get("title"),
                    "description": expected_upload.get("description"),
                    "tags": expected_upload.get("tags"),
                    "category_id": expected_upload.get("category_id"),
                    "mode": expected_upload.get("mode"),
                    "copyright": expected_upload.get("copyright"),
                    "source_credit": expected_upload.get("source_credit"),
                    "target_overrides": overrides,
                },
            )
        except WorkflowError:
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
                if (
                    job_id in discovered_ids
                    or job_id in combined_ids
                    or job.get("source_id") != source_id
                    or job.get("account_id") != account_id
                    or job.get("platform") != binding_platforms[account_id]
                ):
                    return CancellationSnapshot("attention", code="upload_request_mismatch")
                discovered_ids.add(job_id)
                combined_ids.append(job_id)
                combined_targets.append(
                    {
                        "job_id": job_id,
                        "source_id": source_id,
                        "account_id": account_id,
                        "platform": binding_platforms[account_id],
                    }
                )
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
                slot = LocalWorkflowAdapter._cover_slot(
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

    @staticmethod
    def _cover_slot(platform: str, width: int, height: int) -> str | None:
        if platform == "bilibili":
            return "cover_landscape_asset_id" if width >= height else None
        if platform == "douyin":
            return (
                "cover_landscape_asset_id"
                if width >= height
                else "cover_portrait_asset_id"
            )
        if platform == "tencent":
            ratio = width / height
            if abs(ratio - 4 / 3) <= 0.04:
                return "cover_landscape_asset_id"
            if abs(ratio - 3 / 4) <= 0.04:
                return "cover_portrait_asset_id"
        return None


__all__ = ["LocalWorkflowAdapter"]

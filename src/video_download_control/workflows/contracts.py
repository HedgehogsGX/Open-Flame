"""Small interfaces between the workflow module and the three media domains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence


StepStatus = Literal["waiting", "ready", "failed", "attention"]
UploadOutcome = Literal["submitted", "draft_saved", "mixed"]
CancellationStatus = Literal[
    "stopped", "waiting", "attention", "upload_completed"
]
MAX_WORKFLOW_SEGMENTS = 10
MAX_WORKFLOW_ACCOUNTS = 3
MAX_WORKFLOW_UPLOAD_JOBS = MAX_WORKFLOW_SEGMENTS * MAX_WORKFLOW_ACCOUNTS


class WorkflowError(ValueError):
    """A stable workflow-domain error code shared across public boundaries."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def workflow_outputs_match_state(
    state: object,
    outputs: Sequence[Mapping[str, Any]],
    expected_count: int,
) -> bool:
    """Return whether a canonical fan-out shape is reachable in ``state``."""

    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or not 1 <= expected_count <= MAX_WORKFLOW_SEGMENTS
        or not isinstance(outputs, Sequence)
        or isinstance(outputs, (str, bytes))
        or len(outputs) not in {0, expected_count}
    ):
        return False
    prepared_count = sum(
        isinstance(output, Mapping) and output.get("upload_source_id") is not None
        for output in outputs
    )
    if state in {
        "created",
        "downloading",
        "preparing_edit",
        "awaiting_ai_review",
    }:
        return not outputs
    if state in {"awaiting_edit_confirmation", "rendering"}:
        return not outputs or prepared_count == 0
    if state == "preparing_upload":
        return len(outputs) == expected_count
    if state in {"awaiting_upload_confirmation", "uploading", "completed"}:
        return len(outputs) == expected_count and prepared_count == expected_count
    if state in {"attention_required", "canceled"}:
        return True
    return False


@dataclass(frozen=True, slots=True)
class DownloadSnapshot:
    status: StepStatus
    asset_id: str | None = None
    code: str = ""
    source_title: str | None = None


@dataclass(frozen=True, slots=True)
class EditPrepared:
    project_id: str
    draft_version: int
    plan_id: str | None
    awaiting_ai_review: bool = False


@dataclass(frozen=True, slots=True)
class AiSnapshot:
    status: StepStatus | Literal["review"]
    draft_version: int | None = None
    plan_id: str | None = None
    code: str = ""


@dataclass(frozen=True, slots=True)
class EditSnapshot:
    status: StepStatus
    output_id: str | None = None
    cover_id: str | None = None
    code: str = ""
    # Ordered by the recipe segment ordinal.  ``output_id`` remains populated
    # only for the historical single-output shape so older workflow stores fail
    # closed instead of silently selecting the first item from a multi-output
    # render.
    output_ids: tuple[str, ...] = ()
    # True only while the current plan is still a local review that may be
    # confirmed. Older adapters default to the conservative confirmation gate.
    needs_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class UploadPrepared:
    source_id: str
    cover_id: str | None
    job_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UploadSnapshot:
    status: StepStatus
    code: str = ""
    job_ids: tuple[str, ...] | None = None
    outcome: UploadOutcome | None = None
    # True when at least one current leaf is a local draft that still needs the
    # domain confirmation gate. Fail closed for adapters that have not yet
    # classified a waiting upload.
    needs_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class CancellationSnapshot:
    """Conservative result of stopping one workflow-owned domain operation."""

    status: CancellationStatus
    code: str = ""
    outcome: UploadOutcome | None = None


class WorkflowDomainAdapter(Protocol):
    """The complete cross-domain seam consumed by the workflow state machine."""

    def preflight(
        self,
        recipe: Mapping[str, Any],
        ai: Mapping[str, Any] | None,
        upload: Mapping[str, Any],
        *,
        cover_aspect_ratio: str | None,
        expected_account_bindings: Sequence[Mapping[str, str]] | None = None,
    ) -> Sequence[Mapping[str, str]]: ...

    def create_download(
        self, workflow_id: str, source_url: str, credential_mode: str
    ) -> str: ...

    def inspect_download(self, batch_id: str) -> DownloadSnapshot: ...

    def resolve_upload(
        self,
        upload: Mapping[str, Any],
        download: DownloadSnapshot,
    ) -> Mapping[str, Any]: ...

    def cancel_download(
        self,
        batch_id: str,
        *,
        expected_name: str,
        expected_source_url: str,
    ) -> CancellationSnapshot: ...

    def cancel_download_for_workflow(
        self,
        workflow_id: str,
        *,
        expected_name: str,
        expected_source_url: str,
    ) -> CancellationSnapshot: ...

    def prepare_edit(
        self,
        workflow_id: str,
        asset_id: str,
        recipe: Mapping[str, Any],
    ) -> EditPrepared: ...

    def advance_ai(
        self,
        workflow_id: str,
        project_id: str,
        recipe: Mapping[str, Any],
        ai: Mapping[str, Any],
        *,
        authorize: bool,
        explicit: bool,
    ) -> AiSnapshot: ...

    def retry_ai(
        self,
        workflow_id: str,
        project_id: str,
        recipe: Mapping[str, Any],
        ai: Mapping[str, Any],
    ) -> None: ...

    def cancel_ai(
        self,
        project_id: str,
        *,
        expected_name: str,
        expected_source_asset_id: str,
    ) -> CancellationSnapshot: ...

    def cancel_edit_for_workflow(
        self,
        workflow_id: str,
        *,
        expected_project_id: str | None,
        expected_name: str,
        expected_source_asset_id: str,
        expected_recipe: Mapping[str, Any],
    ) -> CancellationSnapshot: ...

    def retry_edit(self, workflow_id: str, plan_id: str) -> str: ...

    def inspect_edit(self, plan_id: str) -> EditSnapshot: ...

    def confirm_edit(self, plan_id: str) -> None: ...

    def cancel_edit(
        self,
        plan_id: str,
        *,
        expected_project_id: str,
        expected_name: str,
        expected_source_asset_id: str,
    ) -> CancellationSnapshot: ...

    def prepare_upload(
        self,
        workflow_id: str,
        output_id: str,
        cover_id: str | None,
        upload: Mapping[str, Any],
        *,
        segment_ordinal: int = 1,
    ) -> UploadPrepared: ...

    def inspect_upload(
        self,
        job_ids: Sequence[str],
        expected_targets: Sequence[Mapping[str, str]] | None = None,
    ) -> UploadSnapshot: ...

    def confirm_uploads(
        self,
        job_ids: Sequence[str],
        account_bindings: Sequence[Mapping[str, str]],
    ) -> None: ...

    def cancel_uploads(
        self,
        job_ids: Sequence[str],
        *,
        expected_targets: Sequence[Mapping[str, str]],
        expected_account_bindings: Sequence[Mapping[str, str]],
    ) -> CancellationSnapshot: ...

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
    ) -> CancellationSnapshot: ...

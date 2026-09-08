"""Small interfaces between the workflow module and the three media domains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence


StepStatus = Literal["waiting", "ready", "failed", "attention"]


@dataclass(frozen=True, slots=True)
class DownloadSnapshot:
    status: StepStatus
    asset_id: str | None = None
    code: str = ""


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


class WorkflowDomainAdapter(Protocol):
    """The complete cross-domain seam consumed by the workflow state machine."""

    def create_download(
        self, workflow_id: str, source_url: str, credential_mode: str
    ) -> str: ...

    def inspect_download(self, batch_id: str) -> DownloadSnapshot: ...

    def validate_upload(
        self,
        upload: Mapping[str, Any],
        *,
        cover_aspect_ratio: str | None,
    ) -> Sequence[Mapping[str, str]]: ...

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

    def retry_edit(self, workflow_id: str, plan_id: str) -> str: ...

    def inspect_edit(self, plan_id: str) -> EditSnapshot: ...

    def confirm_edit(self, plan_id: str) -> None: ...

    def prepare_upload(
        self,
        workflow_id: str,
        output_id: str,
        cover_id: str | None,
        upload: Mapping[str, Any],
    ) -> UploadPrepared: ...

    def inspect_upload(self, job_ids: Sequence[str]) -> UploadSnapshot: ...

    def confirm_uploads(
        self,
        job_ids: Sequence[str],
        account_bindings: Sequence[Mapping[str, str]],
    ) -> None: ...

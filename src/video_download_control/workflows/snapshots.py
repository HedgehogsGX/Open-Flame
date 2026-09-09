"""Pure classifications for facts returned by workflow domain adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import AiSnapshot, EditSnapshot, UploadSnapshot


AiSnapshotDisposition = Literal["waiting", "ready", "attention", "invalid"]
EditSnapshotDisposition = Literal[
    "waiting_confirmation",
    "waiting_active",
    "ready",
    "failed",
    "attention",
    "invalid",
]
UploadSnapshotDisposition = Literal[
    "waiting_confirmation",
    "waiting_active",
    "ready",
    "attention",
    "invalid",
]


@dataclass(frozen=True, slots=True)
class AiSnapshotClassification:
    disposition: AiSnapshotDisposition
    code: str = ""


@dataclass(frozen=True, slots=True)
class EditSnapshotClassification:
    disposition: EditSnapshotDisposition
    code: str = ""


@dataclass(frozen=True, slots=True)
class UploadSnapshotClassification:
    disposition: UploadSnapshotDisposition
    code: str = ""


def classify_ai_snapshot(snapshot: AiSnapshot) -> AiSnapshotClassification:
    """Classify an AI observation without granting permission to act on it."""

    if not isinstance(snapshot.status, str) or not isinstance(snapshot.code, str):
        return AiSnapshotClassification("invalid")
    if snapshot.status in {"waiting", "review"}:
        return AiSnapshotClassification("waiting", snapshot.code)
    if snapshot.status in {"failed", "attention"}:
        return AiSnapshotClassification(
            "attention", snapshot.code or "ai_review_required"
        )
    if snapshot.status == "ready":
        return AiSnapshotClassification("ready")
    return AiSnapshotClassification("invalid")


def classify_edit_snapshot(snapshot: EditSnapshot) -> EditSnapshotClassification:
    """Classify an editing observation without granting permission to confirm."""

    if (
        not isinstance(snapshot.status, str)
        or not isinstance(snapshot.code, str)
        or type(snapshot.needs_confirmation) is not bool
    ):
        return EditSnapshotClassification("invalid")
    if snapshot.status == "failed":
        return EditSnapshotClassification(
            "failed", snapshot.code or "edit_attention_required"
        )
    if snapshot.status == "attention":
        return EditSnapshotClassification(
            "attention", snapshot.code or "edit_attention_required"
        )
    if snapshot.status == "ready":
        return EditSnapshotClassification("ready", snapshot.code)
    if snapshot.status == "waiting":
        return EditSnapshotClassification(
            (
                "waiting_confirmation"
                if snapshot.needs_confirmation
                else "waiting_active"
            ),
            snapshot.code,
        )
    return EditSnapshotClassification("invalid")


def classify_upload_snapshot(
    snapshot: UploadSnapshot,
) -> UploadSnapshotClassification:
    """Classify an upload observation without granting permission to confirm."""

    if (
        not isinstance(snapshot.status, str)
        or not isinstance(snapshot.code, str)
        or type(snapshot.needs_confirmation) is not bool
    ):
        return UploadSnapshotClassification("invalid")
    if snapshot.status in {"failed", "attention"}:
        return UploadSnapshotClassification(
            "attention", snapshot.code or "upload_attention_required"
        )
    if snapshot.status == "ready":
        return UploadSnapshotClassification("ready")
    if snapshot.status == "waiting":
        return UploadSnapshotClassification(
            (
                "waiting_confirmation"
                if snapshot.needs_confirmation
                else "waiting_active"
            ),
            snapshot.code,
        )
    return UploadSnapshotClassification("invalid")


__all__ = [
    "AiSnapshotClassification",
    "AiSnapshotDisposition",
    "EditSnapshotClassification",
    "EditSnapshotDisposition",
    "UploadSnapshotClassification",
    "UploadSnapshotDisposition",
    "classify_ai_snapshot",
    "classify_edit_snapshot",
    "classify_upload_snapshot",
]

"""Pure classifications for facts returned by workflow domain adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import AiSnapshot, UploadSnapshot


AiSnapshotDisposition = Literal["waiting", "ready", "attention", "invalid"]
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
    "UploadSnapshotClassification",
    "UploadSnapshotDisposition",
    "classify_ai_snapshot",
    "classify_upload_snapshot",
]

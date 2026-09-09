"""Pure classifications for facts returned by workflow domain adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import AiSnapshot


AiSnapshotDisposition = Literal["waiting", "ready", "attention", "invalid"]


@dataclass(frozen=True, slots=True)
class AiSnapshotClassification:
    disposition: AiSnapshotDisposition
    code: str = ""


def classify_ai_snapshot(snapshot: AiSnapshot) -> AiSnapshotClassification:
    """Classify an AI observation without granting permission to act on it."""

    if snapshot.status in {"waiting", "review"}:
        return AiSnapshotClassification("waiting", snapshot.code or "")
    if snapshot.status in {"failed", "attention"}:
        return AiSnapshotClassification(
            "attention", snapshot.code or "ai_review_required"
        )
    if snapshot.status == "ready":
        return AiSnapshotClassification("ready")
    return AiSnapshotClassification("invalid")


__all__ = [
    "AiSnapshotClassification",
    "AiSnapshotDisposition",
    "classify_ai_snapshot",
]

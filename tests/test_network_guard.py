from __future__ import annotations

from pathlib import Path

import pytest

from video_download_control.adapters import AdapterNetworkMode, ScriptedFakeAdapter
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.worker import Worker


class ControlledAdapter(ScriptedFakeAdapter):
    network_mode = AdapterNetworkMode.CONTROLLED_EGRESS


class DirectAdapter(ScriptedFakeAdapter):
    network_mode = AdapterNetworkMode.DIRECT_EGRESS


class NeverClaimRepository:
    claimed = False

    def claim_next(self, **kwargs):
        self.claimed = True
        raise AssertionError("queue must not be touched when guard is unavailable")


class FailingGuard:
    def assert_ready(self, *, adapter_name: str) -> None:
        raise RuntimeError(f"controlled egress unavailable for {adapter_name}")


def worker_args(tmp_path: Path) -> dict:
    return {
        "worker_id": "guard-test-worker",
        "repository": NeverClaimRepository(),
        "adapter": ControlledAdapter(),
        "asset_store": AssetStore(tmp_path / "data", min_free_bytes=0),
        "verifier": NonEmptyTestVerifier(),
    }


def test_networked_adapter_without_guard_fails_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="network guard"):
        Worker(**worker_args(tmp_path))


def test_failed_guard_is_checked_before_queue_claim(tmp_path: Path) -> None:
    arguments = worker_args(tmp_path)
    repository = arguments["repository"]
    worker = Worker(**arguments, network_guard=FailingGuard())
    with pytest.raises(RuntimeError, match="controlled egress unavailable"):
        worker.run_once()
    assert repository.claimed is False


def test_explicit_direct_adapter_still_requires_an_execution_guard(
    tmp_path: Path,
) -> None:
    arguments = worker_args(tmp_path)
    arguments["adapter"] = DirectAdapter()

    with pytest.raises(ValueError, match="network guard"):
        Worker(**arguments)

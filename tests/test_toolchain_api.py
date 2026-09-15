from __future__ import annotations

from local_http_client import download_client

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import video_download_control.api as api_module
from video_download_control.api import create_app
from video_download_control.config import Settings


class _Inspection:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return dict(self._payload)


def _payload(*, state: str) -> dict[str, object]:
    ready = state == "ready"
    return {
        "state": state,
        "detail_code": "ok" if ready else f"toolchain_{state}",
        "yt_dlp_version": "2026.08.19" if ready else None,
        "ffmpeg_version": "N-126390-g9fc8c785e2-20260902" if ready else None,
        "ffprobe_version": "N-126390-g9fc8c785e2-20260902" if ready else None,
        "offline_smoke_passed": ready,
        "isolated_worker_ready": ready,
        "platform_download_verified": ready,
        "redistribution_status": "local_private_only",
        "network_download_enabled": ready,
    }


def test_toolchain_endpoint_integrates_with_the_real_unconfigured_inspector(
    settings: Settings,
) -> None:
    with download_client(create_app(settings)) as client:
        response = client.get("/api/v1/operations/tools")

    payload = response.json()
    assert response.status_code == 200
    assert payload["state"] == "unconfigured"
    assert payload["detail_code"] == "tool_root_unconfigured"
    assert payload["yt_dlp_version"] is None
    assert payload["ffmpeg_version"] is None
    assert payload["ffprobe_version"] is None
    assert payload["offline_smoke_passed"] is False
    assert payload["isolated_worker_ready"] is False
    assert payload["platform_download_verified"] is False
    assert payload["network_download_enabled"] is False


def test_unconfigured_toolchain_is_visible_without_changing_worker_health(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_roots: list[Path | None] = []

    def inspect(tool_root: Path | None) -> _Inspection:
        observed_roots.append(tool_root)
        return _Inspection(_payload(state="unconfigured"))

    monkeypatch.setattr(api_module, "inspect_toolchain", inspect)

    with download_client(create_app(settings)) as client:
        health = client.get("/health")
        response = client.get("/api/v1/operations/tools")

    assert health.status_code == 200
    assert health.json()["worker"] == "external_status_unknown"
    assert observed_roots == [None]
    assert response.status_code == 200
    assert response.json() == {
        **_payload(state="unconfigured"),
        "isolated_worker_ready": False,
        "platform_download_verified": False,
        "network_download_enabled": False,
        "local_direct_worker_available": False,
        "security_note": (
            "本机工具链 ready 只表示固定工具通过离线检查；本机托管 Worker 的"
            "运行状态另由应用心跳报告，外部 Worker 状态保持未知；隔离运行环境与平台级能力"
            "仍未验证。redistribution_status 只描述本机第三方工具包，不描述项目源码"
            "的 Apache-2.0 许可状态。"
        ),
    }


def test_ready_local_tools_never_claim_an_isolated_or_verified_worker(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_root = (tmp_path / "tools-private-marker").resolve()
    configured = Settings(
        data_root=settings.data_root,
        database_path=settings.database_path,
        tool_root=tool_root,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
    )

    def inspect(observed: Path | None) -> _Inspection:
        assert observed == tool_root
        return _Inspection(_payload(state="ready"))

    monkeypatch.setattr(api_module, "inspect_toolchain", inspect)

    with download_client(create_app(configured)) as client:
        response = client.get("/api/v1/operations/tools")

    payload = response.json()
    assert response.status_code == 200
    assert payload["state"] == "ready"
    assert payload["offline_smoke_passed"] is True
    assert payload["isolated_worker_ready"] is False
    assert payload["platform_download_verified"] is False
    assert payload["network_download_enabled"] is False
    assert payload["local_direct_worker_available"] is (os.name == "nt")
    assert payload["yt_dlp_version"] == "2026.08.19"
    assert str(tool_root) not in response.text
    assert "运行状态另由应用心跳报告" in payload["security_note"]
    assert "外部 Worker 状态保持未知" in payload["security_note"]
    assert "只描述本机第三方工具包" in payload["security_note"]


def test_invalid_toolchain_returns_a_bounded_detail_code(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_root = (tmp_path / "invalid-tools-private-marker").resolve()
    configured = Settings(
        data_root=settings.data_root,
        database_path=settings.database_path,
        tool_root=tool_root,
        max_batch_urls=settings.max_batch_urls,
        route_policy_version=settings.route_policy_version,
    )
    monkeypatch.setattr(
        api_module,
        "inspect_toolchain",
        lambda observed: _Inspection(_payload(state="invalid")),
    )

    with download_client(create_app(configured)) as client:
        response = client.get("/api/v1/operations/tools")

    payload = response.json()
    assert response.status_code == 200
    assert payload["state"] == "invalid"
    assert payload["detail_code"] == "toolchain_invalid"
    assert payload["offline_smoke_passed"] is False
    assert payload["isolated_worker_ready"] is False
    assert payload["platform_download_verified"] is False
    assert str(tool_root) not in response.text

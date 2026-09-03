from __future__ import annotations

import os
from pathlib import Path

import pytest

from video_download_control.adapters import AdapterNetworkMode
from video_download_control.database import Database
from video_download_control.local_worker_cli import (
    FEATURE_GATE,
    LocalDirectNetworkGuard,
    LocalWorkerConfig,
    build_local_worker,
    build_parser,
    main,
)
import video_download_control.local_worker_cli as local_worker_module
from video_download_control.runtime_logging import read_recent_runtime_events


def cli_arguments(tmp_path: Path, *, acknowledge: bool = True) -> list[str]:
    arguments = [
        "--data-root",
        str((tmp_path / "data").resolve()),
        "--tool-root",
        str((tmp_path / "tools").resolve()),
    ]
    if acknowledge:
        arguments.append("--allow-direct-network")
    return arguments


def test_local_worker_requires_exact_feature_gate_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FEATURE_GATE, raising=False)

    with pytest.raises(SystemExit, match="disabled"):
        main(cli_arguments(tmp_path))


def test_local_worker_requires_command_line_direct_network_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FEATURE_GATE, "1")

    with pytest.raises(SystemExit, match="allow-direct-network"):
        main(cli_arguments(tmp_path, acknowledge=False))


def test_local_direct_guard_rechecks_feature_gate_each_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FEATURE_GATE, "1")
    guard = LocalDirectNetworkGuard(acknowledged=True)
    guard.assert_ready(adapter_name="yt_dlp")

    monkeypatch.setenv(FEATURE_GATE, "0")
    with pytest.raises(RuntimeError, match="disabled"):
        guard.assert_ready(adapter_name="yt_dlp")


def test_local_direct_guard_rejects_non_windows_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FEATURE_GATE, "1")
    monkeypatch.setattr(local_worker_module.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="Windows"):
        LocalDirectNetworkGuard(acknowledged=True).assert_ready(
            adapter_name="yt_dlp"
        )


def test_imported_builder_rechecks_gate_before_paths_or_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FEATURE_GATE, raising=False)
    tool_called = False

    def unexpected_tool_call(*_args, **_kwargs):
        nonlocal tool_called
        tool_called = True
        raise AssertionError("tool verification must not run while disabled")

    monkeypatch.setattr(local_worker_module, "verify_toolchain", unexpected_tool_call)
    config = LocalWorkerConfig(
        data_root=(tmp_path / "missing-data").resolve(),
        database_path=(tmp_path / "missing-data" / "control.sqlite3").resolve(),
        tool_root=(tmp_path / "missing-tools").resolve(),
        worker_id="local-test-worker",
        allow_direct_network=True,
    )

    with pytest.raises(RuntimeError, match="disabled"):
        build_local_worker(config)

    assert tool_called is False
    assert not config.data_root.exists()


def test_config_defaults_database_below_explicit_data_root(tmp_path: Path) -> None:
    arguments = build_parser().parse_args(cli_arguments(tmp_path))

    config = LocalWorkerConfig.from_args(arguments)

    assert config.data_root == (tmp_path / "data").resolve()
    assert config.database_path == config.data_root / "control.sqlite3"
    assert config.tool_root == (tmp_path / "tools").resolve()
    assert config.allow_direct_network is True


def test_build_local_worker_uses_shared_database_and_explicit_direct_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FEATURE_GATE, "1")
    data_root = (tmp_path / "data").resolve()
    data_root.mkdir()
    database_path = data_root / "control.sqlite3"
    Database(database_path).initialize()
    tool_root = (tmp_path / "tools").resolve()
    entrypoint = tool_root / "yt-dlp" / "yt-dlp"
    ffmpeg_root = tool_root / "ffmpeg" / "bin"
    entrypoint.parent.mkdir(parents=True)
    ffmpeg_root.mkdir(parents=True)
    entrypoint.write_bytes(b"yt-dlp-test-double")
    (ffmpeg_root / "ffmpeg.exe").write_bytes(b"ffmpeg-test-double")
    (ffmpeg_root / "ffprobe.exe").write_bytes(b"ffprobe-test-double")

    class Verified:
        yt_dlp_version = "2026.08.19"
        ffmpeg_version = "n9-test"
        ffprobe_version = "n9-test"
        offline_smoke_passed = True

    class YtLock:
        entrypoint = "yt-dlp/yt-dlp"

    class Lock:
        yt_dlp = YtLock()

    class Runner:
        def run(self, spec, *, is_cancelled=None):  # pragma: no cover - not invoked
            raise AssertionError("startup should use the injected verifier doubles")

    class Verifier:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def validate_runtime(self) -> None:
            return None

    monkeypatch.setattr(local_worker_module, "verify_toolchain", lambda *_a, **_k: Verified())
    monkeypatch.setattr(local_worker_module, "load_toolchain_lock", lambda: Lock())
    monkeypatch.setattr(local_worker_module, "FfprobeVerifier", Verifier)
    config = LocalWorkerConfig(
        data_root=data_root,
        database_path=database_path,
        tool_root=tool_root,
        worker_id="local-test-worker",
        allow_direct_network=True,
    )

    runtime_logger = local_worker_module._runtime_logger(config)
    worker = build_local_worker(  # type: ignore[arg-type]
        config,
        runner=Runner(),
        runtime_logger=runtime_logger,
    )

    assert worker.network_mode is AdapterNetworkMode.DIRECT_EGRESS
    assert worker.asset_store.data_root == data_root
    assert worker.repository.database.path == database_path
    assert worker.skip_unsupported_graph_jobs is True
    assert [
        event["event"]
        for event in read_recent_runtime_events(data_root / "logs")
    ] == ["toolchain.inspected"]


def test_main_runs_one_idle_cycle_with_explicit_local_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = (tmp_path / "data").resolve()
    database_path = data_root / "control.sqlite3"
    Database(database_path).initialize()
    monkeypatch.setenv(FEATURE_GATE, "1")
    seen: list[LocalWorkerConfig] = []

    class IdleWorker:
        def run_once(self):
            return None

    def fake_build(config: LocalWorkerConfig, **_kwargs):
        seen.append(config)
        return IdleWorker()

    monkeypatch.setattr(local_worker_module, "build_local_worker", fake_build)

    main(cli_arguments(tmp_path))

    assert capsys.readouterr().out.strip() == '{"status": "idle"}'
    assert len(seen) == 1
    assert seen[0].database_path == database_path
    events = read_recent_runtime_events(data_root / "logs")
    assert [event["event"] for event in events] == [
        "worker.initializing",
        "worker.started",
        "worker.stopped",
    ]
    assert {event["component"] for event in events} == {"local-worker"}


def test_singleton_lock_rejects_second_process_scope_and_allows_stale_reuse(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    data_root.mkdir()

    with local_worker_module._exclusive_local_worker(data_root):
        with pytest.raises(RuntimeError, match="already using"):
            with local_worker_module._exclusive_local_worker(data_root):
                raise AssertionError("a second lock must never be acquired")

    lock_path = data_root / "logs" / ".local-worker.lock"
    assert lock_path.is_file()
    with local_worker_module._exclusive_local_worker(data_root):
        pass


def test_control_database_hardlink_is_rejected(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    original = data_root / "control.sqlite3"
    Database(original).initialize()
    alias = data_root / "alias.sqlite3"
    os.link(original, alias)
    config = LocalWorkerConfig(
        data_root=data_root,
        database_path=alias,
        tool_root=(tmp_path / "tools").resolve(),
        worker_id="local-test-worker",
        allow_direct_network=True,
    )

    with pytest.raises(RuntimeError, match="plain file"):
        local_worker_module._validate_control_paths(config)


def test_unavailable_initial_log_prevents_builder_and_queue_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = (tmp_path / "data").resolve()
    Database(data_root / "control.sqlite3").initialize()
    monkeypatch.setenv(FEATURE_GATE, "1")
    builder_called = False

    class UnavailableLogger:
        def emit(self, *_args, **_kwargs) -> bool:
            return False

    def unexpected_build(*_args, **_kwargs):
        nonlocal builder_called
        builder_called = True
        raise AssertionError("builder must not run without the initial log")

    monkeypatch.setattr(local_worker_module, "_runtime_logger", lambda _config: UnavailableLogger())
    monkeypatch.setattr(local_worker_module, "build_local_worker", unexpected_build)

    with pytest.raises(SystemExit, match="log is unavailable.*no job was claimed"):
        main(cli_arguments(tmp_path))

    assert builder_called is False


def test_singleton_open_error_is_bounded_before_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = (tmp_path / "data").resolve()
    Database(data_root / "control.sqlite3").initialize()
    monkeypatch.setenv(FEATURE_GATE, "1")
    sentinel = "SENSITIVE-LOCK-PATH"
    builder_called = False

    def denied_open(*_args, **_kwargs):
        raise PermissionError(f"denied at {sentinel}")

    def unexpected_build(*_args, **_kwargs):
        nonlocal builder_called
        builder_called = True

    monkeypatch.setattr(local_worker_module.os, "open", denied_open)
    monkeypatch.setattr(local_worker_module, "build_local_worker", unexpected_build)

    with pytest.raises(SystemExit) as raised:
        main(cli_arguments(tmp_path))

    message = str(raised.value)
    assert message == "local Worker singleton lock is unavailable"
    assert sentinel not in message
    assert builder_called is False


def test_builder_revalidates_js_runtime_before_tool_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = (tmp_path / "data").resolve()
    database_path = data_root / "control.sqlite3"
    Database(database_path).initialize()
    runtime_path = (tmp_path / "node.exe").resolve()
    runtime_path.write_bytes(b"node-test-double")
    js_runtime = local_worker_module._js_runtime(f"node:{runtime_path}")
    runtime_path.unlink()
    monkeypatch.setenv(FEATURE_GATE, "1")
    tool_called = False

    def unexpected_tool_call(*_args, **_kwargs):
        nonlocal tool_called
        tool_called = True
        raise AssertionError("tool inspection must not run after runtime replacement")

    monkeypatch.setattr(local_worker_module, "load_toolchain_lock", unexpected_tool_call)
    config = LocalWorkerConfig(
        data_root=data_root,
        database_path=database_path,
        tool_root=(tmp_path / "tools").resolve(),
        worker_id="local-test-worker",
        allow_direct_network=True,
        js_runtime=js_runtime,
    )

    with pytest.raises(RuntimeError, match="JavaScript runtime is unavailable"):
        build_local_worker(
            config,
            runtime_logger=local_worker_module._runtime_logger(config),
        )

    assert tool_called is False

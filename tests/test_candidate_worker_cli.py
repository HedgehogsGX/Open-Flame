from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

import video_download_control.candidate_worker_cli as cli_module
from video_download_control.adapters import YtDlpJsRuntime, YtDlpJsRuntimeName
from video_download_control.adapters.base import ProbeRequest
from video_download_control.candidate_cookies import (
    AttemptCookieResolver,
    CookiePreparationError,
    CookieSource,
)
from video_download_control.candidate_worker_cli import (
    CREDENTIAL_PROFILE_WIRING_STATUS,
    CandidateStartupError,
    CandidateWorkerConfig,
    build_candidate_worker,
    main,
)
from video_download_control.database import Database
from video_download_control.domain import Platform, SourceType
from video_download_control.repository import BatchRepository
from video_download_control.runtime_logging import read_recent_runtime_events
from video_download_control.security import NetworkIsolationError
from video_download_control.service import BatchService
from video_download_control.subprocess_runner import CommandResult, CommandSpec


@pytest.fixture(autouse=True)
def enable_candidate_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VDC_ENABLE_CANDIDATE_REAL_WORKER", "1")
    monkeypatch.setenv("VDC_RUNTIME_LOG_LEVEL", "INFO")
    monkeypatch.setenv("VDC_RUNTIME_LOG_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setenv("VDC_RUNTIME_LOG_BACKUP_COUNT", "2")


class FakeVersionRunner:
    def __init__(
        self,
        *,
        yt_dlp_version: str = "2026.08.19",
        ffmpeg_version: str = "7.1.1",
        ffprobe_version: str = "7.1.1",
    ) -> None:
        self.yt_dlp_version = yt_dlp_version
        self.ffmpeg_version = ffmpeg_version
        self.ffprobe_version = ffprobe_version
        self.calls: list[CommandSpec] = []

    def run(self, spec: CommandSpec, *, is_cancelled=None) -> CommandResult:
        del is_cancelled
        self.calls.append(spec)
        uses_zipimport_entrypoint = bool(
            spec.arguments and Path(spec.arguments[0]).name.startswith("yt-dlp")
        )
        if spec.executable.name.startswith("yt-dlp") or uses_zipimport_entrypoint:
            stdout = self.yt_dlp_version.encode("utf-8") + b"\n"
        elif spec.executable.name.startswith("ffmpeg"):
            stdout = f"ffmpeg version {self.ffmpeg_version}\n".encode()
        else:
            stdout = f"ffprobe version {self.ffprobe_version}\n".encode()
        return CommandResult(0, stdout, b"", 0.01)


class RecordingGuard:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    def assert_ready(self, *, adapter_name: str) -> None:
        self.calls.append(adapter_name)
        if self.fail:
            raise NetworkIsolationError("controlled namespace is unavailable")


def make_config(tmp_path: Path) -> CandidateWorkerConfig:
    data_root = (tmp_path / "data").resolve()
    data_root.mkdir()
    tools = (tmp_path / "tools").resolve()
    tools.mkdir()
    yt_dlp = tools / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    yt_dlp.write_bytes(b"offline-test-double")
    ffmpeg = tools / "ffmpeg"
    ffmpeg.mkdir()
    ffmpeg_executable = ffmpeg / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    ffmpeg_executable.write_bytes(b"offline-test-double")
    ffprobe = ffmpeg / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    ffprobe.write_bytes(b"offline-test-double")
    return CandidateWorkerConfig(
        data_root=data_root,
        database_path=data_root / "control.sqlite3",
        yt_dlp_executable=yt_dlp,
        ffmpeg_directory=ffmpeg,
        ffmpeg_executable=ffmpeg_executable,
        ffprobe_executable=ffprobe,
        unix_socket_path=(tmp_path / "proxy.sock").resolve(),
        yt_dlp_version="2026.08.19",
        ffmpeg_version="7.1.1",
        ffprobe_version="7.1.1",
        egress_policy_version="egress-v1",
        relay_port=18080,
        max_height=1080,
        max_file_bytes=1024 * 1024,
        storage_min_free_bytes=0,
        socket_timeout_seconds=20,
        probe_timeout_seconds=60,
        download_timeout_seconds=300,
        ffprobe_timeout_seconds=30,
        attempt_timeout_seconds=600,
        max_items_per_source=20,
        max_cookie_bytes=1024 * 1024,
        worker_id="candidate-worker-test",
    )


def argv_for(config: CandidateWorkerConfig) -> list[str]:
    arguments = [
        "--data-root",
        str(config.data_root),
        "--database-path",
        str(config.database_path),
        "--yt-dlp-executable",
        str(config.yt_dlp_executable),
        "--ffmpeg-directory",
        str(config.ffmpeg_directory),
        "--ffmpeg-executable",
        str(config.ffmpeg_executable),
        "--ffprobe-executable",
        str(config.ffprobe_executable),
        "--unix-socket-path",
        str(config.unix_socket_path),
        "--yt-dlp-version",
        config.yt_dlp_version,
        "--ffmpeg-version",
        config.ffmpeg_version,
        "--ffprobe-version",
        config.ffprobe_version,
        "--egress-policy-version",
        config.egress_policy_version,
        "--relay-port",
        str(config.relay_port),
        "--max-height",
        str(config.max_height),
        "--max-file-bytes",
        str(config.max_file_bytes),
        "--storage-min-free-bytes",
        str(config.storage_min_free_bytes),
        "--socket-timeout-seconds",
        str(config.socket_timeout_seconds),
        "--probe-timeout-seconds",
        str(config.probe_timeout_seconds),
        "--download-timeout-seconds",
        str(config.download_timeout_seconds),
        "--ffprobe-timeout-seconds",
        str(config.ffprobe_timeout_seconds),
        "--attempt-timeout-seconds",
        str(config.attempt_timeout_seconds),
        "--max-items-per-source",
        str(config.max_items_per_source),
        "--max-cookie-bytes",
        str(config.max_cookie_bytes),
        "--worker-id",
        config.worker_id,
    ]
    if config.yt_dlp_zipimport_entrypoint is not None:
        arguments.extend(
            (
                "--yt-dlp-zipimport-entrypoint",
                str(config.yt_dlp_zipimport_entrypoint),
            )
        )
    if config.js_runtime is not None:
        arguments.extend(
            (
                "--js-runtime",
                f"{config.js_runtime.name.value}:{config.js_runtime.executable}",
            )
        )
    return arguments


def test_build_candidate_worker_preflights_versions_and_stays_idle_offline(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    runner = FakeVersionRunner()
    guard = RecordingGuard()

    worker = build_candidate_worker(config, runner=runner, network_guard=guard)

    assert config.database_path.is_file()
    assert worker.adapter.name == "yt_dlp"
    assert worker.adapter.version == config.yt_dlp_version
    assert worker.verifier.version == config.ffprobe_version
    assert guard.calls == ["yt_dlp"]
    assert [call.executable for call in runner.calls] == [
        config.yt_dlp_executable,
        config.ffmpeg_executable,
        config.ffprobe_executable,
    ]

    assert worker.run_once() is None
    assert guard.calls == ["yt_dlp", "yt_dlp"]
    # Empty queue: no real probe/download process was launched.
    assert len(runner.calls) == 3


def test_build_candidate_worker_supports_python_zipimport_entrypoint(
    tmp_path: Path,
) -> None:
    direct_config = make_config(tmp_path)
    tools = direct_config.yt_dlp_executable.parent
    python_executable = tools / ("python.exe" if sys.platform == "win32" else "python")
    python_executable.write_bytes(b"offline-python-test-double")
    zipimport_entrypoint = tools / "yt-dlp-zipimport"
    zipimport_entrypoint.write_bytes(b"offline-zipimport-test-double")
    config = replace(
        direct_config,
        yt_dlp_executable=python_executable,
        yt_dlp_zipimport_entrypoint=zipimport_entrypoint,
    )
    runner = FakeVersionRunner()
    guard = RecordingGuard()

    parsed = cli_module.build_parser().parse_args(argv_for(config))
    assert cli_module.config_from_args(parsed) == config
    worker = build_candidate_worker(config, runner=runner, network_guard=guard)

    assert worker.adapter.version == config.yt_dlp_version
    assert runner.calls[0].executable == python_executable
    assert runner.calls[0].arguments[:2] == (
        str(zipimport_entrypoint),
        "--ignore-config",
    )
    assert guard.calls == ["yt_dlp"]


def test_missing_zipimport_entrypoint_fails_before_tool_or_database_use(
    tmp_path: Path,
) -> None:
    direct_config = make_config(tmp_path)
    python_executable = direct_config.yt_dlp_executable.parent / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    python_executable.write_bytes(b"offline-python-test-double")
    config = replace(
        direct_config,
        yt_dlp_executable=python_executable,
        yt_dlp_zipimport_entrypoint=(
            direct_config.yt_dlp_executable.parent / "missing-yt-dlp-zipimport"
        ),
    )
    runner = FakeVersionRunner()

    with pytest.raises(CandidateStartupError, match="zipimport entrypoint"):
        build_candidate_worker(
            config,
            runner=runner,
            network_guard=RecordingGuard(),
        )

    assert runner.calls == []
    assert not config.database_path.exists()


def test_candidate_worker_accepts_one_explicit_js_runtime_and_round_trips_cli(
    tmp_path: Path,
) -> None:
    direct_config = make_config(tmp_path)
    node = direct_config.yt_dlp_executable.parent / (
        "node.exe" if sys.platform == "win32" else "node"
    )
    node.write_bytes(b"offline-node-test-double")
    runtime = YtDlpJsRuntime(YtDlpJsRuntimeName.NODE, node)
    config = replace(direct_config, js_runtime=runtime)

    parsed = cli_module.build_parser().parse_args(argv_for(config))
    assert cli_module.config_from_args(parsed) == config
    worker = build_candidate_worker(
        config,
        runner=FakeVersionRunner(),
        network_guard=RecordingGuard(),
    )
    attempt = (tmp_path / "js-runtime-command-attempt").resolve()
    attempt.mkdir()
    command = worker.adapter._factory.probe_command(  # noqa: SLF001
        ProbeRequest(
            job_id="js-runtime-test-job",
            canonical_url="https://www.youtube.com/watch?v=test",
            platform=Platform.YOUTUBE,
            source_type=SourceType.YOUTUBE_VIDEO,
        ),
        temporary_root=attempt,
    )
    option = command.arguments.index("--js-runtimes")
    assert command.arguments[option + 1] == f"node:{node}"


def test_candidate_worker_revalidates_js_runtime_before_tool_or_database_use(
    tmp_path: Path,
) -> None:
    direct_config = make_config(tmp_path)
    node = direct_config.yt_dlp_executable.parent / (
        "node.exe" if sys.platform == "win32" else "node"
    )
    node.write_bytes(b"offline-node-test-double")
    runtime = YtDlpJsRuntime(YtDlpJsRuntimeName.NODE, node)
    config = replace(direct_config, js_runtime=runtime)
    node.unlink()
    runner = FakeVersionRunner()

    with pytest.raises(CandidateStartupError, match="JavaScript runtime"):
        build_candidate_worker(
            config,
            runner=runner,
            network_guard=RecordingGuard(),
        )

    assert runner.calls == []
    assert not config.database_path.exists()


@pytest.mark.parametrize("value", ["python:C:/tools/python.exe", "node=bad"])
def test_candidate_cli_rejects_invalid_js_runtime_shape(
    tmp_path: Path,
    value: str,
) -> None:
    config = make_config(tmp_path)
    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args(
            [*argv_for(config), "--js-runtime", value]
        )


def test_failed_guard_prevents_database_initialization_and_tool_execution(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    runner = FakeVersionRunner()
    guard = RecordingGuard()
    guard.fail = True

    with pytest.raises(NetworkIsolationError, match="namespace"):
        build_candidate_worker(config, runner=runner, network_guard=guard)

    assert not config.database_path.exists()
    assert runner.calls == []


def test_guard_is_rechecked_before_claim_and_leaves_job_queued(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    guard = RecordingGuard()
    worker = build_candidate_worker(
        config,
        runner=FakeVersionRunner(),
        network_guard=guard,
    )
    service = BatchService(
        repository=BatchRepository(Database(config.database_path)),
        max_batch_urls=50,
        route_policy_version="test-v1",
    )
    batch = service.create_batch(
        name="must stay queued",
        raw_inputs=["https://www.youtube.com/watch?v=guard-test"],
    )
    guard.fail = True

    with pytest.raises(NetworkIsolationError):
        worker.run_once()

    assert service.get_batch(batch["id"])["jobs"][0]["status"] == "queued"


def test_tool_version_mismatch_fails_before_database_and_redacts_output(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    secret_output = "wrong-version-credential-secret"

    with pytest.raises(CandidateStartupError) as caught:
        build_candidate_worker(
            config,
            runner=FakeVersionRunner(yt_dlp_version=secret_output),
            network_guard=RecordingGuard(),
        )

    assert not config.database_path.exists()
    assert secret_output not in str(caught.value)


def test_read_only_cookie_mapping_is_validated_without_copying_at_startup(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    source = (tmp_path / "youtube-mounted-cookie.txt").resolve()
    source.write_bytes(b"cookie-source")
    source.chmod(0o444)
    config = replace(
        config,
        cookie_sources=(CookieSource(Platform.YOUTUBE, "youtube-profile", source),),
    )
    try:
        worker = build_candidate_worker(
            config,
            runner=FakeVersionRunner(),
            network_guard=RecordingGuard(),
        )
    finally:
        try:
            source.chmod(0o600)
        except OSError:
            pass

    resolver = worker.adapter._cookie_resolver
    assert isinstance(resolver, AttemptCookieResolver)
    assert resolver.configured_platforms == (Platform.YOUTUBE,)
    assert not (config.data_root / "temporary").exists()


def test_feature_gate_is_required_before_candidate_worker_is_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    called = False

    def forbidden_build(config):
        nonlocal called
        called = True
        raise AssertionError("must not build")

    monkeypatch.delenv("VDC_ENABLE_CANDIDATE_REAL_WORKER", raising=False)
    monkeypatch.setattr(cli_module, "build_candidate_worker", forbidden_build)

    with pytest.raises(SystemExit, match="disabled"):
        main(argv_for(config))

    assert called is False
    assert not config.database_path.exists()


def test_main_redacts_cookie_startup_error_and_discards_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)

    def failing_build(config, *, runtime_logger=None):
        del runtime_logger
        del config
        raise CookiePreparationError("/private/cookie/path-marker")

    monkeypatch.setattr(cli_module, "build_candidate_worker", failing_build)

    with pytest.raises(SystemExit) as caught:
        main(argv_for(config))

    assert str(caught.value) == "candidate Worker credential preparation failed"
    assert "path-marker" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    events = read_recent_runtime_events(config.data_root / "logs")
    assert [event["event"] for event in events] == ["worker.startup_failed"]
    assert events[0]["exception_type"] == "CookiePreparationError"
    assert "path-marker" not in next((config.data_root / "logs").iterdir()).read_text(
        "utf-8"
    )


def test_main_redacts_unexpected_startup_error_and_discards_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    marker = "/private/startup/path-marker"

    def failing_build(config, *, runtime_logger=None):
        del runtime_logger
        del config
        try:
            raise OSError(marker)
        except OSError as exc:
            raise RuntimeError("unexpected startup failure") from exc

    monkeypatch.setattr(cli_module, "build_candidate_worker", failing_build)

    with pytest.raises(SystemExit) as caught:
        main(argv_for(config))

    assert str(caught.value) == "candidate Worker configuration or startup check failed"
    assert "path-marker" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_imported_builder_cannot_bypass_feature_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    runner = FakeVersionRunner()
    monkeypatch.delenv("VDC_ENABLE_CANDIDATE_REAL_WORKER", raising=False)

    with pytest.raises(CandidateStartupError, match="disabled"):
        build_candidate_worker(
            config,
            runner=runner,
            network_guard=RecordingGuard(),
        )

    assert runner.calls == []
    assert not config.database_path.exists()


def test_explicit_gate_can_run_injected_idle_worker_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)

    class IdleWorker:
        def run_once(self):
            return None

    monkeypatch.setenv("VDC_ENABLE_CANDIDATE_REAL_WORKER", "1")
    monkeypatch.setattr(
        cli_module,
        "build_candidate_worker",
        lambda configured, **_kwargs: IdleWorker(),
    )

    main(argv_for(config))

    assert capsys.readouterr().out.strip() == '{"status": "idle"}'
    events = read_recent_runtime_events(config.data_root / "logs")
    assert {event["event"] for event in events} == {
        "worker.started",
        "worker.stopped",
    }
    started = next(event for event in events if event["event"] == "worker.started")
    stopped = next(event for event in events if event["event"] == "worker.stopped")
    assert started["adapter"] == "yt_dlp"
    assert stopped["reason"] == "idle"
    assert not any(
        config.worker_id in path.name for path in (config.data_root / "logs").iterdir()
    )


def test_main_redacts_runtime_failure_and_discards_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    marker = "/private/runtime/socket-path-marker"

    class FailingWorker:
        def run_once(self):
            try:
                raise OSError(marker)
            except OSError as exc:
                raise NetworkIsolationError("network isolation failed") from exc

    monkeypatch.setattr(
        cli_module,
        "build_candidate_worker",
        lambda configured, **_kwargs: FailingWorker(),
    )

    with pytest.raises(SystemExit) as caught:
        main(argv_for(config))

    assert str(caught.value) == "candidate Worker runtime failed safely"
    assert "path-marker" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    events = read_recent_runtime_events(config.data_root / "logs")
    assert {event["event"] for event in events} == {
        "worker.started",
        "worker.cycle_failed",
        "worker.stopped",
    }
    cycle_failed = next(
        event for event in events if event["event"] == "worker.cycle_failed"
    )
    stopped = next(event for event in events if event["event"] == "worker.stopped")
    assert cycle_failed["exception_type"] == "NetworkIsolationError"
    assert stopped["reason"] == "runtime_error"
    assert "path-marker" not in next((config.data_root / "logs").iterdir()).read_text(
        "utf-8"
    )


def test_resident_poll_mode_waits_on_idle_without_busy_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    calls = 0
    waits: list[float] = []

    class IdleWorker:
        def run_once(self):
            nonlocal calls
            calls += 1

    def stop_after_wait(seconds: float) -> None:
        waits.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        cli_module,
        "build_candidate_worker",
        lambda configured, **_kwargs: IdleWorker(),
    )
    monkeypatch.setattr(cli_module.time, "sleep", stop_after_wait)

    main([*argv_for(config), "--poll-interval-seconds", "2.5"])

    assert calls == 1
    assert waits == [2.5]
    assert capsys.readouterr().out.strip() == '{"status": "idle"}'
    events = read_recent_runtime_events(config.data_root / "logs")
    assert {event["event"] for event in events} == {
        "worker.started",
        "worker.stopped",
    }
    stopped = next(event for event in events if event["event"] == "worker.stopped")
    assert stopped["reason"] == "keyboard_interrupt"


@pytest.mark.parametrize("value", ["0", "0.09", "nan", "inf", "61"])
def test_poll_interval_is_strictly_bounded(tmp_path: Path, value: str) -> None:
    config = make_config(tmp_path)
    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args(
            [*argv_for(config), "--poll-interval-seconds", value]
        )


def test_candidate_config_rejects_relative_or_outside_data_paths(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    with pytest.raises(ValueError, match="below the data root"):
        replace(config, database_path=(tmp_path / "outside.sqlite3").resolve())
    with pytest.raises(ValueError, match="absolute"):
        replace(config, unix_socket_path=Path("relative.sock"))
    with pytest.raises(ValueError, match="normalized"):
        replace(
            config,
            database_path=config.data_root / "nested" / ".." / "control.sqlite3",
        )
    with pytest.raises(ValueError, match="attempt timeout"):
        replace(config, attempt_timeout_seconds=1)


def test_cli_help_describes_opaque_claim_validated_credential_flow() -> None:
    assert "claim-validated" in CREDENTIAL_PROFILE_WIRING_STATUS
    assert "credential_profile_id" in CREDENTIAL_PROFILE_WIRING_STATUS
    assert "opaque" in CREDENTIAL_PROFILE_WIRING_STATUS

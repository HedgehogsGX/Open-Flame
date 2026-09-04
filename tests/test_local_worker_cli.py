from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from video_download_control.adapters import AdapterNetworkMode
from video_download_control.candidate_cookies import (
    AttemptCookieResolver,
    CookiePreparationError,
    CookieSource,
)
from video_download_control.assets import NonEmptyTestVerifier
from video_download_control.credentials import CredentialRepository
from video_download_control.database import Database
from video_download_control.domain import Platform
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
from video_download_control.subprocess_runner import CommandResult


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


def test_config_accepts_repeatable_platform_cookie_sources_and_check_mode(
    tmp_path: Path,
) -> None:
    youtube_cookie = (tmp_path / "youtube.cookies.txt").resolve()
    douyin_cookie = (tmp_path / "douyin.cookies.txt").resolve()
    arguments = build_parser().parse_args(
        cli_arguments(tmp_path)
        + [
            "--cookie-source",
            f"youtube:youtube-primary={youtube_cookie}",
            "--cookie-source",
            f"douyin:douyin-primary={douyin_cookie}",
            "--max-cookie-bytes",
            "4096",
            "--check",
        ]
    )

    config = LocalWorkerConfig.from_args(arguments)

    assert arguments.check is True
    assert config.max_cookie_bytes == 4096
    assert [source.platform for source in config.cookie_sources] == [
        Platform.YOUTUBE,
        Platform.DOUYIN,
    ]
    assert [source.credential_ref for source in config.cookie_sources] == [
        "youtube-primary",
        "douyin-primary",
    ]


def test_config_loads_cookie_sources_from_one_read_only_json_file(
    tmp_path: Path,
) -> None:
    config_path = (tmp_path / "private-cookie-sources.json").resolve()
    cookie_path = (tmp_path / "youtube.cookies.txt").resolve()
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cookie_sources": [
                    {
                        "platform": "youtube",
                        "credential_ref": "youtube-primary",
                        "path": str(cookie_path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o444)
    arguments = build_parser().parse_args(
        cli_arguments(tmp_path) + ["--cookie-config", str(config_path), "--check"]
    )

    config = LocalWorkerConfig.from_args(arguments)

    assert config.cookie_config_path == config_path
    assert len(config.cookie_sources) == 1
    assert config.cookie_sources[0].platform is Platform.YOUTUBE
    assert config.cookie_sources[0].credential_ref == "youtube-primary"
    assert config.cookie_sources[0].path == cookie_path


def test_cookie_config_is_mutually_exclusive_with_inline_cookie_sources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_ref = "PRIVATE-COOKIE-REFERENCE-MARKER"
    private_source = (tmp_path / "private-source.cookies.txt").resolve()
    private_config = (tmp_path / "private-config.json").resolve()

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            cli_arguments(tmp_path)
            + [
                "--cookie-config",
                str(private_config),
                "--cookie-source",
                f"youtube:{private_ref}={private_source}",
            ]
        )

    error = capsys.readouterr().err
    assert private_ref not in error
    assert str(private_source) not in error
    assert str(private_config) not in error


def test_config_rejects_cookie_config_inside_data_or_tool_root(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    tool_root = (tmp_path / "tools").resolve()

    for path in (data_root / "cookie.json", tool_root / "cookie.json"):
        with pytest.raises(ValueError, match="configuration must stay outside"):
            LocalWorkerConfig(
                data_root=data_root,
                database_path=data_root / "control.sqlite3",
                tool_root=tool_root,
                worker_id="local-test-worker",
                allow_direct_network=True,
                cookie_config_path=path,
            )


def test_config_rejects_duplicate_cookie_platforms(tmp_path: Path) -> None:
    first = (tmp_path / "first.cookies.txt").resolve()
    second = (tmp_path / "second.cookies.txt").resolve()
    arguments = build_parser().parse_args(
        cli_arguments(tmp_path)
        + [
            "--cookie-source",
            f"youtube:first={first}",
            "--cookie-source",
            f"youtube:second={second}",
        ]
    )

    with pytest.raises(ValueError, match="cookie source configuration"):
        LocalWorkerConfig.from_args(arguments)


def test_config_rejects_cookie_source_inside_data_or_tool_root(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    tool_root = (tmp_path / "tools").resolve()

    for path in (data_root / "cookie.txt", tool_root / "cookie.txt"):
        with pytest.raises(ValueError, match="outside data and tool roots"):
            LocalWorkerConfig(
                data_root=data_root,
                database_path=data_root / "control.sqlite3",
                tool_root=tool_root,
                worker_id="local-test-worker",
                allow_direct_network=True,
                cookie_sources=(
                    CookieSource(Platform.YOUTUBE, "youtube-primary", path),
                ),
            )


def test_cookie_argument_failure_does_not_echo_private_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "PRIVATE-COOKIE-REFERENCE-MARKER"

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            cli_arguments(tmp_path)
            + ["--cookie-source", f"youtube:{marker}=relative-cookie.txt"]
        )

    assert marker not in capsys.readouterr().err


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
    cookie_source = (tmp_path / "youtube.cookies.txt").resolve()
    cookie_source.write_bytes(b"cookie-source-test-double")
    cookie_source.chmod(0o444)

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
        cookie_sources=(
            CookieSource(Platform.YOUTUBE, "youtube-primary", cookie_source),
        ),
        max_cookie_bytes=4096,
    )

    try:
        runtime_logger = local_worker_module._runtime_logger(config)
        worker = build_local_worker(  # type: ignore[arg-type]
            config,
            runner=Runner(),
            runtime_logger=runtime_logger,
            claim_gate_run_id="d" * 32,
        )

        assert worker.network_mode is AdapterNetworkMode.DIRECT_EGRESS
        assert worker.asset_store.data_root == data_root
        assert worker.repository.database.path == database_path
        assert worker.skip_unsupported_graph_jobs is True
        assert worker.claim_gate_run_id == "d" * 32
        resolver = worker.adapter._cookie_resolver
        assert isinstance(resolver, AttemptCookieResolver)
        assert resolver.configured_platforms == (Platform.YOUTUBE,)
        assert resolver.max_cookie_bytes == 4096
        assert [
            event["event"]
            for event in read_recent_runtime_events(data_root / "logs")
        ] == ["toolchain.inspected"]
    finally:
        cookie_source.chmod(0o600)


def test_local_worker_cookie_profile_reaches_yt_dlp_attempt_copy_and_ready_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings,
    database: Database,
    service,
) -> None:
    monkeypatch.setenv(FEATURE_GATE, "1")
    batch = service.create_batch(
        name="synthetic local Cookie E2E",
        raw_inputs=["https://www.douyin.com/video/7123456789012345678"],
    )
    credential_ref = "douyin-primary"
    profile = CredentialRepository(database).register(
        platform=Platform.DOUYIN,
        name="Douyin local profile",
        secret_ref=credential_ref,
    )
    assigned = CredentialRepository(database).assign(
        profile_id=profile["id"],
        batch_id=batch["id"],
    )
    assert assigned["assigned_count"] == 1

    tool_root = (tmp_path / "tools").resolve()
    entrypoint = tool_root / "yt-dlp" / "yt-dlp"
    ffmpeg_root = tool_root / "ffmpeg" / "bin"
    entrypoint.parent.mkdir(parents=True)
    ffmpeg_root.mkdir(parents=True)
    entrypoint.write_bytes(b"yt-dlp-test-double")
    (ffmpeg_root / "ffmpeg.exe").write_bytes(b"ffmpeg-test-double")
    (ffmpeg_root / "ffprobe.exe").write_bytes(b"ffprobe-test-double")
    cookie_secret = b"synthetic-cookie-secret-marker"
    cookie_source = (tmp_path / "douyin.cookies.txt").resolve()
    cookie_source.write_bytes(cookie_secret)
    cookie_source.chmod(0o444)
    seen_cookie_paths: list[Path] = []

    class Verified:
        yt_dlp_version = "2026.08.19"
        ffmpeg_version = "n9-test"
        ffprobe_version = "n9-test"
        offline_smoke_passed = True

    class YtLock:
        entrypoint = "yt-dlp/yt-dlp"

    class Lock:
        yt_dlp = YtLock()

    class Verifier(NonEmptyTestVerifier):
        def __init__(self, **_kwargs) -> None:
            pass

        def validate_runtime(self) -> None:
            return None

    class CookieAwareRunner:
        def run(self, spec, *, is_cancelled=None):
            del is_cancelled
            arguments = spec.arguments
            if "--version" in arguments:
                return CommandResult(
                    returncode=0,
                    stdout=b"2026.08.19\n",
                    stderr=b"",
                    duration_seconds=0.01,
                )
            cookie_option = arguments.index("--cookies")
            cookie_path = Path(arguments[cookie_option + 1])
            assert cookie_path != cookie_source
            assert cookie_path.is_relative_to(settings.data_root / "temporary")
            assert cookie_path.read_bytes() == cookie_secret
            seen_cookie_paths.append(cookie_path)
            if "--dump-single-json" in arguments:
                payload = json.dumps(
                    {
                        "id": "7123456789012345678",
                        "title": "Synthetic Douyin item",
                        "duration": 1.0,
                        "height": 720,
                        "ext": "mp4",
                        "vcodec": "h264",
                        "acodec": "aac",
                    }
                ).encode()
                return CommandResult(0, payload, b"", 0.01)
            output = Path(arguments[arguments.index("--paths") + 1])
            original = output / "7123456789012345678.mp4"
            original.write_bytes(b"synthetic-douyin-video")
            mapping = Path(
                arguments[arguments.index("--print-to-file") + 2].replace("%%", "%")
            )
            mapping.write_text(
                json.dumps(
                    {
                        "id": "7123456789012345678",
                        "filepath": str(original.resolve()),
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            return CommandResult(0, b"", b"", 0.01)

    monkeypatch.setattr(
        local_worker_module,
        "verify_toolchain",
        lambda *_args, **_kwargs: Verified(),
    )
    monkeypatch.setattr(local_worker_module, "load_toolchain_lock", lambda: Lock())
    monkeypatch.setattr(local_worker_module, "FfprobeVerifier", Verifier)
    config = LocalWorkerConfig(
        data_root=settings.data_root.resolve(),
        database_path=settings.database_path.resolve(),
        tool_root=tool_root,
        worker_id="local-cookie-e2e",
        allow_direct_network=True,
        cookie_sources=(
            CookieSource(Platform.DOUYIN, credential_ref, cookie_source),
        ),
        max_cookie_bytes=4096,
        storage_min_free_bytes=0,
    )

    try:
        runtime_logger = local_worker_module._runtime_logger(config)
        worker = build_local_worker(
            config,
            runner=CookieAwareRunner(),  # type: ignore[arg-type]
            runtime_logger=runtime_logger,
        )
        result = worker.run_once()

        assert result is not None and result.status == "ready"
        assert len(seen_cookie_paths) == 2
        assert seen_cookie_paths[0] != seen_cookie_paths[1]
        assert all(not path.exists() for path in seen_cookie_paths)
        with database.connect() as connection:
            asset = connection.execute(
                "SELECT status, size_bytes FROM media_assets"
            ).fetchone()
        assert dict(asset) == {
            "status": "ready",
            "size_bytes": len(b"synthetic-douyin-video"),
        }
        serialized_log = runtime_logger.path.read_text("utf-8")
        assert credential_ref not in serialized_log
        assert str(cookie_source) not in serialized_log
        assert cookie_secret.decode() not in serialized_log
    finally:
        cookie_source.chmod(0o600)


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


def test_main_check_validates_without_claiming_and_reports_cookie_platforms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = (tmp_path / "data").resolve()
    Database(data_root / "control.sqlite3").initialize()
    cookie_source = (tmp_path / "youtube.cookies.txt").resolve()
    monkeypatch.setenv(FEATURE_GATE, "1")
    run_once_called = False

    class NoClaimWorker:
        def run_once(self):
            nonlocal run_once_called
            run_once_called = True
            raise AssertionError("--check must not claim from the queue")

    monkeypatch.setattr(
        local_worker_module,
        "build_local_worker",
        lambda *_args, **_kwargs: NoClaimWorker(),
    )

    main(
        cli_arguments(tmp_path)
        + [
            "--cookie-source",
            f"youtube:youtube-primary={cookie_source}",
            "--check",
        ]
    )

    assert run_once_called is False
    assert json.loads(capsys.readouterr().out) == {
        "cookie_platforms": ["youtube"],
        "status": "ready",
    }
    events = read_recent_runtime_events(data_root / "logs")
    assert [event["event"] for event in events] == [
        "worker.preflight_started",
        "worker.preflight_succeeded",
    ]


def test_main_check_real_builder_preserves_queue_and_attempt_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings,
    database: Database,
    service,
) -> None:
    monkeypatch.setenv(FEATURE_GATE, "1")
    batch = service.create_batch(
        name="synthetic preflight no-claim",
        raw_inputs=["https://www.douyin.com/video/7123456789012345678"],
    )
    credential_ref = "douyin-preflight"
    profile = CredentialRepository(database).register(
        platform=Platform.DOUYIN,
        name="Douyin preflight profile",
        secret_ref=credential_ref,
    )
    CredentialRepository(database).assign(
        profile_id=profile["id"],
        batch_id=batch["id"],
    )

    tool_root = (tmp_path / "tools").resolve()
    entrypoint = tool_root / "yt-dlp" / "yt-dlp"
    ffmpeg_root = tool_root / "ffmpeg" / "bin"
    entrypoint.parent.mkdir(parents=True)
    ffmpeg_root.mkdir(parents=True)
    entrypoint.write_bytes(b"yt-dlp-test-double")
    (ffmpeg_root / "ffmpeg.exe").write_bytes(b"ffmpeg-test-double")
    (ffmpeg_root / "ffprobe.exe").write_bytes(b"ffprobe-test-double")
    cookie_source = (tmp_path / "douyin-preflight.cookies.txt").resolve()
    cookie_source.write_bytes(b"synthetic-preflight-cookie")
    cookie_source.chmod(0o444)

    class Verified:
        yt_dlp_version = "2026.08.19"
        ffmpeg_version = "n9-test"
        ffprobe_version = "n9-test"
        offline_smoke_passed = True

    class YtLock:
        entrypoint = "yt-dlp/yt-dlp"

    class Lock:
        yt_dlp = YtLock()

    class Verifier(NonEmptyTestVerifier):
        def __init__(self, **_kwargs) -> None:
            pass

        def validate_runtime(self) -> None:
            return None

    def database_snapshot() -> dict[str, tuple[tuple[object, ...], ...]]:
        tables = (
            "download_jobs",
            "job_attempts",
            "media_assets",
            "asset_commit_intents",
        )
        with database.connect() as connection:
            return {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608
                    ).fetchall()
                )
                for table in tables
            }

    monkeypatch.setattr(
        local_worker_module,
        "verify_toolchain",
        lambda *_args, **_kwargs: Verified(),
    )
    monkeypatch.setattr(local_worker_module, "load_toolchain_lock", lambda: Lock())
    monkeypatch.setattr(local_worker_module, "FfprobeVerifier", Verifier)
    before_database = database_snapshot()
    before_cookie = cookie_source.stat()

    try:
        main(
            cli_arguments(tmp_path)
            + [
                "--database-path",
                str(settings.database_path.resolve()),
                "--cookie-source",
                f"douyin:{credential_ref}={cookie_source}",
                "--check",
            ]
        )

        assert database_snapshot() == before_database
        after_cookie = cookie_source.stat()
        assert (
            after_cookie.st_dev,
            after_cookie.st_ino,
            after_cookie.st_size,
            after_cookie.st_mtime_ns,
        ) == (
            before_cookie.st_dev,
            before_cookie.st_ino,
            before_cookie.st_size,
            before_cookie.st_mtime_ns,
        )
        assert not list((settings.data_root / "temporary").rglob("secrets"))
        assert json.loads(capsys.readouterr().out) == {
            "cookie_platforms": ["douyin"],
            "status": "ready",
        }
        events = read_recent_runtime_events(settings.data_root / "logs")
        assert [event["event"] for event in events] == [
            "worker.preflight_started",
            "toolchain.inspected",
            "worker.preflight_succeeded",
        ]
    finally:
        cookie_source.chmod(0o600)


def test_main_redacts_cookie_startup_failure_and_discards_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = (tmp_path / "data").resolve()
    Database(data_root / "control.sqlite3").initialize()
    monkeypatch.setenv(FEATURE_GATE, "1")
    marker = "PRIVATE-COOKIE-PATH-MARKER"

    def failing_build(*_args, **_kwargs):
        raise CookiePreparationError(marker)

    monkeypatch.setattr(local_worker_module, "build_local_worker", failing_build)

    with pytest.raises(SystemExit) as caught:
        main(cli_arguments(tmp_path))

    assert str(caught.value) == (
        "local real Worker credential preparation failed; inspect the runtime log"
    )
    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    events = read_recent_runtime_events(data_root / "logs")
    assert [event["event"] for event in events] == [
        "worker.initializing",
        "worker.startup_failed",
    ]
    assert events[-1]["exception_type"] == "CookiePreparationError"
    assert marker not in next((data_root / "logs").iterdir()).read_text("utf-8")


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

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from video_download_control.adapters import (
    ControlledEgressEndpoint,
    CookieMount,
    DirectEgress,
    YtDlpCommandFactory,
    YtDlpContractError,
    YtDlpJsRuntime,
    YtDlpJsRuntimeName,
)
from video_download_control.adapters.base import DownloadRequest, ProbeRequest
from video_download_control.domain import Platform, SourceType


def factory(tmp_path: Path) -> YtDlpCommandFactory:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    executable = tools / "yt-dlp.exe"
    executable.touch(exist_ok=True)
    return YtDlpCommandFactory(
        executable=executable.resolve(),
        ffmpeg_directory=tools.resolve(),
        expected_version="2026.08.19",
        egress=ControlledEgressEndpoint(
            "http://127.0.0.1:8080", policy_version="egress-v1"
        ),
    )


def probe_request() -> ProbeRequest:
    return ProbeRequest(
        job_id=str(uuid4()),
        canonical_url="https://www.youtube.com/watch?v=test",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
    )


def test_probe_is_skip_download_single_json_and_forces_proxy(tmp_path: Path) -> None:
    attempt = (tmp_path / "attempt").resolve()
    attempt.mkdir()
    command = factory(tmp_path).probe_command(probe_request(), temporary_root=attempt)
    assert command.expected_version == "2026.08.19"
    assert "--ignore-config" in command.arguments
    assert command.arguments.count("--encoding") == 1
    assert command.arguments[command.arguments.index("--encoding") + 1] == "utf-8"
    assert "--skip-download" in command.arguments
    assert "--convert-thumbnails" not in command.arguments
    assert "--dump-single-json" in command.arguments
    assert command.arguments[command.arguments.index("--proxy") + 1] == (
        "http://127.0.0.1:8080"
    )
    assert "--no-plugin-dirs" in command.arguments
    assert "--no-remote-components" in command.arguments
    assert "--netrc" not in command.arguments
    assert "--no-netrc" not in command.arguments
    assert "--no-js-runtimes" in command.arguments
    assert "--js-runtimes" not in command.arguments
    assert command.arguments[-2:] == (
        "--",
        "https://www.youtube.com/watch?v=test",
    )


@pytest.mark.parametrize(
    ("platform", "source_type", "canonical_url"),
    (
        (
            Platform.BILIBILI,
            SourceType.BILIBILI_VIDEO,
            "https://www.bilibili.com/video/BV1xx411c7mD",
        ),
        (
            Platform.DOUYIN,
            SourceType.DOUYIN_VIDEO,
            "https://www.douyin.com/video/1234567890123456789",
        ),
        (
            Platform.TIKTOK,
            SourceType.TIKTOK_VIDEO,
            "https://www.tiktok.com/@example/video/1234567890123456789",
        ),
        (
            Platform.INSTAGRAM,
            SourceType.INSTAGRAM_REEL,
            "https://www.instagram.com/reel/Example_123",
        ),
    ),
)
def test_candidate_platform_routes_preserve_the_exact_canonical_url(
    tmp_path: Path,
    platform: Platform,
    source_type: SourceType,
    canonical_url: str,
) -> None:
    attempt = (tmp_path / platform.value).resolve()
    attempt.mkdir()
    request = ProbeRequest(
        job_id=str(uuid4()),
        canonical_url=canonical_url,
        platform=platform,
        source_type=source_type,
    )

    command = factory(tmp_path).probe_command(request, temporary_root=attempt)

    assert "--no-playlist" in command.arguments
    assert command.arguments[-2:] == ("--", canonical_url)


def test_explicit_direct_egress_omits_proxy_without_weakening_other_options(
    tmp_path: Path,
) -> None:
    tools = (tmp_path / "direct-tools").resolve()
    tools.mkdir()
    executable = tools / "yt-dlp.exe"
    executable.write_bytes(b"offline-yt-dlp-test-double")
    command_factory = YtDlpCommandFactory(
        executable=executable,
        ffmpeg_directory=tools,
        expected_version="2026.08.19",
        egress=DirectEgress(),
    )
    attempt = (tmp_path / "direct-attempt").resolve()
    attempt.mkdir()

    command = command_factory.probe_command(
        probe_request(),
        temporary_root=attempt,
    )

    assert command.arguments.count("--proxy") == 1
    assert command.arguments[command.arguments.index("--proxy") + 1] == ""
    assert "--no-remote-components" in command.arguments
    assert "--no-js-runtimes" in command.arguments
    assert command.arguments.count("--encoding") == 1
    assert command.arguments[command.arguments.index("--encoding") + 1] == "utf-8"


def test_python_zipimport_entrypoint_prefixes_every_yt_dlp_command(
    tmp_path: Path,
) -> None:
    tools = (tmp_path / "zipimport-tools").resolve()
    tools.mkdir()
    python_executable = tools / "python.exe"
    python_executable.write_bytes(b"trusted-python-test-double")
    zipimport_entrypoint = tools / "yt-dlp"
    zipimport_entrypoint.write_bytes(b"trusted-zipimport-test-double")
    command_factory = YtDlpCommandFactory(
        executable=python_executable,
        zipimport_entrypoint=zipimport_entrypoint,
        ffmpeg_directory=tools,
        expected_version="2026.08.19",
        egress=ControlledEgressEndpoint(
            "http://127.0.0.1:8080", policy_version="egress-v1"
        ),
    )
    attempt = (tmp_path / "zipimport-attempt").resolve()
    output = attempt / "output"
    output.mkdir(parents=True)
    download_request = DownloadRequest(
        job_id=str(uuid4()),
        source_item_id=str(uuid4()),
        canonical_url="https://www.youtube.com/watch?v=test",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        output_dir=output,
        expected_media_keys=("test",),
    )

    commands = (
        command_factory.version_command(),
        command_factory.probe_command(probe_request(), temporary_root=attempt),
        command_factory.download_command(download_request, temporary_root=attempt),
    )

    assert "--encoding" not in commands[0].arguments
    for command in commands[1:]:
        assert command.arguments.count("--encoding") == 1
        assert command.arguments[command.arguments.index("--encoding") + 1] == "utf-8"

    for command in commands:
        assert command.executable == python_executable
        assert command.arguments[:2] == (
            str(zipimport_entrypoint),
            "--ignore-config",
        )


def test_zipimport_entrypoint_must_be_an_absolute_regular_file(
    tmp_path: Path,
) -> None:
    tools = (tmp_path / "zipimport-validation-tools").resolve()
    tools.mkdir()
    python_executable = tools / "python.exe"
    python_executable.write_bytes(b"trusted-python-test-double")
    common = {
        "executable": python_executable,
        "ffmpeg_directory": tools,
        "expected_version": "2026.08.19",
        "egress": ControlledEgressEndpoint(
            "http://127.0.0.1:8080", policy_version="egress-v1"
        ),
    }

    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpCommandFactory(
            **common,
            zipimport_entrypoint=Path("yt-dlp"),
        )
    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpCommandFactory(
            **common,
            zipimport_entrypoint=(tools / "missing-yt-dlp").resolve(),
        )
    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpCommandFactory(
            **common,
            zipimport_entrypoint=tools,
        )


def test_download_output_and_options_are_fixed_inside_attempt(tmp_path: Path) -> None:
    attempt = (tmp_path / "attempt").resolve()
    output = attempt / "output"
    output.mkdir(parents=True)
    request = DownloadRequest(
        job_id=str(uuid4()),
        source_item_id=str(uuid4()),
        canonical_url="https://www.youtube.com/watch?v=test",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        output_dir=output,
        expected_media_keys=("test",),
    )
    command = factory(tmp_path).download_command(request, temporary_root=attempt)
    assert command.arguments[command.arguments.index("--paths") + 1] == str(output)
    assert command.arguments[command.arguments.index("--output") + 1] == (
        "%(id)s.%(ext)s"
    )
    output_templates = [
        command.arguments[index + 1]
        for index, argument in enumerate(command.arguments)
        if argument == "--output"
    ]
    assert output_templates == [
        "%(id)s.%(ext)s",
        "thumbnail:%(id)s.thumbnail.%(ext)s",
        "subtitle:%(id)s.caption.%(language)s.%(ext)s",
    ]
    print_index = command.arguments.index("--print-to-file")
    assert command.arguments[print_index + 1] == "after_move:%(.{id,filepath})j"
    assert command.mapping_path == attempt / "yt-dlp-after-move.jsonl"
    assert command.arguments[print_index + 2] == str(command.mapping_path)
    assert not command.mapping_path.exists()
    assert command.arguments[command.arguments.index("--format") + 1] == (
        "bv*[height<=1080][protocol!*=m3u8]+ba[protocol!*=m3u8]/"
        "b[height<=1080][protocol!*=m3u8]/"
        "bv*[height<=1080]+ba/b[height<=1080]"
    )
    assert "--no-playlist" in command.arguments
    assert "--abort-on-error" in command.arguments
    assert command.arguments.count("--encoding") == 1
    assert command.arguments[command.arguments.index("--encoding") + 1] == "utf-8"
    assert "--no-write-playlist-metafiles" in command.arguments
    assert "--no-write-info-json" in command.arguments
    assert command.arguments.count("--convert-thumbnails") == 1
    assert command.arguments[command.arguments.index("--convert-thumbnails") + 1] == (
        "image>png"
    )
    assert command.arguments[command.arguments.index("--sub-langs") + 1] == (
        "en,zh,zh-Hans,zh-Hant"
    )
    assert command.arguments.count("--progress") == 1
    assert command.arguments.count("--newline") == 1
    assert command.arguments[command.arguments.index("--progress-delta") + 1] == (
        "0.5"
    )
    progress_templates = [
        command.arguments[index + 1]
        for index, argument in enumerate(command.arguments)
        if argument == "--progress-template"
    ]
    assert progress_templates == [
        "download:VDC_PROGRESS|%(info.id&1|0)s|"
        "%(info.format_id&1|0)s|%(progress.status)j|"
        "%(progress.downloaded_bytes)j|%(progress.total_bytes)j|"
        "%(progress.total_bytes_estimate)j|%(progress.progress_idx)j|"
        "%(progress.max_progress)j",
        "postprocess:VDC_PHASE|postprocessing",
    ]
    assert "--external-downloader" not in command.arguments
    assert "--exec" not in command.arguments

    probe = factory(tmp_path).probe_command(probe_request(), temporary_root=attempt)
    for progress_option in (
        "--progress",
        "--progress-delta",
        "--progress-template",
        "--newline",
    ):
        assert progress_option not in probe.arguments


def test_explicit_js_runtime_clears_defaults_then_enables_only_that_runtime(
    tmp_path: Path,
) -> None:
    tools = (tmp_path / "js-runtime-tools").resolve()
    tools.mkdir()
    yt_dlp = tools / "yt-dlp.exe"
    yt_dlp.write_bytes(b"offline-yt-dlp-test-double")
    node = tools / "node.exe"
    node.write_bytes(b"offline-node-test-double")
    runtime = YtDlpJsRuntime(YtDlpJsRuntimeName.NODE, node)
    command_factory = YtDlpCommandFactory(
        executable=yt_dlp,
        ffmpeg_directory=tools,
        expected_version="2026.08.19",
        egress=ControlledEgressEndpoint(
            "http://127.0.0.1:8080", policy_version="egress-v1"
        ),
        js_runtime=runtime,
    )
    attempt = (tmp_path / "js-runtime-attempt").resolve()
    attempt.mkdir()

    command = command_factory.probe_command(
        probe_request(),
        temporary_root=attempt,
    )

    assert command.arguments.count("--no-js-runtimes") == 1
    assert command.arguments.count("--js-runtimes") == 1
    clear_index = command.arguments.index("--no-js-runtimes")
    runtime_index = command.arguments.index("--js-runtimes")
    assert clear_index < runtime_index
    assert command.arguments[runtime_index + 1] == f"node:{node}"
    assert "--no-remote-components" in command.arguments


def test_js_runtime_rejects_unsupported_or_unsafe_executables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = (tmp_path / "node.exe").resolve()
    executable.write_bytes(b"offline-node-test-double")

    with pytest.raises(YtDlpContractError, match="unsupported"):
        YtDlpJsRuntime("python", executable)  # type: ignore[arg-type]
    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpJsRuntime(YtDlpJsRuntimeName.NODE, Path("node.exe"))
    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpJsRuntime(
            YtDlpJsRuntimeName.NODE,
            (tmp_path / "missing-node.exe").resolve(),
        )
    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpJsRuntime(YtDlpJsRuntimeName.NODE, tmp_path.resolve())

    original_lstat = Path.lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    monkeypatch.setattr(
        stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        reparse_flag,
        raising=False,
    )

    def reparse_lstat(path: Path):
        info = original_lstat(path)
        if path == executable:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=reparse_flag,
            )
        return info

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpJsRuntime(YtDlpJsRuntimeName.NODE, executable)


def test_js_runtime_rejects_symbolic_link(tmp_path: Path) -> None:
    target = (tmp_path / "real-node.exe").resolve()
    target.write_bytes(b"offline-node-test-double")
    alias = (tmp_path / "node-alias.exe").resolve()
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable to this test user")

    with pytest.raises(YtDlpContractError, match="absolute regular file"):
        YtDlpJsRuntime(YtDlpJsRuntimeName.NODE, alias)


def test_mapping_file_template_escapes_literal_percent_in_attempt_path(
    tmp_path: Path,
) -> None:
    attempt = (tmp_path / "100%attempt").resolve()
    output = attempt / "output"
    output.mkdir(parents=True)
    request = DownloadRequest(
        job_id=str(uuid4()),
        source_item_id=str(uuid4()),
        canonical_url="https://www.youtube.com/watch?v=test",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        output_dir=output,
        expected_media_keys=("test",),
    )

    command = factory(tmp_path).download_command(
        request,
        temporary_root=attempt,
    )

    option = command.arguments.index("--print-to-file")
    assert command.mapping_path == attempt / "yt-dlp-after-move.jsonl"
    assert "100%%attempt" in command.arguments[option + 2]


def test_output_escape_and_mismatched_cookie_are_rejected(tmp_path: Path) -> None:
    attempt = (tmp_path / "attempt").resolve()
    attempt.mkdir()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    request = DownloadRequest(
        job_id=str(uuid4()),
        source_item_id=str(uuid4()),
        canonical_url="https://www.youtube.com/watch?v=test",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        output_dir=outside,
        expected_media_keys=("test",),
    )
    with pytest.raises(YtDlpContractError, match="inside"):
        factory(tmp_path).download_command(request, temporary_root=attempt)

    cookie_path = (tmp_path / "cookies.txt").resolve()
    cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    with pytest.raises(YtDlpContractError, match="platform"):
        factory(tmp_path).probe_command(
            probe_request(),
            temporary_root=attempt,
            cookie=CookieMount(Platform.X, cookie_path),
        )


def test_cookie_must_be_an_attempt_private_nonlinked_copy(tmp_path: Path) -> None:
    attempt = (tmp_path / "attempt").resolve()
    secrets = attempt / "secrets"
    secrets.mkdir(parents=True)
    private_copy = secrets / "cookies.txt"
    private_copy.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    private_copy.chmod(0o600)

    command = factory(tmp_path).probe_command(
        probe_request(),
        temporary_root=attempt,
        cookie=CookieMount(Platform.YOUTUBE, private_copy),
    )
    assert command.arguments[command.arguments.index("--cookies") + 1] == str(
        private_copy
    )

    mounted_source = (tmp_path / "mounted-cookies.txt").resolve()
    mounted_source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    with pytest.raises(YtDlpContractError, match="attempt-private"):
        factory(tmp_path).probe_command(
            probe_request(),
            temporary_root=attempt,
            cookie=CookieMount(Platform.YOUTUBE, mounted_source),
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://egress-proxy:8080",
        "http://user:secret@egress-proxy:8080",
        "http://egress-proxy:8080/path",
        "http://egress-proxy",
        "http://egress-proxy:99999",
        "http://egress-proxy:8080",
        "http://192.0.2.1:8080",
    ],
)
def test_controlled_proxy_endpoint_rejects_ambiguous_or_secret_shapes(
    url: str,
) -> None:
    with pytest.raises(YtDlpContractError):
        ControlledEgressEndpoint(url, policy_version="egress-v1")

from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from video_download_control.adapters.base import (
    AdapterContext,
    AdapterFailure,
    AdapterNetworkMode,
    DownloadRequest,
    ProbeRequest,
)
from video_download_control.adapters.yt_dlp import YtDlpAdapter
import video_download_control.adapters.yt_dlp as yt_dlp_module
from video_download_control.adapters.yt_dlp_contract import (
    ControlledEgressEndpoint,
    DirectEgress,
    YtDlpCommandFactory,
)
from video_download_control.domain import ErrorCode, Platform, SourceType
from video_download_control.subprocess_runner import (
    CommandCancelled,
    CommandOutputLimitExceeded,
    CommandResult,
    CommandSpec,
    CommandTimedOut,
)


RunnerStep = CommandResult | BaseException | Callable[[CommandSpec], CommandResult]


class FakeRunner:
    def __init__(self, *steps: RunnerStep) -> None:
        self.steps = deque(steps)
        self.calls: list[tuple[CommandSpec, Callable[[], bool] | None]] = []

    def run(
        self,
        spec: CommandSpec,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CommandResult:
        self.calls.append((spec, is_cancelled))
        if not self.steps:
            raise AssertionError("unexpected runner call")
        step = self.steps.popleft()
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(spec)
        return step


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def command_result(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> CommandResult:
    return CommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
    )


def mapping_path_from_spec(spec: CommandSpec) -> Path:
    option = spec.arguments.index("--print-to-file")
    assert spec.arguments[option + 1] == "after_move:%(.{id,filepath})j"
    # FILE follows output-template syntax, so literal percent signs are doubled.
    return Path(spec.arguments[option + 2].replace("%%", "%"))


def append_mapping(spec: CommandSpec, *records: dict[str, str]) -> Path:
    mapping_path = mapping_path_from_spec(spec)
    assert mapping_path.is_file()
    assert mapping_path.stat().st_size == 0
    with mapping_path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")))
            handle.write("\n")
    return mapping_path


def make_factory(tmp_path: Path) -> YtDlpCommandFactory:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    executable = tools / "yt-dlp.exe"
    executable.touch(exist_ok=True)
    return YtDlpCommandFactory(
        executable=executable.resolve(),
        ffmpeg_directory=tools.resolve(),
        expected_version="2026.08.19",
        egress=ControlledEgressEndpoint(
            "http://127.0.0.1:8080",
            policy_version="egress-v1",
        ),
    )


def make_context(tmp_path: Path) -> AdapterContext:
    attempt = (tmp_path / "attempt").resolve()
    attempt.mkdir(exist_ok=True)
    return AdapterContext(
        worker_id="worker-test",
        attempt_id="attempt-test",
        temporary_dir=attempt,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


def make_probe_request(*, max_items: int = 1) -> ProbeRequest:
    return ProbeRequest(
        job_id="job-1",
        canonical_url="https://www.youtube.com/watch?v=stable-id",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        max_items=max_items,
    )


def make_download_request(
    context: AdapterContext,
    *,
    expected_media_keys: tuple[str, ...] = ("video-123",),
) -> DownloadRequest:
    output = context.temporary_dir / "output"
    output.mkdir(exist_ok=True)
    return DownloadRequest(
        job_id="job-1",
        source_item_id="source-item-1",
        canonical_url="https://www.youtube.com/watch?v=stable-id",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        output_dir=output,
        expected_media_keys=expected_media_keys,
    )


def test_probe_validates_exact_runtime_and_reduces_json_to_safe_dtos(
    tmp_path: Path,
) -> None:
    secret = "signed-secret-value"
    payload = json.dumps(
        {
            "id": "video-123",
            "title": "Safe title\u0000with control",
            "uploader": "Uploader",
            "duration": 12.5,
            "height": 1080,
            "ext": "mp4",
            "vcodec": "avc1",
            "url": f"https://cdn.invalid/media?token={secret}",
            "http_headers": {"Authorization": secret},
            "formats": [{"url": secret}],
        }
    ).encode()
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19\n"),
        command_result(stdout=payload),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    result = adapter.probe(make_probe_request(), make_context(tmp_path))

    assert adapter.name == "yt_dlp"
    assert adapter.version == "2026.08.19"
    assert adapter.network_mode is AdapterNetworkMode.CONTROLLED_EGRESS
    assert len(runner.calls) == 2
    version_spec, _ = runner.calls[0]
    probe_spec, _ = runner.calls[1]
    assert version_spec.arguments == ("--ignore-config", "--version")
    assert version_spec.stdout_limit_bytes == 256
    assert probe_spec.stdout_limit_bytes == 2 * 1024 * 1024
    assert probe_spec.stderr_limit_bytes == 1024 * 1024
    assert result.expected_item_count == 1
    assert result.items[0].media_key == "video-123"
    assert result.items[0].media_kind == "video"
    assert result.items[0].canonical_url == make_probe_request().canonical_url
    serialized = json.dumps(
        {
            "source": dict(result.sanitized_source),
            "metadata": dict(result.items[0].metadata),
        }
    )
    assert secret not in serialized
    assert "formats" not in serialized
    assert "http_headers" not in serialized
    assert len(result.discovery_snapshot_hash) == 64


def test_adapter_declares_direct_network_only_for_proxyless_factory(
    tmp_path: Path,
) -> None:
    tools = (tmp_path / "direct-tools").resolve()
    tools.mkdir()
    executable = tools / "yt-dlp.exe"
    executable.write_bytes(b"offline-yt-dlp-test-double")
    direct_factory = YtDlpCommandFactory(
        executable=executable,
        ffmpeg_directory=tools,
        expected_version="2026.08.19",
        egress=DirectEgress(),
    )

    adapter = YtDlpAdapter(factory=direct_factory, runner=FakeRunner())  # type: ignore[arg-type]

    assert adapter.network_mode is AdapterNetworkMode.DIRECT_EGRESS


def test_probe_parses_multiple_entries_only_within_explicit_limit(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        {
            "id": "post-1",
            "entries": [
                {"id": "attachment-1", "title": "First", "ext": "mp4"},
                {
                    "id": "attachment-2",
                    "title": "Second",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "aac",
                },
            ],
        }
    ).encode()
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        command_result(stdout=payload),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    result = adapter.probe(
        make_probe_request(max_items=2),
        make_context(tmp_path),
    )

    assert result.expected_item_count == 2
    assert [item.media_key for item in result.items] == [
        "attachment-1",
        "attachment-2",
    ]
    assert [item.media_kind for item in result.items] == ["video", "audio"]


def test_probe_rejects_more_entries_than_request_limit(tmp_path: Path) -> None:
    payload = json.dumps({"entries": [{"id": "1"}, {"id": "2"}]}).encode()
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        command_result(stdout=payload),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(max_items=1), make_context(tmp_path))

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_probe_rejects_duplicate_media_identifiers(tmp_path: Path) -> None:
    payload = json.dumps({"entries": [{"id": "same"}, {"id": "same"}]}).encode()
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        command_result(stdout=payload),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(max_items=2), make_context(tmp_path))

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_injected_runner_cannot_bypass_probe_json_byte_limit(tmp_path: Path) -> None:
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        command_result(stdout=b"{" + (b" " * 64) + b"}"),
    )
    adapter = YtDlpAdapter(
        factory=make_factory(tmp_path),
        runner=runner,  # type: ignore[arg-type]
        probe_stdout_limit_bytes=32,
    )

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(), make_context(tmp_path))

    assert caught.value.code is ErrorCode.EXTRACTOR_BROKEN
    assert "bounded" in caught.value.diagnostic


def test_runtime_version_mismatch_fails_closed_without_echoing_output(
    tmp_path: Path,
) -> None:
    actual = "unexpected-version-with-secret"
    runner = FakeRunner(command_result(stdout=actual.encode()))
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(), make_context(tmp_path))

    assert caught.value.code is ErrorCode.EXTRACTOR_BROKEN
    assert actual not in caught.value.diagnostic


def test_nonzero_version_command_is_always_extractor_breakage(tmp_path: Path) -> None:
    runner = FakeRunner(
        command_result(
            returncode=1,
            stderr=b"HTTP Error 429 with credential=secret",
        )
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(), make_context(tmp_path))

    assert caught.value.code is ErrorCode.EXTRACTOR_BROKEN
    assert "secret" not in caught.value.diagnostic


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"ERROR: HTTP Error 429: token=secret", ErrorCode.RATE_LIMITED),
        (b"ERROR: This video is DRM protected", ErrorCode.DRM_PROTECTED),
        (b"ERROR: not available in your country", ErrorCode.GEO_RESTRICTED),
        (b"ERROR: Private video", ErrorCode.PRIVATE_CONTENT),
        (b"ERROR: Sign in to confirm you're not a bot", ErrorCode.AUTHENTICATION_REQUIRED),
        (
            b"ERROR: Fresh cookies (not necessarily logged in) are needed",
            ErrorCode.AUTHENTICATION_REQUIRED,
        ),
        (b"ERROR: Video unavailable", ErrorCode.CONTENT_UNAVAILABLE),
        (
            b"ERROR: Requested format is not available. Use --list-formats",
            ErrorCode.CONTENT_UNAVAILABLE,
        ),
        (b"ERROR: Unsupported URL", ErrorCode.ADAPTER_UNSUPPORTED),
        (b"ERROR: Temporary failure in name resolution", ErrorCode.NETWORK_ERROR),
        (b"ERROR: new extractor traceback token=secret", ErrorCode.EXTRACTOR_BROKEN),
    ],
)
def test_nonzero_exit_maps_to_stable_code_without_stderr_leak(
    tmp_path: Path,
    stderr: bytes,
    expected: ErrorCode,
) -> None:
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        command_result(returncode=1, stderr=stderr),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(), make_context(tmp_path))

    assert caught.value.code is expected
    assert "secret" not in caught.value.diagnostic
    assert stderr.decode() not in caught.value.diagnostic


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (CommandCancelled("credential=secret"), ErrorCode.WORKER_LOST),
        (CommandTimedOut("credential=secret"), ErrorCode.WORKER_TIMEOUT),
        (
            CommandOutputLimitExceeded("credential=secret"),
            ErrorCode.EXTRACTOR_BROKEN,
        ),
    ],
)
def test_runner_control_failures_have_stable_sanitized_codes(
    tmp_path: Path,
    failure: BaseException,
    expected: ErrorCode,
) -> None:
    runner = FakeRunner(failure)
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(), make_context(tmp_path))

    assert caught.value.code is expected
    assert "secret" not in caught.value.diagnostic


def test_expired_context_deadline_prevents_process_execution(tmp_path: Path) -> None:
    runner = FakeRunner(command_result(stdout=b"should-not-run"))
    context = make_context(tmp_path)
    expired = AdapterContext(
        worker_id=context.worker_id,
        attempt_id=context.attempt_id,
        temporary_dir=context.temporary_dir,
        deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(make_probe_request(), expired)

    assert caught.value.code is ErrorCode.WORKER_TIMEOUT
    assert runner.calls == []


def test_request_must_match_fixed_height_policy_before_execution(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(command_result(stdout=b"should-not-run"))
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]
    request = ProbeRequest(
        job_id="job-1",
        canonical_url="https://www.youtube.com/watch?v=stable-id",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        max_height=720,
    )

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(request, make_context(tmp_path))

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert runner.calls == []


@pytest.mark.parametrize(
    "expected_media_keys",
    [(), ("duplicate", "duplicate"), (" leading-space",), ("x" * 257,)],
)
def test_download_expectations_are_validated_before_execution(
    tmp_path: Path,
    expected_media_keys: tuple[str, ...],
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(
        context,
        expected_media_keys=expected_media_keys,
    )
    runner = FakeRunner(command_result(stdout=b"should-not-run"))
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert runner.calls == []


@pytest.mark.parametrize("max_items", [0, 51])
def test_probe_item_limit_is_strictly_bounded_before_execution(
    tmp_path: Path,
    max_items: int,
) -> None:
    runner = FakeRunner(command_result(stdout=b"should-not-run"))
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(
            make_probe_request(max_items=max_items),
            make_context(tmp_path),
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert runner.calls == []


def test_download_executes_once_and_classifies_controlled_outputs(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_outputs(spec: CommandSpec) -> CommandResult:
        assert spec.arguments.count("--retries") == 1
        assert spec.arguments[spec.arguments.index("--retries") + 1] == "0"
        original = request.output_dir / "video-123.mkv"
        original.write_bytes(b"video")
        (request.output_dir / "video-123.thumbnail.webp").write_bytes(b"thumb")
        (request.output_dir / "video-123.caption.en.vtt").write_bytes(b"WEBVTT\n")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result(stdout=b"download progress without secrets")

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19\n"),
        write_outputs,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]
    updates = []

    result = adapter.download(
        request,
        context,
        updates.append,
        NeverCancelled(),
    )

    assert len(runner.calls) == 2
    assert runner.calls[0][1] is not None
    assert runner.calls[1][1] is not None
    assert [update.fraction for update in updates] == [0.0, 1.0]
    assert updates[-1].downloaded_bytes == len(b"video") + len(b"thumb") + len(b"WEBVTT\n")
    assert [item.path.name for item in result.files] == ["video-123.mkv"]
    assert [item.path.name for item in result.thumbnails] == [
        "video-123.thumbnail.webp"
    ]
    assert [item.path.name for item in result.captions] == [
        "video-123.caption.en.vtt"
    ]
    assert result.files[0].media_key == "video-123"
    assert result.thumbnails[0].media_key == "video-123"
    assert result.captions[0].media_key == "video-123"
    assert result.files[0].role == "original"
    assert result.thumbnails[0].role == "thumbnail"
    assert result.captions[0].role == "caption"
    assert all(
        item.path.is_relative_to(request.output_dir.resolve())
        for group in (result.files, result.thumbnails, result.captions)
        for item in group
    )
    assert not (context.temporary_dir / "yt-dlp-after-move.jsonl").exists()


def test_photo_only_download_treats_images_as_original_media(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_photo(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.jpg"
        original.write_bytes(b"photo")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_photo,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    result = adapter.download(
        request,
        context,
        lambda update: None,
        NeverCancelled(),
    )

    assert result.files[0].media_kind == "image"
    assert result.thumbnails == ()


@pytest.mark.parametrize("unsafe_name", ["leftover.part", "raw.info.json"])
def test_download_rejects_unexpected_output_types(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_unsafe(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        (request.output_dir / unsafe_name).write_bytes(b"unexpected")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_unsafe,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(
            request,
            context,
            lambda update: None,
            NeverCancelled(),
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_download_rejects_nonregular_output_entry(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_directory(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        (request.output_dir / "unexpected").mkdir()
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_directory,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(
            request,
            context,
            lambda update: None,
            NeverCancelled(),
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_download_rejects_stale_mapping_before_runner_can_append(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)
    mapping_path = context.temporary_dir / "yt-dlp-after-move.jsonl"
    stale = "stale-mapping-secret"
    mapping_path.write_text(stale, encoding="utf-8")
    runner = FakeRunner(command_result(stdout=b"2026.08.19"))
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert stale not in caught.value.diagnostic
    assert mapping_path.read_text(encoding="utf-8") == stale
    assert len(runner.calls) == 1


def test_download_requires_nonempty_mapping_and_deletes_empty_control_file(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        command_result(),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.EXTRACTOR_BROKEN
    assert not (context.temporary_dir / "yt-dlp-after-move.jsonl").exists()


def test_download_mapping_is_bounded_even_with_injected_runner(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_mapping_over_limit(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_mapping_over_limit,
    )
    adapter = YtDlpAdapter(
        factory=make_factory(tmp_path),
        runner=runner,  # type: ignore[arg-type]
        mapping_limit_bytes=32,
    )

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.EXTRACTOR_BROKEN
    assert not (context.temporary_dir / "yt-dlp-after-move.jsonl").exists()


def test_download_deletes_mapping_when_subprocess_is_interrupted(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        CommandTimedOut("signed-url-secret"),
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.WORKER_TIMEOUT
    assert "secret" not in caught.value.diagnostic
    assert not (context.temporary_dir / "yt-dlp-after-move.jsonl").exists()


@pytest.mark.parametrize(
    "records",
    [
        ({"id": "wrong-id", "filepath": "{original}"},),
        (
            {"id": "video-123", "filepath": "{original}"},
            {"id": "video-123", "filepath": "{original}"},
        ),
        (
            {
                "id": "video-123",
                "filepath": "{original}",
                "unexpected": "secret",
            },
        ),
    ],
)
def test_download_rejects_wrong_duplicate_or_expanded_mapping_records(
    tmp_path: Path,
    records: tuple[dict[str, str], ...],
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_invalid_mapping(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        rendered = tuple(
            {
                key: value.replace("{original}", str(original.resolve()))
                for key, value in record.items()
            }
            for record in records
        )
        append_mapping(spec, *rendered)
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_invalid_mapping,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code in {
        ErrorCode.EXTRACTOR_BROKEN,
        ErrorCode.VALIDATION_FAILED,
    }
    assert "secret" not in caught.value.diagnostic
    assert not (context.temporary_dir / "yt-dlp-after-move.jsonl").exists()


def test_download_rejects_mapping_to_file_outside_output(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)
    outside = (tmp_path / "outside.mp4").resolve()

    def write_outside_mapping(spec: CommandSpec) -> CommandResult:
        outside.write_bytes(b"outside")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(outside)},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_outside_mapping,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_download_maps_multiple_originals_and_sidecars_by_exact_expected_id(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(
        context,
        expected_media_keys=("first", "second"),
    )

    def write_multiple(spec: CommandSpec) -> CommandResult:
        first = request.output_dir / "first.mkv"
        second = request.output_dir / "second.jpg"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        (request.output_dir / "first.thumbnail.webp").write_bytes(b"thumb")
        (request.output_dir / "second.caption.en.vtt").write_bytes(b"caption")
        # Mapping order is deliberately reversed. Adapter output must follow
        # immutable probe expectation order.
        append_mapping(
            spec,
            {"id": "second", "filepath": str(second.resolve())},
            {"id": "first", "filepath": str(first.resolve())},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_multiple,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    result = adapter.download(
        request,
        context,
        lambda update: None,
        NeverCancelled(),
    )

    assert [(item.media_key, item.path.name) for item in result.files] == [
        ("first", "first.mkv"),
        ("second", "second.jpg"),
    ]
    assert [(item.media_key, item.path.name) for item in result.thumbnails] == [
        ("first", "first.thumbnail.webp")
    ]
    assert [(item.media_key, item.path.name) for item in result.captions] == [
        ("second", "second.caption.en.vtt")
    ]


@pytest.mark.parametrize(
    "extra_name",
    ["extra.mp4", "intruder.thumbnail.webp", "intruder.caption.en.vtt"],
)
def test_download_rejects_unowned_original_or_sidecar(
    tmp_path: Path,
    extra_name: str,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_unowned(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        (request.output_dir / extra_name).write_bytes(b"unowned")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_unowned,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_download_rejects_sidecar_with_ambiguous_original_owner(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(
        context,
        expected_media_keys=("first", "second"),
    )

    def write_ambiguous(spec: CommandSpec) -> CommandResult:
        first = request.output_dir / "a.mkv"
        second = request.output_dir / "a.thumbnail.mkv"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        (request.output_dir / "a.thumbnail.thumbnail.webp").write_bytes(b"thumb")
        append_mapping(
            spec,
            {"id": "first", "filepath": str(first.resolve())},
            {"id": "second", "filepath": str(second.resolve())},
        )
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_ambiguous,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_download_rejects_hardlinked_mapping_control_file(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def hardlink_mapping(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        mapping_path = append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        try:
            os.link(mapping_path, context.temporary_dir / "mapping-copy.jsonl")
        except OSError:
            pytest.skip("hard links unavailable on this test filesystem")
        return command_result()

    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        hardlink_mapping,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_fstat_detects_lstat_open_swap_and_cleanup_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)
    control_path = context.temporary_dir / "yt-dlp-after-move.jsonl"

    def write_valid(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result()

    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        is_read = flags & (os.O_WRONLY | os.O_RDWR) == 0
        if Path(path) == control_path and is_read and not swapped:
            swapped = True
            control_path.replace(context.temporary_dir / "original-control.jsonl")
            control_path.write_text("replacement-secret", encoding="utf-8")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(yt_dlp_module.os, "open", swapping_open)
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_valid,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert swapped is True
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert "replacement-secret" not in caught.value.diagnostic
    assert control_path.read_text(encoding="utf-8") == "replacement-secret"


def test_fstat_detects_mapping_size_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = make_context(tmp_path)
    request = make_download_request(context)

    def write_valid(spec: CommandSpec) -> CommandResult:
        original = request.output_dir / "video-123.mp4"
        original.write_bytes(b"video")
        append_mapping(
            spec,
            {"id": "video-123", "filepath": str(original.resolve())},
        )
        return command_result()

    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        info = real_fstat(descriptor)
        if calls != 3:
            return info
        return SimpleNamespace(
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_mode=info.st_mode,
            st_nlink=info.st_nlink,
            st_size=info.st_size + 1,
        )

    monkeypatch.setattr(yt_dlp_module.os, "fstat", changing_fstat)
    runner = FakeRunner(
        command_result(stdout=b"2026.08.19"),
        write_valid,
    )
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, lambda update: None, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert "changed" in caught.value.diagnostic


def test_download_maps_command_factory_path_rejection_to_adapter_failure(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    request = DownloadRequest(
        job_id="job-1",
        source_item_id="source-item-1",
        canonical_url="https://www.youtube.com/watch?v=stable-id",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        output_dir=outside,
        expected_media_keys=("video-123",),
    )
    runner = FakeRunner(command_result(stdout=b"2026.08.19"))
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]
    updates = []

    with pytest.raises(AdapterFailure) as caught:
        adapter.download(request, context, updates.append, NeverCancelled())

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert len(runner.calls) == 1
    assert updates == []


def test_credential_reference_fails_closed_without_resolver(tmp_path: Path) -> None:
    runner = FakeRunner(command_result(stdout=b"2026.08.19"))
    adapter = YtDlpAdapter(factory=make_factory(tmp_path), runner=runner)  # type: ignore[arg-type]
    request = ProbeRequest(
        job_id="job-1",
        canonical_url="https://www.youtube.com/watch?v=stable-id",
        platform=Platform.YOUTUBE,
        source_type=SourceType.YOUTUBE_VIDEO,
        credential_ref="credential-ref-secret",
    )

    with pytest.raises(AdapterFailure) as caught:
        adapter.probe(request, make_context(tmp_path))

    assert caught.value.code is ErrorCode.AUTHENTICATION_REQUIRED
    assert "credential-ref-secret" not in caught.value.diagnostic


def test_constructor_rejects_unbounded_or_invalid_limits(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = FakeRunner()

    with pytest.raises(ValueError, match="finite positive"):
        YtDlpAdapter(
            factory=factory,
            runner=runner,  # type: ignore[arg-type]
            download_timeout_seconds=float("inf"),
        )
    with pytest.raises(ValueError, match="positive integer"):
        YtDlpAdapter(
            factory=factory,
            runner=runner,  # type: ignore[arg-type]
            probe_stdout_limit_bytes=0,
        )
    with pytest.raises(ValueError, match="upper bound"):
        YtDlpAdapter(
            factory=factory,
            runner=runner,  # type: ignore[arg-type]
            mapping_limit_bytes=(1024 * 1024) + 1,
        )

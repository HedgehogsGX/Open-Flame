from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from video_download_control.adapters import DirectEgress, YtDlpCommandFactory
from video_download_control.adapters.base import DownloadRequest
from video_download_control.domain import Platform, SourceType
from video_download_control.toolchain import load_toolchain_lock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCAL_BUNDLE_ROOT = REPOSITORY_ROOT / "runtime-tools" / "windows-x64"
PINNED_YT_DLP_VERSION = "2026.08.19"
PINNED_YT_DLP_SIZE = 3_072_469
PINNED_YT_DLP_SHA256 = (
    "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"
)

_EVALUATOR_SCRIPT = r"""
import json
import sys

entrypoint, download_option, postprocess_option = sys.argv[1:]
sys.path.insert(0, entrypoint)

from yt_dlp import YoutubeDL
from yt_dlp.options import parseOpts
from yt_dlp.version import __version__

_, options, urls = parseOpts([
    "--ignore-config",
    "--no-plugin-dirs",
    "--progress-template",
    download_option,
    "--progress-template",
    postprocess_option,
    "--",
    "https://example.invalid/video",
])
templates = options.progress_template
cases = {
    "main_sequential": {
        "info": {
            "id": "do-not-emit-media-id",
            "format_id": "do-not-emit-format-id",
        },
        "progress": {
            "status": "downloading",
            "downloaded_bytes": 1024,
            "total_bytes_estimate": 4096.0,
        },
    },
    "sidecar": {
        "info": {
            "url": "https://example.invalid/caption.vtt?token=do-not-emit-token",
            "ext": "vtt",
        },
        "progress": {
            "status": "downloading",
            "downloaded_bytes": 100,
            "total_bytes": 100,
        },
    },
    "main_indexed": {
        "info": {
            "id": "do-not-emit-media-id",
            "format_id": "do-not-emit-format-id",
        },
        "progress": {
            "status": "downloading",
            "downloaded_bytes": 512,
            "total_bytes_estimate": 1024.0,
            "progress_idx": 0,
            "max_progress": 2,
        },
    },
}

with YoutubeDL(
    {"quiet": True, "no_warnings": True, "noprogress": True},
    auto_init=False,
) as ydl:
    lines = {
        name: ydl.evaluate_outtmpl(templates["download"], data)
        for name, data in cases.items()
    }
    lines["postprocessing"] = ydl.evaluate_outtmpl(
        templates["postprocess"],
        {"info": {}, "progress": {}},
    )

print(json.dumps({
    "version": __version__,
    "template_keys": sorted(templates),
    "urls": urls,
    "lines": lines,
}, sort_keys=True))
""".lstrip()


def _pinned_bundle_entrypoint() -> Path:
    if not LOCAL_BUNDLE_ROOT.exists():
        pytest.skip(
            "pinned local yt-dlp runtime bundle is not installed; "
            "the contract test never downloads it"
        )
    assert LOCAL_BUNDLE_ROOT.is_dir(), "local runtime bundle root is not a directory"

    lock_path = LOCAL_BUNDLE_ROOT / "bundle-lock.json"
    assert lock_path.is_file(), "local runtime bundle is incomplete: lock is missing"
    lock = load_toolchain_lock(lock_path)
    assert lock.yt_dlp.version == PINNED_YT_DLP_VERSION
    assert lock.yt_dlp.execution == "python-zipimport"
    assert lock.yt_dlp.artifact.size == PINNED_YT_DLP_SIZE
    assert lock.yt_dlp.artifact.sha256 == PINNED_YT_DLP_SHA256

    entrypoint = LOCAL_BUNDLE_ROOT / lock.yt_dlp.entrypoint
    assert not entrypoint.is_symlink(), "yt-dlp bundle entrypoint must not be a symlink"
    assert entrypoint.is_file(), "local runtime bundle is incomplete: yt-dlp is missing"
    assert entrypoint.stat().st_size == PINNED_YT_DLP_SIZE
    with entrypoint.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    assert digest == PINNED_YT_DLP_SHA256

    resolved_root = LOCAL_BUNDLE_ROOT.resolve(strict=True)
    resolved_entrypoint = entrypoint.resolve(strict=True)
    assert resolved_entrypoint.is_relative_to(resolved_root)
    return resolved_entrypoint


def _generated_progress_templates(tmp_path: Path, entrypoint: Path) -> tuple[str, str]:
    ffmpeg_directory = (tmp_path / "ffmpeg").resolve()
    ffmpeg_directory.mkdir()
    attempt = (tmp_path / "attempt").resolve()
    output = attempt / "output"
    output.mkdir(parents=True)
    factory = YtDlpCommandFactory(
        executable=Path(sys.executable).resolve(),
        zipimport_entrypoint=entrypoint,
        ffmpeg_directory=ffmpeg_directory,
        expected_version=PINNED_YT_DLP_VERSION,
        egress=DirectEgress(),
    )
    command = factory.download_command(
        DownloadRequest(
            job_id="bundle-contract-job",
            source_item_id="bundle-contract-source",
            canonical_url="https://example.invalid/video",
            platform=Platform.YOUTUBE,
            source_type=SourceType.YOUTUBE_VIDEO,
            output_dir=output,
            expected_media_keys=("bundle-contract-media",),
        ),
        temporary_root=attempt,
    )
    templates = tuple(
        command.arguments[index + 1]
        for index, argument in enumerate(command.arguments)
        if argument == "--progress-template"
    )
    assert len(templates) == 2
    return templates


def test_pinned_bundle_evaluates_generated_progress_control_lines(
    tmp_path: Path,
) -> None:
    entrypoint = _pinned_bundle_entrypoint()
    download_template, postprocess_template = _generated_progress_templates(
        tmp_path,
        entrypoint,
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _EVALUATOR_SCRIPT,
            str(entrypoint),
            download_template,
            postprocess_template,
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    payload = json.loads(completed.stdout.decode("utf-8"))

    assert payload["version"] == PINNED_YT_DLP_VERSION
    assert payload["template_keys"] == ["download", "postprocess"]
    assert payload["urls"] == ["https://example.invalid/video"]
    assert payload["lines"] == {
        "main_sequential": (
            'VDC_PROGRESS|1|1|"downloading"|1024|NA|4096.0|NA|NA'
        ),
        "sidecar": 'VDC_PROGRESS|0|0|"downloading"|100|100|NA|NA|NA',
        "main_indexed": (
            'VDC_PROGRESS|1|1|"downloading"|512|NA|1024.0|0|2'
        ),
        "postprocessing": "VDC_PHASE|postprocessing",
    }
    serialized_lines = json.dumps(payload["lines"], sort_keys=True)
    assert "do-not-emit-media-id" not in serialized_lines
    assert "do-not-emit-format-id" not in serialized_lines
    assert "do-not-emit-token" not in serialized_lines

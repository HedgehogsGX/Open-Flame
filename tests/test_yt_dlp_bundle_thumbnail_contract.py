from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from video_download_control.adapters import DirectEgress, YtDlpCommandFactory
from video_download_control.adapters.base import AdapterContext, AdapterFailure, DownloadRequest
from video_download_control.adapters import yt_dlp as adapter_module
from video_download_control.assets import AssetStore, AssetValidationError
from video_download_control.domain import ErrorCode, Platform, SourceType
from video_download_control.toolchain import load_toolchain_lock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCAL_BUNDLE_ROOT = REPOSITORY_ROOT / "runtime-tools" / "windows-x64"
PINNED_YT_DLP_VERSION = "2026.08.19"
PINNED_YT_DLP_SHA256 = "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"


# Exercise the installed parser, postprocessor, deletion and after_move printer.
# No extractor, HTTP request, real media, account or third-party Python package is used.
_CONVERTER_SCRIPT = r"""
import json
import socket
import sys
from pathlib import Path

payload = json.load(sys.stdin)
sys.dont_write_bytecode = True
sys.path.insert(0, payload['entrypoint'])

import yt_dlp
from yt_dlp.utils import PostProcessingError
from yt_dlp.version import __version__

def network_forbidden(*args, **kwargs):
    raise AssertionError('offline thumbnail contract attempted a network request')

socket.socket.connect = network_forbidden
socket.create_connection = network_forbidden
yt_dlp.YoutubeDL.urlopen = network_forbidden

class QuietLogger:
    def debug(self, message): pass
    def info(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass

results = []
for case in payload['cases']:
    parsed = yt_dlp.parse_options(case['arguments'])
    options = parsed.ydl_opts
    options.update(logger=QuietLogger(), quiet=True, no_warnings=True, noprogress=True)
    thumbnail_processors = [p for p in options['postprocessors']
                            if p['key'] == 'FFmpegThumbnailsConvertor']
    original = case['original']
    thumbnail = case['thumbnail']
    info = {
        'id': 'synthetic-media', 'title': 'synthetic fixture', 'ext': 'mp4',
        'filepath': original, 'filename': original,
        'thumbnails': [{'id': '0', 'filepath': thumbnail,
                        'url': 'https://example.invalid/thumbnail'}],
        '__files_to_move': {thumbnail: thumbnail},
    }
    with yt_dlp.YoutubeDL(options, auto_init=False) as ydl:
        try:
            info = ydl.run_all_pps('before_dl', info)
        except PostProcessingError:
            results.append({'status': 'postprocessing_failed',
                            'processors': thumbnail_processors})
            continue
        # Fixtures already occupy their final attempt-output paths. Invoke the
        # genuine after_move printer instead of hand-writing an adapter mapping.
        ydl.run_all_pps('after_move', info)
        results.append({'status': 'completed', 'processors': thumbnail_processors,
                        'thumbnail_name': Path(info['thumbnails'][0]['filepath']).name})
print(json.dumps({'version': __version__, 'results': results}, sort_keys=True))
""".lstrip()


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _subprocess_options() -> dict:
    # No proxies, account settings, FFREPORT destination or arbitrary process secrets.
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    return {
        "env": environment,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        "capture_output": True,
        "check": False,
        "timeout": 30,
    }


def _assert_subprocess_success(completed: subprocess.CompletedProcess) -> None:
    if completed.returncode:
        lines = completed.stderr.decode("utf-8", errors="replace").splitlines()
        # Show the decisive exception, not a command/env repr or private stack paths.
        detail = next((line.strip() for line in reversed(lines) if line.strip()), "no stderr")
        for root in (REPOSITORY_ROOT, Path(os.environ.get("TEMP", "."))):
            detail = detail.replace(str(root), "<test-root>")
        pytest.fail(f"offline fixture process exited {completed.returncode}: {detail[-500:]}", pytrace=False)


@pytest.fixture(scope="module")
def pinned_tools() -> tuple[Path, Path]:
    if os.name != "nt":
        pytest.skip("thumbnail contract requires the pinned Windows FFmpeg bundle")
    if not LOCAL_BUNDLE_ROOT.exists():
        pytest.skip("pinned local media bundle is not installed; this test never downloads it")
    assert LOCAL_BUNDLE_ROOT.is_dir()
    lock = load_toolchain_lock()
    installed = load_toolchain_lock(LOCAL_BUNDLE_ROOT / "bundle-lock.json")
    assert installed.raw_bytes == lock.raw_bytes, "installed bundle lock differs from checked-in pin"
    assert lock.yt_dlp.version == PINNED_YT_DLP_VERSION
    assert lock.yt_dlp.execution == "python-zipimport"
    assert lock.yt_dlp.artifact.sha256 == PINNED_YT_DLP_SHA256
    root = LOCAL_BUNDLE_ROOT.resolve(strict=True)
    entrypoint = root / lock.yt_dlp.entrypoint
    # Verify the actual executable and all shared codec DLLs, not just a version string.
    payload = [(entrypoint, lock.yt_dlp.artifact.size, PINNED_YT_DLP_SHA256)]
    payload.extend((root / item.install_path, item.size, item.sha256) for item in lock.ffmpeg.payload)
    for path, size, sha256 in payload:
        assert path.is_file() and not path.is_symlink(), "installed bundle payload is incomplete"
        assert path.resolve(strict=True).is_relative_to(root)
        assert path.stat().st_size == size
        assert _digest(path) == sha256, "installed bundle payload hash mismatch"
    return entrypoint, root / "ffmpeg" / "bin" / "ffmpeg.exe"


def _case(tmp_path: Path, pinned_tools: tuple[Path, Path], *, label: str, suffix: str,
          owner: str = "synthetic-media") -> dict:
    entrypoint, ffmpeg = pinned_tools
    attempt = (tmp_path / label).resolve()
    output = attempt / "output"
    output.mkdir(parents=True)
    context = AdapterContext(
        worker_id="thumbnail-contract-worker", attempt_id=label, temporary_dir=attempt,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    request = DownloadRequest(
        job_id="thumbnail-contract-job", source_item_id="thumbnail-contract-source",
        canonical_url="https://example.invalid/video", platform=Platform.TIKTOK,
        source_type=SourceType.TIKTOK_VIDEO, output_dir=output,
        expected_media_keys=("synthetic-media",),
    )
    command = YtDlpCommandFactory(
        executable=Path(sys.executable).resolve(), zipimport_entrypoint=entrypoint,
        ffmpeg_directory=ffmpeg.parent, expected_version=PINNED_YT_DLP_VERSION,
        egress=DirectEgress(),
    ).download_command(request, temporary_root=attempt)
    assert command.arguments.count("--convert-thumbnails") == 1, (
        "production download command must normalize ambiguous .image thumbnails"
    )
    assert command.arguments[command.arguments.index("--convert-thumbnails") + 1] == "image>png"
    control_file = adapter_module._prepare_mapping_file(command, context=context, output_dir=output)
    original = output / "synthetic-media.mp4"
    # An opaque stand-in is sufficient here: this seam must never decode/recode main media.
    original.write_bytes(b"synthetic original media must remain byte-identical\n")
    return {
        "request": request, "control_file": control_file, "original": original,
        "thumbnail": output / f"{owner}.thumbnail.{suffix}",
        "arguments": list(command.arguments[1:]),
    }


def _convert(pinned_tools: tuple[Path, Path], cases: list[dict]) -> list[dict]:
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _CONVERTER_SCRIPT],
        input=json.dumps({
            "entrypoint": str(pinned_tools[0]),
            "cases": [{"arguments": case["arguments"], "original": str(case["original"]),
                       "thumbnail": str(case["thumbnail"])} for case in cases],
        }).encode("utf-8"),
        **_subprocess_options(),
    )
    _assert_subprocess_success(completed)
    result = json.loads(completed.stdout)
    assert result["version"] == PINNED_YT_DLP_VERSION
    for row in result["results"]:
        assert row["processors"] == [{"key": "FFmpegThumbnailsConvertor", "format": "image>png",
                                       "when": "before_dl"}]
    return result["results"]


def _synthetic_image(ffmpeg: Path, codec: str) -> bytes:
    pixels = bytes(component for y in range(16) for x in range(16)
                   for component in (x * 16, y * 16, (x + y) * 8))
    source = b"P6\n16 16\n255\n" + pixels
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "image2pipe",
         "-i", "pipe:0", "-frames:v", "1", "-c:v", codec,
         "-f", "image2pipe", "pipe:1"],
        input=source, **_subprocess_options(),
    )
    _assert_subprocess_success(result)
    assert result.stdout
    return result.stdout


def _rgb_pixels(ffmpeg: Path, source: bytes) -> bytes:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "image2pipe",
         "-i", "pipe:0", "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        input=source, **_subprocess_options(),
    )
    _assert_subprocess_success(result)
    assert len(result.stdout) == 16 * 16 * 3
    return result.stdout


def _mappings(case: dict):
    return adapter_module._read_download_mapping(
        case["control_file"], expected_media_keys=case["request"].expected_media_keys,
        output_dir=case["request"].output_dir, max_bytes=8192,
    )


def test_pinned_converter_normalizes_ambiguous_thumbnail_and_preserves_regular_formats(
    tmp_path: Path, pinned_tools: tuple[Path, Path],
) -> None:
    cases = [_case(tmp_path, pinned_tools, label=label, suffix=suffix)
             for label, suffix in (("jpeg-image", "image"), ("regular-jpg", "jpg"),
                                   ("regular-png", "png"), ("regular-webp", "webp"),
                                   ("webp-image", "image"))]
    ffmpeg = pinned_tools[1]
    jpeg = _synthetic_image(ffmpeg, "mjpeg")
    png = _synthetic_image(ffmpeg, "png")
    webp = _synthetic_image(ffmpeg, "libwebp")
    payloads = [jpeg, jpeg, png, webp, webp]
    originals = [_digest(case["original"]) for case in cases]
    for case, payload in zip(cases, payloads, strict=True):
        case["thumbnail"].write_bytes(payload)
    rows = _convert(pinned_tools, cases)
    expected_suffixes = ["png", "jpg", "png", "webp", "webp"]
    for index, (case, row, suffix) in enumerate(zip(cases, rows, expected_suffixes, strict=True)):
        assert row["status"] == "completed"
        thumbnail = case["request"].output_dir / f"synthetic-media.thumbnail.{suffix}"
        assert row["thumbnail_name"] == thumbnail.name
        mappings = _mappings(case)
        result = adapter_module._classify_output(case["request"], mappings)
        assert len(result.files) == len(result.thumbnails) == 1
        assert result.files[0].path == case["original"]
        assert result.thumbnails[0].path == thumbnail
        assert result.thumbnails[0].media_key == result.files[0].media_key == "synthetic-media"
        auxiliary = AssetStore.describe_auxiliary_file(path=thumbnail, kind="thumbnail", ordinal=0)
        assert auxiliary.mime_type == {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[suffix]
        assert _digest(case["original"]) == originals[index]
        assert not list(case["request"].output_dir.glob("*.image"))
        if index == 0:
            assert thumbnail.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert _rgb_pixels(ffmpeg, thumbnail.read_bytes()) == _rgb_pixels(ffmpeg, jpeg)
        else:
            assert thumbnail.read_bytes() == payloads[index]


def test_undecodable_image_fails_before_after_move_mapping(
    tmp_path: Path, pinned_tools: tuple[Path, Path],
) -> None:
    case = _case(tmp_path, pinned_tools, label="undecodable", suffix="image")
    case["thumbnail"].write_bytes(b"synthetic invalid image, not a codec payload\n")
    original_hash = _digest(case["original"])
    assert _convert(pinned_tools, [case])[0]["status"] == "postprocessing_failed"
    assert case["control_file"].path.read_bytes() == b""
    assert not list(case["request"].output_dir.glob("*.png"))
    assert _digest(case["original"]) == original_hash
    with pytest.raises(AdapterFailure):
        _mappings(case)


@pytest.mark.parametrize(("owner", "suffix"), [("synthetic-media", "unknown"), ("unowned", "image")])
def test_conversion_does_not_expand_output_types_or_sidecar_ownership(
    tmp_path: Path, pinned_tools: tuple[Path, Path], owner: str, suffix: str,
) -> None:
    case = _case(tmp_path, pinned_tools, label="rejected-sidecar", suffix=suffix, owner=owner)
    case["thumbnail"].write_bytes(_synthetic_image(pinned_tools[1], "png"))
    assert _convert(pinned_tools, [case])[0]["status"] == "completed"
    with pytest.raises(AdapterFailure) as caught:
        adapter_module._classify_output(case["request"], _mappings(case))
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert ".image" not in adapter_module._KNOWN_OUTPUT_EXTENSIONS
    assert ".unknown" not in adapter_module._KNOWN_OUTPUT_EXTENSIONS
    for disallowed in ("image", "unknown"):
        with pytest.raises(AssetValidationError, match="thumbnail extension is not allowed"):
            AssetStore.describe_auxiliary_file(
                path=case["request"].output_dir / f"synthetic-media.thumbnail.{disallowed}",
                kind="thumbnail", ordinal=0,
            )

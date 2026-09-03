from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_download_control.assets import AssetValidationError
from video_download_control.subprocess_runner import CommandResult
from video_download_control.verifiers import FfprobeVerifier


class FakeRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        return self.results.pop(0)


def result(document: object, *, returncode: int = 0) -> CommandResult:
    return CommandResult(
        returncode=returncode,
        stdout=json.dumps(document).encode(),
        stderr=b"redacted by verifier boundary",
        duration_seconds=0.01,
    )


def verifier(fake: FakeRunner) -> FfprobeVerifier:
    return FfprobeVerifier(
        executable=Path("C:/tools/ffprobe.exe"),
        runner=fake,  # type: ignore[arg-type]
        expected_version="8.0.1",
    )


def test_video_verification_accepts_silent_video(tmp_path: Path) -> None:
    media = tmp_path / "silent.mp4"
    media.write_bytes(b"not inspected by fake runner")
    fake = FakeRunner(
        [
            result(
                {
                    "format": {"format_name": "mov,mp4", "duration": "3.25"},
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1920,
                            "height": 1080,
                        }
                    ],
                }
            )
        ]
    )
    verified = verifier(fake).verify(media.resolve(), "video")
    assert verified.container == "mov,mp4"
    assert verified.codec == "h264"
    assert verified.duration_seconds == 3.25
    assert (verified.width, verified.height) == (1920, 1080)
    assert "-show_streams" in fake.specs[0].arguments
    assert fake.specs[0].arguments[-1] == str(media.resolve())


def test_audio_and_image_require_their_expected_stream_type(tmp_path: Path) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"x")
    audio = verifier(
        FakeRunner(
            [
                result(
                    {
                        "format": {"format_name": "mp3"},
                        "streams": [
                            {"codec_type": "audio", "codec_name": "mp3"}
                        ],
                    }
                )
            ]
        )
    ).verify(media.resolve(), "audio")
    assert audio.codec == "mp3"

    image = verifier(
        FakeRunner(
            [
                result(
                    {
                        "format": {"format_name": "image2", "duration": "1"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "png",
                                "width": 10,
                                "height": 20,
                            }
                        ],
                    }
                )
            ]
        )
    ).verify(media.resolve(), "image")
    assert image.duration_seconds is None
    assert image.codec == "png"


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"format": {"format_name": "mp4"}, "streams": []},
        {
            "format": {"format_name": "mp4"},
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        },
    ],
)
def test_malformed_or_wrong_stream_output_is_rejected(
    tmp_path: Path, document: object
) -> None:
    media = tmp_path / "bad.mp4"
    media.write_bytes(b"x")
    with pytest.raises(AssetValidationError):
        verifier(FakeRunner([result(document)])).verify(media.resolve(), "video")


def test_nonzero_ffprobe_exit_does_not_expose_stderr(tmp_path: Path) -> None:
    media = tmp_path / "bad.mp4"
    media.write_bytes(b"x")
    fake = FakeRunner(
        [
            CommandResult(
                returncode=1,
                stdout=b"",
                stderr=b"https://signed.example/file?token=secret",
                duration_seconds=0.01,
            )
        ]
    )
    with pytest.raises(AssetValidationError) as exc_info:
        verifier(fake).verify(media.resolve(), "video")
    assert "secret" not in str(exc_info.value)


def test_runtime_version_pin_must_match() -> None:
    good = FakeRunner(
        [
            CommandResult(
                0,
                b"ffprobe version 8.0.1 Copyright FFmpeg\n",
                b"",
                0.01,
            )
        ]
    )
    verifier(good).validate_runtime()

    bad = FakeRunner(
        [
            CommandResult(
                0,
                b"ffprobe version 7.1 Copyright FFmpeg\n",
                b"",
                0.01,
            )
        ]
    )
    with pytest.raises(AssetValidationError, match="version"):
        verifier(bad).validate_runtime()

    prefix_collision = FakeRunner(
        [
            CommandResult(
                0,
                b"ffprobe version 8.0.10 Copyright FFmpeg\n",
                b"",
                0.01,
            )
        ]
    )
    with pytest.raises(AssetValidationError, match="version"):
        verifier(prefix_collision).validate_runtime()

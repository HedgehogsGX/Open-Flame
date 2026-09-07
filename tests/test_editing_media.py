from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image, ImageFont

from video_download_control.editing.contracts import (
    CoverSpec,
    DubbingSpec,
    EditRecipe,
    EditingError,
    SegmentSpec,
    TranslationSpec,
)
from video_download_control.editing import media as editing_media
from video_download_control.editing.media import MediaProcessor
from video_download_control.subprocess_runner import (
    CommandCancelled,
    CommandResult,
    CommandSpec,
    SecureSubprocessRunner,
)


def _probe_document(
    *,
    duration: str = "10.000",
    width: int = 640,
    height: int = 360,
    container: str = "mov,mp4,m4a,3gp,3g2,mj2",
    video_codec: str = "h264",
    audio: bool = True,
) -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "codec_type": "video",
            "codec_name": video_codec,
            "width": width,
            "height": height,
        }
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {
        "format": {"format_name": container, "duration": duration},
        "streams": streams,
    }


def _result(document: object, *, returncode: int = 0, stderr: bytes = b"") -> CommandResult:
    return CommandResult(
        returncode=returncode,
        stdout=json.dumps(document).encode("utf-8"),
        stderr=stderr,
        duration_seconds=0.01,
    )


class FakeRunner:
    def __init__(
        self,
        *,
        source_document: object | None = None,
        fail_ffmpeg_at: int | None = None,
        cancel_on_ffmpeg: bool = False,
        invalid_cover: bool = False,
        oriented_cover: bool = False,
        segment_container: str = "mov,mp4,m4a,3gp,3g2,mj2",
        segment_video_codec: str = "h264",
        segment_audio: bool = True,
        segment_duration_offset: float = 0.0,
    ) -> None:
        self.source_document = source_document or _probe_document()
        self.fail_ffmpeg_at = fail_ffmpeg_at
        self.cancel_on_ffmpeg = cancel_on_ffmpeg
        self.invalid_cover = invalid_cover
        self.oriented_cover = oriented_cover
        self.segment_container = segment_container
        self.segment_video_codec = segment_video_codec
        self.segment_audio = segment_audio
        self.segment_duration_offset = segment_duration_offset
        self.specs: list[CommandSpec] = []
        self.cancel_checks: list[object] = []
        self.ffmpeg_calls = 0

    def run(self, spec: CommandSpec, *, is_cancelled=None) -> CommandResult:
        self.specs.append(spec)
        self.cancel_checks.append(is_cancelled)
        if is_cancelled is not None and is_cancelled():
            raise CommandCancelled("cancelled by fake runner")
        if spec.executable.name == "ffprobe.exe":
            target = Path(spec.arguments[-1])
            if target.name.startswith("segment-"):
                duration = self._segment_duration(target)
                return _result(
                    _probe_document(
                        duration=f"{duration:.3f}",
                        container=self.segment_container,
                        video_codec=self.segment_video_codec,
                        audio=self.segment_audio,
                    )
                )
            return _result(self.source_document)

        assert spec.executable.name == "ffmpeg.exe"
        self.ffmpeg_calls += 1
        target = Path(spec.arguments[-1])
        if target.suffix == ".mp4":
            target.write_bytes(f"segment-{self.ffmpeg_calls}".encode("ascii"))
        elif self.invalid_cover:
            target.write_bytes(b"not-a-png")
        else:
            image = Image.new("RGB", (400, 200), (0, 255, 0))
            for x in range(100):
                for y in range(200):
                    image.putpixel((x, y), (255, 0, 0))
                    image.putpixel((399 - x, y), (0, 0, 255))
            if self.oriented_cover:
                exif = Image.Exif()
                exif[274] = 6
                image.save(target, format="PNG", exif=exif)
            else:
                image.save(target, format="PNG")
        if is_cancelled is not None and is_cancelled():
            raise CommandCancelled("cancelled by fake runner after output")
        if self.cancel_on_ffmpeg:
            owner = getattr(is_cancelled, "__self__", None)
            event = (
                owner
                if isinstance(owner, Event)
                else getattr(owner, "cancel_event", None)
            )
            if isinstance(event, Event):
                event.set()
            raise CommandCancelled("cancelled after partial output")
        if self.fail_ffmpeg_at == self.ffmpeg_calls:
            return CommandResult(
                returncode=1,
                stdout=b"",
                stderr=b"credential=must-not-escape",
                duration_seconds=0.01,
            )
        return CommandResult(0, b"", b"", 0.01)

    def _segment_duration(self, target: Path) -> float:
        for spec in reversed(self.specs):
            if spec.executable.name != "ffmpeg.exe" or spec.arguments[-1] != str(target):
                continue
            index = spec.arguments.index("-t")
            return float(spec.arguments[index + 1]) + self.segment_duration_offset
        raise AssertionError("segment probe did not follow its render command")


def _processor(tmp_path: Path, fake: FakeRunner) -> MediaProcessor:
    return MediaProcessor((tmp_path / "tools").resolve(), runner=fake)  # type: ignore[arg-type]


def _source(tmp_path: Path) -> Path:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"immutable-source")
    return source


def _output_dir(tmp_path: Path) -> Path:
    output = (tmp_path / "outputs").resolve()
    output.mkdir()
    return output


def test_probe_uses_pinned_ffprobe_with_fixed_bounded_arguments(tmp_path: Path) -> None:
    fake = FakeRunner()
    source = _source(tmp_path)
    source_before = source.read_bytes()
    processor = _processor(tmp_path, fake)

    probe = processor.probe(source)

    assert probe.duration_ms == 10_000
    assert (probe.width, probe.height) == (640, 360)
    assert probe.video_codec == "h264"
    assert probe.audio_codec == "aac"
    assert len(fake.specs) == 1
    spec = fake.specs[0]
    assert spec.executable == (
        tmp_path / "tools" / "ffmpeg" / "bin" / "ffprobe.exe"
    ).resolve()
    assert spec.arguments == (
        "-v",
        "error",
        "-f",
        "mov",
        "-protocol_whitelist",
        "file",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    )
    assert spec.cwd == source.parent
    assert spec.stdout_limit_bytes == 2 * 1024 * 1024
    assert spec.stderr_limit_bytes == 64 * 1024
    assert source.read_bytes() == source_before


def test_renamed_ffconcat_source_is_rejected_before_ffmpeg(
    tmp_path: Path,
) -> None:
    sibling = (tmp_path / "victim.mp4").resolve()
    sibling.write_bytes(b"unregistered-sibling")
    source = _source(tmp_path)
    source.write_text(
        "ffconcat version 1.0\nfile victim.mp4\nduration 1.0\n",
        encoding="ascii",
        newline="\n",
    )
    payload = source.read_bytes()
    output = _output_dir(tmp_path)
    fake = FakeRunner(source_document=_probe_document(duration="1.000", container="concat"))

    with pytest.raises(EditingError, match="^media_processing_failed$"):
        _processor(tmp_path, fake).render(
            source,
            output,
            EditRecipe(segments=(SegmentSpec(0, 800, "clip"),)),
            expected_source_size=len(payload),
            expected_source_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert [spec.executable.name for spec in fake.specs] == ["ffprobe.exe"]
    assert fake.specs[0].arguments[:6] == (
        "-v",
        "error",
        "-f",
        "mov",
        "-protocol_whitelist",
        "file",
    )
    assert list(output.iterdir()) == []
    assert source.read_bytes() == payload
    assert sibling.read_bytes() == b"unregistered-sibling"


def test_render_creates_verified_segments_and_center_cropped_titled_cover(
    tmp_path: Path,
) -> None:
    fake = FakeRunner()
    source = _source(tmp_path)
    source_before = source.read_bytes()
    output = _output_dir(tmp_path)
    recipe = EditRecipe(
        segments=(
            SegmentSpec(0, 1_250, "$(never execute this label)"),
            SegmentSpec(2_000, 4_500, "second"),
        ),
        cover=CoverSpec(
            timestamp_ms=3_000,
            aspect_ratio="1:1",
            title="$(never execute this title)",
        ),
    )

    processor = _processor(tmp_path, fake)
    result = processor.render(source, output, recipe)

    assert result.status == "ready"
    assert result.code == "render_complete"
    assert [asset.kind for asset in result.assets] == ["segment", "segment", "cover"]
    assert [asset.name for asset in result.assets] == [
        "segment-001.mp4",
        "segment-002.mp4",
        "cover.png",
    ]
    assert [asset.ordinal for asset in result.assets] == [1, 2, 1]
    for asset in result.assets:
        assert asset.path.is_file()
        assert asset.size_bytes == asset.path.stat().st_size > 0
        assert asset.sha256 == hashlib.sha256(asset.path.read_bytes()).hexdigest()
    first, second, cover = result.assets
    assert first.duration_ms == 1_250
    assert second.duration_ms == 2_500
    assert first.mime_type == second.mime_type == "video/mp4"
    assert first.video_codec == second.video_codec == "h264"
    assert cover.mime_type == "image/png"
    assert (cover.width, cover.height) == (1080, 1080)
    assert source.read_bytes() == source_before
    with Image.open(cover.path) as image:
        image.load()
        assert image.size == (1080, 1080)
        assert image.getpixel((540, 50)) == (0, 255, 0)

    ffmpeg_specs = [
        spec for spec in fake.specs if spec.executable.name == "ffmpeg.exe"
    ]
    assert len(ffmpeg_specs) == 3
    for spec in ffmpeg_specs:
        joined = "\0".join(spec.arguments)
        assert "$(never execute this label)" not in joined
        assert "$(never execute this title)" not in joined
        assert spec.arguments[:5] == (
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
        )
        protocol_index = spec.arguments.index("-protocol_whitelist")
        assert spec.arguments[protocol_index + 1] == "file"
        assert spec.arguments[protocol_index - 2 : protocol_index] == ("-f", "mov")
    segment_spec = ffmpeg_specs[0]
    assert segment_spec.arguments == (
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "mov",
        "-protocol_whitelist",
        "file",
        "-i",
        str(source),
        "-ss",
        "0.000",
        "-t",
        "1.250",
        "-map",
        "0:V:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-c:v",
        "libopenh264",
        "-b:v",
        "4M",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-fs",
        str(processor._segment_output_limit(1_250, audio=True)),
        str(output / "segment-001.mp4"),
    )
    cover_spec = ffmpeg_specs[-1]
    assert cover_spec.arguments[:-1] == (
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "mov",
        "-protocol_whitelist",
        "file",
        "-i",
        str(source),
        "-ss",
        "3.000",
        "-map",
        "0:V:0",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
        "-c:v",
        "png",
        "-f",
        "image2",
        "-update",
        "1",
        "-fs",
        str(editing_media.MAX_IMAGE_BYTES),
    )
    assert Path(cover_spec.arguments[-1]).parent == output
    assert Path(cover_spec.arguments[-1]).name.startswith(".cover-frame-")
    assert Path(cover_spec.arguments[-1]).suffix == ".png"
    assert not (tmp_path / "never execute this label").exists()
    assert not list(output.glob(".cover-frame-*.png"))


def test_ascii_cover_uses_embedded_font_without_os_font_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRunner()
    processor = _processor(tmp_path, fake)
    monkeypatch.setattr(
        MediaProcessor,
        "_cover_font_candidates",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("font lookup"))),
    )

    result = processor.render(
        _source(tmp_path),
        _output_dir(tmp_path),
        EditRecipe(
            cover=CoverSpec(timestamp_ms=100, aspect_ratio="4:3", title="ASCII title")
        ),
    )

    assert result.status == "ready"
    assert [asset.kind for asset in result.assets] == ["cover"]


def test_non_ascii_cover_without_allowed_os_font_fails_before_media_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRunner()
    processor = _processor(tmp_path, fake)
    output = _output_dir(tmp_path)
    monkeypatch.setattr(
        MediaProcessor,
        "_cover_font_candidates",
        staticmethod(lambda: ()),
    )

    with pytest.raises(EditingError, match="^cover_font_unavailable$"):
        processor.render(
            _source(tmp_path),
            output,
            EditRecipe(
                segments=(SegmentSpec(0, 1_000, "segment"),),
                cover=CoverSpec(
                    timestamp_ms=100,
                    aspect_ratio="4:3",
                    title="中文封面",
                ),
            ),
        )

    assert fake.specs == []
    assert list(output.iterdir()) == []


def test_non_ascii_cover_rejects_a_loadable_font_with_missing_glyphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = ImageFont.load_default(size=32)
    payload = getattr(embedded, "font_bytes", None)
    assert isinstance(payload, bytes) and payload
    candidate = (tmp_path / "ascii-only.ttf").resolve()
    candidate.write_bytes(payload)
    fake = FakeRunner()
    processor = _processor(tmp_path, fake)
    output = _output_dir(tmp_path)
    monkeypatch.setattr(
        MediaProcessor,
        "_cover_font_candidates",
        staticmethod(lambda: (candidate,)),
    )

    with pytest.raises(EditingError, match="^cover_glyph_unsupported$"):
        processor.render(
            _source(tmp_path),
            output,
            EditRecipe(
                cover=CoverSpec(
                    timestamp_ms=100,
                    aspect_ratio="4:3",
                    title="中文封面",
                )
            ),
        )

    assert fake.specs == []
    assert list(output.iterdir()) == []


def test_cover_font_candidate_over_32_mib_is_never_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = (tmp_path / "oversized.ttf").resolve()
    with candidate.open("wb") as handle:
        handle.truncate(editing_media.MAX_COVER_FONT_BYTES + 1)
    fake = FakeRunner()
    processor = _processor(tmp_path, fake)
    output = _output_dir(tmp_path)
    monkeypatch.setattr(
        MediaProcessor,
        "_cover_font_candidates",
        staticmethod(lambda: (candidate,)),
    )

    with pytest.raises(EditingError, match="^cover_font_unavailable$"):
        processor.render(
            _source(tmp_path),
            output,
            EditRecipe(
                cover=CoverSpec(
                    timestamp_ms=100,
                    aspect_ratio="4:3",
                    title="中文封面",
                )
            ),
        )

    assert fake.specs == []
    assert list(output.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="fixed Windows font smoke")
def test_fixed_windows_font_renders_distinct_cjk_glyphs(tmp_path: Path) -> None:
    candidates = tuple(
        path
        for path in MediaProcessor._cover_font_candidates()
        if path.is_file() and path.stat().st_size <= editing_media.MAX_COVER_FONT_BYTES
    )
    if not candidates:
        pytest.skip("no fixed Windows CJK font candidate is installed")
    fake = FakeRunner()
    processor = _processor(tmp_path, fake)
    recipe = EditRecipe(
        cover=CoverSpec(
            timestamp_ms=100,
            aspect_ratio="4:3",
            title="中文封面",
        )
    )

    source = processor._preflight_cover_font(recipe)
    assert source is not None and source.payload
    font = processor._cover_font_at_size(source, 32)
    missing = processor._glyph_signature(font, "\U0010ffff")
    assert processor._glyph_signature(font, "中") != missing
    assert processor._glyph_signature(font, "文") != missing
    assert processor._glyph_signature(font, "中") != processor._glyph_signature(font, "文")

    result = processor.render(_source(tmp_path), _output_dir(tmp_path), recipe)
    assert result.status == "ready"
    assert [asset.kind for asset in result.assets] == ["cover"]


@pytest.mark.parametrize(
    "recipe",
    [
        EditRecipe(segments=(SegmentSpec(500, 500, "bad"),)),
        EditRecipe(segments=(SegmentSpec(2_000, 3_000, "late"),)),
        EditRecipe(cover=CoverSpec(timestamp_ms=2_000, aspect_ratio="4:3")),
    ],
)
def test_invalid_or_out_of_source_recipe_never_starts_ffmpeg(
    tmp_path: Path,
    recipe: EditRecipe,
) -> None:
    fake = FakeRunner(source_document=_probe_document(duration="2.000"))
    source = _source(tmp_path)
    output = _output_dir(tmp_path)

    with pytest.raises(EditingError, match="invalid_edit_recipe"):
        _processor(tmp_path, fake).render(source, output, recipe)

    assert not any(spec.executable.name == "ffmpeg.exe" for spec in fake.specs)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    "recipe",
    [
        EditRecipe(
            segments=(SegmentSpec(0, 1_000, "clip"),),
            translation=TranslationSpec(
                enabled=True,
                source_language="en",
                target_language="zh-Hans",
                provider="local",
                state="ready",
            ),
        ),
        EditRecipe(
            segments=(SegmentSpec(0, 1_000, "clip"),),
            dubbing=DubbingSpec(
                enabled=True,
                language="zh-Hans",
                provider="local",
                voice="standard",
                state="ready",
            ),
        ),
    ],
)
def test_unimplemented_model_operations_are_not_silently_ignored(
    tmp_path: Path,
    recipe: EditRecipe,
) -> None:
    fake = FakeRunner()
    with pytest.raises(EditingError, match="capability_unavailable"):
        _processor(tmp_path, fake).render(
            _source(tmp_path),
            _output_dir(tmp_path),
            recipe,
        )
    assert fake.specs == []


def test_render_rejects_source_replaced_after_database_registration(
    tmp_path: Path,
) -> None:
    fake = FakeRunner()
    source = _source(tmp_path)
    registered = source.read_bytes()
    source.write_bytes(b"x" * len(registered))

    with pytest.raises(EditingError, match="source_asset_changed"):
        _processor(tmp_path, fake).render(
            source,
            _output_dir(tmp_path),
            EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
            expected_source_size=len(registered),
            expected_source_sha256=hashlib.sha256(registered).hexdigest(),
        )

    assert fake.specs == []


def test_failure_after_a_completed_segment_removes_all_outputs_and_redacts_stderr(
    tmp_path: Path,
) -> None:
    fake = FakeRunner(fail_ffmpeg_at=2)
    source = _source(tmp_path)
    output = _output_dir(tmp_path)
    recipe = EditRecipe(
        segments=(
            SegmentSpec(0, 1_000, "first"),
            SegmentSpec(1_000, 2_000, "second"),
        )
    )

    with pytest.raises(EditingError) as caught:
        _processor(tmp_path, fake).render(source, output, recipe)

    assert caught.value.code == "media_processing_failed"
    assert "must-not-escape" not in str(caught.value)
    assert list(output.iterdir()) == []


def test_cancellation_is_forwarded_to_runner_and_cleans_partial_output(
    tmp_path: Path,
) -> None:
    event = Event()
    fake = FakeRunner(cancel_on_ffmpeg=True)
    output = _output_dir(tmp_path)

    result = _processor(tmp_path, fake).render(
        _source(tmp_path),
        output,
        EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        cancel_event=event,
    )

    assert event.is_set()
    assert (result.status, result.code, result.assets) == ("canceled", "canceled", ())
    assert list(output.iterdir()) == []
    assert all(check is not None for check in fake.cancel_checks)


def test_already_canceled_render_does_not_probe_or_load_cover_font(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = Event()
    event.set()
    fake = FakeRunner()
    output = _output_dir(tmp_path)
    monkeypatch.setattr(
        MediaProcessor,
        "_cover_font_candidates",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("font lookup"))),
    )
    result = _processor(tmp_path, fake).render(
        _source(tmp_path),
        output,
        EditRecipe(
            segments=(SegmentSpec(0, 1_000, "clip"),),
            cover=CoverSpec(timestamp_ms=100, aspect_ratio="4:3", title="中文封面"),
        ),
        cancel_event=event,
    )
    assert (result.status, result.code, result.assets) == ("canceled", "canceled", ())
    assert fake.specs == []
    assert list(output.iterdir()) == []


def test_cancellation_during_registered_source_hash_returns_canceled(
    tmp_path: Path,
) -> None:
    event = Event()
    event.set()
    fake = FakeRunner()
    source = _source(tmp_path)
    payload = source.read_bytes()
    result = _processor(tmp_path, fake).render(
        source,
        _output_dir(tmp_path),
        EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        cancel_event=event,
        expected_source_size=len(payload),
        expected_source_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert (result.status, result.code, result.assets) == ("canceled", "canceled", ())
    assert fake.specs == []


def test_render_rejects_insufficient_peak_capacity_before_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner()
    monkeypatch.setattr(
        editing_media.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=editing_media.EDITING_RESERVE_BYTES),
    )

    with pytest.raises(EditingError, match="editing_storage_full"):
        _processor(tmp_path, fake).render(
            _source(tmp_path),
            _output_dir(tmp_path),
            EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        )

    assert fake.ffmpeg_calls == 0


def test_active_ffmpeg_output_limit_cancels_and_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner()
    processor = _processor(tmp_path, fake)
    output = _output_dir(tmp_path)
    monkeypatch.setattr(editing_media, "_RESOURCE_POLL_SECONDS", 0)
    monkeypatch.setattr(
        MediaProcessor,
        "_segment_output_limit",
        staticmethod(lambda _duration_ms, *, audio: 4),
    )

    with pytest.raises(EditingError, match="media_output_too_large"):
        processor.render(
            _source(tmp_path),
            output,
            EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        )

    assert list(output.iterdir()) == []
    command = next(
        spec for spec in fake.specs if spec.executable.name == "ffmpeg.exe"
    )
    file_size_index = command.arguments.index("-fs")
    assert command.arguments[file_size_index + 1] == "4"


@pytest.mark.parametrize(
    ("fake", "recipe"),
    [
        (
            FakeRunner(segment_container="matroska,webm"),
            EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        ),
        (
            FakeRunner(segment_duration_offset=1.0),
            EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        ),
        (
            FakeRunner(segment_video_codec="vp9"),
            EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        ),
        (
            FakeRunner(invalid_cover=True),
            EditRecipe(cover=CoverSpec(timestamp_ms=100, aspect_ratio="4:3")),
        ),
        (
            FakeRunner(oriented_cover=True),
            EditRecipe(cover=CoverSpec(timestamp_ms=100, aspect_ratio="4:3")),
        ),
    ],
)
def test_invalid_generated_media_is_rejected_and_removed(
    tmp_path: Path,
    fake: FakeRunner,
    recipe: EditRecipe,
) -> None:
    output = _output_dir(tmp_path)
    with pytest.raises(EditingError, match="media_processing_failed"):
        _processor(tmp_path, fake).render(_source(tmp_path), output, recipe)
    assert list(output.iterdir()) == []


def test_existing_output_collision_is_preserved_without_running_commands(
    tmp_path: Path,
) -> None:
    output = _output_dir(tmp_path)
    collision = output / "segment-001.mp4"
    collision.write_bytes(b"keep-me")
    fake = FakeRunner()

    with pytest.raises(EditingError, match="edit_output_exists"):
        _processor(tmp_path, fake).render(
            _source(tmp_path),
            output,
            EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
        )

    assert collision.read_bytes() == b"keep-me"
    assert fake.specs == []


def test_attached_cover_art_does_not_count_as_a_video_stream(tmp_path: Path) -> None:
    fake = FakeRunner(
        source_document={
            "format": {"format_name": "mp3", "duration": "2.000"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 600,
                    "height": 600,
                    "disposition": {"attached_pic": 1},
                },
                {"codec_type": "audio", "codec_name": "mp3"},
            ],
        }
    )

    with pytest.raises(EditingError, match="media_processing_failed"):
        _processor(tmp_path, fake).probe(_source(tmp_path))


def test_render_maps_the_first_non_attached_video_stream(tmp_path: Path) -> None:
    fake = FakeRunner()
    _processor(tmp_path, fake).render(
        _source(tmp_path),
        _output_dir(tmp_path),
        EditRecipe(segments=(SegmentSpec(0, 1_000, "clip"),)),
    )
    command = next(
        spec for spec in fake.specs if spec.executable.name == "ffmpeg.exe"
    )
    map_index = command.arguments.index("-map")
    assert command.arguments[map_index + 1] == "0:V:0"


def test_silent_source_produces_a_verified_silent_segment(tmp_path: Path) -> None:
    fake = FakeRunner(
        source_document=_probe_document(audio=False),
        segment_audio=False,
    )
    result = _processor(tmp_path, fake).render(
        _source(tmp_path),
        _output_dir(tmp_path),
        EditRecipe(segments=(SegmentSpec(0, 1_000, "silent"),)),
    )
    assert result.assets[0].audio_codec is None


def test_default_runner_is_restricted_to_locked_ffmpeg_directory(tmp_path: Path) -> None:
    tool_root = (tmp_path / "tools").resolve()
    processor = MediaProcessor(tool_root)
    assert isinstance(processor.runner, SecureSubprocessRunner)
    assert processor.runner.allowed_executable_roots == (
        (tool_root / "ffmpeg" / "bin").resolve(),
    )
    assert processor.ffmpeg_executable == tool_root / "ffmpeg" / "bin" / "ffmpeg.exe"
    assert processor.ffprobe_executable == tool_root / "ffmpeg" / "bin" / "ffprobe.exe"


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PINNED_TOOL_ROOT = (PROJECT_ROOT / "runtime-tools" / "windows-x64").resolve()
PINNED_FFMPEG = PINNED_TOOL_ROOT / "ffmpeg" / "bin" / "ffmpeg.exe"
PINNED_FFPROBE = PINNED_TOOL_ROOT / "ffmpeg" / "bin" / "ffprobe.exe"


@pytest.mark.skipif(
    sys.platform != "win32" or not PINNED_FFMPEG.is_file() or not PINNED_FFPROBE.is_file(),
    reason="the pinned Windows FFmpeg tools are not installed in this checkout",
)
def test_pinned_toolchain_rejects_renamed_ffconcat_before_output(
    tmp_path: Path,
) -> None:
    sibling = (tmp_path / "victim.mp4").resolve()
    generator = SecureSubprocessRunner(
        allowed_executable_roots=(PINNED_FFMPEG.parent,),
        poll_interval_seconds=0.005,
    )
    generated = generator.run(
        CommandSpec(
            executable=PINNED_FFMPEG,
            arguments=(
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:size=160x90:rate=25",
                "-t",
                "1.000",
                "-an",
                "-c:v",
                "libopenh264",
                "-b:v",
                "200k",
                "-pix_fmt",
                "yuv420p",
                str(sibling),
            ),
            cwd=tmp_path.resolve(),
            timeout_seconds=30,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=512 * 1024,
        )
    )
    assert generated.returncode == 0
    source = (tmp_path / "registered.mp4").resolve()
    source.write_text(
        "ffconcat version 1.0\nfile victim.mp4\nduration 1.0\n",
        encoding="ascii",
        newline="\n",
    )
    payload = source.read_bytes()
    output = _output_dir(tmp_path)

    with pytest.raises(EditingError, match="^media_processing_failed$"):
        MediaProcessor(PINNED_TOOL_ROOT).render(
            source,
            output,
            EditRecipe(segments=(SegmentSpec(0, 800, "audit"),)),
            expected_source_size=len(payload),
            expected_source_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert list(output.iterdir()) == []
    assert source.read_bytes() == payload
    assert sibling.is_file()


@pytest.mark.skipif(
    sys.platform != "win32" or not PINNED_FFMPEG.is_file() or not PINNED_FFPROBE.is_file(),
    reason="the pinned Windows FFmpeg tools are not installed in this checkout",
)
def test_pinned_toolchain_real_synthetic_segment_and_cover_smoke(tmp_path: Path) -> None:
    source = (tmp_path / "synthetic-source.mp4").resolve()
    generator = SecureSubprocessRunner(
        allowed_executable_roots=(PINNED_FFMPEG.parent,),
        poll_interval_seconds=0.005,
    )
    generated = generator.run(
        CommandSpec(
            executable=PINNED_FFMPEG,
            arguments=(
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=25",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "2.000",
                "-c:v",
                "libopenh264",
                "-b:v",
                "500k",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ),
            cwd=tmp_path.resolve(),
            timeout_seconds=30,
            stdout_limit_bytes=64 * 1024,
            stderr_limit_bytes=512 * 1024,
        )
    )
    assert generated.returncode == 0
    output = _output_dir(tmp_path)

    result = MediaProcessor(PINNED_TOOL_ROOT).render(
        source,
        output,
        EditRecipe(
            segments=(SegmentSpec(100, 1_100, "real smoke"),),
            cover=CoverSpec(timestamp_ms=500, aspect_ratio="16:9", title="Smoke"),
        ),
    )

    assert result.status == "ready"
    assert [(asset.kind, asset.mime_type) for asset in result.assets] == [
        ("segment", "video/mp4"),
        ("cover", "image/png"),
    ]
    assert all(asset.size_bytes > 0 and len(asset.sha256) == 64 for asset in result.assets)

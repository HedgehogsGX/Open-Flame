from __future__ import annotations

import asyncio
import ctypes
import hashlib
import importlib.util
import json
import os
import signal
import sys
import threading
import time
import types
import zipfile
from pathlib import Path

import pytest

from video_download_control.uploads.backend import SauBackend
from video_download_control.uploads.bridge import (
    PlatformParameterMismatch,
    ScheduleWindowElapsed,
    SingleSubmission,
    SubmissionUncertain,
    bilibili_arguments,
    browser_upload_options,
    install_platform_parameter_policy,
    install_statement_policy,
    submit_tencent_once,
    tencent_create_response,
)
from video_download_control.uploads.contracts import BackendResult, UploadRequest
from video_download_control.uploads.runtime_setup import (
    BILIUP_SHA256,
    BILIUP_VERSION,
    LOCK_PATH,
    SAU_COMMIT,
    SAU_SHA256,
    SetupError,
    extract_zip,
    install,
    inspect_runtime,
    runtime_lock,
)


def request(tmp_path: Path, **overrides) -> UploadRequest:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"local-test-media")
    data = dict(job_id="job1", account_id="account1", platform="douyin", file_path=media,
                title="Test", description="Description", tags=("tag",))
    data.update(overrides)
    return UploadRequest(**data)


def tencent_schedule(*, offset_minutes: int = 570) -> int:
    candidate = int(time.time()) + 6 * 3600
    return ((candidate + offset_minutes * 60 + 3599) // 3600 * 3600
            - offset_minutes * 60)


def minute_schedule(platform: str) -> int:
    lead = 6 * 3600 + 5 * 60 if platform == "bilibili" else 4 * 3600 + 5 * 60
    return ((int(time.time()) + lead + 3600) // 60 + 1) * 60


def ready_backend(tmp_path: Path, monkeypatch) -> SauBackend:
    backend = SauBackend(tmp_path)
    ready = lambda: {"ready": True, "code": "ready"}
    monkeypatch.setattr(backend, "inspect", ready)
    monkeypatch.setattr(backend, "_inspect_for_execution", ready)
    return backend


def account(tmp_path: Path, platform="douyin") -> Path:
    path = tmp_path / "private" / "accounts" / platform / "account1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def test_disconnect_local_removes_only_the_exact_account_file(tmp_path):
    target = account(tmp_path)
    sibling = target.with_name("other-account.json")
    sibling.write_text('{"keep":true}', encoding="utf-8")
    backend = SauBackend(tmp_path)

    backend.disconnect_local("douyin", "account1")
    backend.disconnect_local("douyin", "account1")

    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == '{"keep":true}'


def test_disconnect_local_rejects_hardlinked_account_state(tmp_path):
    target = account(tmp_path)
    other = tmp_path / "linked-secret.json"
    os.link(target, other)

    with pytest.raises(ValueError, match="invalid_account"):
        SauBackend(tmp_path).disconnect_local("douyin", "account1")

    assert target.exists() and other.exists()


def dummy_bridge(tmp_path: Path, backend: SauBackend, monkeypatch, body: str) -> None:
    script = tmp_path / "dummy.py"
    script.write_text(
        "import sys,json,time,subprocess\nfrom pathlib import Path\n"
        "operation=Path(sys.argv[-1])\n"
        "while not (operation/'go').exists(): time.sleep(.01)\n" + body,
        encoding="utf-8",
    )
    monkeypatch.setattr(backend, "_bridge_command", lambda operation: [sys.executable, "-I", str(script), str(operation)])


def test_runtime_inspect_does_not_install_or_create_directories(tmp_path):
    assert inspect_runtime(tmp_path) == {"ready": False, "code": "runtime_missing"}
    assert list(tmp_path.iterdir()) == []


def test_legacy_runtime_requires_explicit_reinstall(tmp_path):
    runtime = tmp_path / "runtime"
    source = runtime / "source"
    python = runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    browser = runtime / "browsers" / "chromium-synthetic"
    source.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    browser.mkdir(parents=True)
    (runtime / "biliup.exe").write_bytes(b"biliup")
    (source / "sau_cli.py").write_bytes(b"source")
    python.write_bytes(b"python")
    artifacts = {
        "biliup.exe": hashlib.sha256(b"biliup").hexdigest(),
        "source/sau_cli.py": hashlib.sha256(b"source").hexdigest(),
    }
    (runtime / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "sau_commit": SAU_COMMIT,
                "sau_archive_sha256": SAU_SHA256,
                "biliup_version": BILIUP_VERSION,
                "biliup_archive_sha256": BILIUP_SHA256,
                "requirements_sha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
                "cli_help_verified": True,
                "browser_launch_verified": True,
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )

    assert inspect_runtime(tmp_path) == {
        "ready": False,
        "code": "runtime_upgrade_required",
    }


def test_install_never_resigns_legacy_runtime(tmp_path, monkeypatch):
    test_legacy_runtime_requires_explicit_reinstall(tmp_path)
    monkeypatch.setattr(
        "video_download_control.uploads.runtime_setup._private_acl", lambda *_: None
    )
    monkeypatch.setattr(
        "video_download_control.uploads.runtime_setup._command", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "video_download_control.uploads.runtime_setup._download",
        lambda *_: pytest.fail("legacy runtime must stop before downloading"),
    )

    with pytest.raises(SetupError, match="runtime_upgrade_requires_reinstall"):
        install(tmp_path, Path(sys.executable))


@pytest.mark.skipif(os.name != "nt", reason="Windows OS file lock")
def test_setup_writer_excludes_live_backend_reader(tmp_path):
    backend = SauBackend(tmp_path)
    with runtime_lock(tmp_path, exclusive=True):
        assert backend.inspect()["code"] == "runtime_busy"
    assert backend.inspect()["code"] == "runtime_missing"


def test_execution_integrity_check_preserves_windows_only_gate(
    tmp_path, monkeypatch
):
    import video_download_control.uploads.backend as backend_module

    backend = SauBackend(tmp_path)
    monkeypatch.setattr(backend_module, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        backend_module,
        "inspect_runtime",
        lambda *_args, **_kwargs: pytest.fail(
            "unsupported platforms must stop before inspecting or executing"
        ),
    )

    result = backend.check("douyin", "account1", threading.Event())

    assert (result.status, result.code) == ("failed", "unsupported_platform")
    assert not (tmp_path / "private").exists()


def test_bilibili_staging_cleans_only_its_operation_checkpoint(tmp_path, monkeypatch):
    import video_download_control.uploads.backend as module
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test")
    operation = tmp_path / "operation"
    operation.mkdir()
    checkpoint_root = tmp_path / "local-data"
    checkpoint_root.mkdir()
    unrelated = checkpoint_root / "unrelated.json"
    unrelated.write_text("untouched")
    monkeypatch.setattr(module, "_windows_local_data", lambda: checkpoint_root)
    with module._biliup_media(operation, {"file_path": str(source)}) as staged:
        media = Path(staged["file_path"])
        assert media.parent == operation
        assert media.read_bytes() == b"test"
        checkpoint = checkpoint_root / module._biliup_checkpoint_name(media)
        if os.name == "nt":
            checkpoint.write_text("temporary checkpoint")
    if os.name == "nt":
        assert not checkpoint.exists()
    assert unrelated.read_text() == "untouched"
    assert source.read_bytes() == b"test"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", "", "name:stream"])
def test_account_paths_are_not_user_controlled(tmp_path, monkeypatch, bad):
    backend = ready_backend(tmp_path, monkeypatch)
    assert backend.login("douyin", bad, threading.Event()).code == "invalid_account"
    assert not (tmp_path / "private").exists()


def test_upload_requires_existing_account_and_never_implicitly_logs_in(tmp_path, monkeypatch):
    backend = ready_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "_execute", lambda *_: pytest.fail("must not start any child"))
    assert backend.upload(request(tmp_path), threading.Event()).code == "account_missing"


@pytest.mark.parametrize("metadata", [dict(copyright=2, source_credit=""),
                                      dict(copyright=1, source_credit="repost-only"),
                                      dict(copyright=True), dict(copyright=3), dict(category_id=0)])
def test_bilibili_repost_and_category_validation(tmp_path, monkeypatch, metadata):
    backend = ready_backend(tmp_path, monkeypatch)
    values = dict(platform="bilibili", category_id=171, copyright=2, source_credit="https://example.org/original")
    values.update(metadata)
    assert backend.upload(request(tmp_path, **values), threading.Event()).code == "invalid_bilibili_metadata"


def test_bilibili_copyright_and_source_are_real_cli_arguments(tmp_path):
    metadata = dict(file_path=str(tmp_path / "x.mp4"), title="Title", description="Desc", category_id=171,
                    tags=["tag"], copyright=2, source_credit="https://example.org/owned-source")
    args = bilibili_arguments(metadata, tmp_path / "private.json")
    assert args[args.index("--copyright") + 1] == "2"
    assert args[args.index("--source") + 1] == metadata["source_credit"]
    assert args[args.index("--tid") + 1] == "171"


def test_bilibili_extended_metadata_maps_only_to_fixed_cli_arguments(tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover")
    metadata = dict(
        file_path=str(tmp_path / "x.mp4"),
        title="Title",
        description="Desc",
        category_id=171,
        tags=["tag"],
        copyright=1,
        source_credit="",
        cover_landscape_path=str(cover),
        cover_portrait_path=None,
        publish_at_unix=minute_schedule("bilibili"),
        publish_timezone_offset_minutes=570,
        dynamic="Dynamic copy",
        no_reprint=True,
        close_comments=True,
        close_danmu=True,
    )

    args = bilibili_arguments(metadata, tmp_path / "private.json")

    assert args[args.index("--cover") + 1] == str(cover)
    assert args[args.index("--dtime") + 1] == str(metadata["publish_at_unix"])
    assert args[args.index("--dynamic") + 1] == "Dynamic copy"
    assert args[args.index("--no-reprint") + 1] == "1"
    assert "--up-close-reply" in args
    assert "--up-close-danmu" in args
    assert args[args.index("--submit") + 1] == "app"
    assert "--extra-fields" not in args


def test_browser_upload_options_map_covers_and_schedule_without_extra_fields(tmp_path):
    landscape = tmp_path / "landscape.png"
    portrait = tmp_path / "portrait.webp"
    landscape.write_bytes(b"landscape")
    portrait.write_bytes(b"portrait")
    publish_at = tencent_schedule()
    payload = dict(
        title="Title",
        file_path=str(tmp_path / "video.mp4"),
        tags=["tag"],
        description="Desc",
        cover_landscape_path=str(landscape),
        cover_portrait_path=str(portrait),
        publish_at_unix=publish_at,
        publish_timezone_offset_minutes=570,
        short_title="七个字短标题呀",
        content_label="含AI生成内容",
        mode="publish",
        ignored_upstream_option="must-not-pass",
    )

    options = browser_upload_options("tencent", payload, "account.json")

    assert options["thumbnail_landscape_path"] == str(landscape)
    assert options["thumbnail_portrait_path"] == str(portrait)
    assert int(options["publish_date"].timestamp()) == publish_at
    assert options["publish_date"].utcoffset().total_seconds() == 570 * 60
    assert options["publish_strategy"] == "scheduled"
    assert options["short_title"] == "七个字短标题呀"
    assert options["is_draft"] is False
    assert "content_label" not in options
    assert "ignored_upstream_option" not in options


def test_douyin_cover_is_explicitly_single_choice(tmp_path):
    landscape = tmp_path / "landscape.png"
    portrait = tmp_path / "portrait.png"
    landscape.write_bytes(b"landscape")
    portrait.write_bytes(b"portrait")
    payload = dict(
        title="Title",
        file_path=str(tmp_path / "video.mp4"),
        tags=[],
        description="",
        cover_landscape_path=str(landscape),
        cover_portrait_path=str(portrait),
    )

    with pytest.raises(ValueError, match="invalid_platform_metadata"):
        browser_upload_options("douyin", payload, "account.json")


@pytest.mark.parametrize(
    ("platform", "overrides"),
    [
        ("douyin", {"declaration": "arbitrary declaration"}),
        ("douyin", {"dynamic": "Bilibili only"}),
        ("tencent", {"content_label": "arbitrary label"}),
        ("tencent", {"short_title": "short"}),
        ("bilibili", {"category_id": 171, "declaration": "内容由AI生成"}),
        ("tencent", {"publish_at_unix": True, "publish_timezone_offset_minutes": 570}),
        ("tencent", {"mode": "draft", "publish_at_unix": 2_000_000_000,
                     "publish_timezone_offset_minutes": 570}),
    ],
)
def test_backend_rejects_unscoped_or_unbounded_platform_metadata(
    tmp_path, monkeypatch, platform, overrides
):
    backend = ready_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "_run", lambda *_: pytest.fail("invalid request must not start"))

    result = backend.upload(request(tmp_path, platform=platform, **overrides), threading.Event())

    assert (result.status, result.code) == ("failed", "invalid_platform_metadata")


def test_backend_serializes_cover_paths_and_fixed_options(tmp_path, monkeypatch):
    backend = ready_backend(tmp_path, monkeypatch)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    captured = {}

    def run(_action, _platform, _account, payload, _stop, _timeout):
        captured.update(payload)
        return BackendResult("submitted", "upstream_submitted")

    monkeypatch.setattr(backend, "_run", run)
    upload = request(
        tmp_path,
        platform="douyin",
        cover_portrait_path=cover,
        publish_at_unix=minute_schedule("douyin"),
        publish_timezone_offset_minutes=570,
        declaration="内容为个人观点或见解",
    )

    result = backend.upload(upload, threading.Event())

    assert result.status == "submitted"
    assert captured["cover_landscape_path"] is None
    assert captured["cover_portrait_path"] == str(cover)
    assert captured["publish_at_unix"] == upload.publish_at_unix
    assert captured["publish_timezone_offset_minutes"] == 570
    assert captured["declaration"] == "内容为个人观点或见解"


@pytest.mark.parametrize(
    ("platform", "lead"),
    [
        ("bilibili", 6 * 3600 + 5 * 60),
        ("douyin", 4 * 3600 + 5 * 60),
        ("tencent", 4 * 3600 + 5 * 60),
    ],
)
def test_backend_reports_elapsed_schedule_without_starting_the_child(
    tmp_path, monkeypatch, platform, lead
):
    backend = ready_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "_run", lambda *_args: pytest.fail("must not start child"))
    base = (2_000_000_000 // 3600) * 3600
    monkeypatch.setattr("video_download_control.uploads.backend.time.time", lambda: base)
    publish_at = base + lead
    if platform == "tencent":
        publish_at = publish_at // 3600 * 3600
    overrides = {
        "platform": platform,
        "publish_at_unix": publish_at,
        "publish_timezone_offset_minutes": 0,
    }
    if platform == "bilibili":
        overrides.update(category_id=171, copyright=1, source_credit="")

    result = backend.upload(request(tmp_path, **overrides), threading.Event())

    assert (result.status, result.code) == ("failed", "schedule_window_elapsed")


def test_absent_statements_replace_pinned_ai_defaults_with_no_ops():
    class Uploader:
        declaration = None

        async def apply_self_declaration(self, _page):
            raise AssertionError("pinned default must not run")

        async def apply_original_statement(self, _page):
            raise AssertionError("pinned default must not run")

    douyin = Uploader()
    install_statement_policy("douyin", douyin, {"declaration": None})
    assert douyin.declaration
    asyncio.run(douyin.apply_self_declaration(object()))

    tencent = Uploader()
    install_statement_policy("tencent", tencent, {"content_label": None})
    asyncio.run(tencent.apply_original_statement(object()))


def test_explicit_tencent_label_is_strict_and_propagates_missing_control():
    class Missing:
        first = None

        def __init__(self):
            self.first = self

        async def count(self):
            return 0

    class Page:
        def get_by_text(self, *_args, **_kwargs):
            return Missing()

    uploader = types.SimpleNamespace()
    install_statement_policy("tencent", uploader, {"content_label": "含AI生成内容"})

    with pytest.raises(RuntimeError, match="tencent_content_label_unavailable"):
        asyncio.run(uploader.apply_original_statement(Page()))


@pytest.mark.parametrize(("selected", "fails"), [(True, False), (False, True)])
def test_explicit_tencent_label_requires_selected_readback(selected, fails):
    class Locator:
        first = property(lambda self: self)

        def __init__(self, count=1):
            self._count = count

        def filter(self, **_kwargs):
            return self

        async def count(self):
            return self._count

        async def click(self):
            return None

        async def wait_for(self, **_kwargs):
            return None

        async def evaluate(self, _expression):
            assert "querySelector" not in _expression
            assert _expression.index("option-list-wrap") < _expression.index("aria-selected")
            assert "className" not in _expression
            return selected

    class Page:
        def get_by_text(self, *_args, **_kwargs):
            return Locator()

        def locator(self, _selector):
            raise AssertionError("readback must stay scoped to the exact label option")

        async def wait_for_timeout(self, _milliseconds):
            return None

    uploader = types.SimpleNamespace()
    install_statement_policy("tencent", uploader, {"content_label": "含AI生成内容"})
    operation = lambda: asyncio.run(uploader.apply_original_statement(Page()))
    if fails:
        with pytest.raises(RuntimeError, match="tencent_content_label_mismatch"):
            operation()
    else:
        operation()


def test_explicit_douyin_declaration_failure_is_not_silently_ignored():
    class Uploader:
        async def apply_self_declaration(self, _page):
            return False

    uploader = Uploader()
    install_statement_policy("douyin", uploader, {"declaration": "内容为转载信息"})
    with pytest.raises(RuntimeError, match="douyin_declaration_not_applied"):
        asyncio.run(uploader.apply_self_declaration(object()))


def test_explicit_douyin_declaration_requires_hidden_dialog_and_readback():
    class Locator:
        first = property(lambda self: self)

        def __init__(self, *, count=0, visible=False):
            self._count = count
            self._visible = visible

        def filter(self, **_kwargs):
            return self

        def nth(self, _index):
            return self

        async def count(self):
            return self._count

        async def is_visible(self):
            return self._visible

    class Page:
        def __init__(self, *, dialog=0, selected=0, readback=0):
            self.dialog = dialog
            self.selected = selected
            self.readback = readback

        def locator(self, selector):
            if ":visible" not in selector:
                assert "[class*=" not in selector
            return Locator(count=self.dialog if ":visible" in selector else self.selected)

        def get_by_text(self, *_args, **_kwargs):
            return Locator(count=self.readback, visible=bool(self.readback))

    class Uploader:
        async def apply_self_declaration(self, _page):
            return None

    selected = Uploader()
    install_statement_policy("douyin", selected, {"declaration": "内容为转载信息"})
    asyncio.run(selected.apply_self_declaration(Page(selected=1)))

    visible = Uploader()
    install_statement_policy("douyin", visible, {"declaration": "内容为转载信息"})
    with pytest.raises(RuntimeError, match="douyin_declaration_not_applied"):
        asyncio.run(visible.apply_self_declaration(Page(dialog=1, selected=1)))

    missing = Uploader()
    install_statement_policy("douyin", missing, {"declaration": "内容为转载信息"})
    with pytest.raises(RuntimeError, match="douyin_declaration_not_applied"):
        # A matching title/body elsewhere on the page is not declaration state.
        asyncio.run(missing.apply_self_declaration(Page(readback=1)))


def test_tencent_short_title_absence_is_noop_and_explicit_missing_field_fails():
    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = None
        thumbnail_portrait_path = None

        async def set_short_title(self, *_args):
            raise AssertionError("upstream must not synthesize a title")

    uploader = Uploader()
    install_platform_parameter_policy("tencent", uploader, {"short_title": None})
    asyncio.run(uploader.set_short_title(object(), "Main copy", None))

    class Missing:
        first = property(lambda self: self)

        def locator(self, *_args, **_kwargs):
            return self

        async def count(self):
            return 0

    class Page:
        def locator(self, *_args, **_kwargs):
            return Missing()

        def get_by_text(self, *_args, **_kwargs):
            return Missing()

    explicit = Uploader()
    install_platform_parameter_policy(
        "tencent", explicit, {"short_title": "视频号短标题测试"}
    )
    with pytest.raises(RuntimeError, match="tencent_short_title_unavailable"):
        asyncio.run(explicit.set_short_title(Page(), "Main copy", "视频号短标题测试"))


def test_browser_schedule_fails_if_required_lead_expires_during_form_entry(
    monkeypatch,
):
    class Uploader:
        publish_strategy = "scheduled"
        thumbnail_landscape_path = None
        thumbnail_portrait_path = None

        def __init__(self):
            self.calls = 0

        async def set_schedule_time_douyin(self, _page, _publish_date):
            self.calls += 1

    base = 2_000_000_000
    publish_at = base + 2 * 3600 + 1
    publish_date = types.SimpleNamespace(timestamp=lambda: publish_at)
    uploader = Uploader()
    ticks = iter((base, base + 2))
    monkeypatch.setattr(
        "video_download_control.uploads.bridge.time.time", lambda: next(ticks)
    )
    install_platform_parameter_policy("douyin", uploader, {})

    with pytest.raises(ScheduleWindowElapsed, match="schedule_window_elapsed"):
        asyncio.run(uploader.set_schedule_time_douyin(object(), publish_date))
    assert uploader.calls == 1

    expired = Uploader()
    monkeypatch.setattr(
        "video_download_control.uploads.bridge.time.time", lambda: base + 2
    )
    install_platform_parameter_policy("douyin", expired, {})
    with pytest.raises(ScheduleWindowElapsed, match="schedule_window_elapsed"):
        asyncio.run(expired.set_schedule_time_douyin(object(), publish_date))
    assert expired.calls == 0


@pytest.mark.parametrize(
    ("platform", "selected", "expected_error"),
    [
        ("douyin", True, None),
        ("douyin", False, "schedule_mode_mismatch"),
        ("tencent", True, None),
        ("tencent", False, "schedule_mode_mismatch"),
    ],
)
def test_browser_schedule_requires_the_exact_radio_to_be_selected(
    monkeypatch, platform, selected, expected_error
):
    base = 2_000_000_000
    publish_at = base + 3 * 3600
    publish_date = types.SimpleNamespace(
        timestamp=lambda: publish_at,
        year=2033,
        month=5,
        day=18,
        hour=11,
        minute=0,
    )

    class Locator:
        first = property(lambda self: self)

        def __init__(self, kind):
            self.kind = kind

        def filter(self, **kwargs):
            if self.kind == "mode":
                assert platform == "tencent"
                assert kwargs == {"has_text": "定时"}
            return self

        def nth(self, index):
            if self.kind == "mode":
                assert platform == "tencent"
                assert index == 1
            return self

        async def count(self):
            return 1

        async def evaluate(self, expression):
            assert self.kind == "mode"
            assert "input[type=\"radio\"]:checked" in expression
            assert "className" not in expression
            assert "radio-not-selected" not in expression
            return selected

        async def input_value(self):
            values = {
                "combined": "2033-05-18 11:00",
                "date": "2033-05-18",
                "time": "11:00",
            }
            return values[self.kind]

    class Page:
        def locator(self, selector):
            if platform == "douyin" and selector == (
                "[class^='radio']:has-text('定时发布')"
            ):
                return Locator("mode")
            if platform == "tencent" and selector == "label":
                return Locator("mode")
            if "日期和时间" in selector:
                return Locator("combined")
            if "发表时间" in selector:
                return Locator("date")
            if "请选择时间" in selector:
                return Locator("time")
            raise AssertionError(selector)

    class Uploader:
        publish_strategy = "scheduled"
        thumbnail_landscape_path = None
        thumbnail_portrait_path = None

        async def set_schedule_time_douyin(self, _page, _publish_date):
            return None

        async def set_schedule_time_tencent(self, _page, _publish_date):
            return None

        async def set_short_title(self, *_args):
            return None

    monkeypatch.setattr(
        "video_download_control.uploads.bridge.time.time", lambda: base
    )
    uploader = Uploader()
    install_platform_parameter_policy(platform, uploader, {})
    operation = lambda: asyncio.run(
        getattr(uploader, f"set_schedule_time_{platform}")(Page(), publish_date)
    )
    if expected_error:
        with pytest.raises(RuntimeError, match=f"^{expected_error}$"):
            operation()
    else:
        operation()


@pytest.mark.parametrize(
    ("platform", "label", "mutation"),
    [
        ("douyin", "发布", "selected"),
        ("douyin", "发布", "combined"),
        ("tencent", "发表", "selected"),
        ("tencent", "发表", "date"),
        ("tencent", "发表", "time"),
    ],
)
def test_final_click_rechecks_schedule_state_after_the_page_rerenders(
    monkeypatch, platform, label, mutation
):
    base = 2_000_000_000
    publish_at = base + 3 * 3600
    publish_date = types.SimpleNamespace(
        timestamp=lambda: publish_at,
        year=2033,
        month=5,
        day=18,
        hour=11,
        minute=0,
    )

    class Locator:
        first = property(lambda self: self)

        def __init__(self, page, kind):
            self.page = page
            self.kind = kind

        def filter(self, **_kwargs):
            return self

        def nth(self, _index):
            return self

        async def count(self):
            return 1

        async def evaluate(self, _expression):
            return self.page.selected

        async def input_value(self):
            return self.page.values[self.kind]

    class Page:
        selected = True

        def __init__(self):
            self.values = {
                "combined": "2033-05-18 11:00",
                "date": "2033-05-18",
                "time": "11:00",
            }

        def locator(self, selector):
            if selector in ("[class^='radio']:has-text('定时发布')", "label"):
                return Locator(self, "mode")
            if "日期和时间" in selector:
                return Locator(self, "combined")
            if "发表时间" in selector:
                return Locator(self, "date")
            if "请选择时间" in selector:
                return Locator(self, "time")
            raise AssertionError(selector)

    class Uploader:
        publish_strategy = "scheduled"
        thumbnail_landscape_path = None
        thumbnail_portrait_path = None

        async def set_schedule_time_douyin(self, _page, _date):
            return None

        async def set_schedule_time_tencent(self, _page, _date):
            return None

        async def set_short_title(self, *_args):
            return None

    monkeypatch.setattr("video_download_control.uploads.bridge.time.time", lambda: base)
    guard, uploader, page = SingleSubmission(platform, publish_at), Uploader(), Page()
    install_platform_parameter_policy(platform, uploader, {}, guard)
    method = (
        uploader.set_schedule_time_douyin
        if platform == "douyin"
        else uploader.set_schedule_time_tencent
    )
    asyncio.run(method(page, publish_date))
    if mutation == "selected":
        page.selected = False
    else:
        page.values[mutation] = "2033-05-18 12:30"

    with pytest.raises(PlatformParameterMismatch, match="platform_parameter_mismatch"):
        asyncio.run(guard.before_click(label))
    assert guard.attempted is False


@pytest.mark.parametrize("platform", ["douyin", "tencent"])
def test_final_click_rechecks_main_content_after_initial_fill(platform):
    request_payload = {
        "title": "核对标题",
        "description": "第一行\n第二行",
        "tags": ["旅行", "教程"],
    }

    class Locator:
        first = property(lambda self: self)

        def __init__(self, page):
            self.page = page

        async def count(self):
            return 1

        async def input_value(self):
            return self.page.title

        async def is_visible(self):
            return True

        async def inner_text(self):
            return self.page.editor

    class Page:
        title = request_payload["title"]
        editor = (
            "第一行\n第二行 #旅行 #教程"
            if platform == "douyin"
            else "核对标题\n#旅行 #教程\n第一行\n第二行"
        )

        def locator(self, selector):
            if "填写作品标题" in selector or "zone-container" in selector:
                return Locator(self)
            if selector == "div.input-editor":
                return Locator(self)
            raise AssertionError(selector)

    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = None
        thumbnail_portrait_path = None

        async def fill_title_and_description(self, _page, _title, _description, _tags=None):
            return None

        async def fill_description(self, _page):
            return None

        async def set_short_title(self, *_args):
            return None

    guard, uploader, page = SingleSubmission(platform), Uploader(), Page()
    install_platform_parameter_policy(platform, uploader, request_payload, guard)
    if platform == "douyin":
        asyncio.run(uploader.fill_title_and_description(
            page, request_payload["title"], request_payload["description"],
            request_payload["tags"],
        ))
    else:
        asyncio.run(uploader.fill_description(page))
    page.editor = "页面静默丢失了标签"

    with pytest.raises(PlatformParameterMismatch, match="platform_parameter_mismatch"):
        asyncio.run(guard.before_click("发布" if platform == "douyin" else "发表"))
    assert guard.attempted is False


def test_tencent_explicit_cover_requires_the_editor_dialog():
    class Preview:
        first = property(lambda self: self)

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def evaluate(self, _expression):
            return '{"images":["before"],"backgrounds":[]}'

    class Page:
        def locator(self, _selector):
            return Preview()

    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = "cover.png"
        thumbnail_portrait_path = None

        async def open_thumbnail_dialog(self, *_args):
            return None

        async def upload_thumbnail_in_dialog(self, *_args):
            raise AssertionError("missing dialog must stop first")

        async def confirm_thumbnail_crop(self, *_args):
            raise AssertionError("missing dialog must stop first")

        async def set_short_title(self, *_args):
            return None

    uploader = Uploader()
    install_platform_parameter_policy("tencent", uploader, {"short_title": None})
    with pytest.raises(RuntimeError, match="tencent_cover_control_unavailable"):
        asyncio.run(uploader.set_single_thumbnail(
            Page(), "cover.png", ["selector"], ["dialog"], "4:3"
        ))


@pytest.mark.parametrize(
    ("crop_hidden", "main_hidden", "preview_changed", "fails"),
    [
        (False, True, True, True),
        (True, False, True, True),
        (True, True, False, True),
        (True, True, True, False),
    ],
)
def test_tencent_explicit_cover_requires_closed_dialogs_and_changed_preview(
    crop_hidden, main_hidden, preview_changed, fails
):
    state_data = {"preview": "before", "crop_hidden": False, "main_hidden": False}

    class Locator:
        first = property(lambda self: self)

        def __init__(self, kind):
            self.kind = kind

        def filter(self, **_kwargs):
            return self

        async def count(self):
            return 1

        async def is_visible(self):
            if self.kind == "crop":
                return not state_data["crop_hidden"]
            if self.kind == "main":
                return not state_data["main_hidden"]
            return True

        async def wait_for(self, *, state, **_kwargs):
            if state == "hidden" and await self.is_visible():
                raise TimeoutError

        async def evaluate(self, _expression):
            return json.dumps({"images": [state_data["preview"]], "backgrounds": []})

    main_dialog = Locator("main")

    class Page:
        def locator(self, selector):
            return Locator("crop") if "weui-desktop-dialog" in selector else Locator("preview")

        async def wait_for_timeout(self, _milliseconds):
            return None

    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = "cover.png"
        thumbnail_portrait_path = None

        async def open_thumbnail_dialog(self, *_args):
            return main_dialog

        async def upload_thumbnail_in_dialog(self, *_args):
            return None

        async def confirm_thumbnail_crop(self, *_args):
            state_data["crop_hidden"] = crop_hidden
            state_data["main_hidden"] = main_hidden
            if preview_changed:
                state_data["preview"] = "after"

        async def set_short_title(self, *_args):
            return None

    uploader = Uploader()
    install_platform_parameter_policy("tencent", uploader, {"short_title": None})
    action = lambda: asyncio.run(uploader.set_single_thumbnail(
        Page(), "cover.png", ["selector"], ["dialog"], "4:3"
    ))
    if fails:
        with pytest.raises(RuntimeError, match="tencent_cover_not_applied"):
            action()
    else:
        action()


def test_douyin_explicit_cover_requires_the_exact_file_input_even_if_preview_changes():
    class Locator:
        first = property(lambda self: self)

        def __init__(self, page, *, count=1):
            self.page = page
            self._count = count

        def filter(self, **_kwargs):
            return self

        def locator(self, _selector):
            return self

        async def count(self):
            return self._count

        async def get_attribute(self, _name):
            return self.page.preview

        async def is_visible(self):
            return False

    class Page:
        preview = "default-before"

        def locator(self, selector):
            return Locator(self, count=0) if "dy-creator-content-modal" in selector else Locator(self)

        async def wait_for_timeout(self, _milliseconds):
            return None

    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = "cover.png"
        thumbnail_portrait_path = None

        async def set_thumbnail(self, page):
            # Simulate the default video thumbnail arriving while the pinned
            # adapter silently skips an unopened custom-cover dialog.
            page.preview = "generated-default-after"

    uploader = Uploader()
    install_platform_parameter_policy("douyin", uploader, {})
    with pytest.raises(RuntimeError, match="douyin_cover_not_applied"):
        asyncio.run(uploader.set_thumbnail(Page()))


def test_douyin_explicit_cover_never_falls_back_to_a_recommended_cover():
    class Locator:
        first = property(lambda self: self)

        def __init__(self, page, kind):
            self.page = page
            self.kind = kind

        def filter(self, **_kwargs):
            return self

        def locator(self, _selector):
            return Locator(self.page, "image")

        async def count(self):
            return 0 if self.kind == "dialog" else 1

        async def is_visible(self):
            return self.page.warning if self.kind == "warning" else False

        async def get_attribute(self, _name):
            return self.page.preview

        async def set_input_files(self, _files):
            return None

    class Page:
        preview = "before"
        warning = False
        recommended_clicks = 0

        def locator(self, selector):
            if selector == "div.dy-creator-content-modal":
                return Locator(self, "dialog")
            return Locator(self, "preview")

        def get_by_text(self, text, **_kwargs):
            assert text == "请设置封面后再发布"
            return Locator(self, "warning")

        async def wait_for_timeout(self, _milliseconds):
            return None

    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = "cover.png"
        thumbnail_portrait_path = None

        async def set_thumbnail(self, page):
            await page.locator("cover-upload").set_input_files(
                self.thumbnail_landscape_path
            )
            page.preview = "custom-cover"

        async def handle_auto_video_cover(self, page):
            page.recommended_clicks += 1
            return True

    guard, uploader, page = SingleSubmission("douyin"), Uploader(), Page()
    install_platform_parameter_policy("douyin", uploader, {}, guard)
    asyncio.run(uploader.set_thumbnail(page))
    page.warning = True

    with pytest.raises(PlatformParameterMismatch, match="platform_parameter_mismatch"):
        asyncio.run(guard.before_click("发布"))
    with pytest.raises(RuntimeError, match="douyin_explicit_cover_rejected"):
        asyncio.run(uploader.handle_auto_video_cover(page))
    assert guard.attempted is False
    assert page.recommended_clicks == 0


def test_douyin_without_an_explicit_cover_keeps_the_upstream_fallback_handler():
    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = None
        thumbnail_portrait_path = None

        async def set_thumbnail(self, _page):
            return None

        async def handle_auto_video_cover(self, _page):
            return "upstream-handler"

    uploader = Uploader()
    original = uploader.handle_auto_video_cover.__func__
    install_platform_parameter_policy("douyin", uploader, {})
    assert uploader.handle_auto_video_cover.__func__ is original
    assert asyncio.run(uploader.handle_auto_video_cover(object())) == "upstream-handler"


@pytest.mark.parametrize(
    ("platform", "request_payload", "method_name", "final_label"),
    [
        ("douyin", {"declaration": "内容为转载信息"}, "apply_self_declaration", "发布"),
        ("tencent", {"content_label": "含AI生成内容"}, "apply_original_statement", "发表"),
    ],
)
def test_final_click_rechecks_explicit_statement_selection(
    platform, request_payload, method_name, final_label
):
    state = {"selected": True}

    class Locator:
        first = property(lambda self: self)

        def __init__(self, kind="option"):
            self.kind = kind

        def filter(self, **_kwargs):
            return self

        async def count(self):
            if self.kind == "dialog":
                return 0
            return 1 if state["selected"] else 0

        async def click(self):
            return None

        async def wait_for(self, **_kwargs):
            return None

        async def evaluate(self, _expression):
            return state["selected"]

    class Page:
        def locator(self, selector):
            return Locator("dialog" if ":visible" in selector else "option")

        def get_by_text(self, *_args, **_kwargs):
            return Locator()

        async def wait_for_timeout(self, _milliseconds):
            return None

    class Uploader:
        async def apply_self_declaration(self, _page):
            return None

    guard, uploader, page = SingleSubmission(platform), Uploader(), Page()
    install_statement_policy(platform, uploader, request_payload, guard)
    asyncio.run(getattr(uploader, method_name)(page))
    state["selected"] = False

    with pytest.raises(PlatformParameterMismatch, match="platform_parameter_mismatch"):
        asyncio.run(guard.before_click(final_label))
    assert guard.attempted is False


def test_final_click_rechecks_tencent_short_title():
    state = {"value": ""}

    class Locator:
        first = property(lambda self: self)

        def locator(self, *_args):
            return self

        async def count(self):
            return 1

        async def fill(self, value):
            state["value"] = value

        async def input_value(self):
            return state["value"]

    class Page:
        def locator(self, *_args):
            return Locator()

        def get_by_text(self, *_args, **_kwargs):
            return Locator()

    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = None
        thumbnail_portrait_path = None

        async def set_short_title(self, *_args):
            return None

    value = "视频号短标题测试"
    guard, uploader, page = SingleSubmission("tencent"), Uploader(), Page()
    install_platform_parameter_policy("tencent", uploader, {"short_title": value}, guard)
    asyncio.run(uploader.set_short_title(page, "main", value))
    state["value"] = "页面重渲染后的旧值"

    with pytest.raises(PlatformParameterMismatch, match="platform_parameter_mismatch"):
        asyncio.run(guard.before_click("发表"))
    assert guard.attempted is False


def test_final_click_rechecks_tencent_explicit_cover_fingerprint():
    state = {"preview": "before", "crop_visible": False, "dialog_visible": False}

    class Locator:
        first = property(lambda self: self)

        def __init__(self, kind):
            self.kind = kind

        def filter(self, **_kwargs):
            return self

        async def count(self):
            return 1

        async def is_visible(self):
            if self.kind == "crop":
                return state["crop_visible"]
            if self.kind == "dialog":
                return state["dialog_visible"]
            return True

        async def wait_for(self, *, state, **_kwargs):
            if state == "hidden" and await self.is_visible():
                raise TimeoutError

        async def evaluate(self, _expression):
            return json.dumps({"images": [state["preview"]], "backgrounds": []})

    dialog = Locator("dialog")

    class Page:
        def locator(self, selector):
            return Locator("crop" if "weui-desktop-dialog" in selector else "preview")

        async def wait_for_timeout(self, _milliseconds):
            return None

    class Uploader:
        publish_strategy = "immediate"
        thumbnail_landscape_path = "cover.png"
        thumbnail_portrait_path = None

        async def open_thumbnail_dialog(self, *_args):
            state["dialog_visible"] = True
            return dialog

        async def upload_thumbnail_in_dialog(self, *_args):
            return None

        async def confirm_thumbnail_crop(self, *_args):
            state["preview"] = "custom-cover"
            state["dialog_visible"] = False

        async def set_short_title(self, *_args):
            return None

    guard, uploader, page = SingleSubmission("tencent"), Uploader(), Page()
    install_platform_parameter_policy("tencent", uploader, {"short_title": None}, guard)
    asyncio.run(uploader.set_single_thumbnail(
        page, "cover.png", ["selector"], ["dialog"], "4:3"
    ))
    state["preview"] = "default-after-rerender"

    with pytest.raises(PlatformParameterMismatch, match="platform_parameter_mismatch"):
        asyncio.run(guard.before_click("发表"))
    assert guard.attempted is False


@pytest.mark.parametrize("label", ["发布", "发表", "保存草稿"])
def test_second_final_click_escapes_upstream_exception_retry_loop(label):
    guard = SingleSubmission()
    assert not guard.before("发表视频")
    assert guard.before(label)
    with pytest.raises(SubmissionUncertain):
        try:
            guard.before(label)
        except Exception:
            pytest.fail("upstream Exception retry loop must not swallow the stop")


@pytest.mark.parametrize(("platform", "label"), [("douyin", "发布"), ("tencent", "发表")])
def test_expired_schedule_stops_before_the_final_browser_click(
    monkeypatch, platform, label
):
    class Locator:
        def __init__(self):
            self.clicks = 0

        async def evaluate(self, _expression, *args, **kwargs):
            return label

        async def click(self, *args, **kwargs):
            self.clicks += 1

    monkeypatch.setitem(
        sys.modules, "patchright.async_api", types.SimpleNamespace(Locator=Locator)
    )
    base = 2_000_000_000
    publish_at = base + 2 * 3600 + 59
    monkeypatch.setattr(
        "video_download_control.uploads.bridge.time.time", lambda: base
    )
    guard = SingleSubmission(platform, publish_at)
    guard.install()
    locator = Locator()

    with pytest.raises(ScheduleWindowElapsed, match="^schedule_window_elapsed$"):
        asyncio.run(locator.click())
    assert locator.clicks == 0
    assert guard.attempted is False


def test_locator_click_and_javascript_fallback_share_the_same_once_guard(monkeypatch):
    class Locator:
        def __init__(self):
            self.clicks = 0

        async def evaluate(self, expression, *args, **kwargs):
            if ".click(" in expression:
                self.clicks += 1
            else:
                return "发表"

        async def click(self, *args, **kwargs):
            self.clicks += 1

    monkeypatch.setitem(sys.modules, "patchright.async_api", types.SimpleNamespace(Locator=Locator))
    guard = SingleSubmission()
    guard.install()
    locator = Locator()

    async def exercise():
        await locator.click()
        with pytest.raises(SubmissionUncertain):
            await locator.evaluate("el => el.click()")
        with pytest.raises(SubmissionUncertain):
            await locator.click()

    asyncio.run(exercise())
    assert locator.clicks == 1


class FakeResponse:
    url = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/post_create"
    status = 200
    request = types.SimpleNamespace(method="POST")

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeSubmissionPage:
    def __init__(self, payload, *, no_response=False):
        self.response = FakeResponse(payload)
        self.clicks = 0
        self.url = "https://channels.weixin.qq.com/login.html"
        self.no_response = no_response

    def get_by_role(self, *args, **kwargs):
        return types.SimpleNamespace(first=self)

    async def wait_for(self, **kwargs):
        pass

    async def click(self, **kwargs):
        self.clicks += 1

    def expect_response(self, predicate, **kwargs):
        assert predicate(self.response)
        page = self

        class Expectation:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                if page.no_response:
                    raise TimeoutError()

            @property
            def value(self):
                async def value():
                    return page.response
                return value()

        return Expectation()


def tencent_uploader():
    async def dismiss(_page):
        pass
    return types.SimpleNamespace(is_draft=False, _dismiss_switch_account_dialog=dismiss,
                                 _open_flame_acknowledged=False)


@pytest.mark.parametrize("payload", [{}, {"errCode": False}, {"errCode": "0"}, {"errCode": 1}, []])
def test_tencent_missing_or_nonzero_response_is_unknown(payload):
    page = FakeSubmissionPage(payload)
    uploader = tencent_uploader()
    with pytest.raises(SubmissionUncertain):
        asyncio.run(submit_tencent_once(uploader, page))
    assert page.clicks == 1
    assert not uploader._open_flame_acknowledged


def test_tencent_login_redirect_or_missing_response_never_acknowledges_submission():
    page = FakeSubmissionPage({"errCode": 0}, no_response=True)
    uploader = tencent_uploader()
    with pytest.raises(SubmissionUncertain):
        asyncio.run(submit_tencent_once(uploader, page))
    assert page.clicks == 1
    assert not uploader._open_flame_acknowledged


def test_tencent_explicit_success_response_acknowledges_once():
    page = FakeSubmissionPage({"errCode": 0})
    uploader = tencent_uploader()
    asyncio.run(submit_tencent_once(uploader, page))
    assert uploader._open_flame_acknowledged
    assert page.clicks == 1


@pytest.mark.parametrize("url", ["http://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create",
                                 "https://evil.test/cgi-bin/mmfinderassistant-bin/post/post_create",
                                 "https://channels.weixin.qq.com/login.html",
                                 "https://channels.weixin.qq.com:444/cgi-bin/mmfinderassistant-bin/post/post_create"])
def test_tencent_response_must_match_exact_origin_and_operation(url):
    response = FakeResponse({"errCode": 0})
    response.url = url
    assert not tencent_create_response(response)


def test_confirmed_result_is_bounded_and_no_raw_output_is_exposed(tmp_path, monkeypatch, capsys):
    backend = ready_backend(tmp_path, monkeypatch)
    account(tmp_path)
    dummy_bridge(tmp_path, backend, monkeypatch,
        "print('SECRET-COOKIE-value',flush=True)\n"
        "print('SECRET-access-token',file=sys.stderr,flush=True)\n"
        "(operation/'result.json').write_text(json.dumps({'status':'submitted','code':'upstream_submitted'}))\n")
    result = backend.upload(request(tmp_path), threading.Event())
    assert (result.status, result.code) == ("submitted", "upstream_submitted")
    assert "SECRET" not in capsys.readouterr().out
    assert list((tmp_path / "private" / "operations").iterdir()) == []


@pytest.mark.parametrize("body", [
    "sys.exit(1)\n",
    "(operation/'result.json').write_text('{}')\n",
    "(operation/'result.json').write_text(json.dumps({'status':'submitted','code':'SECRET-cookie'}))\n",
    "(operation/'result.json').write_text('X'*2048)\n",
])
def test_upload_without_acknowledgement_is_unknown(tmp_path, monkeypatch, body):
    backend = ready_backend(tmp_path, monkeypatch)
    account(tmp_path)
    dummy_bridge(tmp_path, backend, monkeypatch, body)
    result = backend.upload(request(tmp_path), threading.Event())
    assert result.status == "unknown", result
    assert result.code == "upstream_result_unknown"


def test_check_cannot_accept_an_upload_acknowledgement(tmp_path, monkeypatch):
    backend = ready_backend(tmp_path, monkeypatch)
    account(tmp_path)
    dummy_bridge(tmp_path, backend, monkeypatch,
        "(operation/'result.json').write_text(json.dumps({'status':'submitted','code':'upstream_submitted'}))\n")
    assert backend.check("douyin", "account1", threading.Event()).status == "failed"


def test_upload_stop_before_launch_is_cancelled_without_child(tmp_path, monkeypatch):
    backend = ready_backend(tmp_path, monkeypatch)
    stop = threading.Event()
    stop.set()
    assert backend.upload(request(tmp_path), stop).status == "cancelled"


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            value = ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(value))) and value.value == 259
        finally:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_stop_owned_grandchildren(tmp_path, monkeypatch, cancel):
    backend = ready_backend(tmp_path, monkeypatch)
    backend.upload_timeout = 1.5
    account(tmp_path)
    marker = tmp_path / "grandchild.pid"
    dummy_bridge(tmp_path, backend, monkeypatch,
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n")
    stop = threading.Event()
    timer = threading.Timer(0.8, stop.set) if cancel else None
    if timer:
        timer.start()
    result = backend.upload(request(tmp_path), stop)
    if timer:
        timer.join()
    assert result.status == "unknown", result
    assert result.code == ("upload_cancelled_unknown" if cancel else "upload_timeout_unknown")
    pid = int(marker.read_text())
    deadline = time.monotonic() + 3
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not process_alive(pid)


def test_archive_path_traversal_is_rejected(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("top/../../outside", "bad")
    with pytest.raises(SetupError, match="unsafe_archive"):
        extract_zip(archive, tmp_path / "extract", strip_root=True)
    assert not (tmp_path / "outside").exists()


def test_only_tencent_supports_draft(tmp_path, monkeypatch):
    backend = ready_backend(tmp_path, monkeypatch)
    assert backend.upload(request(tmp_path, mode="draft"), threading.Event()).code == "unsupported_mode"

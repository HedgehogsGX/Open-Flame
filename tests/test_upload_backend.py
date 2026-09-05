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
    SingleSubmission,
    SubmissionUncertain,
    bilibili_arguments,
    submit_tencent_once,
    tencent_create_response,
)
from video_download_control.uploads.contracts import UploadRequest
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


@pytest.mark.parametrize("metadata", [dict(copyright=2, source_credit=""), dict(copyright=True),
                                      dict(copyright=3), dict(category_id=0)])
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

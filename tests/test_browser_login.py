from __future__ import annotations

import asyncio
import json
import struct
import sys
import types
from pathlib import Path

import pytest

from video_download_control.uploads import browser_login as login


def png(width=160, height=160, suffix=b"qr"):
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", width, height) + suffix


def state(platform="douyin"):
    return {"cookies": [{"name": "sessionid", "value": "secret-not-for-events",
                         "domain": ".douyin.com" if platform == "douyin" else "channels.weixin.qq.com"}],
            "origins": []}


class Locator:
    def __init__(self, visible=False, *, image=None, box=None):
        self.visible = visible
        self.image = image
        self.box = box or {"width": 160, "height": 160}
        self.captured = False

    @property
    def first(self):
        return self

    async def is_visible(self, **kwargs):
        return self.visible

    async def count(self):
        return int(self.visible)

    def nth(self, index):
        assert index == 0
        return self

    async def bounding_box(self, **kwargs):
        return self.box

    async def evaluate(self, expression):
        assert "naturalWidth" in expression
        return self.image is not None

    async def screenshot(self, **kwargs):
        assert kwargs == {"type": "png", "timeout": 10000, "animations": "disabled"}
        self.captured = True
        return self.image


class Surface:
    def __init__(self, url, *, selectors=None, texts=()):
        self.url = url
        self.selectors = selectors or {}
        self.texts = set(texts)
        self.main_frame = object()
        self.frames = [self.main_frame]
        self.visits = []
        self.block_navigation = False

    def locator(self, selector):
        return self.selectors.get(selector, Locator())

    def get_by_text(self, text, *, exact):
        assert exact is True
        return Locator(text in self.texts)

    async def goto(self, url, **kwargs):
        self.visits.append(url)
        if self.block_navigation:
            await asyncio.Event().wait()


class Context:
    def __init__(self, page, saved_state=None):
        self.page = page
        self.saved_state = saved_state or state()
        self.closed = False

    def set_default_timeout(self, timeout):
        assert timeout == 3000

    async def new_page(self):
        return self.page

    async def cookies(self):
        return self.saved_state["cookies"]

    async def storage_state(self):
        return self.saved_state

    async def close(self):
        self.closed = True


def install_runtime(monkeypatch, tmp_path, page, saved_state=None):
    context = Context(page, saved_state)

    class Browser:
        closed = False

        async def new_context(self, **kwargs):
            # Login never consumes a saved account or persistent browser profile.
            assert kwargs == {"viewport": {"width": 1280, "height": 900}}
            return context

        async def close(self):
            self.closed = True

    browser = Browser()

    async def launch(**kwargs):
        assert kwargs == {"executable_path": str(tmp_path / "chrome.exe"),
                          "headless": True, "timeout": 30000}
        return browser

    class Manager:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch))

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "patchright.async_api", types.SimpleNamespace(async_playwright=Manager))
    monkeypatch.setattr(login, "POLL_SECONDS", 0)
    data = {"platform": "douyin", "browser_path": str(tmp_path / "chrome.exe"),
            "account_file": str(tmp_path / "account.json"), "operation_dir": str(tmp_path),
            "timeout_seconds": 1}
    return data, context, browser


@pytest.mark.parametrize("url", [
    "https://creator.douyin.com.evil.test/creator-micro/home",
    "http://creator.douyin.com/creator-micro/home",
    "https://creator.douyin.com:444/creator-micro/home",
    "https://user@creator.douyin.com/creator-micro/home",
    "https://creator.douyin.com/creator-micro-fake/home",
    "https://creator.douyin.com/",
])
def test_authentication_rejects_untrusted_or_login_routes(url):
    page = Surface(url, texts=("发布视频",))
    assert not asyncio.run(login._authenticated(page, Context(page), "douyin", False))


@pytest.mark.parametrize("platform,url,marker", [
    ("douyin", "https://creator.douyin.com/creator-micro/home", "发布视频"),
    ("tencent", "https://channels.weixin.qq.com/platform", "发表视频"),
])
def test_authentication_requires_ui_session_and_no_login_qr(platform, url, marker):
    page = Surface(url, texts=(marker,))
    context = Context(page, state(platform))
    assert asyncio.run(login._authenticated(page, context, platform, False))
    assert not asyncio.run(login._authenticated(page, context, platform, True))
    page.texts.clear()
    assert not asyncio.run(login._authenticated(page, context, platform, False))
    page.texts.add(marker)
    context.saved_state = {"cookies": []}
    assert not asyncio.run(login._authenticated(page, context, platform, False))


def test_tencent_live_qr_selector_captures_only_trusted_frame_element():
    image = Locator(True, image=png())
    page = Surface("https://channels.weixin.qq.com/login.html")
    unrelated = Surface("https://other.test/connect/qrconnect", selectors={"img.js_qrcode_img": image})
    frame = Surface("https://open.weixin.qq.com/connect/qrconnect?private=not-returned",
                    selectors={"img.js_qrcode_img.web_qrcode_img": image})
    page.frames.extend([unrelated, frame])
    selected = asyncio.run(login._find_qr(page, "tencent"))
    assert selected == (frame, image)
    assert asyncio.run(login._qr_png(selected[1])) == png()
    assert image.captured


def test_qr_does_not_fall_back_to_logos_or_tiny_images():
    page = Surface("https://creator.douyin.com/", selectors={
        "img": Locator(True, image=png()),
        'img[aria-label="二维码"]': Locator(True, image=png(), box={"width": 30, "height": 30}),
    })
    assert asyncio.run(login._find_qr(page, "douyin")) is None


@pytest.mark.parametrize("image", [b"not-png", png(40, 40), png(2048, 2048),
                                    png(100, 800), png(suffix=b"x" * (512 * 1024))],
                         ids=["not-png", "too-small", "too-large", "not-square", "too-many-bytes"])
def test_invalid_or_oversized_qr_rejected(image):
    assert asyncio.run(login._qr_png(Locator(True, image=image))) is None


def test_initial_douyin_sms_form_is_not_additional_verification():
    page = Surface("https://creator.douyin.com/", selectors={
        'input[placeholder*="验证码"], input[placeholder*="短信"]': Locator(True),
    })
    assert not asyncio.run(login._verification(page, "douyin", False, True))
    assert asyncio.run(login._verification(page, "douyin", True, False))


def test_login_success_saves_fresh_state_and_closes(monkeypatch, tmp_path):
    page = Surface("https://creator.douyin.com/creator-micro/home", texts=("发布视频",))
    data, context, browser = install_runtime(monkeypatch, tmp_path, page)
    account = Path(data["account_file"])
    account.write_text("old-state-must-not-be-loaded", encoding="utf-8")
    events = []
    result = asyncio.run(login.login_browser(data, lambda phase, **payload: events.append((phase, payload))))
    assert result == ("ready", "account_ready")
    assert json.loads(account.read_text(encoding="utf-8")) == state()
    assert context.closed and browser.closed
    assert not list(tmp_path.glob("login-state-*.json"))
    assert all(payload == {"qr_png": None, "expires_at": None} for _, payload in events)
    assert "secret" not in repr(events)


def test_timeout_hides_qr_closes_and_preserves_old_account(monkeypatch, tmp_path):
    page = Surface("https://creator.douyin.com/")
    page.block_navigation = True
    data, context, browser = install_runtime(monkeypatch, tmp_path, page)
    data["timeout_seconds"] = 0.01
    account = Path(data["account_file"])
    account.write_bytes(b"old-account")
    events = []
    result = asyncio.run(login.login_browser(data, lambda phase, **payload: events.append((phase, payload))))
    assert result == ("failed", "login_expired")
    assert account.read_bytes() == b"old-account"
    assert events[-1] == ("expired", {"qr_png": None, "expires_at": None})
    assert context.closed and browser.closed


def test_verification_is_explicit_and_does_not_overwrite_account(monkeypatch, tmp_path):
    page = Surface("https://creator.douyin.com/", texts=("请完成安全验证",))
    data, context, browser = install_runtime(monkeypatch, tmp_path, page)
    account = Path(data["account_file"])
    account.write_bytes(b"old-account")
    events = []
    result = asyncio.run(login.login_browser(data, lambda phase, **payload: events.append((phase, payload))))
    assert result == ("failed", "login_verification_required")
    assert events[-1] == ("verification_required", {"qr_png": None, "expires_at": None})
    assert account.read_bytes() == b"old-account"
    assert context.closed and browser.closed


def test_expiry_refresh_is_bounded_and_uses_only_fixed_login_url(monkeypatch, tmp_path):
    page = Surface("https://creator.douyin.com/", texts=("二维码失效",))
    data, _, _ = install_runtime(monkeypatch, tmp_path, page)
    events = []
    result = asyncio.run(login.login_browser(data, lambda phase, **payload: events.append((phase, payload))))
    assert result == ("failed", "login_expired")
    assert page.visits == ["https://creator.douyin.com/"] * 4
    assert not Path(data["account_file"]).exists()
    assert all(payload["qr_png"] is None for _, payload in events)


def test_qr_snapshot_and_cancellation_close_without_saving(monkeypatch, tmp_path):
    page = Surface("https://creator.douyin.com/", selectors={
        'img[aria-label="二维码"]': Locator(True, image=png()),
    })
    data, context, browser = install_runtime(monkeypatch, tmp_path, page)
    events = []

    def emit(phase, **payload):
        events.append((phase, payload))
        if phase == "waiting_scan":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(login.login_browser(data, emit))
    assert events[-1] == ("waiting_scan", {"qr_png": png(), "expires_at": None})
    assert context.closed and browser.closed
    assert not Path(data["account_file"]).exists()


def test_scan_hides_old_qr_until_new_code(monkeypatch, tmp_path):
    image = Locator(True, image=png())
    page = Surface("https://creator.douyin.com/", selectors={'img[aria-label="二维码"]': image})
    data, _, _ = install_runtime(monkeypatch, tmp_path, page)
    events = []
    checks = 0

    async def scanned(*args):
        nonlocal checks
        checks += 1
        if checks == 2:
            return True
        if checks == 4:
            image.image = png(suffix=b"rotated")
        return False

    monkeypatch.setattr(login, "_scanned", scanned)

    def emit(phase, **payload):
        events.append((phase, payload))
        if phase == "waiting_scan" and payload["qr_png"] == png(suffix=b"rotated"):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(login.login_browser(data, emit))
    assert [phase for phase, _ in events] == ["preparing", "waiting_scan", "scanned", "waiting_scan"]
    assert events[2][1]["qr_png"] is None


def test_invalid_platform_does_not_launch_browser():
    assert asyncio.run(login.login_browser({"platform": "bilibili"}, lambda *_: None)) == (
        "failed", "invalid_platform")


def test_missing_qr_has_bounded_failure_without_account_write(monkeypatch, tmp_path):
    page = Surface("https://creator.douyin.com/")
    data, context, browser = install_runtime(monkeypatch, tmp_path, page)
    monkeypatch.setattr(login, "QR_WAIT_SECONDS", 0)
    assert asyncio.run(login.login_browser(data, lambda *_, **__: None)) == (
        "failed", "login_qr_unavailable")
    assert not Path(data["account_file"]).exists()
    assert context.closed and browser.closed


def test_expired_qr_is_never_reexposed_after_refresh(monkeypatch, tmp_path):
    image = Locator(True, image=png())
    page = Surface("https://creator.douyin.com/", selectors={'img[aria-label="二维码"]': image})
    data, _, _ = install_runtime(monkeypatch, tmp_path, page)
    checks = 0
    events = []

    async def expired(*args):
        nonlocal checks
        checks += 1
        if checks > 3:
            raise asyncio.CancelledError()
        return checks == 2

    monkeypatch.setattr(login, "_expired", expired)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(login.login_browser(data, lambda phase, **payload: events.append((phase, payload))))
    assert [phase for phase, _ in events] == ["preparing", "waiting_scan", "expired", "preparing"]
    assert len([entry for entry in events if entry[1]["qr_png"] is not None]) == 1


def test_atomic_write_failure_preserves_previous_account(monkeypatch, tmp_path):
    account = tmp_path / "account.json"
    account.write_bytes(b"previous-account")

    def blocked_replace(*args):
        raise OSError("private detail must not be returned")

    monkeypatch.setattr(login.os, "replace", blocked_replace)
    page = Surface("https://creator.douyin.com/creator-micro/home", texts=("发布视频",))
    data, _, _ = install_runtime(monkeypatch, tmp_path, page)
    assert asyncio.run(login.login_browser(data, lambda *_, **__: None)) == ("failed", "login_failed")
    assert account.read_bytes() == b"previous-account"
    assert not list(tmp_path.glob("login-state-*.json"))


@pytest.mark.parametrize("platform,domain,name", [
    ("douyin", "douyin.com.evil.test", "sessionid"),
    ("tencent", "weixin.qq.com.evil.test", "sessionid"),
    ("douyin", ".douyin.com", "anonymous-id"),
])
def test_unrelated_cookies_cannot_establish_login(platform, domain, name):
    assert not login._session_state({"cookies": [{"domain": domain, "name": name, "value": "x"}]}, platform)

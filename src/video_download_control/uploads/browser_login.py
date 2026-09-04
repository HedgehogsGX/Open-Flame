"""Isolated, headless QR login; only QR pixels and fixed states leave the child.

The initial QR selectors were checked against the unauthenticated pages on
2026-09-04 and social-auto-upload 0012d2c355f88f683cc38dde2a2db209e14091bc.
Authentication and extra verification still require real user interaction.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


LOGIN_URLS = {
    "douyin": "https://creator.douyin.com/",
    "tencent": "https://channels.weixin.qq.com/",
}
QR_SELECTORS = {
    "douyin": (
        'img[aria-label="二维码"]',
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
    ),
    "tencent": (
        "img.js_qrcode_img.web_qrcode_img",
        "img.js_qrcode_img",
        "div.login-qrcode-wrap img.qrcode",
        "div.qrcode-wrap img.qrcode",
        "img.qrcode",
    ),
}
AUTH_MARKERS = {
    "douyin": ("发布视频", "作品管理", "内容管理"),
    "tencent": ("发表视频", "内容管理", "发表", "保存草稿"),
}
EXPIRED_TEXT = ("二维码失效", "二维码已失效", "二维码已过期", "二维码已过期，点击刷新")
SCANNED_TEXT = (
    "已扫码", "扫码成功", "扫描成功", "需在手机上进行确认",
    "请在手机上确认登录", "请在微信中点击确认登录", "请在微信中点击确认即可登录",
)
VERIFICATION_TEXT = (
    "请完成安全验证", "请完成下方验证", "请先完成验证", "拖动滑块完成拼图",
    "请进行身份验证", "需要进行身份验证", "请完成短信验证", "请完成实名认证",
)
POLL_SECONDS = 1.5
QR_WAIT_SECONDS = 45.0
MAX_QR_BYTES = 512 * 1024


def _trusted_url(value: str, host: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname == host
                and parsed.port in {None, 443} and not parsed.username and not parsed.password)
    except (TypeError, ValueError):
        return False


def _management_url(value: str, platform: str) -> bool:
    host = "creator.douyin.com" if platform == "douyin" else "channels.weixin.qq.com"
    if not _trusted_url(value, host):
        return False
    path = urlsplit(value).path
    if platform == "douyin":
        return path == "/creator-micro" or path.startswith("/creator-micro/")
    return path == "/platform" or path.startswith("/platform/")


def _surfaces(page, platform: str):
    """Never inspect arbitrary embedded frames for credentials or QR images."""
    host = "creator.douyin.com" if platform == "douyin" else "channels.weixin.qq.com"
    if _trusted_url(page.url, host):
        yield page
    if platform == "tencent":
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if (_trusted_url(frame.url, "open.weixin.qq.com")
                    and urlsplit(frame.url).path == "/connect/qrconnect"):
                yield frame
            elif (_trusted_url(frame.url, host)
                  and "login-for-iframe" in urlsplit(frame.url).path):
                yield frame


async def _visible(locator) -> bool:
    try:
        return await locator.is_visible(timeout=500)
    except Exception:
        return False


async def _text_present(surface, texts: tuple[str, ...]) -> bool:
    for text in texts:
        if await _any_visible(surface.get_by_text(text, exact=True)):
            return True
    return False


async def _any_visible(locator) -> bool:
    try:
        for index in range(min(await locator.count(), 8)):
            if await _visible(locator.nth(index)):
                return True
    except Exception:
        pass
    return False


async def _find_qr(page, platform: str):
    for surface in _surfaces(page, platform):
        for selector in QR_SELECTORS[platform]:
            candidates = surface.locator(selector)
            try:
                count = min(await candidates.count(), 8)
            except Exception:
                continue
            for index in range(count):
                qr = candidates.nth(index)
                if not await _visible(qr):
                    continue
                try:
                    box = await qr.bounding_box(timeout=1000)
                    loaded = await qr.evaluate("e => e.tagName === 'IMG' && e.complete && e.naturalWidth > 0")
                    if (loaded and box and 80 <= box["width"] <= 1024
                            and 80 <= box["height"] <= 1024
                            and 0.75 <= box["width"] / box["height"] <= 1.34):
                        return surface, qr
                except Exception:
                    continue
    return None


def _valid_png(png: bytes) -> bool:
    if not isinstance(png, bytes) or not 24 <= len(png) <= MAX_QR_BYTES:
        return False
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return False
    width, height = struct.unpack(">II", png[16:24])
    return 80 <= width <= 1024 and 80 <= height <= 1024 and 0.75 <= width / height <= 1.34


async def _qr_png(qr) -> bytes | None:
    try:
        # Element capture may wait for the login page's initial layout; the
        # outer login deadline still bounds this wait.
        png = await qr.screenshot(type="png", timeout=10000, animations="disabled")
        return png if _valid_png(png) else None
    except Exception:
        return None


async def _expired(page, platform: str) -> bool:
    for surface in _surfaces(page, platform):
        if await _text_present(surface, EXPIRED_TEXT):
            return True
        # The currently served WeChat QR widget exposes its refresh control only
        # when a QR can no longer be used. Never click an arbitrary page button.
        if platform == "tencent" and await _any_visible(surface.locator("button.js_refresh_qrcode")):
            return True
    return False


async def _scanned(page, platform: str) -> bool:
    return any([await _text_present(surface, SCANNED_TEXT)
                for surface in _surfaces(page, platform)])


async def _verification(page, platform: str, scanned: bool, has_qr: bool) -> bool:
    for surface in _surfaces(page, platform):
        if await _text_present(surface, VERIFICATION_TEXT):
            return True
        for selector in ('iframe[src*="captcha"]', '#captcha-verify-image',
                         '[class*="captcha_verify_container"]', '[id*="captcha-verify"]'):
            if await _visible(surface.locator(selector).first):
                return True
        # The initial Douyin page has a normal SMS form next to its QR. Only a
        # post-scan form without the login QR is an additional verification step.
        if scanned and not has_qr:
            selector = 'input[placeholder*="验证码"], input[placeholder*="短信"]'
            if await _visible(surface.locator(selector).first):
                return True
    return False


def _session_state(state, platform: str) -> bool:
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        return False
    domain = "douyin.com" if platform == "douyin" else "weixin.qq.com"
    for cookie in state["cookies"]:
        if not isinstance(cookie, dict):
            continue
        scope = str(cookie.get("domain", "")).lstrip(".")
        if scope != domain and not scope.endswith("." + domain):
            continue
        if not cookie.get("value"):
            continue
        if platform == "douyin" and cookie.get("name") != "sessionid":
            continue
        return True
    return False


async def _authenticated(page, context, platform: str, has_qr: bool) -> bool:
    if has_qr or not _management_url(page.url, platform):
        return False
    if await _text_present(page, ("扫码登录", "手机号登录", "登录视频号助手")):
        return False
    if not await _text_present(page, AUTH_MARKERS[platform]):
        return False
    # Tencent's source does not define a stable cookie name contract. Require
    # protected management UI plus nonempty state scoped to its own domain;
    # Douyin additionally requires the sessionid used by the pinned uploader.
    return _session_state({"cookies": await context.cookies()}, platform)


def _save_state(state: dict, account_file: Path, operation_dir: Path, platform: str) -> None:
    if not _session_state(state, platform):
        raise ValueError("login_failed")
    if account_file.is_symlink() or operation_dir.is_symlink():
        raise ValueError("login_failed")
    account_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="login-state-", suffix=".json", dir=operation_dir)
    pending = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, account_file)
    finally:
        pending.unlink(missing_ok=True)


async def _close(target) -> None:
    if target is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(target.close(), timeout=3)


async def _login(data: dict, emit) -> tuple[str, str]:
    from patchright.async_api import async_playwright

    platform = data["platform"]
    account_file = Path(data["account_file"])
    operation_dir = Path(data["operation_dir"])
    browser_path = Path(data["browser_path"])
    if (not account_file.is_absolute() or not operation_dir.is_absolute()
            or not operation_dir.is_dir() or not browser_path.is_absolute()
            or account_file.is_symlink() or operation_dir.is_symlink()):
        return "failed", "login_failed"
    last_snapshot = None

    def snapshot(phase: str, png: bytes | None = None):
        nonlocal last_snapshot
        key = (phase, hashlib.sha256(png).digest() if png else None)
        if key != last_snapshot:
            emit(phase, qr_png=png if phase == "waiting_scan" else None, expires_at=None)
            last_snapshot = key

    snapshot("preparing")
    async with async_playwright() as playwright:
        browser = context = None
        try:
            browser = await playwright.chromium.launch(
                executable_path=str(browser_path), headless=True, timeout=30000,
            )
            # A new context with no storage_state argument is intentional: the
            # old account must not silently log in on the user's behalf.
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            context.set_default_timeout(3000)
            page = await context.new_page()
            await page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded", timeout=45000)
            loop = asyncio.get_running_loop()
            qr_deadline = loop.time() + QR_WAIT_SECONDS
            seen_qr = scanned = False
            last_qr_digest = None
            expired_qr_digests = set()
            refreshes = authenticated_checks = 0
            while True:
                found = await _find_qr(page, platform)
                if await _verification(page, platform, scanned, found is not None):
                    snapshot("verification_required")
                    return "failed", "login_verification_required"
                if await _authenticated(page, context, platform, found is not None):
                    authenticated_checks += 1
                    if authenticated_checks >= 2:
                        state = await context.storage_state()
                        _save_state(state, account_file, operation_dir, platform)
                        return "ready", "account_ready"
                    snapshot("scanned")
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                authenticated_checks = 0
                if await _expired(page, platform):
                    snapshot("expired")
                    if last_qr_digest is not None:
                        expired_qr_digests.add(last_qr_digest)
                    if refreshes >= 3:
                        return "failed", "login_expired"
                    refreshes += 1
                    seen_qr = scanned = False
                    # Reload only the fixed login entry point. This obtains a
                    # fresh QR without clicking a login, SMS, or publish action.
                    await page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded", timeout=30000)
                    qr_deadline = loop.time() + QR_WAIT_SECONDS
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                if await _scanned(page, platform):
                    scanned = True
                    snapshot("scanned")
                elif found:
                    png = await _qr_png(found[1])
                    if png is not None:
                        digest = hashlib.sha256(png).digest()
                        if digest in expired_qr_digests:
                            snapshot("preparing")
                        elif not scanned or digest != last_qr_digest:
                            seen_qr = True
                            scanned = False
                            snapshot("waiting_scan", png)
                        last_qr_digest = digest
                    elif not scanned:
                        snapshot("preparing")
                elif not scanned:
                    # A disappearing or loading QR must not leave its old pixels
                    # available to the front end, even during a frame reload.
                    snapshot("preparing")
                if not seen_qr and loop.time() >= qr_deadline:
                    return "failed", "login_expired" if refreshes else "login_qr_unavailable"
                await asyncio.sleep(POLL_SECONDS)
        finally:
            await _close(context)
            await _close(browser)


async def login_browser(data: dict, emit) -> tuple[str, str]:
    """Run one user-requested login; emit synchronously, never include URLs.

    ``waiting_scan`` is a complete PNG snapshot; all other phases clear it.
    QR expiry timestamps are deliberately unknown. Overall waiting is bounded
    independently from the parent process's cancellation/Windows Job lifetime.
    Cancellation propagates after closing this login's context and browser.
    """
    if not isinstance(data, dict) or data.get("platform") not in LOGIN_URLS:
        return "failed", "invalid_platform"
    try:
        seconds = float(data.get("timeout_seconds", 290))
        if not 0 < seconds <= 300:
            return "failed", "login_failed"
        async with asyncio.timeout(seconds):
            return await _login(data, emit)
    except TimeoutError:
        emit("expired", qr_png=None, expires_at=None)
        return "failed", "login_expired"
    except Exception:
        return "failed", "login_failed"

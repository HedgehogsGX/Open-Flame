"""Standalone child: only fixed result codes cross back to the host process.

This file runs with the optional runtime Python, without importing Open-Flame's
FastAPI environment. Upstream code remains in its pinned external source tree.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from urllib.parse import urlsplit


class SubmissionUncertain(BaseException):
    """Escape upstream catch/retry loops after an uncertain submission."""


class SilentLogger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def configure_source(source: Path, operation: Path, browser_path: Path | None = None) -> None:
    sys.path.insert(0, str(source))
    config = types.ModuleType("conf")
    config.BASE_DIR = operation
    config.XHS_SERVER = "http://127.0.0.1:11901"
    config.LOCAL_CHROME_PATH = str(browser_path or source.parent / "chrome" / "chrome.exe")
    config.LOCAL_CHROME_HEADLESS = False
    config.DEBUG_MODE = False
    config.YT_PROXY = None
    sys.modules["conf"] = config
    # Upstream utils.log normally writes rich account/page information to disk.
    # Supply its logging interface before any uploader import, with no sinks.
    logs = types.ModuleType("utils.log")
    for name in ("douyin", "tencent", "xhs", "tiktok", "bilibili", "kuaishou",
                 "baijiahao", "xiaohongshu", "youtube", "alipay", "weibo", "hupu"):
        setattr(logs, name + "_logger", SilentLogger())
    sys.modules["utils.log"] = logs
    assets = importlib.import_module("utils.base_social_media")
    assets.BASE_DIR = source
    from patchright.async_api import BrowserType
    launch = BrowserType.launch

    async def launch_owned(browser_type, *args, **kwargs):
        if browser_type.name == "chromium":
            kwargs.pop("channel", None)
            kwargs["executable_path"] = config.LOCAL_CHROME_PATH
        return await launch(browser_type, *args, **kwargs)

    BrowserType.launch = launch_owned


def tencent_create_response(response) -> bool:
    url = urlsplit(response.url)
    return (url.scheme == "https" and url.hostname == "channels.weixin.qq.com"
            and url.port in {None, 443} and response.request.method == "POST"
            and url.path in {"/micro/content/cgi-bin/mmfinderassistant-bin/post/post_create",
                             "/cgi-bin/mmfinderassistant-bin/post/post_create"})


async def submit_tencent_once(uploader, page) -> None:
    """Use a server acknowledgement, not a disappearing button, for publish."""
    await uploader._dismiss_switch_account_dialog(page)
    draft = uploader.is_draft
    button = page.get_by_role("button", name="保存草稿" if draft else "发表", exact=True).first
    await button.wait_for(state="visible", timeout=60000)
    if draft:
        await button.click(timeout=10000)
        try:
            await page.wait_for_url("**/post/list**", timeout=30000)
            url = urlsplit(page.url)
            if (url.scheme != "https" or url.hostname != "channels.weixin.qq.com"
                    or url.path not in {"/platform/post/list", "/micro/content/post/list"}):
                raise SubmissionUncertain()
        except Exception:
            raise SubmissionUncertain() from None
        uploader._open_flame_acknowledged = True
        return
    try:
        async with page.expect_response(tencent_create_response, timeout=30000) as observed:
            await button.click(timeout=10000)
        response = await observed.value
        if response.status != 200:
            raise SubmissionUncertain()
        data = await response.json()
        if not isinstance(data, dict) or type(data.get("errCode")) is not int or data["errCode"] != 0:
            raise SubmissionUncertain()
        uploader._open_flame_acknowledged = True
    except Exception:
        raise SubmissionUncertain() from None


class SingleSubmission:
    """Permit one final action; never click it again after an ambiguous outcome."""
    def __init__(self):
        self.attempted = False

    def before(self, label: str) -> bool:
        if "".join(label.split()) not in {"发布", "发表", "保存草稿"}:
            return False
        if self.attempted:
            raise SubmissionUncertain()
        self.attempted = True
        return True

    def install(self) -> None:
        from patchright.async_api import Locator
        click = Locator.click
        evaluate = Locator.evaluate
        guard = self

        async def label(locator):
            return await evaluate(locator, "el => (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') ? (el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim() : ''", timeout=3000)

        async def safe_click(locator, *args, **kwargs):
            final = guard.before(await label(locator))
            try:
                return await click(locator, *args, **kwargs)
            except Exception:
                if final:
                    raise SubmissionUncertain() from None
                raise

        async def safe_evaluate(locator, expression, *args, **kwargs):
            # The pinned Tencent adapter uses this fallback when click fails.
            if ".click(" in expression:
                final = guard.before(await label(locator))
                try:
                    return await evaluate(locator, expression, *args, **kwargs)
                except Exception:
                    if final:
                        raise SubmissionUncertain() from None
                    raise
            return await evaluate(locator, expression, *args, **kwargs)

        Locator.click = safe_click
        Locator.evaluate = safe_evaluate


def bilibili_arguments(payload: dict, account: Path) -> list[str]:
    copyright_value = payload["copyright"]
    source = payload.get("source_credit", "")
    if copyright_value not in {1, 2} or (copyright_value == 2 and not source.strip()):
        raise ValueError("invalid_bilibili_metadata")
    return ["-u", str(account), "upload", payload["file_path"],
            "--title", payload["title"], "--desc", payload["description"],
            "--tid", str(payload["category_id"]), "--tag", ",".join(payload["tags"]),
            "--copyright", str(copyright_value), "--source", source]


def run_bilibili(data: dict) -> tuple[str, str]:
    action = data["action"]
    account = Path(data["account_file"])
    binary = data["biliup"]
    if action == "login":
        if os.name != "nt":
            return "failed", "login_terminal_unavailable"
        # This console is opened only for an explicit UI login action. Its QR
        # code/prompts stay in the local console, never in the HTTP response.
        rc = subprocess.run([binary, "-u", str(account), "login"], check=False).returncode
        return ("ready", "account_ready") if rc == 0 and account.is_file() else ("failed", "login_failed")
    if not account.is_file():
        return "failed", "account_missing"
    rc = subprocess.run([binary, "-u", str(account), "renew"], check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL).returncode
    if rc != 0:
        return "failed", "account_invalid"
    if action == "check":
        return "ready", "account_ready"
    command = [binary, *bilibili_arguments(data["payload"], account)]
    rc = subprocess.run(command, check=False, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL).returncode
    return ("submitted", "upstream_submitted") if rc == 0 else ("unknown", "upstream_result_unknown")


async def run_browser(data: dict) -> tuple[str, str]:
    platform = data["platform"]
    module = importlib.import_module(f"uploader.{platform}_uploader.main")
    action = data["action"]
    account = data["account_file"]
    if action == "login":
        setup = module.douyin_setup if platform == "douyin" else module.tencent_setup
        result = await setup(account, handle=True, return_detail=True, headless=False)
        success = isinstance(result, dict) and result.get("success") is True
        return ("ready", "account_ready") if success and Path(account).is_file() else ("failed", "login_failed")
    # Never ask setup(handle=True) during check/upload: authentication is a
    # separate, explicit action. Existing browser profiles are not imported.
    if not Path(account).is_file():
        return "failed", "account_missing"
    if not await module.cookie_auth(account):
        return "failed", "account_invalid"
    if action == "check":
        return "ready", "account_ready"
    request = data["payload"]
    guard = SingleSubmission()
    guard.install()
    options = dict(title=request["title"], file_path=request["file_path"],
                   tags=request["tags"], publish_date=0, account_file=account,
                   desc=request["description"], debug=False, headless=False)
    if platform == "douyin":
        async def no_shared_verification_file(_file):
            # An SMS challenge is resolved manually in the visible browser.
            # Never read a leftover verification code from another operation.
            return ""
        module._read_verify_code = no_shared_verification_file
        uploader = module.DouYinVideo(**options)
        await uploader.douyin_upload_video()
    else:
        uploader = module.TencentVideo(**options, is_draft=request.get("mode") == "draft")
        uploader._open_flame_acknowledged = False
        uploader.submit_publish = types.MethodType(submit_tencent_once, uploader)
        await uploader.tencent_upload_video()
        if not uploader._open_flame_acknowledged:
            return "unknown", "upstream_result_unknown"
    if not guard.attempted:
        return "unknown", "upstream_result_unknown"
    if request.get("mode") == "draft":
        return "draft_saved", "upstream_draft_saved"
    return "submitted", "upstream_submitted"


def check_install(source: Path, operation: Path) -> int:
    configure_source(source, operation)
    cli = importlib.import_module("sau_cli")
    # Exercise the actual pinned CLI parser for every enabled operation, without
    # invoking login, renewal or upload.
    for platform in ("bilibili", "douyin", "tencent"):
        for action in ("login", "check", "upload-video"):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    cli.build_parser().parse_args([platform, action, "--help"])
                except SystemExit as exc:
                    if exc.code != 0:
                        return 1
    print(json.dumps({"status": "ready", "code": "cli_help_verified"}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", type=Path, required=True)
    parser.add_argument("--check-install", type=Path)
    args = parser.parse_args()
    if args.check_install:
        try:
            return check_install(args.check_install, args.operation)
        except Exception:
            print('{"status":"failed","code":"cli_help_failed"}')
            return 1
    operation = args.operation
    deadline = time.monotonic() + 15
    while not (operation / "go").is_file():
        if time.monotonic() > deadline:
            return 1
        time.sleep(0.02)
    data = json.loads((operation / "request.json").read_text(encoding="utf-8"))
    result = ("unknown", "upstream_result_unknown") if data["action"] == "upload" else ("failed", "backend_failed")
    try:
        if data["action"] == "login" and data.get("inline_login") is True:
            # Only our own sibling modules are loaded, inside the owned child.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from login_progress import update_writer
            emit = update_writer(operation)
            emit("preparing")
            if data["platform"] == "bilibili":
                from bilibili_login import login_bilibili
                result = login_bilibili(data, emit)
            else:
                from browser_login import login_browser
                result = asyncio.run(login_browser(data, emit))
        elif data["platform"] == "bilibili":
            result = run_bilibili(data)
        else:
            configure_source(Path(data["source"]), operation, Path(data["browser_path"]))
            result = asyncio.run(run_browser(data))
    except (Exception, SubmissionUncertain):
        pass
    temp = operation / "result.tmp"
    temp.write_text(json.dumps({"status": result[0], "code": result[1]}), encoding="utf-8")
    temp.replace(operation / "result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

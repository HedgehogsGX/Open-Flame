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
import re
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_SCHEDULE_LEAD_SECONDS = {
    # Platform-side minimums.  The host service/backend already reserved the
    # two-hour upload timeout and process overhead before launching this child.
    "bilibili": 4 * 3600,
    "douyin": 2 * 3600,
    "tencent": 2 * 3600,
}
_SCHEDULE_FINAL_ACTION_MARGIN_SECONDS = 60
_TENCENT_SCHEDULE_MAX_SECONDS = 28 * 24 * 3600
_DOUYIN_DECLARATIONS = frozenset({
    "内容由AI生成",
    "内容为转载信息",
    "内容为个人观点或见解",
})
_TENCENT_CONTENT_LABELS = frozenset({"含AI生成内容"})
_NO_DOUYIN_DECLARATION = "__open_flame_no_declaration__"


class SubmissionUncertain(BaseException):
    """Escape upstream catch/retry loops after an uncertain submission."""


class ScheduleWindowElapsed(SubmissionUncertain):
    """Escape upstream retry loops before an expired scheduled submission."""


class PlatformParameterMismatch(SubmissionUncertain):
    """Escape upstream retry loops before a final action with stale parameters."""


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
    def __init__(self, platform: str | None = None, publish_at_unix: int | None = None):
        self.attempted = False
        self.platform = platform
        self.publish_at_unix = publish_at_unix
        self._final_preflights = {}

    def register_final_preflight(self, key: str, callback) -> None:
        self._final_preflights[key] = callback

    def _check(self, label: str) -> bool:
        normalized = "".join(label.split())
        if normalized not in {"发布", "发表", "保存草稿"}:
            return False
        if self.attempted:
            raise SubmissionUncertain()
        if normalized in {"发布", "发表"} and self.publish_at_unix is not None:
            lead = _SCHEDULE_LEAD_SECONDS.get(self.platform)
            if (
                lead is None
                or self.publish_at_unix
                <= int(time.time()) + lead + _SCHEDULE_FINAL_ACTION_MARGIN_SECONDS
            ):
                raise ScheduleWindowElapsed("schedule_window_elapsed")
        return True

    def before(self, label: str) -> bool:
        final = self._check(label)
        if final:
            self.attempted = True
        return final

    async def before_click(self, label: str) -> bool:
        final = self._check(label)
        if not final:
            return False
        try:
            for callback in tuple(self._final_preflights.values()):
                await callback()
        except Exception:
            raise PlatformParameterMismatch("platform_parameter_mismatch") from None
        # A DOM read can wait. Recheck the clock immediately before marking the
        # one permitted final action as attempted.
        self._check(label)
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
            final = await guard.before_click(await label(locator))
            try:
                return await click(locator, *args, **kwargs)
            except Exception:
                if final:
                    raise SubmissionUncertain() from None
                raise

        async def safe_evaluate(locator, expression, *args, **kwargs):
            # The pinned Tencent adapter uses this fallback when click fails.
            if ".click(" in expression:
                final = await guard.before_click(await label(locator))
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
    category_id = payload.get("category_id")
    if (type(copyright_value) is not int or copyright_value not in {1, 2}
            or not isinstance(source, str)
            or copyright_value == 2 and not source.strip()
            or copyright_value == 1 and bool(source.strip())
            or type(category_id) is not int or not 1 <= category_id <= 65535
            or payload.get("mode", "publish") != "publish"):
        raise ValueError("invalid_bilibili_metadata")
    if (payload.get("declaration") is not None
            or payload.get("short_title") is not None
            or payload.get("content_label") is not None):
        raise ValueError("invalid_bilibili_metadata")
    covers = [payload.get("cover_landscape_path"), payload.get("cover_portrait_path")]
    if sum(cover is not None for cover in covers) > 1:
        raise ValueError("invalid_bilibili_metadata")
    cover = next((cover for cover in covers if cover is not None), None)
    if cover is not None:
        _checked_cover_path(cover)
    publish_at, _ = _checked_publish_schedule(
        "bilibili", payload.get("publish_at_unix"),
        payload.get("publish_timezone_offset_minutes"),
    )
    dynamic = payload.get("dynamic", "")
    if not isinstance(dynamic, str) or len(dynamic) > 250 or "\x00" in dynamic:
        raise ValueError("invalid_bilibili_metadata")
    flags = (payload.get("no_reprint", False), payload.get("close_comments", False),
             payload.get("close_danmu", False))
    if any(type(flag) is not bool for flag in flags):
        raise ValueError("invalid_bilibili_metadata")
    arguments = ["-u", str(account), "upload", payload["file_path"],
                 "--title", payload["title"], "--desc", payload["description"],
                 "--tid", str(category_id), "--tag", ",".join(payload["tags"]),
                 "--copyright", str(copyright_value), "--source", source]
    if cover is not None:
        arguments.extend(["--cover", cover])
    if publish_at is not None:
        arguments.extend(["--dtime", str(publish_at)])
    if dynamic:
        arguments.extend(["--dynamic", dynamic])
    if flags[0]:
        arguments.extend(["--no-reprint", "1"])
    if flags[1]:
        arguments.append("--up-close-reply")
    if flags[2]:
        arguments.append("--up-close-danmu")
    if flags[1] or flags[2]:
        arguments.extend(["--submit", "app"])
    return arguments


def _checked_cover_path(value) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_platform_metadata")
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
        raise ValueError("invalid_platform_metadata")
    return value


def _checked_publish_schedule(platform: str, value, offset) -> tuple[int | None, int | None]:
    if value is None:
        if offset is not None:
            raise ValueError("invalid_platform_metadata")
        return None, None
    if (
        type(value) is not int
        or not 1_700_000_000 <= value <= 4_102_444_800
        or type(offset) is not int
        or not -840 <= offset <= 840
        or value % 60
        or platform == "tencent" and (
            (value + offset * 60) % 3600
            or value > int(time.time()) + _TENCENT_SCHEDULE_MAX_SECONDS
        )
    ):
        raise ValueError("invalid_platform_metadata")
    if value <= int(time.time()) + _SCHEDULE_LEAD_SECONDS[platform]:
        raise ScheduleWindowElapsed("schedule_window_elapsed")
    return value, offset


def browser_upload_options(platform: str, request: dict, account: str) -> dict:
    """Map only Open-Flame's fixed contract to the pinned browser uploaders."""
    if platform not in {"douyin", "tencent"}:
        raise ValueError("invalid_platform")
    if (request.get("dynamic", "") != ""
            or request.get("no_reprint", False) is not False
            or request.get("close_comments", False) is not False
            or request.get("close_danmu", False) is not False):
        raise ValueError("invalid_platform_metadata")
    publish_at, publish_offset = _checked_publish_schedule(
        platform, request.get("publish_at_unix"),
        request.get("publish_timezone_offset_minutes"),
    )
    mode = request.get("mode", "publish")
    if mode not in {"publish", "draft"} or platform == "douyin" and mode != "publish":
        raise ValueError("invalid_platform_metadata")
    if mode == "draft" and publish_at is not None:
        raise ValueError("invalid_platform_metadata")
    publish_date = (
        datetime.fromtimestamp(
            publish_at, timezone(timedelta(minutes=publish_offset))
        )
        if publish_at is not None
        else 0
    )
    publish_strategy = "scheduled" if publish_at is not None else "immediate"
    landscape = request.get("cover_landscape_path")
    portrait = request.get("cover_portrait_path")
    if landscape is not None:
        landscape = _checked_cover_path(landscape)
    if portrait is not None:
        portrait = _checked_cover_path(portrait)
    options = dict(
        title=request["title"],
        file_path=request["file_path"],
        tags=request["tags"],
        publish_date=publish_date,
        publish_strategy=publish_strategy,
        account_file=account,
        desc=request["description"],
        thumbnail_landscape_path=landscape,
        thumbnail_portrait_path=portrait,
        debug=False,
        headless=False,
    )
    if platform == "douyin":
        declaration = request.get("declaration")
        if landscape is not None and portrait is not None:
            raise ValueError("invalid_platform_metadata")
        if declaration is not None and declaration not in _DOUYIN_DECLARATIONS:
            raise ValueError("invalid_platform_metadata")
        if request.get("short_title") is not None or request.get("content_label") is not None:
            raise ValueError("invalid_platform_metadata")
        options["declaration"] = declaration
        return options

    short_title = request.get("short_title")
    content_label = request.get("content_label")
    if request.get("declaration") is not None:
        raise ValueError("invalid_platform_metadata")
    if short_title is not None and (
        not isinstance(short_title, str)
        or short_title.strip() != short_title
        or not 7 <= len(short_title) <= 15
        or "\x00" in short_title
    ):
        raise ValueError("invalid_platform_metadata")
    if content_label is not None and content_label not in _TENCENT_CONTENT_LABELS:
        raise ValueError("invalid_platform_metadata")
    options.update(short_title=short_title, is_draft=mode == "draft")
    return options


async def _no_statement(_uploader, _page) -> None:
    return None


async def _no_short_title(_uploader, _page, _title, _short_title=None) -> None:
    return None


def _input_parts(value: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", value)]


async def _require_datetime_value(locator, expected: datetime, *, combined: bool) -> None:
    if not await locator.count():
        raise RuntimeError("schedule_control_unavailable")
    value = (await locator.input_value()).strip()
    parts = _input_parts(value)
    if combined:
        valid = len(parts) >= 5 and parts[-5:] == [
            expected.year, expected.month, expected.day, expected.hour, expected.minute,
        ]
    else:
        valid = (
            len(parts) >= 3
            and parts[-3:] == [expected.year, expected.month, expected.day]
        ) or len(parts) == 2 and parts == [expected.month, expected.day]
    if not valid:
        raise RuntimeError("schedule_value_mismatch")


async def _require_time_value(locator, expected: datetime) -> None:
    if not await locator.count():
        raise RuntimeError("schedule_control_unavailable")
    parts = _input_parts((await locator.input_value()).strip())
    if not parts or parts[0] != expected.hour or len(parts) > 1 and parts[1] != expected.minute:
        raise RuntimeError("schedule_value_mismatch")


async def _require_selected_schedule_control(locator) -> None:
    """Require selected state on the exact schedule radio used by upstream."""

    if not await locator.count():
        raise RuntimeError("schedule_control_unavailable")
    selected = await locator.evaluate(
        """node => {
            return node.matches(
                'input:checked, label:has(input:checked), '
                + '[role="radio"][aria-checked="true"], '
                + '[role="radio"]:has(input:checked), '
                + '[data-state="checked"]'
            ) || node.getAttribute('aria-checked') === 'true'
                || node.querySelector?.('input[type="radio"]:checked') !== null;
        }"""
    )
    if selected is not True:
        raise RuntimeError("schedule_mode_mismatch")


async def _validate_browser_schedule(platform: str, page, publish_date) -> None:
    if platform == "douyin":
        mode_control = page.locator(
            "[class^='radio']:has-text('定时发布')"
        ).first
        await _require_selected_schedule_control(mode_control)
        field = page.locator('.semi-input[placeholder="日期和时间"]').first
        await _require_datetime_value(field, publish_date, combined=True)
        return
    mode_control = page.locator("label").filter(has_text="定时").nth(1)
    await _require_selected_schedule_control(mode_control)
    date_field = page.locator('input[placeholder="请选择发表时间"]').first
    time_field = page.locator('input[placeholder="请选择时间"]').first
    await _require_datetime_value(date_field, publish_date, combined=False)
    await _require_time_value(time_field, publish_date)


async def _require_douyin_declaration(page, declaration: str) -> None:
    visible_dialog = page.locator(
        ".semi-modal-content:visible, .semi-modal-body:visible"
    ).filter(has_text="请选择声明类型")
    if await visible_dialog.count():
        raise RuntimeError("douyin_declaration_not_applied")
    selected = page.locator(
        'label.semi-radio:has(input:checked), '
        'label.semi-radio[aria-checked="true"], '
        'label.semi-radio[data-state="checked"]'
    ).filter(has_text=declaration)
    if not await selected.count():
        raise RuntimeError("douyin_declaration_not_applied")


async def _require_tencent_content_label(page, content_label: str) -> None:
    option = page.get_by_text(content_label, exact=True).first
    if not await option.count():
        raise RuntimeError("tencent_content_label_mismatch")
    selected = await option.evaluate("""node => {
        let current=node;
        for(let depth=0;current&&depth<4;depth+=1,current=current.parentElement){
            if(current.matches?.('.option-list-wrap'))break;
            if(current.getAttribute?.('aria-selected')==='true'||current.getAttribute?.('aria-checked')==='true')return true;
            if(current.getAttribute?.('data-state')==='checked')return true;
            if(current.matches?.('label:has(input:checked),[role="option"]:has(input:checked),[role="radio"]:has(input:checked)'))return true;
        }
        return false;
    }""")
    if selected is not True:
        raise RuntimeError("tencent_content_label_mismatch")


def _normalized_editor_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u200b", "").replace("\ufeff", "")).strip()


async def _require_browser_content(platform: str, page, request: dict) -> None:
    title = request["title"]
    description = request["description"]
    tags = request["tags"]
    if platform == "douyin":
        title_field = page.locator('input[placeholder*="填写作品标题"]').first
        editor = page.locator('div.zone-container[contenteditable="true"]').first
        code = "douyin_content_mismatch"
        expected_editor = " ".join(
            part for part in (description, *("#" + tag for tag in tags)) if part
        )
    else:
        title_field = None
        editor = page.locator("div.input-editor").first
        code = "tencent_content_mismatch"
        expected_editor = " ".join(
            part for part in (title, *("#" + tag for tag in tags), description) if part
        )
    if title_field is not None:
        if not await title_field.count() or (await title_field.input_value()).strip() != title:
            raise RuntimeError(code)
    if not await editor.count():
        raise RuntimeError(code)
    try:
        visible = await editor.is_visible()
        actual = await editor.inner_text()
    except Exception:
        raise RuntimeError(code) from None
    if not visible or _normalized_editor_text(actual) != _normalized_editor_text(expected_editor):
        raise RuntimeError(code)


def _install_strict_content(platform: str, uploader, request: dict,
                            guard: SingleSubmission | None) -> None:
    if not {"title", "description", "tags"}.issubset(request):
        return
    if platform == "douyin":
        original = uploader.fill_title_and_description

        async def strict_content(self, page, title, description, tags=None):
            await original(page, title, description, tags)

            async def validate():
                await _require_browser_content(platform, page, request)

            await validate()
            if guard is not None:
                guard.register_final_preflight("content", validate)

        uploader.fill_title_and_description = types.MethodType(strict_content, uploader)
        return

    original = uploader.fill_description

    async def strict_description(self, page):
        await original(page)

        async def validate():
            await _require_browser_content(platform, page, request)

        await validate()
        if guard is not None:
            guard.register_final_preflight("content", validate)

    uploader.fill_description = types.MethodType(strict_description, uploader)


def _install_strict_schedule(platform: str, uploader,
                             guard: SingleSubmission | None = None) -> None:
    if getattr(uploader, "publish_strategy", "immediate") != "scheduled":
        return
    method_name = "set_schedule_time_douyin" if platform == "douyin" else "set_schedule_time_tencent"
    original = getattr(uploader, method_name)

    async def strict(self, page, publish_date):
        if int(publish_date.timestamp()) <= int(time.time()) + _SCHEDULE_LEAD_SECONDS[platform]:
            raise ScheduleWindowElapsed("schedule_window_elapsed")
        await original(page, publish_date)
        if int(publish_date.timestamp()) <= int(time.time()) + _SCHEDULE_LEAD_SECONDS[platform]:
            raise ScheduleWindowElapsed("schedule_window_elapsed")
        await _validate_browser_schedule(platform, page, publish_date)
        if guard is not None:
            async def validate():
                await _validate_browser_schedule(platform, page, publish_date)
            guard.register_final_preflight("schedule", validate)

    setattr(uploader, method_name, types.MethodType(strict, uploader))


def _install_strict_douyin_cover(uploader, guard: SingleSubmission | None = None) -> None:
    if not (uploader.thumbnail_landscape_path or uploader.thumbnail_portrait_path):
        return
    original = uploader.set_thumbnail

    async def reject_recommended_cover(self, page):
        warning = page.get_by_text("请设置封面后再发布").first
        if await warning.count() and await warning.is_visible():
            raise RuntimeError("douyin_explicit_cover_rejected")
        return False

    uploader.handle_auto_video_cover = types.MethodType(reject_recommended_cover, uploader)

    async def strict(self, page):
        expected_path = os.path.normcase(os.path.abspath(os.fspath(
            self.thumbnail_portrait_path or self.thumbnail_landscape_path
        )))
        locator_type = type(page.locator("html"))
        set_input_files = getattr(locator_type, "set_input_files", None)
        cover_upload_seen = False

        async def tracked_set_input_files(locator, files, *args, **kwargs):
            nonlocal cover_upload_seen
            candidates = files if isinstance(files, (list, tuple)) else [files]
            for candidate in candidates:
                if isinstance(candidate, (str, os.PathLike)) and os.path.normcase(
                    os.path.abspath(os.fspath(candidate))
                ) == expected_path:
                    cover_upload_seen = True
            return await set_input_files(locator, files, *args, **kwargs)

        preview = page.locator('[class*="cover-"]').filter(has=page.locator("img")).first
        if not await preview.count():
            preview = page.locator('[class*="cover"]').first
        image = preview.locator("img").first
        before = await image.get_attribute("src") if await image.count() else None
        if set_input_files is not None:
            locator_type.set_input_files = tracked_set_input_files
        try:
            await original(page)
        finally:
            if set_input_files is not None:
                locator_type.set_input_files = set_input_files
        await page.wait_for_timeout(1000)
        dialog = page.locator("div.dy-creator-content-modal").first
        if await dialog.count() and await dialog.is_visible():
            raise RuntimeError("douyin_cover_not_applied")
        after = await image.get_attribute("src") if await image.count() else None
        if not cover_upload_seen or not before or not after or before == after:
            raise RuntimeError("douyin_cover_not_applied")

        async def validate():
            warning = page.get_by_text("请设置封面后再发布").first
            if await warning.count() and await warning.is_visible():
                raise RuntimeError("douyin_explicit_cover_rejected")
            current_dialog = page.locator("div.dy-creator-content-modal").first
            if await current_dialog.count() and await current_dialog.is_visible():
                raise RuntimeError("douyin_cover_not_applied")
            current = await image.get_attribute("src") if await image.count() else None
            if current != after:
                raise RuntimeError("douyin_cover_not_applied")

        if guard is not None:
            guard.register_final_preflight("douyin-cover", validate)

    uploader.set_thumbnail = types.MethodType(strict, uploader)


def _install_strict_tencent_parameters(uploader, request: dict,
                                       guard: SingleSubmission | None = None) -> None:
    if uploader.thumbnail_landscape_path or uploader.thumbnail_portrait_path:
        async def strict_single(self, page, thumbnail_path, selectors, dialog_titles, label):
            async def find_preview():
                for selector in selectors:
                    candidate = page.locator(selector).first
                    if await candidate.count() and await candidate.is_visible():
                        return candidate
                raise RuntimeError("tencent_cover_control_unavailable")

            async def fingerprint(preview):
                return await preview.evaluate("""node => JSON.stringify({
                    images:[...node.querySelectorAll('img')].map(image=>image.currentSrc||image.getAttribute('src')||''),
                    backgrounds:[node,...node.querySelectorAll('*')].map(element=>getComputedStyle(element).backgroundImage).filter(value=>value&&value!=='none')
                })""")

            preview = await find_preview()
            before = await fingerprint(preview)
            if not before:
                raise RuntimeError("tencent_cover_control_unavailable")
            dialog = await self.open_thumbnail_dialog(page, selectors, dialog_titles)
            if not dialog:
                raise RuntimeError("tencent_cover_control_unavailable")
            await self.upload_thumbnail_in_dialog(page, dialog, thumbnail_path)
            await self.confirm_thumbnail_crop(page)
            crop_dialog = page.locator(
                "div.weui-desktop-dialog"
            ).filter(has_text="裁剪封面图").first
            if await crop_dialog.count():
                try:
                    await crop_dialog.wait_for(state="hidden", timeout=10000)
                except Exception:
                    pass
                if await crop_dialog.count() and await crop_dialog.is_visible():
                    raise RuntimeError("tencent_cover_not_applied")
            try:
                await dialog.wait_for(state="hidden", timeout=10000)
            except Exception:
                pass
            if await dialog.count() and await dialog.is_visible():
                raise RuntimeError("tencent_cover_not_applied")
            await page.wait_for_timeout(1000)
            after = await fingerprint(preview)
            if not after or after == before:
                raise RuntimeError("tencent_cover_not_applied")

            async def validate():
                current_preview = await find_preview()
                current = await fingerprint(current_preview)
                if not current or current != after:
                    raise RuntimeError("tencent_cover_not_applied")
                current_crop = page.locator(
                    "div.weui-desktop-dialog"
                ).filter(has_text="裁剪封面图").first
                if await current_crop.count() and await current_crop.is_visible():
                    raise RuntimeError("tencent_cover_not_applied")
                if await dialog.count() and await dialog.is_visible():
                    raise RuntimeError("tencent_cover_not_applied")

            if guard is not None:
                guard.register_final_preflight("tencent-cover-" + str(label), validate)

        uploader.set_single_thumbnail = types.MethodType(strict_single, uploader)

    short_title = request.get("short_title")
    if short_title is None:
        uploader.set_short_title = types.MethodType(_no_short_title, uploader)
    else:
        async def short_title_field(page):
            field = page.locator(
                'input[placeholder="填写短标题有机会获得更多流量"]'
            ).first
            if not await field.count():
                field = (
                    page.get_by_text("短标题", exact=True).locator("..").locator(
                        "xpath=following-sibling::div"
                    ).locator('span input[type="text"]').first
                )
            if not await field.count():
                raise RuntimeError("tencent_short_title_unavailable")
            return field

        async def strict_short(self, page, _title, _short_title=None):
            field = await short_title_field(page)
            await field.fill(short_title)

            async def validate():
                current = await short_title_field(page)
                if (await current.input_value()).strip() != short_title:
                    raise RuntimeError("tencent_short_title_mismatch")

            await validate()
            if guard is not None:
                guard.register_final_preflight("tencent-short-title", validate)

        uploader.set_short_title = types.MethodType(strict_short, uploader)


def install_platform_parameter_policy(platform: str, uploader, request: dict,
                                      guard: SingleSubmission | None = None) -> None:
    """Turn every explicitly confirmed browser parameter into fail-closed behavior."""
    _install_strict_content(platform, uploader, request, guard)
    _install_strict_schedule(platform, uploader, guard)
    if platform == "douyin":
        _install_strict_douyin_cover(uploader, guard)
    elif platform == "tencent":
        _install_strict_tencent_parameters(uploader, request, guard)
    else:
        raise ValueError("invalid_platform")


async def _apply_tencent_content_label(uploader, page) -> None:
    """Apply the one pinned, evidenced label and propagate every failure."""
    label = uploader.content_label
    entry = page.get_by_text("选择视频标注", exact=True).first
    if not await entry.count():
        raise RuntimeError("tencent_content_label_unavailable")
    await entry.click()
    await page.wait_for_timeout(800)
    option = page.get_by_text(label, exact=True).first
    await option.wait_for(state="visible", timeout=5000)
    await option.click()
    await page.wait_for_timeout(500)
    await _require_tencent_content_label(page, label)


def install_statement_policy(platform: str, uploader, request: dict,
                             guard: SingleSubmission | None = None) -> None:
    """Prevent the pinned adapters from silently choosing an AI declaration."""
    if platform == "douyin":
        declaration = request.get("declaration")
        if declaration is None:
            # The pinned upload method assigns an AI declaration whenever this
            # attribute is falsey. A private truthy sentinel plus a no-op method
            # prevents both that assignment and any platform interaction.
            uploader.declaration = _NO_DOUYIN_DECLARATION
            uploader.apply_self_declaration = types.MethodType(_no_statement, uploader)
        elif declaration not in _DOUYIN_DECLARATIONS:
            raise ValueError("invalid_platform_metadata")
        else:
            original = uploader.apply_self_declaration

            async def strict_declaration(self, page):
                # The pinned adapter raises when its boolean
                # set_self_declaration result is false, then returns None on
                # success. Preserve that exact contract while still rejecting
                # an explicit false result from a future compatible wrapper.
                if await original(page) is False:
                    raise RuntimeError("douyin_declaration_not_applied")

                async def validate():
                    await _require_douyin_declaration(page, declaration)

                await validate()
                if guard is not None:
                    guard.register_final_preflight("douyin-declaration", validate)

            uploader.apply_self_declaration = types.MethodType(
                strict_declaration, uploader
            )
        return
    if platform == "tencent":
        content_label = request.get("content_label")
        if content_label is None:
            uploader.apply_original_statement = types.MethodType(_no_statement, uploader)
        elif content_label in _TENCENT_CONTENT_LABELS:
            uploader.content_label = content_label

            async def strict_content_label(self, page):
                await _apply_tencent_content_label(self, page)

                async def validate():
                    await _require_tencent_content_label(page, content_label)

                if guard is not None:
                    guard.register_final_preflight("tencent-content-label", validate)

            uploader.apply_original_statement = types.MethodType(strict_content_label, uploader)
        else:
            raise ValueError("invalid_platform_metadata")
        return
    raise ValueError("invalid_platform")


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
    guard = SingleSubmission(platform, request.get("publish_at_unix"))
    guard.install()
    options = browser_upload_options(platform, request, account)
    if platform == "douyin":
        async def no_shared_verification_file(_file):
            # An SMS challenge is resolved manually in the visible browser.
            # Never read a leftover verification code from another operation.
            return ""
        module._read_verify_code = no_shared_verification_file
        uploader = module.DouYinVideo(**options)
        install_statement_policy(platform, uploader, request, guard)
        install_platform_parameter_policy(platform, uploader, request, guard)
        await uploader.douyin_upload_video()
    else:
        uploader = module.TencentVideo(**options)
        install_statement_policy(platform, uploader, request, guard)
        install_platform_parameter_policy(platform, uploader, request, guard)
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
    except ScheduleWindowElapsed:
        result = ("failed", "schedule_window_elapsed")
    except PlatformParameterMismatch:
        result = ("failed", "platform_parameter_mismatch")
    except (Exception, SubmissionUncertain):
        pass
    temp = operation / "result.tmp"
    temp.write_text(json.dumps({"status": result[0], "code": result[1]}), encoding="utf-8")
    temp.replace(operation / "result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bilibili TV QR login, displayed locally for the user to scan and confirm.

Protocol and account structure were checked against biliup v1.2.4, commit
03b7a84f55a31f407570f7d19ef5581101434bf3, crates/biliup/src/uploader/credential.rs
(get_qrcode, login_by_qrcode, LoginInfo, TokenInfo, renew_tokens).
The embedded TV application identifier/signing value below are public upstream
protocol constants, not an Open-Flame account or user credential. This module
never calls any QR scan/confirmation endpoint and never converts web Cookies
into a silently confirmed second login.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable


_PASSPORT = "https://passport.bilibili.com"
_TV_APP_KEY = "4409e2ce8ffd12b8"
_TV_APP_SIGNING_VALUE = "59b43e04ad6965f34319062b478f83dd"
_MAX_RESPONSE_BYTES = 128 * 1024
_LOGIN_SECONDS = 180
_POLL_SECONDS = 2.0
_AUTH_CODE = re.compile(r"^[0-9a-fA-F]{32}$")
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_REQUIRED_COOKIES = frozenset({"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"})
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class _LoginFailure(ValueError):
    """Private control-flow exception; messages never include network content."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise _LoginFailure("login_redirect_rejected")


def _signed_form(extra: dict[str, str] | None = None) -> bytes:
    # serde_json's sorted map plus serde_urlencoded in the pinned Rust source.
    form = {"appkey": _TV_APP_KEY, "local_id": "0", "ts": str(int(time.time()))}
    if extra:
        form.update(extra)
    encoded = urllib.parse.urlencode(sorted(form.items()))
    form["sign"] = hashlib.md5((encoded + _TV_APP_SIGNING_VALUE).encode("ascii")).hexdigest()
    return urllib.parse.urlencode(sorted(form.items())).encode("ascii")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise _LoginFailure("invalid_login_response")
        result[name] = value
    return result


def _request_json(opener, action: str, auth_code: str | None = None) -> dict:
    if action not in {"auth_code", "poll"}:
        raise _LoginFailure("invalid_login_action")
    extra = {"auth_code": auth_code} if auth_code is not None else None
    request = urllib.request.Request(
        _PASSPORT + "/x/passport-tv-login/qrcode/" + action,
        data=_signed_form(extra),
        headers={"User-Agent": _USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise _LoginFailure("login_http_failed")
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise _LoginFailure("login_response_too_large")
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise _LoginFailure("invalid_login_response")
    return payload


def _safe_text(value, maximum: int, *, empty: bool = False) -> bool:
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and all(ord(character) >= 32 for character in value))


def _qr_request(payload: dict) -> tuple[str, str]:
    data = payload.get("data")
    if type(payload.get("code")) is not int or payload["code"] != 0 or not isinstance(data, dict):
        raise _LoginFailure("login_qr_unavailable")
    auth_code, url = data.get("auth_code"), data.get("url")
    if not isinstance(auth_code, str) or not _AUTH_CODE.fullmatch(auth_code) or not _safe_text(url, 2048):
        raise _LoginFailure("invalid_login_qr")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "passport.bilibili.com"
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 443} or parsed.fragment
            or parsed.path != "/x/passport-tv-login/h5/qrcode/auth"):
        raise _LoginFailure("invalid_login_qr")
    parameters = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    if parameters.get("auth_code") != [auth_code]:
        raise _LoginFailure("invalid_login_qr")
    return auth_code, url


def _qr_png(url: str) -> bytes:
    # Segno 1.6.6 is already part of the 17-package isolated runtime. Its PNG
    # writer is self-contained, so Pillow/pypng and new dependencies are absent.
    import segno
    code = segno.make(url, error="m", micro=False)
    width, height = code.symbol_size(scale=6, border=4)
    if not 80 <= width <= 1024 or not 80 <= height <= 1024:
        raise _LoginFailure("invalid_login_qr")
    output = io.BytesIO()
    code.save(output, kind="png", scale=6, border=4)
    png = output.getvalue()
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 512 * 1024:
        raise _LoginFailure("invalid_login_qr")
    return png


def _login_info(payload: dict) -> dict:
    """Require a real successful TV login, never invent missing app tokens."""
    data = payload.get("data")
    if type(payload.get("code")) is not int or payload["code"] != 0 or not isinstance(data, dict):
        raise _LoginFailure("invalid_login_result")
    cookie_info, token, sso = data.get("cookie_info"), data.get("token_info"), data.get("sso")
    if not isinstance(cookie_info, dict) or not isinstance(token, dict):
        raise _LoginFailure("invalid_login_result")
    raw_cookies = cookie_info.get("cookies")
    if not isinstance(raw_cookies, list) or not 4 <= len(raw_cookies) <= 32:
        raise _LoginFailure("invalid_login_result")
    cookies = {}
    for entry in raw_cookies:
        if not isinstance(entry, dict):
            raise _LoginFailure("invalid_login_result")
        name, value = entry.get("name"), entry.get("value")
        if (not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name)
                or name in cookies or not _safe_text(value, 8192)):
            raise _LoginFailure("invalid_login_result")
        cookies[name] = value
    if not _REQUIRED_COOKIES.issubset(cookies):
        raise _LoginFailure("invalid_login_result")
    mid, expires_in = token.get("mid"), token.get("expires_in")
    if (type(mid) is not int or not 0 < mid < 2**64
            or type(expires_in) is not int or not 0 < expires_in < 2**32
            or cookies["DedeUserID"] != str(mid)
            or not _safe_text(token.get("access_token"), 4096)
            or not _safe_text(token.get("refresh_token"), 4096)
            or not isinstance(sso, list) or len(sso) > 32
            or not all(_safe_text(item, 2048) for item in sso)):
        raise _LoginFailure("invalid_login_result")
    return {
        "cookie_info": {"cookies": [{"name": name, "value": value} for name, value in cookies.items()]},
        "sso": sso,
        "token_info": {name: token[name] for name in ("access_token", "expires_in", "mid", "refresh_token")},
        "platform": "BiliTV",
    }


def _validate_account_path(account: Path) -> None:
    if not account.is_absolute() or account != Path(os.path.abspath(account)):
        raise _LoginFailure("invalid_account_path")
    for path in reversed(account.parents):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise _LoginFailure("invalid_account_path")
    if account.exists() or account.is_symlink():
        info = account.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink != 1):
            raise _LoginFailure("invalid_account_path")


def _save_account(account: Path, login_info: dict) -> None:
    _validate_account_path(account)
    descriptor, name = tempfile.mkstemp(prefix=".login-", suffix=".json", dir=account.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(login_info, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        _validate_account_path(account)
        # Same-directory replace preserves the old account on failure. No
        # delete/truncate happens before this single replacement operation.
        os.replace(temporary, account)
    finally:
        temporary.unlink(missing_ok=True)


def login_bilibili(
    data: dict,
    emit: Callable[..., None],
) -> tuple[str, str]:
    """Run one bounded QR session; all emitted data is a display-only snapshot.

    An unscanned QR is not refreshed or confirmed automatically. Cancellation
    is owned by the bridge's process tree. Unknown statuses fail closed.
    """
    try:
        account = Path(data["account_file"])
        _validate_account_path(account)
        emit("preparing")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
        auth_code, url = _qr_request(_request_json(opener, "auth_code"))
        png = _qr_png(url)
        deadline = time.monotonic() + _LOGIN_SECONDS
        expires_at = int(time.time()) + _LOGIN_SECONDS
        emit("waiting_scan", qr_png=png, expires_at=expires_at)
        previous = "waiting_scan"
        while time.monotonic() < deadline:
            result = _request_json(opener, "poll", auth_code)
            code = result["code"]
            if code == 0:
                login_info = _login_info(result)
                _save_account(account, login_info)
                return "ready", "account_ready"
            if code == 86038:
                emit("expired")
                return "failed", "login_qr_expired"
            if code == 86090:
                if previous != "scanned":
                    emit("scanned")
                    previous = "scanned"
            elif code == 86039:
                if previous != "waiting_scan":
                    # A regression must not resurrect a code after a scan.
                    raise _LoginFailure("invalid_login_progress")
            else:
                raise _LoginFailure("login_failed")
            time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        emit("expired")
        return "failed", "login_qr_expired"
    except Exception:
        # Never publish upstream response bodies, auth_code, tokens, cookies,
        # QR URLs, raw exceptions, or account/operation paths.
        return "failed", "login_failed"

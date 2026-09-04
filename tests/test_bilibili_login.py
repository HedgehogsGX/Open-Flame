from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from video_download_control.uploads import bilibili_login as login


AUTH = "a" * 32
QR_URL = "https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/auth?auth_code=" + AUTH
PNG = b"\x89PNG\r\n\x1a\nsynthetic-only"


def _success():
    # Synthetic markers only, never a captured login response or real token.
    return {"code": 0, "data": {
        "cookie_info": {"cookies": [
            {"name": "SESSDATA", "value": "synthetic-session"},
            {"name": "bili_jct", "value": "synthetic-csrf"},
            {"name": "DedeUserID", "value": "12345"},
            {"name": "DedeUserID__ckMd5", "value": "synthetic-user-check"},
        ]},
        "token_info": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "mid": 12345, "expires_in": 15552000},
        "sso": ["https://www.bilibili.com"],
    }}


@pytest.fixture
def login_case(tmp_path, monkeypatch):
    account = tmp_path / "account.json"
    account.write_bytes(b"unchanged-old-account")
    operation = tmp_path / "operation"
    operation.mkdir()
    events, requests = [], []
    queue = [{"code": 0, "data": {"auth_code": AUTH, "url": QR_URL}}]

    def request(opener, action, auth_code=None):
        requests.append((action, auth_code))
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(login, "_request_json", request)
    monkeypatch.setattr(login, "_qr_png", lambda url: PNG)
    monkeypatch.setattr(login.time, "sleep", lambda _: None)
    return SimpleNamespace(account=account, operation=operation, events=events, requests=requests, queue=queue,
                           run=lambda: login.login_bilibili({"account_file": str(account), "operation_dir": str(operation)}, lambda phase, **fields: events.append((phase, fields))))


def test_tv_login_saves_real_token_shape_only_after_explicit_success(login_case):
    case = login_case
    case.queue.extend([{"code": 86039, "data": None}, {"code": 86090, "data": None}, _success()])
    assert case.run() == ("ready", "account_ready")
    saved = json.loads(case.account.read_text())
    assert saved["platform"] == "BiliTV"
    assert saved["token_info"] == _success()["data"]["token_info"]
    assert {entry["name"] for entry in saved["cookie_info"]["cookies"]} == login._REQUIRED_COOKIES
    assert [phase for phase, fields in case.events] == ["preparing", "waiting_scan", "scanned"]
    assert case.events[1][1]["qr_png"] == PNG
    assert type(case.events[1][1]["expires_at"]) is int
    assert case.events[-1][1] == {}
    assert all(action in {"auth_code", "poll"} for action, _ in case.requests)
    assert list(case.operation.iterdir()) == []
    assert list(case.account.parent.glob(".login-*")) == []


@pytest.mark.parametrize("mutate", [
    lambda result: result.update(code=86039),
    lambda result: result.update(code=False),
    lambda result: result["data"].pop("token_info"),
    lambda result: result["data"]["token_info"].update(access_token=""),
    lambda result: result["data"]["token_info"].update(refresh_token=""),
    lambda result: result["data"]["token_info"].update(mid=54321),
    lambda result: result["data"]["token_info"].update(mid=True),
    lambda result: result["data"]["token_info"].update(expires_in=0),
    lambda result: result["data"]["cookie_info"]["cookies"].pop(),
    lambda result: result["data"]["cookie_info"]["cookies"].append({"name": "SESSDATA", "value": "duplicate"}),
    lambda result: result["data"]["cookie_info"]["cookies"][0].update(value="bad\nvalue"),
    lambda result: result["data"].update(sso="not-a-list"),
])
def test_missing_or_inconsistent_credentials_are_not_compatible_accounts(mutate):
    result = _success()
    mutate(result)
    with pytest.raises(login._LoginFailure):
        login._login_info(result)


@pytest.mark.parametrize("result", [{"code": 86038, "data": None}, {"code": -400, "message": "synthetic-private-response"}, {"code": 0, "data": {"token_info": {}}}, RuntimeError("synthetic-secret-exception")])
def test_failed_expired_or_malformed_login_preserves_previous_account(login_case, result, capsys):
    case = login_case
    case.queue.append(result)
    outcome = case.run()
    assert outcome[0] == "failed"
    assert case.account.read_bytes() == b"unchanged-old-account"
    assert list(case.account.parent.glob(".login-*")) == []
    assert list(case.operation.iterdir()) == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert "synthetic-private-response" not in repr(case.events)
    if isinstance(result, dict) and result["code"] == 86038:
        assert case.events[-1] == ("expired", {})
        assert outcome == ("failed", "login_qr_expired")


def test_atomic_replace_failure_never_truncates_old_account(login_case, monkeypatch):
    case = login_case
    case.queue.append(_success())
    monkeypatch.setattr(login.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("synthetic replacement failure")))
    assert case.run() == ("failed", "login_failed")
    assert case.account.read_bytes() == b"unchanged-old-account"
    assert list(case.account.parent.glob(".login-*")) == []


def test_fsync_failure_never_replaces_old_account(login_case, monkeypatch):
    case = login_case
    case.queue.append(_success())
    monkeypatch.setattr(login.os, "fsync", lambda *args: (_ for _ in ()).throw(OSError("synthetic fsync failure")))
    assert case.run() == ("failed", "login_failed")
    assert case.account.read_bytes() == b"unchanged-old-account"
    assert list(case.account.parent.glob(".login-*")) == []


def test_client_deadline_expires_without_refresh_or_confirmation(login_case, monkeypatch):
    case = login_case
    monkeypatch.setattr(login, "_LOGIN_SECONDS", 0)
    assert case.run() == ("failed", "login_qr_expired")
    assert case.requests == [("auth_code", None)]
    assert case.events[-1] == ("expired", {})
    assert case.account.read_bytes() == b"unchanged-old-account"


@pytest.mark.parametrize("url", [
    "https://evil.example/?auth_code=" + AUTH,
    "http://passport.bilibili.com/?auth_code=" + AUTH,
    "https://passport.bilibili.com@evil.example/?auth_code=" + AUTH,
    "https://passport.bilibili.com/?auth_code=" + "b" * 32,
    QR_URL + "&auth_code=" + AUTH,
])
def test_qr_source_and_session_are_bound_before_display(login_case, url):
    case = login_case
    case.queue[0]["data"]["url"] = url
    assert case.run() == ("failed", "login_failed")
    assert case.events == [("preparing", {})]
    assert case.account.read_bytes() == b"unchanged-old-account"


def test_request_uses_pinned_tv_signing_contract_and_fixed_post_endpoints(monkeypatch):
    monkeypatch.setattr(login.time, "time", lambda: 1700000000)
    calls = []

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, maximum):
            assert maximum == login._MAX_RESPONSE_BYTES + 1
            return b'{"code":86039,"data":null}'

    class Opener:
        def open(self, request, *, timeout):
            calls.append(request)
            assert timeout == 10
            return Response()

    assert login._request_json(Opener(), "poll", AUTH)["code"] == 86039
    request = calls[0]
    assert request.method == "POST"
    assert request.full_url == "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
    assert AUTH not in request.full_url
    form = parse_qs(request.data.decode())
    expected = "appkey=4409e2ce8ffd12b8&auth_code=" + AUTH + "&local_id=0&ts=1700000000"
    assert form["sign"] == [hashlib.md5((expected + login._TV_APP_SIGNING_VALUE).encode()).hexdigest()]


def test_redirects_are_not_followed():
    with pytest.raises(login._LoginFailure):
        login._NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://evil.example")


def test_existing_link_account_is_rejected_before_any_network(login_case):
    case = login_case
    alternate = case.account.parent / "linked.json"
    import os
    os.link(case.account, alternate)
    assert case.run() == ("failed", "login_failed")
    assert case.requests == []
    assert case.account.read_bytes() == alternate.read_bytes() == b"unchanged-old-account"

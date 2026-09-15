"""Synthetic inline-login boundaries; no platform, account or browser is used."""
from __future__ import annotations

import base64
import json
import os
import queue
import sqlite3
import struct
import sys
import threading
import time
import zlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.uploads import backend as backend_module
from video_download_control.uploads import service as service_module
from video_download_control.uploads.backend import SauBackend
from video_download_control.uploads.contracts import BackendResult, UploadError
from video_download_control.uploads.login_progress import MAX_QR_BYTES
from video_download_control.uploads.service import UploadService


def png_image(shade=0, width=80, height=80):
    """Encode a complete grayscale PNG, including pixel data and chunk CRCs."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = (b"\x00" + bytes([shade]) * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


@dataclass
class LoginCall:
    platform: str
    account_id: str
    stop: threading.Event
    on_update: object
    release: threading.Event = field(default_factory=threading.Event)
    result: BackendResult = field(default_factory=lambda: BackendResult("ready", "account_ready"))

    def emit(self, phase="waiting_scan", png=None, expires_at=None):
        if phase == "waiting_scan" and png is None:
            png = png_image()
        self.on_update(phase, png, expires_at)


class ControlledBackend:
    def __init__(self):
        self.pending = queue.Queue()
        self.closed = threading.Event()
        self.calls = []
        self.checks = []

    def inspect(self):
        return {"ready": True, "code": "synthetic_ready"}

    def login(self, *args):
        raise AssertionError("inline login must not fall back to a terminal")

    def login_interactive(self, platform, account_id, stop, on_update):
        call = LoginCall(platform, account_id, stop, on_update)
        self.calls.append(call)
        self.pending.put(call)
        # Intentionally allow a late success after cancellation to exercise the
        # service's final commit guard, independently of a cooperative backend.
        while not call.release.wait(0.01):
            if self.closed.is_set():
                return BackendResult("canceled", "canceled")
        return call.result

    def check(self, platform, account_id, stop):
        self.checks.append((platform, account_id))
        return BackendResult("ready", "account_ready")

    def next_call(self):
        return self.pending.get(timeout=3)


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("synthetic login did not reach its expected state")


def operation(service, operation_id):
    return next(row for row in service.operations() if row["id"] == operation_id)


def account(service, account_id):
    return next(row for row in service.accounts() if row["id"] == account_id)


def begin_login(service, platform="bilibili", name="Synthetic login"):
    row = service.add_account(platform, name)
    op = service.account_action(row["id"], "login")
    call = service.backend.next_call()
    assert (call.platform, call.account_id) == (platform, row["id"])
    return row, op, call


def no_qr(service, operation_id):
    with pytest.raises(UploadError, match="^login_qr_unavailable$"):
        service.login_qr(operation_id)
    assert operation(service, operation_id)["qr_available"] is False


@pytest.fixture
def login_service(tmp_path):
    backend = ControlledBackend()
    service = UploadService(tmp_path / "uploads", backend)
    service.start()
    try:
        yield service
    finally:
        backend.closed.set()
        service.stop()


@pytest.fixture
def login_client(settings):
    backend = ControlledBackend()
    app = create_app(settings)
    app.state.upload_service_factory = lambda root: UploadService(root, backend)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/v1/uploads/session").json()["csrf_token"]
        client.headers["X-Upload-CSRF"] = token
        try:
            yield client, app, backend
        finally:
            backend.closed.set()


@pytest.mark.parametrize("platform", ["bilibili", "douyin", "tencent"])
def test_inline_qr_scan_does_not_make_account_ready(login_service, platform):
    service = login_service
    row, op, call = begin_login(service, platform)
    assert operation(service, op["id"])["login_phase"] == "preparing"
    expiry = int(time.time()) + 90
    call.emit(expires_at=expiry)
    current = operation(service, op["id"])
    assert current["login_phase"] == "waiting_scan"
    assert current["qr_available"] is True and current["qr_revision"] > 0
    assert current["expires_at"] == expiry
    assert service.login_qr(op["id"]) == png_image()
    assert account(service, row["id"])["auth_state"] == "checking"
    call.emit("scanned")
    no_qr(service, op["id"])
    assert account(service, row["id"])["auth_state"] == "checking"
    call.release.set()
    wait_for(lambda: operation(service, op["id"])["state"] == "ready")
    assert account(service, row["id"])["auth_state"] == "ready"
    call.emit(expires_at=expiry)
    no_qr(service, op["id"])


def test_second_instance_cannot_enqueue_inline_login_or_read_owner_qr(login_service):
    service = login_service
    row, op, call = begin_login(service)
    call.emit(expires_at=int(time.time()) + 90)
    reader = UploadService(service.root, ControlledBackend())
    reader.start()
    try:
        assert reader.status()["worker_running"] is False
        before = reader.operations()
        with pytest.raises(UploadError, match="^login_owned_by_other_instance$"):
            reader.account_action(row["id"], "login")
        assert reader.operations() == before
        no_qr(reader, op["id"])
        assert service.login_qr(op["id"]) == png_image()
    finally:
        reader.stop()


def test_second_instance_cancel_hides_qr_immediately_and_discards_late_ready(login_service):
    service = login_service
    row, op, call = begin_login(service)
    expiry = int(time.time()) + 90
    call.emit(expires_at=expiry)
    reader = UploadService(service.root, ControlledBackend())
    reader.start()
    try:
        reader.cancel_operation(op["id"])
        no_qr(service, op["id"])
        call.emit(png=png_image(255), expires_at=expiry)
        no_qr(service, op["id"])
        wait_for(call.stop.is_set)
        call.release.set()
        wait_for(lambda: operation(service, op["id"])["state"] == "canceled")
        assert account(service, row["id"])["auth_state"] == "unchecked"
    finally:
        reader.stop()


def test_canceled_queued_account_does_not_stop_active_login(login_service):
    service = login_service
    first, active, call = begin_login(service, "douyin", "First")
    call.emit(expires_at=int(time.time()) + 90)
    second = service.add_account("douyin", "Second")
    queued = service.account_action(second["id"], "login")
    assert operation(service, queued["id"])["state"] == "queued"
    no_qr(service, queued["id"])
    service.cancel_operation(queued["id"])
    assert not call.stop.is_set()
    assert service.login_qr(active["id"]) == png_image()
    call.release.set()
    wait_for(lambda: operation(service, active["id"])["state"] == "ready")
    assert operation(service, queued["id"])["state"] == "canceled"
    assert account(service, second["id"])["auth_state"] == "unchecked"
    assert [item.account_id for item in service.backend.calls] == [first["id"]]


def test_old_callback_cannot_replace_new_login_or_cancel_its_identity(login_service):
    service = login_service
    row, old, old_call = begin_login(service)
    expiry = int(time.time()) + 90
    old_call.emit(expires_at=expiry)
    service.cancel_operation(old["id"])
    old_call.release.set()
    wait_for(lambda: operation(service, old["id"])["state"] == "canceled")
    new = service.account_action(row["id"], "login")
    new_call = service.backend.next_call()
    assert new["id"] != old["id"] and new_call.stop is not old_call.stop
    new_call.emit(png=png_image(255), expires_at=expiry)
    before = operation(service, new["id"])
    old_call.emit(expires_at=expiry)
    service.cancel_operation(old["id"])
    assert not new_call.stop.is_set()
    assert operation(service, new["id"]) == before
    assert service.login_qr(new["id"]) == png_image(255)
    no_qr(service, old["id"])
    assert account(service, row["id"])["auth_state"] == "checking"


def test_expiry_uses_monotonic_time_and_repeated_png_cannot_extend_it(login_service, monkeypatch):
    service = login_service
    _, op, call = begin_login(service)
    clock = {"wall": time.time(), "monotonic": time.monotonic()}
    monkeypatch.setattr(service_module, "time", SimpleNamespace(
        time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]))
    expiry = int(clock["wall"]) + 30
    call.emit(expires_at=expiry)
    revision = operation(service, op["id"])["qr_revision"]
    call.emit(expires_at=expiry)
    assert operation(service, op["id"])["qr_revision"] == revision
    clock["monotonic"] += 20
    clock["wall"] -= 3600
    call.emit(expires_at=expiry + 300)
    assert operation(service, op["id"])["expires_at"] == expiry
    assert service.login_qr(op["id"]) == png_image()
    clock["monotonic"] += 11
    no_qr(service, op["id"])
    assert operation(service, op["id"])["login_phase"] == "expired"
    call.emit(expires_at=expiry + 400)
    no_qr(service, op["id"])


def test_already_expired_qr_is_not_exposed(login_service):
    _, op, call = begin_login(login_service)
    call.emit(expires_at=int(time.time()) - 1)
    no_qr(login_service, op["id"])
    assert operation(login_service, op["id"])["login_phase"] == "expired"


@pytest.mark.parametrize("intermediate_phase", ["scanned", "verification_required"])
def test_phase_change_cannot_renew_the_same_expired_qr(login_service, monkeypatch, intermediate_phase):
    service = login_service
    _, op, call = begin_login(service)
    clock = {"wall": time.time(), "monotonic": time.monotonic()}
    monkeypatch.setattr(service_module, "time", SimpleNamespace(
        time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]))
    expiry = int(clock["wall"]) + 30
    call.emit(expires_at=expiry)
    call.emit(intermediate_phase)
    no_qr(service, op["id"])
    clock["monotonic"] += 31
    call.emit(expires_at=expiry + 300)
    no_qr(service, op["id"])
    # A genuinely new QR can still replace the expired one.
    call.emit(png=png_image(255), expires_at=expiry + 300)
    assert service.login_qr(op["id"]) == png_image(255)


@pytest.mark.parametrize("update", [
    ("waiting_scan", b"<svg onload='not-an-image'>", None),
    ("waiting_scan", b"\x89PNG\r\n\x1a\n" + b"x" * MAX_QR_BYTES, None),
    ("waiting_scan", png_image()[:-1] + b"x", None),
    ("waiting_scan", png_image(width=79), None),
    ("waiting_scan", png_image(height=1025), None),
    ("waiting_scan", bytearray(png_image()), None),
    ("waiting_scan", png_image(), float("nan")),
    ("waiting_scan", png_image(), float("inf")),
    ("waiting_scan", png_image(), True),
    ("waiting_scan", png_image(), 10**15),
    ("scanned", png_image(), None),
    ("ready", None, None),
])
def test_malformed_login_update_does_not_replace_current_qr(login_service, update):
    _, op, call = begin_login(login_service)
    call.emit(expires_at=int(time.time()) + 90)
    before = operation(login_service, op["id"])
    with pytest.raises(ValueError, match="^invalid_login_update$"):
        call.on_update(*update)
    assert operation(login_service, op["id"]) == before
    assert login_service.login_qr(op["id"]) == png_image()


def test_service_stop_restart_does_not_recover_a_qr_or_accept_old_callback(login_service):
    service = login_service
    row, op, call = begin_login(service)
    expiry = int(time.time()) + 90
    call.emit(expires_at=expiry)
    service.backend.closed.set()
    service.stop()
    no_qr(service, op["id"])
    replacement = UploadService(service.root, ControlledBackend())
    replacement.start()
    try:
        no_qr(replacement, op["id"])
        new = replacement.account_action(row["id"], "login")
        next_call = replacement.backend.next_call()
        next_call.emit(png=png_image(255), expires_at=expiry)
        call.emit(expires_at=expiry)
        no_qr(service, op["id"])
        assert replacement.login_qr(new["id"]) == png_image(255)
    finally:
        replacement.backend.closed.set()
        replacement.stop()


def test_account_check_never_starts_interactive_login(login_service):
    row = login_service.add_account("tencent", "Synthetic check")
    op = login_service.account_action(row["id"], "check")
    wait_for(lambda: operation(login_service, op["id"])["state"] == "ready")
    assert login_service.backend.calls == []
    assert login_service.backend.checks == [("tencent", row["id"])]
    no_qr(login_service, op["id"])


def test_api_qr_requires_header_nonce_and_same_origin_without_leaking_png(login_client, caplog):
    client, app, backend = login_client
    row = client.post("/api/v1/uploads/accounts", json={"platform": "bilibili", "name": "Synthetic"}).json()
    response = client.post(f"/api/v1/uploads/accounts/{row['id']}/login")
    assert response.status_code == 202
    op = response.json()
    call = backend.next_call()
    expiry = int(time.time()) + 90
    call.emit(expires_at=expiry)
    path = f"/api/v1/uploads/operations/{op['id']}/qr"
    token = client.headers.pop("X-Upload-CSRF")
    assert client.get(path).status_code == 403
    assert client.get(path, params={"csrf_token": token}).status_code == 403
    assert client.get(path, headers={"X-Upload-CSRF": "wrong"}).status_code == 403
    assert client.get(path, headers=[("X-Upload-CSRF", token), ("X-Upload-CSRF", token)]).status_code == 403
    for origin in ("null", "https://evil.example", "http://localhost", "http://127.0.0.1:8888"):
        assert client.get(path, headers={"X-Upload-CSRF": token, "Origin": origin}).status_code == 403
    assert client.get(path, headers={"X-Upload-CSRF": token, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    image = client.get(path, headers={"X-Upload-CSRF": token, "Origin": "http://127.0.0.1"})
    assert image.status_code == 200 and image.content == png_image()
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    rows = client.get("/api/v1/uploads/operations").json()
    assert set(rows[0]) == {"id", "account_id", "action", "state", "code", "login_phase",
                            "qr_available", "qr_revision", "expires_at"}
    assert rows[0]["expires_at"] == expiry and rows[0]["qr_available"] is True
    encoded = base64.b64encode(png_image()).decode("ascii")
    assert encoded not in json.dumps(rows) and encoded not in caplog.text
    service = app.state.upload_manager.service
    with sqlite3.connect(service.database_path) as db:
        dump = "\n".join(db.iterdump())
    assert encoded not in dump and png_image().hex() not in dump.lower()
    client.headers["X-Upload-CSRF"] = token
    assert client.post(f"/api/v1/uploads/operations/{op['id']}/cancel").status_code == 200
    unavailable = client.get(path)
    assert unavailable.status_code == 409 and unavailable.json()["detail"] == "login_qr_unavailable"
    assert unavailable.headers["cache-control"] == "no-store"


@pytest.mark.skipif(os.name != "nt", reason="Bilibili backend targets Windows")
@pytest.mark.parametrize("platform", ["bilibili", "douyin", "tencent"])
@pytest.mark.parametrize("cancel", [False, True])
def test_backend_delivers_inline_progress_and_cleans_owned_process(tmp_path, monkeypatch, platform, cancel, capfd):
    backend = SauBackend(tmp_path, login_timeout=5)
    monkeypatch.setattr(backend, "inspect", lambda: {"ready": True, "code": "ready"})
    monkeypatch.setattr(backend, "_inspect_for_execution", lambda: {"ready": True, "code": "ready"})
    monkeypatch.setattr(backend_module, "browser_view", lambda _: nullcontext(None))
    script = tmp_path / "synthetic_bridge.py"
    script.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "operation=Path(sys.argv[-1])\n"
        "while not (operation/'go').exists(): time.sleep(.01)\n"
        "request=json.loads((operation/'request.json').read_text())\n"
        "assert request['inline_login'] is True\n"
        "assert request['action']=='login'\n"
        "print('SYNTHETIC-CHILD-OUTPUT-MUST-STAY-PRIVATE',flush=True)\n"
        f"update={{'revision':1,'phase':'waiting_scan','png':{base64.b64encode(png_image()).decode('ascii')!r},'expires_at':int(time.time())+90}}\n"
        "temporary=operation/'login-update.tmp'\n"
        "temporary.write_text(json.dumps(update))\n"
        "temporary.replace(operation/'login-update.json')\n"
        "while not (operation/'seen').exists(): time.sleep(.01)\n"
        "(operation/'result.json').write_text(json.dumps({'status':'ready','code':'account_ready','evidence_kind':None}))\n",
        encoding="utf-8",
    )
    operations = []
    processes = []
    original_popen = backend_module.subprocess.Popen

    def command(operation_dir):
        operations.append(operation_dir)
        return [sys.executable, "-I", str(script), str(operation_dir)]

    def popen(*args, **kwargs):
        assert kwargs["stdout"] == kwargs["stderr"] == backend_module.subprocess.DEVNULL
        assert kwargs["creationflags"] == backend_module.subprocess.CREATE_NO_WINDOW
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(backend, "_bridge_command", command)
    monkeypatch.setattr(backend_module.subprocess, "Popen", popen)
    stop = threading.Event()
    updates = []

    def on_update(phase, png, expires_at):
        updates.append((phase, png, expires_at))
        if cancel:
            stop.set()
        else:
            (operations[0] / "seen").write_text("seen")

    result = backend.login_interactive(platform, "synthetic-account", stop, on_update)
    expected = BackendResult("cancelled", "cancelled") if cancel else BackendResult("ready", "account_ready")
    assert result == expected
    assert len(updates) == 1 and updates[0][:2] == ("waiting_scan", png_image())
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not operations[0].exists()
    assert "SYNTHETIC-CHILD-OUTPUT" not in capfd.readouterr().out

"""Owned subprocess boundary for optional, separately installed upload tools.

No upstream output is returned to the API or written to an application log.
The child must pass an ownership gate before it may import an uploader.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Event

from ..windows_job import WindowsKillOnCloseJob
from .contracts import BackendResult, UploadRequest
from .login_progress import read_update
from .runtime_setup import (
    BILIUP_VERSION,
    SAU_COMMIT,
    RuntimeInspectionCache,
    SetupError,
    browser_view,
    inspect_runtime,
    isolated_python_command,
    runtime_lock,
)


_ACCOUNT = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_PLATFORMS = frozenset({"bilibili", "douyin", "tencent"})
_RESULTS = frozenset({
    ("ready", "account_ready"),
    ("failed", "account_missing"),
    ("failed", "account_invalid"),
    ("failed", "login_failed"),
    ("failed", "backend_failed"),
    ("failed", "login_terminal_unavailable"),
    ("failed", "login_expired"),
    ("failed", "login_qr_expired"),
    ("failed", "login_verification_required"),
    ("failed", "login_qr_unavailable"),
    ("failed", "login_timeout"),
    ("failed", "invalid_platform"),
    ("submitted", "upstream_submitted"),
    ("draft_saved", "upstream_draft_saved"),
    ("unknown", "upstream_result_unknown"),
})


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    # Refuse redirects in this path, including Windows junctions.
    while current != current.parent:
        metadata = current.lstat()
        if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("unsafe_private_directory")
        current = current.parent
    if os.name != "nt":
        path.chmod(0o700)


@contextmanager
def _operation_directory(parent: Path):
    operation = Path(tempfile.mkdtemp(prefix="operation-", dir=parent))
    try:
        yield operation
    finally:
        # Windows may release a killed descendant's cwd a moment after the
        # parent exits. Cleanup must never convert an uncertain upload to a
        # safely retryable failure. Any residual files remain in private storage.
        for attempt in range(40):
            try:
                shutil.rmtree(operation)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt < 39:
                    time.sleep(0.05)


def _biliup_checkpoint_name(media: Path) -> str:
    # Fixed biliup v1.2.4 uploader.rs uses this 64-bit polynomial over the
    # *argument path*. Stage a unique operation path, so no account can resume
    # another account's earlier upload checkpoint for the original media path.
    value = 0
    for character in str(media):
        value = (value * 31 + ord(character)) & 0xFFFFFFFFFFFFFFFF
    return f"biliup_checkpoint_{value}.json"


def _windows_local_data() -> Path:
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                      wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(32768)
    if shell.SHGetFolderPathW(None, 0x1C, None, 0, buffer) != 0:
        raise OSError("local_data_unavailable")
    return Path(buffer.value)


@contextmanager
def _biliup_media(operation: Path, payload: dict):
    media = operation / ("media" + Path(payload["file_path"]).suffix.lower())
    try:
        os.link(payload["file_path"], media)
    except OSError:
        shutil.copyfile(payload["file_path"], media)
    checkpoint = _windows_local_data() / _biliup_checkpoint_name(media) if os.name == "nt" else None
    if checkpoint is not None and checkpoint.exists():
        raise ValueError("checkpoint_collision")
    try:
        yield {**payload, "file_path": str(media)}
    finally:
        # dirs::data_local_dir uses Windows Known Folders, not just LOCALAPPDATA.
        # Delete only this previously absent, operation-specific checkpoint.
        if checkpoint is not None:
            for attempt in range(40):
                try:
                    checkpoint.unlink(missing_ok=True)
                    break
                except OSError:
                    if attempt < 39:
                        time.sleep(.05)


class SauBackend:
    def __init__(self, root: Path, *, login_timeout: float = 600,
                 check_timeout: float = 120, upload_timeout: float = 7200):
        self.root = Path(root).absolute()
        self.login_timeout = login_timeout
        self.check_timeout = check_timeout
        self.upload_timeout = upload_timeout
        self._runtime_cache = RuntimeInspectionCache()

    def inspect(self) -> dict:
        if os.name != "nt":
            status = {"ready": False, "code": "unsupported_platform"}
        elif not self.root.is_dir():
            status = {"ready": False, "code": "runtime_missing"}
        else:
            try:
                with runtime_lock(self.root, exclusive=False):
                    status = inspect_runtime(self.root, cache=self._runtime_cache)
            except SetupError as exc:
                status = {"ready": False, "code": str(exc)}
        return {**status, "backend": "social-auto-upload", "revision": SAU_COMMIT,
                "biliup_version": BILIUP_VERSION,
                "platforms": ["bilibili", "douyin", "tencent"]}

    def _inspect_for_execution(self) -> dict:
        """Perform the uncached integrity check required before any child starts."""
        if os.name != "nt":
            return {"ready": False, "code": "unsupported_platform"}
        return inspect_runtime(self.root)

    def login(self, platform: str, account_id: str, stop: Event) -> BackendResult:
        return self._run("login", platform, account_id, {}, stop, self.login_timeout)

    def login_interactive(self, platform: str, account_id: str, stop: Event, on_update) -> BackendResult:
        return self._run("login", platform, account_id, {"inline_login": True}, stop,
                         self.login_timeout, on_update)

    def check(self, platform: str, account_id: str, stop: Event) -> BackendResult:
        return self._run("check", platform, account_id, {}, stop, self.check_timeout)

    def upload(self, request: UploadRequest, stop: Event) -> BackendResult:
        if request.mode not in {"publish", "draft"} or (
            request.mode == "draft" and request.platform != "tencent"
        ):
            return BackendResult("failed", "unsupported_mode")
        if request.platform == "bilibili" and (
            request.copyright not in {1, 2}
            or isinstance(request.copyright, bool)
            or not isinstance(request.category_id, int)
            or isinstance(request.category_id, bool)
            or not 1 <= request.category_id <= 65535
            or (request.copyright == 2 and not request.source_credit.strip())
        ):
            return BackendResult("failed", "invalid_bilibili_metadata")
        if not request.file_path.is_absolute() or not request.file_path.is_file():
            return BackendResult("failed", "media_missing")
        data = asdict(request)
        data["file_path"] = str(request.file_path)
        return self._run("upload", request.platform, request.account_id, data,
                         stop, self.upload_timeout)

    def _run(self, action: str, platform: str, account: str, payload: dict,
             stop: Event, timeout: float, on_update=None) -> BackendResult:
        if not self.root.is_dir():
            return BackendResult("failed", "runtime_missing")
        try:
            with runtime_lock(self.root, exclusive=False):
                return self._run_locked(action, platform, account, payload, stop, timeout, on_update)
        except SetupError as exc:
            return BackendResult("failed", str(exc))

    def _run_locked(self, action: str, platform: str, account: str, payload: dict,
                    stop: Event, timeout: float, on_update=None) -> BackendResult:
        if platform not in _PLATFORMS or not _ACCOUNT.fullmatch(account):
            return BackendResult("failed", "invalid_account")
        if stop.is_set():
            return BackendResult("cancelled", "cancelled")
        state = self._inspect_for_execution()
        if not state["ready"]:
            return BackendResult("failed", state["code"])
        if action == "login" and platform == "bilibili" and os.name != "nt":
            return BackendResult("failed", "login_terminal_unavailable")
        try:
            private = self.root / "private"
            _private_directory(private)
            account_dir = private / "accounts" / platform
            _private_directory(account_dir)
            account_file = account_dir / f"{account}.json"
            if account_file.exists() and (account_file.is_symlink() or
                getattr(account_file.lstat(), "st_file_attributes", 0) & 0x400):
                return BackendResult("failed", "invalid_account")
            if action != "login" and not account_file.is_file():
                return BackendResult("failed", "account_missing")
            operations = private / "operations"
            _private_directory(operations)
            with _operation_directory(operations) as operation:
                request_file = operation / "request.json"
                data = {"action": action, "platform": platform,
                        "account_file": str(account_file), "payload": payload,
                        "source": str(self.root / "runtime" / "source"),
                        "biliup": str(self.root / "runtime" / "biliup.exe")}
                data["inline_login"] = action == "login" and payload.get("inline_login") is True
                data["operation_dir"] = str(operation)
                data["timeout_seconds"] = min(timeout, 300)
                view = browser_view(self.root / "runtime") if platform != "bilibili" else nullcontext(None)
                media = _biliup_media(operation, payload) if platform == "bilibili" and action == "upload" else nullcontext(payload)
                with view as browser, media as upload_payload:
                    data["payload"] = upload_payload
                    data["browser_path"] = str(browser) if browser else None
                    request_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    result = self._execute(operation, action, platform, stop, timeout, on_update)
                if action == "upload":
                    expected = "draft_saved" if payload.get("mode") == "draft" else "submitted"
                    if result.status in {"ready", "submitted", "draft_saved"} and result.status != expected:
                        return BackendResult("unknown", "upstream_result_unknown")
                return result
        except (OSError, ValueError):
            return BackendResult("failed", "backend_failed")

    def _execute(self, operation: Path, action: str, platform: str,
                 stop: Event, timeout: float, on_update=None) -> BackendResult:
        runtime = self.root / "runtime"
        command = self._bridge_command(operation)
        visible = action == "login" and platform == "bilibili" and os.name == "nt" and on_update is None
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                                          "USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA"}}
        environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                            "PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")})
        scratch = operation / "tmp"
        scratch.mkdir()
        environment.update({"TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch),
                            "XDG_DATA_HOME": str(scratch), "LOCALAPPDATA": str(scratch)})
        options = {"cwd": str(operation), "env": environment, "shell": False,
                   "stdin": None if visible else subprocess.DEVNULL,
                   "stdout": None if visible else subprocess.DEVNULL,
                   "stderr": None if visible else subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_CONSOLE if visible else subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
        job = None
        process = None
        released = False
        try:
            if os.name == "nt":
                job = WindowsKillOnCloseJob()
            process = subprocess.Popen(command, **options)
            if job is not None:
                job.assign_pid(process.pid)
            if stop.is_set():
                return BackendResult("cancelled", "cancelled")
            (operation / "go").write_text("go", encoding="ascii")
            released = True
            deadline = time.monotonic() + timeout
            revision = 0
            while process.poll() is None:
                if stop.wait(0.1):
                    return BackendResult("unknown", "upload_cancelled_unknown") if action == "upload" else BackendResult("cancelled", "cancelled")
                if time.monotonic() >= deadline:
                    return BackendResult("unknown", "upload_timeout_unknown") if action == "upload" else BackendResult("failed", "backend_timeout")
                if on_update is not None:
                    update = read_update(operation, revision)
                    if update is not None:
                        revision, phase, png, expires_at = update
                        on_update(phase, png, expires_at)
            result_path = operation / "result.json"
            if result_path.is_file() and result_path.stat().st_size <= 1024:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                pair = (result.get("status"), result.get("code"))
                if pair in _RESULTS and process.returncode == 0:
                    # Result classes must also match the operation that was requested.
                    if action == "upload" or pair[0] not in {"submitted", "draft_saved", "unknown"}:
                        return BackendResult(*pair)
            return BackendResult("unknown", "upstream_result_unknown") if action == "upload" else BackendResult("failed", "backend_failed")
        except Exception:
            return BackendResult("unknown", "upstream_result_unknown") if action == "upload" and released else BackendResult("failed", "backend_failed")
        finally:
            if job is not None:
                job.close()
            elif process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def _bridge_command(self, operation: Path) -> list[str]:
        runtime = self.root / "runtime"
        python = runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return isolated_python_command(
            python,
            Path(__file__).with_name("bridge.py"),
            operation / "tmp" / "pycache",
            "--operation",
            str(operation),
        )

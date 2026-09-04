"""Explicit installer for the optional pinned Windows upload runtime.

Downloads and installation happen only through this command, never on a web
request. The application environment is neither installed into nor upgraded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..source_environment_lock import SourceEnvironmentBusy, source_environment_lock
from ..windows_job import WindowsKillOnCloseJob


SAU_COMMIT = "0012d2c355f88f683cc38dde2a2db209e14091bc"
SAU_SHA256 = "c647bfd86be8e9c35bd50dafd92b1a1e150a148891b5f856f3505ebe7fe6615c"
SAU_URL = f"https://codeload.github.com/dreammis/social-auto-upload/zip/{SAU_COMMIT}"
BILIUP_VERSION = "v1.2.4"
BILIUP_URL = "https://github.com/biliup/biliup/releases/download/v1.2.4/biliupR-v1.2.4-x86_64-windows.zip"
BILIUP_SHA256 = "cb5af47aeaffd63719c94fa354a4d1404dd8437b6cc215513ec4e6054177c93e"
LOCK_PATH = Path(__file__).with_name("runtime-lock.json")


class SetupError(RuntimeError):
    pass


@contextmanager
def runtime_lock(root: Path, *, exclusive: bool):
    if os.name == "nt":
        try:
            with source_environment_lock(root, exclusive=exclusive):
                yield
        except SourceEnvironmentBusy:
            raise SetupError("runtime_busy") from None
    else:
        # Optional upload runtime is currently Windows-only. This branch keeps
        # offline contract inspection available without pretending browser
        # descendants can be contained by a POSIX process group alone.
        yield


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_python(root: Path) -> Path:
    return root / "runtime" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _reject_redirects(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.exists() or current.is_symlink():
            metadata = current.lstat()
            if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise SetupError("runtime_redirect_rejected")
        current = current.parent


def inspect_runtime(root: Path) -> dict:
    runtime = Path(root) / "runtime"
    manifest_path = runtime / "manifest.json"
    if not manifest_path.is_file():
        return {"ready": False, "code": "runtime_missing"}
    try:
        _reject_redirects(runtime)
        if manifest_path.stat().st_size > 1_000_000:
            raise ValueError()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest["schema"] != 1 or manifest["sau_commit"] != SAU_COMMIT
                or manifest["sau_archive_sha256"] != SAU_SHA256
                or manifest["biliup_version"] != BILIUP_VERSION
                or manifest["biliup_archive_sha256"] != BILIUP_SHA256
                or manifest["requirements_sha256"] != sha256(LOCK_PATH)
                or manifest["cli_help_verified"] is not True
                or manifest["browser_launch_verified"] is not True):
            raise ValueError()
        if not runtime_python(Path(root)).is_file():
            raise ValueError()
        artifacts = manifest["artifacts"]
        if not isinstance(artifacts, dict) or "biliup.exe" not in artifacts or "source/sau_cli.py" not in artifacts:
            raise ValueError()
        for relative, digest in artifacts.items():
            path = runtime.joinpath(*PurePosixPath(relative).parts)
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise ValueError()
            if path.is_symlink() or not path.is_file() or sha256(path) != digest:
                raise ValueError()
        if not any((runtime / "browsers").glob("chromium-*")):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, SetupError):
        return {"ready": False, "code": "runtime_invalid"}
    return {"ready": True, "code": "ready"}


def _download(url: str, path: Path, expected: str) -> None:
    if path.is_file() and sha256(path) == expected:
        return
    request = urllib.request.Request(url, headers={"User-Agent": "Open-Flame-upload-runtime/1"})
    part = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as target:
        shutil.copyfileobj(response, target)
    if sha256(part) != expected:
        part.unlink()
        raise SetupError("archive_hash_mismatch")
    part.replace(path)


def extract_zip(archive: Path, destination: Path, *, strip_root: bool = False) -> None:
    with zipfile.ZipFile(archive) as bundle:
        total = 0
        for item in bundle.infolist():
            parts = PurePosixPath(item.filename).parts
            if (PurePosixPath(item.filename).is_absolute() or ".." in parts
                    or any(":" in p or "\\" in p for p in parts)
                    or (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise SetupError("unsafe_archive")
            parts = parts[1:] if strip_root else parts
            if not parts:
                continue
            total += item.file_size
            if total > 512 * 1024 * 1024:
                raise SetupError("archive_too_large")
            target = destination.joinpath(*parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)


def _environment(runtime: Path) -> dict:
    keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
            "HOME", "LOCALAPPDATA", "APPDATA", "PATH"}
    result = {key: value for key, value in os.environ.items() if key.upper() in keep}
    result.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                   "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                   "PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")})
    return result


def _command(command: list[str], runtime: Path, code: str, *, timeout: float = 1800) -> None:
    if os.name != "nt":
        raise SetupError("windows_x64_required")
    process = None
    with tempfile.TemporaryDirectory(prefix="command-", dir=runtime) as scratch:
        gate = Path(scratch) / "go"
        wrapped = [sys.executable, "-I", str(Path(__file__).with_name("setup_gate.py")),
                   "--gate", str(gate), *command]
        with WindowsKillOnCloseJob() as job:
            try:
                process = subprocess.Popen(wrapped, cwd=runtime, env=_environment(runtime),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign_pid(process.pid)
                gate.write_text("go", encoding="ascii")
                if process.wait(timeout=timeout):
                    raise SetupError(code)
            except Exception:
                raise SetupError(code) from None
            finally:
                job.close()
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)


def _requirements() -> str:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8-sig"))
    return "\n".join(
        f"{p['name']}=={p['version']} " + " ".join(f"--hash=sha256:{h}" for h in p["sha256"])
        for p in lock["packages"]
    ) + "\n"


def _private_acl(private: Path, runtime: Path) -> None:
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # The current local user owns the optional accounts and temporary files.
        # No credential values are involved in resolving or applying this ACL.
        name = subprocess.check_output(["whoami"], text=True, encoding="utf-8").strip()
        _command(["icacls", str(private), "/inheritance:r", "/grant:r", f"{name}:(OI)(CI)F"],
                 runtime, "private_acl_failed", timeout=30)
    else:
        private.chmod(0o700)


def verify_cli(root: Path) -> None:
    runtime = root / "runtime"
    python = runtime_python(root)
    with tempfile.TemporaryDirectory(prefix="help-", dir=runtime) as scratch:
        _command([str(python), "-I", str(Path(__file__).with_name("bridge.py")),
                  "--operation", scratch, "--check-install", str(runtime / "source")],
                 runtime, "cli_help_failed", timeout=60)
    _command([str(runtime / "biliup.exe"), "upload", "--help"], runtime, "biliup_help_failed", timeout=30)
    with browser_view(runtime) as browser:
        _command([str(python), "-I", "-c",
              "from patchright.sync_api import sync_playwright; "
              f"p=sync_playwright().start(); b=p.chromium.launch(headless=True,executable_path={str(browser)!r}); "
              "page=b.new_page(); page.set_content('<title>Open-Flame runtime check</title>'); "
              "assert page.title()=='Open-Flame runtime check'; b.close(); p.stop()"],
             runtime, "browser_launch_failed", timeout=60)


def _prepare_short_browser(runtime: Path) -> None:
    # Windows SxS assembly lookup can fail for the deeper Playwright cache path
    # even when CreateProcess's overall path is under MAX_PATH. Use a flat owned
    # layout. NTFS hardlinks keep disk use unchanged and retain original bytes.
    downloaded = runtime / "browsers" / "chromium-1208" / "chrome-win64"
    target = runtime / "chrome"
    for source in downloaded.rglob("*"):
        path = target / source.relative_to(downloaded)
        if source.is_dir():
            path.mkdir(parents=True, exist_ok=True)
        elif not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, path)
            except OSError:
                shutil.copyfile(source, path)


@contextmanager
def browser_view(runtime: Path):
    """An owned binary-only temporary view; no account or browser profile here.

    The 145 Windows SxS loader failed for the normal application's storage path
    on the verified host. An independently owned temporary hardlink view of the
    same bytes launches correctly without changing system/browser settings.
    """
    base = Path(tempfile.mkdtemp(prefix="of-browser-"))
    try:
        for source in (runtime / "chrome").rglob("*"):
            target = base / source.relative_to(runtime / "chrome")
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)
        yield base / "chrome.exe"
    finally:
        for attempt in range(40):
            try:
                shutil.rmtree(base)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt < 39:
                    time.sleep(.05)


def install(root: Path, python: Path) -> dict:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SetupError("windows_x64_required")
    if not python.is_absolute() or not python.is_file():
        raise SetupError("python_absolute_path_required")
    for directory in (root, root / "runtime", root / "private"):
        _reject_redirects(directory)
    root.mkdir(parents=True, exist_ok=True)
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    _private_acl(root / "private", runtime)
    _command([str(python), "-I", "-c",
              "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8"],
             runtime, "cpython_312_x64_required", timeout=30)
    previous = inspect_runtime(root)
    if previous["ready"]:
        verify_cli(root)
        return {"ready": True, "code": "runtime_already_ready"}
    if previous["code"] == "runtime_invalid":
        raise SetupError("runtime_invalid_requires_new_root")
    print("Preparing pinned source archives", flush=True)
    cache = runtime / "archives"
    cache.mkdir(exist_ok=True)
    source_zip = cache / f"social-auto-upload-{SAU_COMMIT}.zip"
    bili_zip = cache / f"biliup-{BILIUP_VERSION}-windows-x64.zip"
    _download(SAU_URL, source_zip, SAU_SHA256)
    _download(BILIUP_URL, bili_zip, BILIUP_SHA256)
    source = runtime / "source"
    if not source.exists():
        source.mkdir()
        extract_zip(source_zip, source, strip_root=True)
        (runtime / "source_revision.txt").write_text(SAU_COMMIT, encoding="ascii")
    elif (runtime / "source_revision.txt").read_text(encoding="ascii") != SAU_COMMIT:
        raise SetupError("existing_source_mismatch")
    if not (runtime / "biliup.exe").is_file():
        with tempfile.TemporaryDirectory(prefix="biliup-", dir=runtime) as scratch:
            extracted = Path(scratch)
            extract_zip(bili_zip, extracted)
            binaries = [p for p in extracted.rglob("*.exe") if p.name.lower() in {"biliup.exe", "biliupr.exe"}]
            if len(binaries) != 1:
                raise SetupError("biliup_binary_missing")
            shutil.copyfile(binaries[0], runtime / "biliup.exe")
    print("Preparing isolated CPython 3.12 environment", flush=True)
    if not runtime_python(root).is_file():
        _command([str(python), "-I", "-m", "venv", str(runtime / "venv")], runtime, "venv_creation_failed")
    requirement_path = runtime / "requirements.lock"
    requirement_path.write_text(_requirements(), encoding="utf-8")
    _command([str(runtime_python(root)), "-I", "-m", "pip", "install", "--require-hashes",
              "--only-binary=:all:", "--index-url", "https://pypi.org/simple",
              "-r", str(requirement_path)], runtime, "dependency_install_failed")
    _command([str(runtime_python(root)), "-I", "-m", "pip", "check"], runtime, "dependency_check_failed", timeout=60)
    print("Preparing pinned Chromium runtime", flush=True)
    _command([str(runtime_python(root)), "-I", "-m", "patchright", "install", "chromium", "--no-shell"],
             runtime, "browser_install_failed")
    _prepare_short_browser(runtime)
    print("Checking uploader command contracts and local browser launch", flush=True)
    verify_cli(root)
    artifacts = {"biliup.exe": sha256(runtime / "biliup.exe"),
                 "chrome/chrome.exe": sha256(runtime / "chrome" / "chrome.exe"),
                 "chrome/145.0.7632.6.manifest": sha256(runtime / "chrome" / "145.0.7632.6.manifest")}
    with zipfile.ZipFile(source_zip) as bundle:
        for entry in bundle.infolist():
            relative = PurePosixPath(*PurePosixPath(entry.filename).parts[1:])
            if not entry.is_dir() and (relative.suffix in {".py", ".js"} or relative.name == "LICENSE"):
                expected = hashlib.sha256(bundle.read(entry)).hexdigest()
                path = source.joinpath(*relative.parts)
                if sha256(path) != expected:
                    raise SetupError("source_integrity_failed")
                artifacts["source/" + relative.as_posix()] = expected
    actual_sources = {p.relative_to(runtime).as_posix() for p in source.rglob("*")
                      if p.is_file() and (p.suffix in {".py", ".js"} or p.name == "LICENSE")}
    if actual_sources != {p for p in artifacts if p.startswith("source/")}:
        raise SetupError("unexpected_source_file")
    with zipfile.ZipFile(bili_zip) as bundle:
        binaries = [entry for entry in bundle.infolist() if PurePosixPath(entry.filename).name.lower() in {"biliup.exe", "biliupr.exe"}]
        if len(binaries) != 1 or hashlib.sha256(bundle.read(binaries[0])).hexdigest() != artifacts["biliup.exe"]:
            raise SetupError("biliup_integrity_failed")
    manifest = {"schema": 1, "sau_commit": SAU_COMMIT, "sau_archive_sha256": SAU_SHA256,
                "biliup_version": BILIUP_VERSION, "biliup_archive_sha256": BILIUP_SHA256,
                "requirements_sha256": sha256(LOCK_PATH), "cli_help_verified": True,
                "browser_launch_verified": True, "artifacts": artifacts}
    (runtime / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return inspect_runtime(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path, help="Absolute path to an existing CPython 3.12 x64 interpreter")
    parser.add_argument("--check", action="store_true", help="Verify existing runtime; never install or log in")
    args = parser.parse_args(argv)
    if not args.root.is_absolute():
        parser.error("--root must be absolute")
    try:
        if args.check:
            if not args.root.is_dir():
                result = {"ready": False, "code": "runtime_missing"}
            else:
                with runtime_lock(args.root, exclusive=False):
                    result = inspect_runtime(args.root)
                    if result["ready"]:
                        verify_cli(args.root)
        else:
            if args.python is None:
                parser.error("--python is required for installation")
            args.root.mkdir(parents=True, exist_ok=True)
            with runtime_lock(args.root, exclusive=True):
                result = install(args.root, args.python)
    except SetupError as exc:
        result = {"ready": False, "code": str(exc)}
    except Exception:
        result = {"ready": False, "code": "runtime_setup_failed"}
    print(json.dumps(result))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

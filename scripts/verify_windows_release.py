"""Verify a fresh Windows source release using its real Setup and Start entries.

Default operation requires both local caches. Explicit --allow-network permits
the installer's pinned downloads. Every generated file stays under a new work
directory; reports contain fixed stage names, hashes and counts, not raw output.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import zipfile
from ctypes import wintypes
from pathlib import Path, PurePosixPath


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent / "src"))
from video_download_control.build_identity import package_payload_sha256
from video_download_control.windows_job import WindowsKillOnCloseJob


_MAX_OUTPUT = 2 * 1024 * 1024
_MAX_SOURCE_BYTES = 256 * 1024 * 1024
_HELPER = (
    "import subprocess,sys; gate=sys.stdin.buffer.read(1); "
    "sys.exit(71 if gate != b'G' else subprocess.call(sys.argv[1],"
    "stdin=subprocess.DEVNULL,shell=False,creationflags=subprocess.CREATE_NO_WINDOW))"
)
_UPLOAD_EXPECTED = {
    "status": "passed",
    "runtime_ready": False,
    "runtime_code": "runtime_missing",
    "platforms": ["bilibili", "douyin", "tencent"],
    "initial_draft_count": 3,
    "queue_counts": [1, 2, 3],
    "remaining_draft_counts": [2, 1, 0],
    "independent_changes": [1, 1, 1],
    "backend_calls": 0,
    "network_calls": 0,
}
_UPLOAD_PROBE = r'''from __future__ import annotations
import json
from pathlib import Path
import socket
import sys


def main():
    try:
        source = Path(sys.argv[1]).resolve(strict=True)
        root = Path(sys.argv[2]).resolve()
        sys.path.insert(0, str(source / "src"))

        network_calls = []
        original_socket = socket.socket

        class OfflineSocket(original_socket):
            def connect(self, *args, **kwargs):
                network_calls.append("connect")
                raise RuntimeError("network_disabled")

            def connect_ex(self, *args, **kwargs):
                network_calls.append("connect_ex")
                raise RuntimeError("network_disabled")

        def deny_connection(*args, **kwargs):
            network_calls.append("create_connection")
            raise RuntimeError("network_disabled")

        socket.socket = OfflineSocket
        socket.create_connection = deny_connection

        from video_download_control.uploads.service import UploadService

        missing = UploadService(root / "missing-runtime")
        try:
            runtime = missing.status()["backend"]
        finally:
            missing.stop()
        assert runtime.get("ready") is False
        assert runtime.get("code") == "runtime_missing"

        class ZeroRemoteBackend:
            def __init__(self):
                self.calls = []

            def inspect(self):
                return {"ready": True, "code": "synthetic_ready",
                        "backend": "zero-remote-synthetic",
                        "platforms": ["bilibili", "douyin", "tencent"]}

            def _unexpected(self, *args, **kwargs):
                self.calls.append("unexpected")
                raise RuntimeError("remote_backend_disabled")

            login = check = upload = _unexpected

        backend = ZeroRemoteBackend()
        service = UploadService(root / "synthetic", backend)
        try:
            accounts = {
                platform: service.add_account(platform, "synthetic-" + platform)
                for platform in ("bilibili", "douyin", "tencent")
            }
            with service._db() as database:
                database.execute("UPDATE accounts SET auth_state='ready',code='synthetic_ready'")
            media = root / "synthetic-input.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"offline release upload probe")
            source_row = service.import_source(media, "synthetic-input.mp4")
            jobs = []
            for platform in ("bilibili", "douyin", "tencent"):
                jobs.extend(service.create_jobs(
                    source_id=source_row["id"],
                    account_ids=[accounts[platform]["id"]],
                    title="synthetic release draft",
                    description="",
                    tags=["synthetic"] if platform == "bilibili" else [],
                    category_id=249 if platform == "bilibili" else None,
                    mode="draft" if platform == "tencent" else "publish",
                    copyright=1,
                    idempotency_key="release_" + platform,
                ))
            identifiers = [job["id"] for job in jobs]
            initial = service.jobs_by_ids(identifiers)
            assert all(job["state"] == "draft" for job in initial)
            assert {job["platform"] for job in initial} == set(accounts)
            queue_counts, draft_counts, changes = [], [], []
            previous = {job["id"]: job["state"] for job in initial}
            for job in jobs:
                assert service.confirm(job["id"])["state"] == "queued"
                current_rows = service.jobs_by_ids(identifiers)
                current = {row["id"]: row["state"] for row in current_rows}
                queue_counts.append(sum(state == "queued" for state in current.values()))
                draft_counts.append(sum(state == "draft" for state in current.values()))
                changes.append(sum(previous[key] != current[key] for key in identifiers))
                previous = current
            result = {
                "status": "passed",
                "runtime_ready": runtime["ready"],
                "runtime_code": runtime["code"],
                "platforms": sorted(job["platform"] for job in initial),
                "initial_draft_count": sum(job["state"] == "draft" for job in initial),
                "queue_counts": queue_counts,
                "remaining_draft_counts": draft_counts,
                "independent_changes": changes,
                "backend_calls": len(backend.calls),
                "network_calls": len(network_calls),
            }
            assert result == ''' + repr(_UPLOAD_EXPECTED) + r'''
        finally:
            service.stop()
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except BaseException:
        print('{"code":"upload_gate_failed","status":"failed"}')
        return 2


raise SystemExit(main())
'''


class VerificationFailure(Exception):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(stage)


def _require(condition: object, stage: str) -> None:
    if not condition:
        raise VerificationFailure(stage)


def _verify_release(directory: Path) -> dict:
    # Resolve the verifier beside this script, never an installed "release"
    # module or a historical local validation helper.
    specification = importlib.util.spec_from_file_location(
        "_open_flame_release_verifier", SCRIPTS / "release.py"
    )
    _require(specification is not None and specification.loader is not None, "release_verification")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module.verify_release(directory)


def _plain(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    _require(
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and not path.is_symlink()
        and not (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        and (directory or info.st_nlink == 1),
        "filesystem",
    )


def _absolute(path: Path, stage: str) -> None:
    _require(path.is_absolute() and Path(os.path.abspath(path)) == path, stage)
    _require(path != Path(path.anchor), stage)
    for parent in (path, *path.parents):
        if parent.exists():
            _plain(parent, directory=True)


def _sha256(path: Path) -> str:
    _plain(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    _plain(root, directory=True)
    snapshot = {}
    for path in sorted(root.rglob("*")):
        _plain(path, directory=path.is_dir())
        if path.is_file():
            snapshot[path.relative_to(root).as_posix()] = _sha256(path)
    return snapshot


def _extract_source(archive: Path, destination: Path, version: str) -> None:
    _plain(archive)
    top = f"Open-Flame-{version}-source"
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        _require(0 < len(entries) <= 4096, "source_extract")
        _require(sum(entry.file_size for entry in entries) <= _MAX_SOURCE_BYTES, "source_extract")
        planned, seen = [], set()
        for entry in entries:
            name = entry.orig_filename
            parts = name.rstrip("/").split("/")
            kind = stat.S_IFMT(entry.external_attr >> 16)
            _require(
                parts[0] == top and "\\" not in name
                and not PurePosixPath(name).is_absolute()
                and all(part not in {"", ".", ".."} and ":" not in part
                        and not part.endswith((" ", "."))
                        and part.split(".", 1)[0].upper() not in reserved
                        and not any(ord(character) < 32 for character in part)
                        for part in parts)
                and kind in {0, stat.S_IFREG, stat.S_IFDIR}
                and not (kind == stat.S_IFDIR and not entry.is_dir()),
                "source_extract",
            )
            folded = "/".join(parts).casefold()
            _require(folded not in seen, "source_extract")
            seen.add(folded)
            if len(parts) == 1:
                _require(entry.is_dir(), "source_extract")
                continue
            target = destination.joinpath(*parts[1:])
            _require(target.is_relative_to(destination), "source_extract")
            planned.append((entry, target))
        destination.mkdir()
        for entry, target in planned:
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)


def _isolated_environment(profile: Path) -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    for relative in ("local", "roaming", "temp"):
        (profile / relative).mkdir(parents=True, exist_ok=True)
    environment.update(
        USERPROFILE=str(profile), LOCALAPPDATA=str(profile / "local"),
        APPDATA=str(profile / "roaming"), TEMP=str(profile / "temp"), TMP=str(profile / "temp"),
        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
    )
    return environment


class _Capture:
    def __init__(self, stream):
        self.stream, self.payload = stream, bytearray()
        self.failed, self.overflow = False, False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while chunk := self.stream.read1(65536):
                remaining = max(0, _MAX_OUTPUT - len(self.payload))
                self.payload.extend(chunk[:remaining])
                self.overflow |= len(chunk) > remaining
        except (OSError, ValueError):
            self.failed = True

    def finish(self) -> bytes:
        self.thread.join(timeout=5)
        _require(not self.thread.is_alive() and not self.failed and not self.overflow, "command_output")
        return bytes(self.payload)


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("total_user", ctypes.c_longlong), ("total_kernel", ctypes.c_longlong),
        ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD),
    ]


def _job_counts(job: WindowsKillOnCloseJob) -> tuple[int, int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.QueryInformationJobObject
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    query.restype = wintypes.BOOL
    accounting = _JobAccounting()
    _require(query(job._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None), "command_cleanup")
    return int(accounting.total_processes), int(accounting.active_processes)


def _cmd_argument(value: str) -> str:
    _require(not any(character in value for character in ('"', "%", "\r", "\n", "\x00")), "command_arguments")
    return '"' + value + '"'


def _run_owned_cmd(entry: Path, arguments: list[str], *, cwd: Path, environment: dict,
                   work: Path, label: str, timeout: float) -> tuple[dict, bytes, bytes]:
    command_processor = Path(os.environ["SYSTEMROOT"]) / "System32/cmd.exe"
    command = (
        _cmd_argument(str(command_processor)) + ' /d /v:off /s /c "'
        + " ".join(_cmd_argument(value) for value in (str(entry), *arguments)) + '"'
    )
    started = time.monotonic()
    captures, process = [], None
    with WindowsKillOnCloseJob() as ownership:
        try:
            process = subprocess.Popen(
                [sys._base_executable, "-I", "-c", _HELPER, command],
                cwd=cwd, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            ownership.assign_pid(process.pid)
            captures = [_Capture(process.stdout), _Capture(process.stderr)]
            process.stdin.write(b"G")
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=timeout)
            stdout, stderr = (capture.finish() for capture in captures)
            deadline = time.monotonic() + 5
            while True:
                total, active = _job_counts(ownership)
                if active == 0 or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            _require(active == 0, "command_cleanup")
            return ({"label": label, "return_code": process.returncode,
                     "duration_seconds": round(time.monotonic() - started, 3),
                     "owned_processes_observed": total, "owned_processes_remaining": active}, stdout, stderr)
        except subprocess.TimeoutExpired:
            raise VerificationFailure("command_timeout") from None
        finally:
            # Closing this owned Job is the failure backstop, never pass evidence.
            ownership.close()
            if process is not None:
                if process.poll() is None:
                    process.wait(timeout=10)
                for capture in captures:
                    capture.thread.join(timeout=5)
                for suffix, capture in zip(("stdout", "stderr"), captures):
                    (work / f"{label}.{suffix}.log").write_bytes(capture.payload)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def _free_port(port: int = 0) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))
        return int(listener.getsockname()[1])


def _runtime_evidence(app: Path, version: str) -> dict:
    database = app / "data/control.sqlite3"
    _plain(database)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("credential_profiles", "batches", "download_jobs", "job_attempts", "media_assets")}
    _require(all(value == 0 for value in counts.values()), "check_database")
    paths = list((app / "data/logs").glob("*.jsonl"))
    _require(len(paths) == 3, "check_logs")
    events = []
    for path in paths:
        _plain(path)
        _require(path.stat().st_size <= 4 * 1024 * 1024, "check_logs")
        events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    names = {event["event"] for event in events}
    required = {"local_app.initializing", "local_app.child_ready", "local_app.claim_gate_prepared",
                "local_app.claim_gate_stopped", "local_app.stopped", "control.started", "control.stopped",
                "worker.preflight_started", "worker.preflight_succeeded", "worker.stopped"}
    _require(required <= names, "check_logs")
    _require(not names & {"worker.job_claimed", "worker.started", "local_app.claim_gate_activated",
                         "local_app.forced_shutdown", "runtime_log.event_rejected"}, "check_logs")
    _require({event["component"] for event in events} == {"control", "local-app", "local-worker"}, "check_logs")
    _require(len({event["run_id"] for event in events}) == 1 and len({event["pid"] for event in events}) == 3, "check_logs")
    _require(not any(event["level"] in {"ERROR", "CRITICAL"} for event in events), "check_logs")
    initial = next(event for event in events if event["event"] == "local_app.initializing")
    _require(initial["check_only"] and not initial["browser_enabled"] and initial["app_version"] == version, "check_logs")
    _require(all(event["reason"] == "check_complete" for event in events
                 if event["event"] in {"local_app.stopped", "worker.stopped"}), "check_logs")
    return {"database_counts": counts, "runtime_components": 3, "runtime_processes": 3,
            "runtime_run_count": 1, "runtime_event_count": len(events)}


def _upload_evidence(stdout: bytes) -> dict:
    """Accept only the fixed, identifier-free result from the extracted source probe."""
    try:
        lines = stdout.splitlines()
        payload = json.loads(lines[0]) if len(lines) == 1 else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    _require(payload == _UPLOAD_EXPECTED, "upload_offline_gate")
    return {
        "upload_runtime_code": payload["runtime_code"],
        "upload_platform_drafts": payload["initial_draft_count"],
        "upload_explicit_confirmations": payload["queue_counts"][-1],
        "upload_backend_calls": payload["backend_calls"],
        "upload_network_calls": payload["network_calls"],
    }


def verify_windows_release(release_dir: Path, work_dir: Path, *, wheelhouse: Path | None = None,
                           artifact_cache: Path | None = None, allow_network: bool = False) -> dict:
    _require(sys.platform == "win32", "windows_required")
    _absolute(release_dir, "release_directory")
    _absolute(work_dir, "work_directory")
    _require(not os.path.lexists(work_dir), "work_directory")
    _require(work_dir.parent.is_dir(), "work_directory")
    _require(allow_network or (wheelhouse is not None and artifact_cache is not None), "network_policy")
    for cache in (wheelhouse, artifact_cache):
        if cache is not None:
            _absolute(cache, "cache_directory")
            _plain(cache, directory=True)
    try:
        manifest = _verify_release(release_dir)
    except Exception:
        raise VerificationFailure("release_verification") from None
    version = manifest["product_identity"].split("+build.sha256.", 1)[0]
    _require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+_-]*)", version), "release_verification")
    source_zip = manifest["source_zip"]
    _require(Path(source_zip).name == source_zip and source_zip in manifest["artifacts"], "release_verification")
    work_dir.mkdir()
    report = {"status": "failed", "stage": "source_extract", "format_version": 1,
              "version": version, "product_identity": manifest["product_identity"],
              "source_zip_sha256": manifest["artifacts"][source_zip]["sha256"],
              "network_mode": "allowed" if allow_network else "offline_cache",
              "network_media_samples": 0, "steps": []}
    stage = "source_extract"
    started = time.monotonic()
    try:
        source = work_dir / "source 中文 !"
        archive = release_dir / source_zip
        expected_archive = manifest["artifacts"][source_zip]
        _require(_sha256(archive) == expected_archive["sha256"]
                 and archive.stat().st_size == expected_archive["size"], "source_archive_changed")
        _extract_source(archive, source, version)
        _require(_sha256(archive) == expected_archive["sha256"], "source_archive_changed")
        _require(not (source / ".venv").exists() and not (source / "runtime-tools").exists(), stage)
        initial_hash = package_payload_sha256(source / "src/video_download_control")
        _require(manifest["product_identity"] == f"{version}+build.sha256.{initial_hash}", "source_identity")
        entrypoints = manifest["entrypoint_sha256"]
        _require(all(_sha256(source / name) == digest for name, digest in entrypoints.items()), "source_identity")
        profile = work_dir / "profile 中文 !"
        environment = _isolated_environment(profile)
        working = work_dir / "外部 working !"
        working.mkdir()
        app = profile / "local/Open-Flame/video-download-control"
        arguments = ["--yes"]
        if wheelhouse is not None:
            arguments += ["--wheelhouse", str(wheelhouse)]
        if artifact_cache is not None:
            arguments += ["--artifact-cache", str(artifact_cache)]
        for stage, label in (("initial_install", "setup"), ("repeat_install", "repeat")):
            step, stdout, stderr = _run_owned_cmd(source / "Setup-Open-Flame.cmd", arguments,
                cwd=working, environment=environment, work=work_dir, label=label, timeout=1200)
            report["steps"].append(step)
            _require(step["return_code"] == 0 and not stderr, stage)
            _require({"status": "ready"} in [json.loads(line) for line in stdout.splitlines() if line.startswith(b"{")], stage)
            _require(not app.exists(), "setup_business_data")
            if label == "setup":
                before_environment = _snapshot(source / ".venv")
                before_tools = _snapshot(source / "runtime-tools")
        _require(before_environment == _snapshot(source / ".venv"), "repeat_environment_changed")
        _require(before_tools == _snapshot(source / "runtime-tools"), "repeat_tools_changed")
        report["repeat_environment_unchanged"] = report["repeat_tools_unchanged"] = True
        stage = "start_check"
        port = _free_port()
        step, stdout, stderr = _run_owned_cmd(source / "Start-Open-Flame.cmd",
            ["--check", "--no-open-browser", "--port", str(port),
             "--startup-timeout-seconds", "90", "--shutdown-timeout-seconds", "20"],
            cwd=working, environment=environment, work=work_dir, label="check", timeout=150)
        report["steps"].append(step)
        _require(step["return_code"] == 0 and not stderr, stage)
        _require([json.loads(line) for line in stdout.splitlines() if line.startswith(b"{")] == [{"status": "checked"}], stage)
        _free_port(port)
        report["port_released"] = True
        report.update(_runtime_evidence(app, version))
        stage = "upload_offline_gate"
        probe = work_dir / "upload-release-probe.py"
        probe.write_text(_UPLOAD_PROBE, encoding="utf-8", newline="\n")
        launcher = work_dir / "upload-release-probe.cmd"
        launcher.write_bytes(
            b'@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
            b'"%~1" -I "%~2" "%~3" "%~4"\r\nexit /b %errorlevel%\r\n'
        )
        step, stdout, stderr = _run_owned_cmd(
            launcher,
            [str(source / ".venv/Scripts/python.exe"), str(probe), str(source),
             str(work_dir / "upload probe data")],
            cwd=working, environment=environment, work=work_dir, label="upload", timeout=60,
        )
        report["steps"].append(step)
        _require(step["return_code"] == 0 and not stderr, stage)
        report.update(_upload_evidence(stdout))
        _require(not list((profile / "local/Open-Flame/diagnostics").glob("*.jsonl*")), "unexpected_diagnostic")
        _require(not (working / "data").exists() and not (source / "data").exists(), "business_data_location")
        _require(initial_hash == package_payload_sha256(source / "src/video_download_control"), "source_identity")
        _require(all(_sha256(source / name) == digest for name, digest in entrypoints.items()), "source_identity")
        report.update(status="passed", stage="complete", source_identity_unchanged=True,
                      entrypoints_unchanged=True, business_database_created_only_by_start=True)
    except VerificationFailure as error:
        report["stage"] = error.stage
    except KeyboardInterrupt:
        report["stage"] = "interrupted"
    except Exception:
        report["stage"] = stage
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    try:
        (work_dir / "report.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    except OSError:
        report.update(status="failed", stage="report_write")
    return report


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise VerificationFailure("arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--artifact-cache", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    try:
        args = parser.parse_args(argv)
        report = verify_windows_release(args.release_dir, args.work_dir, wheelhouse=args.wheelhouse,
                                        artifact_cache=args.artifact_cache, allow_network=args.allow_network)
    except VerificationFailure as error:
        report = {"status": "failed", "stage": error.stage}
    except KeyboardInterrupt:
        report = {"status": "failed", "stage": "interrupted"}
    except Exception:
        report = {"status": "failed", "stage": "internal_error"}
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["status"] == "passed" else (130 if report["stage"] == "interrupted" else 1)


if __name__ == "__main__":
    raise SystemExit(main())

"""The release smoke driver is standalone, scoped and defaults to local caches."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("windows_release_smoke", ROOT / "scripts/verify_windows_release.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
REAL_VERIFY_RELEASE = smoke._verify_release
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows release verification")
VERSION = "9.8.7"
PRIVATE = "SYNTHETIC-PRIVATE-SMOKE-OUTPUT"


@pytest.fixture
def release_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = tmp_path / "release"
    release.mkdir()
    payload = tmp_path / "payload"
    package = payload / "src/video_download_control"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{VERSION}"\n', encoding="utf-8")
    entrypoints = {}
    for name in ("Start-Open-Flame.cmd", "start_open_flame.py", "Setup-Open-Flame.cmd", "setup_open_flame.py"):
        data = f"synthetic {name}\n".encode()
        (payload / name).write_bytes(data)
        entrypoints[name] = hashlib.sha256(data).hexdigest()
    archive = release / f"Open-Flame-{VERSION}-source.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in payload.rglob("*"):
            if path.is_file():
                bundle.write(path, f"Open-Flame-{VERSION}-source/" + path.relative_to(payload).as_posix())
    manifest = {
        "source_zip": archive.name,
        "product_identity": f"{VERSION}+build.sha256.{smoke.package_payload_sha256(package)}",
        "entrypoint_sha256": entrypoints,
        "artifacts": {archive.name: {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size}},
    }
    monkeypatch.setattr(smoke, "_verify_release", lambda directory: manifest)
    wheels, tools = tmp_path / "wheels", tmp_path / "archives"
    wheels.mkdir()
    tools.mkdir()
    return release, manifest, wheels, tools


def _write_runtime(app: Path) -> None:
    logs = app / "data/logs"
    logs.mkdir(parents=True)
    with sqlite3.connect(app / "data/control.sqlite3") as database:
        for table in ("credential_profiles", "batches", "download_jobs", "job_attempts", "media_assets"):
            database.execute(f"CREATE TABLE {table} (id TEXT)")
    components = {
        "local-app": (1001, ["local_app.initializing", "local_app.child_ready", "local_app.claim_gate_prepared",
                             "local_app.claim_gate_stopped", "local_app.stopped"]),
        "control": (1002, ["control.started", "control.stopped"]),
        "local-worker": (1003, ["worker.preflight_started", "worker.preflight_succeeded", "worker.stopped"]),
    }
    for component, (pid, names) in components.items():
        events = []
        for name in names:
            event = {"component": component, "pid": pid, "event": name, "run_id": "synthetic-one-run", "level": "INFO"}
            if name == "local_app.initializing":
                event.update(check_only=True, browser_enabled=False, app_version=VERSION)
            if name in {"local_app.stopped", "worker.stopped"}:
                event["reason"] = "check_complete"
            events.append(event)
        (logs / f"runtime-{component}.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))


def _fake_commands(monkeypatch: pytest.MonkeyPatch, *, failure=None, mutation=None, forbidden_event=False):
    observed = []

    def run(entry, arguments, *, cwd, environment, work, label, timeout):
        source = entry.parent
        observed.append((label, list(arguments), dict(environment)))
        assert cwd == work / "外部 working !"
        if label == "upload":
            assert entry == work / "upload-release-probe.cmd"
            assert arguments == [
                str(work / "source 中文 !/.venv/Scripts/python.exe"),
                str(work / "upload-release-probe.py"),
                str(work / "source 中文 !"),
                str(work / "upload probe data"),
            ]
        else:
            assert source == work / "source 中文 !"
        assert all(Path(environment[key]).is_relative_to(work) for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"))
        assert not any(key.upper().startswith(("VDC_", "PIP_")) or key.upper().endswith("_PROXY") for key in environment)
        step = {"label": label, "return_code": 0, "duration_seconds": 0,
                "owned_processes_observed": 3, "owned_processes_remaining": 0}
        if label == failure:
            step["return_code"] = 23
            return step, b"", PRIVATE.encode()
        if label == "setup":
            (source / ".venv").mkdir()
            (source / ".venv/owned.txt").write_text("owned synthetic environment")
            (source / "runtime-tools").mkdir()
            (source / "runtime-tools/owned.txt").write_text("owned synthetic tools")
        elif label == "repeat" and mutation:
            (source / mutation / "owned.txt").write_text("changed unexpectedly")
        elif label == "check":
            app = Path(environment["LOCALAPPDATA"]) / "Open-Flame/video-download-control"
            _write_runtime(app)
            if forbidden_event:
                with (app / "data/logs/runtime-local-app.jsonl").open("a") as stream:
                    stream.write(json.dumps({"event": "local_app.claim_gate_activated"}) + "\n")
            return step, b'{"status":"checked"}\n', b""
        elif label == "upload":
            return step, (json.dumps(smoke._UPLOAD_EXPECTED, sort_keys=True).encode() + b"\n"), b""
        return step, b'{"status":"ready"}\n', b""

    monkeypatch.setattr(smoke, "_run_owned_cmd", run)
    return observed


def test_default_requires_both_local_caches_before_creating_work(release_fixture, tmp_path):
    release, _, wheels, _ = release_fixture
    work = tmp_path / "work"
    for arguments in ({}, {"wheelhouse": wheels}):
        with pytest.raises(smoke.VerificationFailure, match="^network_policy$"):
            smoke.verify_windows_release(release, work, **arguments)
    assert not work.exists()


def test_existing_work_directory_is_preserved(release_fixture, tmp_path):
    release, _, wheels, tools = release_fixture
    work = tmp_path / "work"
    work.mkdir()
    sentinel = work / "keep.txt"
    sentinel.write_text(PRIVATE)
    with pytest.raises(smoke.VerificationFailure, match="^work_directory$"):
        smoke.verify_windows_release(release, work, wheelhouse=wheels, artifact_cache=tools)
    assert sentinel.read_text() == PRIVATE
    assert list(work.iterdir()) == [sentinel]


def test_clean_flow_uses_manifest_identity_and_scoped_profile(release_fixture, tmp_path, monkeypatch):
    release, manifest, wheels, tools = release_fixture
    monkeypatch.setenv("PIP_INDEX_URL", PRIVATE)
    monkeypatch.setenv("HTTP_PROXY", PRIVATE)
    monkeypatch.setenv("VDC_DATA_ROOT", PRIVATE)
    observed = _fake_commands(monkeypatch)
    work = tmp_path / "work"
    report = smoke.verify_windows_release(release, work, wheelhouse=wheels, artifact_cache=tools)
    assert report["status"] == "passed" and report["stage"] == "complete"
    assert report["product_identity"] == manifest["product_identity"]
    assert report["network_mode"] == "offline_cache"
    assert report["runtime_components"] == 3 and report["runtime_run_count"] == 1
    assert report["business_database_created_only_by_start"]
    assert report["upload_runtime_code"] == "runtime_missing"
    assert report["upload_platform_drafts"] == 3
    assert report["upload_explicit_confirmations"] == 3
    assert report["upload_backend_calls"] == report["upload_network_calls"] == 0
    assert [entry[0] for entry in observed] == ["setup", "repeat", "check", "upload"]
    assert observed[0][1] == ["--yes", "--wheelhouse", str(wheels), "--artifact-cache", str(tools)]
    assert "--app-root" not in observed[2][1]
    text = (work / "report.json").read_text()
    assert PRIVATE not in text and str(tmp_path) not in text
    assert json.loads(text) == report


def test_network_install_requires_explicit_flag_and_passes_no_cache_options(release_fixture, tmp_path, monkeypatch):
    release, _, _, _ = release_fixture
    observed = _fake_commands(monkeypatch)
    report = smoke.verify_windows_release(release, tmp_path / "work", allow_network=True)
    assert report["status"] == "passed"
    assert report["network_mode"] == "allowed"
    assert observed[0][1] == ["--yes"]


def test_actual_sibling_verifier_rejects_incomplete_release_before_any_install(tmp_path, monkeypatch):
    release, work = tmp_path / "release", tmp_path / "work"
    release.mkdir()
    monkeypatch.setattr(smoke, "_verify_release", REAL_VERIFY_RELEASE)
    monkeypatch.setattr(smoke, "_run_owned_cmd", lambda *args, **kwargs: pytest.fail("invalid release ran"))
    with pytest.raises(smoke.VerificationFailure, match="^release_verification$"):
        smoke.verify_windows_release(release, work, allow_network=True)
    assert not work.exists()


def test_archive_changed_after_initial_verification_is_not_executed(release_fixture, tmp_path, monkeypatch):
    release, manifest, wheels, tools = release_fixture
    (release / manifest["source_zip"]).write_bytes(b"SYNTHETIC-changed-archive")
    monkeypatch.setattr(smoke, "_run_owned_cmd", lambda *args, **kwargs: pytest.fail("changed release ran"))
    report = smoke.verify_windows_release(release, tmp_path / "work", wheelhouse=wheels, artifact_cache=tools)
    assert report["status"] == "failed"
    assert report["stage"] == "source_archive_changed"


@pytest.mark.parametrize(("failure", "stage"), (("setup", "initial_install"), ("repeat", "repeat_install"),
                                                  ("check", "start_check"), ("upload", "upload_offline_gate")))
def test_failed_command_never_reports_pass_or_raw_output(release_fixture, tmp_path, monkeypatch, failure, stage):
    release, _, wheels, tools = release_fixture
    _fake_commands(monkeypatch, failure=failure)
    report = smoke.verify_windows_release(release, tmp_path / "work", wheelhouse=wheels, artifact_cache=tools)
    assert report["status"] == "failed" and report["stage"] == stage
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("runtime_code", "runtime_invalid"),
        ("queue_counts", [1, 1, 3]),
        ("independent_changes", [1, 2, 0]),
        ("backend_calls", 1),
        ("network_calls", 1),
        ("account_id", "a" * 32),
        ("source_path", PRIVATE),
    ),
)
def test_upload_evidence_rejects_wrong_behavior_or_extra_identifiers(field, value):
    payload = dict(smoke._UPLOAD_EXPECTED)
    payload[field] = value
    with pytest.raises(smoke.VerificationFailure, match="^upload_offline_gate$"):
        smoke._upload_evidence(json.dumps(payload).encode() + b"\n")


def test_actual_upload_probe_uses_packaged_source_and_emits_only_fixed_evidence(tmp_path):
    work = tmp_path / "probe work"
    source = work / "source 中文 !"
    shutil.copytree(ROOT / "src", source / "src")
    probe = work / "upload-release-probe.py"
    probe.write_text(smoke._UPLOAD_PROBE, encoding="utf-8", newline="\n")
    upload_root = work / "upload probe data"

    result = subprocess.run(
        [sys.executable, "-I", str(probe), str(source), str(upload_root)],
        cwd=work,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert result.returncode == 0 and result.stderr == b""
    assert smoke._upload_evidence(result.stdout)["upload_network_calls"] == 0
    assert str(tmp_path).encode() not in result.stdout
    assert not any(upload_root.rglob("runtime"))


@pytest.mark.parametrize(("tree", "stage"), ((".venv", "repeat_environment_changed"), ("runtime-tools", "repeat_tools_changed")))
def test_repeat_must_leave_environment_and_tools_unchanged(release_fixture, tmp_path, monkeypatch, tree, stage):
    release, _, wheels, tools = release_fixture
    _fake_commands(monkeypatch, mutation=tree)
    report = smoke.verify_windows_release(release, tmp_path / "work", wheelhouse=wheels, artifact_cache=tools)
    assert report["status"] == "failed" and report["stage"] == stage


def test_check_mode_must_not_activate_claim_gate(release_fixture, tmp_path, monkeypatch):
    release, _, wheels, tools = release_fixture
    _fake_commands(monkeypatch, forbidden_event=True)
    report = smoke.verify_windows_release(release, tmp_path / "work", wheelhouse=wheels, artifact_cache=tools)
    assert report["status"] == "failed" and report["stage"] == "check_logs"


@pytest.mark.parametrize("member", ("../escape.txt", "Open-Flame-9.8.7-source/../../escape.txt", "Open-Flame-9.8.7-source/link", "wrong-top/file", "Open-Flame-9.8.7-source/a\\b", "Open-Flame-9.8.7-source/NUL"))
def test_extraction_rejects_paths_and_links_even_after_archive_verification(tmp_path, member):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        entry = zipfile.ZipInfo(member)
        # ZipInfo normalizes platform separators at construction on Windows.
        # Preserve the raw archive header for this hostile-member fixture.
        entry.filename = entry.orig_filename = member
        if member.endswith("/link"):
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(entry, b"synthetic")
    with pytest.raises(smoke.VerificationFailure, match="^source_extract$"):
        smoke._extract_source(archive, tmp_path / "source", VERSION)
    assert not (tmp_path / "source").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_real_owned_cmd_supports_unicode_spaces_and_checks_exit_before_job_close(tmp_path):
    work = tmp_path / "工作 空格 !"
    work.mkdir()
    source = work / "source 中文 !"
    source.mkdir()
    entry = source / "synthetic.cmd"
    entry.write_text('@echo off\necho {"status":"ready"}\nexit /b 0\n', encoding="ascii")
    cwd = work / "外部 working !"
    cwd.mkdir()
    environment = smoke._isolated_environment(work / "profile")
    step, stdout, stderr = smoke._run_owned_cmd(entry, [], cwd=cwd, environment=environment,
                                             work=work, label="synthetic", timeout=20)
    assert step["return_code"] == 0
    assert step["owned_processes_remaining"] == 0
    assert step["owned_processes_observed"] >= 2
    assert json.loads(stdout) == {"status": "ready"}
    assert stderr == b""
    assert (work / "synthetic.stdout.log").read_bytes() == stdout


def test_invalid_cli_does_not_echo_arguments_or_create_work(tmp_path, capsys):
    result = smoke.main(["--unknown", PRIVATE])
    captured = capsys.readouterr()
    assert result == 1 and captured.err == ""
    assert json.loads(captured.out) == {"status": "failed", "stage": "arguments"}
    assert PRIVATE not in captured.out
    assert not list(tmp_path.iterdir())

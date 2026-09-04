"""Offline policy and subprocess contracts for the independent wheel verifier."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("open_flame_wheel_smoke_tests", ROOT / "scripts/verify_wheel_release.py")
assert _SPEC is not None and _SPEC.loader is not None
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


@pytest.fixture
def release_fixture(tmp_path, monkeypatch):
    directory = tmp_path / "release"
    directory.mkdir()
    cache = tmp_path / "wheel cache"
    cache.mkdir()
    work = tmp_path / "new wheel run"
    runtime = "".join(f"{name}==1.0 \\\n    --hash=sha256:{'a' * 64}\n" for name in smoke.RUNTIME_MODULES).encode()
    scripts = {f"video-download-example-{number}": "video_download_control.cli:main" for number in range(13)}
    config = ("[project.scripts]\n" + "".join(f'{name} = "{value}"\n' for name, value in scripts.items())).encode()
    source = {"deployment/requirements.runtime.lock": runtime, "pyproject.toml": config}
    wheel_name = "video_download_control-0.23.0-py3-none-any.whl"
    wheel = b"synthetic verified project wheel"
    manifest = {
        "version": "0.23.0", "product_identity": "0.23.0+build.sha256." + "b" * 64,
        "source_zip": "Open-Flame-0.23.0-source.zip",
        "source_files": {name: smoke.release.fingerprint(payload) for name, payload in source.items()},
        "artifacts": {wheel_name: smoke.release.fingerprint(wheel)},
    }
    calls = []

    def verify(path):
        assert path == directory and not work.exists()
        calls.append(("verify",))
        return manifest

    monkeypatch.setattr(smoke.release, "verify_release", verify)
    monkeypatch.setattr(smoke.release, "archive_payloads", lambda path: {"Open-Flame-0.23.0-source/" + n: b for n, b in source.items()})
    monkeypatch.setattr(smoke.release, "read_plain", lambda root, name: wheel)
    monkeypatch.setattr(smoke.release, "command", lambda python, arguments, cwd: calls.append((python, arguments, cwd)))
    return directory, cache, work, manifest, calls


def test_requires_explicit_network_or_cache_before_any_work(release_fixture):
    directory, _, work, _, calls = release_fixture
    result = smoke.verify_wheel_release(directory, work)
    assert result["status"] == "failed" and result["error_code"] == "network_confirmation_required"
    assert not work.exists() and not calls


@pytest.mark.parametrize("kind", ["directory", "file", "relative"])
def test_existing_or_relative_work_target_is_preserved(release_fixture, kind):
    directory, cache, work, _, calls = release_fixture
    if kind == "directory":
        work.mkdir()
        (work / "sentinel").write_bytes(b"synthetic existing content")
    elif kind == "file":
        work.write_bytes(b"synthetic existing content")
    else:
        work = Path("relative-new-work")
    result = smoke.verify_wheel_release(directory, work, wheelhouse=cache)
    assert result["error_code"] == "new_absolute_work_required" and not calls
    if kind == "directory":
        assert (work / "sentinel").read_bytes() == b"synthetic existing content"
        assert len(list(work.iterdir())) == 1
    elif kind == "file":
        assert work.read_bytes() == b"synthetic existing content"


def test_work_cannot_mutate_release_directory(release_fixture):
    directory, cache, _, _, calls = release_fixture
    work = directory / "new work"
    result = smoke.verify_wheel_release(directory, work, wheelhouse=cache)
    assert result["error_code"] == "work_overlaps_release"
    assert not work.exists() and not calls


def test_invalid_release_never_creates_environment_or_leaks_exception(release_fixture, monkeypatch, capsys):
    directory, cache, work, _, calls = release_fixture
    def invalid(_path):
        raise smoke.release.ReleaseError("SYNTHETIC-private-release-error")
    monkeypatch.setattr(smoke.release, "verify_release", invalid)
    assert smoke.main(["--release-dir", str(directory), "--work-dir", str(work), "--wheelhouse", str(cache)]) == 2
    output = capsys.readouterr()
    report = json.loads(output.err)
    assert report["status"] == "failed" and report["error_code"] == "release_verification_failed"
    assert "SYNTHETIC-private" not in output.err and str(work) not in output.err
    assert not work.exists() and not calls


def test_offline_commands_install_only_locked_runtime_and_verified_wheel(release_fixture):
    directory, cache, work, manifest, calls = release_fixture
    result = smoke.verify_wheel_release(directory, work, wheelhouse=cache, allow_network=True)
    assert result["status"] == "passed" and result["console_scripts_verified"] == 13
    assert result["runtime_dependencies_verified"] == 13
    assert calls[0] == ("verify",)
    commands = [call[1] for call in calls[1:]]
    assert commands[0][:3] == ["-m", "venv", "--without-pip"]
    assert commands[1] == ["-m", "ensurepip", "--upgrade"]
    runtime, wheel = [command for command in commands if "install" in command]
    for command in (runtime, wheel):
        assert {"--isolated", "--no-cache-dir", "--no-deps", "--only-binary=:all:", "--require-hashes", "--no-index"}.issubset(command)
        assert "--index-url" not in command and "--extra" not in command
    assert runtime[runtime.index("--find-links") + 1] == str(cache)
    assert "--find-links" not in wheel
    assert any(command[-1] == "check" for command in commands)
    project_lock = (work / "requirements.project-wheel.lock").read_text()
    assert "file:///" in project_lock and "%20" in project_lock
    assert manifest["artifacts"]["video_download_control-0.23.0-py3-none-any.whl"]["sha256"] in project_lock
    assert "pytest" not in (work / "requirements.runtime.lock").read_text()
    probe = commands[-1]
    assert probe[:2] == ["-c", smoke.INSTALLED_PROBE]
    expected = json.loads(probe[2])
    assert expected["product_identity"] == manifest["product_identity"]
    assert len(expected["scripts"]) == len(expected["runtime"]) == 13
    assert all(call[2] == work for call in calls[1:])
    assert json.loads((work / smoke.REPORT_NAME).read_text()) == result
    assert str(work) not in json.dumps(result)


def test_authorized_network_only_affects_runtime_install(release_fixture):
    directory, _, work, _, calls = release_fixture
    assert smoke.verify_wheel_release(directory, work, allow_network=True)["status"] == "passed"
    runtime, wheel = [call[1] for call in calls[1:] if "install" in call[1]]
    assert runtime[runtime.index("--index-url") + 1] == "https://pypi.org/simple"
    assert "--no-index" in wheel and "--index-url" not in wheel


@pytest.mark.parametrize("failure_at", [0, 1, 2, 3, 4, 5])
def test_every_child_failure_produces_failed_saved_report(release_fixture, monkeypatch, failure_at):
    directory, cache, work, _, _ = release_fixture
    count = 0
    def command(*_args):
        nonlocal count
        current = count
        count += 1
        if current == failure_at:
            raise RuntimeError("SYNTHETIC-private-child-output")
    monkeypatch.setattr(smoke.release, "command", command)
    result = smoke.verify_wheel_release(directory, work, wheelhouse=cache)
    assert result["status"] == "failed" and result["error_code"] == "wheel_smoke_failed"
    assert result["console_scripts_verified"] == 0
    assert "SYNTHETIC-private" not in json.dumps(result)
    assert count == failure_at + 1
    assert json.loads((work / smoke.REPORT_NAME).read_text()) == result


def test_cancelled_child_is_not_reported_as_passed(release_fixture, monkeypatch):
    directory, cache, work, _, _ = release_fixture
    def cancelled(*_args):
        raise KeyboardInterrupt
    monkeypatch.setattr(smoke.release, "command", cancelled)
    result = smoke.verify_wheel_release(directory, work, wheelhouse=cache)
    assert result["status"] == "cancelled"
    assert json.loads((work / smoke.REPORT_NAME).read_text()) == result


def test_source_lock_and_wheel_are_rechecked_after_release_verification(release_fixture, monkeypatch):
    directory, cache, work, _, calls = release_fixture
    monkeypatch.setattr(smoke.release, "read_plain", lambda *_: b"changed wheel")
    result = smoke.verify_wheel_release(directory, work, wheelhouse=cache)
    assert result["error_code"] == "release_changed" and not work.exists()
    assert calls == [("verify",)]


@pytest.mark.parametrize("payload", [b"--index-url https://example.invalid\n", b"pytest==1.0 --hash=sha256:" + b"a" * 64, b"anyio>=1.0\n"])
def test_runtime_lock_rejects_unpinned_dev_or_directive_content(payload):
    with pytest.raises(smoke.WheelSmokeError, match="runtime_lock_invalid"):
        smoke.runtime_requirements(payload)


def test_real_cli_help_and_invalid_arguments_are_safe(tmp_path):
    script = ROOT / "scripts/verify_wheel_release.py"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for arguments, expected_code in ((["--help"], 0), (["--unknown", "SYNTHETIC-PRIVATE-ARGUMENT"], 2)):
        result = subprocess.run([sys.executable, "-I", "-B", str(script), *arguments], cwd=tmp_path,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, **options)
        assert result.returncode == expected_code
        assert b"SYNTHETIC-PRIVATE-ARGUMENT" not in result.stdout + result.stderr
        if expected_code:
            assert json.loads(result.stderr)["error_code"] == "invalid_arguments"

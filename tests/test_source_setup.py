from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_download_control import source_setup as setup


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "source tree"
    root.mkdir()
    (root / "deployment").mkdir()
    (root / "deployment/requirements.runtime.lock").write_text(
        "example==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(setup, "_check_prerequisites", lambda root: None)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(profile))
    return root


def test_decline_never_creates_environment_or_runs_children(source, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    monkeypatch.setattr(setup, "_install", lambda *args: pytest.fail("declined setup ran"))
    assert setup.main([], repository_root=source) == 130
    assert not (source / ".venv").exists()
    assert '"status":"cancelled"' in capsys.readouterr().out


def test_eof_does_not_silently_authorize_installation(source, monkeypatch):
    def eof(_prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    monkeypatch.setattr(setup, "_install", lambda *args: pytest.fail("EOF setup ran"))
    assert setup.main([], repository_root=source) == 130


def test_explicit_confirmation_passes_only_install_options(source, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup, "_install", lambda *args: calls.append(args))
    assert setup.main(["--yes", "--repair"], repository_root=source) == 0
    assert len(calls) == 1
    assert calls[0][0] == source
    assert calls[0][1].repair
    assert '"status":"ready"' in capsys.readouterr().out


def test_invalid_arguments_do_not_echo_private_input(source, capsys):
    marker = "SYNTHETIC-PRIVATE-INVALID-SETUP"
    assert setup.main(["--unknown", marker], repository_root=source) == 2
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    assert json.loads(captured.err)["error_code"] == "invalid_arguments"


def test_unknown_broken_environment_is_not_changed(source, monkeypatch):
    environment = source / ".venv"
    environment.mkdir()
    sentinel = environment / "keep.txt"
    sentinel.write_text("SYNTHETIC-existing-environment")
    monkeypatch.setattr(setup, "_runtime_ready", lambda *args: False)
    monkeypatch.setattr(setup, "_command", lambda *args, **kwargs: pytest.fail("unknown env modified"))
    with pytest.raises(setup.SetupFailure) as failure:
        setup._ensure_environment(source, repair=True, wheelhouse=None)
    assert failure.value.code.value == "setup_environment_unavailable"
    assert sentinel.read_text() == "SYNTHETIC-existing-environment"
    assert list(environment.iterdir()) == [sentinel]


def test_healthy_unknown_environment_is_reused_without_pip(source, monkeypatch):
    (source / ".venv").mkdir()
    monkeypatch.setattr(setup, "_runtime_ready", lambda *args: True)
    monkeypatch.setattr(setup, "_command", lambda *args, **kwargs: pytest.fail("healthy env mutated"))
    assert setup._ensure_environment(source, repair=False, wheelhouse=None) == source / ".venv/Scripts/python.exe"
    assert not (source / ".venv" / setup.OWNER_FILENAME).exists()


def test_fresh_environment_is_marked_then_installed_with_hashes(source, monkeypatch):
    calls = []
    checks = iter([False, True])
    monkeypatch.setattr(setup, "_runtime_ready", lambda *args: next(checks))
    monkeypatch.setattr(setup, "_command", lambda python, arguments, **kwargs: calls.append(arguments) or True)
    python = setup._ensure_environment(source, repair=False, wheelhouse=None)
    assert python == source / ".venv/Scripts/python.exe"
    assert setup._owned_environment(source / ".venv")
    install = next(command for command in calls if "install" in command)
    for flag in ("--require-hashes", "--only-binary=:all:", "--no-deps", "--force-reinstall"):
        assert flag in install
    assert "https://pypi.org/simple" in install
    assert all("--extra" not in command for command in calls)


def test_owned_partial_environment_can_resume(source, monkeypatch):
    env = source / ".venv"
    env.mkdir()
    setup._mark_owned_environment(env)
    checks = iter([False, True])
    monkeypatch.setattr(setup, "_runtime_ready", lambda *args: next(checks))
    calls = []
    monkeypatch.setattr(setup, "_command", lambda python, arguments, **kwargs: calls.append(arguments) or True)
    setup._ensure_environment(source, repair=False, wheelhouse=source)
    install = next(command for command in calls if "install" in command)
    assert "--no-index" in install and "--find-links" in install
    assert "https://pypi.org/simple" not in install


def test_failed_install_preserves_owned_environment_for_retry(source, monkeypatch):
    monkeypatch.setattr(setup, "_runtime_ready", lambda *args: False)
    monkeypatch.setattr(setup, "_command", lambda python, arguments, **kwargs: "install" not in arguments)
    with pytest.raises(setup.SetupFailure) as failure:
        setup._ensure_environment(source, repair=False, wheelhouse=None)
    assert failure.value.code.value == "setup_dependencies_failed"
    assert setup._owned_environment(source / ".venv")


def test_prepare_tools_reuses_exact_checkout_selection(source, monkeypatch):
    tools = source / "runtime-tools/windows-x64"
    tools.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(setup, "verify_toolchain", lambda root, **kwargs: calls.append(("verify", root)))
    monkeypatch.setattr(setup, "run_offline_smoke", lambda root, **kwargs: calls.append(("smoke", root)))
    monkeypatch.setattr(setup, "install_toolchain", lambda *args, **kwargs: pytest.fail("existing tools overwritten"))
    setup._prepare_tools(source, source / ".venv/Scripts/python.exe", None)
    assert calls == [("verify", tools), ("smoke", tools)]


def test_tools_absent_are_installed_for_existing_launcher(source, monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "install_toolchain", lambda root, **kwargs: calls.append((root, kwargs)))
    python = source / ".venv/Scripts/python.exe"
    setup._prepare_tools(source, python, source)
    assert calls == [(source / "runtime-tools/windows-x64", {"python_executable": python, "artifact_cache": source})]


def test_broken_existing_tools_do_not_fall_back_or_overwrite(source, monkeypatch):
    tools = source / "runtime-tools/windows-x64"
    tools.mkdir(parents=True)
    sentinel = tools / "keep.txt"
    sentinel.write_text("SYNTHETIC-existing-tools")
    monkeypatch.setattr(setup, "verify_toolchain", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("SYNTHETIC-PRIVATE")))
    monkeypatch.setattr(setup, "install_toolchain", lambda *args, **kwargs: pytest.fail("bad bundle overwritten"))
    with pytest.raises(setup.SetupFailure) as failure:
        setup._prepare_tools(source, source / ".venv/Scripts/python.exe", None)
    assert failure.value.code.value == "setup_toolchain_failed"
    assert sentinel.read_text() == "SYNTHETIC-existing-tools"


def test_runtime_lock_must_be_exact_hashed_requirements(source):
    lock = source / "deployment/requirements.runtime.lock"
    assert setup._locked_requirements(lock) == {"example": "1.0"}
    for payload in ("example>=1.0", "--extra-index-url https://example.invalid", "example==1.0\n", "example==1.0 \\\n --hash=md5:abcd"):
        lock.write_text(payload)
        with pytest.raises(setup.SetupFailure):
            setup._locked_requirements(lock)


def test_interrupted_install_is_distinct_from_crash(source, monkeypatch, capsys):
    def cancel(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(setup, "_install", cancel)
    assert setup.main(["--yes"], repository_root=source) == 130
    assert json.loads(capsys.readouterr().err)["error_code"] == "setup_interrupted"

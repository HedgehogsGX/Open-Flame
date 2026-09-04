from __future__ import annotations

import importlib
import builtins
import io
import json
from pathlib import Path
import sys

import pytest

import video_download_control.local_app_cli as cli_module


def _launcher():
    return importlib.import_module("video_download_control.desktop_launcher")


def test_launcher_uses_checkout_tools_without_changing_business_root_or_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    repository = tmp_path / "checkout 中文 !"
    tools = repository / "runtime-tools" / "windows-x64"
    tools.mkdir(parents=True)
    working = tmp_path / "different cwd"
    working.mkdir()
    monkeypatch.chdir(working)
    seen = []
    monkeypatch.setattr(cli_module, "main", lambda args: seen.append(args) or 0)

    assert launcher.main(["--check", "--no-open-browser"], repository_root=repository) == 0
    assert seen == [["--allow-direct-network", "--tool-root", str(tools),
                     "--check", "--no-open-browser"]]
    assert "--app-root" not in seen[0]
    assert Path.cwd() == working


def test_explicit_tools_override_checkout_default_and_arguments_are_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    (tmp_path / "runtime-tools/windows-x64").mkdir(parents=True)
    supplied = ["--tool-root", str(tmp_path / "explicit tools"), "--cookie-config",
                str(tmp_path / "private config.json"), "--port", "8123"]
    seen = []
    monkeypatch.setattr(cli_module, "main", lambda args: seen.append(args) or 0)
    assert launcher.main(supplied, repository_root=tmp_path) == 0
    # argparse resolves repeated single-value options to the last explicit value.
    parsed = cli_module.build_parser().parse_args(seen[0])
    assert parsed.tool_root == tmp_path / "explicit tools"
    assert parsed.cookie_config == tmp_path / "private config.json"
    assert parsed.port == 8123
    assert seen[0][-len(supplied):] == supplied


def test_missing_checkout_tools_preserves_existing_local_app_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    seen = []
    monkeypatch.setattr(cli_module, "main", lambda args: seen.append(args) or 0)
    assert launcher.main([], repository_root=tmp_path) == 0
    assert seen == [["--allow-direct-network"]]


def test_bad_existing_checkout_tools_are_not_silently_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    candidate = tmp_path / "runtime-tools/windows-x64"
    candidate.parent.mkdir()
    candidate.write_text("synthetic invalid tool directory", encoding="utf-8")
    seen = []
    monkeypatch.setattr(cli_module, "main", lambda args: seen.append(args) or 0)
    assert launcher.main([], repository_root=tmp_path) == 0
    assert seen == [["--allow-direct-network", "--tool-root", str(candidate)]]


@pytest.mark.skipif(sys.platform != "win32", reason="local diagnostic persistence is Windows-only")
def test_dependency_import_failure_is_persisted_without_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = _launcher()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def missing(*args, **kwargs):
        raise ImportError("SYNTHETIC-PRIVATE-IMPORT-MARKER Cookie=do-not-emit")

    monkeypatch.setattr(launcher, "import_module", missing)
    assert launcher.main([], repository_root=tmp_path) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["error_code"] == "local_dependencies_unavailable"
    assert payload["diagnostic_status"] == "saved"
    assert "SYNTHETIC-PRIVATE" not in captured.err
    assert "Traceback" not in captured.err
    assert list((tmp_path / "Open-Flame/diagnostics").glob("*.jsonl"))


def test_launcher_preserves_help_exit_without_failure_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def help_exit(args):
        raise SystemExit(0)

    monkeypatch.setattr(cli_module, "main", help_exit)
    with pytest.raises(SystemExit) as result:
        launcher.main(["--help"], repository_root=tmp_path)
    assert result.value.code == 0
    assert not (tmp_path / "Open-Flame/diagnostics").exists()


def test_launcher_cancellation_is_not_logged_as_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def canceled(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "main", canceled)
    assert launcher.main([], repository_root=tmp_path) == 130
    assert not (tmp_path / "Open-Flame/diagnostics").exists()


def test_dependency_import_cancellation_uses_the_same_normal_cancel_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def canceled(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "import_module", canceled)
    try:
        result = launcher.main([], repository_root=tmp_path)
    except KeyboardInterrupt:
        pytest.fail("dependency import cancellation escaped the normal launcher boundary")
    assert result == 130
    assert not (tmp_path / "Open-Flame/diagnostics").exists()


def test_english_windows_pipe_encoding_does_not_prevent_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    seen = []
    monkeypatch.setattr(cli_module, "main", lambda args: seen.append(args) or 0)
    with io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as stream:
        with monkeypatch.context() as scoped:
            scoped.setattr(sys, "stdout", stream)
            assert launcher.main([], repository_root=tmp_path) == 0
    assert seen == [["--allow-direct-network"]]
    assert not (tmp_path / "Open-Flame/diagnostics").exists()


def test_bootstrap_import_has_no_launcher_or_business_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    import runpy

    # Windows spawn loads the script with __mp_main__, not __main__.
    namespace = runpy.run_path(str(repository / "start_open_flame.py"), run_name="__mp_main__")
    assert callable(namespace["main"])
    assert not (tmp_path / "Open-Flame").exists()


@pytest.mark.parametrize("failure", [ImportError, KeyboardInterrupt])
def test_bootstrap_missing_code_or_import_cancellation_is_safe_in_english_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException],
) -> None:
    import runpy

    repository = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    namespace = runpy.run_path(str(repository / "start_open_flame.py"), run_name="__mp_main__")
    real_import = builtins.__import__

    def fail_launcher(name, *args, **kwargs):
        if name == "video_download_control.desktop_launcher":
            raise failure("SYNTHETIC-PRIVATE-BOOTSTRAP-MARKER")
        return real_import(name, *args, **kwargs)

    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding="cp1252", errors="strict") as stream:
        with monkeypatch.context() as scoped:
            scoped.setattr(builtins, "__import__", fail_launcher)
            scoped.setattr(sys, "stderr", stream)
            result = namespace["main"]()
            stream.flush()
            emitted = buffer.getvalue()
    if failure is KeyboardInterrupt:
        assert result == 130 and emitted == b""
    else:
        assert result == 2
        payload = json.loads(emitted)
        assert payload["error_code"] == "local_launcher_unavailable"
        assert payload["diagnostic_status"] == "unavailable"
    assert b"SYNTHETIC-PRIVATE" not in emitted
    assert not (tmp_path / "Open-Flame").exists()

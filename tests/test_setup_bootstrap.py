"""Source setup entry checks using synthetic modules, never the real installer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

from video_download_control.windows_job import WindowsKillOnCloseJob


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "SYNTHETIC-PRIVATE-SETUP-ARGUMENT"
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows setup CMD")
_OWNED_COMMAND_HELPER = (
    "import subprocess,sys; ready=sys.stdin.buffer.read(1); "
    "sys.exit(71 if ready != b'G' else subprocess.call("
    "sys.argv[1],stdin=subprocess.DEVNULL,shell=False,"
    "creationflags=subprocess.CREATE_NO_WINDOW))"
)


def _source_fixture(tmp_path: Path, module: str | None) -> Path:
    source = ROOT / "setup_open_flame.py"
    if not source.is_file():
        pytest.fail("root source setup bootstrap is not implemented", pytrace=False)
    repository = tmp_path / "安装 源码!"
    repository.mkdir()
    shutil.copyfile(source, repository / source.name)
    package = repository / "src/video_download_control"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    if module is not None:
        (package / "source_setup.py").write_text(module, encoding="utf-8")
    return repository


def _python_run(
    repository: Path, cwd: Path, arguments: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [sys.executable, "-I", str(repository / "setup_open_flame.py"), *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("synthetic setup bootstrap exceeded its deadline", pytrace=False)


def test_setup_bootstrap_uses_own_repository_and_forwards_arguments(tmp_path: Path) -> None:
    arguments = ("--yes", "--repair", "--wheelhouse", PRIVATE)
    repository = _source_fixture(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\n"
        "def main(*, repository_root):\n"
        "    expected = Path(__file__).resolve().parents[2]\n"
        "    print(json.dumps({'own_repository': repository_root == expected, "
        "'arguments': sys.argv[1:], 'isolated': sys.flags.isolated}))\n"
        "    return 0\n",
    )
    cwd = tmp_path / "外部 目录!"
    cwd.mkdir()
    result = _python_run(repository, cwd, arguments)
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "own_repository": True, "arguments": list(arguments), "isolated": 1,
    }
    assert not (repository / ".venv").exists()


def _copy_cmd(repository: Path) -> None:
    source = ROOT / "Setup-Open-Flame.cmd"
    if not source.is_file():
        pytest.fail("root setup CMD is not implemented", pytrace=False)
    shutil.copyfile(source, repository / source.name)


def _run_cmd(
    repository: Path, cwd: Path, search_path: str, arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    values = (str(repository / "Setup-Open-Flame.cmd"), *arguments)
    if any(any(character in value for character in ('"', "%", "\r", "\n")) for value in values):
        pytest.fail("invalid synthetic CMD argument", pytrace=False)
    command_processor = Path(os.environ["SYSTEMROOT"]) / "System32/cmd.exe"
    command = f'"{command_processor}" /d /v:off /s /c "' + " ".join(
        f'"{value}"' for value in values
    ) + '"'
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() not in {"PATH", "PYTHONPATH", "VIRTUAL_ENV"}
        and not key.upper().startswith("VDC_")
    }
    environment["PATH"] = search_path
    with WindowsKillOnCloseJob() as ownership:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _OWNED_COMMAND_HELPER, command],
            cwd=cwd, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            ownership.assign_pid(process.pid)
            try:
                stdout, stderr = process.communicate(input=b"G", timeout=30)
            except subprocess.TimeoutExpired:
                ownership.close()
                process.communicate(timeout=10)
                pytest.fail("owned synthetic setup CMD exceeded its deadline", pytrace=False)
        finally:
            ownership.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    return subprocess.CompletedProcess(
        "owned-synthetic-setup", process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


@WINDOWS_ONLY
@pytest.mark.parametrize("cwd_has_launcher", (False, True))
def test_setup_cmd_fallback_python_forwards_unicode_paths_without_using_checkout_venv(
    tmp_path: Path, cwd_has_launcher: bool,
) -> None:
    interpreter_root = tmp_path / "test-python"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(interpreter_root)
    interpreter = interpreter_root / "Scripts/python.exe"
    arguments = (
        "--yes", "--repair", "--wheelhouse", str(tmp_path / "离线 wheels!"),
        "--artifact-cache", str(tmp_path / "工具 cache!"),
    )
    search_path = str(interpreter.parent)
    repository = _source_fixture(
        tmp_path,
        "import json,os,sys\nfrom pathlib import Path\n"
        "def main(*, repository_root):\n"
        "    print(json.dumps({"
        "'own_repository': repository_root == Path(__file__).resolve().parents[2], "
        f"'arguments_ok': sys.argv[1:] == {list(arguments)!r}, "
        f"'fallback_python': Path(sys.executable) == Path({str(interpreter)!r}), "
        f"'path_unchanged': os.environ.get('PATH') == {search_path!r}, "
        "'isolated': sys.flags.isolated, "
        "'automatic_install_disabled': os.environ.get('PYTHON_MANAGER_AUTOMATIC_INSTALL') == 'false'"
        "}))\n    return 0\n",
    )
    _copy_cmd(repository)
    cwd = tmp_path / "外部 目录!"
    cwd.mkdir()
    if cwd_has_launcher:
        launcher = shutil.which("py.exe")
        if launcher is None or not Path(launcher).is_file():
            pytest.skip("CWD-shadowing check requires an installed standalone py launcher")
        shutil.copyfile(launcher, cwd / "py.exe")
    result = _run_cmd(repository, cwd, search_path, arguments)
    assert result.returncode == 0
    assert result.stderr == ""
    payloads = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert payloads == [{
        "own_repository": True, "arguments_ok": True, "fallback_python": True,
        "path_unchanged": True, "isolated": 1, "automatic_install_disabled": True,
    }]
    assert not (repository / ".venv").exists()


@pytest.mark.parametrize("module", (None, f"raise RuntimeError({PRIVATE!r})\n"))
def test_setup_bootstrap_import_failure_is_fixed_ascii_json(
    tmp_path: Path, module: str | None,
) -> None:
    repository = _source_fixture(tmp_path, module)
    result = _python_run(repository, tmp_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.isascii()
    assert PRIVATE not in result.stderr
    assert "Traceback" not in result.stderr
    assert str(repository) not in result.stderr
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "setup_launcher_unavailable"
    assert payload["diagnostic_status"] == "unavailable"
    assert payload["log_status"] == "not_started"
    assert not (repository / ".venv").exists()


@pytest.mark.parametrize(
    "module",
    (
        f"raise KeyboardInterrupt({PRIVATE!r})\n",
        "def main(*, repository_root):\n    raise KeyboardInterrupt\n",
    ),
)
def test_setup_bootstrap_cancellation_is_exit_130_without_diagnostic(
    tmp_path: Path, module: str,
) -> None:
    repository = _source_fixture(tmp_path, module)
    result = _python_run(repository, tmp_path)
    assert result.returncode == 130
    assert result.stdout == ""
    assert result.stderr == ""
    assert not (repository / ".venv").exists()


def test_setup_bootstrap_spawn_import_does_not_invoke_setup(tmp_path: Path) -> None:
    repository = _source_fixture(tmp_path, "raise RuntimeError('must not import setup')\n")
    result = subprocess.run(
        [
            sys.executable, "-I", "-c",
            "import runpy,sys; runpy.run_path(sys.argv[1], run_name='__mp_main__')",
            str(repository / "setup_open_flame.py"),
        ],
        cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True,
        encoding="utf-8", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@WINDOWS_ONLY
def test_setup_cmd_missing_python_has_fixed_guidance_and_no_diagnostic(tmp_path: Path) -> None:
    repository = _source_fixture(tmp_path, "raise AssertionError('must not run')\n")
    _copy_cmd(repository)
    empty_path = tmp_path / "empty-search-path"
    empty_path.mkdir()
    result = _run_cmd(repository, tmp_path, str(empty_path), ("--help",))
    assert result.returncode == 2
    assert result.stderr == ""
    assert "CPython 3.12" in result.stdout
    assert "No diagnostic was saved" in result.stdout
    assert "README.md" in result.stdout
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_setup_cmd_prefers_supported_py_launcher_over_python_and_help_does_not_pause(
    tmp_path: Path,
) -> None:
    launcher_path = shutil.which("py.exe")
    if launcher_path is None or not Path(launcher_path).is_file():
        pytest.skip("priority check requires an installed standalone py launcher")
    interpreter_root = tmp_path / "fallback-python"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(interpreter_root)
    fallback = interpreter_root / "Scripts/python.exe"
    launcher_directory = tmp_path / "launcher-bin"
    launcher_directory.mkdir()
    shutil.copyfile(launcher_path, launcher_directory / "py.exe")
    repository = _source_fixture(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\n"
        "def main(*, repository_root):\n"
        "    print(json.dumps({"
        f"'used_fallback': Path(sys.executable) == Path({str(fallback)!r}), "
        "'help_forwarded': sys.argv[1:] == ['--help'], 'isolated': sys.flags.isolated"
        "}))\n    return 0\n",
    )
    _copy_cmd(repository)
    result = _run_cmd(
        repository, tmp_path,
        os.pathsep.join((str(launcher_directory), str(fallback.parent))),
        ("--help",),
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "used_fallback": False, "help_forwarded": True, "isolated": 1,
    }
    assert not (repository / ".venv").exists()


@WINDOWS_ONLY
def test_setup_cmd_user_cancellation_does_not_print_error_or_pause(tmp_path: Path) -> None:
    interpreter_root = tmp_path / "test-python"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(interpreter_root)
    repository = _source_fixture(
        tmp_path, "def main(*, repository_root):\n    return 130\n",
    )
    _copy_cmd(repository)
    result = _run_cmd(repository, tmp_path, str(interpreter_root / "Scripts"), ())
    assert result.returncode == 130
    assert result.stdout == result.stderr == ""
    assert not (repository / ".venv").exists()

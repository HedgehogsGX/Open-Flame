"""Real, offline Windows process checks for the source install command boundary."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from video_download_control import source_setup
from video_download_control.subprocess_runner import SecureSubprocessRunner
from video_download_control.windows_job import WindowsKillOnCloseJob


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows source setup")
PRIVATE = "SYNTHETIC-PRIVATE-INSTALL-OUTPUT"
_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class _HeldProcesses:
    """Keep stable handles, so exit assertions never query recycled PID numbers."""

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handles: list[int] = []

    def hold(self, pid: int) -> None:
        handle = self.kernel.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED_INFORMATION, False, pid)
        assert handle, "the owned process must still exist at its handshake"
        self.handles.append(handle)
        assert self.kernel.WaitForSingleObject(handle, 0) == 258

    def assert_all_exited(self) -> None:
        assert len(self.handles) >= 2
        for handle in self.handles:
            assert self.kernel.WaitForSingleObject(handle, 5000) == 0
            exit_code = wintypes.DWORD()
            assert self.kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            assert exit_code.value != _STILL_ACTIVE

    def close(self) -> None:
        for handle in self.handles:
            self.kernel.CloseHandle(handle)
        self.handles.clear()


@pytest.mark.parametrize("use_venv_python", (False, True))
def test_command_keyboard_interrupt_preserves_exception_and_reaps_owned_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    use_venv_python: bool,
) -> None:
    python = Path(sys._base_executable)
    if use_venv_python:
        environment = tmp_path / "cancel-environment"
        assert source_setup._command(
            python, ("-m", "venv", "--without-pip", str(environment)),
            repository=tmp_path, timeout=30,
        )
        python = environment / "Scripts" / "python.exe"
    release = tmp_path / "spawn-descendant"
    handshake = tmp_path / "tree.json"
    child_handshake = tmp_path / "child.json"
    spawn_handshake = tmp_path / "spawn.json"
    grandchild = (
        "import json, os, pathlib, sys, time; "
        f"print({PRIVATE!r}, file=sys.stderr, flush=True); "
        "target = pathlib.Path(sys.argv[1]); "
        "temporary = target.with_suffix('.pending'); "
        "temporary.write_text(json.dumps({'parent_pid': os.getppid(), 'grandchild_pid': os.getpid()})); "
        "temporary.replace(target); time.sleep(120)"
    )
    child = (
        "import json, os, pathlib, subprocess, sys, time\n"
        f"print({PRIVATE!r}, flush=True)\n"
        "release = pathlib.Path(sys.argv[1])\n"
        "child = pathlib.Path(sys.argv[3])\n"
        "temporary = child.with_suffix('.pending')\n"
        "temporary.write_text(json.dumps({'pid': os.getpid(), 'parent_pid': os.getppid()}))\n"
        "temporary.replace(child)\n"
        "while not release.exists(): time.sleep(0.01)\n"
        f"descendant = subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}, sys.argv[2]], "
        "stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "spawn = pathlib.Path(sys.argv[4])\n"
        "temporary = spawn.with_suffix('.pending')\n"
        "temporary.write_text(json.dumps({'pid': descendant.pid, 'parent_pid': os.getpid()}))\n"
        "temporary.replace(spawn)\n"
        "descendant.wait()\n"
    )
    interruption = KeyboardInterrupt("synthetic install cancellation")
    held = _HeldProcesses()
    observed = []
    deadline = time.monotonic() + 15
    # This Job is a test-only cleanup backstop. The actual runner must terminate
    # its tree and satisfy every handle assertion while the Job is still open.
    with WindowsKillOnCloseJob() as cleanup:
        def poll(process):
            if not observed:
                observed.append(process)
                held.hold(process.pid)
                cleanup.assign_pid(process.pid)
            assert time.monotonic() < deadline, "owned descendant handshake timed out"
            if child_handshake.exists() and not release.exists():
                child_record = json.loads(child_handshake.read_text())
                if child_record["pid"] != process.pid:
                    # A Windows venv uses a redirector process in front of the
                    # interpreter. Own and observe both before allowing spawn.
                    assert child_record["parent_pid"] == process.pid
                    held.hold(child_record["pid"])
                    cleanup.assign_pid(child_record["pid"])
                release.touch()
            if handshake.exists() and spawn_handshake.exists():
                child_record = json.loads(child_handshake.read_text())
                spawn_record = json.loads(spawn_handshake.read_text())
                assert spawn_record["parent_pid"] == child_record["pid"]
                held.hold(spawn_record["pid"])
                record = json.loads(handshake.read_text())
                if record["grandchild_pid"] != spawn_record["pid"]:
                    assert record["parent_pid"] == spawn_record["pid"]
                    held.hold(record["grandchild_pid"])
                else:
                    assert record["parent_pid"] == child_record["pid"]
                raise interruption
            return process.poll()

        monkeypatch.setattr(SecureSubprocessRunner, "_poll_process", staticmethod(poll))
        try:
            with pytest.raises(KeyboardInterrupt) as raised:
                source_setup._command(
                    python,
                    ("-c", child, str(release), str(handshake), str(child_handshake), str(spawn_handshake)),
                    repository=tmp_path,
                    timeout=30,
                )
            assert raised.value is interruption
            assert len(observed) == 1
            assert observed[0].poll() is not None
            assert len(held.handles) == (4 if use_venv_python else 2)
            held.assert_all_exited()
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == ""
        finally:
            held.close()


def test_command_real_pip_configuration_is_isolated_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = tmp_path / "probe-environment"
    report = tmp_path / "pip-isolation.json"
    private_config = tmp_path / "host-pip.ini"
    private_config.write_text(
        f"[global]\nindex-url = https://{PRIVATE}.invalid/simple\n", encoding="utf-8"
    )
    for key, value in {
        "PIP_CONFIG_FILE": str(private_config),
        "PIP_INDEX_URL": f"https://{PRIVATE}.invalid/simple",
        "PIP_EXTRA_INDEX_URL": f"https://{PRIVATE}.invalid/extra",
        "PIP_TARGET": str(tmp_path / "unexpected-target"),
        "PIP_PREFIX": str(tmp_path / "unexpected-prefix"),
        "PIP_USER": "1",
        "HTTP_PROXY": f"http://{PRIVATE}.invalid:1",
        "HTTPS_PROXY": f"http://{PRIVATE}.invalid:1",
        "PYTHONPATH": str(tmp_path / "unexpected-python-path"),
        "PYTHONHOME": str(tmp_path / "unexpected-python-home"),
    }.items():
        monkeypatch.setenv(key, value)
    assert source_setup._command(
        Path(sys.executable), ("-m", "venv", "--without-pip", str(environment)),
        repository=tmp_path, timeout=30,
    )
    python = environment / "Scripts" / "python.exe"
    # ensurepip uses CPython's bundled wheel; this installs only into this fresh
    # temporary venv and does not consult an index or change the caller's env.
    assert source_setup._command(
        python, ("-m", "ensurepip", "--upgrade"), repository=tmp_path, timeout=60,
    )
    site_config = environment / "pip.ini"
    site_config.write_text(private_config.read_text(encoding="utf-8"), encoding="utf-8")
    probe = (
        "import json, os, pathlib, sys, pip\n"
        "from pip._internal.configuration import Configuration\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "assert pathlib.Path(sys.prefix).resolve() == root.resolve()\n"
        "pathlib.Path(pip.__file__).resolve().relative_to(root.resolve())\n"
        "assert sys.flags.isolated == 1 and sys.flags.no_user_site == 1\n"
        "assert os.environ['PIP_CONFIG_FILE'] == os.devnull\n"
        "assert not any(key in os.environ for key in "
        "('PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER', "
        "'HTTP_PROXY', 'HTTPS_PROXY', 'PYTHONPATH', 'PYTHONHOME'))\n"
        "configuration = Configuration(isolated=True)\n"
        "configuration.load()\n"
        "assert dict(configuration.items()) == {}\n"
        f"print({PRIVATE!r}, flush=True)\n"
        f"print({PRIVATE!r}, file=sys.stderr, flush=True)\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({'isolated': True, 'pip_configuration': {}}))\n"
    )
    assert source_setup._command(
        python, ("-c", probe, str(environment), str(report)), repository=tmp_path, timeout=30,
    )
    assert json.loads(report.read_text()) == {"isolated": True, "pip_configuration": {}}
    assert PRIVATE in private_config.read_text(encoding="utf-8")
    assert PRIVATE in site_config.read_text(encoding="utf-8")
    assert not (tmp_path / "unexpected-target").exists()
    assert not (tmp_path / "unexpected-prefix").exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

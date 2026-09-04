from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from video_download_control.source_environment_lock import SourceEnvironmentBusy, source_environment_lock


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows shared source lock")


def _other_process_can_lock(root: Path, *, exclusive: bool) -> bool:
    code = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
from video_download_control.source_environment_lock import source_environment_lock, SourceEnvironmentBusy
try:
    with source_environment_lock(pathlib.Path(sys.argv[2]), exclusive=sys.argv[3] == 'write'):
        pass
except SourceEnvironmentBusy:
    raise SystemExit(2)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1] / "src"), str(root), "write" if exclusive else "read"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode in {0, 2}
    assert result.stderr == b""
    return result.returncode == 0


def test_launch_readers_coexist_but_installer_writer_cannot_enter(tmp_path):
    with source_environment_lock(tmp_path, exclusive=False):
        assert _other_process_can_lock(tmp_path, exclusive=False)
        assert not _other_process_can_lock(tmp_path, exclusive=True)
    assert _other_process_can_lock(tmp_path, exclusive=True)


def test_installer_excludes_launch_and_other_setup_until_cleanup(tmp_path):
    with pytest.raises(RuntimeError):
        with source_environment_lock(tmp_path, exclusive=True):
            assert not _other_process_can_lock(tmp_path, exclusive=False)
            assert not _other_process_can_lock(tmp_path, exclusive=True)
            raise RuntimeError("synthetic installation failure")
    assert _other_process_can_lock(tmp_path, exclusive=True)
    assert _other_process_can_lock(tmp_path, exclusive=False)


def test_lock_rejects_existing_nonempty_file_without_truncation(tmp_path):
    path = tmp_path / ".open-flame-setup.lock"
    path.write_bytes(b"SYNTHETIC-existing-file")
    with pytest.raises(SourceEnvironmentBusy):
        with source_environment_lock(tmp_path, exclusive=True):
            pytest.fail("invalid lock was accepted")
    assert path.read_bytes() == b"SYNTHETIC-existing-file"


def test_lock_rejects_hardlink_without_modifying_other_file(tmp_path):
    target = tmp_path / "other.txt"
    target.write_bytes(b"")
    os.link(target, tmp_path / ".open-flame-setup.lock")
    with pytest.raises(SourceEnvironmentBusy):
        with source_environment_lock(tmp_path, exclusive=False):
            pytest.fail("hardlink was accepted")
    assert target.read_bytes() == b""


@pytest.mark.parametrize(("entry", "exclusive"), (("setup_open_flame.py", False), ("start_open_flame.py", True)))
def test_real_bootstrap_reports_busy_before_running_installer_or_app(tmp_path, entry, exclusive):
    repository = Path(__file__).resolve().parents[1]
    profile = tmp_path / "profile"
    profile.mkdir()
    environment = dict(os.environ, LOCALAPPDATA=str(profile))
    with source_environment_lock(repository, exclusive=exclusive):
        completed = subprocess.run(
            [sys._base_executable, "-I", str(repository / entry), *(('--yes',) if entry.startswith('setup') else ())],
            cwd=tmp_path, env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    assert completed.returncode == 2
    records = [json.loads(line) for line in (profile / "Open-Flame/diagnostics/runtime-launch-diagnostics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["diagnostic_code"] == "setup_busy"
    assert not (profile / "Open-Flame/video-download-control").exists()

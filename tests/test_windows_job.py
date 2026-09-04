from __future__ import annotations

import os
import subprocess
import sys

import pytest

from video_download_control.windows_job import (
    WindowsJobError,
    WindowsKillOnCloseJob,
)


def test_windows_job_rejects_other_hosts_before_native_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")

    with pytest.raises(WindowsJobError, match="unavailable"):
        WindowsKillOnCloseJob()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are Windows-only")
def test_windows_job_close_terminates_an_assigned_owned_process_tree() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    job = WindowsKillOnCloseJob()
    try:
        job.assign_pid(process.pid)
        assert process.poll() is None
        job.close()
        process.wait(timeout=10)
        assert process.poll() is not None
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are Windows-only")
def test_windows_job_assignment_failure_is_generic() -> None:
    with WindowsKillOnCloseJob() as job:
        with pytest.raises(
            WindowsJobError,
            match="owned child process could not be assigned",
        ) as exc_info:
            job.assign_pid(0xFFFFFFFF)

    assert exc_info.value.__cause__ is None

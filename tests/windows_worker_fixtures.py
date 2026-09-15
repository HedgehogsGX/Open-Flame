"""Explicit Windows worker boundary for offline assembly/CLI contract tests.

Windows keeps its real byte lock. POSIX uses a scoped flock-backed stand-in;
this exercises lock ownership without claiming Windows runtime support there.
Host-rejection tests must not request this fixture.
"""
import os
import sys
from types import ModuleType

import pytest

from video_download_control import local_worker


@pytest.fixture
def windows_worker_contract(monkeypatch):
    monkeypatch.setattr(local_worker, "_require_windows_host", lambda: None)
    if os.name == "nt":
        return

    import fcntl

    native = ModuleType("msvcrt")
    native.LK_NBLCK = 2
    native.LK_UNLCK = 0

    def locking(descriptor, operation, length):
        assert length == 1
        assert operation in {native.LK_NBLCK, native.LK_UNLCK}
        flags = (
            fcntl.LOCK_UN if operation == native.LK_UNLCK
            else fcntl.LOCK_EX | fcntl.LOCK_NB
        )
        fcntl.flock(descriptor, flags)

    native.locking = locking
    monkeypatch.setitem(sys.modules, "msvcrt", native)

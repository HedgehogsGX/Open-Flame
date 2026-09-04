"""Minimal Windows Job Object ownership for local application children."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class WindowsJobError(RuntimeError):
    """A bounded public failure from the Windows process ownership boundary."""


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WindowsKillOnCloseJob:
    """Own processes and their descendants until this handle is closed."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsJobError("Windows process ownership is unavailable")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise WindowsJobError("Windows process ownership could not be initialized")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        configured = kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            kernel32.CloseHandle(handle)
            raise WindowsJobError("Windows process ownership could not be initialized")
        self._kernel32 = kernel32
        self._handle: int | None = handle

    def assign_pid(self, pid: int) -> None:
        """Assign one live owned process without retaining a raw process handle."""

        if (
            self._handle is None
            or isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or pid > 0xFFFFFFFF
        ):
            raise WindowsJobError("owned child process could not be assigned")
        process = self._kernel32.OpenProcess(
            _PROCESS_TERMINATE
            | _PROCESS_SET_QUOTA
            | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not process:
            raise WindowsJobError("owned child process could not be assigned")
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process):
                raise WindowsJobError("owned child process could not be assigned")
        finally:
            self._kernel32.CloseHandle(process)

    def terminate(self) -> None:
        """Terminate only processes assigned to this job."""

        if self._handle is not None and not self._kernel32.TerminateJobObject(
            self._handle, 1
        ):
            raise WindowsJobError("owned child processes could not be terminated")

    def close(self) -> None:
        """Close the kill-on-close handle exactly once."""

        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)

    def __enter__(self) -> WindowsKillOnCloseJob:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


__all__ = ["WindowsJobError", "WindowsKillOnCloseJob"]

"""Windows process primitives (kernel32/ntdll through ctypes). Imported only on Windows.

Every function has an explicit prototype: without `restype = HANDLE` ctypes would truncate
64-bit handles to a C int. A private WinDLL instance is used so the prototypes do not
leak into `ctypes.windll`, which other libraries share.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

if sys.platform != "win32":  # pragma: no cover - every importer checks the platform first
    raise ImportError("jarvis.core._win32 is only available on Windows")

from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87  # OpenProcess: no process with this id.
WAIT_TIMEOUT = 0x00000102

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# 100-ns intervals between 1601-01-01 (the FILETIME epoch) and 1970-01-01.
_FILETIME_UNIX_EPOCH = 116444736000000000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _prototype(library: Any, name: str, restype: Any, *argtypes: Any) -> Callable[..., Any]:
    function = getattr(library, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function  # type: ignore[no-any-return]


_OpenProcess = _prototype(
    _kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
)
_CloseHandle = _prototype(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_GetProcessTimes = _prototype(
    _kernel32, "GetProcessTimes", wintypes.BOOL, wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
)
_WaitForSingleObject = _prototype(
    _kernel32, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD
)
_TerminateProcess = _prototype(
    _kernel32, "TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT
)
_CreateJobObjectW = _prototype(
    _kernel32, "CreateJobObjectW", wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR
)
_SetInformationJobObject = _prototype(
    _kernel32, "SetInformationJobObject", wintypes.BOOL,
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
)
_QueryInformationJobObject = _prototype(
    _kernel32, "QueryInformationJobObject", wintypes.BOOL,
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD,
)
_AssignProcessToJobObject = _prototype(
    _kernel32, "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE
)
_TerminateJobObject = _prototype(
    _kernel32, "TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT
)
# NTSTATUS NtResumeProcess(HANDLE): resumes every thread of a process created suspended.
_NtResumeProcess = _prototype(_ntdll, "NtResumeProcess", ctypes.c_long, wintypes.HANDLE)


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _open_process(pid: int, access: int) -> Iterator[int | None]:
    """A process handle, or None if no process has this id. Always closed on exit."""
    handle = _OpenProcess(access, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_INVALID_PARAMETER:
            yield None
            return
        raise ctypes.WinError(error)
    try:
        yield handle
    finally:
        _CloseHandle(handle)


def _has_exited(handle: int) -> bool:
    # Signalled (WAIT_OBJECT_0) once the process has exited; WAIT_FAILED is treated alike.
    return bool(_WaitForSingleObject(handle, 0) != WAIT_TIMEOUT)


def process_creation_time(pid: int) -> float | None:
    """Creation time of a live process in seconds since the Unix epoch, else None.

    An exited process stays queryable while any handle to it is open, so exit is checked
    explicitly.
    """
    with _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE) as handle:
        if handle is None or _has_exited(handle):
            return None
        creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_), ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise _last_error()
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if ticks == 0:
            return None
        return (ticks - _FILETIME_UNIX_EPOCH) / 10_000_000


def process_is_running(pid: int) -> bool:
    try:
        with _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE) as handle:
            return handle is not None and not _has_exited(handle)
    except OSError as e:
        if getattr(e, "winerror", None) == ERROR_ACCESS_DENIED:
            return True  # Exists, but is protected or belongs to another user.
        raise


def resume_process(pid: int) -> None:
    """Resume a process that was created with CREATE_SUSPENDED."""
    with _open_process(pid, PROCESS_SUSPEND_RESUME) as handle:
        if handle is None:
            raise ProcessLookupError(pid)
        status = _NtResumeProcess(handle)
        if status < 0:
            raise OSError(
                f"NtResumeProcess failed for process {pid}: NTSTATUS {status & 0xFFFFFFFF:#010x}"
            )


def terminate_process(pid: int, exit_code: int = 1) -> None:
    with _open_process(pid, PROCESS_TERMINATE) as handle:
        if handle is not None and not _TerminateProcess(handle, exit_code):
            raise _last_error()


class JobObject:
    """A kill-on-close job: terminating or closing it ends every process in it.

    Processes started by a member join the job automatically, so a command's whole tree,
    including descendants whose parent already exited, is contained. If Jarvis itself
    dies, Windows closes the handle and the job's processes are killed with it.
    """

    def __init__(self) -> None:
        handle = _CreateJobObjectW(None, None)
        if not handle:
            raise _last_error()
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = _last_error()
            _CloseHandle(handle)
            raise error
        self._handle: int | None = handle

    def assign(self, pid: int) -> None:
        with _open_process(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE) as process:
            if process is None:
                raise ProcessLookupError(pid)
            if not _AssignProcessToJobObject(self._require_handle(), process):
                raise _last_error()

    def active_processes(self) -> int:
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not _QueryInformationJobObject(
            self._require_handle(), _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info), None,
        ):
            raise _last_error()
        return int(info.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle is not None and not _TerminateJobObject(self._handle, exit_code):
            raise _last_error()

    def close(self) -> None:
        """Close the job; any process still in it is killed (kill-on-close)."""
        handle, self._handle = self._handle, None
        if handle is not None:
            _CloseHandle(handle)

    @property
    def closed(self) -> bool:
        return self._handle is None

    def _require_handle(self) -> int:
        if self._handle is None:
            raise ValueError("job object is closed")
        return self._handle

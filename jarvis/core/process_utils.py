"""Native process queries and process-tree termination (Windows, macOS, Linux).

A process is identified by its PID *and* its creation time: PIDs are reused, so a PID
recorded before a crash may name an unrelated process after a restart. Every query here
uses the operating system's own process table:

- Windows: kernel32 (OpenProcess, GetProcessTimes, WaitForSingleObject).
- macOS: sysctl(KERN_PROC_PID), the same source `ps` uses (there is no /proc).
- Linux: /proc/<pid>/stat.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

LOG = logging.getLogger(__name__)

# A recorded and an observed creation time of the same process come from the same native
# source and agree exactly; the tolerance only absorbs float rounding. Linux reports start
# times in clock ticks (normally 10 ms).
CREATION_TIME_TOLERANCE = 0.05


def get_process_creation_time(pid: int) -> float | None:
    """Creation time (seconds since the Unix epoch) of a live process.

    None if there is no such process, it has exited (including a zombie awaiting
    reaping), or the time cannot be read.
    """
    if pid <= 0:
        return None
    try:
        return _creation_time(pid)
    except Exception as e:  # A query failure must never take recovery down.
        LOG.debug(f"Could not read the creation time of process {pid}: {e}")
        return None


def is_process_running(pid: int) -> bool:
    """True if a process with this PID exists and has not exited.

    A process that exists but cannot be inspected (another user's) counts as running.
    """
    if pid <= 0:
        return False
    try:
        return _is_running(pid)
    except Exception as e:
        LOG.debug(f"Could not query process {pid}: {e}")
        return False


def is_same_process(pid: int, recorded_creation_time: float) -> bool:
    """True if `pid` still names the live process created at `recorded_creation_time`."""
    actual = get_process_creation_time(pid)
    return actual is not None and abs(actual - recorded_creation_time) <= CREATION_TIME_TOLERANCE


def is_identity_unverifiable(pid: int, recorded_creation_time: float | None) -> bool:
    """True if a process with this PID may still be alive but cannot be identified.

    That is the case when no creation time was recorded for it, or when the operating
    system refuses to say when the live process was created (for example access denied
    to another user's or an elevated process on Windows). Such a record must not be
    discarded: dropping it would forget a process that might still be Jarvis's.

    False when the process is gone, or when its creation time can be read (then
    `is_same_process` decides whether it is the recorded one).
    """
    if pid <= 0:
        return False
    if recorded_creation_time is None:
        return is_process_running(pid)
    try:
        _creation_time(pid)
    except Exception as e:
        LOG.debug(f"Could not read the creation time of process {pid}: {e}")
        try:
            return _is_running(pid)
        except Exception:
            return True  # Cannot even tell whether it exists.
    return False


def terminate_process_tree(pid: int) -> None:
    """Forcefully terminate a process Jarvis started earlier, and its descendants.

    Used when no live handle to the process exists (orphan recovery). Callers must first
    establish that `pid` is that process (`is_same_process`).
    """
    if sys.platform == "win32":
        # /T: the whole tree (descendants still linked to it by parent PID).
        subprocess.run(
            [system32_program("taskkill.exe"), "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    # Jarvis starts each command as the leader of a new session, so its group id equals
    # its pid. Signal the group only in that case, and never Jarvis's own group.
    if pgid == pid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def system32_program(name: str) -> str:
    """Absolute path of a Windows system program, so PATH cannot substitute another."""
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(root, "System32", name)


# --- Platform implementations ------------------------------------------------------------

if sys.platform == "win32":
    from jarvis.core import _win32

    def _creation_time(pid: int) -> float | None:
        return _win32.process_creation_time(pid)

    def _is_running(pid: int) -> bool:
        return _win32.process_is_running(pid)

elif sys.platform == "darwin":
    import ctypes
    import struct

    _libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    _sysctl = _libc.sysctl
    _sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
    ]
    _sysctl.restype = ctypes.c_int

    _CTL_KERN, _KERN_PROC, _KERN_PROC_PID = 1, 14, 1
    # struct kinfo_proc begins with struct extern_proc, whose first member is a union that
    # holds `struct timeval p_starttime` (64-bit tv_sec, 32-bit tv_usec); p_stat is a char
    # at offset 36 and p_pid an int at offset 40 (<sys/proc.h>, LP64).
    _P_STARTTIME = struct.Struct("=qi")
    _P_STAT_OFFSET = 36
    _P_PID = struct.Struct("=i")
    _P_PID_OFFSET = 40
    _SZOMB = 5
    _KINFO_PROC_BUFFER = 1024  # sizeof(struct kinfo_proc) is 648

    def _kinfo_proc(pid: int) -> bytes | None:
        mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid)
        buffer = ctypes.create_string_buffer(_KINFO_PROC_BUFFER)
        size = ctypes.c_size_t(_KINFO_PROC_BUFFER)
        if _sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if size.value == 0:
            return None  # No such process.
        data = buffer.raw[: size.value]
        if (
            size.value < _P_PID_OFFSET + _P_PID.size
            or _P_PID.unpack_from(data, _P_PID_OFFSET)[0] != pid
        ):
            raise OSError(f"unexpected kinfo_proc layout for process {pid}")
        return data

    def _creation_time(pid: int) -> float | None:
        data = _kinfo_proc(pid)
        if data is None or data[_P_STAT_OFFSET] == _SZOMB:
            return None
        seconds, microseconds = _P_STARTTIME.unpack_from(data, 0)
        return float(seconds + microseconds / 1_000_000)

    def _is_running(pid: int) -> bool:
        data = _kinfo_proc(pid)
        return data is not None and data[_P_STAT_OFFSET] != _SZOMB

else:
    _boot_time: float | None = None

    def _read_stat(pid: int) -> list[str] | None:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
        except (FileNotFoundError, ProcessLookupError):
            return None
        # The fields after the command name, which is parenthesised and may contain spaces.
        return stat[stat.rindex(")") + 2 :].split()

    def _get_boot_time() -> float:
        global _boot_time
        if _boot_time is None:
            with open("/proc/stat", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("btime "):
                        _boot_time = float(line.split()[1])
                        break
                else:
                    raise OSError("btime not found in /proc/stat")
        return _boot_time

    def _creation_time(pid: int) -> float | None:
        fields = _read_stat(pid)
        if fields is None or fields[0] in ("Z", "X"):
            return None
        start_ticks = int(fields[19])  # Field 22 (starttime), counting from 1.
        return _get_boot_time() + start_ticks / os.sysconf("SC_CLK_TCK")

    def _is_running(pid: int) -> bool:
        fields = _read_stat(pid)
        return fields is not None and fields[0] not in ("Z", "X")

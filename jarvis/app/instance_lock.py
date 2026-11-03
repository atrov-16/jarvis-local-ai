"""One daemon per database.

Startup recovery terminates processes registered as running and marks running tasks
interrupted. A second daemon on the same database would do that to the first daemon's live
work before it even tried to bind its port, so the daemon takes an exclusive lock first.
The lock is an OS-level file lock: it is released when the process exits, however it dies,
so there is never a stale lock to clean up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO


class DaemonAlreadyRunningError(RuntimeError):
    """Another Jarvis daemon already holds the lock for this database."""


class InstanceLock:
    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_name(database_path.name + ".lock")
        self._file: IO[bytes] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = open(self.path, "a+b")  # noqa: SIM115 - held for the process lifetime
        try:
            _lock(file)
        except OSError:
            file.close()
            raise DaemonAlreadyRunningError(
                f"Another Jarvis daemon is already running against {self.path.parent} "
                f"(lock file {self.path}). Stop it first, or use a different database path."
            ) from None
        file.seek(0)
        file.truncate()
        file.write(str(os.getpid()).encode())
        file.flush()
        self._file = file

    def release(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            try:
                _unlock(file)
            finally:
                file.close()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


if sys.platform == "win32":
    import msvcrt

    def _lock(file: IO[bytes]) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(file: IO[bytes]) -> None:
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(file: IO[bytes]) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(file: IO[bytes]) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)

"""Service for detecting and recovering orphaned processes."""

from __future__ import annotations

import asyncio
import logging
import time

from jarvis.core import process_utils
from jarvis.core.process_registry import ProcessRegistryService

LOG = logging.getLogger(__name__)

# After termination, wait up to ~3 s for the process to disappear before giving up.
_EXIT_POLLS = 30
_EXIT_POLL_INTERVAL = 0.1


class OrphanRecoveryService:
    """Scans and cleans up processes that survived a daemon crash.

    A registered process is terminated only if its PID still names the *same* process: the
    creation time recorded when Jarvis started it must match the creation time the OS
    reports now. PIDs are reused, so a PID alone (or a command-line match) could name an
    unrelated process. A record without a creation time cannot be verified and is never
    terminated.
    """

    def __init__(self, registry: ProcessRegistryService) -> None:
        self._registry = registry

    async def recover(self) -> list[str]:
        """Scan the registry and terminate surviving orphaned processes.

        Returns:
            list[str]: A list of task IDs that had orphaned processes terminated.
        """
        terminated_tasks: list[str] = []
        processes = await self._registry.list_processes()
        for p in processes:
            if p["status"] == "running":
                pid = p["pid"]
                creation_time = p.get("creation_time")
                if self._is_orphaned(pid, p["command_display"], creation_time):
                    LOG.info(f"Terminating orphaned process {pid} for task {p['task_id']}")
                    # Off the event loop: tree termination and the wait for exit block.
                    if await asyncio.to_thread(self._terminate, pid):
                        terminated_tasks.append(p["task_id"])
                    else:
                        LOG.warning(
                            f"Process {pid} for task {p['task_id']} is still running after "
                            f"termination was attempted; keeping its registry record."
                        )
                        continue
                elif process_utils.is_identity_unverifiable(pid, creation_time):
                    # It may still be Jarvis's, but it cannot be told apart from an unrelated
                    # process, so it is neither terminated nor forgotten.
                    LOG.warning(
                        f"Not terminating process {pid} ({p['command_display']!r}): its "
                        f"identity cannot be verified (no recorded creation time, or the "
                        f"operating system refused to report it). Keeping its registry "
                        f"record; stop the process manually if it is still Jarvis's."
                    )
                    continue
                elif process_utils.is_process_running(pid):
                    LOG.info(f"PID {pid} now belongs to a different process (PID reused).")

            # Gone, proven to be a different process, or terminated: the record is stale.
            await self._registry.unregister_process(p["id"])

        return terminated_tasks

    def _is_orphaned(
        self, pid: int, expected_command: str, recorded_creation_time: float | None = None
    ) -> bool:
        """True if `pid` is still the process Jarvis registered (same PID and creation time).

        A record without a creation time can never be confirmed, so it is never an orphan
        to terminate.
        """
        if recorded_creation_time is None:
            return False
        return process_utils.is_same_process(pid, recorded_creation_time)

    def _terminate(self, pid: int) -> bool:
        """Terminate the orphaned process and its descendants.

        Returns True only once the process is verifiably gone; anything else leaves the
        registry record in place so the process is not forgotten.
        """
        try:
            process_utils.terminate_process_tree(pid)
        except Exception as e:
            LOG.warning(f"Failed to terminate process {pid}: {e}")
        for _ in range(_EXIT_POLLS):
            if not process_utils.is_process_running(pid):
                return True
            time.sleep(_EXIT_POLL_INTERVAL)
        return not process_utils.is_process_running(pid)

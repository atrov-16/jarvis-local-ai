"""Service for detecting and recovering orphaned processes."""

from __future__ import annotations

import logging

from jarvis.core import process_utils
from jarvis.core.process_registry import ProcessRegistryService

LOG = logging.getLogger(__name__)


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
                    self._terminate(pid)
                    terminated_tasks.append(p["task_id"])

            # Clean up the record
            await self._registry.unregister_process(p["id"])

        return terminated_tasks

    def _is_orphaned(
        self, pid: int, expected_command: str, recorded_creation_time: float | None = None
    ) -> bool:
        """True if `pid` is still the process Jarvis registered (same PID and creation time)."""
        if recorded_creation_time is None:
            if process_utils.is_process_running(pid):
                LOG.warning(
                    f"Not terminating process {pid} ({expected_command!r}): it was registered "
                    f"without a creation time, so it cannot be told apart from an unrelated "
                    f"process that reused the PID. Stop it manually if it is still Jarvis's."
                )
            return False

        if process_utils.is_same_process(pid, recorded_creation_time):
            return True
        if process_utils.is_process_running(pid):
            LOG.info(f"PID {pid} now belongs to a different process (PID reused). Skipping.")
        return False

    def _terminate(self, pid: int) -> None:
        """Terminate the orphaned process and its descendants."""
        try:
            process_utils.terminate_process_tree(pid)
        except Exception as e:
            LOG.warning(f"Failed to terminate process {pid}: {e}")

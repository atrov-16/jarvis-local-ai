"""Service for managing the lifecycle of external processes."""

from __future__ import annotations

import logging
from typing import Any

from jarvis.storage.unit_of_work import UnitOfWork

LOG = logging.getLogger(__name__)


class ProcessRegistryService:
    """Manages the registration and lifecycle of external processes."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def register_process(
        self, id: str, pid: int, task_id: str, command_display: str, creation_time: float | None = None
    ) -> None:
        """Register a new running process."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            await unit.repositories.processes.register(
                id=id,
                pid=pid,
                task_id=task_id,
                command_display=command_display,
                status="running",
                creation_time=creation_time,
            )
            LOG.debug(f"Registered process {pid} (ID: {id}) for task {task_id}")

    async def update_status(self, id: str, status: str) -> None:
        """Update the status of a registered process."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            await unit.repositories.processes.update_status(id, status)
            LOG.debug(f"Updated process {id} status to {status}")

    async def unregister_process(self, id: str) -> None:
        """Remove a process from the registry completely."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            await unit.repositories.processes.unregister(id)
            LOG.debug(f"Unregistered process {id}")

    async def list_processes(self) -> list[dict[str, Any]]:
        """List all registered processes."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            return await unit.repositories.processes.list_all()

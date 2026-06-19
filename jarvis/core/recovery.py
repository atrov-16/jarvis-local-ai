"""System recovery service for handling daemon restarts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from jarvis.core.event_bus import EventBus
from jarvis.core.events import Event
from jarvis.core.orphan_recovery import OrphanRecoveryService
from jarvis.storage.unit_of_work import UnitOfWork

LOG = logging.getLogger(__name__)


class SystemRecoveryService:
    """Orchestrates all recovery operations during daemon startup."""

    def __init__(
        self,
        uow: UnitOfWork,
        event_bus: EventBus,
        orphan_recovery: OrphanRecoveryService,
    ) -> None:
        self._uow = uow
        self._event_bus = event_bus
        self._orphan_recovery = orphan_recovery

    async def run_startup_recovery(self) -> None:
        """Run the full recovery sequence."""
        LOG.info("Starting system recovery sequence.")
        
        # 1. Recover orphaned external processes
        terminated_task_ids = await self._orphan_recovery.recover()
        
        # 2. Recover interrupted tasks
        await self._recover_tasks(terminated_task_ids)
        
        LOG.info("System recovery sequence completed.")

    async def _recover_tasks(self, terminated_task_ids: list[str]) -> None:
        """Recover tasks from interrupted states (e.g. after daemon crash)."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            
            # Find tasks that were active when the daemon crashed
            cursor = await unit.connection.execute(
                "SELECT id, status FROM tasks WHERE status IN ('running', 'planning')"
            )
            rows = await cursor.fetchall()
            
            now = datetime.now(UTC).isoformat()
            
            for row in rows:
                task_id = row["id"]
                previous_status = row["status"]
                
                # We now use "interrupted" since it's supported by the schema
                new_status = "interrupted"
                
                orphan_terminated = task_id in terminated_task_ids
                
                recovery_metadata = {
                    "recovery_reason": "daemon_restart",
                    "previous_status": previous_status,
                    "recovered_at": now,
                    "orphan_process_terminated": orphan_terminated
                }
                
                # Update task status and metadata
                await unit.repositories.tasks.update(
                    task_id, 
                    status=new_status,
                    # We could merge metadata, but we don't have a direct repository method for deep merge
                    # For now, we will add an event with the metadata
                )
                
                # Add task event
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    event_type="status_change",
                    message=f"Task recovered after daemon restart and marked as {new_status}.",
                    payload=recovery_metadata
                )
                
                # Publish event via EventBus
                await self._event_bus.publish(Event(
                    type="task.recovered",
                    payload={
                        "task_id": task_id,
                        **recovery_metadata
                    }
                ))
                
                LOG.info(f"Recovered task {task_id}: transitioned from {previous_status} to {new_status}.")

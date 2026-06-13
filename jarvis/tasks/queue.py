"""Task queue and background worker for single-threaded task execution."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jarvis.core.event_bus import EventBus
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.planner import Planner
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.base import ToolCategory

if TYPE_CHECKING:
    from jarvis.tasks.planner import PlannedTask

LOG = logging.getLogger(__name__)


class TaskQueue:
    def __init__(
        self, 
        uow: UnitOfWork, 
        event_bus: EventBus, 
        planner: Planner,
        tool_executor: ToolExecutor,
    ) -> None:
        self._uow = uow
        self._event_bus = event_bus
        self._planner = planner
        self._tool_executor = tool_executor
        self._worker_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the background worker and run recovery."""
        await self.run_recovery()
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())
            LOG.info("TaskQueue worker started.")

    async def stop(self) -> None:
        """Stop the background worker."""
        self._stop_event.set()
        if self._worker_task:
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
            self._worker_task = None
            LOG.info("TaskQueue worker stopped.")

    async def run_recovery(self) -> None:
        """Recover tasks from interrupted states (e.g. after daemon crash)."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            # Any 'running' task should be paused on restart
            cursor = await unit.connection.execute(
                "SELECT id FROM tasks WHERE status = 'running'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                task_id = row["id"]
                await unit.repositories.tasks.update(task_id, status="paused")
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    event_type="status_change",
                    message="Task paused due to daemon restart.",
                    payload={"old_status": "running", "new_status": "paused"}
                )
                LOG.info(f"Recovered task {task_id}: marked as paused.")

            # Any 'planning' task should be reverted to 'queued'
            cursor = await unit.connection.execute(
                "SELECT id FROM tasks WHERE status = 'planning'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                task_id = row["id"]
                await unit.repositories.tasks.update(task_id, status="queued")
                LOG.info(f"Recovered task {task_id}: reverted to queued.")

    async def _worker_loop(self) -> None:
        """The main loop for processing tasks sequentially."""
        while not self._stop_event.is_set():
            try:
                task_to_run = await self._get_next_task()
                if task_to_run:
                    await self._process_task(task_to_run)
                else:
                    await asyncio.sleep(1.0)
            except Exception as e:
                LOG.exception(f"Error in TaskQueue worker loop: {e}")
                await asyncio.sleep(5.0)

    async def _get_next_task(self) -> dict[str, Any] | None:
        """Fetch the next task that is ready to be planned or executed."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            # Prioritize 'queued' tasks
            cursor = await unit.connection.execute(
                """
                SELECT * FROM tasks 
                WHERE status = 'queued' 
                ORDER BY priority ASC, created_at ASC 
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            
            # Or tasks that are already approved and waiting to run
            # (Note: In V1, 'queued' means ready for planning OR ready for execution if steps exist)
            # Actually, let's look for 'running' if it was resume? 
            # No, 'queued' tasks are the only ones we auto-pick.
            return None

    async def _process_task(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        
        # Check if it needs planning
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            steps = await unit.repositories.tasks.list_steps(task_id)
            
        if not steps:
            await self._run_planning(task_id, task["user_request"])
            return

        # If it has steps, it must be approved before running
        # Actually, if status is 'queued' and it has steps, it means it's ready to run 
        # (after 'waiting_for_plan_approval' -> 'queued' transition)
        await self._run_execution(task_id)

    async def _run_planning(self, task_id: str, user_request: str) -> None:
        LOG.info(f"Starting planning for task {task_id}")
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            await unit.repositories.tasks.update(task_id, status="planning", claimed_at=datetime.now(UTC).isoformat())
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Planning started",
                payload={"status": "planning"}
            )

        try:
            plan: PlannedTask = await self._planner.create_plan(user_request)
            
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                # Create steps
                for i, step in enumerate(plan.steps):
                    await unit.repositories.tasks.insert_step(
                        task_id=task_id,
                        step_index=i,
                        title=step.title,
                        description=step.description,
                        tool_name=step.tool_name,
                        input_json=step.input_json,
                        requires_approval=step.requires_approval
                    )
                
                await unit.repositories.tasks.update(
                    task_id, 
                    status="waiting_for_plan_approval",
                    title=plan.title
                )
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    event_type="status_change",
                    message="Plan generated, waiting for approval",
                    payload={"status": "waiting_for_plan_approval", "title": plan.title}
                )
        except Exception as e:
            LOG.exception(f"Planning failed for task {task_id}: {e}")
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                await unit.repositories.tasks.update(task_id, status="failed")
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    event_type="status_change",
                    message=f"Planning failed: {e}",
                    payload={"status": "failed", "error": str(e)}
                )

    async def _run_execution(self, task_id: str) -> None:
        LOG.info(f"Starting execution for task {task_id}")
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            await unit.repositories.tasks.update(
                task_id, 
                status="running", 
                started_at=datetime.now(UTC).isoformat(),
                claimed_at=datetime.now(UTC).isoformat()
            )
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Execution started",
                payload={"status": "running"}
            )
            steps = await unit.repositories.tasks.list_steps(task_id)

        # Execute steps sequentially
        for step in steps:
            if self._stop_event.is_set():
                break
            
            if step["status"] == "completed":
                continue
                
            # Check for pause/cancellation
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                current_task = await unit.repositories.tasks.get(task_id)
                if current_task["status"] != "running":
                    LOG.info(f"Task {task_id} no longer running (status: {current_task['status']}), stopping execution.")
                    return

            # Execute the step
            success = await self._execute_step(task_id, step)
            if not success:
                # Task halted on first failure in V1
                return

        # Finish task
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            await unit.repositories.tasks.update(
                task_id, 
                status="completed", 
                completed_at=datetime.now(UTC).isoformat()
            )
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Task completed successfully",
                payload={"status": "completed"}
            )

    async def _execute_step(self, task_id: str, step: dict[str, Any]) -> bool:
        step_id = step["id"]
        LOG.info(f"Executing step {step_id}: {step['title']}")
        
        # 1. Resolve Tool Category and Workspace Policies
        tool_name = step["tool_name"]
        if not tool_name:
            # Native step with no tool, just mark completed
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                await unit.repositories.tasks.update_step(step_id, status="running")
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    step_id=step_id,
                    event_type="step_started",
                    message=f"Starting step: {step['title']}"
                )
                await unit.repositories.tasks.update_step(step_id, status="completed")
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    step_id=step_id,
                    event_type="step_completed",
                    message=f"Completed step: {step['title']}"
                )
            return True

        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            project_id = task["project_id"]
            
            # Get tool info
            try:
                tool = self._tool_executor._registry.get(tool_name)
                tool_category = tool.category
            except KeyError:
                await unit.repositories.tasks.update_step(step_id, status="failed", error=f"Tool not found: {tool_name}")
                return False

            # Check for Approval Requirement
            needs_approval = bool(step["requires_approval"])
            
            # If not already flagged by LLM, check workspace policy for mutating tools
            if not needs_approval and tool_category == ToolCategory.MUTATING and project_id:
                workspaces = await unit.repositories.projects.list_workspaces(project_id)
                for ws in workspaces:
                    if ws["write_policy"] == "approval_required":
                        needs_approval = True
                        break

            # If we need approval and haven't gotten it yet (indicated by not being in 'running' already)
            # Actually, the queue sets status to 'running' BEFORE calling _execute_step in the original code.
            # But we want to pause BEFORE marking it running if approval is needed.
            
            # Let's adjust: if it needs approval and status is not 'running', pause.
            if needs_approval and step["status"] != "running":
                await unit.repositories.tasks.update_step(step_id, status="waiting_for_approval")
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    step_id=step_id,
                    event_type="status_change",
                    message=f"Step '{step['title']}' requires approval.",
                    payload={"status": "waiting_for_approval"}
                )
                # Halt task execution for this task
                await unit.repositories.tasks.update(task_id, status="paused")
                return False

            # 2. Mark step as running (if not already)
            await unit.repositories.tasks.update_step(
                step_id, 
                status="running", 
                attempt_count=step["attempt_count"] + 1
            )
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                step_id=step_id,
                event_type="step_started",
                message=f"Starting step: {step['title']}"
            )

        # 3. Context Preparation
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            workspaces = []
            if project_id:
                workspaces = await unit.repositories.projects.list_workspaces(project_id)
            
            context = {
                "uow": self._uow,
                "project_id": project_id,
                "task_id": task_id,
                "current_task_id": task_id,
                "workspaces": workspaces
            }

        # 4. Actual Execution
        result = await self._tool_executor.execute_step(
            tool_name=tool_name,
            input_json=step["input_json"],
            **context
        )
        
        # 5. Result Handling
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            if result.success:
                await unit.repositories.tasks.update_step(
                    step_id, 
                    status="completed", 
                    output_json=json.dumps(result.data)
                )
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    step_id=step_id,
                    event_type="step_completed",
                    message=f"Completed step: {step['title']}"
                )
                return True
            else:
                await unit.repositories.tasks.update_step(
                    step_id, 
                    status="failed", 
                    error=result.error
                )
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    step_id=step_id,
                    event_type="step_failed",
                    message=f"Step failed: {result.error}",
                    payload={"error": result.error}
                )
                return False

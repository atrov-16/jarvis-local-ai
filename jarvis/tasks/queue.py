"""Task queue and background worker for single-threaded task execution."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jarvis.approvals.broker import ApprovalBroker
from jarvis.approvals.models import ApprovalActionType, ProposedAction, RiskLevel
from jarvis.core.event_bus import EventBus
from jarvis.memory.store import MemoryStore
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.planner import Planner
from jarvis.tools.executor import ToolExecutor

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
        approval_broker: ApprovalBroker,
    ) -> None:
        self._uow = uow
        self._event_bus = event_bus
        self._planner = planner
        self._tool_executor = tool_executor
        self._approval_broker = approval_broker
        self._worker_task: asyncio.Task[None] | None = None
        self._event_listener_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the background worker."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())
            LOG.info("TaskQueue worker started.")
        if self._event_listener_task is None:
            self._event_listener_task = asyncio.create_task(self._event_listener_loop())

    async def stop(self) -> None:
        """Stop the background worker."""
        self._stop_event.set()
        if self._worker_task:
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except TimeoutError:
                self._worker_task.cancel()
            self._worker_task = None
            
        if self._event_listener_task:
            self._event_listener_task.cancel()
            self._event_listener_task = None
            
        LOG.info("TaskQueue worker stopped.")

    async def _event_listener_loop(self) -> None:
        subscription = self._event_bus.subscribe(max_queue_size=1000)
        try:
            while not self._stop_event.is_set():
                try:
                    event = await subscription.get()
                    if event.type == "memory.retrieved":
                        memory_ids = event.payload.get("memory_ids", [])
                        if memory_ids:
                            try:
                                async with self._uow.begin() as unit:
                                    assert unit.repositories is not None
                                    await unit.repositories.memory.update_memory_access(memory_ids)
                            except Exception as e:
                                LOG.exception(f"Failed to update memory access: {e}")
                except Exception as e:
                    LOG.exception(f"Unexpected error in event listener loop: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            await subscription.close()




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
            assert unit.connection is not None
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
            task = await unit.repositories.tasks.get(task_id)
            project_id = str(task["project_id"]) if task and task.get("project_id") else None
            
            await unit.repositories.tasks.update(task_id, status="planning", claimed_at=datetime.now(UTC).isoformat())
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Planning started",
                payload={"status": "planning"}
            )
            
        memory_store = MemoryStore(self._uow)
        context_str, memory_ids = await memory_store.get_planner_context(user_request, project_id=project_id, task_id=task_id)
        
        if memory_ids:
            await self._event_bus.publish("memory.retrieved", {"memory_ids": memory_ids})

        try:
            plan: PlannedTask = await self._planner.create_plan(user_request, memory_context=context_str)
            
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
                
                # Create a centralized approval request for the plan
                proposed_plan = ProposedAction(
                    action_type=ApprovalActionType.PLAN,
                    summary=f"Approve plan: {plan.title}",
                    action_json=plan.model_dump_json(),
                    task_id=task_id,
                    risk_level=RiskLevel.MEDIUM
                )
                await self._approval_broker.create_request(proposed_plan, unit=unit)

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
        
        # 1. Ensure task is marked as 'running'
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if task["status"] != "running":
                await unit.repositories.tasks.update(
                    task_id, 
                    status="running", 
                    started_at=datetime.now(UTC).isoformat() if not task["started_at"] else task["started_at"],
                    claimed_at=datetime.now(UTC).isoformat()
                )
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    event_type="status_change",
                    message="Execution started/resumed",
                    payload={"status": "running"}
                )
            
            steps = await unit.repositories.tasks.list_steps(task_id)

        # 2. Execute steps sequentially
        for step in steps:
            if self._stop_event.is_set():
                break
            
            if step["status"] == "completed":
                continue
                
            # Check for external pause/cancellation
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                current_task = await unit.repositories.tasks.get(task_id)
                if current_task["status"] != "running":
                    LOG.info(f"Task {task_id} transitioned to {current_task['status']}, stopping execution loop.")
                    return

            # Execute the step
            success = await self._execute_step(task_id, step)
            if not success:
                # Task halted on failure or approval pause
                return

        # 3. Finish task
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            current_task = await unit.repositories.tasks.get(task_id)
            if current_task["status"] == "running":
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
                await self._event_bus.publish("task.completed", {"task_id": task_id, "project_id": current_task.get("project_id")})

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
            # Refresh step data to get the latest status (important for resumption)
            current_step = await unit.repositories.tasks.get_step(step_id)
            if not current_step:
                return False
            
            task = await unit.repositories.tasks.get(task_id)
            project_id = task["project_id"]
            
            # Get tool info
            try:
                tool = self._tool_executor._registry.get(tool_name)
                tool_category = tool.category
            except KeyError:
                await unit.repositories.tasks.update_step(step_id, status="failed", error=f"Tool not found: {tool_name}")
                return False

            # Unified Risk Classification
            proposed_action = ProposedAction(
                action_type=ApprovalActionType.TOOL,
                summary=f"Execute tool: {tool_name} ({step['title']})",
                action_json=step["input_json"] or "{}",
                task_id=task_id,
                step_id=step_id,
                context_id=project_id,
            )
            
            # Simple workspace check for now
            is_outside = False # Placeholder for path-level checks
            risk_level = await self._approval_broker.get_risk_level(proposed_action, tool_category, is_outside)
            
            # Always respect the planner's flag if it's set to True
            needs_approval = (risk_level != RiskLevel.LOW) or bool(current_step["requires_approval"])

            approval_request_id = None
            if needs_approval:
                # Check for existing approval request
                assert unit.connection is not None
                cursor = await unit.connection.execute(
                    "SELECT id, status FROM approval_requests WHERE step_id = ? ORDER BY created_at DESC LIMIT 1",
                    (step_id,)
                )
                approval_req = await cursor.fetchone()
                
                if not approval_req or approval_req["status"] != "approved":
                    if not approval_req or approval_req["status"] in ("denied", "expired", "cancelled"):
                        # Create new request if none exists or previous was not approved
                        proposed_action.risk_level = risk_level
                        await self._approval_broker.create_request(proposed_action, unit=unit)
                    
                    await unit.repositories.tasks.update_step(step_id, status="waiting_for_approval")
                    await unit.repositories.tasks.insert_event(
                        task_id=task_id,
                        step_id=step_id,
                        event_type="approval_requested",
                        message=f"Step '{step['title']}' requires approval (Risk: {risk_level.value}).",
                        payload={"status": "waiting_for_approval", "risk_level": risk_level.value},
                        severity="warning",
                        correlation_id=approval_request_id or (approval_req["id"] if approval_req else None)
                    )
                    # Halt task execution
                    if task["status"] == "running":
                        await unit.repositories.tasks.update(task_id, status="paused")
                    return False
                
                approval_request_id = approval_req["id"]

            # 2. Mark step as running (if not already)
            await unit.repositories.tasks.update_step(
                step_id, 
                status="running", 
                attempt_count=current_step["attempt_count"] + 1
            )
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                step_id=step_id,
                event_type="step_started",
                message=f"Starting step: {step['title']}",
                correlation_id=approval_request_id
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
            approval_request_id=approval_request_id,
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
                    message=f"Completed step: {step['title']}",
                    payload={"execution_time": result.execution_time},
                    correlation_id=approval_request_id
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
                    payload={"error": result.error, "timeout": result.timeout_occurred},
                    severity="error",
                    correlation_id=approval_request_id
                )
                return False

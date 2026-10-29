"""Task queue and background worker for single-threaded task execution.

Task lifecycle invariants enforced here:
- Every status change is a compare-and-set (`TaskRepository.transition`) from the states
  it is valid from, so a concurrent user action (cancel, deny) is never overwritten.
- A task becomes `completed` only when every step is `completed` or `skipped`.
- Shutdown never fabricates completion: an in-progress task becomes `interrupted`.
- Any unexpected error while planning or executing fails the task (and its running
  step) instead of leaving it stuck in `planning` or `running`.
- A step approval authorises exactly one execution attempt (see `ApprovalRepository.is_consumed`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from jarvis.approvals.broker import ApprovalBroker
from jarvis.approvals.models import ApprovalActionType, ProposedAction, RiskLevel
from jarvis.core.event_bus import EventBus
from jarvis.memory.store import MemoryStore
from jarvis.storage.unit_of_work import UnitOfWork, UnitOfWorkScope
from jarvis.tasks.planner import Planner
from jarvis.tools.executor import ToolExecutor

if TYPE_CHECKING:
    from jarvis.tasks.planner import PlannedTask

LOG = logging.getLogger(__name__)

# States in which the worker may still be acting on a task.
ACTIVE_STATUSES = ("queued", "planning", "running")
SUCCESSFUL_STEP_STATUSES = ("completed", "skipped")


class StepOutcome(Enum):
    COMPLETED = "completed"
    PAUSED = "paused"  # Waiting for approval; not a failure.
    FAILED = "failed"


class _TaskStateChanged(Exception):
    """The task left the expected state concurrently (e.g. cancelled); abandon the work."""


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
        """Stop the background worker.

        The worker gets a grace period to reach a step boundary (where it marks the task
        `interrupted`). If a step is still executing after that, the worker is cancelled;
        cancellation terminates any child process and also marks the task `interrupted`.
        """
        self._stop_event.set()
        if self._worker_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=5.0)
            except TimeoutError:
                self._worker_task.cancel()
                try:
                    await asyncio.wait_for(self._worker_task, timeout=5.0)
                except (asyncio.CancelledError, TimeoutError):
                    pass
                except Exception as e:
                    LOG.exception(f"TaskQueue worker failed during shutdown: {e}")
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
        """Fetch the next task that is ready to be planned or executed.

        Only 'queued' tasks are picked: they need planning (no steps yet) or have an
        approved plan / were explicitly resumed.
        """
        async with self._uow.begin() as unit:
            assert unit.connection is not None
            cursor = await unit.connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued'
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _process_task(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        try:
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                steps = await unit.repositories.tasks.list_steps(task_id)

            if not steps:
                await self._run_planning(task_id, task["user_request"])
            else:
                await self._run_execution(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Unexpected error (including failures persisting results): never leave the
            # task stuck in planning/running and never report success.
            LOG.exception(f"Unexpected error processing task {task_id}: {e}")
            await self._fail_task(task_id, f"Unexpected error: {e}")

    async def _run_planning(self, task_id: str, user_request: str) -> None:
        LOG.info(f"Starting planning for task {task_id}")
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            claimed = await unit.repositories.tasks.transition(
                task_id,
                "planning",
                from_statuses=("queued",),
                claimed_at=datetime.now(UTC).isoformat(),
            )
            if not claimed:
                LOG.info(f"Task {task_id} is no longer queued; skipping planning.")
                return
            task = await unit.repositories.tasks.get(task_id)
            project_id = str(task["project_id"]) if task and task.get("project_id") else None
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Planning started",
                payload={"status": "planning"}
            )

        try:
            memory_store = MemoryStore(self._uow)
            context_str, memory_ids = await memory_store.get_planner_context(
                user_request, project_id=project_id, task_id=task_id
            )
            if memory_ids:
                await self._event_bus.publish("memory.retrieved", {"memory_ids": memory_ids})

            plan: PlannedTask = await self._planner.create_plan(user_request, memory_context=context_str)
            if not plan.steps:
                raise ValueError("Planner returned a plan with no steps.")

            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                moved = await unit.repositories.tasks.transition(
                    task_id,
                    "waiting_for_plan_approval",
                    from_statuses=("planning",),
                    title=plan.title,
                )
                if not moved:
                    # Cancelled while planning: roll back, do not attach a plan.
                    raise _TaskStateChanged(task_id)

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
        except _TaskStateChanged:
            LOG.info(f"Task {task_id} changed state during planning; plan discarded.")
        except Exception as e:
            LOG.exception(f"Planning failed for task {task_id}: {e}")
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                moved = await unit.repositories.tasks.transition(
                    task_id, "failed", from_statuses=("planning",)
                )
                if moved:
                    await unit.repositories.tasks.insert_event(
                        task_id=task_id,
                        event_type="status_change",
                        message=f"Planning failed: {e}",
                        payload={"status": "failed", "error": str(e)},
                        severity="error",
                    )

    async def _run_execution(self, task_id: str) -> None:
        LOG.info(f"Starting execution for task {task_id}")

        # 1. Claim the task: queued -> running
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if task is None:
                return
            now = datetime.now(UTC).isoformat()
            claimed = await unit.repositories.tasks.transition(
                task_id,
                "running",
                from_statuses=("queued",),
                started_at=task["started_at"] or now,
                claimed_at=now,
            )
            if not claimed:
                LOG.info(f"Task {task_id} is no longer queued ({task['status']}); not executing.")
                return
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Execution started/resumed",
                payload={"status": "running"}
            )
            steps = await unit.repositories.tasks.list_steps(task_id)

        # 2. Execute steps sequentially
        for step in steps:
            if step["status"] in SUCCESSFUL_STEP_STATUSES:
                continue

            if self._stop_event.is_set():
                await self._interrupt_task(
                    task_id, f"Daemon shutdown before step '{step['title']}'"
                )
                return

            # Check for external pause/cancellation
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                current_task = await unit.repositories.tasks.get(task_id)
                if current_task is None or current_task["status"] != "running":
                    status = current_task["status"] if current_task else "deleted"
                    LOG.info(f"Task {task_id} transitioned to {status}, stopping execution loop.")
                    return

            try:
                outcome = await self._execute_step(task_id, step)
            except asyncio.CancelledError:
                await self._interrupt_task(
                    task_id, f"Daemon shutdown while step '{step['title']}' was executing"
                )
                raise
            if outcome is not StepOutcome.COMPLETED:
                # Failed (task already failed) or paused for approval.
                return

        # 3. Finish: only when every step actually succeeded.
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            steps = await unit.repositories.tasks.list_steps(task_id)
            incomplete = [s for s in steps if s["status"] not in SUCCESSFUL_STEP_STATUSES]
            if incomplete:
                raise RuntimeError(
                    f"Execution ended with {len(incomplete)} incomplete step(s); "
                    "refusing to complete."
                )
            completed = await unit.repositories.tasks.transition(
                task_id, "completed", from_statuses=("running",),
                completed_at=datetime.now(UTC).isoformat(),
            )
            if completed:
                await unit.repositories.tasks.insert_event(
                    task_id=task_id,
                    event_type="status_change",
                    message="Task completed successfully",
                    payload={"status": "completed"}
                )
            current_task = await unit.repositories.tasks.get(task_id)

        # Publish only after the completion is committed.
        if completed and current_task is not None:
            await self._event_bus.publish(
                "task.completed", {"task_id": task_id, "project_id": current_task.get("project_id")}
            )

    async def _execute_step(self, task_id: str, step: dict[str, Any]) -> StepOutcome:
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
            return StepOutcome.COMPLETED

        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            # Refresh step data to get the latest status (important for resumption)
            current_step = await unit.repositories.tasks.get_step(step_id)
            if not current_step or current_step["task_id"] != task_id:
                raise RuntimeError(f"Step {step_id} no longer exists for task {task_id}.")

            task = await unit.repositories.tasks.get(task_id)
            assert task is not None
            project_id = task["project_id"]

            # Get tool info
            try:
                tool = self._tool_executor._registry.get(tool_name)
                tool_category = tool.category
            except KeyError:
                error = f"Tool not found: {tool_name}"
                await self._record_step_failure(unit, task_id, step_id, error)
                return StepOutcome.FAILED

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
                approvals = unit.repositories.approvals
                approval_req = await approvals.get_latest_for_step(task_id, step_id, "tool")
                consumed = bool(
                    approval_req
                    and approval_req["status"] == "approved"
                    and await approvals.is_consumed(approval_req["id"])
                )

                if not approval_req or approval_req["status"] != "approved" or consumed:
                    correlation_id = approval_req["id"] if approval_req else None
                    if not approval_req or approval_req["status"] != "pending":
                        # None yet, or the last one was denied/expired/cancelled or already
                        # used for a previous attempt: this attempt needs fresh consent.
                        proposed_action.risk_level = risk_level
                        correlation_id = await self._approval_broker.create_request(
                            proposed_action, unit=unit
                        )

                    message = (
                        f"Step '{step['title']}' requires approval (Risk: {risk_level.value})."
                    )
                    if consumed:
                        message += " The previous approval was already used by an earlier attempt."
                    await unit.repositories.tasks.update_step(step_id, status="waiting_for_approval")
                    await unit.repositories.tasks.insert_event(
                        task_id=task_id,
                        step_id=step_id,
                        event_type="approval_requested",
                        message=message,
                        payload={"status": "waiting_for_approval", "risk_level": risk_level.value},
                        severity="warning",
                        correlation_id=correlation_id,
                    )
                    # Halt task execution
                    if await unit.repositories.tasks.transition(
                        task_id, "paused", from_statuses=("running",)
                    ):
                        await unit.repositories.tasks.insert_event(
                            task_id=task_id,
                            step_id=step_id,
                            event_type="status_change",
                            message="Task paused awaiting step approval",
                            payload={"status": "paused"},
                        )
                    return StepOutcome.PAUSED

                approval_request_id = approval_req["id"]

            # 2. Mark step as running. The step_started event carries the approval id; it is
            # written in this same transaction and marks the approval as consumed.
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
                return StepOutcome.COMPLETED

            await self._record_step_failure(
                unit,
                task_id,
                step_id,
                result.error or "Tool reported failure without an error message.",
                correlation_id=approval_request_id,
                timeout=result.timeout_occurred,
            )
            return StepOutcome.FAILED

    async def _record_step_failure(
        self,
        unit: UnitOfWorkScope,
        task_id: str,
        step_id: str,
        error: str,
        *,
        correlation_id: str | None = None,
        timeout: bool = False,
    ) -> None:
        """Expected, unsuccessful step outcome: fail the step and (if still running) the task."""
        assert unit.repositories is not None
        tasks = unit.repositories.tasks
        await tasks.update_step(step_id, status="failed", error=error)
        await tasks.insert_event(
            task_id=task_id,
            step_id=step_id,
            event_type="step_failed",
            message=f"Step failed: {error}",
            payload={"error": error, "timeout": timeout},
            severity="error",
            correlation_id=correlation_id,
        )
        # Conditional: a task the user cancelled meanwhile stays cancelled.
        if await tasks.transition(task_id, "failed", from_statuses=("running",)):
            await tasks.insert_event(
                task_id=task_id,
                step_id=step_id,
                event_type="status_change",
                message=f"Task failed due to step failure: {error}",
                payload={"status": "failed", "error": error},
                severity="error",
                correlation_id=correlation_id,
            )

    async def _fail_task(self, task_id: str, reason: str) -> None:
        """Unexpected error: fail any running step and the task if it is still active."""
        try:
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                tasks = unit.repositories.tasks
                failed_steps = await tasks.fail_running_steps(task_id, error=reason)
                if await tasks.transition(task_id, "failed", from_statuses=ACTIVE_STATUSES):
                    await tasks.insert_event(
                        task_id=task_id,
                        event_type="status_change",
                        message=f"Task failed: {reason}",
                        payload={"status": "failed", "error": reason, "failed_steps": failed_steps},
                        severity="error",
                    )
        except Exception as e:
            # Startup recovery will mark the task interrupted if this cannot be persisted.
            LOG.exception(f"Could not persist failure for task {task_id}: {e}")

    async def _interrupt_task(self, task_id: str, reason: str) -> None:
        """Daemon shutdown: record the task as interrupted (resumable), never completed."""
        try:
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                tasks = unit.repositories.tasks
                interrupted_steps = await tasks.fail_running_steps(
                    task_id,
                    error=f"Interrupted: {reason}. The action may have partially completed.",
                )
                if await tasks.transition(task_id, "interrupted", from_statuses=("running",)):
                    await tasks.insert_event(
                        task_id=task_id,
                        event_type="status_change",
                        message=f"Task interrupted: {reason}",
                        payload={
                            "status": "interrupted",
                            "reason": reason,
                            "interrupted_steps": interrupted_steps,
                        },
                        severity="warning",
                    )
        except Exception as e:
            LOG.exception(f"Could not persist interruption for task {task_id}: {e}")

"""Regression tests for task lifecycle integrity (Phase 12).

Each test drives TaskQueue._process_task directly against a disposable SQLite database,
so state transitions are deterministic and fully persisted.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import BaseModel

from jarvis.approvals.broker import ApprovalBroker
from jarvis.core.event_bus import EventBus
from jarvis.core.orphan_recovery import OrphanRecoveryService
from jarvis.core.recovery import SystemRecoveryService
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.planner import PlannedStep, PlannedTask, Planner
from jarvis.tasks.queue import TaskQueue
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry


class EmptyInput(BaseModel):
    pass


class ScriptedTool(BaseTool):
    """Tool whose behaviour is supplied by the test; records every execution."""

    def __init__(
        self,
        name: str,
        category: ToolCategory,
        behaviour: Callable[[], Awaitable[ToolResult]] | None = None,
    ) -> None:
        super().__init__(name=name, description=name, category=category)
        self.calls = 0
        self._behaviour = behaviour

    def get_input_schema(self) -> type[BaseModel]:
        return EmptyInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        if self._behaviour is None:
            return ToolResult(success=True, data={"ok": True})
        return await self._behaviour()


@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "lifecycle.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def broker(uow, bus):
    return ApprovalBroker(uow, bus)


@pytest.fixture
def planner():
    return MagicMock(spec=Planner)


@pytest.fixture
def queue(uow, bus, planner, registry, broker):
    return TaskQueue(uow, bus, planner, ToolExecutor(registry, broker), broker)


async def make_task(uow, steps, status="queued", step_statuses=None) -> str:
    """Insert a task (with optional pre-planned steps) bound to a fresh project."""
    async with uow.begin() as unit:
        repos = unit.repositories
        project_id = await repos.projects.insert(name=f"project-{uuid4()}")
        task_id = await repos.tasks.insert(
            title="Task", user_request="do it", project_id=project_id, status=status
        )
        for i, (title, tool_name) in enumerate(steps):
            await repos.tasks.insert_step(
                task_id=task_id,
                step_index=i,
                title=title,
                tool_name=tool_name,
                input_json="{}" if tool_name else None,
                status=(step_statuses or {}).get(i, "pending"),
            )
    return task_id


async def process(queue, uow, task_id):
    async with uow.begin() as unit:
        row = await unit.repositories.tasks.get(task_id)
    await queue._process_task(row)


async def snapshot(uow, task_id):
    async with uow.begin() as unit:
        task = await unit.repositories.tasks.get(task_id)
        steps = await unit.repositories.tasks.list_steps(task_id)
        events = await unit.repositories.tasks.list_events(task_id)
    return task, steps, events


def status_changes(events) -> list[str]:
    return [
        json.loads(e["payload_json"]).get("status")
        for e in events
        if e["event_type"] == "status_change"
    ]


def drain(subscription) -> list[str]:
    types = []
    while not subscription._queue.empty():
        types.append(subscription._queue.get_nowait().type)
    return types


# --- Success -----------------------------------------------------------------


async def test_single_step_task_completes(queue, uow, bus):
    sub = bus.subscribe()
    task_id = await make_task(uow, [("Think", None)])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "completed"
    assert task["completed_at"] is not None
    assert [s["status"] for s in steps] == ["completed"]
    assert status_changes(events) == ["running", "completed"]
    assert "task.completed" in drain(sub)


async def test_multi_step_task_completes(queue, uow, registry):
    reader = ScriptedTool("reader", ToolCategory.READ_ONLY)
    registry.register(reader)
    task_id = await make_task(
        uow, [("Read", "reader"), ("Read again", "reader"), ("Wrap up", None)]
    )
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "completed"
    assert [s["status"] for s in steps] == ["completed"] * 3
    assert reader.calls == 2
    assert status_changes(events).count("completed") == 1


# --- Failures ----------------------------------------------------------------


async def test_tool_failure_result_fails_task(queue, uow, registry, bus):
    sub = bus.subscribe()

    async def fail() -> ToolResult:
        return ToolResult(success=False, error="disk full")

    registry.register(ScriptedTool("reader", ToolCategory.READ_ONLY, fail))
    task_id = await make_task(uow, [("Read", "reader")])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    assert steps[0]["status"] == "failed"
    assert steps[0]["error"] == "disk full"
    assert status_changes(events)[-1] == "failed"
    assert "task.completed" not in drain(sub)


async def test_missing_tool_fails_task(queue, uow):
    task_id = await make_task(uow, [("Ghost", "no_such_tool")])
    await process(queue, uow, task_id)

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    assert steps[0]["status"] == "failed"
    assert "Tool not found" in steps[0]["error"]


async def test_unexpected_tool_exception_fails_task(queue, uow, registry):
    async def explode() -> ToolResult:
        raise RuntimeError("kaboom")

    registry.register(ScriptedTool("reader", ToolCategory.READ_ONLY, explode))
    task_id = await make_task(uow, [("Read", "reader")])
    await process(queue, uow, task_id)

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    assert steps[0]["status"] == "failed"
    assert "kaboom" in steps[0]["error"]


async def test_failure_persisting_result_fails_task_instead_of_sticking(queue, uow, registry):
    async def unserialisable() -> ToolResult:
        return ToolResult(success=True, data=object())  # json.dumps cannot persist this

    registry.register(ScriptedTool("reader", ToolCategory.READ_ONLY, unserialisable))
    task_id = await make_task(uow, [("Read", "reader"), ("Next", None)])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "failed"  # not stuck 'running', never 'completed'
    assert steps[0]["status"] == "failed"
    assert "Unexpected error" in steps[0]["error"]
    assert steps[1]["status"] == "pending"
    assert "completed" not in status_changes(events)


async def test_failure_after_earlier_step_completed(queue, uow, registry):
    async def fail() -> ToolResult:
        return ToolResult(success=False, error="second step broke")

    registry.register(ScriptedTool("ok", ToolCategory.READ_ONLY))
    registry.register(ScriptedTool("bad", ToolCategory.READ_ONLY, fail))
    task_id = await make_task(uow, [("One", "ok"), ("Two", "bad"), ("Three", "ok")])
    await process(queue, uow, task_id)

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    assert [s["status"] for s in steps] == ["completed", "failed", "pending"]


# --- Planning ----------------------------------------------------------------


async def test_planning_success_waits_for_plan_approval(queue, uow, planner):
    planner.create_plan = AsyncMock(
        return_value=PlannedTask(title="Plan", steps=[PlannedStep(title="S", description="d")])
    )
    task_id = await make_task(uow, [])
    await process(queue, uow, task_id)

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "waiting_for_plan_approval"
    assert len(steps) == 1
    async with uow.begin() as unit:
        plan_request = await unit.repositories.approvals.get_latest_for_task(task_id, "plan")
    assert plan_request is not None and plan_request["status"] == "pending"


async def test_planner_error_fails_task(queue, uow, planner):
    planner.create_plan = AsyncMock(side_effect=ValueError("bad JSON from model"))
    task_id = await make_task(uow, [])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    assert steps == []
    assert status_changes(events) == ["planning", "failed"]


async def test_memory_context_error_does_not_strand_planning(queue, uow, planner):
    planner.create_plan = AsyncMock()
    task_id = await make_task(uow, [])
    with patch(
        "jarvis.tasks.queue.MemoryStore.get_planner_context",
        AsyncMock(side_effect=RuntimeError("database is locked")),
    ):
        await process(queue, uow, task_id)

    task, _, _ = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    planner.create_plan.assert_not_called()


async def test_empty_plan_fails_instead_of_looping(queue, uow, planner):
    planner.create_plan = AsyncMock(return_value=PlannedTask(title="Nothing", steps=[]))
    task_id = await make_task(uow, [])
    await process(queue, uow, task_id)

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    assert steps == []


async def test_cancel_during_planning_discards_plan(queue, uow, planner):
    task_holder: dict[str, str] = {}

    async def plan_then_cancelled(*args, **kwargs):
        async with uow.begin() as unit:
            await unit.repositories.tasks.update(task_holder["id"], status="cancelled")
        return PlannedTask(title="Plan", steps=[PlannedStep(title="S", description="d")])

    planner.create_plan = AsyncMock(side_effect=plan_then_cancelled)
    task_holder["id"] = task_id = await make_task(uow, [])
    await process(queue, uow, task_id)

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "cancelled"
    assert steps == []


# --- Shutdown, interruption, cancellation ------------------------------------


async def test_graceful_shutdown_between_steps_interrupts_not_completes(queue, uow, registry, bus):
    sub = bus.subscribe()

    async def first_then_shutdown() -> ToolResult:
        queue._stop_event.set()  # daemon shutdown requested while step 1 runs
        return ToolResult(success=True, data="done")

    second = ScriptedTool("second", ToolCategory.READ_ONLY)
    registry.register(ScriptedTool("first", ToolCategory.READ_ONLY, first_then_shutdown))
    registry.register(second)
    task_id = await make_task(uow, [("One", "first"), ("Two", "second")])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "interrupted"
    assert task["completed_at"] is None
    assert [s["status"] for s in steps] == ["completed", "pending"]
    assert second.calls == 0
    assert "completed" not in status_changes(events)
    assert "task.completed" not in drain(sub)


async def test_interruption_during_execution_marks_task_interrupted(queue, uow, registry):
    started = asyncio.Event()

    async def long_running() -> ToolResult:
        started.set()
        await asyncio.sleep(60)
        return ToolResult(success=True)

    registry.register(ScriptedTool("slow", ToolCategory.READ_ONLY, long_running))
    task_id = await make_task(uow, [("Slow", "slow"), ("After", None)])
    runner = asyncio.create_task(process(queue, uow, task_id))
    await asyncio.wait_for(started.wait(), timeout=10)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "interrupted"
    assert steps[0]["status"] == "failed"
    assert "Interrupted" in steps[0]["error"]
    assert steps[1]["status"] == "pending"
    assert status_changes(events)[-1] == "interrupted"


async def test_queue_stop_during_long_step_interrupts_task(queue, uow, registry):
    started = asyncio.Event()

    async def long_running() -> ToolResult:
        started.set()
        await asyncio.sleep(60)
        return ToolResult(success=True)

    registry.register(ScriptedTool("slow", ToolCategory.READ_ONLY, long_running))
    task_id = await make_task(uow, [("Slow", "slow")])
    await queue.start()
    await asyncio.wait_for(started.wait(), timeout=10)
    await queue.stop()  # grace period elapses, then the worker is cancelled

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "interrupted"
    assert steps[0]["status"] == "failed"


async def test_cancellation_stops_execution_without_completing(queue, uow, registry):
    holder: dict[str, str] = {}

    async def cancel_mid_task() -> ToolResult:
        async with uow.begin() as unit:
            await unit.repositories.tasks.update(holder["id"], status="cancelled")
        return ToolResult(success=True)

    second = ScriptedTool("second", ToolCategory.READ_ONLY)
    registry.register(ScriptedTool("first", ToolCategory.READ_ONLY, cancel_mid_task))
    registry.register(second)
    holder["id"] = task_id = await make_task(uow, [("One", "first"), ("Two", "second")])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "cancelled"
    assert [s["status"] for s in steps] == ["completed", "pending"]
    assert second.calls == 0
    assert "completed" not in status_changes(events)


async def test_failure_after_cancellation_keeps_task_cancelled(queue, uow, registry):
    holder: dict[str, str] = {}

    async def cancel_then_fail() -> ToolResult:
        async with uow.begin() as unit:
            await unit.repositories.tasks.update(holder["id"], status="cancelled")
        return ToolResult(success=False, error="failed after cancel")

    registry.register(ScriptedTool("first", ToolCategory.READ_ONLY, cancel_then_fail))
    holder["id"] = task_id = await make_task(uow, [("One", "first")])
    await process(queue, uow, task_id)

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "cancelled"  # the user's cancellation is not overwritten
    assert steps[0]["status"] == "failed"


async def test_completed_task_is_never_restarted(queue, uow, registry):
    tool = ScriptedTool("writer", ToolCategory.MUTATING)
    registry.register(tool)
    task_id = await make_task(
        uow, [("Write", "writer")], status="completed", step_statuses={0: "completed"}
    )
    _, _, events_before = await snapshot(uow, task_id)
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "completed"
    assert tool.calls == 0
    assert len(events) == len(events_before)


# --- Approvals -----------------------------------------------------------------


async def test_approval_pause_is_not_a_failure(queue, uow, registry):
    tool = ScriptedTool("writer", ToolCategory.MUTATING)
    registry.register(tool)
    task_id = await make_task(uow, [("Write", "writer")])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "paused"
    assert steps[0]["status"] == "waiting_for_approval"
    assert steps[0]["error"] is None
    assert tool.calls == 0
    assert status_changes(events) == ["running", "paused"]
    async with uow.begin() as unit:
        request = await unit.repositories.approvals.get_latest_for_step(task_id, steps[0]["id"])
    assert request["status"] == "pending"


async def test_approval_authorises_only_one_execution_attempt(queue, uow, registry, broker):
    async def fail() -> ToolResult:
        return ToolResult(success=False, error="transient")

    tool = ScriptedTool("writer", ToolCategory.MUTATING, fail)
    registry.register(tool)
    task_id = await make_task(uow, [("Write", "writer")])

    # 1. Pauses for approval; approve it the way the API does.
    await process(queue, uow, task_id)
    _, steps, _ = await snapshot(uow, task_id)
    step_id = steps[0]["id"]
    async with uow.begin() as unit:
        first = await unit.repositories.approvals.get_latest_for_step(task_id, step_id)
        assert await broker.approve(first["id"], unit=unit)
        await unit.repositories.tasks.transition_step(
            step_id, "approved", from_statuses=("waiting_for_approval",)
        )
        await unit.repositories.tasks.transition(task_id, "queued", from_statuses=("paused",))

    # 2. Executes once under that approval and fails.
    await process(queue, uow, task_id)
    task, _, _ = await snapshot(uow, task_id)
    assert task["status"] == "failed"
    assert tool.calls == 1

    # 3. Resume: the used approval must not authorise a second attempt.
    async with uow.begin() as unit:
        assert await unit.repositories.tasks.transition(
            task_id, "queued", from_statuses=("failed",)
        )
        assert await unit.repositories.approvals.is_consumed(first["id"])
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "paused"
    assert steps[0]["status"] == "waiting_for_approval"
    assert tool.calls == 1
    async with uow.begin() as unit:
        latest = await unit.repositories.approvals.get_latest_for_step(task_id, step_id)
    assert latest["id"] != first["id"] and latest["status"] == "pending"
    assert any("already used" in (e["message"] or "") for e in events)


# --- Startup recovery ----------------------------------------------------------


async def test_startup_recovery_preserves_terminal_states_and_is_idempotent(uow, bus):
    running = await make_task(
        uow,
        [("Done", None), ("Mid", None), ("Later", None)],
        status="running",
        step_statuses={0: "completed", 1: "running"},
    )
    planning = await make_task(uow, [], status="planning")
    completed = await make_task(
        uow, [("Done", None)], status="completed", step_statuses={0: "completed"}
    )
    cancelled = await make_task(uow, [("X", None)], status="cancelled")
    paused = await make_task(
        uow, [("Wait", "w")], status="paused", step_statuses={0: "waiting_for_approval"}
    )

    orphans = AsyncMock(spec=OrphanRecoveryService)
    orphans.recover.return_value = []
    recovery = SystemRecoveryService(uow, bus, orphans)
    sub = bus.subscribe()
    await recovery.run_startup_recovery()

    task, steps, events = await snapshot(uow, running)
    assert task["status"] == "interrupted"
    assert [s["status"] for s in steps] == ["completed", "failed", "pending"]
    assert "Interrupted" in steps[1]["error"]
    assert (await snapshot(uow, planning))[0]["status"] == "interrupted"
    assert (await snapshot(uow, completed))[0]["status"] == "completed"
    assert (await snapshot(uow, cancelled))[0]["status"] == "cancelled"
    paused_task, paused_steps, _ = await snapshot(uow, paused)
    assert paused_task["status"] == "paused"
    assert paused_steps[0]["status"] == "waiting_for_approval"
    assert drain(sub).count("task.recovered") == 2

    # Second run changes nothing.
    await recovery.run_startup_recovery()
    _, _, events_again = await snapshot(uow, running)
    assert len(events_again) == len(events)
    assert drain(sub) == []

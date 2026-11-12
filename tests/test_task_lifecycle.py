"""Regression tests for task lifecycle integrity (Phase 12, ported to the agent loop in Phase 17).

Each test drives TaskQueue._process_task directly against a disposable SQLite database, with a
scripted model, so state transitions are deterministic and fully persisted. The loop itself is
covered in test_agent_loop.py; this file keeps the shutdown, interruption, one-time-approval and
startup-recovery guarantees.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from _agent_helpers import ScriptedModel, text_reply, tool_reply
from pydantic import BaseModel

from jarvis.approvals.broker import ApprovalBroker
from jarvis.core.event_bus import EventBus
from jarvis.core.orphan_recovery import OrphanRecoveryService
from jarvis.core.recovery import SystemRecoveryService
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.agent_loop import StepOutcome
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


def make_queue(uow, bus, registry, broker, *script) -> TaskQueue:
    return TaskQueue(uow, bus, ScriptedModel(*script), ToolExecutor(registry, broker), broker)


async def make_task(uow, steps=(), status="queued", step_statuses=None) -> str:
    """Insert a task (with optional steps) bound to a fresh project."""
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


# --- Shutdown and interruption -----------------------------------------------------------------


async def test_graceful_shutdown_between_calls_interrupts_not_completes(uow, bus, registry, broker):
    sub = bus.subscribe()
    queue = make_queue(
        uow, bus, registry, broker,
        tool_reply(("first", {}, "c1"), ("second", {}, "c2")),
        text_reply("unreachable"),
    )

    async def first_then_shutdown() -> ToolResult:
        queue._stop_event.set()  # daemon shutdown requested while the first call runs
        return ToolResult(success=True, data="done")

    second = ScriptedTool("second", ToolCategory.READ_ONLY)
    registry.register(ScriptedTool("first", ToolCategory.READ_ONLY, first_then_shutdown))
    registry.register(second)
    task_id = await make_task(uow)
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "interrupted"
    assert task["completed_at"] is None and task["final_answer"] is None
    assert [s["status"] for s in steps] == ["completed", "pending"]
    assert second.calls == 0
    assert "completed" not in status_changes(events)
    assert "task.completed" not in drain(sub)


async def test_interruption_during_a_tool_call_marks_task_interrupted(uow, bus, registry, broker):
    started = asyncio.Event()

    async def long_running() -> ToolResult:
        started.set()
        await asyncio.sleep(60)
        return ToolResult(success=True)

    registry.register(ScriptedTool("slow", ToolCategory.READ_ONLY, long_running))
    queue = make_queue(
        uow, bus, registry, broker, tool_reply(("slow", {}, "c1"), ("slow", {}, "c2"))
    )
    task_id = await make_task(uow)
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


async def test_interruption_while_waiting_for_the_model_marks_task_interrupted(
    uow, bus, registry, broker
):
    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.sleep(60)

    queue = make_queue(uow, bus, registry, broker, hang)
    task_id = await make_task(uow)
    runner = asyncio.create_task(process(queue, uow, task_id))
    await asyncio.wait_for(started.wait(), timeout=10)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner

    task, _, _ = await snapshot(uow, task_id)
    assert task["status"] == "interrupted"


async def test_queue_stop_during_long_step_interrupts_task(uow, bus, registry, broker):
    started = asyncio.Event()

    async def long_running() -> ToolResult:
        started.set()
        await asyncio.sleep(60)
        return ToolResult(success=True)

    registry.register(ScriptedTool("slow", ToolCategory.READ_ONLY, long_running))
    queue = make_queue(uow, bus, registry, broker, tool_reply(("slow", {}, "c1")))
    task_id = await make_task(uow)
    await queue.start()
    await asyncio.wait_for(started.wait(), timeout=10)
    await queue.stop()  # grace period elapses, then the worker is cancelled

    task, steps, _ = await snapshot(uow, task_id)
    assert task["status"] == "interrupted"
    assert steps[0]["status"] == "failed"


# --- One-time approvals ------------------------------------------------------------------------


async def test_approval_authorises_only_one_execution_attempt(uow, bus, registry, broker):
    async def fail() -> ToolResult:
        return ToolResult(success=False, error="transient")

    tool = ScriptedTool("writer", ToolCategory.MUTATING, fail)
    registry.register(tool)
    queue = make_queue(uow, bus, registry, broker, tool_reply(("writer", {}, "w1")))
    task_id = await make_task(uow)

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
        assert await unit.repositories.tasks.transition(task_id, "queued", from_statuses=("paused",))

    # 2. Executes once under that approval; the tool fails, which is the model's to handle
    # (the scripted model has no further reply, so the task itself fails after this turn).
    await process(queue, uow, task_id)
    _, steps, _ = await snapshot(uow, task_id)
    assert (steps[0]["status"], tool.calls) == ("failed", 1)

    # 3. A second attempt at the same step: the used approval must not authorise it.
    async with uow.begin() as unit:
        assert await unit.repositories.approvals.is_consumed(first["id"])
        await unit.repositories.tasks.update_step(step_id, status="pending")
        await unit.repositories.tasks.update(task_id, status="running")
        step = await unit.repositories.tasks.get_step(step_id)
    assert await queue._execute_step(task_id, step) is StepOutcome.PAUSED

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "paused"
    assert steps[0]["status"] == "waiting_for_approval"
    assert tool.calls == 1
    async with uow.begin() as unit:
        latest = await unit.repositories.approvals.get_latest_for_step(task_id, step_id)
    assert latest["id"] != first["id"] and latest["status"] == "pending"
    assert any("already used" in (e["message"] or "") for e in events)


async def test_approval_pause_is_not_a_failure(uow, bus, registry, broker):
    sub = bus.subscribe()
    tool = ScriptedTool("writer", ToolCategory.MUTATING)
    registry.register(tool)
    queue = make_queue(uow, bus, registry, broker, tool_reply(("writer", {}, "w1")))
    task_id = await make_task(uow)
    await process(queue, uow, task_id)

    task, steps, events = await snapshot(uow, task_id)
    assert task["status"] == "paused"
    assert steps[0]["status"] == "waiting_for_approval"
    assert tool.calls == 0
    assert "failed" not in status_changes(events)
    assert "task.completed" not in drain(sub)


# --- Startup recovery --------------------------------------------------------------------------


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

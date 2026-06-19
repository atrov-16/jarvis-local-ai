"""Tests for TaskQueue service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.core.event_bus import EventBus
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.planner import PlannedStep, PlannedTask, Planner
from jarvis.tasks.queue import TaskQueue


@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "test.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)

@pytest.fixture
def event_bus():
    return EventBus()

from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry


@pytest.fixture
def mock_planner():
    return MagicMock(spec=Planner)

@pytest.fixture
def mock_tool_executor():
    registry = ToolRegistry()
    return ToolExecutor(registry)

@pytest.fixture
def approval_broker(uow, event_bus):
    from jarvis.approvals.broker import ApprovalBroker
    return ApprovalBroker(uow, event_bus)

@pytest.fixture
def task_queue(uow, event_bus, mock_planner, mock_tool_executor, approval_broker):
    return TaskQueue(uow, event_bus, mock_planner, mock_tool_executor, approval_broker)

async def test_task_lifecycle_planning_to_completed(task_queue, uow, mock_planner):
    # Setup mock plan
    mock_planner.create_plan = AsyncMock(return_value=PlannedTask(
        title="Test Task",
        steps=[PlannedStep(title="Step 1", description="Do it")]
    ))
    
    # 1. Submit task
    async with uow.begin() as unit:
        task_id = await unit.repositories.tasks.insert(
            title="Initial Title",
            user_request="Perform test"
        )
    
    # Start worker briefly
    await task_queue.start()
    
    # Wait for planning to complete
    max_wait = 10
    while max_wait > 0:
        async with uow.begin() as unit:
            task = await unit.repositories.tasks.get(task_id)
            if task["status"] == "waiting_for_plan_approval":
                break
        await asyncio.sleep(0.1)
        max_wait -= 1
    
    assert task["status"] == "waiting_for_plan_approval"
    assert task["title"] == "Test Task"
    
    # 2. Approve plan (manual transition for now)
    async with uow.begin() as unit:
        await unit.repositories.tasks.update(task_id, status="queued")
    
    # Wait for execution to complete
    max_wait = 50
    while max_wait > 0:
        async with uow.begin() as unit:
            task = await unit.repositories.tasks.get(task_id)
            if task["status"] == "completed":
                break
        await asyncio.sleep(0.1)
        max_wait -= 1
        
    assert task["status"] == "completed"
    
    # Verify events
    async with uow.begin() as unit:
        events = await unit.repositories.tasks.list_events(task_id)
        event_types = [e["event_type"] for e in events]
        assert "status_change" in event_types
        assert "step_started" in event_types
        assert "step_completed" in event_types
        
    await task_queue.stop()



async def test_claimed_at_updated(task_queue, uow, mock_planner):
    mock_planner.create_plan = AsyncMock(return_value=PlannedTask(
        title="Claimed Task",
        steps=[PlannedStep(title="Step 1", description="Do it")]
    ))
    
    async with uow.begin() as unit:
        task_id = await unit.repositories.tasks.insert(title="Claim Me", user_request="...")
        
    await task_queue.start()
    
    max_wait = 10
    while max_wait > 0:
        async with uow.begin() as unit:
            task = await unit.repositories.tasks.get(task_id)
            if task["claimed_at"]:
                break
        await asyncio.sleep(0.1)
        max_wait -= 1
        
    assert task["claimed_at"] is not None
    await task_queue.stop()

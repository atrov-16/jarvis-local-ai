"""Tests for TaskRepository and Phase 5 migrations."""

import pytest

from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "test.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)

async def test_task_lifecycle_storage(uow):
    async with uow.begin() as unit:
        # 1. Insert Task
        task_id = await unit.repositories.tasks.insert(
            title="Test Task",
            user_request="Perform a test",
            status="queued"
        )
        assert task_id is not None
        
        # 2. Insert Steps
        step1_id = await unit.repositories.tasks.insert_step(
            task_id=task_id,
            step_index=0,
            title="Step 1",
            status="pending"
        )
        step2_id = await unit.repositories.tasks.insert_step(
            task_id=task_id,
            step_index=1,
            title="Step 2",
            status="pending"
        )
        
        # 3. List Steps
        steps = await unit.repositories.tasks.list_steps(task_id)
        assert len(steps) == 2
        assert steps[0]["id"] == step1_id
        assert steps[1]["id"] == step2_id
        
        # 4. Update Task Status
        await unit.repositories.tasks.update(task_id, status="running")
        task = await unit.repositories.tasks.get(task_id)
        assert task["status"] == "running"
        
        # 5. Update Step Status
        await unit.repositories.tasks.update_step(step1_id, status="completed", attempt_count=1)
        step = await unit.repositories.tasks.get_step(step1_id)
        assert step["status"] == "completed"
        assert step["attempt_count"] == 1

async def test_task_events(uow):
    async with uow.begin() as unit:
        task_id = await unit.repositories.tasks.insert(
            title="Event Test",
            user_request="Test events",
        )
        
        await unit.repositories.tasks.insert_event(
            task_id=task_id,
            event_type="status_change",
            message="Task started",
            payload={"from": "queued", "to": "running"}
        )
        
        events = await unit.repositories.tasks.list_events(task_id)
        assert len(events) == 1
        assert events[0]["event_type"] == "status_change"
        assert "queued" in events[0]["payload_json"]

async def test_subtask_relationship(uow):
    async with uow.begin() as unit:
        parent_id = await unit.repositories.tasks.insert(
            title="Parent",
            user_request="Parent request"
        )
        child_id = await unit.repositories.tasks.insert(
            parent_task_id=parent_id,
            title="Child",
            user_request="Child request"
        )
        
        child = await unit.repositories.tasks.get(child_id)
        assert child["parent_task_id"] == parent_id

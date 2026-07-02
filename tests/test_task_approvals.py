"""Approval and notification tests for Phase 6."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.planner import PlannedStep, PlannedTask
from jarvis.tools.base import ToolCategory, ToolResult


@pytest.fixture
def event_bus():
    return EventBus()

@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "test_approvals.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)

@pytest.fixture
def app(uow, event_bus, tmp_path):
    config = JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "test_approvals.sqlite"))
    secret_manager = SecretManager({"JARVIS_API_TOKEN": "test-token"}, use_keyring=False)
    return create_app(
        config=config,
        secret_manager=secret_manager,
        event_bus=event_bus,
        database_path=tmp_path / "test_approvals.sqlite"
    )

@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        resp = c.post("/v1/projects", json={"name": "test-project"})
        project_id = resp.json()["id"]
        c.post("/v1/projects/current", json={"id": project_id})
        yield c

@pytest.fixture
def mock_planner(app):
    planner = MagicMock()
    app.state.task_queue._planner = planner
    return planner

@pytest.fixture
def registry(app):
    return app.state.task_queue._tool_executor._registry

@pytest.mark.asyncio
async def test_mutating_tool_requires_approval_by_policy(client, uow, registry, mock_planner, app):
    # 1. Setup a workspace with 'approval_required' policy
    async with uow.begin() as unit:
        ws_id = await unit.repositories.workspaces.insert(
            name="Secure", path="C:/secure", write_policy="approval_required"
        )
        project_id = await unit.repositories.projects.insert(name="SecureProject")

        await unit.repositories.projects.link_workspace(project_id, ws_id)

    # 2. Register a mutating tool
    class MutatingInput(BaseModel):
        pass

    class MutatingTool(MagicMock):
        name = "mutating_tool"
        description = "Mutates things"
        category = ToolCategory.MUTATING
        timeout_seconds = 60
        def get_input_schema(self):
            return MutatingInput
        async def execute(self, **kwargs):
            return ToolResult(success=True, data="done")

    tool = MutatingTool()
    registry.register(tool)

    # 3. Setup planner to return a step with this tool, but NOT flagging approval
    # The TaskQueue should catch it anyway due to workspace policy.
    mock_planner.create_plan = AsyncMock(return_value=PlannedTask(
        title="Mutate Task",
        steps=[PlannedStep(title="Mutate", description="...", tool_name="mutating_tool", requires_approval=False)]
    ))

    # 4. Create task
    resp = client.post("/v1/tasks", json={"user_request": "mutate", "project_id": project_id})
    task_id = resp.json()["id"]

    # Wait for plan approval
    max_wait = 20
    while max_wait > 0:
        resp = client.get(f"/v1/tasks/{task_id}")
        if resp.json()["status"] == "waiting_for_plan_approval":
            break
        await asyncio.sleep(0.1)
        max_wait -= 1
    
    # Approve plan
    client.post(f"/v1/tasks/{task_id}/plan/approve")

    # 5. Wait for it to hit 'waiting_for_approval'
    max_wait = 20
    while max_wait > 0:
        resp = client.get(f"/v1/tasks/{task_id}")
        task = resp.json()
        if any(s["status"] == "waiting_for_approval" for s in task["steps"]):
            break
        await asyncio.sleep(0.1)
        max_wait -= 1

    assert task["status"] == "paused"
    step = next(s for s in task["steps"] if s["status"] == "waiting_for_approval")
    assert step["title"] == "Mutate"
    
    # Verify 'approval_requested' event
    events = [e for e in task["events"] if e["event_type"] == "approval_requested"]
    assert len(events) > 0

    # 6. Approve the step
    resp = client.post(f"/v1/tasks/{task_id}/steps/{step['id']}/approve", json={"reason": "I trust you"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # 7. Wait for completion
    max_wait = 20
    while max_wait > 0:
        resp = client.get(f"/v1/tasks/{task_id}")
        if resp.json()["status"] == "completed":
            break
        await asyncio.sleep(0.1)
        max_wait -= 1

    assert resp.json()["status"] == "completed"
    
    # Verify 'approval_granted' event
    events = [e for e in resp.json()["events"] if e["event_type"] == "approval_granted"]
    assert len(events) > 0
    assert events[0]["payload_json"] is not None
    assert "I trust you" in events[0]["payload_json"]

@pytest.mark.asyncio
async def test_step_denial_fails_task(client, uow, registry, mock_planner):
    # Setup similar to above but deny
    async with uow.begin() as unit:
        ws_id = await unit.repositories.workspaces.insert(
            name="Secure2", path="C:/secure2", write_policy="approval_required"
        )
        project_id = await unit.repositories.projects.insert(name="SecureProject2")
        await unit.repositories.projects.link_workspace(project_id, ws_id)

    # Re-use mutating_tool if already registered, or skip if duplicate
    try:
        class MutatingInput(BaseModel):
            pass
        class MutatingTool(MagicMock):
            name = "mutating_tool_2"
            description = "Mutates things"
            category = ToolCategory.MUTATING
            timeout_seconds = 60
            def get_input_schema(self):
                return MutatingInput
            async def execute(self, **kwargs):
                return ToolResult(success=True, data="done")
        registry.register(MutatingTool())
    except ValueError:
        pass

    mock_planner.create_plan = AsyncMock(return_value=PlannedTask(
        title="Mutate Task 2",
        steps=[PlannedStep(title="Mutate", description="...", tool_name="mutating_tool_2" if "mutating_tool_2" in registry._tools else "mutating_tool", requires_approval=False)]
    ))

    # 4. Create task
    resp = client.post("/v1/tasks", json={"user_request": "mutate", "project_id": project_id})
    task_id = resp.json()["id"]

    # Wait for plan approval
    max_wait = 20
    while max_wait > 0:
        resp = client.get(f"/v1/tasks/{task_id}")
        if resp.json()["status"] == "waiting_for_plan_approval":
            break
        await asyncio.sleep(0.1)
        max_wait -= 1
    
    # Approve plan
    client.post(f"/v1/tasks/{task_id}/plan/approve")

    # 5. Wait for it to hit 'waiting_for_approval'
    max_wait = 20
    while max_wait > 0:
        task = client.get(f"/v1/tasks/{task_id}").json()
        if any(s["status"] == "waiting_for_approval" for s in task["steps"]):
            break
        await asyncio.sleep(0.1)
        max_wait -= 1

    step = next(s for s in task["steps"] if s["status"] == "waiting_for_approval")
    
    # Deny
    resp = client.post(f"/v1/tasks/{task_id}/steps/{step['id']}/deny", json={"reason": "Too risky"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    
    task = client.get(f"/v1/tasks/{task_id}").json()
    assert task["status"] == "failed"
    
    # Verify 'approval_denied' event
    events = [e for e in task["events"] if e["event_type"] == "approval_denied"]
    assert len(events) > 0
    assert "Too risky" in events[0]["payload_json"]

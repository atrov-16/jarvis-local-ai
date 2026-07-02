"""Tests for Task API endpoints."""

import pytest
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager


@pytest.fixture
def client(tmp_path):
    config = JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "memory.sqlite"))
    app = create_app(
        config=config,
        secret_manager=SecretManager({"JARVIS_API_TOKEN": "test-token"}, use_keyring=False),
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        resp = client.post("/v1/projects", headers=headers, json={"name": "test-project"})
        project_id = resp.json()["id"]
        client.post("/v1/projects/current", headers=headers, json={"id": project_id})
        yield client

def test_task_create_and_list(client):
    headers = {"Authorization": "Bearer test-token"}
    
    # Create task
    response = client.post(
        "/v1/tasks",
        headers=headers,
        json={"user_request": "Test task"}
    )
    assert response.status_code == 201
    task_id = response.json()["id"]
    
    # List tasks
    response = client.get("/v1/tasks", headers=headers)
    assert response.status_code == 200
    tasks = response.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    assert tasks[0]["status"] == "queued"

def test_task_get_detail(client):
    headers = {"Authorization": "Bearer test-token"}
    response = client.post(
        "/v1/tasks",
        headers=headers,
        json={"user_request": "Test detail"}
    )
    task_id = response.json()["id"]
    
    response = client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert response.status_code == 200
    task = response.json()
    assert task["id"] == task_id
    assert "steps" in task
    assert "events" in task
    assert len(task["steps"]) == 0

def test_task_approve_plan(client):
    headers = {"Authorization": "Bearer test-token"}
    response = client.post("/v1/tasks", headers=headers, json={"user_request": "Test"})
    task_id = response.json()["id"]
    
    # Approve plan should fail initially because status is 'queued'
    response = client.post(f"/v1/tasks/{task_id}/plan/approve", headers=headers)
    assert response.status_code == 400
    
    # Wait, need a way to mock the worker or manipulate DB to test transitions
    # Use internal state
    import asyncio
    async def set_status():
        async with client.app.state.task_queue._uow.begin() as unit:
            await unit.repositories.tasks.update(task_id, status="waiting_for_plan_approval")
    asyncio.run(set_status())
    
    response = client.post(f"/v1/tasks/{task_id}/plan/approve", headers=headers)
    assert response.status_code == 200
    
    response = client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert response.json()["status"] == "queued"

def test_task_resume(client):
    headers = {"Authorization": "Bearer test-token"}
    response = client.post("/v1/tasks", headers=headers, json={"user_request": "Test"})
    task_id = response.json()["id"]
    
    import asyncio
    async def set_status():
        async with client.app.state.task_queue._uow.begin() as unit:
            await unit.repositories.tasks.update(task_id, status="paused")
    asyncio.run(set_status())
    
    response = client.post(f"/v1/tasks/{task_id}/resume", headers=headers)
    assert response.status_code == 200
    
    response = client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert response.json()["status"] == "queued"

def test_task_cancel(client):
    headers = {"Authorization": "Bearer test-token"}
    response = client.post("/v1/tasks", headers=headers, json={"user_request": "Test"})
    task_id = response.json()["id"]
    
    response = client.post(f"/v1/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 200
    
    response = client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert response.json()["status"] == "cancelled"

"""Tests for Task API endpoints."""

import time

import pytest
from _agent_helpers import EchoTool, ScriptedModel, text_reply, tool_reply
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

def test_the_retired_plan_approval_endpoint_is_gone(client):
    headers = {"Authorization": "Bearer test-token"}
    task_id = client.post("/v1/tasks", headers=headers, json={"user_request": "Test"}).json()["id"]

    assert client.post(f"/v1/tasks/{task_id}/plan/approve", headers=headers).status_code == 404


def test_task_title_comes_from_the_request(client):
    headers = {"Authorization": "Bearer test-token"}
    response = client.post(
        "/v1/tasks", headers=headers, json={"user_request": "Fix the login bug\nit crashes"}
    )

    assert response.status_code == 201
    assert response.json()["title"] == "Fix the login bug"
    assert response.json()["final_answer"] is None


def test_task_runs_to_a_final_answer_returned_by_the_api(client):
    headers = {"Authorization": "Bearer test-token"}
    echo = EchoTool()
    client.app.state.task_queue._tool_executor._registry.register(echo)
    model = ScriptedModel(
        tool_reply(("echo", {"text": "hi"}, "call_1")), text_reply("It said hi.")
    )
    client.app.state.model_router.complete = model.complete

    task_id = client.post("/v1/tasks", headers=headers, json={"user_request": "Say hi"}).json()["id"]
    deadline = time.monotonic() + 15
    while True:
        task = client.get(f"/v1/tasks/{task_id}", headers=headers).json()
        if task["status"] in ("completed", "failed") or time.monotonic() > deadline:
            break
        time.sleep(0.1)

    assert task["status"] == "completed"
    assert task["final_answer"] == "It said hi."
    assert [(s["tool_name"], s["status"]) for s in task["steps"]] == [("echo", "completed")]
    assert echo.calls == [{"text": "hi"}]
    listed = client.get("/v1/tasks", headers=headers).json()
    assert [t["final_answer"] for t in listed if t["id"] == task_id] == ["It said hi."]


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

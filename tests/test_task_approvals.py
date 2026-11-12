"""Approval flow through the HTTP API and the real worker (Phase 6, ported to the agent loop).

A mutating tool call pauses the task; approving runs it once and the model answers; denying
tells the model, which adapts. Ownership and replay rules are in test_approval_enforcement.py.
"""

import time

import pytest
from _agent_helpers import ScriptedModel, WriteTool, text_reply, tool_reply
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "test_approvals.sqlite"
    return create_app(
        config=JarvisConfig(memory=MemoryConfig(database_path=db_path)),
        secret_manager=SecretManager({"JARVIS_API_TOKEN": "test-token"}, use_keyring=False),
        event_bus=EventBus(),
        database_path=db_path,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        project_id = c.post("/v1/projects", json={"name": "test-project"}).json()["id"]
        c.post("/v1/projects/current", json={"id": project_id})
        yield c


@pytest.fixture
def write_tool(app):
    tool = WriteTool()
    app.state.task_queue._tool_executor._registry.register(tool)
    return tool


def script(app, *replies) -> ScriptedModel:
    model = ScriptedModel(*replies)
    app.state.model_router.complete = model.complete
    return model


def wait_for(client, task_id, predicate, timeout=15.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        task = client.get(f"/v1/tasks/{task_id}").json()
        if predicate(task):
            return task
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out; the task is {task['status']}.")
        time.sleep(0.1)


def waiting_step(task) -> bool:
    return any(s["status"] == "waiting_for_approval" for s in task["steps"])


def test_mutating_tool_requires_approval_then_runs_once_and_the_model_answers(
    client, app, write_tool
):
    model = script(
        app, tool_reply(("write", {"text": "hello"}, "call_1")), text_reply("Wrote hello.")
    )
    task_id = client.post("/v1/tasks", json={"user_request": "Write hello"}).json()["id"]

    task = wait_for(client, task_id, waiting_step)
    assert task["status"] == "paused"
    step = next(s for s in task["steps"] if s["status"] == "waiting_for_approval")
    assert step["title"] == "write: hello"
    assert step["input_json"] == '{"text": "hello"}'
    assert write_tool.calls == []  # nothing ran before approval
    assert any(e["event_type"] == "approval_requested" for e in task["events"])
    assert client.get("/v1/approvals").json()  # the request is listed for the user to decide

    resp = client.post(
        f"/v1/tasks/{task_id}/steps/{step['id']}/approve", json={"reason": "I trust you"}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "approved"

    task = wait_for(client, task_id, lambda t: t["status"] == "completed")
    assert task["final_answer"] == "Wrote hello."
    assert write_tool.calls == [{"text": "hello"}]  # exactly once
    granted = [e for e in task["events"] if e["event_type"] == "approval_granted"]
    assert granted and "I trust you" in granted[0]["payload_json"]
    assert model.roles(1) == ["system", "user", "assistant", "tool"]  # the result went back


def test_denying_a_call_tells_the_model_which_adapts(client, app, write_tool):
    model = script(
        app,
        tool_reply(("write", {"text": "hello"}, "call_1")),
        text_reply("I could not write it because you denied the change."),
    )
    task_id = client.post("/v1/tasks", json={"user_request": "Write hello"}).json()["id"]
    task = wait_for(client, task_id, waiting_step)
    step = next(s for s in task["steps"] if s["status"] == "waiting_for_approval")

    resp = client.post(
        f"/v1/tasks/{task_id}/steps/{step['id']}/deny", json={"reason": "Too risky"}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "skipped"

    task = wait_for(client, task_id, lambda t: t["status"] == "completed")
    assert task["final_answer"].startswith("I could not write it")
    assert write_tool.calls == []
    denied = [e for e in task["events"] if e["event_type"] == "approval_denied"]
    assert denied and "Too risky" in denied[0]["payload_json"]
    told = model.requests[1].messages[-1]
    assert (told.role, told.tool_call_id) == ("tool", "call_1")
    assert told.content == "Not run: the user denied this call. Reason: Too risky"


def test_a_denied_call_cannot_be_approved_later(client, app, write_tool):
    script(app, tool_reply(("write", {"text": "x"}, "call_1")), text_reply("ok"))
    task_id = client.post("/v1/tasks", json={"user_request": "Write x"}).json()["id"]
    task = wait_for(client, task_id, waiting_step)
    step_id = next(s["id"] for s in task["steps"] if s["status"] == "waiting_for_approval")
    assert client.post(f"/v1/tasks/{task_id}/steps/{step_id}/deny", json={}).status_code == 200
    wait_for(client, task_id, lambda t: t["status"] == "completed")

    assert client.post(f"/v1/tasks/{task_id}/steps/{step_id}/approve", json={}).status_code == 409
    assert write_tool.calls == []

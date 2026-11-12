"""Phase 17, slice 5: deciding an approval from the approval center drives the task.

`POST /v1/approvals/{id}/approve|deny` and `POST /v1/approvals/bulk` must do exactly what
`POST /v1/tasks/{task_id}/steps/{step_id}/approve|deny` does: move the step, put the paused task
back in the queue, and write the events. Before this, they flipped the approval row only and the
task stayed paused for ever. The rules do not change: an approval authorises one execution
attempt, and only pending requests can be decided.
"""

import asyncio
import time

import pytest
from _agent_helpers import ScriptedModel, WriteTool, text_reply, tool_reply
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.approvals.models import ApprovalActionType, ProposedAction, RiskLevel
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "approval_center.sqlite"
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


def paused_with_request(client, app, text="hello"):
    """A task paused on a write call; returns (task_id, step_id, approval_id)."""
    script(app, tool_reply(("write", {"text": text}, "call_1")), text_reply("All done."))
    task_id = client.post("/v1/tasks", json={"user_request": "Write it"}).json()["id"]
    task = wait_for(
        client, task_id, lambda t: any(s["status"] == "waiting_for_approval" for s in t["steps"])
    )
    step_id = next(s["id"] for s in task["steps"] if s["status"] == "waiting_for_approval")
    approval = next(a for a in client.get("/v1/approvals").json() if a["step_id"] == step_id)
    return task_id, step_id, approval["id"]


def test_approving_the_request_runs_the_call_once_and_the_task_finishes(client, app, write_tool):
    task_id, step_id, approval_id = paused_with_request(client, app)
    assert write_tool.calls == []

    resp = client.post(f"/v1/approvals/{approval_id}/approve", json={"reason": "go ahead"})
    assert resp.status_code == 200 and resp.json() == {"approved": True}

    task = wait_for(client, task_id, lambda t: t["status"] == "completed")
    assert task["final_answer"] == "All done."
    assert write_tool.calls == [{"text": "hello"}]  # exactly once
    types = [e["event_type"] for e in task["events"]]
    assert types.count("approval_granted") == 1 and types.count("step_started") == 1
    granted = next(e for e in task["events"] if e["event_type"] == "approval_granted")
    assert granted["step_id"] == step_id and "go ahead" in granted["payload_json"]


def test_denying_the_request_tells_the_model_and_the_task_goes_on(client, app, write_tool):
    task_id, step_id, approval_id = paused_with_request(client, app)

    resp = client.post(f"/v1/approvals/{approval_id}/deny", json={"reason": "no"})
    assert resp.status_code == 200 and resp.json() == {"denied": True}

    task = wait_for(client, task_id, lambda t: t["status"] == "completed")
    step = next(s for s in task["steps"] if s["id"] == step_id)
    assert step["status"] == "skipped" and "denied" in step["error"] and "no" in step["error"]
    assert write_tool.calls == []
    assert any(e["event_type"] == "approval_denied" for e in task["events"])


def test_a_request_can_only_be_decided_once(client, app, write_tool):
    task_id, _, approval_id = paused_with_request(client, app)
    assert client.post(f"/v1/approvals/{approval_id}/approve", json={}).status_code == 200
    wait_for(client, task_id, lambda t: t["status"] == "completed")

    assert client.post(f"/v1/approvals/{approval_id}/approve", json={}).status_code == 409
    assert client.post(f"/v1/approvals/{approval_id}/deny", json={}).status_code == 409
    assert write_tool.calls == [{"text": "hello"}]  # still exactly once


def test_deciding_by_step_after_the_request_was_decided_is_rejected(client, app, write_tool):
    task_id, step_id, approval_id = paused_with_request(client, app)
    assert client.post(f"/v1/approvals/{approval_id}/deny", json={}).status_code == 200
    wait_for(client, task_id, lambda t: t["status"] == "completed")

    assert client.post(f"/v1/tasks/{task_id}/steps/{step_id}/approve", json={}).status_code == 409
    assert write_tool.calls == []


def test_a_cancelled_task_cannot_be_driven_by_its_old_request(client, app, write_tool):
    task_id, _, approval_id = paused_with_request(client, app)
    assert client.post(f"/v1/tasks/{task_id}/cancel").status_code == 200

    assert client.post(f"/v1/approvals/{approval_id}/approve", json={}).status_code == 409
    time.sleep(1.5)  # longer than the worker's poll interval
    assert client.get(f"/v1/tasks/{task_id}").json()["status"] == "cancelled"
    assert write_tool.calls == []


def test_an_unknown_request_is_404(client):
    assert client.post("/v1/approvals/nope/approve", json={}).status_code == 404
    assert client.post("/v1/approvals/nope/deny", json={}).status_code == 404


def test_bulk_approve_drives_the_task_like_the_single_route(client, app, write_tool):
    task_id, _, approval_id = paused_with_request(client, app)

    resp = client.post(
        "/v1/approvals/bulk",
        json={"action": "approve", "items": [{"id": approval_id, "type": "action"}]},
    )

    assert resp.status_code == 200 and resp.json()["summary"]["success"] == 1
    task = wait_for(client, task_id, lambda t: t["status"] == "completed")
    assert task["final_answer"] == "All done."
    assert write_tool.calls == [{"text": "hello"}]


def test_bulk_deny_drives_the_task_like_the_single_route(client, app, write_tool):
    task_id, step_id, approval_id = paused_with_request(client, app)

    resp = client.post(
        "/v1/approvals/bulk",
        json={"action": "deny", "items": [{"id": approval_id, "type": "action"}]},
    )

    assert resp.status_code == 200 and resp.json()["summary"]["success"] == 1
    task = wait_for(client, task_id, lambda t: t["status"] == "completed")
    assert next(s for s in task["steps"] if s["id"] == step_id)["status"] == "skipped"
    assert write_tool.calls == []


def test_bulk_reports_a_request_that_is_no_longer_pending_as_failed(client, app, write_tool):
    task_id, _, approval_id = paused_with_request(client, app)
    assert client.post(f"/v1/approvals/{approval_id}/approve", json={}).status_code == 200
    wait_for(client, task_id, lambda t: t["status"] == "completed")

    resp = client.post(
        "/v1/approvals/bulk",
        json={"action": "approve", "items": [{"id": approval_id, "type": "action"}]},
    )

    assert resp.json()["summary"] == {"total": 1, "success": 0, "failed": 1}
    assert write_tool.calls == [{"text": "hello"}]


def test_a_request_not_tied_to_a_step_is_still_decided_as_before(client, app):
    async def create() -> str:
        action = ProposedAction(
            action_type=ApprovalActionType.TOOL,
            summary="Standalone request",
            action_json="{}",
            risk_level=RiskLevel.MEDIUM,
        )
        return await app.state.approval_broker.create_request(action)

    approval_id = asyncio.run(create())

    assert client.post(f"/v1/approvals/{approval_id}/approve", json={}).status_code == 200
    assert client.get(f"/v1/approvals/{approval_id}").json()["status"] == "approved"
    assert client.post(f"/v1/approvals/{approval_id}/approve", json={}).status_code == 409

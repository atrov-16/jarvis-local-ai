"""Regression tests: approval ownership, state validation and replay prevention (Phase 12).

These go through the HTTP API with the real background worker running, so they also
prove that an invalid approval never causes a tool to execute.
"""

import asyncio
import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from jarvis.api.http import create_app
from jarvis.approvals.broker import ApprovalBroker
from jarvis.approvals.models import ApprovalActionType, ProposedAction, RiskLevel
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult


class EmptyInput(BaseModel):
    pass


class CountingWriteTool(BaseTool):
    def __init__(self) -> None:
        super().__init__(name="counting_write", description="test", category=ToolCategory.MUTATING)
        self.calls = 0

    def get_input_schema(self) -> type[BaseModel]:
        return EmptyInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, data="written")


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "approvals.sqlite"
    app = create_app(
        config=JarvisConfig(memory=MemoryConfig(database_path=db_path)),
        secret_manager=SecretManager({"JARVIS_API_TOKEN": "test-token"}, use_keyring=False),
        event_bus=EventBus(),
        database_path=db_path,
    )
    tool = CountingWriteTool()
    app.state.task_queue._tool_executor._registry.register(tool)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer test-token"
        yield client, UnitOfWork(db_path), tool


def run(coro):
    return asyncio.run(coro)


async def _paused_step(
    uow: UnitOfWork, *, with_request: str | None = "pending"
) -> tuple[str, str, str | None]:
    """A task paused on a mutating step, as the worker leaves it; optionally with its request."""
    # A separate broker/bus: the app's bus belongs to the app's event loop.
    broker = ApprovalBroker(uow, EventBus())
    async with uow.begin() as unit:
        repos = unit.repositories
        project_id = await repos.projects.insert(name=f"p-{uuid4()}")
        task_id = await repos.tasks.insert(
            title="T", user_request="write", project_id=project_id, status="paused"
        )
        step_id = await repos.tasks.insert_step(
            task_id=task_id,
            step_index=0,
            title="Write",
            tool_name="counting_write",
            input_json="{}",
            status="waiting_for_approval",
        )
        request_id = None
        if with_request:
            request_id = await broker.create_request(
                ProposedAction(
                    action_type=ApprovalActionType.TOOL,
                    summary="write",
                    action_json="{}",
                    task_id=task_id,
                    step_id=step_id,
                    context_id=project_id,
                    risk_level=RiskLevel.HIGH,
                ),
                unit=unit,
            )
            if with_request != "pending":
                await repos.approvals.update_status(request_id, with_request)
    return task_id, step_id, request_id


async def _state(uow: UnitOfWork, task_id: str, step_id: str, request_id: str | None = None):
    async with uow.begin() as unit:
        task = await unit.repositories.tasks.get(task_id)
        step = await unit.repositories.tasks.get_step(step_id)
        request = await unit.repositories.approvals.get(request_id) if request_id else None
    return task["status"], step["status"], request["status"] if request else None


def wait_for_status(client, task_id: str, status: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/v1/tasks/{task_id}").json()
        if task["status"] == status:
            return task
        time.sleep(0.1)
    raise AssertionError(f"task never reached {status}: {task['status']}")


def approve(client, task_id, step_id):
    return client.post(f"/v1/tasks/{task_id}/steps/{step_id}/approve", json={"reason": "ok"})


def deny(client, task_id, step_id):
    return client.post(f"/v1/tasks/{task_id}/steps/{step_id}/deny", json={"reason": "no"})


# --- Tool (step) approvals -------------------------------------------------------


def test_valid_tool_approval_executes_exactly_once(env):
    client, uow, tool = env
    task_id, step_id, request_id = run(_paused_step(uow))

    resp = approve(client, task_id, step_id)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    wait_for_status(client, task_id, "completed")
    assert tool.calls == 1
    assert run(_state(uow, task_id, step_id, request_id)) == ("completed", "completed", "approved")


def test_repeated_approval_is_rejected_and_never_re_executes(env):
    client, uow, tool = env
    task_id, step_id, _ = run(_paused_step(uow))
    assert approve(client, task_id, step_id).status_code == 200
    wait_for_status(client, task_id, "completed")

    resp = approve(client, task_id, step_id)
    assert resp.status_code == 409
    time.sleep(1.5)  # give the worker a chance to (wrongly) pick the task up again
    assert tool.calls == 1
    assert run(_state(uow, task_id, step_id))[:2] == ("completed", "completed")


def test_step_from_another_task_is_rejected(env):
    client, uow, tool = env
    task_a, step_a, _ = run(_paused_step(uow))
    task_b, step_b, _ = run(_paused_step(uow))

    resp = approve(client, task_a, step_b)
    assert resp.status_code == 404
    assert deny(client, task_a, step_b).status_code == 404
    assert run(_state(uow, task_a, step_a))[:2] == ("paused", "waiting_for_approval")
    assert run(_state(uow, task_b, step_b))[:2] == ("paused", "waiting_for_approval")
    assert tool.calls == 0


def test_unknown_task_or_step_is_404(env):
    client, uow, _ = env
    task_id, step_id, _ = run(_paused_step(uow))
    assert approve(client, "no-such-task", step_id).status_code == 404
    assert approve(client, task_id, "no-such-step").status_code == 404


def test_approval_after_completion_is_rejected(env):
    client, uow, tool = env
    task_id, step_id, _ = run(_paused_step(uow))

    async def mark_completed():
        async with uow.begin() as unit:
            await unit.repositories.tasks.update(task_id, status="completed")
            await unit.repositories.tasks.update_step(step_id, status="completed")

    run(mark_completed())
    assert approve(client, task_id, step_id).status_code == 409
    assert deny(client, task_id, step_id).status_code == 409
    assert run(_state(uow, task_id, step_id))[:2] == ("completed", "completed")
    assert tool.calls == 0


def test_approval_after_cancellation_is_rejected(env):
    client, uow, tool = env
    task_id, step_id, request_id = run(_paused_step(uow))

    assert client.post(f"/v1/tasks/{task_id}/cancel").status_code == 200
    # Cancelling invalidates the outstanding approval request.
    assert run(_state(uow, task_id, step_id, request_id)) == (
        "cancelled",
        "waiting_for_approval",
        "cancelled",
    )

    assert approve(client, task_id, step_id).status_code == 409
    # Nor can the cancelled request be approved directly.
    assert client.post(f"/v1/approvals/{request_id}/approve", json={}).status_code == 409
    time.sleep(1.5)
    assert run(_state(uow, task_id, step_id))[0] == "cancelled"
    assert tool.calls == 0


def test_missing_approval_request_is_rejected(env):
    client, uow, tool = env
    task_id, step_id, _ = run(_paused_step(uow, with_request=None))
    assert approve(client, task_id, step_id).status_code == 409
    assert run(_state(uow, task_id, step_id))[:2] == ("paused", "waiting_for_approval")
    assert tool.calls == 0


@pytest.mark.parametrize("status", ["denied", "expired", "cancelled"])
def test_non_pending_approval_request_is_rejected(env, status):
    client, uow, tool = env
    task_id, step_id, _ = run(_paused_step(uow, with_request=status))
    assert approve(client, task_id, step_id).status_code == 409
    assert run(_state(uow, task_id, step_id))[:2] == ("paused", "waiting_for_approval")
    assert tool.calls == 0


def test_already_consumed_approval_is_rejected(env):
    client, uow, tool = env
    task_id, step_id, request_id = run(_paused_step(uow, with_request="approved"))

    async def consume():
        # An execution attempt already started under this approval.
        async with uow.begin() as unit:
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                step_id=step_id,
                event_type="step_started",
                message="Starting step",
                correlation_id=request_id,
            )

    run(consume())
    resp = approve(client, task_id, step_id)
    assert resp.status_code == 409
    assert "already used" in resp.json()["detail"]
    assert tool.calls == 0


def test_valid_denial_fails_task_without_execution(env):
    client, uow, tool = env
    task_id, step_id, request_id = run(_paused_step(uow))

    resp = deny(client, task_id, step_id)
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert run(_state(uow, task_id, step_id, request_id)) == ("failed", "failed", "denied")

    # The decision is final: neither a second denial nor a late approval is accepted.
    assert deny(client, task_id, step_id).status_code == 409
    assert approve(client, task_id, step_id).status_code == 409
    time.sleep(1.5)
    assert tool.calls == 0


def test_generic_approval_endpoints_reject_decided_requests(env):
    client, uow, _ = env
    _, _, request_id = run(_paused_step(uow))
    assert client.post(f"/v1/approvals/{request_id}/deny", json={}).status_code == 200
    assert client.post(f"/v1/approvals/{request_id}/approve", json={}).status_code == 409
    assert client.post(f"/v1/approvals/{request_id}/deny", json={}).status_code == 409
    assert client.post("/v1/approvals/missing/approve", json={}).status_code == 404


# --- Plan approvals --------------------------------------------------------------


async def _waiting_plan(
    uow: UnitOfWork, request_status: str | None = "pending"
) -> tuple[str, str | None]:
    async with uow.begin() as unit:
        repos = unit.repositories
        project_id = await repos.projects.insert(name=f"p-{uuid4()}")
        task_id = await repos.tasks.insert(
            title="T",
            user_request="plan",
            project_id=project_id,
            status="waiting_for_plan_approval",
        )
        # A native (tool-less) step so the worker can finish the task without an LLM.
        await repos.tasks.insert_step(task_id=task_id, step_index=0, title="Think")
        request_id = None
        if request_status:
            request_id = await repos.approvals.insert(
                task_id=task_id,
                action_type="plan",
                risk_level="medium",
                summary="Approve plan",
                action_json="{}",
                action_hash="h",
                status=request_status,
            )
    return task_id, request_id


def test_valid_plan_approval_queues_execution(env):
    client, uow, _ = env
    task_id, request_id = run(_waiting_plan(uow))
    assert client.post(f"/v1/tasks/{task_id}/plan/approve").status_code == 200
    wait_for_status(client, task_id, "completed")

    async def request_status():
        async with uow.begin() as unit:
            return (await unit.repositories.approvals.get(request_id))["status"]

    assert run(request_status()) == "approved"
    # Approving again is not possible once the task left plan approval.
    assert client.post(f"/v1/tasks/{task_id}/plan/approve").status_code == 400


def test_plan_approval_without_request_is_rejected(env):
    client, uow, _ = env
    task_id, _ = run(_waiting_plan(uow, request_status=None))
    assert client.post(f"/v1/tasks/{task_id}/plan/approve").status_code == 409
    assert client.get(f"/v1/tasks/{task_id}").json()["status"] == "waiting_for_plan_approval"


def test_plan_approval_with_denied_request_is_rejected(env):
    client, uow, _ = env
    task_id, _ = run(_waiting_plan(uow, request_status="denied"))
    assert client.post(f"/v1/tasks/{task_id}/plan/approve").status_code == 409
    assert client.get(f"/v1/tasks/{task_id}").json()["status"] == "waiting_for_plan_approval"


# --- Resume / cancel guards ------------------------------------------------------


def test_terminal_tasks_cannot_be_resumed_or_cancelled(env):
    client, uow, _ = env
    task_id, step_id, _ = run(_paused_step(uow))

    async def mark(status):
        async with uow.begin() as unit:
            await unit.repositories.tasks.update(task_id, status=status)

    run(mark("completed"))
    assert client.post(f"/v1/tasks/{task_id}/resume").status_code == 400
    assert client.post(f"/v1/tasks/{task_id}/cancel").status_code == 400
    run(mark("cancelled"))
    assert client.post(f"/v1/tasks/{task_id}/resume").status_code == 400
    assert client.get(f"/v1/tasks/{task_id}").json()["status"] == "cancelled"

"""Approving or denying a tool call: the one place that moves the approval, the step and the task.

A task that needs consent for a tool call is `paused` with the step `waiting_for_approval` and an
approval request. Deciding it must change all three together, in one transaction:

- approve: request -> approved, step -> approved, task paused -> queued (the worker runs the call);
- deny:    request -> denied,   step -> skipped (the model is told),  task paused -> queued.

`POST /v1/tasks/{id}/steps/{sid}/approve|deny`, `POST /v1/approvals/{id}/approve|deny` and the
bulk endpoint all go through here, so they cannot drift apart. The rules stay as they were:
an approval authorises exactly one execution attempt, only a pending request can be decided, and
a request that was already used, has expired, or belongs to a task that is no longer paused is
rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jarvis.approvals.broker import ApprovalBroker
from jarvis.storage.unit_of_work import UnitOfWorkScope


class DecisionError(Exception):
    """A decision that must be refused. `status_code` is the HTTP status to answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def load_step_awaiting_decision(
    unit: UnitOfWorkScope, task_id: str, step_id: str
) -> dict[str, Any]:
    """Validate that a step decision is legitimate; return the step's approval request.

    Raises 404 if the task or step is missing or the step belongs to another task,
    and 409 if the task/step/approval is not in a decidable state (completed,
    cancelled, already decided, or an approval that was already used).
    """
    assert unit.repositories is not None
    task = await unit.repositories.tasks.get(task_id)
    if not task:
        raise DecisionError(404, "Task not found.")
    step = await unit.repositories.tasks.get_step(step_id)
    if not step or step["task_id"] != task_id:
        raise DecisionError(404, "Step not found for this task.")
    if step["status"] != "waiting_for_approval":
        raise DecisionError(
            409, f"Step is {step['status']}; only steps waiting for approval can be decided."
        )
    if task["status"] != "paused":
        raise DecisionError(
            409, f"Task is {task['status']}; step decisions require a paused task."
        )
    request = await unit.repositories.approvals.get_latest_for_step(task_id, step_id, "tool")
    if not request:
        raise DecisionError(409, "No approval request exists for this step.")
    approvals = unit.repositories.approvals
    if request["status"] == "approved" and await approvals.is_consumed(request["id"]):
        raise DecisionError(409, "Approval request was already used.")
    if request["status"] not in ("pending", "approved"):
        raise DecisionError(409, f"Approval request is {request['status']}.")
    if request["expires_at"] and str(request["expires_at"]) <= datetime.now(UTC).isoformat():
        raise DecisionError(409, "Approval request has expired.")
    return request


async def decide_step(
    unit: UnitOfWorkScope,
    broker: ApprovalBroker,
    *,
    task_id: str,
    step_id: str,
    approve: bool,
    reason: str | None = None,
    decided_by: str = "user",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Approve or deny the tool call a paused task is waiting on; returns the updated step.

    `request_id`, when given, is the approval request the caller is deciding: it must be the
    step's current request, so a stale request can never drive a newer one.
    """
    assert unit.repositories is not None
    request = await load_step_awaiting_decision(unit, task_id, step_id)
    if request_id is not None and request["id"] != request_id:
        raise DecisionError(409, "A newer approval request exists for this step.")
    tasks = unit.repositories.tasks
    denied_step: dict[str, Any] | None = None

    if approve:
        # A pending request is approved here; one already approved elsewhere but not yet
        # used is accepted as it is.
        if request["status"] == "pending" and not await broker.approve(
            request["id"], decided_by=decided_by, reason=reason, unit=unit
        ):
            raise DecisionError(409, "Approval request is no longer pending.")
        if not await tasks.transition_step(
            step_id, "approved", from_statuses=("waiting_for_approval",)
        ):
            raise DecisionError(409, "Step state changed; not approved.")
        if not await tasks.transition(task_id, "queued", from_statuses=("paused",)):
            raise DecisionError(409, "Task state changed; not approved.")
        granted, queued_message = "approval_granted", "Task queued after step approval"
        granted_message = "Step approved by user"
    else:
        if not await broker.deny(
            request["id"],
            decided_by=decided_by,
            reason=reason,
            unit=unit,
            expected_status=request["status"],
        ):
            raise DecisionError(409, "Approval request state changed; not denied.")
        # 'skipped' is the step's final state; this text is what the model is told.
        error_msg = "the user denied this call."
        if reason:
            error_msg += f" Reason: {reason}"
        denied_step = await tasks.get_step(step_id)
        if not await tasks.transition_step(
            step_id, "skipped", from_statuses=("waiting_for_approval",), error=error_msg
        ):
            raise DecisionError(409, "Step state changed; not denied.")
        if not await tasks.transition(task_id, "queued", from_statuses=("paused",)):
            raise DecisionError(409, "Task state changed; not denied.")
        granted, queued_message = "approval_denied", (
            "Task queued after step denial; the model will be told"
        )
        granted_message = "Step denied by user"

    await tasks.insert_event(
        task_id=task_id,
        step_id=step_id,
        event_type=granted,
        message=granted_message,
        payload={"reason": reason},
        correlation_id=request["id"],
    )
    if not approve:
        assert denied_step is not None
        # The call will never run: close it for clients (the relay publishes `step_failed` as a
        # completed call with success=False).
        await tasks.insert_event(
            task_id=task_id,
            step_id=step_id,
            event_type="step_failed",
            message=f"Call not run: {error_msg}",
            payload={
                "error": error_msg,
                "tool_call_id": denied_step.get("tool_call_id"),
                "tool_name": denied_step.get("tool_name"),
            },
            severity="warning",
            correlation_id=request["id"],
        )
    await tasks.insert_event(
        task_id=task_id,
        step_id=step_id,
        event_type="status_change",
        message=queued_message,
        payload={"status": "queued", "previous_status": "paused"},
        correlation_id=request["id"],
    )
    updated = await tasks.get_step(step_id)
    assert updated is not None
    return updated


async def decide_approval(
    unit: UnitOfWorkScope,
    broker: ApprovalBroker,
    approval_id: str,
    *,
    approve: bool,
    reason: str | None = None,
    decided_by: str = "user",
) -> None:
    """Approve or deny an approval request by its own id.

    A request for a task's tool call drives the step and the task (see `decide_step`). Any other
    request (not tied to a step) only has its own state changed. Only pending requests can be
    decided.
    """
    assert unit.repositories is not None
    request = await unit.repositories.approvals.get(approval_id)
    if not request:
        raise DecisionError(404, "Approval request not found.")
    verb = "approved" if approve else "denied"
    if request["status"] != "pending":
        raise DecisionError(
            409, f"Approval request is {request['status']}; only pending requests can be {verb}."
        )

    if request["task_id"] and request["step_id"] and request["action_type"] == "tool":
        await decide_step(
            unit,
            broker,
            task_id=request["task_id"],
            step_id=request["step_id"],
            approve=approve,
            reason=reason,
            decided_by=decided_by,
            request_id=approval_id,
        )
        return

    decide = broker.approve if approve else broker.deny
    if not await decide(approval_id, decided_by=decided_by, reason=reason, unit=unit):
        raise DecisionError(
            409, f"Approval request is no longer pending; only pending requests can be {verb}."
        )

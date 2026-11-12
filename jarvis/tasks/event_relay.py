"""Publish committed task events on the event bus.

Every `task_events` row a transaction writes is handed here by `UnitOfWork` after the commit
(never after a rollback), so a bus subscriber (the WebSocket, the GUI) is only told about things
that are already in the database. Each bus event carries the row's `seq`, the same cursor
`GET /v1/tasks/{id}/events?since=` takes: a client that missed events while disconnected asks for
everything after the last `seq` it saw.

Only the task events a client wants to follow are relayed; the rest stay in the log.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from jarvis.core.event_bus import EventBus

# task_events.event_type -> bus event type.
BUS_EVENT_FOR = {
    "status_change": "task.status_changed",
    "tool_call_proposed": "task.tool_call_proposed",
    "step_started": "task.tool_call_started",
    "step_completed": "task.tool_call_completed",
    "step_failed": "task.tool_call_completed",  # carries success=False and the error
    "approval_requested": "task.approval_required",
    "final_answer": "task.final_answer",
}


def bus_payload(event: dict[str, object]) -> dict[str, object]:
    """The bus payload for one stored task event: its own payload plus where it belongs."""
    stored = event.get("payload")
    payload: dict[str, object] = dict(stored) if isinstance(stored, dict) else {}
    payload.update(
        seq=event["seq"],
        task_id=event["task_id"],
        step_id=event.get("step_id"),
        message=event.get("message"),
        severity=event.get("severity"),
    )
    if event["event_type"] == "step_failed":
        payload["success"] = False
    return payload


def make_commit_hook(bus: EventBus) -> Callable[[list[dict[str, object]]], Awaitable[None]]:
    async def publish_committed(events: list[dict[str, object]]) -> None:
        for event in events:
            bus_type = BUS_EVENT_FOR.get(str(event["event_type"]))
            if bus_type is not None:
                await bus.publish(bus_type, bus_payload(event))

    return publish_committed

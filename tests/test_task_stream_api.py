"""Phase 17, slice 5: what a client (CLI now, GUI later) sees of a running task.

- the task/step response fields (`final_answer`, `tool_call_id`, `risk_level`, full `input_json`);
- bus events, published only after the transaction that wrote them has committed;
- `GET /v1/tasks/{id}/events?since=` for catching up after a dropped WebSocket.
"""

import json
import sqlite3
import time

import pytest
from _agent_helpers import EchoTool, ScriptedModel, WriteTool, text_reply, tool_reply
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus
from jarvis.models.schemas import ToolCall


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "stream.sqlite"


@pytest.fixture
def app(db_path):
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
def tools(app):
    registry = app.state.task_queue._tool_executor._registry
    write, echo = WriteTool(), EchoTool()
    registry.register(write)
    registry.register(echo)
    return write, echo


@pytest.fixture
def published(app, db_path):
    """Every `task.*` bus event, with the database state seen at the moment it was published."""
    seen: list[dict] = []
    bus = app.state.event_bus
    original = bus.publish

    async def spy(event_type, payload=None):
        if event_type.startswith("task."):
            with sqlite3.connect(db_path) as conn:  # a separate connection: committed data only
                rows = conn.execute(
                    "SELECT rowid FROM task_events WHERE task_id = ?",
                    ((payload or {}).get("task_id"),),
                ).fetchall()
                task = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", ((payload or {}).get("task_id"),)
                ).fetchone()
            seen.append(
                {
                    "type": event_type,
                    "payload": dict(payload or {}),
                    "committed_seqs": {r[0] for r in rows},
                    "task_status": task[0] if task else None,
                }
            )
        return await original(event_type, payload)

    bus.publish = spy
    return seen


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


def run_write_task(client, app, text="hello"):
    """Submit a task whose one call needs approval; approve it; wait for the answer."""
    script(app, tool_reply(("write", {"text": text}, "call_1")), text_reply("Done."))
    task_id = client.post("/v1/tasks", json={"user_request": "Write it"}).json()["id"]
    task = wait_for(client, task_id, waiting_step)
    step_id = next(s["id"] for s in task["steps"] if s["status"] == "waiting_for_approval")
    assert client.post(f"/v1/tasks/{task_id}/steps/{step_id}/approve", json={}).status_code == 200
    return task_id, wait_for(client, task_id, lambda t: t["status"] == "completed")


# --- Response fields ------------------------------------------------------------------------


def test_steps_carry_the_tool_call_id_risk_and_the_full_input(client, app, tools):
    long_text = "x" * 5000
    script(
        app,
        tool_reply(("write", {"text": long_text}, "call_w"), ("echo", {"text": "hi"}, "call_e")),
        text_reply("Done."),
    )
    task_id = client.post("/v1/tasks", json={"user_request": "Do both"}).json()["id"]
    task = wait_for(client, task_id, waiting_step)

    write_step = next(s for s in task["steps"] if s["tool_name"] == "write")
    echo_step = next(s for s in task["steps"] if s["tool_name"] == "echo")
    assert write_step["tool_call_id"] == "call_w"
    assert write_step["risk_level"] in {"medium", "high", "critical"}
    assert json.loads(write_step["input_json"]) == {"text": long_text}  # nothing truncated
    assert echo_step["tool_call_id"] == "call_e"
    assert echo_step["risk_level"] is None  # it ran without needing an approval request


def test_the_step_returned_by_approve_carries_its_risk_level(client, app, tools):
    script(app, tool_reply(("write", {"text": "a"}, "call_1")), text_reply("ok"))
    task_id = client.post("/v1/tasks", json={"user_request": "Write a"}).json()["id"]
    task = wait_for(client, task_id, waiting_step)
    step_id = next(s["id"] for s in task["steps"] if s["status"] == "waiting_for_approval")

    step = client.post(f"/v1/tasks/{task_id}/steps/{step_id}/approve", json={}).json()

    assert step["tool_call_id"] == "call_1"
    assert step["risk_level"] in {"medium", "high", "critical"}


def test_the_final_answer_is_on_the_task_in_detail_and_in_the_list(client, app, tools):
    task_id, task = run_write_task(client, app)

    assert task["final_answer"] == "Done."
    listed = next(t for t in client.get("/v1/tasks").json() if t["id"] == task_id)
    assert listed["final_answer"] == "Done."


# --- Bus events -------------------------------------------------------------------------------


def labels(published) -> list[str]:
    out = []
    for event in published:
        if event["type"] == "task.status_changed":
            out.append(f"status:{event['payload']['status']}")
        elif event["type"] != "task.completed":
            out.append(event["type"].removeprefix("task."))
    return out


def test_a_task_that_needs_one_approval_publishes_its_whole_story_in_order(
    client, app, tools, published
):
    run_write_task(client, app)
    time.sleep(0.3)  # the final events are published just after the task row flips

    assert labels(published) == [
        "status:running",
        "tool_call_proposed",
        "approval_required",
        "status:paused",
        "status:queued",
        "status:running",
        "tool_call_started",
        "tool_call_completed",
        "final_answer",
        "status:completed",
    ]


def test_event_payloads_name_the_task_step_and_call(client, app, tools, published):
    task_id, _ = run_write_task(client, app, text="payload check")
    time.sleep(0.3)
    by_type = {e["type"]: e["payload"] for e in published}

    for name in ("tool_call_proposed", "tool_call_started", "tool_call_completed"):
        payload = by_type[f"task.{name}"]
        assert payload["task_id"] == task_id
        assert payload["tool_call_id"] == "call_1" and payload["tool_name"] == "write"
        assert payload["step_id"]
    assert by_type["task.tool_call_completed"]["success"] is True
    needed = by_type["task.approval_required"]
    assert needed["approval_id"] and needed["risk_level"] in {"medium", "high", "critical"}
    assert needed["step_id"] == by_type["task.tool_call_proposed"]["step_id"]
    assert by_type["task.final_answer"]["answer"] == "Done."


def test_events_are_published_only_after_their_transaction_committed(
    client, app, tools, published
):
    run_write_task(client, app)
    time.sleep(0.3)

    assert published
    for event in published:
        seq = event["payload"].get("seq")
        if event["type"] == "task.completed":
            assert event["task_status"] == "completed"
        else:
            assert seq in event["committed_seqs"], f"{event['type']} was published before commit"
    completed = next(e for e in published if e["payload"].get("status") == "completed")
    assert completed["task_status"] == "completed"


def test_a_failed_call_is_published_as_a_completed_call_that_did_not_succeed(
    client, app, tools, published
):
    script(app, tool_reply(("no_such_tool", {}, "call_1")), text_reply("It does not exist."))
    task_id = client.post("/v1/tasks", json={"user_request": "Try"}).json()["id"]
    wait_for(client, task_id, lambda t: t["status"] == "completed")
    time.sleep(0.3)

    done = [e["payload"] for e in published if e["type"] == "task.tool_call_completed"]
    assert len(done) == 1 and done[0]["success"] is False and done[0]["error"]


def proposed_and_done(published, task_id):
    proposed = {
        e["payload"]["tool_call_id"]
        for e in published
        if e["type"] == "task.tool_call_proposed" and e["payload"]["task_id"] == task_id
    }
    done = {
        e["payload"]["tool_call_id"]: e["payload"]
        for e in published
        if e["type"] == "task.tool_call_completed" and e["payload"]["task_id"] == task_id
    }
    return proposed, done


def test_a_call_with_bad_arguments_gets_a_terminal_event(client, app, tools, published):
    bad = ToolCall(
        id="call_bad",
        name="echo",
        arguments={},
        error="The arguments were not valid JSON.",
        raw_arguments="{oops",
    )
    script(app, tool_reply(bad), text_reply("I will fix the arguments."))
    task_id = client.post("/v1/tasks", json={"user_request": "Try"}).json()["id"]
    wait_for(client, task_id, lambda t: t["status"] == "completed")
    time.sleep(0.3)

    proposed, done = proposed_and_done(published, task_id)
    assert proposed == {"call_bad"}
    assert done["call_bad"]["success"] is False and done["call_bad"]["error"]


def test_a_call_over_the_per_turn_limit_gets_a_terminal_event(client, app, tools, published):
    limit = app.state.task_queue._agent_loop._max_tool_calls_per_turn
    calls = [("echo", {"text": str(i)}, f"call_{i}") for i in range(limit + 1)]
    script(app, tool_reply(*calls), text_reply("Done."))
    task_id = client.post("/v1/tasks", json={"user_request": "Many"}).json()["id"]
    wait_for(client, task_id, lambda t: t["status"] == "completed")
    time.sleep(0.3)

    proposed, done = proposed_and_done(published, task_id)
    assert set(done) == proposed  # no call is left at "proposed"
    assert done[f"call_{limit}"]["success"] is False
    assert all(done[f"call_{i}"]["success"] is True for i in range(limit))


def test_a_denied_call_gets_a_terminal_event(client, app, tools, published):
    script(app, tool_reply(("write", {"text": "a"}, "call_1")), text_reply("Understood."))
    task_id = client.post("/v1/tasks", json={"user_request": "Write a"}).json()["id"]
    task = wait_for(client, task_id, waiting_step)
    step_id = next(s["id"] for s in task["steps"] if s["status"] == "waiting_for_approval")
    deny = client.post(f"/v1/tasks/{task_id}/steps/{step_id}/deny", json={"reason": "not now"})
    assert deny.status_code == 200
    wait_for(client, task_id, lambda t: t["status"] == "completed")
    time.sleep(0.3)

    proposed, done = proposed_and_done(published, task_id)
    assert proposed == {"call_1"}
    assert done["call_1"]["success"] is False
    assert "denied" in done["call_1"]["error"]


# --- Catch-up: GET /v1/tasks/{id}/events?since= -------------------------------------------------


def test_events_can_be_fetched_after_a_cursor(client, app, tools):
    task_id, _ = run_write_task(client, app)

    everything = client.get(f"/v1/tasks/{task_id}/events").json()
    seqs = [e["seq"] for e in everything]
    assert len(seqs) >= 8 and seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    after = client.get(f"/v1/tasks/{task_id}/events", params={"since": seqs[3]}).json()
    assert [e["seq"] for e in after] == seqs[4:]
    assert client.get(f"/v1/tasks/{task_id}/events", params={"since": seqs[-1]}).json() == []


def test_the_cursor_continues_where_the_websocket_left_off(client, app, tools, published):
    task_id, _ = run_write_task(client, app)
    time.sleep(0.3)
    streamed = sorted(e["payload"]["seq"] for e in published if "seq" in e["payload"])
    last_seen = streamed[2]  # the client dropped after the third event

    missed = client.get(f"/v1/tasks/{task_id}/events", params={"since": last_seen}).json()

    missed_seqs = [e["seq"] for e in missed]
    assert missed_seqs and min(missed_seqs) > last_seen
    # Everything the bus published after that point is in the catch-up (which also holds the
    # events that are logged but not relayed, such as approval_granted).
    assert set(streamed[3:]) <= set(missed_seqs)


def test_events_are_limited_and_paged_by_the_cursor(client, app, tools):
    task_id, _ = run_write_task(client, app)
    everything = client.get(f"/v1/tasks/{task_id}/events").json()

    page = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 3}).json()
    assert [e["seq"] for e in page] == [e["seq"] for e in everything[:3]]
    rest = client.get(
        f"/v1/tasks/{task_id}/events", params={"since": page[-1]["seq"], "limit": 500}
    ).json()
    assert [e["seq"] for e in page + rest] == [e["seq"] for e in everything]


def test_events_of_an_unknown_task_are_404_and_a_bad_cursor_is_422(client, app, tools):
    assert client.get("/v1/tasks/nope/events").status_code == 404
    task_id = client.post("/v1/tasks", json={"user_request": "x"}).json()["id"]
    assert client.get(f"/v1/tasks/{task_id}/events", params={"since": -1}).status_code == 422
    assert client.get(f"/v1/tasks/{task_id}/events", params={"since": "abc"}).status_code == 422
    assert client.get(f"/v1/tasks/{task_id}/events", params={"limit": 0}).status_code == 422


def test_events_need_the_api_token(client, app, tools):
    task_id = client.post("/v1/tasks", json={"user_request": "x"}).json()["id"]
    wrong = {"Authorization": "Bearer wrong-token"}
    assert client.get(f"/v1/tasks/{task_id}/events", headers=wrong).status_code == 401


def test_task_detail_events_carry_the_same_seq(client, app, tools):
    task_id, task = run_write_task(client, app)

    detail = [e["seq"] for e in task["events"]]
    assert detail == [e["seq"] for e in client.get(f"/v1/tasks/{task_id}/events").json()]

"""Phase 17 slice 4: the observe-act agent loop (jarvis/tasks/agent_loop.py), driven through
TaskQueue with a scripted model and dummy tools.

The loop is run directly (`_process_task`) so the tests do not wait for the worker's polling
interval; the tests that need the real worker start it explicitly. Approval and denial through
the HTTP endpoints are covered in test_agent_loop_api.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from _agent_helpers import (
    EchoTool,
    FailingTool,
    ScriptedModel,
    SlowTool,
    WriteTool,
    text_reply,
    tool_reply,
    wait_for_task_status,
    wait_until,
)

from jarvis.approvals.broker import ApprovalBroker
from jarvis.core.event_bus import EventBus
from jarvis.models.providers import RetryableProviderError
from jarvis.models.schemas import ToolCall
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.queue import TaskQueue
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry


@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "loop.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def tools():
    return {
        "echo": EchoTool(),
        "write": WriteTool(),
        "broken": FailingTool(),
        "slow": SlowTool(),
    }


@pytest.fixture
def registry(tools):
    registry = ToolRegistry()
    for tool in tools.values():
        registry.register(tool)
    return registry


def make_queue(uow, bus, registry, model, **limits) -> TaskQueue:
    broker = ApprovalBroker(uow, bus)
    return TaskQueue(uow, bus, model, ToolExecutor(registry, broker), broker, **limits)


async def new_task(uow, request: str = "Do the thing", **kwargs) -> str:
    async with uow.begin() as unit:
        return await unit.repositories.tasks.insert(
            title=kwargs.pop("title", "New Task"), user_request=request, **kwargs
        )


async def run_task(queue: TaskQueue, task_id: str) -> None:
    await queue._process_task({"id": task_id})


async def snapshot(uow, task_id: str) -> dict[str, Any]:
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        return {
            "task": await tasks.get(task_id),
            "steps": await tasks.list_steps(task_id),
            "messages": await tasks.list_messages(task_id),
            "events": await tasks.list_events(task_id),
        }


def roles(snap: dict[str, Any]) -> list[str]:
    return [m["role"] for m in snap["messages"]]


async def requeue(uow, task_id: str, *, from_statuses: tuple[str, ...]) -> None:
    async with uow.begin() as unit:
        assert await unit.repositories.tasks.transition(
            task_id, "queued", from_statuses=from_statuses
        )


async def approve_waiting_step(uow, bus, task_id: str) -> str:
    """What POST /v1/tasks/{id}/steps/{step}/approve does, for tests that do not use HTTP."""
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        (step,) = [s for s in await tasks.list_steps(task_id) if s["status"] == "waiting_for_approval"]
        request = await unit.repositories.approvals.get_latest_for_step(task_id, step["id"], "tool")
        assert await ApprovalBroker(uow, bus).approve(request["id"], unit=unit)
        assert await tasks.transition_step(
            step["id"], "approved", from_statuses=("waiting_for_approval",)
        )
        assert await tasks.transition(task_id, "queued", from_statuses=("paused",))
        return request["id"]


# --- The basic loop ----------------------------------------------------------------------------


async def test_tool_call_result_answer_is_persisted_and_completes(uow, bus, registry, tools):
    model = ScriptedModel(
        tool_reply(("echo", {"text": "hi"}, "call_a")),
        text_reply("The echo said hi."),
    )
    queue = make_queue(uow, bus, registry, model)
    completed = bus.subscribe()
    task_id = await new_task(uow, "  Say hi\nplease  ")

    await run_task(queue, task_id)

    snap = await snapshot(uow, task_id)
    task = snap["task"]
    assert (task["status"], task["final_answer"]) == ("completed", "The echo said hi.")
    assert task["title"] == "Say hi"  # derived from the request's first line
    assert task["completed_at"] is not None
    assert roles(snap) == ["system", "user", "assistant", "tool", "assistant"]
    assistant, tool_message = snap["messages"][2], snap["messages"][3]
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_a"]
    assert (tool_message["tool_call_id"], json.loads(tool_message["content"])) == (
        "call_a",
        {"echo": "hi"},
    )
    (step,) = snap["steps"]
    assert (step["status"], step["tool_name"], step["tool_call_id"]) == ("completed", "echo", "call_a")
    assert step["input_json"] == '{"text": "hi"}'  # canonical
    assert tools["echo"].calls == [{"text": "hi"}]
    assert {e["event_type"] for e in snap["events"]} >= {
        "tool_call_proposed", "step_started", "step_completed", "final_answer",
    }
    # The second model request carried the result back.
    assert model.roles(1) == ["system", "user", "assistant", "tool"]
    assert model.requests[1].messages[-1].tool_call_id == "call_a"
    event = await asyncio.wait_for(completed.get(), 1)
    assert (event.type, event.payload["task_id"]) == ("task.completed", task_id)


async def test_the_model_is_offered_the_registered_tools(uow, bus, registry):
    model = ScriptedModel(text_reply("ok"))
    await run_task(make_queue(uow, bus, registry, model), await new_task(uow))

    request = model.requests[0]
    assert {t["function"]["name"] for t in request.tools} == {"echo", "write", "broken", "slow"}
    assert request.tool_choice == "auto"


async def test_a_text_only_first_reply_completes_without_steps(uow, bus, registry):
    model = ScriptedModel(text_reply("4"))
    task_id = await new_task(uow, "What is 2+2?")

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == ("completed", "4")
    assert snap["steps"] == []
    assert roles(snap) == ["system", "user", "assistant"]


async def test_an_empty_reply_fails_the_task(uow, bus, registry):
    model = ScriptedModel(text_reply("   "))
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == ("failed", None)


async def test_max_turns_fails_the_task_and_never_completes_it(uow, bus, registry):
    model = ScriptedModel(*[tool_reply(("echo", {"text": str(n)})) for n in range(6)])
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model, max_turns=3), task_id)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == ("failed", None)
    assert len(model.requests) == 3
    failure = [e for e in snap["events"] if e["message"].startswith("Task failed")]
    assert "max_turns" in failure[0]["message"]


async def test_provider_failure_fails_the_task_and_resume_continues_the_conversation(
    uow, bus, registry
):
    first = ScriptedModel(tool_reply(("echo", {"text": "a"})), RetryableProviderError("502"))
    task_id = await new_task(uow)
    await run_task(make_queue(uow, bus, registry, first), task_id)
    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "failed"
    assert any("Model request failed: 502" in e["message"] for e in snap["events"])

    second = ScriptedModel(text_reply("recovered"))
    await requeue(uow, task_id, from_statuses=("failed",))
    await run_task(make_queue(uow, bus, registry, second), task_id)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == ("completed", "recovered")
    assert second.roles(0) == ["system", "user", "assistant", "tool"]  # history was reused


# --- Tool calls: batches, limits, bad calls ----------------------------------------------------


async def test_every_call_of_a_turn_is_stored_before_any_runs_and_answered_in_order(
    uow, bus, registry, tools
):
    model = ScriptedModel(
        tool_reply(
            ("slow", {}, "c1"),
            ("echo", {"text": "two"}, "c2"),
            ("echo", {"text": "three"}, "c3"),
        ),
        text_reply("done"),
    )
    queue = make_queue(uow, bus, registry, model)
    task_id = await new_task(uow)

    running = asyncio.create_task(run_task(queue, task_id))
    await asyncio.wait_for(tools["slow"].started.wait(), 5)
    during = await snapshot(uow, task_id)  # the first call is running; the others have not run
    assert [(s["tool_call_id"], s["status"]) for s in during["steps"]] == [
        ("c1", "running"), ("c2", "pending"), ("c3", "pending"),
    ]
    assert all(s["input_json"] is not None for s in during["steps"])
    assert roles(during) == ["system", "user", "assistant"]  # no result goes back early
    tools["slow"].release.set()
    await asyncio.wait_for(running, 5)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "completed"
    assert [s["step_index"] for s in snap["steps"]] == [0, 1, 2]
    assert roles(snap) == ["system", "user", "assistant", "tool", "tool", "tool", "assistant"]
    assert [m["tool_call_id"] for m in snap["messages"] if m["role"] == "tool"] == ["c1", "c2", "c3"]


async def test_calls_beyond_the_per_turn_limit_are_not_run_but_are_answered(
    uow, bus, registry, tools
):
    model = ScriptedModel(
        tool_reply(*[("echo", {"text": str(n)}, f"c{n}") for n in range(4)]),
        text_reply("done"),
    )
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model, max_tool_calls_per_turn=2), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "completed"
    assert [s["status"] for s in snap["steps"]] == ["completed", "completed", "skipped", "skipped"]
    assert tools["echo"].calls == [{"text": "0"}, {"text": "1"}]
    tool_messages = [m for m in snap["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c0", "c1", "c2", "c3"]
    assert tool_messages[2]["content"].startswith("Not run: more than 2 tool calls in one turn")


async def test_malformed_arguments_are_reported_to_the_model_not_run(uow, bus, registry, tools):
    bad = ToolCall(
        id="bad", name="echo", error="Tool arguments are not valid JSON: x", raw_arguments="{oops"
    )
    model = ScriptedModel(tool_reply(bad, ("echo", {"text": "ok"}, "good")), text_reply("fixed"))
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "completed"
    assert [s["status"] for s in snap["steps"]] == ["failed", "completed"]
    assert tools["echo"].calls == [{"text": "ok"}]  # the good call still ran
    content = next(m["content"] for m in snap["messages"] if m["tool_call_id"] == "bad")
    assert content.startswith("Error: Tool arguments are not valid JSON") and "{oops" in content


async def test_unknown_tool_and_tool_failure_are_information_not_task_failure(
    uow, bus, registry
):
    model = ScriptedModel(
        tool_reply(("no_such_tool", {}, "c1"), ("broken", {}, "c2")),
        text_reply("Neither worked, here is why."),
    )
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == (
        "completed", "Neither worked, here is why.",
    )
    assert [s["status"] for s in snap["steps"]] == ["failed", "failed"]
    results = {m["tool_call_id"]: m["content"] for m in snap["messages"] if m["role"] == "tool"}
    assert "Error: Tool not found: no_such_tool" in results["c1"]
    assert results["c2"] == "Error: the disk is on fire"


async def test_a_call_id_reused_by_the_model_gets_a_fresh_id(uow, bus, registry):
    model = ScriptedModel(
        tool_reply(("echo", {"text": "1"}, "same")),
        tool_reply(("echo", {"text": "2"}, "same")),
        text_reply("done"),
    )
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "completed"
    ids = [s["tool_call_id"] for s in snap["steps"]]
    assert ids[0] == "same" and ids[1] != "same" and len(set(ids)) == 2
    second_call = snap["messages"][4]["tool_calls"][0]["id"]
    assert snap["messages"][5]["tool_call_id"] == second_call == ids[1]


async def test_a_huge_tool_result_is_truncated(uow, bus, registry):
    big = "x" * 50_000
    model = ScriptedModel(tool_reply(("echo", {"text": big}, "c1")), text_reply("done"))
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    content = next(m["content"] for m in snap["messages"] if m["role"] == "tool")
    assert len(content) < 21_000 and content.endswith("... [truncated]")
    assert len(snap["steps"][0]["output_json"]) > 50_000  # the step keeps the full output


# --- Memory context ----------------------------------------------------------------------------


async def test_memory_context_and_precedence_rules_are_in_the_system_prompt(uow, bus, registry):
    model = ScriptedModel(text_reply("ok"))
    context = "### SYSTEM CONTEXT & MEMORIES\n[Decision] (Project) Always use pathlib"
    task_id = await new_task(uow)
    published = bus.subscribe()

    with patch(
        "jarvis.tasks.queue.MemoryStore.get_planner_context", return_value=(context, ["m1"])
    ):
        await run_task(make_queue(uow, bus, registry, model), task_id)

    system = model.requests[0].messages[0]
    assert system.role == "system"
    assert "Decision Precedence" in system.content and "Conflict Resolution" in system.content
    assert "Always use pathlib" in system.content
    event = await asyncio.wait_for(published.get(), 1)
    assert (event.type, event.payload) == ("memory.retrieved", {"memory_ids": ["m1"]})


# --- Cancellation, pausing and resuming --------------------------------------------------------


async def test_cancelling_during_a_tool_call_stops_the_loop(uow, bus, registry, tools):
    model = ScriptedModel(tool_reply(("slow", {}, "c1")), text_reply("must not be asked"))
    queue = make_queue(uow, bus, registry, model)
    task_id = await new_task(uow)

    running = asyncio.create_task(run_task(queue, task_id))
    await asyncio.wait_for(tools["slow"].started.wait(), 5)
    async with uow.begin() as unit:
        assert await unit.repositories.tasks.transition(
            task_id, "cancelled", from_statuses=("running",)
        )
    tools["slow"].release.set()
    await asyncio.wait_for(running, 5)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == ("cancelled", None)
    assert roles(snap) == ["system", "user", "assistant"]  # no result, no further turn
    assert model.remaining == 1


async def test_a_cancel_after_the_last_check_stops_the_call_before_it_runs(
    uow, bus, registry, tools
):
    # Review finding (P3): a cancel landing between the loop's "still running?" check and the
    # step being marked running used to let the call run anyway. Starting a call must be
    # conditional on the task still running, in the same transaction.
    from jarvis.tasks.agent_loop import AgentLoop

    model = ScriptedModel(
        tool_reply(("echo", {"text": "hi"}, "c1")), text_reply("must not be asked")
    )
    queue = make_queue(uow, bus, registry, model)
    task_id = await new_task(uow)
    original = AgentLoop._still_running

    async def check_then_cancel(self, tid):
        still = await original(self, tid)
        async with uow.begin() as unit:
            await unit.repositories.tasks.transition(tid, "cancelled", from_statuses=("running",))
        return still

    with patch.object(AgentLoop, "_still_running", check_then_cancel):
        await asyncio.wait_for(run_task(queue, task_id), 5)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "cancelled"
    assert tools["echo"].calls == []
    assert [s["status"] for s in snap["steps"]] != ["running"]
    assert all(s["status"] != "completed" for s in snap["steps"])
    assert model.remaining == 1


async def test_cancelling_while_the_model_answers_discards_the_answer(uow, bus, registry):
    task_id = await new_task(uow)

    async def cancel_then_answer(request):
        async with uow.begin() as unit:
            await unit.repositories.tasks.transition(
                task_id, "cancelled", from_statuses=("running",)
            )
        return text_reply("too late")

    await run_task(make_queue(uow, bus, registry, ScriptedModel(cancel_then_answer)), task_id)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == ("cancelled", None)
    assert roles(snap) == ["system", "user"]


async def test_a_mutating_call_pauses_the_task_and_frees_the_worker(uow, bus, registry, tools):
    model = ScriptedModel(
        tool_reply(("write", {"text": "x"}, "w1")),  # paused here
        text_reply("the other task's answer"),
    )
    queue = make_queue(uow, bus, registry, model)
    paused_id = await new_task(uow, "Change something")
    other_id = await new_task(uow, "Just answer", priority=200)

    await queue.start()
    try:
        paused = await wait_for_task_status(uow, paused_id, "paused")
        # The single worker is free again: the next queued task runs while the first waits.
        other = await wait_for_task_status(uow, other_id, "completed", timeout=15)
    finally:
        await queue.stop()

    assert other["final_answer"] == "the other task's answer"
    snap = await snapshot(uow, paused_id)
    assert paused["status"] == "paused"
    (step,) = snap["steps"]
    assert step["status"] == "waiting_for_approval"
    assert tools["write"].calls == []
    assert roles(snap) == ["system", "user", "assistant"]
    async with uow.begin() as unit:
        request = await unit.repositories.approvals.get_latest_for_step(paused_id, step["id"], "tool")
    assert request["status"] == "pending"
    assert request["action_json"] == '{"text": "x"}'


async def test_approving_resumes_from_the_stored_conversation_after_a_restart(
    uow, bus, registry, tools
):
    before_restart = ScriptedModel(tool_reply(("write", {"text": "x"}, "w1")))
    task_id = await new_task(uow, "Change something")
    await run_task(make_queue(uow, bus, registry, before_restart), task_id)
    assert (await snapshot(uow, task_id))["task"]["status"] == "paused"

    # A new process: new queue, new model, nothing in memory. Only the database is shared.
    after_restart = ScriptedModel(text_reply("Changed it."))
    queue = make_queue(uow, bus, registry, after_restart)
    approval_id = await approve_waiting_step(uow, bus, task_id)
    await run_task(queue, task_id)

    snap = await snapshot(uow, task_id)
    assert (snap["task"]["status"], snap["task"]["final_answer"]) == ("completed", "Changed it.")
    assert tools["write"].calls == [{"text": "x"}]  # ran exactly once, after approval
    assert after_restart.roles(0) == ["system", "user", "assistant", "tool"]
    assert json.loads(after_restart.requests[0].messages[-1].content) == {"written": "x"}
    async with uow.begin() as unit:
        assert await unit.repositories.approvals.is_consumed(approval_id)  # used up by that run


async def test_a_resumed_turn_runs_only_the_steps_that_had_not_finished(uow, bus, registry, tools):
    model = ScriptedModel(
        tool_reply(("echo", {"text": "before"}, "c1"), ("write", {"text": "w"}, "c2"),
                   ("echo", {"text": "after"}, "c3")),
    )
    task_id = await new_task(uow)
    await run_task(make_queue(uow, bus, registry, model), task_id)
    assert (await snapshot(uow, task_id))["task"]["status"] == "paused"
    assert tools["echo"].calls == [{"text": "before"}]  # c3 waits behind the paused c2

    await approve_waiting_step(uow, bus, task_id)
    resumed = ScriptedModel(text_reply("all done"))
    await run_task(make_queue(uow, bus, registry, resumed), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "completed"
    assert tools["echo"].calls == [{"text": "before"}, {"text": "after"}]  # "before" not repeated
    assert [m["tool_call_id"] for m in snap["messages"] if m["role"] == "tool"] == ["c1", "c2", "c3"]


async def test_an_interrupted_task_resumes_and_the_model_sees_the_interrupted_call(
    uow, bus, registry, tools
):
    first = ScriptedModel(tool_reply(("write", {"text": "x"}, "w1")))
    task_id = await new_task(uow)
    await run_task(make_queue(uow, bus, registry, first), task_id)
    await approve_waiting_step(uow, bus, task_id)
    # The daemon died while the approved step was running (what startup recovery leaves behind).
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        (step,) = await tasks.list_steps(task_id)
        await tasks.update_step(step["id"], status="running")
        await tasks.update(task_id, status="running")
        await tasks.fail_running_steps(task_id, error="Interrupted by daemon restart while running.")
        await tasks.update(task_id, status="interrupted")

    second = ScriptedModel(text_reply("It was interrupted; nothing was confirmed."))
    await requeue(uow, task_id, from_statuses=("interrupted",))
    await run_task(make_queue(uow, bus, registry, second), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "completed"
    assert tools["write"].calls == []  # not silently run a second time
    assert "Error: Interrupted by daemon restart" in second.requests[0].messages[-1].content


async def test_a_step_left_running_is_failed_not_rerun(uow, bus, registry, tools):
    # A crash that startup recovery never saw: the step is 'running', and no result reached
    # the conversation.
    task_id = await new_task(uow, status="queued")
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        await tasks.append_message(task_id, role="system", content="rules")
        await tasks.append_message(task_id, role="user", content="go")
        await tasks.append_message(
            task_id,
            role="assistant",
            tool_calls=[{"id": "c1", "name": "echo", "arguments": {"text": "x"}}],
        )
        await tasks.insert_step(
            task_id=task_id, step_index=0, title="echo", tool_name="echo",
            input_json='{"text": "x"}', status="running", tool_call_id="c1",
        )
    model = ScriptedModel(text_reply("ok"))

    await run_task(make_queue(uow, bus, registry, model), task_id)

    assert tools["echo"].calls == []  # a possibly half-done action is never repeated on its own
    assert "Error: Interrupted before it finished" in model.requests[0].messages[-1].content
    assert (await snapshot(uow, task_id))["task"]["status"] == "completed"


async def test_a_task_from_the_retired_planner_fails_with_a_clear_message(uow, bus, registry):
    task_id = await new_task(uow, "old")
    async with uow.begin() as unit:
        await unit.repositories.tasks.insert_step(task_id=task_id, step_index=0, title="old step")
    model = ScriptedModel()

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "failed" and model.requests == []
    assert any("retired one-shot planner" in e["message"] for e in snap["events"])


async def test_an_unexpected_error_fails_the_task(uow, bus, registry):
    model = ScriptedModel(RuntimeError("boom"))
    task_id = await new_task(uow)

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "failed"
    assert any("Unexpected error: boom" in e["message"] for e in snap["events"])


async def test_a_task_that_is_not_queued_is_left_alone(uow, bus, registry):
    model = ScriptedModel()
    task_id = await new_task(uow, status="cancelled")

    await run_task(make_queue(uow, bus, registry, model), task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "cancelled" and snap["messages"] == []


async def test_shutdown_between_turns_interrupts_the_task(uow, bus, registry):
    model = ScriptedModel(tool_reply(("echo", {"text": "x"})), text_reply("unreachable"))
    queue = make_queue(uow, bus, registry, model)
    queue._stop_event.set()  # the daemon is shutting down
    task_id = await new_task(uow)

    await run_task(queue, task_id)

    snap = await snapshot(uow, task_id)
    assert snap["task"]["status"] == "interrupted" and model.requests == []


async def test_the_worker_picks_up_a_queued_task_and_finishes_it(uow, bus, registry):
    model = ScriptedModel(text_reply("hello"))
    queue = make_queue(uow, bus, registry, model)
    task_id = await new_task(uow)

    await queue.start()
    try:
        task = await wait_for_task_status(uow, task_id, "completed", timeout=15)
        assert task["claimed_at"] is not None and task["started_at"] is not None
    finally:
        await queue.stop()
    await wait_until(lambda: model.remaining == 0)

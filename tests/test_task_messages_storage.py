"""Phase 17 slice 3: the conversation, final answer and tool-call ids stored for the agent loop.

Covers TaskRepository (jarvis/storage/repositories.py) on a fully migrated database.
"""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "messages.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)


async def _new_task(unit, status: str = "queued") -> str:
    return await unit.repositories.tasks.insert(title="t", user_request="do it", status=status)


async def test_messages_are_numbered_per_task_and_listed_in_order(uow) -> None:
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        first, second = await _new_task(unit), await _new_task(unit)

        assert await tasks.append_message(first, role="system", content="rules") == 0
        assert await tasks.append_message(first, role="user", content="hello") == 1
        assert await tasks.append_message(second, role="user", content="other") == 0  # own count
        assert await tasks.append_message(first, role="assistant", content="hi") == 2

        messages = await tasks.list_messages(first)

    assert [(m["seq"], m["role"], m["content"]) for m in messages] == [
        (0, "system", "rules"),
        (1, "user", "hello"),
        (2, "assistant", "hi"),
    ]
    assert all(m["tool_calls"] is None and m["tool_call_id"] is None for m in messages)


async def test_a_tool_round_trip_is_stored_and_decoded(uow) -> None:
    calls = [
        {"id": "call_1", "name": "read_file", "arguments": {"path": "a.txt"}},
        {"id": "call_2", "name": "git", "arguments": {"operation": "status"}},
    ]
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        task = await _new_task(unit)
        await tasks.append_message(task, role="user", content="go")
        await tasks.append_message(task, role="assistant", content="", tool_calls=calls)
        await tasks.append_message(task, role="tool", content="hello", tool_call_id="call_1")
        await tasks.append_message(task, role="tool", content="clean", tool_call_id="call_2")

    async with uow.begin() as unit:
        messages = await unit.repositories.tasks.list_messages(task)

    assert messages[1]["tool_calls"] == calls  # round-trips as data, not as a JSON string
    assert [m["tool_call_id"] for m in messages] == [None, None, "call_1", "call_2"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": "tool", "content": "x"},  # a result must say which call it answers
        {"role": "user", "content": "x", "tool_call_id": "call_1"},
        {"role": "user", "content": "x", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "content": "x", "tool_call_id": "c", "tool_calls": [{"id": "c"}]},
    ],
)
async def test_inconsistent_messages_are_refused_before_they_are_stored(uow, kwargs) -> None:
    async with uow.begin() as unit:
        task = await _new_task(unit)
        with pytest.raises(ValueError):
            await unit.repositories.tasks.append_message(task, **kwargs)
        assert await unit.repositories.tasks.list_messages(task) == []


async def test_an_unknown_role_or_task_is_rejected_by_the_database(uow) -> None:
    async with uow.begin() as unit:
        task = await _new_task(unit)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            await unit.repositories.tasks.append_message(task, role="narrator", content="x")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            await unit.repositories.tasks.append_message("no-such-task", role="user", content="x")


async def test_the_conversation_survives_a_new_unit_of_work_and_goes_with_its_task(uow) -> None:
    async with uow.begin() as unit:
        task = await _new_task(unit)
        await unit.repositories.tasks.append_message(task, role="user", content="persisted")

    async with uow.begin() as unit:  # as after a restart: nothing is cached in memory
        assert [m["content"] for m in await unit.repositories.tasks.list_messages(task)] == [
            "persisted"
        ]
        await unit.connection.execute("DELETE FROM tasks WHERE id = ?", (task,))
        assert await unit.repositories.tasks.list_messages(task) == []


async def test_final_answer_is_set_with_the_status_transition(uow) -> None:
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        task = await _new_task(unit, status="running")
        assert (await tasks.get(task))["final_answer"] is None

        # A stale caller cannot complete a task that is no longer running...
        assert not await tasks.transition(
            task, "completed", from_statuses=("paused",), final_answer="late"
        )
        assert (await tasks.get(task))["final_answer"] is None
        # ...and the answer lands in the same statement as the status.
        assert await tasks.transition(
            task, "completed", from_statuses=("running",), final_answer="All done."
        )
        stored = await tasks.get(task)

    assert (stored["status"], stored["final_answer"]) == ("completed", "All done.")


async def test_final_answer_can_be_updated_directly(uow) -> None:
    async with uow.begin() as unit:
        task = await _new_task(unit)
        assert await unit.repositories.tasks.update(task, final_answer="text")
        assert (await unit.repositories.tasks.get(task))["final_answer"] == "text"


async def test_steps_remember_the_call_they_run_and_the_id_is_unique_within_a_task(uow) -> None:
    async with uow.begin() as unit:
        tasks = unit.repositories.tasks
        task, other = await _new_task(unit), await _new_task(unit)

        step = await tasks.insert_step(
            task_id=task, step_index=0, title="read", tool_name="read_file", tool_call_id="call_1"
        )
        plain = await tasks.insert_step(task_id=task, step_index=1, title="no call")
        await tasks.insert_step(task_id=other, step_index=0, title="same id", tool_call_id="call_1")

        found = await tasks.get_step_by_tool_call_id(task, "call_1")
        assert found is not None and found["id"] == step and found["tool_call_id"] == "call_1"
        assert (await tasks.get_step(plain))["tool_call_id"] is None
        assert await tasks.get_step_by_tool_call_id(task, "nope") is None
        assert (await tasks.get_step_by_tool_call_id(other, "call_1"))["id"] != step

        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            await tasks.insert_step(
                task_id=task, step_index=2, title="dup", tool_call_id="call_1"
            )

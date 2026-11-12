"""Phase 17, slice 5: a UnitOfWork hands the task events it wrote to `on_commit`, after the commit
and never when the transaction rolled back. The daemon uses this to publish them on the bus."""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "uow.sqlite"
    async with sqlite_connection(path) as conn:
        await run_migrations(conn)
    return path


async def new_task(uow: UnitOfWork) -> str:
    async with uow.begin() as unit:
        return await unit.repositories.tasks.insert(
            title="t", user_request="r", project_id=None, priority=100
        )


async def test_recorded_events_reach_the_hook_after_the_commit(db_path):
    delivered: list[list[dict]] = []
    visible: list[int] = []

    async def on_commit(events: list[dict]) -> None:
        with sqlite3.connect(db_path) as conn:  # separate connection: sees committed data only
            visible.append(conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
        delivered.append(events)

    uow = UnitOfWork(db_path, on_commit=on_commit)
    task_id = await new_task(uow)
    async with uow.begin() as unit:
        await unit.repositories.tasks.insert_event(
            task_id=task_id, event_type="status_change", message="m", payload={"status": "queued"}
        )
        assert delivered == []  # nothing yet: still inside the transaction

    assert len(delivered) == 1 and visible == [1]
    event = delivered[0][0]
    assert event["task_id"] == task_id and event["event_type"] == "status_change"
    assert event["payload"] == {"status": "queued"} and event["message"] == "m"
    assert isinstance(event["seq"], int) and event["seq"] > 0


async def test_a_rolled_back_transaction_delivers_nothing(db_path):
    delivered: list[list[dict]] = []

    async def on_commit(events: list[dict]) -> None:
        delivered.append(events)

    uow = UnitOfWork(db_path, on_commit=on_commit)
    task_id = await new_task(uow)
    with pytest.raises(RuntimeError):
        async with uow.begin() as unit:
            await unit.repositories.tasks.insert_event(
                task_id=task_id, event_type="status_change", payload={"status": "x"}
            )
            raise RuntimeError("boom")

    assert delivered == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0


async def test_a_transaction_without_task_events_does_not_call_the_hook(db_path):
    calls = 0

    async def on_commit(events: list[dict]) -> None:
        nonlocal calls
        calls += 1

    uow = UnitOfWork(db_path, on_commit=on_commit)
    await new_task(uow)

    assert calls == 0


async def test_a_failing_hook_does_not_undo_or_fail_the_committed_work(db_path):
    async def on_commit(events: list[dict]) -> None:
        raise RuntimeError("subscriber exploded")

    uow = UnitOfWork(db_path, on_commit=on_commit)
    task_id = await new_task(uow)
    async with uow.begin() as unit:
        await unit.repositories.tasks.insert_event(
            task_id=task_id, event_type="status_change", payload={}
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1


async def test_a_unit_of_work_without_a_hook_works_as_before(db_path):
    uow = UnitOfWork(db_path)
    task_id = await new_task(uow)
    async with uow.begin() as unit:
        await unit.repositories.tasks.insert_event(
            task_id=task_id, event_type="status_change", payload={}
        )
        events = await unit.repositories.tasks.list_events(task_id)
    assert [e["event_type"] for e in events] == ["status_change"]
    assert events[0]["seq"] > 0

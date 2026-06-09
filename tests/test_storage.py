from __future__ import annotations

from pathlib import Path

from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork


async def test_migrations_create_database_and_are_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite"

    async with sqlite_connection(database_path) as connection:
        applied_first = await run_migrations(connection)
        applied_second = await run_migrations(connection)

    assert database_path.exists()
    assert applied_first == [1]
    assert applied_second == []


async def test_app_state_and_audit_repositories(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite"

    async with UnitOfWork(database_path) as unit:
        assert unit.repositories is not None
        await unit.repositories.app_state.set("current_project_id", {"id": "project-1"})
        audit_id = await unit.repositories.audit.insert(
            actor="test",
            action_type="phase0.test",
            summary="Inserted audit row.",
            details={"ok": True},
        )

    async with UnitOfWork(database_path) as unit:
        assert unit.repositories is not None
        value = await unit.repositories.app_state.get("current_project_id")

    assert value == {"id": "project-1"}
    assert audit_id


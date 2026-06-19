from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.projects.registry import ProjectRegistry
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.workspaces.registry import WorkspaceRegistry


from jarvis.storage.migrations import run_migrations

@pytest.fixture
async def uow(tmp_path: Path) -> UnitOfWork:
    db_path = tmp_path / "memory.sqlite"
    # Ensure migrations are run since UnitOfWork no longer does it implicitly
    from jarvis.storage.connection import open_sqlite_connection
    conn = await open_sqlite_connection(db_path)
    try:
        await run_migrations(conn)
    finally:
        await conn.close()
    return UnitOfWork(db_path)


async def test_workspace_registry(uow: UnitOfWork) -> None:
    registry = WorkspaceRegistry(uow)
    
    w_id = await registry.add("Test Workspace", "C:\\Test")
    workspaces = await registry.list()
    
    assert len(workspaces) == 1
    assert workspaces[0]["name"] == "Test Workspace"
    
    # Verify audit log
    async with uow.begin() as unit:
        cursor = await unit.connection.execute("SELECT action_type FROM audit_log WHERE target = ?", (w_id,))
        row = await cursor.fetchone()
        assert row["action_type"] == "workspace.add"

    assert await registry.remove(w_id) is True
    assert len(await registry.list()) == 0


async def test_project_registry_and_persistence(uow: UnitOfWork) -> None:
    registry = ProjectRegistry(uow)
    
    p_id = await registry.create("Test Project")
    assert await registry.get_current_id() is None
    
    await registry.switch_current(p_id)
    assert await registry.get_current_id() == p_id
    
    # Verify persistence survives "restart" (new registry instance)
    new_registry = ProjectRegistry(uow)
    assert await new_registry.get_current_id() == p_id
    
    await registry.switch_current(None)
    assert await registry.get_current_id() is None


async def test_workspace_registry_normalization_and_lookup(uow: UnitOfWork) -> None:
    registry = WorkspaceRegistry(uow)
    
    # Path normalization
    w_id = await registry.add("W1", "C:\\temp\\..\\workspace")
    workspaces = await registry.list()
    
    assert len(workspaces) == 1
    # On Windows, resolve() might change capitalization or add drive letter details
    # but it will be consistent.
    normalized = workspaces[0]["path"]
    assert ".." not in normalized
    
    # Duplicate registration of same normalized path should fail
    with pytest.raises(ValueError, match="already registered"):
        await registry.add("W2", "C:\\workspace")

    # Lookups
    assert (await registry.get(w_id))["name"] == "W1"
    assert (await registry.get_by_path("C:\\workspace"))["id"] == w_id
    
    # Update
    assert await registry.update(w_id, enabled=False, name="New Name") is True
    updated = await registry.get(w_id)
    assert updated["enabled"] == 0
    assert updated["name"] == "New Name"


async def test_project_registry_hardening(uow: UnitOfWork) -> None:
    registry = ProjectRegistry(uow)
    w_reg = WorkspaceRegistry(uow)
    
    p_id = await registry.create("P1")
    w_id = await w_reg.add("W1", "C:\\W1")
    
    # Lookups
    assert (await registry.get(p_id))["name"] == "P1"
    assert (await registry.get_by_name("P1"))["id"] == p_id
    
    # Update
    assert await registry.update(p_id, description="Desc") is True
    assert (await registry.get(p_id))["description"] == "Desc"
    
    # Unlink
    await registry.link_workspace(p_id, w_id)
    assert await registry.unlink_workspace(p_id, w_id) is True
    async with uow.begin() as unit:
        assert len(await unit.repositories.projects.list_workspaces(p_id)) == 0


async def test_project_delete_safety(uow: UnitOfWork) -> None:
    registry = ProjectRegistry(uow)
    p_id = await registry.create("P1")
    
    await registry.switch_current(p_id)
    assert await registry.get_current_id() == p_id
    
    # Delete current project
    await registry.delete(p_id)
    
    # app_state should be cleared
    assert await registry.get_current_id() is None
    
    # Verify audit log for the clear
    async with uow.begin() as unit:
        cursor = await unit.connection.execute(
            "SELECT summary FROM audit_log WHERE action_type = 'project.switch' ORDER BY created_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert "deleted" in row["summary"]

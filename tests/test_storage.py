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
    assert applied_first == [1, 2]
    assert applied_second == []


async def test_projects_and_workspaces_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite"

    async with sqlite_connection(database_path) as connection:
        await run_migrations(connection)

        # Verify tables exist
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('workspaces', 'projects', 'project_workspaces', 'project_notes', 'project_goals')"
        )
        tables = {row["name"] for row in await cursor.fetchall()}
        assert tables == {
            "workspaces",
            "projects",
            "project_workspaces",
            "project_notes",
            "project_goals",
        }


async def test_workspace_path_case_insensitivity(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite"

    async with sqlite_connection(database_path) as connection:
        await run_migrations(connection)

        # Insert a workspace with a specific case
        await connection.execute(
            "INSERT INTO workspaces (id, name, path, created_at, updated_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            ("w1", "Work", "C:\\Project",),
        )
        await connection.commit()

        # Attempt to insert a workspace with the same path but different case
        # This should fail due to UNIQUE COLLATE NOCASE
        import sqlite3
        import pytest
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute(
                "INSERT INTO workspaces (id, name, path, created_at, updated_at) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                ("w2", "Work2", "c:\\project",),
            )
            await connection.commit()


async def test_cascading_deletes(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite"

    async with sqlite_connection(database_path) as connection:
        await run_migrations(connection)
        await connection.execute("PRAGMA foreign_keys = ON")

        # Setup: Project with workspace, note, and goal
        await connection.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES ('p1', 'Project 1', 'now', 'now')"
        )
        await connection.execute(
            "INSERT INTO workspaces (id, name, path, created_at, updated_at) VALUES ('w1', 'W1', 'C:\\W1', 'now', 'now')"
        )
        await connection.execute(
            "INSERT INTO project_workspaces (project_id, workspace_id, created_at) VALUES ('p1', 'w1', 'now')"
        )
        await connection.execute(
            "INSERT INTO project_notes (id, project_id, body, created_at, updated_at) VALUES ('n1', 'p1', 'Note', 'now', 'now')"
        )
        await connection.execute(
            "INSERT INTO project_goals (id, project_id, title, created_at, updated_at) VALUES ('g1', 'p1', 'Goal', 'now', 'now')"
        )
        await connection.commit()

        # Delete project
        await connection.execute("DELETE FROM projects WHERE id = 'p1'")
        await connection.commit()

        # Verify cascades
        cursor = await connection.execute("SELECT count(*) FROM project_workspaces WHERE project_id = 'p1'")
        assert (await cursor.fetchone())[0] == 0
        cursor = await connection.execute("SELECT count(*) FROM project_notes WHERE project_id = 'p1'")
        assert (await cursor.fetchone())[0] == 0
        cursor = await connection.execute("SELECT count(*) FROM project_goals WHERE project_id = 'p1'")
        assert (await cursor.fetchone())[0] == 0


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


async def test_workspace_and_project_repositories(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite"

    async with UnitOfWork(database_path) as unit:
        assert unit.repositories is not None
        repo_w = unit.repositories.workspaces
        repo_p = unit.repositories.projects

        # Workspaces
        w1_id = await repo_w.insert(name="W1", path="C:\\W1")
        w2_id = await repo_w.insert(name="W2", path="C:\\W2", enabled=False)

        workspaces = await repo_w.list()
        assert len(workspaces) == 2
        w1 = await repo_w.get(w1_id)
        assert w1["name"] == "W1"
        assert w1["enabled"] == 1

        # Projects
        p1_id = await repo_p.insert(name="P1", description="First project")
        p2_id = await repo_p.insert(name="P2")

        projects = await repo_p.list()
        assert len(projects) == 2
        p1 = await repo_p.get(p1_id)
        assert p1["name"] == "P1"

        # Linking
        await repo_p.link_workspace(p1_id, w1_id)
        await repo_p.link_workspace(p1_id, w2_id)

        p1_workspaces = await repo_p.list_workspaces(p1_id)
        assert len(p1_workspaces) == 2
        assert {w["id"] for w in p1_workspaces} == {w1_id, w2_id}

        # Notes and Goals
        await repo_p.insert_note(p1_id, title="Note 1", body="Body 1")
        await repo_p.insert_goal(p1_id, title="Goal 1")

        notes = await repo_p.list_notes(p1_id)
        assert len(notes) == 1
        assert notes[0]["title"] == "Note 1"

        goals = await repo_p.list_goals(p1_id)
        assert len(goals) == 1
        assert goals[0]["title"] == "Goal 1"

        # Deletion
        assert await repo_p.delete(p1_id) is True
        assert await repo_p.get(p1_id) is None
        # Verify cascade indirectly (link should be gone)
        p1_workspaces_after = await repo_p.list_workspaces(p1_id)
        assert len(p1_workspaces_after) == 0
        
        # Verify note and goal cascade
        notes_after = await repo_p.list_notes(p1_id)
        assert len(notes_after) == 0


"""Storage repositories for Phase 0 tables."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import aiosqlite


class AppStateRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def get(self, key: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            "SELECT value_json FROM app_state WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        value = json.loads(str(row["value_json"]))
        if not isinstance(value, dict):
            raise ValueError("Stored app_state value must be a JSON object.")
        return value

    async def set(self, key: str, value: dict[str, object]) -> None:
        await self._connection.execute(
            """
            INSERT INTO app_state (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, sort_keys=True), _now()),
        )


class AuditRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def insert(
        self,
        *,
        actor: str,
        action_type: str,
        summary: str,
        target: str | None = None,
        details: dict[str, object] | None = None,
        task_id: str | None = None,
        approval_request_id: str | None = None,
    ) -> str:
        audit_id = str(uuid4())
        await self._connection.execute(
            """
            INSERT INTO audit_log (
                id, actor, action_type, target, summary, details_json, 
                task_id, approval_request_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                actor,
                action_type,
                target,
                summary,
                json.dumps(details or {}, sort_keys=True),
                task_id,
                approval_request_id,
                _now(),
            ),
        )
        return audit_id


class WorkspaceRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def insert(
        self,
        *,
        id: str | None = None,
        name: str,
        path: str,
        enabled: bool = True,
        read_policy: str = "auto_inside_workspace",
        write_policy: str = "approval_required",
    ) -> str:
        workspace_id = id or str(uuid4())
        now = _now()
        await self._connection.execute(
            """
            INSERT INTO workspaces (
                id, name, path, enabled, read_policy, write_policy, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                name,
                path,
                1 if enabled else 0,
                read_policy,
                write_policy,
                now,
                now,
            ),
        )
        return workspace_id

    async def get(self, workspace_id: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list(self) -> list[dict[str, object]]:
        cursor = await self._connection.execute("SELECT * FROM workspaces")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete(self, workspace_id: str) -> bool:
        cursor = await self._connection.execute(
            "DELETE FROM workspaces WHERE id = ?", (workspace_id,)
        )
        return cursor.rowcount > 0


class ProjectRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def insert(
        self,
        *,
        id: str | None = None,
        name: str,
        description: str | None = None,
        status: str = "active",
    ) -> str:
        project_id = id or str(uuid4())
        now = _now()
        await self._connection.execute(
            """
            INSERT INTO projects (id, name, description, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, name, description, status, now, now),
        )
        return project_id

    async def get(self, project_id: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list(self) -> list[dict[str, object]]:
        cursor = await self._connection.execute("SELECT * FROM projects")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete(self, project_id: str) -> bool:
        cursor = await self._connection.execute(
            "DELETE FROM projects WHERE id = ?", (project_id,)
        )
        return cursor.rowcount > 0

    async def link_workspace(self, project_id: str, workspace_id: str) -> None:
        await self._connection.execute(
            "INSERT INTO project_workspaces (project_id, workspace_id, created_at) VALUES (?, ?, ?)",
            (project_id, workspace_id, _now()),
        )

    async def list_workspaces(self, project_id: str) -> list[dict[str, object]]:
        cursor = await self._connection.execute(
            """
            SELECT w.* FROM workspaces w
            JOIN project_workspaces pw ON w.id = pw.workspace_id
            WHERE pw.project_id = ?
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def insert_note(self, project_id: str, body: str, title: str | None = None) -> str:
        note_id = str(uuid4())
        now = _now()
        await self._connection.execute(
            """
            INSERT INTO project_notes (id, project_id, title, body, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (note_id, project_id, title, body, now, now),
        )
        return note_id

    async def list_notes(self, project_id: str) -> list[dict[str, object]]:
        cursor = await self._connection.execute(
            "SELECT * FROM project_notes WHERE project_id = ?", (project_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def insert_goal(self, project_id: str, title: str, description: str | None = None) -> str:
        goal_id = str(uuid4())
        now = _now()
        await self._connection.execute(
            """
            INSERT INTO project_goals (id, project_id, title, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (goal_id, project_id, title, description, now, now),
        )
        return goal_id

    async def list_goals(self, project_id: str) -> list[dict[str, object]]:
        cursor = await self._connection.execute(
            "SELECT * FROM project_goals WHERE project_id = ?", (project_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


class StorageRepositories:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.app_state = AppStateRepository(connection)
        self.audit = AuditRepository(connection)
        self.workspaces = WorkspaceRepository(connection)
        self.projects = ProjectRepository(connection)


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

    async def get_by_path(self, path: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            "SELECT * FROM workspaces WHERE path = ? COLLATE NOCASE", (path,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update(self, workspace_id: str, **kwargs: object) -> bool:
        if not kwargs:
            return False
        
        allowed_keys = {"name", "enabled", "read_policy", "write_policy"}
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed_keys}
        if not filtered_kwargs:
            return False

        if "enabled" in filtered_kwargs:
            filtered_kwargs["enabled"] = 1 if filtered_kwargs["enabled"] else 0

        set_clause = ", ".join([f"{k} = ?" for k in filtered_kwargs.keys()])
        values = list(filtered_kwargs.values())
        values.append(_now())
        values.append(workspace_id)

        cursor = await self._connection.execute(
            f"UPDATE workspaces SET {set_clause}, updated_at = ? WHERE id = ?",
            tuple(values),
        )
        return cursor.rowcount > 0

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

    async def get_by_name(self, name: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list(self) -> list[dict[str, object]]:
        cursor = await self._connection.execute("SELECT * FROM projects")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update(self, project_id: str, **kwargs: object) -> bool:
        if not kwargs:
            return False
        
        allowed_keys = {"name", "description", "status"}
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed_keys}
        if not filtered_kwargs:
            return False

        set_clause = ", ".join([f"{k} = ?" for k in filtered_kwargs.keys()])
        values = list(filtered_kwargs.values())
        values.append(_now())
        values.append(project_id)

        cursor = await self._connection.execute(
            f"UPDATE projects SET {set_clause}, updated_at = ? WHERE id = ?",
            tuple(values),
        )
        return cursor.rowcount > 0

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

    async def unlink_workspace(self, project_id: str, workspace_id: str) -> bool:
        cursor = await self._connection.execute(
            "DELETE FROM project_workspaces WHERE project_id = ? AND workspace_id = ?",
            (project_id, workspace_id),
        )
        return cursor.rowcount > 0

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


class MemoryRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def insert_short_term(
        self,
        *,
        id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        source: str,
        role: str | None = None,
        content: str,
        tags: list[str] | None = None,
        importance: int = 0,
        expires_at: str | None = None,
    ) -> str:
        memory_id = id or str(uuid4())
        await self._connection.execute(
            """
            INSERT INTO short_term_context (
                id, project_id, task_id, source, role, content, tags_json, 
                importance, expires_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                project_id,
                task_id,
                source,
                role,
                content,
                json.dumps(tags or []),
                importance,
                expires_at,
                _now(),
            ),
        )
        return memory_id

    async def propose_long_term(
        self,
        *,
        id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        memory_type: str,
        proposed_content: str,
        proposed_tags: list[str] | None = None,
        reason: str,
    ) -> str:
        proposal_id = id or str(uuid4())
        await self._connection.execute(
            """
            INSERT INTO memory_proposals (
                id, project_id, task_id, memory_type, proposed_content, 
                proposed_tags_json, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                project_id,
                task_id,
                memory_type,
                proposed_content,
                json.dumps(proposed_tags or []),
                reason,
                _now(),
            ),
        )
        return proposal_id

    async def get_proposal(self, proposal_id: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        if row:
            data = dict(row)
            data["proposed_tags"] = json.loads(str(data.pop("proposed_tags_json")))
            return data
        return None

    async def update_proposal_status(
        self, proposal_id: str, status: str, decided_at: str | None = None
    ) -> bool:
        cursor = await self._connection.execute(
            "UPDATE memory_proposals SET status = ?, decided_at = ? WHERE id = ?",
            (status, decided_at or _now(), proposal_id),
        )
        return cursor.rowcount > 0

    async def insert_long_term(
        self,
        *,
        id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        memory_type: str,
        title: str | None = None,
        content: str,
        tags: list[str] | None = None,
        source: str,
    ) -> str:
        memory_id = id or str(uuid4())
        now = _now()
        await self._connection.execute(
            """
            INSERT INTO long_term_memory (
                id, project_id, task_id, memory_type, title, content, 
                tags_json, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                project_id,
                task_id,
                memory_type,
                title,
                content,
                json.dumps(tags or []),
                source,
                now,
                now,
            ),
        )
        return memory_id

    async def get_long_term(self, memory_id: str) -> dict[str, object] | None:
        cursor = await self._connection.execute(
            "SELECT * FROM long_term_memory WHERE id = ?", (memory_id,)
        )
        row = await cursor.fetchone()
        if row:
            data = dict(row)
            data["tags"] = json.loads(str(data.pop("tags_json")))
            return data
        return None

    async def search_long_term(
        self,
        query: str,
        *,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        sql = """
            SELECT m.*, rank
            FROM long_term_memory m
            JOIN long_term_memory_idx idx ON m.id = idx.id
            WHERE long_term_memory_idx MATCH ?
        """
        params: list[object] = [query]

        if project_id:
            sql += " AND m.project_id = ?"
            params.append(project_id)
        
        if memory_type:
            sql += " AND m.memory_type = ?"
            params.append(memory_type)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        cursor = await self._connection.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data["tags"] = json.loads(str(data.pop("tags_json")))
            results.append(data)
        return results

    async def delete_long_term(self, memory_id: str) -> bool:
        cursor = await self._connection.execute(
            "DELETE FROM long_term_memory WHERE id = ?", (memory_id,)
        )
        return cursor.rowcount > 0


class StorageRepositories:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.app_state = AppStateRepository(connection)
        self.audit = AuditRepository(connection)
        self.workspaces = WorkspaceRepository(connection)
        self.projects = ProjectRepository(connection)
        self.memory = MemoryRepository(connection)


def _now() -> str:
    return datetime.now(UTC).isoformat()


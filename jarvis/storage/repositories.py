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
    ) -> str:
        audit_id = str(uuid4())
        await self._connection.execute(
            """
            INSERT INTO audit_log (
                id, actor, action_type, target, summary, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                actor,
                action_type,
                target,
                summary,
                json.dumps(details or {}, sort_keys=True),
                _now(),
            ),
        )
        return audit_id


class StorageRepositories:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.app_state = AppStateRepository(connection)
        self.audit = AuditRepository(connection)


def _now() -> str:
    return datetime.now(UTC).isoformat()


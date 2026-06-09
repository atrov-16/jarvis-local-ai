"""SQLite migration runner and SQL migration resources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files

import aiosqlite

MIGRATION_NAME_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>.+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


async def run_migrations(connection: aiosqlite.Connection) -> list[int]:
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """
    )
    await connection.commit()

    applied = await _applied_versions(connection)
    applied_now: list[int] = []
    for migration in _load_migrations():
        if migration.version in applied:
            continue
        await connection.executescript(migration.sql)
        await connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, ?, datetime('now'))",
            (migration.version, migration.name),
        )
        await connection.commit()
        applied_now.append(migration.version)
    return applied_now


async def _applied_versions(connection: aiosqlite.Connection) -> set[int]:
    cursor = await connection.execute("SELECT version FROM schema_migrations")
    rows = await cursor.fetchall()
    return {int(row["version"]) for row in rows}


def _load_migrations() -> list[Migration]:
    migration_dir = files("jarvis.storage.migrations")
    migrations: list[Migration] = []
    for resource in migration_dir.iterdir():
        match = MIGRATION_NAME_PATTERN.match(resource.name)
        if match is None:
            continue
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=resource.read_text(encoding="utf-8"),
            )
        )
    return sorted(migrations, key=lambda migration: migration.version)

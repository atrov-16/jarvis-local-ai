"""SQLite connection helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from jarvis.config.models import JarvisConfig


def resolve_database_path(config: JarvisConfig) -> Path:
    return Path(config.memory.database_path)


async def open_sqlite_connection(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute("PRAGMA journal_mode = WAL")
    await connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@asynccontextmanager
async def sqlite_connection(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await open_sqlite_connection(path)
    try:
        yield connection
    finally:
        await connection.close()

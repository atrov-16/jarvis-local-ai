"""SQLite connection helpers."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from jarvis.config.models import JarvisConfig
from jarvis.config.paths import expand_path


def resolve_database_path(config: JarvisConfig) -> Path:
    """The absolute database path, so every component of the process opens the same file."""
    return expand_path(config.memory.database_path)


async def open_sqlite_connection(path: Path) -> aiosqlite.Connection:
    created_dir = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created_dir and os.name != "nt":
        path.parent.chmod(0o700)  # Jarvis data is private to the user (Windows: per-user ACLs)
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute("PRAGMA journal_mode = WAL")
    await connection.execute("PRAGMA synchronous = NORMAL")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return connection


@asynccontextmanager
async def sqlite_connection(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await open_sqlite_connection(path)
    try:
        yield connection
    finally:
        await connection.close()

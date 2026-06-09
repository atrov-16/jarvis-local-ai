"""Unit-of-work helper for transactional storage operations."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import aiosqlite

from jarvis.storage.connection import open_sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.repositories import StorageRepositories


class UnitOfWork:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self.connection: aiosqlite.Connection | None = None
        self.repositories: StorageRepositories | None = None

    async def __aenter__(self) -> UnitOfWork:
        self.connection = await open_sqlite_connection(self._database_path)
        await run_migrations(self.connection)
        self.repositories = StorageRepositories(self.connection)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.connection is None:
            return
        if exc_type is None:
            await self.connection.commit()
        else:
            await self.connection.rollback()
        await self.connection.close()

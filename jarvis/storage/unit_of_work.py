"""Unit-of-work helper for transactional storage operations."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import aiosqlite

from jarvis.storage.connection import open_sqlite_connection
from jarvis.storage.repositories import StorageRepositories


class UnitOfWork:
    """Factory for creating transactional storage scopes."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def begin(self) -> UnitOfWorkScope:
        """Begin a new unit of work scope."""
        return UnitOfWorkScope(self._database_path)

    @property
    def database_path(self) -> Path:
        return self._database_path

    async def __aenter__(self) -> UnitOfWorkScope:
        """Deprecated: Use begin() instead. Provided for transitional compatibility."""
        return await self.begin().__aenter__()


class UnitOfWorkScope:
    """A short-lived scope for a single transaction or operation."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self.connection: aiosqlite.Connection | None = None
        self.repositories: StorageRepositories | None = None

    async def __aenter__(self) -> UnitOfWorkScope:
        self.connection = await open_sqlite_connection(self._database_path)
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
        try:
            if exc_type is None:
                await self.connection.commit()
            else:
                await self.connection.rollback()
        finally:
            await self.connection.close()
            self.connection = None
            self.repositories = None

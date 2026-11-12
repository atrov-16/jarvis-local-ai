"""Unit-of-work helper for transactional storage operations."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType

import aiosqlite

from jarvis.storage.connection import open_sqlite_connection
from jarvis.storage.repositories import StorageRepositories

LOG = logging.getLogger(__name__)

# Called with the task events a transaction wrote, once that transaction has committed.
CommitHook = Callable[[list[dict[str, object]]], Awaitable[None]]


class UnitOfWork:
    """Factory for creating transactional storage scopes.

    `on_commit`, if given, receives the `task_events` rows a scope wrote, after its commit and
    never after a rollback. The daemon uses it to publish them on the event bus, so a client is
    never told about something that is not yet in the database.
    """

    def __init__(self, database_path: Path, *, on_commit: CommitHook | None = None) -> None:
        self._database_path = database_path
        self._on_commit = on_commit

    def begin(self) -> UnitOfWorkScope:
        """Begin a new unit of work scope."""
        return UnitOfWorkScope(self._database_path, self._on_commit)

    @property
    def database_path(self) -> Path:
        return self._database_path

    async def __aenter__(self) -> UnitOfWorkScope:
        """Deprecated: Use begin() instead. Provided for transitional compatibility."""
        return await self.begin().__aenter__()


class UnitOfWorkScope:
    """A short-lived scope for a single transaction or operation."""

    def __init__(self, database_path: Path, on_commit: CommitHook | None = None) -> None:
        self._database_path = database_path
        self._on_commit = on_commit
        # Task events written in this scope, in order; filled by the task repository.
        self._task_events: list[dict[str, object]] = []
        self.connection: aiosqlite.Connection | None = None
        self.repositories: StorageRepositories | None = None

    async def __aenter__(self) -> UnitOfWorkScope:
        self.connection = await open_sqlite_connection(self._database_path)
        self.repositories = StorageRepositories(
            self.connection, task_event_log=self._task_events
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.connection is None:
            return
        committed = False
        try:
            if exc_type is None:
                await self.connection.commit()
                committed = True
            else:
                await self.connection.rollback()
        finally:
            await self.connection.close()
            self.connection = None
            self.repositories = None
        if committed and self._task_events and self._on_commit is not None:
            events, self._task_events = self._task_events, []
            try:
                await self._on_commit(events)
            except Exception:
                # The work is committed; a failing subscriber must not undo or fail it.
                LOG.exception("The commit hook failed; the committed work is unaffected.")

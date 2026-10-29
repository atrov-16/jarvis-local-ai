"""F-08: reflection retries only transient failures, never changes the task, and shuts down
cleanly (the "Event loop is closed" warning from background reflection)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.core import reflection
from jarvis.core.event_bus import EventBus
from jarvis.core.reflection import ReflectionService
from jarvis.memory.store import MemoryStore
from jarvis.models.providers import (
    AuthenticationError,
    InsufficientCreditsError,
    MissingCredentialsError,
    RetryableProviderError,
)
from jarvis.models.schemas import Message, ModelResponse
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def no_wait(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(reflection.asyncio, "sleep", no_wait)
    return recorded


def _service(router: AsyncMock) -> ReflectionService:
    """A service whose reflection reaches the model with a non-trivial trace."""
    store = AsyncMock()
    store.get_planner_context.return_value = ("", [])
    service = ReflectionService(MagicMock(), MagicMock(), router, store)
    service._is_nontrivial = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service._aggregator.aggregate = AsyncMock(return_value="trace")  # type: ignore[method-assign]
    unit = service._uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.get = AsyncMock(return_value={"user_request": "r"})
    return service


@pytest.mark.parametrize(
    "outcome",
    [
        InsufficientCreditsError("OpenRouter refused the request (HTTP 402)"),
        AuthenticationError("OpenRouter authentication failed: invalid key"),
        MissingCredentialsError("OpenRouter API key is missing."),
        ModelResponse(
            message=Message(role="assistant", content="not json"),
            provider_name="t",
            model_used="t",
        ),
    ],
    ids=["402", "auth", "missing-key", "unparsable-output"],
)
async def test_non_retryable_failures_make_exactly_one_attempt(outcome, sleeps) -> None:
    router = AsyncMock()
    if isinstance(outcome, Exception):
        router.complete.side_effect = outcome
    else:
        router.complete.return_value = outcome

    await _service(router)._safe_reflect("task-1", None)  # never raises

    assert router.complete.await_count == 1
    assert sleeps == []


async def test_transient_failures_are_retried_with_backoff(sleeps) -> None:
    router = AsyncMock()
    router.complete.side_effect = RetryableProviderError("OpenRouter server error: 502")

    await _service(router)._safe_reflect("task-1", None, retries=2)

    assert router.complete.await_count == 3
    assert sleeps == [1, 2]


async def test_reflection_failure_leaves_the_completed_task_completed(tmp_path: Path) -> None:
    db_path = tmp_path / "reflection.sqlite"
    async with sqlite_connection(db_path) as connection:
        await run_migrations(connection)
    uow = UnitOfWork(db_path)
    async with uow.begin() as unit:
        assert unit.repositories is not None
        task_id = await unit.repositories.tasks.insert(
            title="T", user_request="write hello.py", status="completed"
        )
        await unit.repositories.tasks.insert_step(
            task_id=task_id, step_index=0, title="Write", tool_name="write_file",
            input_json="{}", status="completed",
        )

    router = AsyncMock()
    router.complete.side_effect = InsufficientCreditsError("HTTP 402")
    service = ReflectionService(uow, EventBus(), router, MemoryStore(uow))

    await service._safe_reflect(task_id, None)

    router.complete.assert_awaited_once()
    async with uow.begin() as unit:
        assert unit.repositories is not None
        task = await unit.repositories.tasks.get(task_id)
    assert task is not None and task["status"] == "completed"


async def test_stop_cancels_and_awaits_in_flight_reflections() -> None:
    bus = EventBus()
    service = ReflectionService(MagicMock(), bus, AsyncMock(), AsyncMock())
    started = asyncio.Event()
    unwound: list[str] = []

    async def long_reflection(task_id: str, project_id: str | None) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            unwound.append(task_id)

    service.reflect_on_task = long_reflection  # type: ignore[method-assign]
    await service.start()
    await asyncio.sleep(0)  # the listener subscribes on its first step
    await bus.publish("task.completed", {"task_id": "task-1"})
    await asyncio.wait_for(started.wait(), timeout=5)
    assert len(service._reflections) == 1  # referenced, so it cannot be garbage-collected

    await service.stop()

    assert unwound == ["task-1"]
    assert service._reflections == set()
    assert service._listener_task is None


def test_app_shutdown_closes_reflection_database_work_before_the_loop_closes(
    tmp_path: Path,
) -> None:
    """The lifecycle behind 'RuntimeError: Event loop is closed' in aiosqlite's thread."""
    app = create_app(
        config=JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "app.sqlite")),
        secret_manager=SecretManager({"JARVIS_API_TOKEN": "dummy"}, use_keyring=False),
    )
    service: ReflectionService = app.state.reflection_service
    state: dict[str, object] = {}

    async def reflection_holding_a_connection(task_id: str, project_id: str | None) -> None:
        async with service._uow.begin() as unit:
            state["connection"] = unit.connection
            await asyncio.Event().wait()

    service.reflect_on_task = reflection_holding_a_connection  # type: ignore[method-assign]
    bus = app.state.event_bus

    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while len(bus._subscribers) < 2:  # the task queue's and the reflection listener
            assert time.monotonic() < deadline
            time.sleep(0.01)
        client.portal.call(bus.publish, "task.completed", {"task_id": "task-1"})
        while "connection" not in state:
            assert time.monotonic() < deadline
            time.sleep(0.01)

    # Shutdown cancelled the reflection and its unit of work closed the connection.
    assert service._reflections == set()
    assert state["connection"]._connection is None  # type: ignore[attr-defined]

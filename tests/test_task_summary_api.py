"""F-07: GET /v1/tasks/{id}/summary through the real app and a disposable database."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.models.providers import AuthenticationError
from jarvis.models.schemas import Message, ModelResponse
from jarvis.storage.unit_of_work import UnitOfWork

API_TOKEN = "DUMMY-summary-api-token-91b2"
OPENROUTER_KEY = "sk-or-v1-DUMMY-summary-key-55aa"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def env(tmp_path: Path):
    db_path = tmp_path / "summary.sqlite"
    app = create_app(
        config=JarvisConfig(memory=MemoryConfig(database_path=db_path)),
        secret_manager=SecretManager(
            {"JARVIS_API_TOKEN": API_TOKEN, "OPENROUTER_API_KEY": OPENROUTER_KEY},
            use_keyring=False,
        ),
    )
    llm = AsyncMock(return_value=_reply("The task wrote hello.py and finished."))
    app.state.model_router.complete = llm  # the router instance TraceService uses
    with TestClient(app) as client:
        yield client, UnitOfWork(db_path), llm


def _reply(text: str) -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", content=text), provider_name="test", model_used="test"
    )


def _insert_task(uow: UnitOfWork, **fields: Any) -> str:
    """A task in a terminal state, so the background worker leaves it alone."""

    async def insert() -> str:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            metadata = fields.pop("metadata", None)
            task_id = await unit.repositories.tasks.insert(
                title="T",
                user_request=fields.pop("user_request", "write hello.py"),
                status=fields.pop("status", "completed"),
                metadata=metadata,
            )
            if fields:
                await unit.repositories.tasks.update(task_id, **fields)
            return task_id

    return asyncio.run(insert())


def _metadata(uow: UnitOfWork, task_id: str) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            assert task is not None
            return dict(task["metadata"])  # type: ignore[arg-type]

    return asyncio.run(read())


def test_summary_of_existing_task_is_generated_and_cached(env) -> None:
    client, uow, llm = env
    task_id = _insert_task(
        uow, started_at="2026-10-29T10:00:00+00:00", completed_at="2026-10-29T10:00:12+00:00"
    )

    response = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "task_id": task_id,
        "status": "completed",
        "summary": "The task wrote hello.py and finished.",
        "outcome": "success",
        "tokens_used": None,
        "wall_time": "12.0s",
    }
    assert _metadata(uow, task_id)["summary"] == "The task wrote hello.py and finished."

    # Served from the cache: no second model call.
    again = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)
    assert again.status_code == 200
    assert again.json()["summary"] == "The task wrote hello.py and finished."
    assert llm.await_count == 1


def test_summary_of_unknown_task_is_404(env) -> None:
    client, _, llm = env
    response = client.get("/v1/tasks/does-not-exist/summary", headers=HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found."}
    llm.assert_not_awaited()


def test_summary_of_task_without_steps_events_or_timestamps(env) -> None:
    client, uow, llm = env
    task_id = _insert_task(uow, status="failed")

    response = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["outcome"] == "failed"
    assert body["wall_time"] is None
    prompt = llm.await_args.args[0].messages[0].content
    assert prompt.rstrip().endswith("Provide ONLY the summary text. No preamble.")


def test_summary_requires_the_api_token(env) -> None:
    client, uow, _ = env
    task_id = _insert_task(uow)
    assert client.get(f"/v1/tasks/{task_id}/summary").status_code == 401


def test_summary_never_returns_or_sends_secret_values(env) -> None:
    client, uow, llm = env
    task_id = _insert_task(uow, user_request=f"use token {API_TOKEN} and key {OPENROUTER_KEY}")
    llm.return_value = _reply(f"Used {API_TOKEN} with {OPENROUTER_KEY}.")

    response = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["summary"] == "Used [REDACTED] with [REDACTED]."
    prompt = llm.await_args.args[0].messages[0].content
    assert API_TOKEN not in prompt and OPENROUTER_KEY not in prompt
    cached = json.dumps(_metadata(uow, task_id))
    assert API_TOKEN not in cached and OPENROUTER_KEY not in cached


def test_cached_summary_is_redacted(env) -> None:
    client, uow, llm = env
    task_id = _insert_task(uow, metadata={"summary": f"Saw {API_TOKEN}", "outcome": "success"})

    response = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["summary"] == "Saw [REDACTED]"
    llm.assert_not_awaited()


def test_provider_failure_is_reported_in_the_summary_and_not_cached(env) -> None:
    client, uow, llm = env
    task_id = _insert_task(uow)
    llm.side_effect = AuthenticationError(f"OpenRouter authentication failed: {OPENROUTER_KEY}")

    response = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary.startswith("Error generating summary: OpenRouter authentication failed")
    assert OPENROUTER_KEY not in summary
    assert "summary" not in _metadata(uow, task_id)


def test_unexpected_errors_are_not_disguised(env) -> None:
    client, uow, llm = env
    task_id = _insert_task(uow)
    llm.side_effect = RuntimeError(f"internal detail {API_TOKEN}")

    # The failure propagates (it is not turned into a summary or a 404)...
    with pytest.raises(RuntimeError, match="internal detail"):
        client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)

    # ...and the client gets a generic 500 without internal details.
    quiet = TestClient(client.app, raise_server_exceptions=False)
    response = quiet.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "internal detail" not in response.text
    assert API_TOKEN not in response.text


def test_summary_of_unfinished_task_is_not_cached(env) -> None:
    client, uow, llm = env
    task_id = _insert_task(uow, status="paused")
    llm.return_value = _reply("Waiting for approval of step 1.")

    first = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)
    assert first.status_code == 200
    assert first.json()["summary"] == "Waiting for approval of step 1."
    assert "summary" not in _metadata(uow, task_id)

    # Once the task has finished, a fresh summary is generated (and then cached).
    async def finish() -> None:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            await unit.repositories.tasks.update(task_id, status="completed")

    asyncio.run(finish())
    llm.return_value = _reply("The task finished.")
    second = client.get(f"/v1/tasks/{task_id}/summary", headers=HEADERS)
    assert second.json()["summary"] == "The task finished."
    assert second.json()["outcome"] == "success"
    assert _metadata(uow, task_id)["summary"] == "The task finished."

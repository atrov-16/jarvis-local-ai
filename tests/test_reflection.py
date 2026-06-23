"""Tests for the Reflection Engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.core.reflection import ReflectionService
from jarvis.memory.store import MemoryStore
from jarvis.models.schemas import Message, ModelResponse
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
def mock_uow():
    uow = MagicMock(spec=UnitOfWork)
    uow.begin = MagicMock()
    unit = MagicMock()
    unit.repositories = MagicMock()
    unit.connection = AsyncMock()
    uow.begin.return_value.__aenter__.return_value = unit
    return uow


@pytest.fixture
def mock_model_router():
    router = AsyncMock()
    return router


@pytest.fixture
def mock_memory_store():
    store = AsyncMock(spec=MemoryStore)
    store.get_planner_context.return_value = ("", [])
    return store


@pytest.mark.asyncio
async def test_reflection_thresholds_trivial(mock_uow):
    # Setup: 1 step, no tool
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.list_steps = AsyncMock(return_value=[
        {"id": "step1", "title": "Simple step", "tool_name": None}
    ])
    
    service = ReflectionService(mock_uow, MagicMock(), MagicMock(), MagicMock())
    is_nontrivial = await service._is_nontrivial("task1")
    assert is_nontrivial is False


@pytest.mark.asyncio
async def test_reflection_thresholds_nontrivial_tool(mock_uow):
    # Setup: 1 step with tool
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.list_steps = AsyncMock(return_value=[
        {"id": "step1", "title": "Tool step", "tool_name": "file.read"}
    ])
    
    service = ReflectionService(mock_uow, MagicMock(), MagicMock(), MagicMock())
    is_nontrivial = await service._is_nontrivial("task1")
    assert is_nontrivial is True


@pytest.mark.asyncio
async def test_reflection_thresholds_nontrivial_steps(mock_uow):
    # Setup: 3 steps, no tools
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.list_steps = AsyncMock(return_value=[
        {"id": "s1", "title": "1", "tool_name": None},
        {"id": "s2", "title": "2", "tool_name": None},
        {"id": "s3", "title": "3", "tool_name": None},
    ])
    
    service = ReflectionService(mock_uow, MagicMock(), MagicMock(), MagicMock())
    is_nontrivial = await service._is_nontrivial("task1")
    assert is_nontrivial is True


@pytest.mark.asyncio
async def test_reflect_on_task_creates_proposals(mock_uow, mock_model_router, mock_memory_store):
    # Setup
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.get = AsyncMock(return_value={"id": "task1", "title": "Test Task", "user_request": "Do something"})
    unit.repositories.tasks.list_steps = AsyncMock(return_value=[{
        "id": "s1", 
        "step_index": 0,
        "title": "Step 1", 
        "status": "completed",
        "tool_name": "tool1",
        "input_json": "{}",
        "output_json": "{}"
    }])
    unit.repositories.tasks.list_events = AsyncMock(return_value=[])
    
    audit_cursor = AsyncMock()
    audit_cursor.fetchall.return_value = []
    unit.connection.execute.return_value = audit_cursor

    # Mock LLM Response
    reflection_json = {
        "proposals": [
            {
                "memory_type": "fact",
                "content": "The user is testing reflection.",
                "reason": "Observed during test.",
                "confidence_score": 0.9,
                "tags": ["test"],
                "importance": 0.6
            }
        ]
    }
    mock_model_router.complete.return_value = ModelResponse(
        message=Message(role="assistant", content=f"```json\n{json.dumps(reflection_json)}\n```"),
        provider_name="test",
        model_used="test"
    )

    service = ReflectionService(mock_uow, MagicMock(), mock_model_router, mock_memory_store)
    await service.reflect_on_task("task1", "project1")

    # Verify
    mock_memory_store.propose.assert_called_once()
    args = mock_memory_store.propose.call_args.kwargs
    assert args["memory_type"] == "fact"
    assert args["proposed_content"] == "The user is testing reflection."
    assert args["confidence_score"] == 0.9
    assert args["task_id"] == "task1"
    assert args["project_id"] == "project1"

    # Verify max_tokens
    mock_model_router.complete.assert_called_once()
    req = mock_model_router.complete.call_args[0][0]
    assert req.max_tokens == 4096
    assert "engine" in args["metadata"]

"""Tests for the Memory Consolidation Engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.core.consolidation import ConsolidationService
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
    return store


@pytest.mark.asyncio
async def test_cluster_memories():
    memories = [
        {"id": "1", "memory_type": "fact", "tags": ["python"]},
        {"id": "2", "memory_type": "fact", "tags": ["python"]},
        {"id": "3", "memory_type": "preference", "tags": ["git"]},
        {"id": "4", "memory_type": "fact", "tags": ["javascript"]},
    ]
    service = ConsolidationService(MagicMock(), MagicMock(), MagicMock())
    clusters = service._cluster_memories(memories)
    
    # Only the python facts should cluster
    assert len(clusters) == 1
    assert len(clusters[0]) == 2
    assert clusters[0][0]["id"] in ["1", "2"]


@pytest.mark.asyncio
async def test_consolidate_all_creates_merge_proposals(mock_uow, mock_model_router, mock_memory_store):
    # Setup
    unit = mock_uow.begin.return_value.__aenter__.return_value
    memories = [
        {"id": "1", "memory_type": "fact", "content": "Uses pytest", "tags": ["testing"], "importance": 0.5},
        {"id": "2", "memory_type": "fact", "content": "Test runner is pytest", "tags": ["testing"], "importance": 0.6},
    ]
    unit.repositories.memory.list_long_term = AsyncMock(return_value=memories)

    # Mock LLM Response for Merge
    consolidation_json = {
        "actions": [
            {
                "type": "merge",
                "target_ids": ["1", "2"],
                "proposed_content": "The project uses pytest as its primary test runner.",
                "reason": "Redundant facts about testing.",
                "score": 0.95
            }
        ]
    }
    mock_model_router.complete.return_value = ModelResponse(
        message=Message(role="assistant", content=f"```json\n{json.dumps(consolidation_json)}\n```"),
        provider_name="test",
        model_used="test"
    )

    service = ConsolidationService(mock_uow, mock_model_router, mock_memory_store)
    count = await service.consolidate_all()

    # Verify
    assert count == 1
    mock_memory_store.propose.assert_called_once()
    args = mock_memory_store.propose.call_args.kwargs
    assert args["proposed_content"] == "The project uses pytest as its primary test runner."
    assert "merged_ids" in args["metadata"]
    assert args["metadata"]["merged_ids"] == ["1", "2"]
    assert args["importance"] == 0.6


@pytest.mark.asyncio
async def test_consolidate_all_creates_conflict_proposals(mock_uow, mock_model_router, mock_memory_store):
    # Setup
    unit = mock_uow.begin.return_value.__aenter__.return_value
    memories = [
        {"id": "1", "memory_type": "preference", "content": "Likes tabs", "tags": ["style"], "importance": 0.5},
        {"id": "2", "memory_type": "preference", "content": "Likes spaces", "tags": ["style"], "importance": 0.5},
    ]
    unit.repositories.memory.list_long_term = AsyncMock(return_value=memories)

    # Mock LLM Response for Conflict
    consolidation_json = {
        "actions": [
            {
                "type": "conflict",
                "target_ids": ["1", "2"],
                "reason": "Conflicting indentation preferences detected.",
                "score": 0.9
            }
        ]
    }
    mock_model_router.complete.return_value = ModelResponse(
        message=Message(role="assistant", content=f"```json\n{json.dumps(consolidation_json)}\n```"),
        provider_name="test",
        model_used="test"
    )

    service = ConsolidationService(mock_uow, mock_model_router, mock_memory_store)
    count = await service.consolidate_all()

    # Verify
    assert count == 1
    mock_memory_store.propose.assert_called_once()
    args = mock_memory_store.propose.call_args.kwargs
    assert "CONFLICT DETECTED" in args["proposed_content"]
    assert "conflicting_ids" in args["metadata"]
    assert args["metadata"]["conflicting_ids"] == ["1", "2"]

"""Tests for the Memory Browser API."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.api.services.memory_browser import MemoryBrowserService
from jarvis.memory.store import MemoryStore, MemorySearchResult
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
def mock_uow():
    uow = MagicMock()
    uow.begin = MagicMock()
    unit = MagicMock()
    unit.repositories = MagicMock()
    unit.connection = AsyncMock()
    uow.begin.return_value.__aenter__.return_value = unit
    return uow


@pytest.fixture
def mock_memory_store():
    store = AsyncMock(spec=MemoryStore)
    return store


@pytest.mark.asyncio
async def test_list_memories_simple(mock_uow, mock_memory_store):
    service = MemoryBrowserService(mock_uow, mock_memory_store)
    
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.memory.list_long_term = AsyncMock(return_value=[
        {
            "id": "m1", 
            "memory_type": "fact", 
            "content": "test", 
            "source": "test", 
            "status": "active", 
            "importance": 0.5,
            "confidence_score": 1.0,
            "access_count": 0,
            "created_at": "2026-06-17",
            "updated_at": "2026-06-17"
        }
    ])
    
    memories = await service.list_memories()
    assert len(memories) == 1
    assert memories[0].id == "m1"


from unittest.mock import AsyncMock, MagicMock, ANY

...

@pytest.mark.asyncio
async def test_get_memory_detail_with_lineage(mock_uow, mock_memory_store):
    service = MemoryBrowserService(mock_uow, mock_memory_store)
    
    unit = mock_uow.begin.return_value.__aenter__.return_value
    
    m1 = {
        "id": "m1", 
        "memory_type": "fact", 
        "content": "Merged Fact", 
        "source": "merge", 
        "status": "active",
        "source_ids": ["task1", "m2"],
        "created_at": "2026-06-17",
        "updated_at": "2026-06-17"
    }
    m2 = {
        "id": "m2", 
        "memory_type": "fact", 
        "content": "Parent Fact", 
        "source": "reflection", 
        "status": "merged",
        "source_ids": [],
        "created_at": "2026-06-16",
        "updated_at": "2026-06-17"
    }
    
    # Mock Memory
    def get_long_term_mock(id):
        if id == "m1": return m1
        if id == "m2": return m2
        return None
        
    unit.repositories.memory.get_long_term = AsyncMock(side_effect=get_long_term_mock)
    
    # Mock Task
    unit.repositories.tasks.get = AsyncMock(side_effect=lambda id: {
        "id": "task1", 
        "title": "Fix bug", 
        "user_request": "fix it",
        "created_at": "2026-06-15"
    } if id == "task1" else None)
    
    # Mock Proposal (not found in this case)
    unit.repositories.memory.get_proposal = AsyncMock(return_value=None)

    detail = await service.get_memory_detail("m1")
    
    assert detail.id == "m1"
    # lineage should contain task1 and m2
    node_types = [n.type for n in detail.lineage]
    assert "task" in node_types
    assert "memory" in node_types


@pytest.mark.asyncio
async def test_resolve_conflict_pick_winner(mock_uow, mock_memory_store):
    service = MemoryBrowserService(mock_uow, mock_memory_store)
    
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.memory.update_long_term = AsyncMock(return_value=True)
    unit.repositories.audit.insert = AsyncMock()
    
    success = await service.resolve_conflict(
        memory_id="m1", 
        action="pick_winner", 
        winner_id="m1", 
        conflicting_ids=["m1", "m2"]
    )
    
    assert success is True
    # Verify winner activated
    unit.repositories.memory.update_long_term.assert_any_call("m1", status="active")
    # Verify loser archived
    unit.repositories.memory.update_long_term.assert_any_call("m2", status="archived", archived_at=ANY)

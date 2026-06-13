"""Tests for Phase 7 Step 2 memory ranking and retrieval."""

import pytest
import datetime
from unittest.mock import AsyncMock, MagicMock
from jarvis.memory.store import MemoryStore

@pytest.fixture
def uow():
    mock_uow = MagicMock()
    mock_uow.begin.return_value.__aenter__.return_value = mock_uow
    mock_uow.repositories = MagicMock()
    return mock_uow

@pytest.mark.asyncio
async def test_memory_ranking_multiplicative(uow):
    store = MemoryStore(uow)
    
    # Mock search_long_term to return candidates
    now = datetime.datetime.now(datetime.UTC).isoformat()
    yesterday = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).isoformat()
    old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)).isoformat()
    
    mock_results = [
        # Memory A: Low FTS, low importance
        {
            "id": "A", "memory_type": "fact", "content": "Generic fact", "rank": -8.0,
            "importance": 0.2, "access_count": 0, "last_retrieved_at": old, "project_id": None
        },
        # Memory B: High FTS, high importance, recent, high access
        {
            "id": "B", "memory_type": "preference", "content": "Strong preference", "rank": -10.0,
            "importance": 0.9, "access_count": 50, "last_retrieved_at": yesterday, "project_id": None
        },
        # Memory C: Medium FTS, project match
        {
            "id": "C", "memory_type": "decision", "content": "Project decision", "rank": -9.0,
            "importance": 0.8, "access_count": 5, "last_retrieved_at": old, "project_id": "proj-1"
        }
    ]

    uow.repositories.memory.search_long_term = AsyncMock(return_value=mock_results)
    
    context_str, memory_ids = await store.get_planner_context("test query", project_id="proj-1")
    
    # Memory B should rank highest due to multiplicative boosts (high importance, recent, accesses)
    # Memory C should beat A due to project match and importance.
    assert len(memory_ids) == 3
    assert memory_ids[0] == "B"
    assert memory_ids[1] == "C"
    assert memory_ids[2] == "A"
    
    assert "[Preference] (Global) Strong preference" in context_str
    assert "[Decision] (Project) Project decision" in context_str
    assert "[Fact] (Global) Generic fact" in context_str


@pytest.mark.asyncio
async def test_categorical_token_budgeting(uow):
    store = MemoryStore(uow)
    
    # Create large payloads to exceed budgets
    large_decision = "D" * 1500  # fits in 1600 budget
    large_decision_2 = "D" * 500  # exceeds 1600 budget (total 2000)
    large_reflection = "R" * 1000 # fits in 1200 budget
    large_fact = "F" * 4000       # fits in remaining (6000 - 1500 - 1000 = 3500 remaining, wait 4000 exceeds it?)
    # total used = 1500 (dec) + 1000 (ref) = 2500. Remainder = 3500. A 4000 length fact would be skipped.
    
    mock_results = [
        {"id": "1", "memory_type": "decision", "content": large_decision, "rank": -10.0},
        {"id": "2", "memory_type": "decision", "content": large_decision_2, "rank": -9.0},
        {"id": "3", "memory_type": "reflection", "content": large_reflection, "rank": -8.0},
        {"id": "4", "memory_type": "fact", "content": large_fact, "rank": -7.0},
        {"id": "5", "memory_type": "fact", "content": "Small fact", "rank": -6.0},
    ]
    
    uow.repositories.memory.search_long_term = AsyncMock(return_value=mock_results)
    context_str, memory_ids = await store.get_planner_context("query")
    
    assert "1" in memory_ids
    assert "2" not in memory_ids  # Exceeded decision budget
    assert "3" in memory_ids
    assert "4" not in memory_ids  # Exceeded total budget (would push total over 6000)
    assert "5" in memory_ids      # Fits in remaining total budget

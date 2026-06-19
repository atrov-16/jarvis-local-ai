"""Tests for MemoryRepository and Phase 4 migrations."""

import pytest

from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "test.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)

async def test_short_term_context(uow):
    async with uow.begin() as unit:
        memory_id = await unit.repositories.memory.insert_short_term(
            source="test",
            role="user",
            content="Hello world",
            tags=["greeting"]
        )
        assert memory_id is not None

        # Verify insertion
        cursor = await unit.connection.execute("SELECT * FROM short_term_context WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        assert row is not None
        assert row["content"] == "Hello world"
        assert "greeting" in row["tags_json"]

async def test_long_term_memory_lifecycle(uow):
    async with uow.begin() as unit:
        # 1. Propose
        proposal_id = await unit.repositories.memory.propose_long_term(
            memory_type="fact",
            proposed_content="The sky is blue",
            reason="Observed phenomenon",
            proposed_tags=["nature"],
            importance=0.8,
            source_ids=["task-123"]
        )
        
        # 2. Get Proposal
        proposal = await unit.repositories.memory.get_proposal(proposal_id)
        assert proposal["proposed_content"] == "The sky is blue"
        assert proposal["status"] == "pending"
        assert proposal["importance"] == 0.8
        assert proposal["source_ids"] == ["task-123"]
        
        # 3. Update Status
        await unit.repositories.memory.update_proposal_status(proposal_id, "approved")
        proposal = await unit.repositories.memory.get_proposal(proposal_id)
        assert proposal["status"] == "approved"
        
        # 4. Promote (insert into long_term_memory)
        memory_id = await unit.repositories.memory.insert_long_term(
            memory_type=proposal["memory_type"],
            content=proposal["proposed_content"],
            tags=proposal["proposed_tags"],
            source="proposal_promotion",
            title="Sky Color",
            importance=proposal["importance"],
            source_ids=proposal["source_ids"]
        )
        
        # 5. Verify Long Term
        memory = await unit.repositories.memory.get_long_term(memory_id)
        assert memory["content"] == "The sky is blue"
        assert memory["memory_type"] == "fact"
        assert memory["title"] == "Sky Color"
        assert memory["importance"] == 0.8
        assert memory["source_ids"] == ["task-123"]
        assert memory["access_count"] == 0
        assert memory["last_retrieved_at"] is None

async def test_update_memory_access(uow):
    async with uow.begin() as unit:
        memory_id = await unit.repositories.memory.insert_long_term(
            memory_type="fact",
            content="Testing access",
            source="manual"
        )
        
        await unit.repositories.memory.update_memory_access([memory_id])
        
        memory = await unit.repositories.memory.get_long_term(memory_id)
        assert memory["access_count"] == 1
        assert memory["last_retrieved_at"] is not None

async def test_fts5_search(uow):
    async with uow.begin() as unit:
        await unit.repositories.memory.insert_long_term(
            memory_type="fact",
            title="FastAPI Guide",
            content="FastAPI is a modern web framework for building APIs with Python.",
            source="manual"
        )
        await unit.repositories.memory.insert_long_term(
            memory_type="note",
            title="Grocery List",
            content="Buy milk, eggs, and bread.",
            source="manual"
        )
        
        # Search for FastAPI
        results = await unit.repositories.memory.search_long_term("FastAPI")
        assert len(results) == 1
        assert results[0]["title"] == "FastAPI Guide"
        
        # Search for milk
        results = await unit.repositories.memory.search_long_term("milk")
        assert len(results) == 1
        assert results[0]["title"] == "Grocery List"
        
        # Search for APIs (prefix)
        results = await unit.repositories.memory.search_long_term("API*")
        assert len(results) == 1
        assert results[0]["title"] == "FastAPI Guide"

async def test_search_filtering(uow):
    async with uow.begin() as unit:
        p1 = await unit.repositories.projects.insert(name="Project A")
        p2 = await unit.repositories.projects.insert(name="Project B")
        
        await unit.repositories.memory.insert_long_term(
            project_id=p1,
            memory_type="fact",
            content="Data for Project A",
            source="manual"
        )
        await unit.repositories.memory.insert_long_term(
            project_id=p2,
            memory_type="fact",
            content="Data for Project B",
            source="manual"
        )
        
        # Filter by Project A
        results = await unit.repositories.memory.search_long_term("Data", project_id=p1)
        assert len(results) == 1
        assert results[0]["project_id"] == p1

async def test_memory_deletion_syncs_fts(uow):
    async with uow.begin() as unit:
        memory_id = await unit.repositories.memory.insert_long_term(
            memory_type="note",
            content="Temporary note",
            source="manual"
        )
        
        # Verify it's there
        results = await unit.repositories.memory.search_long_term("Temporary")
        assert len(results) == 1
        
        # Delete
        await unit.repositories.memory.delete_long_term(memory_id)
        
        # Verify it's gone from search
        results = await unit.repositories.memory.search_long_term("Temporary")
        assert len(results) == 0

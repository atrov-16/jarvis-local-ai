"""Tests for MemoryStore service."""

import pytest
from jarvis.memory.store import MemoryStore
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork

@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "test.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)

@pytest.fixture
def memory_store(uow):
    return MemoryStore(uow)

async def test_propose_and_approve(memory_store, uow):
    # 1. Propose
    proposal_id = await memory_store.propose(
        memory_type="fact",
        proposed_content="Python is great",
        reason="User stated preference",
        proposed_tags=["python"]
    )
    assert proposal_id is not None
    
    # Verify audit log for proposal
    async with uow as unit:
        cursor = await unit.connection.execute("SELECT * FROM audit_log WHERE target = ?", (proposal_id,))
        audit = await cursor.fetchone()
        assert audit["action_type"] == "memory.propose"

    # 2. Approve
    memory_id = await memory_store.approve(proposal_id, title="Language Fact")
    assert memory_id is not None
    
    # Verify promotion
    results = await memory_store.search("Python")
    assert len(results) == 1
    assert results[0].content == "Python is great"
    assert results[0].title == "Language Fact"
    
    # Verify audit log for approval
    async with uow as unit:
        cursor = await unit.connection.execute("SELECT * FROM audit_log WHERE action_type = 'memory.approve'")
        audit = await cursor.fetchone()
        assert audit["target"] == memory_id

async def test_deny_proposal(memory_store, uow):
    proposal_id = await memory_store.propose(
        memory_type="note",
        proposed_content="Random thought",
        reason="Testing",
    )
    
    success = await memory_store.deny(proposal_id, reason="Not useful")
    assert success is True
    
    # Verify status in DB
    async with uow as unit:
        cursor = await unit.connection.execute("SELECT status FROM memory_proposals WHERE id = ?", (proposal_id,))
        row = await cursor.fetchone()
        assert row["status"] == "denied"
        
        # Verify audit
        cursor = await unit.connection.execute("SELECT * FROM audit_log WHERE action_type = 'memory.deny'")
        audit = await cursor.fetchone()
        assert audit["target"] == proposal_id

async def test_delete_memory(memory_store, uow):
    # Setup: Propose and approve
    pid = await memory_store.propose(memory_type="fact", proposed_content="Delete me", reason="Testing")
    mid = await memory_store.approve(pid)
    
    # Delete
    success = await memory_store.delete_memory(mid)
    assert success is True
    
    # Verify gone
    results = await memory_store.search("Delete")
    assert len(results) == 0
    
    # Verify audit
    async with uow as unit:
        cursor = await unit.connection.execute("SELECT * FROM audit_log WHERE action_type = 'memory.delete'")
        audit = await cursor.fetchone()
        assert audit["target"] == mid

async def test_search_structure(memory_store):
    await memory_store.propose(memory_type="fact", proposed_content="Structured search", reason="Test")
    # Need to approve to be searchable
    async with memory_store._uow as unit:
        proposals = await unit.repositories.memory._connection.execute("SELECT id FROM memory_proposals")
        row = await proposals.fetchone()
        pid = row[0]
    
    await memory_store.approve(pid)
    
    results = await memory_store.search("Structured")
    assert len(results) == 1
    res = results[0]
    assert hasattr(res, "id")
    assert hasattr(res, "relevance_score")
    assert isinstance(res.relevance_score, float)

async def test_approve_non_pending_fails(memory_store):
    pid = await memory_store.propose(memory_type="fact", proposed_content="Fail test", reason="Test")
    await memory_store.approve(pid)
    
    with pytest.raises(ValueError, match="Proposal is already approved"):
        await memory_store.approve(pid)

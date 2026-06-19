"""Tests for the Approval Center API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.api.services.approval_center import ApprovalCenterService
from jarvis.approvals.broker import ApprovalBroker
from jarvis.memory.store import MemoryStore


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
def mock_broker():
    broker = AsyncMock(spec=ApprovalBroker)
    return broker


@pytest.fixture
def mock_store():
    store = AsyncMock(spec=MemoryStore)
    return store


@pytest.mark.asyncio
async def test_list_pending_unified(mock_uow, mock_broker, mock_store):
    service = ApprovalCenterService(mock_uow, mock_broker, mock_store)
    
    unit = mock_uow.begin.return_value.__aenter__.return_value
    
    # Mock actions
    unit.repositories.approvals.list_all = AsyncMock(return_value=[
        {
            "id": "a1", 
            "action_type": "tool", 
            "summary": "Run git", 
            "risk_level": "high", 
            "created_at": "2026-06-17T12:00:00",
            "action_json": "{}"
        }
    ])
    
    # Mock memory proposals
    unit.repositories.memory.list_proposals = AsyncMock(return_value=[
        {
            "id": "p1", 
            "memory_type": "fact", 
            "proposed_content": "Earth is round", 
            "reason": "Scientific fact", 
            "created_at": "2026-06-17T11:00:00",
            "metadata": {}
        }
    ])
    
    items = await service.list_pending()
    assert len(items) == 2
    # Sorted by created_at DESC: a1 (12:00) then p1 (11:00)
    assert items[0].id == "a1"
    assert items[0].type == "action"
    assert items[1].id == "p1"
    assert items[1].type == "memory"


@pytest.mark.asyncio
async def test_bulk_approve_mixed(mock_uow, mock_broker, mock_store):
    service = ApprovalCenterService(mock_uow, mock_broker, mock_store)
    
    from jarvis.api.schemas import BulkApprovalItem, BulkApprovalRequest
    
    request = BulkApprovalRequest(
        action="approve",
        items=[
            BulkApprovalItem(id="a1", type="action"),
            BulkApprovalItem(id="p1", type="memory")
        ]
    )
    
    mock_broker.approve = AsyncMock(return_value=True)
    mock_store.approve = AsyncMock(return_value="m1")
    
    results = await service.bulk_respond(request)
    
    assert results["summary"]["success"] == 2
    mock_broker.approve.assert_called_once_with("a1", decided_by="user", reason=None)
    mock_store.approve.assert_called_once_with("p1")


@pytest.mark.asyncio
async def test_get_stats(mock_uow, mock_broker, mock_store):
    service = ApprovalCenterService(mock_uow, mock_broker, mock_store)
    unit = mock_uow.begin.return_value.__aenter__.return_value
    
    unit.repositories.approvals.get_stats = AsyncMock(return_value={
        "pending": 5, "approved": 10, "denied": 2, "avg_time": 45.0
    })
    
    # Mock pending memory count
    memory_cursor = AsyncMock()
    memory_cursor.__getitem__.side_effect = lambda key: 3 if key == "count" else None
    unit.connection.execute.return_value.fetchone = AsyncMock(return_value=memory_cursor)
    
    # Mock risk counts
    risk_cursor = AsyncMock()
    risk_cursor.fetchall = AsyncMock(return_value=[
        {"risk_level": "high", "count": 2},
        {"risk_level": "low", "count": 3}
    ])
    # Need to distinguish the two calls to unit.connection.execute
    def execute_mock(sql, params=None):
        m = AsyncMock()
        if "memory_proposals" in sql:
            m.fetchone.return_value = {"count": 3}
            return m
        if "approval_requests" in sql:
            m.fetchall.return_value = [{"risk_level": "high", "count": 2}, {"risk_level": "low", "count": 3}]
            return m
        return m

    unit.connection.execute.side_effect = execute_mock

    stats = await service.get_stats()
    assert stats.pending_count == 8 # 5 actions + 3 memories
    assert stats.avg_decision_time_sec == 45.0
    assert stats.by_risk["high"] == 2
    assert stats.by_risk["low"] == 6 # 3 from actions + 3 from memories

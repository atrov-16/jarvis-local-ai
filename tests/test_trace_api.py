"""Tests for the Trace Explorer API."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.models.schemas import Message, ModelResponse


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
def mock_model_router():
    router = AsyncMock()
    return router


@pytest.fixture
def client(mock_uow, mock_model_router):
    app = create_app(config=MagicMock(), secret_manager=MagicMock())
    # Override dependencies if needed, but here we just want to test the routing
    # Actually, create_app initializes everything. We need to inject mocks.
    # For simplicity, let's just use the real app and mock the internal services if possible.
    return TestClient(app)


@pytest.mark.asyncio
async def test_get_task_trace_not_found(mock_uow):
    from jarvis.api.services.trace import TraceService
    service = TraceService(mock_uow)
    
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.get = AsyncMock(return_value=None)
    
    with pytest.raises(ValueError, match="Task not found"):
        await service.get_task_trace("task1")


@pytest.mark.asyncio
async def test_get_task_trace_aggregation(mock_uow):
    from jarvis.api.services.trace import TraceService
    service = TraceService(mock_uow)
    
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.get = AsyncMock(return_value={"id": "task1", "user_request": "test", "status": "completed"})
    unit.repositories.tasks.list_steps = AsyncMock(return_value=[])
    unit.repositories.tasks.list_events = AsyncMock(return_value=[
        {"created_at": "2026-06-17T12:00:00", "message": "Event 1", "event_type": "test", "severity": "info"}
    ])
    
    # Mock audit and approvals cursors
    audit_cursor = AsyncMock()
    audit_cursor.fetchall.return_value = [
        {"created_at": "2026-06-17T12:00:01", "actor": "system", "summary": "Audit 1", "details_json": "{}"}
    ]
    
    approval_cursor = AsyncMock()
    approval_cursor.fetchall.return_value = [
        {"id": "app1", "created_at": "2026-06-17T11:59:59", "summary": "Approve test", "risk_level": "low", "action_type": "tool", "status": "approved"}
    ]
    
    # We need to ensure unit.connection.execute returns the right cursor based on query
    def execute_mock(sql, params):
        if "audit_log" in sql:
            return audit_cursor
        if "approval_requests" in sql:
            return approval_cursor
        return AsyncMock()

    unit.connection.execute.side_effect = execute_mock

    trace = await service.get_task_trace("task1", include_system=True)
    
    assert len(trace.entries) == 3
    # Sorted by timestamp: approval (11:59), event (12:00:00), audit (12:00:01)
    assert trace.entries[0].type == "approval"
    assert trace.entries[1].type == "event"
    assert trace.entries[2].type == "audit"


@pytest.mark.asyncio
async def test_get_task_summary_generation(mock_uow, mock_model_router):
    from jarvis.api.services.trace import TraceService
    service = TraceService(mock_uow, mock_model_router)
    
    unit = mock_uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.get = AsyncMock(return_value={
        "id": "task1", 
        "user_request": "do test", 
        "status": "completed", 
        "metadata": {},
        "started_at": "2026-06-17T12:00:00",
        "completed_at": "2026-06-17T12:00:10"
    })
    unit.repositories.tasks.update = AsyncMock()
    unit.repositories.tasks.list_steps = AsyncMock(return_value=[])
    unit.repositories.tasks.list_events = AsyncMock(return_value=[])
    unit.connection.execute.return_value.fetchall.return_value = [] # audit and approvals

    mock_model_router.complete.return_value = ModelResponse(
        message=Message(role="assistant", content="Task was successful."),
        provider_name="test",
        model_used="test"
    )

    summary = await service.get_task_summary("task1")
    
    assert summary.summary == "Task was successful."
    assert summary.status == "completed"
    assert summary.wall_time == "10.0s"
    
    # Verify metadata update
    unit.repositories.tasks.update.assert_called_once()
    args = unit.repositories.tasks.update.call_args.kwargs
    meta = json.loads(args["metadata_json"])
    assert meta["summary"] == "Task was successful."
    assert meta["wall_time"] == "10.0s"

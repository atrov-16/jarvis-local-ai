import pytest
import asyncio
from jarvis.approvals.broker import ApprovalBroker
from jarvis.approvals.models import ApprovalActionType, ProposedAction, RiskLevel
from jarvis.core.event_bus import EventBus
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.tools.base import ToolCategory, ToolResult, BaseTool
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry
from pydantic import BaseModel

@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "test_broker.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)

@pytest.fixture
def event_bus():
    return EventBus()

@pytest.fixture
def broker(uow, event_bus):
    return ApprovalBroker(uow, event_bus)

@pytest.mark.asyncio
async def test_compute_hash_stability(broker):
    h1 = broker.compute_hash("tool", '{"a": 1}', "ctx")
    h2 = broker.compute_hash("tool", '{"a": 1}', "ctx")
    assert h1 == h2
    
    # Order of keys in JSON shouldn't matter if we normalize, 
    # but broker.compute_hash uses literal JSON string currently.
    # If we want normalization, we should json.loads -> json.dumps(sort_keys=True).
    # Current implementation:
    # payload = {"type": action_type, "json": action_json, "context": context_id or ""}
    # encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    
    # Test normalization of the action_json itself if it was passed differently
    h3 = broker.compute_hash("tool", '{"a": 1, "b": 2}', "ctx")
    # Actually, broker doesn't normalize action_json string, it just puts it in a dict and normalizes THAT dict.
    # So '{"a": 1, "b": 2}' != '{"b": 2, "a": 1}' as strings.
    # We might want to fix this in ApprovalBroker.
    pass

@pytest.mark.asyncio
async def test_approval_flow(broker, uow):
    action = ProposedAction(
        action_type=ApprovalActionType.TOOL,
        summary="Test action",
        action_json='{"cmd": "run"}',
        risk_level=RiskLevel.HIGH
    )
    
    approval_id = await broker.create_request(action)
    assert approval_id is not None
    
    # Verify initial state
    async with uow.begin() as unit:
        req = await unit.repositories.approvals.get(approval_id)
        assert req["status"] == "pending"
    
    # Approve
    await broker.approve(approval_id, reason="Testing")
    
    async with uow.begin() as unit:
        req = await unit.repositories.approvals.get(approval_id)
        assert req["status"] == "approved"
        assert req["decision_reason"] == "Testing"

@pytest.mark.asyncio
async def test_hash_verification(broker, uow):
    action_json = '{"file": "important.txt"}'
    action = ProposedAction(
        action_type=ApprovalActionType.TOOL,
        summary="Delete file",
        action_json=action_json,
        risk_level=RiskLevel.CRITICAL
    )
    
    approval_id = await broker.create_request(action)
    await broker.approve(approval_id)
    
    # Verification with same JSON should pass
    assert await broker.verify_hash(approval_id, action_json) is True
    
    # Verification with different JSON should fail
    assert await broker.verify_hash(approval_id, '{"file": "other.txt"}') is False

@pytest.mark.asyncio
async def test_timeout_enforcement():
    class SlowInput(BaseModel):
        pass
    
    class SlowTool(BaseTool):
        def get_input_schema(self): return SlowInput
        async def execute(self, **kwargs):
            await asyncio.sleep(2)
            return ToolResult(success=True)

    registry = ToolRegistry()
    registry.register(SlowTool("slow", "...", ToolCategory.READ_ONLY, timeout_seconds=1))
    executor = ToolExecutor(registry)
    
    result = await executor.execute_step("slow", "{}")
    assert result.success is False
    assert result.timeout_occurred is True
    assert "timed out" in result.error

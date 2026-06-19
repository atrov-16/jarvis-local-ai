"""Tests for Phase 7 Step 3 planner context integration."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelResponse
from jarvis.tasks.planner import Planner


@pytest.fixture
def mock_router():
    return MagicMock(spec=ModelRouter)

@pytest.fixture
def planner(mock_router):
    return Planner(mock_router)

@pytest.mark.asyncio
async def test_planner_injects_memory_context(planner, mock_router):
    mock_response = ModelResponse(
        message=Message(role="assistant", content='{"title": "Test Plan", "steps": [{"title": "Step 1", "description": "Do it", "requires_approval": false}]}'),
        finish_reason="stop",
        provider_name="test",
        model_used="test-model"
    )
    mock_router.complete = AsyncMock(return_value=mock_response)
    
    memory_context = "### SYSTEM CONTEXT & MEMORIES\n[Decision] (Project) Always use pathlib"
    
    plan = await planner.create_plan("Read a file", memory_context=memory_context)
    
    assert plan.title == "Test Plan"
    mock_router.complete.assert_called_once()
    
    request_args = mock_router.complete.call_args[0][0]
    system_prompt = request_args.messages[0].content
    
    assert "Decision Precedence:" in system_prompt
    assert "Conflict Resolution:" in system_prompt
    assert memory_context in system_prompt
    assert "Always use pathlib" in system_prompt

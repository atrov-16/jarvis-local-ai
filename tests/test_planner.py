"""Tests for the Planner service."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from jarvis.tasks.planner import Planner, PlannedTask, PlannedStep
from jarvis.models.router import ModelRouter
from jarvis.models.schemas import ModelResponse, Message

@pytest.fixture
def mock_router():
    return MagicMock(spec=ModelRouter)

@pytest.fixture
def planner(mock_router):
    return Planner(mock_router)

async def test_planner_creates_valid_plan(planner, mock_router):
    # Setup mock response
    mock_response = ModelResponse(
        message=Message(role="assistant", content='{"title": "Test Plan", "steps": [{"title": "Step 1", "description": "Do it", "requires_approval": false}]}'),
        finish_reason="stop",
        provider_name="test",
        model_used="test-model"
    )
    mock_router.complete = AsyncMock(return_value=mock_response)
    
    plan = await planner.create_plan("Do something")
    
    assert plan.title == "Test Plan"
    assert len(plan.steps) == 1
    assert plan.steps[0].title == "Step 1"
    assert plan.steps[0].requires_approval is False
    mock_router.complete.assert_called_once()

async def test_planner_extracts_json_from_markdown(planner, mock_router):
    # Setup mock response with markdown
    content = """
Here is your plan:
```json
{
  "title": "Markdown Plan",
  "steps": [
    {"title": "Step 1", "description": "Markdown works", "requires_approval": true}
  ]
}
```
"""
    mock_response = ModelResponse(
        message=Message(role="assistant", content=content),
        finish_reason="stop",
        provider_name="test",
        model_used="test-model"
    )
    mock_router.complete = AsyncMock(return_value=mock_response)
    
    plan = await planner.create_plan("Markdown test")
    
    assert plan.title == "Markdown Plan"
    assert plan.steps[0].requires_approval is True

async def test_planner_handles_invalid_json(planner, mock_router):
    # Setup mock response with garbage
    mock_response = ModelResponse(
        message=Message(role="assistant", content="This is not JSON"),
        finish_reason="stop",
        provider_name="test",
        model_used="test-model"
    )
    mock_router.complete = AsyncMock(return_value=mock_response)
    
    with pytest.raises(ValueError, match="Failed to parse planner output as JSON"):
        await planner.create_plan("Garbage test")

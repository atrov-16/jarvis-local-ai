"""Tests for the Planner service."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelResponse
from jarvis.tasks.planner import Planner
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from pydantic import BaseModel

class DummyToolInput(BaseModel):
    arg: str

class DummyTool(BaseTool):
    def __init__(self, name: str):
        super().__init__(name=name, description=f"Dummy tool {name}", category=ToolCategory.READ_ONLY)
    def get_input_schema(self):
        return DummyToolInput
    async def execute(self, **kwargs):
        return ToolResult(success=True)

@pytest.fixture
def mock_router():
    return MagicMock(spec=ModelRouter)

@pytest.fixture
def mock_registry():
    registry = ToolRegistry()
    registry.register(DummyTool("test_tool_1"))
    return registry

@pytest.fixture
def planner(mock_router, mock_registry):
    return Planner(mock_router, mock_registry)

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
    
    # Verify the ModelRequest has the token cap
    call_args = mock_router.complete.call_args[0]
    request = call_args[0]
    assert request.max_tokens == 2048

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

async def test_planner_prompt_contains_registered_tools(planner, mock_router, mock_registry):
    mock_response = ModelResponse(
        message=Message(role="assistant", content='{"title": "T", "steps": []}'),
        provider_name="test",
        model_used="test"
    )
    mock_router.complete = AsyncMock(return_value=mock_response)
    
    await planner.create_plan("Test tools")
    
    call_args = mock_router.complete.call_args[0]
    sys_msg = call_args[0].messages[0].content
    
    assert "Available Tools:" in sys_msg
    assert "- Name: test_tool_1" in sys_msg
    assert "Dummy tool test_tool_1" in sys_msg

async def test_adding_tool_updates_prompt(planner, mock_router, mock_registry):
    mock_response = ModelResponse(
        message=Message(role="assistant", content='{"title": "T", "steps": []}'),
        provider_name="test",
        model_used="test"
    )
    mock_router.complete = AsyncMock(return_value=mock_response)
    
    # Add new tool
    mock_registry.register(DummyTool("new_dynamic_tool"))
    
    await planner.create_plan("Test tools")
    
    call_args = mock_router.complete.call_args[0]
    sys_msg = call_args[0].messages[0].content
    
    assert "- Name: test_tool_1" in sys_msg
    assert "- Name: new_dynamic_tool" in sys_msg

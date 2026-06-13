"""Tests for tool execution foundations."""

import pytest
from pydantic import BaseModel, Field
from typing import Any, Type

from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.executor import ToolExecutor


class MockInput(BaseModel):
    message: str = Field(..., description="A test message")
    count: int = Field(1, description="A test count")


class MockSuccessTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="mock_success",
            description="A tool that always succeeds.",
            category=ToolCategory.READ_ONLY
        )

    def get_input_schema(self) -> Type[BaseModel]:
        return MockInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, data={"echo": kwargs.get("message"), "count": kwargs.get("count")})


class MockFailTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="mock_fail",
            description="A tool that always fails.",
            category=ToolCategory.MUTATING
        )

    def get_input_schema(self) -> Type[BaseModel]:
        return MockInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=False, error="Simulated failure")


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(MockSuccessTool())
    reg.register(MockFailTool())
    return reg


@pytest.fixture
def executor(registry):
    return ToolExecutor(registry)


def test_registry_registration():
    reg = ToolRegistry()
    tool = MockSuccessTool()
    reg.register(tool)
    
    assert reg.get("mock_success") == tool
    assert tool in reg.list_tools()
    
    with pytest.raises(ValueError, match="already registered"):
        reg.register(tool)


def test_registry_lookup_failure():
    reg = ToolRegistry()
    with pytest.raises(KeyError, match="Tool not found"):
        reg.get("non_existent")


def test_registry_schema_export(registry):
    schemas = registry.get_tool_schemas()
    assert len(schemas) == 2
    
    success_schema = next(s for s in schemas if s["name"] == "mock_success")
    assert success_schema["description"] == "A tool that always succeeds."
    assert success_schema["category"] == "read_only"
    assert "properties" in success_schema["input_schema"]
    assert "message" in success_schema["input_schema"]["properties"]


@pytest.mark.asyncio
async def test_executor_success(executor):
    result = await executor.execute_step("mock_success", '{"message": "hello", "count": 5}')
    assert result.success is True
    assert result.data == {"echo": "hello", "count": 5}


@pytest.mark.asyncio
async def test_executor_tool_not_found(executor):
    result = await executor.execute_step("missing_tool", "{}")
    assert result.success is False
    assert "Tool not found" in result.error


@pytest.mark.asyncio
async def test_executor_invalid_json(executor):
    result = await executor.execute_step("mock_success", "{ invalid json }")
    assert result.success is False
    assert "Invalid JSON input" in result.error


@pytest.mark.asyncio
async def test_executor_validation_failure(executor):
    # Missing required 'message'
    result = await executor.execute_step("mock_success", '{"count": 1}')
    assert result.success is False
    assert "Input validation failed" in result.error


@pytest.mark.asyncio
async def test_executor_tool_failure(executor):
    result = await executor.execute_step("mock_fail", '{"message": "error"}')
    assert result.success is False
    assert result.error == "Simulated failure"


@pytest.mark.asyncio
async def test_executor_unexpected_exception(registry):
    class ExplodingTool(BaseTool):
        def __init__(self):
            super().__init__("boom", "Explodes", ToolCategory.EXTERNAL)
        def get_input_schema(self):
            return MockInput
        async def execute(self, **kwargs):
            raise RuntimeError("Unexpected boom")

    registry.register(ExplodingTool())
    executor = ToolExecutor(registry)
    
    result = await executor.execute_step("boom", '{"message": "hi"}')
    assert result.success is False
    assert "Internal tool error" in result.error
    assert "Unexpected boom" in result.error

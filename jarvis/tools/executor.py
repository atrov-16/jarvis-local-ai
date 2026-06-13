"""Service for safe and validated tool execution."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from jarvis.tools.base import ToolResult
from jarvis.tools.registry import ToolRegistry

LOG = logging.getLogger(__name__)


class ToolExecutor:
    """Orchestrates tool lookup, validation, and execution."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute_step(
        self, 
        tool_name: str, 
        input_json: str | None = None,
        **context: Any
    ) -> ToolResult:
        """
        Execute a tool with the provided JSON input and context.
        
        Args:
            tool_name: The registered name of the tool.
            input_json: A JSON string containing tool arguments.
            context: Additional environmental data (e.g., workspaces, unit_of_work).
            
        Returns:
            ToolResult containing success status and data/error.
        """
        try:
            # 1. Resolve tool
            try:
                tool = self._registry.get(tool_name)
            except KeyError:
                return ToolResult(success=False, error=f"Tool not found: {tool_name}")

            # 2. Parse input
            inputs: dict[str, Any] = {}
            if input_json:
                try:
                    inputs = json.loads(input_json)
                except json.JSONDecodeError as e:
                    return ToolResult(success=False, error=f"Invalid JSON input: {e}")

            # 3. Validate input
            schema = tool.get_input_schema()
            try:
                validated_inputs = schema(**inputs).model_dump()
            except ValidationError as e:
                return ToolResult(success=False, error=f"Input validation failed: {e}")

            # 4. Execute
            LOG.info(f"Executing tool '{tool_name}' (category: {tool.category.value})")
            return await tool.execute(**validated_inputs, **context)

        except Exception as e:
            LOG.exception(f"Unexpected error executing tool '{tool_name}': {e}")
            return ToolResult(success=False, error=f"Internal tool error: {str(e)}")

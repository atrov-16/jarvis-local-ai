"""Registry for managing and discovering Jarvis tools."""

from __future__ import annotations

import logging
from typing import Dict, List

from jarvis.tools.base import BaseTool

LOG = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for all available tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a new tool."""
        if tool.name in self._tools:
            raise ValueError(f"Tool with name '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        LOG.debug(f"Registered tool: {tool.name} ({tool.category})")

    def get(self, name: str) -> BaseTool:
        """Retrieve a tool by name."""
        if name not in self._tools:
            raise KeyError(f"Tool not found: {name}")
        return self._tools[name]

    def list_tools(self) -> List[BaseTool]:
        """Return a list of all registered tools."""
        return list(self._tools.values())

    def get_tool_schemas(self) -> List[dict]:
        """Export tool configurations for LLM planning."""
        schemas = []
        for tool in self._tools.values():
            schemas.append({
                "name": tool.name,
                "description": tool.description,
                "category": tool.category.value,
                "input_schema": tool.get_input_schema().model_json_schema()
            })
        return schemas

"""Registry for managing and discovering Jarvis tools."""

from __future__ import annotations

import logging
import re
from typing import Any

from jarvis.tools.base import BaseTool

LOG = logging.getLogger(__name__)

# What OpenAI-compatible APIs accept as a function name.
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DEFS_PREFIX = "#/$defs/"


def inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of a pydantic JSON schema with every `$ref` replaced by its definition and the
    `$defs` table removed. Keys next to a `$ref` (a field's own description or default)
    override the definition's. The input is not modified.

    Raises ValueError for a reference that cannot be inlined: one that points outside
    `$defs`, names a missing definition, or is recursive.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [resolve(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if not isinstance(ref, str):  # (a *property* named "$ref" maps to a schema, not a str)
            return {key: resolve(value, stack) for key, value in node.items()}
        name = ref[len(_DEFS_PREFIX):]
        if not ref.startswith(_DEFS_PREFIX) or name not in defs:
            raise ValueError(f"Cannot inline schema reference '{ref}'.")
        if name in stack:
            raise ValueError(f"Cannot inline recursive schema reference '{ref}'.")
        target = resolve(defs[name], (*stack, name))
        siblings = {key: resolve(value, stack) for key, value in node.items() if key != "$ref"}
        return {**target, **siblings}

    return resolve({key: value for key, value in schema.items() if key != "$defs"}, ())


class ToolRegistry:
    """Central registry for all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

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

    def list_tools(self) -> list[BaseTool]:
        """Return a list of all registered tools."""
        return list(self._tools.values())

    def to_openai_tools(self) -> list[dict[str, object]]:
        """Every registered tool as an OpenAI-style function tool, for native tool calling.

        The parameters are the tool's `llm_input_schema()` with every `$ref` inlined: several
        model providers reject or ignore `$defs`/`$ref` in function parameters.
        """
        tools: list[dict[str, object]] = []
        for tool in self._tools.values():
            if not _FUNCTION_NAME.fullmatch(tool.name):
                raise ValueError(
                    f"Tool name '{tool.name}' cannot be offered to a model: function names "
                    "may only contain letters, digits, '_' and '-' (at most 64 characters)."
                )
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": inline_schema_refs(tool.llm_input_schema()),
                },
            })
        return tools

    def get_tool_schemas(self) -> list[dict]:
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

"""Slice 1: the tool list offered to a model (ToolRegistry.to_openai_tools)."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from pydantic import BaseModel

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tools.build_runner import BuildTool
from jarvis.tools.filesystem import (
    DeleteFileTool,
    ListDirectoryTool,
    PatchFileTool,
    ReadFileTool,
    RestoreFileTool,
    WriteFileTool,
)
from jarvis.tools.generic_command import GenericCommandTool
from jarvis.tools.git import GitOperation, GitTool, GitToolInput
from jarvis.tools.memory import CreateMemoryProposalTool, SearchMemoryTool
from jarvis.tools.registry import ToolRegistry, inline_schema_refs
from jarvis.tools.tasks import GetTaskStatusTool
from jarvis.tools.test_runner import TestTool as TestRunnerTool  # not collected by pytest


class _Empty(BaseModel):
    pass


class StubTool(BaseTool):
    """A tool whose advertised schema is whatever the test needs."""

    def __init__(
        self,
        name: str = "stub",
        schema: dict[str, Any] | None = None,
        description: str = "A stub tool.",
    ) -> None:
        super().__init__(name, description, ToolCategory.READ_ONLY)
        self._schema = schema if schema is not None else {"type": "object", "properties": {}}

    def get_input_schema(self) -> type[BaseModel]:
        return _Empty

    def llm_input_schema(self) -> dict[str, Any]:
        return self._schema

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True)


def _full_registry() -> ToolRegistry:
    """The tool set the daemon registers (jarvis/api/http.py)."""
    registry = ToolRegistry()
    runner = CommandRunner()
    for tool in (
        ReadFileTool(),
        WriteFileTool(),
        PatchFileTool(),
        DeleteFileTool(),
        RestoreFileTool(),
        ListDirectoryTool(),
        GitTool(runner),
        TestRunnerTool(runner),
        BuildTool(runner),
        GenericCommandTool(runner),
        SearchMemoryTool(),
        CreateMemoryProposalTool(),
        GetTaskStatusTool(),
    ):
        registry.register(tool)
    return registry


def _function(tools: list[dict[str, object]], name: str) -> dict[str, Any]:
    (match,) = [t["function"] for t in tools if t["function"]["name"] == name]  # type: ignore[index]
    return match  # type: ignore[return-value]


# --- The real tool set -------------------------------------------------------------------------


def test_every_real_tool_is_offered_as_a_function_without_refs() -> None:
    registry = _full_registry()
    tools = registry.to_openai_tools()

    assert [t["function"]["name"] for t in tools] == [tool.name for tool in registry.list_tools()]  # type: ignore[index]
    for tool, offered in zip(registry.list_tools(), tools, strict=True):
        assert offered["type"] == "function"
        assert offered["function"]["description"] == tool.description  # type: ignore[index]
        assert offered["function"]["parameters"]["type"] == "object"  # type: ignore[index]
    text = json.dumps(tools)
    assert "$defs" not in text and "$ref" not in text


def test_the_inlining_is_exercised_by_a_real_tool() -> None:
    # Guards the test above: the raw schemas really do contain references to inline.
    raw = json.dumps(PatchFileTool().llm_input_schema())
    assert "$ref" in raw and "$defs" in raw


def test_property_descriptions_survive_inlining() -> None:
    git = _function(_full_registry().to_openai_tools(), "git")
    operation = git["parameters"]["properties"]["operation"]

    assert operation["description"] == "The Git operation to perform."
    # Nested definitions are inlined with their own field descriptions intact.
    patch = _function(_full_registry().to_openai_tools(), "patch_file")
    assert "$ref" not in json.dumps(patch)
    assert patch["parameters"]["properties"]


def test_git_advertises_only_the_operations_it_performs() -> None:
    git = _function(_full_registry().to_openai_tools(), "git")

    operation = git["parameters"]["properties"]["operation"]
    assert operation["enum"] == ["status", "diff", "log"]


def test_git_input_validation_still_accepts_the_full_enum() -> None:
    # Narrowing is only what the model is *told*; validation (the executor's job) is unchanged.
    assert GitToolInput(operation="commit").operation is GitOperation.COMMIT
    schema = GitTool(CommandRunner()).get_input_schema().model_json_schema()
    advertised_in_validation_schema = schema["$defs"]["GitOperation"]["enum"]
    assert {"status", "diff", "log", "commit", "push"} <= set(advertised_in_validation_schema)


def test_exporting_does_not_change_the_underlying_schema() -> None:
    registry = _full_registry()
    first = registry.to_openai_tools()
    second = registry.to_openai_tools()

    assert first == second
    full = GitToolInput.model_json_schema()["$defs"]["GitOperation"]["enum"]
    assert "commit" in full


# --- inline_schema_refs ------------------------------------------------------------------------


def test_nested_definitions_are_inlined_at_every_depth() -> None:
    schema = {
        "$defs": {
            "Outer": {"type": "object", "properties": {"inner": {"$ref": "#/$defs/Inner"}}},
            "Inner": {"type": "string", "enum": ["a", "b"]},
        },
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Outer"}}},
    }

    inlined = inline_schema_refs(schema)

    assert inlined["properties"]["items"]["items"] == {
        "type": "object",
        "properties": {"inner": {"type": "string", "enum": ["a", "b"]}},
    }
    assert "$defs" not in inlined


def test_keys_next_to_a_ref_override_the_definition() -> None:
    schema = {
        "$defs": {"Mode": {"type": "string", "enum": ["a"], "description": "generic"}},
        "properties": {
            "mode": {"$ref": "#/$defs/Mode", "description": "this field", "default": "a"}
        },
    }

    mode = inline_schema_refs(schema)["properties"]["mode"]

    assert mode == {"type": "string", "enum": ["a"], "description": "this field", "default": "a"}


def test_the_input_schema_is_not_modified() -> None:
    schema = PatchFileTool().llm_input_schema()
    before = copy.deepcopy(schema)

    inline_schema_refs(schema)

    assert schema == before


def test_a_property_named_ref_is_not_mistaken_for_a_reference() -> None:
    schema = {"type": "object", "properties": {"$ref": {"type": "string"}}}

    assert inline_schema_refs(schema) == schema


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        (
            {
                "$defs": {"Node": {"properties": {"next": {"$ref": "#/$defs/Node"}}}},
                "properties": {"head": {"$ref": "#/$defs/Node"}},
            },
            "recursive",
        ),
        (
            {
                "$defs": {
                    "A": {"properties": {"b": {"$ref": "#/$defs/B"}}},
                    "B": {"properties": {"a": {"$ref": "#/$defs/A"}}},
                },
                "properties": {"a": {"$ref": "#/$defs/A"}},
            },
            "recursive",
        ),
        ({"properties": {"x": {"$ref": "https://example.com/schema.json"}}}, "https://example.com"),
        ({"properties": {"x": {"$ref": "other.json#/Thing"}}}, "other.json"),
        ({"properties": {"x": {"$ref": "#/definitions/Thing"}}}, "definitions"),
        ({"properties": {"x": {"$ref": "#"}}}, "'#'"),
        ({"$defs": {}, "properties": {"x": {"$ref": "#/$defs/Missing"}}}, "Missing"),
    ],
)
def test_references_that_cannot_be_inlined_raise(schema: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValueError, match=re.escape(reason)):
        inline_schema_refs(schema)


def test_registry_refuses_a_tool_with_an_uninlinable_schema() -> None:
    registry = ToolRegistry()
    registry.register(StubTool(schema={"properties": {"x": {"$ref": "https://example.com/s.json"}}}))

    with pytest.raises(ValueError, match="Cannot inline"):
        registry.to_openai_tools()


# --- Function names ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["has space", "dot.name", "slash/name", "", "x" * 65, "naïve", "semi;colon"]
)
def test_invalid_function_names_are_refused(name: str) -> None:
    registry = ToolRegistry()
    registry.register(StubTool(name=name))

    with pytest.raises(ValueError, match="cannot be offered to a model"):
        registry.to_openai_tools()


@pytest.mark.parametrize("name", ["read_file", "get-task-status", "A1", "x" * 64])
def test_valid_function_names_are_accepted(name: str) -> None:
    registry = ToolRegistry()
    registry.register(StubTool(name=name))

    assert registry.to_openai_tools()[0]["function"]["name"] == name  # type: ignore[index]


def test_an_empty_registry_offers_no_tools() -> None:
    assert ToolRegistry().to_openai_tools() == []

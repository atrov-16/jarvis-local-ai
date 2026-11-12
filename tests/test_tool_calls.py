"""Slice 1: reading tool calls off a provider response, and building tool-calling requests."""

from __future__ import annotations

import json

import pytest

from jarvis.models.schemas import Message, ModelRequest, NamedToolChoice, ToolCall
from jarvis.models.tool_calls import (
    MAX_RAW_ARGUMENTS_CHARS,
    parse_tool_calls,
    to_openai_tool_calls,
    tool_name_for,
)

TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


def _call(arguments: object, call_id: str = "call_1", name: str = "read_file") -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _only(raw: list[dict[str, object]]) -> ToolCall:
    calls = parse_tool_calls(raw)
    assert calls is not None and len(calls) == 1
    return calls[0]


# --- Valid arguments ---------------------------------------------------------------------------


def test_no_tool_calls_is_none() -> None:
    assert parse_tool_calls(None) is None
    assert parse_tool_calls([]) is None


def test_json_string_arguments_are_parsed() -> None:
    call = _only([_call('{"path": "a.txt", "limit": 1.5}')])

    assert call.arguments == {"path": "a.txt", "limit": 1.5}
    assert call.error is None and call.raw_arguments is None


def test_object_arguments_from_ollama_are_kept() -> None:
    call = _only([_call({"path": "a.txt"})])

    assert call.arguments == {"path": "a.txt"}
    assert call.error is None


def test_missing_arguments_mean_no_inputs() -> None:
    call = _only([_call(None)])

    assert call.arguments == {} and call.error is None


# --- Malformed arguments are the model's mistake: an error on the call, never an exception ------


@pytest.mark.parametrize(
    "arguments",
    [
        "{bad",  # not JSON
        '{"path": "a.txt"',  # truncated
        "[1, 2]",  # JSON, but not an object
        '"text"',
        "42",
        "null",
        "",  # an empty string is not an object either
        "   ",
    ],
)
def test_malformed_string_arguments_give_call_error_and_raw_text(arguments: str) -> None:
    call = _only([_call(arguments)])

    assert call.error
    assert call.arguments == {}
    assert call.raw_arguments == arguments  # what the model sent, for the tool message


@pytest.mark.parametrize(
    "arguments",
    [
        '{"limit": NaN}',
        '{"limit": Infinity}',
        '{"limit": -Infinity}',
        '{"nested": {"items": [1, NaN]}}',
        '{"limit": 1e999}',  # overflows to inf without going through parse_constant
    ],
)
def test_non_finite_numbers_in_string_arguments_are_malformed(arguments: str) -> None:
    call = _only([_call(arguments)])

    assert call.error
    assert "finite" in call.error or "not valid JSON" in call.error
    assert call.arguments == {}
    assert call.raw_arguments == arguments


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": float("nan")},
        {"limit": float("inf")},
        {"nested": {"items": [1, float("-inf")]}},
    ],
)
def test_non_finite_numbers_in_object_arguments_are_malformed(arguments: dict[str, object]) -> None:
    # Ollama sends arguments as an object that `response.json()` has already decoded, and
    # Python's decoder accepts a bare NaN.
    call = _only([_call(arguments)])

    assert call.error and "finite" in call.error
    assert call.arguments == {}
    assert call.raw_arguments


@pytest.mark.parametrize("arguments", [5, [1, 2], True])
def test_arguments_of_the_wrong_type_are_malformed(arguments: object) -> None:
    call = _only([_call(arguments)])

    assert call.error and call.arguments == {}
    assert call.raw_arguments is not None


def test_deeply_nested_arguments_are_malformed_not_a_crash() -> None:
    call = _only([_call("[" * 100_000)])

    assert call.error and call.arguments == {}


def test_long_raw_arguments_are_bounded() -> None:
    call = _only([_call("{" + "x" * 5000)])

    assert call.error
    assert call.raw_arguments is not None
    assert call.raw_arguments.startswith("{xxx")
    assert call.raw_arguments.endswith("... [truncated]")
    assert len(call.raw_arguments) <= MAX_RAW_ARGUMENTS_CHARS + len("... [truncated]")


def test_one_bad_call_does_not_affect_the_others() -> None:
    calls = parse_tool_calls([_call('{"path": "a"}', "c1"), _call("{bad", "c2"), _call("{}", "c3")])

    assert calls is not None
    assert [c.id for c in calls] == ["c1", "c2", "c3"]
    assert [c.error is None for c in calls] == [True, False, True]


# --- Ids and structure -------------------------------------------------------------------------


def test_missing_empty_and_repeated_ids_are_replaced_with_unique_ones() -> None:
    raw = [
        {"function": {"name": "read_file", "arguments": {}}},  # Ollama: no id
        _call("{}", ""),
        _call("{}", "dup"),
        _call("{}", "dup"),
    ]
    calls = parse_tool_calls(raw)

    assert calls is not None
    ids = [c.id for c in calls]
    assert all(ids) and len(set(ids)) == 4
    assert ids[2] == "dup"


@pytest.mark.parametrize(
    "raw",
    [
        "not a list",
        {"function": {"name": "x"}},
        ["not an object"],
        [{"id": "c1"}],  # no function
        [{"function": "read_file"}],
        [{"function": {"arguments": "{}"}}],  # no name
        [{"function": {"name": "  "}}],
    ],
)
def test_unusable_structure_raises_value_error(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_tool_calls(raw)


# --- Replaying calls in a request --------------------------------------------------------------


def test_malformed_call_is_replayed_with_empty_object_arguments() -> None:
    call = _only([_call("{bad")])
    replayed = to_openai_tool_calls([call])

    assert replayed[0]["function"] == {"name": "read_file", "arguments": "{}"}


def test_tool_name_is_found_through_the_call_id() -> None:
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="c1", name="read_file"), ToolCall(id="c2", name="git")],
    )
    messages = [
        Message(role="user", content="go"),
        assistant,
        Message(role="tool", content="x", tool_call_id="c2"),
        Message(role="tool", content="y", tool_call_id="nope"),
    ]

    assert tool_name_for(messages, 2) == "git"
    assert tool_name_for(messages, 3) is None
    assert tool_name_for(messages, 0) is None


# --- ModelRequest: a forced tool choice needs tools --------------------------------------------


@pytest.mark.parametrize("choice", ["required", NamedToolChoice(name="read_file")])
@pytest.mark.parametrize("tools", [None, []])
def test_forced_tool_choice_without_tools_is_rejected(
    choice: object, tools: list[dict[str, object]] | None
) -> None:
    with pytest.raises(ValueError, match="offers no tools"):
        ModelRequest(
            messages=[Message(role="user", content="hi")],
            tools=tools,
            tool_choice=choice,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("choice", [None, "auto", "none"])
def test_non_forcing_tool_choice_without_tools_is_harmless(choice: str | None) -> None:
    request = ModelRequest(
        messages=[Message(role="user", content="hi")],
        tool_choice=choice,  # type: ignore[arg-type]
    )

    assert not request.wants_tools


@pytest.mark.parametrize("choice", ["auto", "none", "required", NamedToolChoice(name="read_file")])
def test_any_tool_choice_with_tools_is_accepted(choice: object) -> None:
    request = ModelRequest(
        messages=[Message(role="user", content="hi")],
        tools=TOOLS,
        tool_choice=choice,  # type: ignore[arg-type]
    )

    assert request.wants_tools
    json.dumps(request.model_dump())  # the request stays serialisable

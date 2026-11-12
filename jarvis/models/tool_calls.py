"""Translate tool calls between provider wire formats and the typed `ToolCall`.

OpenAI-compatible APIs (OpenRouter) send `function.arguments` as a JSON *string* and give every
call an id. Ollama sends `function.arguments` as a JSON *object* and no id. Both are read by
`parse_tool_calls`; each provider writes its own format back with the `to_*` helpers.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Sequence

from jarvis.models.schemas import Message, NamedToolChoice, ToolCall, ToolChoice

# Bounds what is kept of arguments that could not be parsed (they go into a tool message).
MAX_RAW_ARGUMENTS_CHARS = 2000


def new_tool_call_id() -> str:
    """An id for a call whose provider gave none (Ollama) or repeated one."""
    return f"call_{uuid.uuid4().hex[:24]}"


def parse_tool_calls(raw: object) -> list[ToolCall] | None:
    """Read a response's `message.tool_calls`. None when the model called no tool.

    Raises ValueError when the structure itself is unusable (not a list, or a call without a
    function name): the provider broke the protocol, and there is nothing to answer. Bad
    *arguments* are different: they are the model's mistake, so the call is returned with
    `error` set and the caller can tell the model what was wrong.

    Ids are unique within the result: a missing, empty or repeated id is replaced, because
    every call is answered by exactly one tool message addressed by id.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"tool_calls is not a list (got {type(raw).__name__})")

    calls: list[ToolCall] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("a tool call is not an object")
        function = entry.get("function")
        if not isinstance(function, dict):
            raise ValueError("a tool call has no function")
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a tool call has no function name")

        call_id = entry.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            call_id = new_tool_call_id()
        seen.add(call_id)

        arguments, error, raw_arguments = _parse_arguments(function.get("arguments"))
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments,
                error=error,
                raw_arguments=raw_arguments,
            )
        )
    return calls or None


def _parse_arguments(value: object) -> tuple[dict[str, object], str | None, str | None]:
    """(arguments, error, raw_arguments) for one call's `function.arguments`."""
    if value is None:
        return {}, None, None
    if isinstance(value, dict):
        # Ollama's body is parsed by `response.json()`, which accepts a bare NaN/Infinity.
        if _has_non_finite_number(value):
            return {}, _NON_FINITE_ERROR, _bounded(repr(value))
        return value, None, None
    if not isinstance(value, str):
        return (
            {},
            f"Tool arguments must be a JSON object, got {type(value).__name__}.",
            _bounded(repr(value)),
        )
    # An empty or blank string is not a JSON object either: it falls through to json.loads and
    # is reported as malformed, so the model is told to send `{}` for a call without inputs.
    try:
        parsed = json.loads(value, parse_constant=_reject_constant)
    except RecursionError:
        return {}, "Tool arguments are nested too deeply to parse.", _bounded(value)
    except ValueError as e:
        return {}, f"Tool arguments are not valid JSON: {e}", _bounded(value)
    if not isinstance(parsed, dict):
        return (
            {},
            f"Tool arguments must be a JSON object, got {type(parsed).__name__}.",
            _bounded(value),
        )
    if _has_non_finite_number(parsed):  # e.g. 1e999, which parse_constant never sees
        return {}, _NON_FINITE_ERROR, _bounded(value)
    return parsed, None, None


_NON_FINITE_ERROR = "Tool arguments contain a number that is not finite (NaN or Infinity)."


def _reject_constant(name: str) -> object:
    """`parse_constant` hook: NaN and Infinity are not JSON, and no tool input means them."""
    raise ValueError(f"{name} is not allowed")


def _has_non_finite_number(value: object) -> bool:
    """True if a NaN or infinite float appears anywhere in `value` (iterative, so depth is
    bounded by memory rather than the recursion limit)."""
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                return True
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _bounded(text: str) -> str:
    if len(text) <= MAX_RAW_ARGUMENTS_CHARS:
        return text
    return text[:MAX_RAW_ARGUMENTS_CHARS] + "... [truncated]"


def to_openai_tool_calls(calls: Sequence[ToolCall]) -> list[dict[str, object]]:
    """Assistant-message `tool_calls` for an OpenAI-compatible API.

    A call whose arguments were malformed is echoed with `{}`: replaying text that is not
    JSON makes some upstream providers reject the whole request, and the tool message that
    answers the call already quotes what the model sent.
    """
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
        }
        for call in calls
    ]


def to_ollama_tool_calls(calls: Sequence[ToolCall]) -> list[dict[str, object]]:
    """Assistant-message `tool_calls` for Ollama's /api/chat (arguments stay an object)."""
    return [{"function": {"name": call.name, "arguments": call.arguments}} for call in calls]


def to_openai_tool_choice(choice: ToolChoice) -> object:
    if isinstance(choice, NamedToolChoice):
        return {"type": "function", "function": {"name": choice.name}}
    return choice


def tool_name_for(messages: Sequence[Message], index: int) -> str | None:
    """The name of the tool that the tool message at `index` answers, found through its
    `tool_call_id` in the assistant messages before it. None if no call has that id."""
    call_id = messages[index].tool_call_id
    if call_id is None:
        return None
    for earlier in reversed(messages[:index]):
        for call in earlier.tool_calls or []:
            if call.id == call_id:
                return call.name
    return None

"""Shared helpers for the agent-loop tests: a scripted model and small dummy tools.

`ScriptedModel` stands in for `ModelRouter.complete`. Each call to it returns the next prepared
reply, so a test states exactly what the "model" says on every turn, and can read back the
requests the loop sent (the conversation, the offered tools).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from jarvis.models.schemas import Message, ModelRequest, ModelResponse, ToolCall, Usage
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult

_ids = itertools.count(1)


def text_reply(text: str) -> ModelResponse:
    """A reply with no tool call: the model's final answer."""
    return ModelResponse(
        message=Message(role="assistant", content=text),
        finish_reason="stop",
        usage=Usage(),
        provider_name="scripted",
        model_used="scripted-model",
    )


def tool_reply(*calls: Any, content: str = "") -> ModelResponse:
    """A reply that calls tools. Each call is `(name, arguments)`, `(name, arguments, call_id)`
    or a ready `ToolCall`."""
    tool_calls: list[ToolCall] = []
    for call in calls:
        if isinstance(call, ToolCall):
            tool_calls.append(call)
            continue
        name, arguments, *rest = call
        call_id = rest[0] if rest else f"call_{next(_ids)}"
        tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return ModelResponse(
        message=Message(role="assistant", content=content, tool_calls=tool_calls),
        finish_reason="tool_calls",
        usage=Usage(),
        provider_name="scripted",
        model_used="scripted-model",
    )


class ScriptedModel:
    """Replays prepared replies. An item may also be an exception (raised) or a callable that
    receives the request and returns the reply (to react to the conversation)."""

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        if not self._script:
            raise AssertionError("The scripted model was asked for more replies than scripted.")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(request)
            if asyncio.iscoroutine(item):
                item = await item
        return item  # type: ignore[no-any-return]

    @property
    def remaining(self) -> int:
        return len(self._script)

    def roles(self, request_index: int = -1) -> list[str]:
        return [m.role for m in self.requests[request_index].messages]


# --- Dummy tools -------------------------------------------------------------------------------


class TextInput(BaseModel):
    text: str = ""


class EchoTool(BaseTool):
    """Read-only; no approval. Returns what it was given."""

    def __init__(self, name: str = "echo") -> None:
        super().__init__(name, "Echo the text back.", ToolCategory.READ_ONLY)
        self.calls: list[dict[str, Any]] = []

    def get_input_schema(self) -> type[BaseModel]:
        return TextInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls.append({"text": kwargs.get("text")})
        return ToolResult(success=True, data={"echo": kwargs.get("text")})


class WriteTool(BaseTool):
    """Mutating, so every call needs approval."""

    def __init__(self, name: str = "write") -> None:
        super().__init__(name, "Change something.", ToolCategory.MUTATING)
        self.calls: list[dict[str, Any]] = []

    def get_input_schema(self) -> type[BaseModel]:
        return TextInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls.append({"text": kwargs.get("text")})
        return ToolResult(success=True, data={"written": kwargs.get("text")})


class FailingTool(BaseTool):
    """Read-only; always reports failure."""

    def __init__(self, name: str = "broken") -> None:
        super().__init__(name, "Always fails.", ToolCategory.READ_ONLY)

    def get_input_schema(self) -> type[BaseModel]:
        return TextInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=False, error="the disk is on fire")


class SlowTool(BaseTool):
    """Read-only; waits until released, so a test can act while a step is running."""

    def __init__(self, name: str = "slow") -> None:
        super().__init__(name, "Waits.", ToolCategory.READ_ONLY)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def get_input_schema(self) -> type[BaseModel]:
        return TextInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.started.set()
        await self.release.wait()
        return ToolResult(success=True, data={"slow": "done"})


# --- Waiting -----------------------------------------------------------------------------------


async def wait_until(
    check: Callable[[], Any], *, timeout: float = 10.0, interval: float = 0.05
) -> Any:
    """Poll an async-or-sync `check` until it returns something truthy; return that value."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for the condition.")
        await asyncio.sleep(interval)


async def wait_for_task_status(
    uow: Any, task_id: str, *statuses: str, timeout: float = 10.0
) -> Any:
    """Wait until the task is in one of `statuses`; returns the task row."""

    async def check() -> Any:
        async with uow.begin() as unit:
            task = await unit.repositories.tasks.get(task_id)
        return task if task and task["status"] in statuses else None

    try:
        return await wait_until(check, timeout=timeout)
    except AssertionError:
        async with uow.begin() as unit:
            task = await unit.repositories.tasks.get(task_id)
        raise AssertionError(
            f"Task never reached {statuses}; it is {task['status'] if task else 'missing'}."
        ) from None

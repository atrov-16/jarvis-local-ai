from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ToolCall(BaseModel):
    """One function call requested by the model, in provider-neutral form.

    `arguments` is always a JSON object. When the model's arguments could not be used (not
    valid JSON, or not an object), `arguments` is empty and `error` says why, with the text the
    model sent kept in `raw_arguments`. That is the model's mistake, not a transport failure:
    the caller answers it with a tool message so the model can correct the call.
    """

    id: str
    name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    raw_arguments: str | None = None


class NamedToolChoice(BaseModel):
    """Force the model to call one specific tool."""

    name: str


# "auto": the model decides; "none": it must answer in text; "required": it must call a tool.
ToolChoice = Literal["auto", "none", "required"] | NamedToolChoice


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class ModelRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    temperature: float | None = 0.7
    max_tokens: int | None = None
    # OpenAI-style function tools, as built by ToolRegistry.to_openai_tools().
    tools: list[dict[str, object]] | None = None
    tool_choice: ToolChoice | None = None
    stream: bool = False

    @model_validator(mode="after")
    def _forced_tool_choice_needs_tools(self) -> ModelRequest:
        """"required" or a named choice with no tools to choose from cannot be honoured, and
        dropping it would let the model answer in text where the caller demanded a call."""
        forced = self.tool_choice == "required" or isinstance(self.tool_choice, NamedToolChoice)
        if forced and not self.tools:
            raise ValueError("tool_choice requires a call, but the request offers no tools.")
        return self

    @property
    def wants_tools(self) -> bool:
        """True when the caller offered tools, so a text-only provider must not serve it."""
        return bool(self.tools)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ModelResponse(BaseModel):
    message: Message
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    provider_name: str
    model_used: str


class ModelChunk(BaseModel):
    content_delta: str
    tool_call_deltas: list[dict[str, object]] | None = None
    finish_reason: str | None = None


class ProviderStatus(BaseModel):
    name: str
    available: bool
    error: str | None = None

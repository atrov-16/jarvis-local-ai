from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, object]] | None = None
    tool_call_id: str | None = None


class ModelRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    temperature: float | None = 0.7
    max_tokens: int | None = None
    tools: list[dict[str, object]] | None = None
    stream: bool = False


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

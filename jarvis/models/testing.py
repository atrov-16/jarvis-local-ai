from __future__ import annotations

from collections.abc import AsyncIterator

from jarvis.models.providers import ModelProvider
from jarvis.models.schemas import (
    Message,
    ModelChunk,
    ModelRequest,
    ModelResponse,
    ProviderStatus,
    Usage,
)


class FakeProvider(ModelProvider):
    def __init__(self, available: bool = True, raise_on_complete: Exception | None = None) -> None:
        self._available = available
        self._raise_on_complete = raise_on_complete

    @property
    def name(self) -> str:
        return "fake"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._raise_on_complete:
            raise self._raise_on_complete
        
        return ModelResponse(
            message=Message(role="assistant", content="Fake response"),
            finish_reason="stop",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            provider_name=self.name,
            model_used=request.model or "fake-model",
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        if self._raise_on_complete:
            raise self._raise_on_complete
            
        yield ModelChunk(content_delta="Fake ", finish_reason=None)
        yield ModelChunk(content_delta="stream", finish_reason="stop")

    async def check_availability(self) -> ProviderStatus:
        if not self._available:
            return ProviderStatus(name=self.name, available=False, error="Fake unavailable")
        return ProviderStatus(name=self.name, available=True, error=None)

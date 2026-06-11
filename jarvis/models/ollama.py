from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from jarvis.models.providers import (
    ModelProvider,
    ModelProviderError,
    RetryableProviderError,
)
from jarvis.models.schemas import (
    Message,
    ModelChunk,
    ModelRequest,
    ModelResponse,
    ProviderStatus,
    Usage,
)


class OllamaProvider(ModelProvider):
    def __init__(
        self,
        default_model: str = "llama3.1",
        base_url: str = "http://localhost:11434",
    ) -> None:
        self._default_model = default_model
        self._base_url = base_url

    @property
    def name(self) -> str:
        return "ollama"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "model": request.model or self._default_model,
            "messages": [
                {"role": msg.role, "content": msg.content} for msg in request.messages
            ],
            "stream": False,
        }
        if request.temperature is not None:
            payload["options"] = {"temperature": request.temperature}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                    timeout=60.0,
                )
                self._handle_http_errors(response)
                
                data = response.json()
                return self._parse_response(data)
            except httpx.TimeoutException as e:
                raise RetryableProviderError(f"Ollama timeout: {e}") from e
            except httpx.RequestError as e:
                raise RetryableProviderError(f"Ollama connection error: {e}") from e
            except json.JSONDecodeError as e:
                raise ModelProviderError(f"Ollama invalid JSON response: {e}") from e

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        raise NotImplementedError("Streaming not yet implemented for OllamaProvider")

    async def check_availability(self) -> ProviderStatus:
        async with httpx.AsyncClient() as client:
            try:
                # Use /api/tags as a lightweight way to check if service is up
                response = await client.get(f"{self._base_url}/api/tags", timeout=3.0)
                if response.status_code == 200:
                    return ProviderStatus(name=self.name, available=True)
                
                return ProviderStatus(
                    name=self.name,
                    available=False,
                    error=f"Ollama returned status {response.status_code}",
                )
            except Exception as e:
                return ProviderStatus(name=self.name, available=False, error=str(e))

    def _parse_response(self, data: dict[str, object]) -> ModelResponse:
        try:
            message_data = data["message"] # type: ignore
            usage = Usage(
                prompt_tokens=data.get("prompt_eval_count", 0), # type: ignore
                completion_tokens=data.get("eval_count", 0), # type: ignore
                total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0), # type: ignore
            )

            return ModelResponse(
                message=Message(
                    role=message_data["role"],
                    content=message_data.get("content") or "",
                ),
                finish_reason=data.get("done_reason"), # type: ignore
                usage=usage,
                provider_name=self.name,
                model_used=str(data.get("model", "")),
            )
        except (KeyError, TypeError) as e:
            raise ModelProviderError(f"Ollama response parsing failed: {e}") from e

    def _handle_http_errors(self, response: httpx.Response) -> None:
        if response.status_code == 200:
            return
            
        if response.status_code >= 500:
            raise RetryableProviderError(f"Ollama server error: {response.text}")
        
        raise ModelProviderError(f"Ollama unexpected status {response.status_code}: {response.text}")

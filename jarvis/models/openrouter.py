from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from jarvis.config.secrets import SecretManager
from jarvis.models.providers import (
    AuthenticationError,
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


class OpenRouterProvider(ModelProvider):
    def __init__(
        self,
        secret_manager: SecretManager,
        default_model: str = "openai/gpt-4o-mini",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        self._secret_manager = secret_manager
        self._default_model = default_model
        self._base_url = base_url

    @property
    def name(self) -> str:
        return "openrouter"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        api_key = self._get_api_key()
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/google/gemini-cli",
            "X-Title": "Jarvis",
            "Content-Type": "application/json",
        }

        payload = self._build_payload(request)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60.0,
                )
                self._handle_http_errors(response)
                
                data = response.json()
                return self._parse_response(data)
            except httpx.TimeoutException as e:
                raise RetryableProviderError(f"OpenRouter timeout: {e}") from e
            except httpx.RequestError as e:
                raise RetryableProviderError(f"OpenRouter connection error: {e}") from e
            except json.JSONDecodeError as e:
                raise ModelProviderError(f"OpenRouter invalid JSON response: {e}") from e

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        # Minimal implementation for Step 2 as requested (Step 2 focused on complete/availability)
        # We will implement full streaming in a later step if needed, or just a placeholder for now.
        raise NotImplementedError("Streaming not yet implemented for OpenRouterProvider")

    async def check_availability(self) -> ProviderStatus:
        api_key = self._secret_manager.get_openrouter_api_key()
        if not api_key:
            return ProviderStatus(
                name=self.name, 
                available=False, 
                error="OpenRouter API key not configured."
            )

        async with httpx.AsyncClient() as client:
            try:
                # We check models endpoint to verify reachability and key
                response = await client.get(
                    f"{self._base_url}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=5.0,
                )
                if response.status_code == 200:
                    return ProviderStatus(name=self.name, available=True)
                
                return ProviderStatus(
                    name=self.name, 
                    available=False, 
                    error=f"OpenRouter returned status {response.status_code}"
                )
            except Exception as e:
                return ProviderStatus(name=self.name, available=False, error=str(e))

    def _get_api_key(self) -> str:
        api_key = self._secret_manager.get_openrouter_api_key()
        if not api_key:
            raise AuthenticationError("OpenRouter API key is missing.")
        return api_key

    def _build_payload(self, request: ModelRequest) -> dict[str, object]:
        messages = []
        for msg in request.messages:
            m = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            messages.append(m)

        payload: dict[str, object] = {
            "model": request.model or self._default_model,
            "messages": messages,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = request.tools
            
        return payload

    def _parse_response(self, data: dict[str, object]) -> ModelResponse:
        try:
            choice = data["choices"][0] # type: ignore
            message_data = choice["message"]
            
            usage_data = data.get("usage", {}) # type: ignore
            usage = Usage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            )

            return ModelResponse(
                message=Message(
                    role=message_data["role"],
                    content=message_data.get("content") or "",
                    tool_calls=message_data.get("tool_calls"),
                ),
                finish_reason=choice.get("finish_reason"),
                usage=usage,
                provider_name=self.name,
                model_used=str(data.get("model", "")),
            )
        except (KeyError, IndexError, TypeError) as e:
            raise ModelProviderError(f"OpenRouter response parsing failed: {e}") from e

    def _handle_http_errors(self, response: httpx.Response) -> None:
        if response.status_code == 200:
            return
            
        if response.status_code in (401, 403):
            raise AuthenticationError(f"OpenRouter authentication failed: {response.text}")
        if response.status_code == 429:
            raise RetryableProviderError(f"OpenRouter rate limit exceeded: {response.text}")
        if response.status_code >= 500:
            raise RetryableProviderError(f"OpenRouter server error: {response.text}")
        
        raise ModelProviderError(f"OpenRouter unexpected status {response.status_code}: {response.text}")

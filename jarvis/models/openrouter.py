from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from jarvis.config.secrets import SecretManager
from jarvis.logging.redaction import redact_sensitive
from jarvis.models.providers import (
    AuthenticationError,
    InsufficientCreditsError,
    MissingCredentialsError,
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
from jarvis.models.tool_calls import (
    parse_tool_calls,
    to_openai_tool_calls,
    to_openai_tool_choice,
)

# Error bodies are kept for diagnostics, but bounded: some gateways return whole HTML pages.
MAX_ERROR_DETAIL_CHARS = 1000


class OpenRouterProvider(ModelProvider):
    supports_tools = True

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
                self._handle_http_errors(response, api_key)
                
                data = response.json()
                return self._parse_response(data, api_key)
            except httpx.TimeoutException as e:
                raise RetryableProviderError(
                    f"OpenRouter timeout: {self._sanitize(str(e), api_key)}"
                ) from e
            except httpx.RequestError as e:
                raise RetryableProviderError(
                    f"OpenRouter connection error: {self._sanitize(str(e), api_key)}"
                ) from e
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
                return ProviderStatus(
                    name=self.name, available=False, error=self._sanitize(str(e), api_key)
                )

    def _get_api_key(self) -> str:
        api_key = self._secret_manager.get_openrouter_api_key()
        if not api_key:
            raise MissingCredentialsError(
                "OpenRouter API key is missing (set OPENROUTER_API_KEY, or store it in the OS "
                "credential store as service 'Jarvis', account 'OpenRouter')."
            )
        return api_key

    def _build_payload(self, request: ModelRequest) -> dict[str, object]:
        messages: list[dict[str, object]] = []
        for msg in request.messages:
            m: dict[str, object] = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                m["tool_calls"] = to_openai_tool_calls(msg.tool_calls)
                if not msg.content:
                    # The API's form for "the reply was only tool calls".
                    m["content"] = None
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
            if request.tool_choice is not None:
                # Only meaningful (and only accepted) alongside `tools`.
                payload["tool_choice"] = to_openai_tool_choice(request.tool_choice)

        return payload

    def _parse_response(self, data: object, api_key: str = "") -> ModelResponse:
        self._raise_for_error_body(data, api_key)
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
                    tool_calls=parse_tool_calls(message_data.get("tool_calls")),
                ),
                finish_reason=choice.get("finish_reason"),
                usage=usage,
                provider_name=self.name,
                model_used=str(data.get("model", "")), # type: ignore
            )
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
            # ValueError covers pydantic's ValidationError and a malformed tool_calls list.
            raise ModelProviderError(f"OpenRouter response parsing failed: {e}") from e

    def _raise_for_error_body(self, data: object, api_key: str) -> None:
        """OpenRouter reports some failures (an upstream provider's rate limit or outage,
        exhausted credits) as HTTP 200 with an `error` object, either instead of `choices` or
        on the choice itself. Classify them like the equivalent HTTP status, so a tool-calling
        loop retries or falls back instead of failing on "response parsing failed"."""
        if not isinstance(data, dict):
            return
        error = data.get("error")
        if error is None:
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                error = choices[0].get("error")
        if not error:
            return

        code: object = None
        message: object = error
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or error
        detail = self._sanitize(str(message)[:MAX_ERROR_DETAIL_CHARS], api_key)
        status = code if isinstance(code, int) and not isinstance(code, bool) else None

        if status in (401, 403):
            raise AuthenticationError(f"OpenRouter authentication failed: {detail}")
        if status == 402:
            raise InsufficientCreditsError(
                f"OpenRouter refused the request for insufficient credits (HTTP 402): {detail}"
            )
        if status in (408, 429) or (status is not None and status >= 500):
            raise RetryableProviderError(f"OpenRouter upstream error ({status}): {detail}")
        raise ModelProviderError(f"OpenRouter returned an error ({code}): {detail}")

    def _handle_http_errors(self, response: httpx.Response, api_key: str) -> None:
        if response.status_code == 200:
            return

        detail = self._sanitize(response.text[:MAX_ERROR_DETAIL_CHARS], api_key)
        if response.status_code in (401, 403):
            raise AuthenticationError(f"OpenRouter authentication failed: {detail}")
        if response.status_code == 402:
            raise InsufficientCreditsError(
                f"OpenRouter refused the request for insufficient credits (HTTP 402): {detail}"
            )
        if response.status_code == 429:
            raise RetryableProviderError(f"OpenRouter rate limit exceeded: {detail}")
        if response.status_code >= 500:
            raise RetryableProviderError(f"OpenRouter server error: {detail}")

        raise ModelProviderError(f"OpenRouter unexpected status {response.status_code}: {detail}")

    @staticmethod
    def _sanitize(text: str, api_key: str) -> str:
        """Provider messages end up in logs, task events and API responses: strip the key."""
        return redact_sensitive(text, [api_key])

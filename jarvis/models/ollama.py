from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from jarvis.models.providers import (
    ModelProvider,
    ModelProviderError,
    RetryableProviderError,
    UnsupportedFeatureError,
)
from jarvis.models.schemas import (
    Message,
    ModelChunk,
    ModelRequest,
    ModelResponse,
    NamedToolChoice,
    ProviderStatus,
    Usage,
)
from jarvis.models.tool_calls import parse_tool_calls, to_ollama_tool_calls, tool_name_for

MAX_ERROR_DETAIL_CHARS = 1000


class OllamaProvider(ModelProvider):
    supports_tools = True

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
        payload: dict[str, object] = {
            "model": request.model or self._default_model,
            "messages": self._build_messages(request),
            "stream": False,
        }
        tools = self._tools_for(request)
        if tools:
            payload["tools"] = tools
        options: dict[str, object] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens:
            # Ollama's name for the output-token limit.
            options["num_predict"] = request.max_tokens
        if options:
            payload["options"] = options

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

    @staticmethod
    def _tools_for(request: ModelRequest) -> list[dict[str, object]] | None:
        """The `tools` to send. /api/chat has no `tool_choice`: the model always decides."""
        choice = request.tool_choice
        if not request.tools:
            return None
        if choice == "none":
            # Without the tool list the model cannot call one, which is what "none" asks for.
            return None
        wanted: str | None = None
        if isinstance(choice, NamedToolChoice):
            wanted = f"the tool '{choice.name}'"
        elif choice == "required":
            wanted = "required"
        if wanted is not None:
            raise UnsupportedFeatureError(
                f"Ollama cannot force a tool call (tool_choice={wanted}); it only supports "
                "letting the model decide."
            )
        return request.tools

    @staticmethod
    def _build_messages(request: ModelRequest) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        for index, msg in enumerate(request.messages):
            m: dict[str, object] = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                m["tool_calls"] = to_ollama_tool_calls(msg.tool_calls)
            if msg.role == "tool":
                # Ollama matches a result to its call by tool name, not by call id.
                tool_name = tool_name_for(request.messages, index)
                if tool_name:
                    m["tool_name"] = tool_name
            messages.append(m)
        return messages

    def _parse_response(self, data: dict[str, object]) -> ModelResponse:
        try:
            message_data = data["message"]
            if not isinstance(message_data, dict):
                raise TypeError(f"message is not an object (got {type(message_data).__name__})")
            usage = Usage(
                prompt_tokens=data.get("prompt_eval_count", 0), # type: ignore
                completion_tokens=data.get("eval_count", 0), # type: ignore
                total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0), # type: ignore
            )
            # Ollama gives tool calls no id; parse_tool_calls synthesizes one per call.
            tool_calls = parse_tool_calls(message_data.get("tool_calls"))

            return ModelResponse(
                message=Message(
                    role=message_data["role"],
                    content=message_data.get("content") or "",
                    tool_calls=tool_calls,
                ),
                # Ollama reports "stop" for a reply that is a tool call; name it like the
                # OpenAI-compatible providers do.
                finish_reason="tool_calls" if tool_calls else data.get("done_reason"), # type: ignore
                usage=usage,
                provider_name=self.name,
                model_used=str(data.get("model", "")),
            )
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            # ValueError covers pydantic's ValidationError and a malformed tool_calls list.
            raise ModelProviderError(f"Ollama response parsing failed: {e}") from e

    def _handle_http_errors(self, response: httpx.Response) -> None:
        if response.status_code == 200:
            return

        if response.status_code == 400 and "does not support tools" in response.text:
            # e.g. {"error":"registry.ollama.ai/library/gemma2:latest does not support tools"}
            raise UnsupportedFeatureError(
                f"Ollama model cannot use tools: {response.text[:MAX_ERROR_DETAIL_CHARS]}"
            )

        if response.status_code >= 500:
            raise RetryableProviderError(f"Ollama server error: {response.text}")
        
        raise ModelProviderError(f"Ollama unexpected status {response.status_code}: {response.text}")

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from jarvis.models.ollama import OllamaProvider
from jarvis.models.providers import (
    RetryableProviderError,
)
from jarvis.models.schemas import Message, ModelRequest


@pytest.fixture
def provider() -> OllamaProvider:
    return OllamaProvider()


async def test_ollama_complete_success(provider: OllamaProvider) -> None:
    mock_response = {
        "model": "llama3.1",
        "message": {"role": "assistant", "content": "The sky is blue."},
        "done_reason": "stop",
        "done": True,
        "prompt_eval_count": 10,
        "eval_count": 5,
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200, json=mock_response)
        
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        resp = await provider.complete(req)

        assert resp.message.content == "The sky is blue."
        assert resp.usage.total_tokens == 15
        assert resp.provider_name == "ollama"
        assert resp.model_used == "llama3.1"
        
        # Verify payload
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert kwargs["json"]["model"] == "llama3.1"
        assert kwargs["json"]["messages"][0]["content"] == "Hi"


async def test_ollama_connection_failure(provider: OllamaProvider) -> None:
    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("Connection refused")):
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        with pytest.raises(RetryableProviderError, match="connection error"):
            await provider.complete(req)


async def test_ollama_timeout(provider: OllamaProvider) -> None:
    with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("Timeout")):
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        with pytest.raises(RetryableProviderError, match="timeout"):
            await provider.complete(req)


async def test_ollama_server_error(provider: OllamaProvider) -> None:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(500, text="Ollama crashed")
        
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        with pytest.raises(RetryableProviderError, match="server error"):
            await provider.complete(req)


async def test_ollama_check_availability_success(provider: OllamaProvider) -> None:
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(200, json={"models": []})
        
        status = await provider.check_availability()
        assert status.available is True
        assert status.error is None


async def test_ollama_check_availability_failure(provider: OllamaProvider) -> None:
    with patch("httpx.AsyncClient.get", side_effect=Exception("Service down")):
        status = await provider.check_availability()
        assert status.available is False
        assert "Service down" in status.error

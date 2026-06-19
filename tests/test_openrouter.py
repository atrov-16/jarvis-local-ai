from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from jarvis.config.secrets import SecretManager
from jarvis.models.openrouter import OpenRouterProvider
from jarvis.models.providers import (
    AuthenticationError,
    RetryableProviderError,
)
from jarvis.models.schemas import Message, ModelRequest


@pytest.fixture
def secret_manager() -> SecretManager:
    return SecretManager({"OPENROUTER_API_KEY": "sk-test-key"}, use_keyring=False)


@pytest.fixture
def provider(secret_manager: SecretManager) -> OpenRouterProvider:
    return OpenRouterProvider(secret_manager)


async def test_openrouter_complete_success(provider: OpenRouterProvider) -> None:
    mock_response = {
        "id": "gen-123",
        "model": "openai/gpt-4o-mini",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello world!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200, json=mock_response)
        
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        resp = await provider.complete(req)

        assert resp.message.content == "Hello world!"
        assert resp.usage.total_tokens == 15
        assert resp.provider_name == "openrouter"
        assert resp.model_used == "openai/gpt-4o-mini"
        
        # Verify payload
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert kwargs["json"]["model"] == "openai/gpt-4o-mini"
        assert kwargs["json"]["messages"][0]["content"] == "Hi"
        assert "Authorization" in kwargs["headers"]
        assert "Bearer sk-test-key" in kwargs["headers"]["Authorization"]


async def test_openrouter_authentication_failure(provider: OpenRouterProvider) -> None:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(401, text="Invalid key")
        
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        with pytest.raises(AuthenticationError, match="authentication failed"):
            await provider.complete(req)


async def test_openrouter_rate_limit(provider: OpenRouterProvider) -> None:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(429, text="Too many requests")
        
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        with pytest.raises(RetryableProviderError, match="rate limit exceeded"):
            await provider.complete(req)


async def test_openrouter_server_error(provider: OpenRouterProvider) -> None:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(500, text="Internal Server Error")
        
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        with pytest.raises(RetryableProviderError, match="server error"):
            await provider.complete(req)


async def test_openrouter_timeout(provider: OpenRouterProvider) -> None:
    with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("Timeout")):
        req = ModelRequest(messages=[Message(role="user", content="Hi")])
        with pytest.raises(RetryableProviderError, match="timeout"):
            await provider.complete(req)


async def test_openrouter_check_availability_success(provider: OpenRouterProvider) -> None:
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(200, json={"data": []})
        
        status = await provider.check_availability()
        assert status.available is True
        assert status.error is None


async def test_openrouter_check_availability_failure(provider: OpenRouterProvider) -> None:
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(401, text="Unauthorized")
        
        status = await provider.check_availability()
        assert status.available is False
        assert "status 401" in status.error


async def test_openrouter_check_availability_no_key(secret_manager: SecretManager) -> None:
    secret_manager._env = {} # Clear env
    provider = OpenRouterProvider(secret_manager)
    
    status = await provider.check_availability()
    assert status.available is False
    assert "key not configured" in status.error

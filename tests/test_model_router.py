from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jarvis.config.models import JarvisConfig, ModelsConfig
from jarvis.config.secrets import SecretManager
from jarvis.models.providers import (
    AuthenticationError,
    RetryableProviderError,
)
from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelRequest, ProviderStatus
from jarvis.models.testing import FakeProvider


@pytest.fixture
def config() -> JarvisConfig:
    return JarvisConfig(
        models=ModelsConfig(
            primary_provider="openrouter",
            fallback_provider="ollama",
        )
    )


@pytest.fixture
def secret_manager() -> SecretManager:
    return SecretManager({}, use_keyring=False)


async def test_router_complete_primary_success(config: JarvisConfig, secret_manager: SecretManager) -> None:
    router = ModelRouter(config, secret_manager)
    
    # Mock providers
    primary = FakeProvider()
    fallback = FakeProvider()
    router._providers = {"openrouter": primary, "ollama": fallback}
    
    req = ModelRequest(messages=[Message(role="user", content="Hi")])
    resp = await router.complete(req)
    
    assert resp.provider_name == "fake"
    assert resp.message.content == "Fake response"


async def test_router_complete_fallback_activation(config: JarvisConfig, secret_manager: SecretManager) -> None:
    router = ModelRouter(config, secret_manager)
    
    # Primary fails with retryable error
    primary = FakeProvider(raise_on_complete=RetryableProviderError("Primary down"))
    # Fallback succeeds
    fallback = FakeProvider()
    fallback._name = "ollama-fake" # type: ignore
    
    router._providers = {"openrouter": primary, "ollama": fallback}
    
    req = ModelRequest(messages=[Message(role="user", content="Hi")])
    resp = await router.complete(req)
    
    assert resp.message.content == "Fake response"
    # Note: FakeProvider currently hardcodes "fake" as name, 
    # but the fact it returned means it fell back.
    # Let's verify by checking if fallback was called.
    
    # Alternatively, wrap them in Spies
    router._providers["ollama"] = MagicMock(spec=FakeProvider, wraps=fallback)
    resp = await router.complete(req)
    router._providers["ollama"].complete.assert_called_once()


async def test_router_no_fallback_on_auth_error(config: JarvisConfig, secret_manager: SecretManager) -> None:
    router = ModelRouter(config, secret_manager)
    
    primary = FakeProvider(raise_on_complete=AuthenticationError("Invalid API Key"))
    fallback = MagicMock(spec=FakeProvider)
    
    router._providers = {"openrouter": primary, "ollama": fallback}
    
    req = ModelRequest(messages=[Message(role="user", content="Hi")])
    with pytest.raises(AuthenticationError):
        await router.complete(req)
        
    fallback.complete.assert_not_called()


async def test_router_fallback_failure(config: JarvisConfig, secret_manager: SecretManager) -> None:
    router = ModelRouter(config, secret_manager)
    
    primary = FakeProvider(raise_on_complete=RetryableProviderError("Primary down"))
    fallback = FakeProvider(raise_on_complete=RetryableProviderError("Fallback also down"))
    
    router._providers = {"openrouter": primary, "ollama": fallback}
    
    req = ModelRequest(messages=[Message(role="user", content="Hi")])
    with pytest.raises(RetryableProviderError, match="Fallback also down"):
        await router.complete(req)


async def test_router_availability_aggregation(config: JarvisConfig, secret_manager: SecretManager) -> None:
    router = ModelRouter(config, secret_manager)
    
    primary = FakeProvider(name="openrouter", available=True)
    fallback = FakeProvider(name="ollama", available=False)
    
    router._providers = {"openrouter": primary, "ollama": fallback}
    
    statuses = await router.check_availability()
    assert len(statuses) == 2
    
    status_map = {s.name: s for s in statuses}
    assert status_map["openrouter"].available is True
    assert status_map["ollama"].available is False
    assert status_map["ollama"].error == "Fake unavailable"

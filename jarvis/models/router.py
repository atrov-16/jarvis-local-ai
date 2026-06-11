from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager
from jarvis.models.ollama import OllamaProvider
from jarvis.models.openrouter import OpenRouterProvider
from jarvis.models.providers import (
    AuthenticationError,
    ModelProvider,
    ModelProviderError,
    RetryableProviderError,
    UnsupportedFeatureError,
)
from jarvis.models.schemas import ModelChunk, ModelRequest, ModelResponse, ProviderStatus


class ModelRouter:
    def __init__(self, config: JarvisConfig, secret_manager: SecretManager) -> None:
        self._config = config
        self._secret_manager = secret_manager
        
        # Initialize providers
        self._providers: dict[str, ModelProvider] = {
            "openrouter": OpenRouterProvider(
                secret_manager=secret_manager,
                default_model=config.models.default_model,
            ),
            "ollama": OllamaProvider(
                default_model=config.models.local_model,
            ),
        }

    async def complete(self, request: ModelRequest) -> ModelResponse:
        primary_name = self._config.models.primary_provider
        fallback_name = self._config.models.fallback_provider
        
        primary = self._get_provider(primary_name)
        
        try:
            return await primary.complete(request)
        except (AuthenticationError, UnsupportedFeatureError):
            # Do not fallback for configuration or feature errors
            raise
        except RetryableProviderError:
            if primary_name == fallback_name:
                raise
            
            fallback = self._get_provider(fallback_name)
            return await fallback.complete(request)
        except ModelProviderError:
            # Other non-retryable provider errors
            raise

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        # Minimal implementation as requested
        primary_name = self._config.models.primary_provider
        primary = self._get_provider(primary_name)
        
        # Streaming fallback is complex (only possible before first chunk).
        # For Phase 3, we implement basic behavior.
        async for chunk in primary.stream(request):
            yield chunk

    async def check_availability(self) -> list[ProviderStatus]:
        tasks = [
            provider.check_availability() 
            for provider in self._providers.values()
        ]
        return list(await asyncio.gather(*tasks))

    def _get_provider(self, name: str) -> ModelProvider:
        provider = self._providers.get(name)
        if not provider:
            raise ModelProviderError(f"Unknown provider: {name}")
        return provider

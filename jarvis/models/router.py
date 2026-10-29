from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager
from jarvis.models.ollama import OllamaProvider
from jarvis.models.openrouter import OpenRouterProvider
from jarvis.models.providers import (
    AuthenticationError,
    InsufficientCreditsError,
    MissingCredentialsError,
    ModelProvider,
    ModelProviderError,
    RetryableProviderError,
    UnsupportedFeatureError,
)
from jarvis.models.schemas import ModelChunk, ModelRequest, ModelResponse, ProviderStatus

LOG = logging.getLogger(__name__)


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
        primary = self._get_provider(primary_name)

        try:
            return await primary.complete(request)
        except (MissingCredentialsError, InsufficientCreditsError) as e:
            # The primary cannot serve this request at all (no key / no credits) and retrying
            # it would not help, but a configured (local) fallback can.
            return await self._complete_with_fallback(primary_name, request, e)
        except (AuthenticationError, UnsupportedFeatureError):
            # Do not fallback for configuration or feature errors
            raise
        except RetryableProviderError as e:
            fallback = self._get_fallback(primary_name)
            if fallback is None:
                raise
            self._log_fallback(primary_name, fallback, e)
            return await fallback.complete(request)
        except ModelProviderError:
            # Other non-retryable provider errors
            raise

    async def _complete_with_fallback(
        self,
        primary_name: str,
        request: ModelRequest,
        error: MissingCredentialsError | InsufficientCreditsError,
    ) -> ModelResponse:
        """One attempt on the fallback. Without a fallback, `error` is raised unchanged; if
        the fallback fails, the error carries both causes, so the root cause (no key, no
        credits) stays visible."""
        fallback_name = self._config.models.fallback_provider
        try:
            fallback = self._get_fallback(primary_name)
        except ModelProviderError as lookup_error:  # e.g. a misspelled fallback_provider
            raise type(error)(
                f"{error} Fallback provider '{fallback_name}' is not available: {lookup_error}"
            ) from lookup_error
        if fallback is None:
            raise error
        if isinstance(error, MissingCredentialsError):
            # Expected in an Ollama-only setup: not worth a warning on every request.
            LOG.info(f"Provider '{primary_name}' is not configured; using '{fallback.name}'.")
        else:
            self._log_fallback(primary_name, fallback, error)
        try:
            return await fallback.complete(request)
        except ModelProviderError as fallback_error:
            message = f"{error} Fallback provider '{fallback_name}' also failed: {fallback_error}"
            if isinstance(error, MissingCredentialsError) and isinstance(
                fallback_error, RetryableProviderError
            ):
                # The fallback is the only usable provider, so its transient failure stays
                # retryable (e.g. reflection retries an Ollama timeout).
                raise RetryableProviderError(message) from fallback_error
            raise type(error)(message) from fallback_error

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

    @staticmethod
    def _log_fallback(primary_name: str, fallback: ModelProvider, error: Exception) -> None:
        LOG.warning(
            f"Provider '{primary_name}' failed ({error}); falling back to '{fallback.name}'."
        )

    def _get_fallback(self, primary_name: str) -> ModelProvider | None:
        """The configured fallback provider; None when it is unset ("") or the primary."""
        fallback_name = self._config.models.fallback_provider
        if not fallback_name or fallback_name == primary_name:
            return None
        return self._get_provider(fallback_name)

    def _get_provider(self, name: str) -> ModelProvider:
        provider = self._providers.get(name)
        if not provider:
            raise ModelProviderError(f"Unknown provider: {name}")
        return provider

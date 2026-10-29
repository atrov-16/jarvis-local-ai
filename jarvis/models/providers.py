from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from jarvis.models.schemas import ModelChunk, ModelRequest, ModelResponse, ProviderStatus


class ModelProviderError(Exception):
    """Base exception for all model provider errors."""
    pass


class AuthenticationError(ModelProviderError):
    """Raised when API keys are missing, invalid, or unauthorized."""
    pass


class MissingCredentialsError(AuthenticationError):
    """Raised when no API key is configured at all (as opposed to a rejected key).

    Unlike a rejected key (401/403), it is not a mistake to report but a provider that is
    simply not set up, so the router may serve the request with a configured fallback.
    """
    pass


class RetryableProviderError(ModelProviderError):
    """Raised for timeouts, 502 Bad Gateway, 429 Rate Limits, or connection drops."""
    pass


class InsufficientCreditsError(ModelProviderError):
    """Raised when the provider refuses a request it cannot bill (HTTP 402).

    Not retryable: the same request fails again until credits are added. Another provider
    (e.g. a local Ollama fallback) can still serve it.
    """
    pass


class UnsupportedFeatureError(ModelProviderError):
    """Raised when a request asks for a feature the provider/model cannot support."""
    pass


class ModelProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """The canonical name of the provider (e.g., 'openrouter', 'ollama')."""
        pass

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """
        Execute a standard blocking completion request.
        
        Raises:
            ModelProviderError (or subclasses) on failure.
        """
        pass

    @abstractmethod
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        """
        Execute a streaming completion request.
        
        Raises:
            ModelProviderError if the connection fails before the stream begins.
        """
        pass

    @abstractmethod
    async def check_availability(self) -> ProviderStatus:
        """
        Perform a fast (e.g., < 3s timeout) check to verify the provider is reachable.
        Should not raise exceptions; returns a Status object.
        """
        pass

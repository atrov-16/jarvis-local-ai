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


class RetryableProviderError(ModelProviderError):
    """Raised for timeouts, 502 Bad Gateway, 429 Rate Limits, or connection drops."""
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

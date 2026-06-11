from __future__ import annotations

import pytest

from jarvis.models.providers import (
    AuthenticationError,
    ModelProviderError,
    RetryableProviderError,
    UnsupportedFeatureError,
)
from jarvis.models.schemas import Message, ModelRequest
from jarvis.models.testing import FakeProvider


async def test_schemas() -> None:
    msg = Message(role="user", content="Hello")
    req = ModelRequest(messages=[msg], model="test-model")
    assert req.model == "test-model"
    assert req.stream is False
    assert msg.role == "user"


async def test_exception_hierarchy() -> None:
    assert issubclass(AuthenticationError, ModelProviderError)
    assert issubclass(RetryableProviderError, ModelProviderError)
    assert issubclass(UnsupportedFeatureError, ModelProviderError)


async def test_fake_provider_complete() -> None:
    provider = FakeProvider()
    req = ModelRequest(messages=[Message(role="user", content="Hi")])
    resp = await provider.complete(req)
    
    assert resp.message.content == "Fake response"
    assert resp.provider_name == "fake"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 15


async def test_fake_provider_stream() -> None:
    provider = FakeProvider()
    req = ModelRequest(messages=[Message(role="user", content="Hi")])
    
    chunks = []
    async for chunk in provider.stream(req):
        chunks.append(chunk)
    
    assert len(chunks) == 2
    assert chunks[0].content_delta == "Fake "
    assert chunks[0].finish_reason is None
    assert chunks[1].content_delta == "stream"
    assert chunks[1].finish_reason == "stop"


async def test_fake_provider_availability() -> None:
    provider_up = FakeProvider(available=True)
    status_up = await provider_up.check_availability()
    assert status_up.available is True
    assert status_up.error is None

    provider_down = FakeProvider(available=False)
    status_down = await provider_down.check_availability()
    assert status_down.available is False
    assert status_down.error == "Fake unavailable"


async def test_fake_provider_exceptions() -> None:
    provider = FakeProvider(raise_on_complete=RetryableProviderError("Timeout"))
    req = ModelRequest(messages=[Message(role="user", content="Hi")])
    
    with pytest.raises(RetryableProviderError):
        await provider.complete(req)

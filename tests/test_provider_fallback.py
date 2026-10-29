"""F-08: HTTP 402 handling, Ollama fallback and output-token budgets.

No real network: providers run over httpx.MockTransport or are fakes, and nothing assumes an
Ollama server exists on this machine.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from jarvis.config.models import JarvisConfig, ModelsConfig
from jarvis.config.secrets import SecretManager
from jarvis.models.openrouter import MAX_ERROR_DETAIL_CHARS, OpenRouterProvider
from jarvis.models.providers import (
    AuthenticationError,
    InsufficientCreditsError,
    ModelProviderError,
    RetryableProviderError,
)
from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelRequest
from jarvis.models.testing import FakeProvider

PAYMENT_REQUIRED = InsufficientCreditsError(
    "OpenRouter refused the request for insufficient credits (HTTP 402): "
    '{"error": {"message": "This request requires more credits", "code": 402}}'
)


def _router(fallback_provider: str = "ollama") -> ModelRouter:
    config = JarvisConfig(
        models=ModelsConfig(primary_provider="openrouter", fallback_provider=fallback_provider)
    )
    return ModelRouter(config, SecretManager({"OPENROUTER_API_KEY": "sk-dummy"}, use_keyring=False))


def _spy(provider: FakeProvider) -> MagicMock:
    spy = MagicMock(spec=FakeProvider, wraps=provider)
    spy.name = provider.name  # `name` is a property; the router reads it for diagnostics
    return spy


def _request(max_tokens: int | None = 256) -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content="Hi")], max_tokens=max_tokens)


async def test_successful_primary_request_does_not_touch_fallback() -> None:
    router = _router()
    primary, fallback = _spy(FakeProvider("openrouter")), _spy(FakeProvider("ollama"))
    router._providers = {"openrouter": primary, "ollama": fallback}

    response = await router.complete(_request())

    assert response.provider_name == "openrouter"
    primary.complete.assert_awaited_once()
    fallback.complete.assert_not_called()


async def test_402_falls_back_to_working_ollama_once() -> None:
    router = _router()
    primary = _spy(FakeProvider("openrouter", raise_on_complete=PAYMENT_REQUIRED))
    fallback = _spy(FakeProvider("ollama"))
    router._providers = {"openrouter": primary, "ollama": fallback}

    response = await router.complete(_request())

    assert response.provider_name == "ollama"
    primary.complete.assert_awaited_once()  # the unpaid request is not retried
    fallback.complete.assert_awaited_once()


async def test_402_with_unavailable_fallback_fails_with_both_diagnostics() -> None:
    router = _router()
    down = RetryableProviderError("Ollama connection error: [Errno 61] Connection refused")
    primary = _spy(FakeProvider("openrouter", raise_on_complete=PAYMENT_REQUIRED))
    fallback = _spy(FakeProvider("ollama", raise_on_complete=down))
    router._providers = {"openrouter": primary, "ollama": fallback}

    with pytest.raises(InsufficientCreditsError) as excinfo:
        await router.complete(_request())

    message = str(excinfo.value)
    assert "insufficient credits (HTTP 402)" in message
    assert "requires more credits" in message
    assert "Fallback provider 'ollama' also failed" in message
    assert "Connection refused" in message
    assert excinfo.value.__cause__ is down
    primary.complete.assert_awaited_once()
    fallback.complete.assert_awaited_once()


@pytest.mark.parametrize("fallback_provider", ["", "openrouter"])
async def test_402_without_a_distinct_fallback_is_raised_unchanged(fallback_provider: str) -> None:
    router = _router(fallback_provider)
    primary = _spy(FakeProvider("openrouter", raise_on_complete=PAYMENT_REQUIRED))
    other = _spy(FakeProvider("ollama"))
    router._providers = {"openrouter": primary, "ollama": other}

    with pytest.raises(InsufficientCreditsError) as excinfo:
        await router.complete(_request())

    assert excinfo.value is PAYMENT_REQUIRED
    primary.complete.assert_awaited_once()
    other.complete.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("OpenRouter authentication failed: invalid key"),
        ModelProviderError("OpenRouter unexpected status 400: bad request"),
    ],
)
async def test_unrelated_non_retryable_failures_do_not_fall_back(error: Exception) -> None:
    router = _router()
    primary = _spy(FakeProvider("openrouter", raise_on_complete=error))
    fallback = _spy(FakeProvider("ollama"))
    router._providers = {"openrouter": primary, "ollama": fallback}

    with pytest.raises(type(error)) as excinfo:
        await router.complete(_request())

    assert excinfo.value is error
    primary.complete.assert_awaited_once()
    fallback.complete.assert_not_called()


async def test_transient_failure_still_falls_back() -> None:
    router = _router()
    primary = _spy(FakeProvider("openrouter", raise_on_complete=RetryableProviderError("502")))
    fallback = _spy(FakeProvider("ollama"))
    router._providers = {"openrouter": primary, "ollama": fallback}

    assert (await router.complete(_request())).provider_name == "ollama"


# --- Real providers over a mock transport ------------------------------------------------------


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"handler": None, "requests": []}

    def dispatch(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)  # type: ignore[attr-defined]
        return state["handler"](request)  # type: ignore[operator]

    real_client = httpx.AsyncClient
    mock = httpx.MockTransport(dispatch)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *args, **kwargs: real_client(*args, transport=mock, **kwargs)
    )
    return state


def _ollama_reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "llama3.1",
            "message": {"role": "assistant", "content": "local answer"},
            "done_reason": "stop",
            "prompt_eval_count": 3,
            "eval_count": 2,
        },
    )


async def test_openrouter_402_is_insufficient_credits_not_retryable(transport) -> None:
    transport["handler"] = lambda request: httpx.Response(402, text="x" * 5000)
    provider = OpenRouterProvider(
        SecretManager({"OPENROUTER_API_KEY": "sk-dummy"}, use_keyring=False)
    )

    with pytest.raises(InsufficientCreditsError) as excinfo:
        await provider.complete(_request())

    assert not isinstance(excinfo.value, RetryableProviderError)
    assert str(excinfo.value).count("x") == MAX_ERROR_DETAIL_CHARS  # bounded diagnostic


async def test_end_to_end_402_then_local_ollama(transport) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(402, json={"error": {"message": "more credits", "code": 402}})
        assert (request.url.host, request.url.port) == ("localhost", 11434)
        return _ollama_reply(request)

    transport["handler"] = handler
    response = await _router().complete(_request(max_tokens=300))

    assert response.provider_name == "ollama"
    assert response.message.content == "local answer"
    hosts = [r.url.host for r in transport["requests"]]  # type: ignore[attr-defined]
    assert hosts == ["openrouter.ai", "localhost"]  # one attempt each
    ollama_payload = transport["requests"][1].read()  # type: ignore[index]
    assert b'"num_predict":300' in ollama_payload.replace(b" ", b"")


async def test_end_to_end_402_with_ollama_not_running(transport) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(402, json={"error": {"message": "more credits", "code": 402}})
        raise httpx.ConnectError("[Errno 61] Connection refused")

    transport["handler"] = handler
    with pytest.raises(InsufficientCreditsError) as excinfo:
        await _router().complete(_request())

    assert "HTTP 402" in str(excinfo.value)
    assert "Ollama connection error" in str(excinfo.value)
    assert len(transport["requests"]) == 2  # type: ignore[arg-type]


async def test_ollama_receives_the_output_budget(transport) -> None:
    from jarvis.models.ollama import OllamaProvider

    transport["handler"] = _ollama_reply
    await OllamaProvider().complete(_request(max_tokens=128))
    await OllamaProvider().complete(_request(max_tokens=None))

    first, second = (r.read().replace(b" ", b"") for r in transport["requests"])  # type: ignore[attr-defined]
    assert b'"options":{"temperature":0.7,"num_predict":128}' in first
    assert b"num_predict" not in second


def test_router_is_not_retried_by_callers_on_402() -> None:
    # The classification callers rely on: 402 is neither retryable nor an auth error.
    assert issubclass(InsufficientCreditsError, ModelProviderError)
    assert not issubclass(InsufficientCreditsError, RetryableProviderError)
    assert not issubclass(InsufficientCreditsError, AuthenticationError)


async def test_router_fallback_error_is_not_swallowed_for_transient_errors() -> None:
    router = _router()
    router._providers = {
        "openrouter": FakeProvider("openrouter", raise_on_complete=RetryableProviderError("down")),
        "ollama": FakeProvider("ollama", raise_on_complete=RetryableProviderError("also down")),
    }
    with pytest.raises(RetryableProviderError, match="also down"):
        await router.complete(_request())


def test_reflection_budget_is_configurable_and_wired(tmp_path) -> None:
    from unittest.mock import patch

    from jarvis.api.http import create_app
    from jarvis.config.models import MemoryConfig

    config = JarvisConfig(
        models=ModelsConfig(reflection_max_tokens=512),
        memory=MemoryConfig(database_path=tmp_path / "db.sqlite"),
    )
    with patch("jarvis.api.http.ReflectionService") as reflection_service:
        create_app(config=config, secret_manager=SecretManager({}, use_keyring=False))

    assert reflection_service.call_args.kwargs["max_tokens"] == 512
    with pytest.raises(ValueError):
        ModelsConfig(reflection_max_tokens=0)


async def test_reflection_request_uses_its_budget() -> None:
    from jarvis.core.reflection import ReflectionService

    router = AsyncMock()
    router.complete.return_value.message.content = '{"proposals": []}'
    store = AsyncMock()
    store.get_planner_context.return_value = ("", [])
    service = ReflectionService(MagicMock(), MagicMock(), router, store, max_tokens=333)
    service._is_nontrivial = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service._aggregator.aggregate = AsyncMock(return_value="trace")  # type: ignore[method-assign]
    unit = service._uow.begin.return_value.__aenter__.return_value
    unit.repositories.tasks.get = AsyncMock(return_value={"user_request": "r"})

    await service.reflect_on_task("t1")

    assert router.complete.await_args.args[0].max_tokens == 333


async def test_402_with_misspelled_fallback_keeps_the_root_cause() -> None:
    router = _router(fallback_provider="Ollama")  # providers are lower-case
    router._providers = {
        "openrouter": FakeProvider("openrouter", raise_on_complete=PAYMENT_REQUIRED),
        "ollama": FakeProvider("ollama"),
    }
    with pytest.raises(InsufficientCreditsError) as excinfo:
        await router.complete(_request())
    message = str(excinfo.value)
    assert "insufficient credits (HTTP 402)" in message
    assert "Fallback provider 'Ollama' is not available: Unknown provider: Ollama" in message


@pytest.mark.parametrize(
    "body",
    [
        {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": None},
        {"choices": [{"message": {"role": None, "content": "hi"}}]},
        ["not", "an", "object"],
    ],
)
async def test_malformed_openrouter_response_is_a_provider_error(transport, body) -> None:
    transport["handler"] = lambda request: httpx.Response(200, json=body)
    provider = OpenRouterProvider(
        SecretManager({"OPENROUTER_API_KEY": "sk-dummy"}, use_keyring=False)
    )
    with pytest.raises(ModelProviderError, match="parsing failed"):
        await provider.complete(_request())


async def test_malformed_ollama_response_is_a_provider_error(transport) -> None:
    from jarvis.models.ollama import OllamaProvider

    transport["handler"] = lambda request: httpx.Response(200, json={"message": None})
    with pytest.raises(ModelProviderError, match="parsing failed"):
        await OllamaProvider().complete(_request())

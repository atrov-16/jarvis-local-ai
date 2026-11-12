"""Slice 1: native tool calling through the OpenRouter and Ollama providers and the router.

No real network: providers run over httpx.MockTransport or are fakes.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from jarvis.config.models import JarvisConfig, ModelsConfig
from jarvis.config.secrets import SecretManager
from jarvis.models.ollama import OllamaProvider
from jarvis.models.openrouter import OpenRouterProvider
from jarvis.models.providers import (
    AuthenticationError,
    InsufficientCreditsError,
    MissingCredentialsError,
    ModelProviderError,
    RetryableProviderError,
    UnsupportedFeatureError,
)
from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelRequest, NamedToolChoice, ToolCall
from jarvis.models.testing import FakeProvider

TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]
USER = Message(role="user", content="Show me a.txt")
API_KEY = "sk-dummy-key-1234567890"


class ToolCapableFake(FakeProvider):
    supports_tools = True


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


def _sent(transport: dict[str, object], index: int = -1) -> dict[str, object]:
    return json.loads(transport["requests"][index].read())  # type: ignore[index]


def _openrouter(key: str | None = API_KEY) -> OpenRouterProvider:
    env = {"OPENROUTER_API_KEY": key} if key else {}
    return OpenRouterProvider(SecretManager(env, use_keyring=False))


def _openrouter_reply(message: dict[str, object], **extra: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "openai/gpt-4o-mini",
            "choices": [{"finish_reason": "stop", "message": message, **extra}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        },
    )


def _router(
    primary: str = "openrouter", fallback: str = "ollama", key: str | None = API_KEY
) -> ModelRouter:
    config = JarvisConfig(
        models=ModelsConfig(primary_provider=primary, fallback_provider=fallback)
    )
    env = {"OPENROUTER_API_KEY": key} if key else {}
    return ModelRouter(config, SecretManager(env, use_keyring=False))


def _ollama_tool_reply() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "llama3.1",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}],
            },
            "done_reason": "stop",
            "prompt_eval_count": 3,
            "eval_count": 2,
        },
    )


# --- OpenRouter round trip ---------------------------------------------------------------------


async def test_openrouter_tool_call_round_trip(transport: dict[str, object]) -> None:
    transport["handler"] = lambda request: _openrouter_reply(
        {
            "role": "assistant",
            "content": None,  # a reply that is only tool calls
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                }
            ],
        },
        finish_reason="tool_calls",
    )
    provider = _openrouter()

    first = await provider.complete(
        ModelRequest(messages=[USER], tools=TOOLS, tool_choice="required")
    )

    sent = _sent(transport)
    assert sent["tools"] == TOOLS
    assert sent["tool_choice"] == "required"
    assert first.finish_reason == "tool_calls"
    assert first.message.content == ""
    assert first.message.tool_calls == [
        ToolCall(id="call_abc", name="read_file", arguments={"path": "a.txt"})
    ]

    # Second turn: the history goes back with the call and its result.
    transport["handler"] = lambda request: _openrouter_reply(
        {"role": "assistant", "content": "It says hello."}
    )
    history = [USER, first.message, Message(role="tool", content="hello", tool_call_id="call_abc")]
    final = await provider.complete(ModelRequest(messages=history, tools=TOOLS))

    sent = _sent(transport)
    assistant, tool_message = sent["messages"][1], sent["messages"][2]  # type: ignore[index]
    assert assistant["content"] is None  # null, not "", for a tool-only reply
    (replayed,) = assistant["tool_calls"]
    assert replayed["id"] == "call_abc" and replayed["type"] == "function"
    assert replayed["function"]["name"] == "read_file"
    assert isinstance(replayed["function"]["arguments"], str)  # OpenAI wire format: a string
    assert json.loads(replayed["function"]["arguments"]) == {"path": "a.txt"}
    assert tool_message == {"role": "tool", "content": "hello", "tool_call_id": "call_abc"}
    assert "tool_choice" not in sent  # none requested
    assert final.message.content == "It says hello."
    assert final.message.tool_calls is None


async def test_openrouter_named_tool_choice_uses_the_function_form(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: _openrouter_reply({"role": "assistant", "content": "x"})

    await _openrouter().complete(
        ModelRequest(messages=[USER], tools=TOOLS, tool_choice=NamedToolChoice(name="read_file"))
    )

    assert _sent(transport)["tool_choice"] == {
        "type": "function",
        "function": {"name": "read_file"},
    }


async def test_openrouter_without_tools_sends_neither_tools_nor_tool_choice(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: _openrouter_reply({"role": "assistant", "content": "x"})

    await _openrouter().complete(ModelRequest(messages=[USER], tool_choice="auto"))

    sent = _sent(transport)
    assert "tools" not in sent and "tool_choice" not in sent


async def test_openrouter_malformed_arguments_are_a_call_error_not_an_exception(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: _openrouter_reply(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{bad"},
                }
            ],
        }
    )

    response = await _openrouter().complete(ModelRequest(messages=[USER], tools=TOOLS))

    (call,) = response.message.tool_calls or []
    assert call.id == "c1" and call.name == "read_file"
    assert call.error and call.arguments == {} and call.raw_arguments == "{bad"


async def test_openrouter_unusable_tool_calls_structure_is_a_provider_error(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: _openrouter_reply(
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    )

    with pytest.raises(ModelProviderError, match="response parsing failed"):
        await _openrouter().complete(ModelRequest(messages=[USER], tools=TOOLS))


# --- Ollama round trip -------------------------------------------------------------------------


async def test_ollama_tool_call_round_trip(transport: dict[str, object]) -> None:
    transport["handler"] = lambda request: httpx.Response(
        200,
        json={
            "model": "llama3.1",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
                    {"function": {"name": "read_file", "arguments": {"path": "b.txt"}}},
                ],
            },
            "done_reason": "stop",  # Ollama says "stop" even for a tool call
            "prompt_eval_count": 3,
            "eval_count": 2,
        },
    )
    provider = OllamaProvider()

    first = await provider.complete(ModelRequest(messages=[USER], tools=TOOLS))

    sent = _sent(transport)
    assert sent["tools"] == TOOLS
    assert "tool_choice" not in sent  # /api/chat has none
    assert first.finish_reason == "tool_calls"
    calls = first.message.tool_calls or []
    assert [c.arguments for c in calls] == [{"path": "a.txt"}, {"path": "b.txt"}]  # objects
    assert all(c.id for c in calls) and len({c.id for c in calls}) == 2  # synthesized, unique

    transport["handler"] = lambda request: httpx.Response(
        200,
        json={
            "model": "llama3.1",
            "message": {"role": "assistant", "content": "done"},
            "done_reason": "stop",
        },
    )
    history = [
        USER,
        first.message,
        Message(role="tool", content="AAA", tool_call_id=calls[0].id),
        Message(role="tool", content="BBB", tool_call_id=calls[1].id),
    ]
    final = await provider.complete(ModelRequest(messages=history, tools=TOOLS))

    messages = _sent(transport)["messages"]
    assert messages[1]["tool_calls"] == [  # type: ignore[index]
        {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
        {"function": {"name": "read_file", "arguments": {"path": "b.txt"}}},
    ]
    # Ollama matches results to calls by tool name; it has no tool_call_id.
    assert messages[2] == {"role": "tool", "content": "AAA", "tool_name": "read_file"}  # type: ignore[index]
    assert messages[3] == {"role": "tool", "content": "BBB", "tool_name": "read_file"}  # type: ignore[index]
    assert final.message.content == "done"
    assert final.finish_reason == "stop"
    assert final.message.tool_calls is None


async def test_ollama_tool_choice_none_does_not_offer_the_tools(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: httpx.Response(
        200, json={"model": "m", "message": {"role": "assistant", "content": "x"}}
    )

    await OllamaProvider().complete(ModelRequest(messages=[USER], tools=TOOLS, tool_choice="none"))

    assert "tools" not in _sent(transport)


@pytest.mark.parametrize("choice", ["required", NamedToolChoice(name="read_file")])
async def test_ollama_cannot_force_a_tool_call(
    transport: dict[str, object], choice: object
) -> None:
    transport["handler"] = lambda request: _ollama_tool_reply()

    with pytest.raises(UnsupportedFeatureError, match="cannot force a tool call"):
        await OllamaProvider().complete(
            ModelRequest(messages=[USER], tools=TOOLS, tool_choice=choice)  # type: ignore[arg-type]
        )

    assert transport["requests"] == []  # refused before anything was sent


async def test_ollama_model_without_tool_support_is_unsupported_feature(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: httpx.Response(
        400, json={"error": "registry.ollama.ai/library/gemma2:latest does not support tools"}
    )

    with pytest.raises(UnsupportedFeatureError, match="does not support tools"):
        await OllamaProvider().complete(ModelRequest(messages=[USER], tools=TOOLS))


async def test_ollama_other_400_is_not_an_unsupported_feature(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: httpx.Response(400, json={"error": "invalid options"})

    with pytest.raises(ModelProviderError) as excinfo:
        await OllamaProvider().complete(ModelRequest(messages=[USER], tools=TOOLS))

    assert not isinstance(excinfo.value, UnsupportedFeatureError)


async def test_ollama_malformed_arguments_are_a_call_error_not_an_exception(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: httpx.Response(
        200,
        json={
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "read_file", "arguments": "{bad"}}],
            },
        },
    )

    response = await OllamaProvider().complete(ModelRequest(messages=[USER], tools=TOOLS))

    (call,) = response.message.tool_calls or []
    assert call.error and call.arguments == {} and call.raw_arguments == "{bad"


async def test_ollama_message_that_is_not_an_object_is_a_provider_error(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: httpx.Response(200, json={"model": "m", "message": "hi"})

    with pytest.raises(ModelProviderError, match="response parsing failed"):
        await OllamaProvider().complete(ModelRequest(messages=[USER]))


# --- OpenRouter: failures reported inside an HTTP 200 -------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, AuthenticationError),
        (402, InsufficientCreditsError),
        (429, RetryableProviderError),
        (502, RetryableProviderError),
        (400, ModelProviderError),
    ],
)
@pytest.mark.parametrize("where", ["top_level", "choice"])
async def test_openrouter_error_body_is_classified_like_the_http_status(
    transport: dict[str, object], where: str, code: int, expected: type[Exception]
) -> None:
    error = {"code": code, "message": "upstream said no"}
    if where == "top_level":
        body: dict[str, object] = {"error": error}
    else:
        body = {"choices": [{"error": error, "message": {"role": "assistant", "content": ""}}]}
    transport["handler"] = lambda request: httpx.Response(200, json=body)

    with pytest.raises(expected) as excinfo:
        await _openrouter().complete(ModelRequest(messages=[USER], tools=TOOLS))

    assert "upstream said no" in str(excinfo.value)
    if expected is ModelProviderError:  # nothing more specific: exactly the base class
        assert type(excinfo.value) is ModelProviderError
    if expected is AuthenticationError:
        assert not isinstance(excinfo.value, MissingCredentialsError)


async def test_openrouter_error_body_without_a_code_is_a_provider_error(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: httpx.Response(200, json={"error": "plain text failure"})

    with pytest.raises(ModelProviderError, match="plain text failure"):
        await _openrouter().complete(ModelRequest(messages=[USER]))


async def test_openrouter_error_body_never_leaks_the_api_key(transport: dict[str, object]) -> None:
    body = {"error": {"code": 400, "message": f"bad key {API_KEY}"}}
    transport["handler"] = lambda request: httpx.Response(200, json=body)

    with pytest.raises(ModelProviderError) as excinfo:
        await _openrouter().complete(ModelRequest(messages=[USER]))

    assert API_KEY not in str(excinfo.value)


# --- Router: 402 with tools set ----------------------------------------------------------------


async def test_http_402_falls_back_to_ollama_with_the_tools_kept(
    transport: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(402, json={"error": {"message": "more credits", "code": 402}})
        return _ollama_tool_reply()

    transport["handler"] = handler

    response = await _router().complete(ModelRequest(messages=[USER], tools=TOOLS))

    assert response.provider_name == "ollama"
    (call,) = response.message.tool_calls or []
    assert call.name == "read_file" and call.arguments == {"path": "a.txt"}
    assert [r.url.host for r in transport["requests"]] == ["openrouter.ai", "localhost"]  # type: ignore[attr-defined]
    assert _sent(transport)["tools"] == TOOLS  # the fallback was asked for tools, not text


async def test_200_with_a_402_error_body_falls_back_with_the_tools_kept(
    transport: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(200, json={"error": {"message": "more credits", "code": 402}})
        return _ollama_tool_reply()

    transport["handler"] = handler

    response = await _router().complete(ModelRequest(messages=[USER], tools=TOOLS))

    assert response.provider_name == "ollama"
    assert response.message.tool_calls
    assert _sent(transport)["tools"] == TOOLS


async def test_402_whose_fallback_cannot_do_tools_raises_instead_of_answering_in_text() -> None:
    router = _router()
    paid = ToolCapableFake(
        "openrouter", raise_on_complete=InsufficientCreditsError("HTTP 402: out of credits")
    )
    text_only = MagicMock(wraps=FakeProvider("ollama"))
    text_only.name = "ollama"
    text_only.supports_tools = False
    router._providers = {"openrouter": paid, "ollama": text_only}

    with pytest.raises(UnsupportedFeatureError) as excinfo:
        await router.complete(ModelRequest(messages=[USER], tools=TOOLS))

    assert not isinstance(excinfo.value, InsufficientCreditsError)  # not the primary's type
    assert "HTTP 402" in str(excinfo.value)  # the root cause stays visible
    assert "does not support tool calling" in str(excinfo.value)
    text_only.complete.assert_not_called()


async def test_402_with_a_fallback_model_that_rejects_tools_reports_both_reasons(
    transport: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(402, json={"error": {"message": "more credits", "code": 402}})
        return httpx.Response(400, json={"error": "gemma2:latest does not support tools"})

    transport["handler"] = handler

    with pytest.raises(UnsupportedFeatureError) as excinfo:
        await _router().complete(ModelRequest(messages=[USER], tools=TOOLS))

    assert not isinstance(excinfo.value, InsufficientCreditsError)
    message = str(excinfo.value)
    assert "HTTP 402" in message and "does not support tools" in message


async def test_missing_key_with_a_fallback_that_rejects_tools_reports_both_reasons(
    transport: dict[str, object],
) -> None:
    transport["handler"] = lambda request: httpx.Response(
        400, json={"error": "gemma2:latest does not support tools"}
    )

    with pytest.raises(UnsupportedFeatureError) as excinfo:
        await _router(key=None).complete(ModelRequest(messages=[USER], tools=TOOLS))

    assert not isinstance(excinfo.value, MissingCredentialsError)
    message = str(excinfo.value)
    assert "API key is missing" in message and "does not support tools" in message


async def test_transient_failure_whose_fallback_cannot_do_tools_reports_both_reasons() -> None:
    router = _router()
    flaky = ToolCapableFake("openrouter", raise_on_complete=RetryableProviderError("502 upstream"))
    text_only = FakeProvider("ollama")
    router._providers = {"openrouter": flaky, "ollama": text_only}

    with pytest.raises(UnsupportedFeatureError) as excinfo:
        await router.complete(ModelRequest(messages=[USER], tools=TOOLS))

    message = str(excinfo.value)
    assert "502 upstream" in message and "does not support tool calling" in message


async def test_transient_failure_whose_fallback_model_rejects_tools_reports_both_reasons(
    transport: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(503, text="overloaded")
        return httpx.Response(400, json={"error": "gemma2:latest does not support tools"})

    transport["handler"] = handler

    with pytest.raises(UnsupportedFeatureError) as excinfo:
        await _router().complete(ModelRequest(messages=[USER], tools=TOOLS))

    message = str(excinfo.value)
    assert "overloaded" in message and "does not support tools" in message


async def test_a_text_only_request_still_falls_back_to_a_text_only_provider() -> None:
    router = _router()
    router._providers = {
        "openrouter": FakeProvider(
            "openrouter", raise_on_complete=InsufficientCreditsError("HTTP 402")
        ),
        "ollama": FakeProvider("ollama"),
    }

    response = await router.complete(ModelRequest(messages=[USER]))

    assert response.provider_name == "ollama"

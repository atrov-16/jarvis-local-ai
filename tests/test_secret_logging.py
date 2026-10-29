"""F-26: credentials never reach logs, exception messages or provider error text.

Each test uses unmistakable dummy secrets and inspects captured log output. Where the claim is
"the code never logs it", the capture handler has no redaction filter, so the redaction filter
cannot mask a leak at the source.
"""

from __future__ import annotations

import io
import logging
import logging.config
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig, ModelsConfig
from jarvis.config.secrets import SecretManager, SecretStorageError, tokens_match
from jarvis.logging.redaction import (
    REDACTION,
    SecretRedactingFilter,
    get_log_redaction_filter,
    redact_sensitive,
)
from jarvis.logging.setup import setup_logging, uvicorn_log_config
from jarvis.models.openrouter import OpenRouterProvider
from jarvis.models.providers import (
    AuthenticationError,
    InsufficientCreditsError,
    ModelProviderError,
    RetryableProviderError,
)
from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelRequest

DUMMY_API_TOKEN = "DUMMY-jarvis-api-token-do-not-log-7f3a9c1e5b2d"
DUMMY_GENERATED_TOKEN = "DUMMY-generated-token-do-not-log-4e8b1a6c9d0f"
DUMMY_OPENROUTER_KEY = "sk-or-v1-DUMMY-openrouter-key-do-not-log-0123456789"


class DictBackend:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self._fail_with = fail_with

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.passwords.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.passwords[(service_name, username)] = password


@pytest.fixture
def raw_log() -> Iterator[io.StringIO]:
    """Everything logged anywhere, at DEBUG, captured without any redaction filter."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _assert_no_secrets(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text


def _app(tmp_path: Path, secret_manager: SecretManager):
    config = JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "jarvis.sqlite"))
    return create_app(config=config, secret_manager=secret_manager)


# --- Generated API token ---------------------------------------------------------------------


def test_generated_api_token_is_stored_but_never_logged(tmp_path, raw_log) -> None:
    backend = DictBackend()
    app = _app(tmp_path, SecretManager({}, backend=backend))

    with patch("jarvis.api.http.token_urlsafe", return_value=DUMMY_GENERATED_TOKEN):
        with TestClient(app) as client:
            # The stored token is the one clients must present.
            assert backend.passwords == {("Jarvis", "API Token"): DUMMY_GENERATED_TOKEN}
            headers = {"Authorization": f"Bearer {DUMMY_GENERATED_TOKEN}"}
            assert client.get("/v1/projects", headers=headers).status_code == 200
            assert client.get("/v1/projects").status_code == 401

    output = raw_log.getvalue()
    _assert_no_secrets(output, DUMMY_GENERATED_TOKEN)
    assert "Generated one and stored it in the OS credential store" in output
    assert "The token value is not logged" in output


def test_token_storage_failure_stops_startup_without_revealing_token(tmp_path, raw_log) -> None:
    # A backend whose error message echoes the value it was asked to store.
    backend = DictBackend(fail_with=RuntimeError(f"locked; refused {DUMMY_GENERATED_TOKEN}"))
    app = _app(tmp_path, SecretManager({}, backend=backend))

    with patch("jarvis.api.http.token_urlsafe", return_value=DUMMY_GENERATED_TOKEN):
        with pytest.raises(SecretStorageError) as excinfo:
            with TestClient(app):
                pass

    message = str(excinfo.value)
    assert "JARVIS_API_TOKEN" in message  # tells the user how to recover
    assert "locked" in message  # keeps the backend's diagnostic
    _assert_no_secrets(message, DUMMY_GENERATED_TOKEN)
    _assert_no_secrets(raw_log.getvalue(), DUMMY_GENERATED_TOKEN)
    assert backend.passwords == {}


def test_configured_token_is_not_logged_during_requests(tmp_path, raw_log) -> None:
    secret_manager = SecretManager(
        {"JARVIS_API_TOKEN": DUMMY_API_TOKEN, "OPENROUTER_API_KEY": DUMMY_OPENROUTER_KEY},
        use_keyring=False,
    )
    app = _app(tmp_path, secret_manager)
    with TestClient(app) as client:
        wrong = {"Authorization": "Bearer wrong"}
        assert client.get("/v1/projects", headers=wrong).status_code == 401
        headers = {"Authorization": f"Bearer {DUMMY_API_TOKEN}"}
        assert client.get("/v1/projects", headers=headers).status_code == 200
        assert client.get("/v1/config/public", headers=headers).status_code == 200
        # Query-string tokens are not accepted at all (the route closes at once).
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/v1/events?token={DUMMY_API_TOKEN}-wrong"):
                pass

    _assert_no_secrets(raw_log.getvalue(), DUMMY_API_TOKEN, DUMMY_OPENROUTER_KEY)


def test_token_comparison() -> None:
    assert tokens_match(DUMMY_API_TOKEN, DUMMY_API_TOKEN)
    assert not tokens_match(DUMMY_API_TOKEN, DUMMY_API_TOKEN + "x")
    assert not tokens_match(None, DUMMY_API_TOKEN)
    assert not tokens_match("", "")
    assert not tokens_match(DUMMY_API_TOKEN, None)
    assert not tokens_match("tökén", "token")  # non-ASCII input is compared, not a TypeError


# --- Provider requests -----------------------------------------------------------------------


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch):
    """Route every httpx.AsyncClient through a MockTransport; returns the request log."""
    state: dict[str, object] = {"handler": None, "requests": []}

    def dispatch(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)  # type: ignore[attr-defined]
        return state["handler"](request)  # type: ignore[operator]

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(dispatch)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs),
    )
    return state


def _openrouter() -> OpenRouterProvider:
    return OpenRouterProvider(
        SecretManager({"OPENROUTER_API_KEY": DUMMY_OPENROUTER_KEY}, use_keyring=False)
    )


def _request() -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content="Hi")], max_tokens=64)


async def test_successful_provider_request_sends_key_but_never_logs_it(mock_http, raw_log) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {DUMMY_OPENROUTER_KEY}"
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            },
        )

    mock_http["handler"] = handler
    response = await _openrouter().complete(_request())

    assert response.message.content == "ok"
    assert len(mock_http["requests"]) == 1  # type: ignore[arg-type]
    output = raw_log.getvalue()
    assert "HTTP Request: POST https://openrouter.ai/api/v1/chat/completions" in output
    _assert_no_secrets(output, DUMMY_OPENROUTER_KEY)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (402, InsufficientCreditsError),
        (400, ModelProviderError),
        (429, RetryableProviderError),
        (500, RetryableProviderError),
    ],
)
async def test_failing_provider_request_does_not_expose_key(
    mock_http, raw_log, status, error_type
) -> None:
    # Error bodies that echo the credential back, as some gateways do.
    body = f'{{"error": {{"message": "rejected key {DUMMY_OPENROUTER_KEY}", "code": {status}}}}}'
    mock_http["handler"] = lambda request: httpx.Response(status, text=body)

    with pytest.raises(error_type) as excinfo:
        await _openrouter().complete(_request())

    assert f'"code": {status}' in str(excinfo.value)  # the diagnostic survives
    _assert_no_secrets(str(excinfo.value), DUMMY_OPENROUTER_KEY)
    _assert_no_secrets(raw_log.getvalue(), DUMMY_OPENROUTER_KEY)


async def test_connection_error_does_not_expose_key(mock_http) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy refused Authorization: Bearer {DUMMY_OPENROUTER_KEY}")

    mock_http["handler"] = handler
    with pytest.raises(RetryableProviderError) as excinfo:
        await _openrouter().complete(_request())
    assert "proxy refused" in str(excinfo.value)
    _assert_no_secrets(str(excinfo.value), DUMMY_OPENROUTER_KEY)


async def test_availability_error_does_not_expose_key(mock_http) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {DUMMY_OPENROUTER_KEY}")

    mock_http["handler"] = handler
    status = await _openrouter().check_availability()
    assert status.available is False
    assert status.error is not None and "refused" in status.error
    _assert_no_secrets(status.error, DUMMY_OPENROUTER_KEY)


async def test_router_fallback_logging_does_not_expose_key(mock_http, raw_log) -> None:
    """402 from OpenRouter echoing the key, then a local fallback that is not running."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(402, text=f"insufficient credits for {DUMMY_OPENROUTER_KEY}")
        raise httpx.ConnectError("connection refused")

    mock_http["handler"] = handler
    config = JarvisConfig(
        models=ModelsConfig(primary_provider="openrouter", fallback_provider="ollama")
    )
    router = ModelRouter(
        config, SecretManager({"OPENROUTER_API_KEY": DUMMY_OPENROUTER_KEY}, use_keyring=False)
    )

    with pytest.raises(InsufficientCreditsError) as excinfo:
        await router.complete(_request())

    output = raw_log.getvalue()
    assert "falling back to 'ollama'" in output
    _assert_no_secrets(output + str(excinfo.value), DUMMY_OPENROUTER_KEY)


# --- Log redaction -----------------------------------------------------------------------------


def _record(msg: object, args: object = None, exc: bool = False) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise RuntimeError(f"failed with Bearer {DUMMY_API_TOKEN}")
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)  # type: ignore[arg-type]


def _format(record: logging.LogRecord, log_filter: logging.Filter) -> str:
    assert log_filter.filter(record)
    return logging.Formatter("%(message)s").format(record)


def test_redaction_filter_masks_known_secrets_in_messages_args_and_tracebacks() -> None:
    log_filter = SecretRedactingFilter([DUMMY_API_TOKEN, DUMMY_OPENROUTER_KEY])

    class Holder:
        def __repr__(self) -> str:
            return f"Holder({DUMMY_OPENROUTER_KEY})"

    cases = [
        _record(f"f-string with {DUMMY_API_TOKEN}"),
        _record("percent args %s %r", (DUMMY_API_TOKEN, Holder())),
        _record("mapping args %(token)s", ({"token": DUMMY_API_TOKEN},)),
        _record("traceback follows", exc=True),
    ]
    for record in cases:
        output = _format(record, log_filter)
        _assert_no_secrets(output, DUMMY_API_TOKEN, DUMMY_OPENROUTER_KEY)
        assert REDACTION in output


def test_redaction_filter_keeps_ordinary_records_intact() -> None:
    log_filter = SecretRedactingFilter([DUMMY_API_TOKEN])
    record = _record("step %d of %s took %.1fs: %r", (3, "plan", 1.5, {"ok": True}))
    assert _format(record, log_filter) == "step 3 of plan took 1.5s: {'ok': True}"
    assert _format(_record("Basic usage of the Bearer scheme"), log_filter) == (
        "Basic usage of the Bearer scheme"
    )


@pytest.mark.parametrize(
    "text",
    [
        f"GET /v1/events?token={DUMMY_API_TOKEN}&x=1",
        f"Authorization: Bearer {DUMMY_API_TOKEN}",
        f"{{'x-jarvis-api-token': '{DUMMY_API_TOKEN}'}}",
        f"api_key={DUMMY_API_TOKEN}",
        f"key {DUMMY_OPENROUTER_KEY} rejected",
    ],
)
def test_credential_shapes_are_redacted_without_knowing_the_value(text: str) -> None:
    redacted = redact_sensitive(text)
    _assert_no_secrets(redacted, DUMMY_API_TOKEN, DUMMY_OPENROUTER_KEY)
    assert REDACTION in redacted


def test_setup_logging_installs_redaction_on_root_handlers() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        setup_logging("INFO")
        assert root.handlers
        assert all(get_log_redaction_filter() in h.filters for h in root.handlers)
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_uvicorn_request_logs_redact_query_string_tokens() -> None:
    """uvicorn logs WebSocket and HTTP paths including the query string.

    The handlers are built from uvicorn_log_config() locally: a global dictConfig would close
    every live handler in the test process.
    """
    import importlib

    from uvicorn.logging import AccessFormatter, DefaultFormatter

    config = uvicorn_log_config()
    factory_path = config["filters"]["redact_secrets"]["()"]
    module, _, name = factory_path.rpartition(".")
    log_filter = getattr(importlib.import_module(module), name)()
    assert log_filter is get_log_redaction_filter()
    assert all("redact_secrets" in h["filters"] for h in config["handlers"].values())

    stream = io.StringIO()
    formatters = {"default": DefaultFormatter, "access": AccessFormatter}
    handlers = {}
    for key, spec in config["handlers"].items():
        handler = logging.StreamHandler(stream)
        fmt = config["formatters"][spec["formatter"]]
        handler.setFormatter(formatters[spec["formatter"]](fmt=fmt["fmt"], use_colors=False))
        handler.addFilter(log_filter)
        handlers[key] = handler

    path = f"/v1/events?token={DUMMY_API_TOKEN}"
    records = [
        ("default", "uvicorn.error", '%s - "WebSocket %s" [accepted]', ("127.0.0.1:1", path)),
        ("access", "uvicorn.access", '%s - "%s %s HTTP/%s" %d',
         ("127.0.0.1:1", "GET", path, "1.1", 200)),
    ]
    for key, logger_name, msg, args in records:
        record = logging.LogRecord(logger_name, logging.INFO, __file__, 1, msg, args, None)
        handlers[key].handle(record)

    output = stream.getvalue()
    assert output.count("/v1/events?token=[REDACTED]") == 2
    assert '"GET /v1/events?token=[REDACTED] HTTP/1.1" 200' in output
    _assert_no_secrets(output, DUMMY_API_TOKEN)


def test_redaction_filter_handles_non_string_messages() -> None:
    log_filter = SecretRedactingFilter([DUMMY_API_TOKEN])
    messages = (
        RuntimeError(f"bad token {DUMMY_API_TOKEN}"),
        {"Authorization": f"Bearer {DUMMY_API_TOKEN}"},
    )
    for msg in messages:
        output = _format(_record(msg), log_filter)
        _assert_no_secrets(output, DUMMY_API_TOKEN)
        assert REDACTION in output


def test_redaction_keeps_placeholders_and_redacts_the_rendered_message() -> None:
    log_filter = SecretRedactingFilter()
    record = _record("refreshing token=%s for %s", ("unknown-secret-value", "user"))
    assert _format(record, log_filter) == f"refreshing token={REDACTION} for user"


def test_redaction_filter_never_raises_into_the_caller() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("boom")

        __repr__ = __str__

    log_filter = SecretRedactingFilter()
    output = _format(_record("state %s", (Unprintable(),)), log_filter)
    assert output == "[log record withheld: it could not be redacted]"


def test_credential_store_that_stores_nothing_stops_startup(tmp_path) -> None:
    """keyring's null backend accepts writes silently; the token must not be assumed saved."""
    from keyring.backends.null import Keyring as NullKeyring

    app = _app(tmp_path, SecretManager({}, backend=NullKeyring()))
    with patch("jarvis.api.http.token_urlsafe", return_value=DUMMY_GENERATED_TOKEN):
        with pytest.raises(SecretStorageError, match="could not be read back") as excinfo:
            with TestClient(app):
                pass
    assert "JARVIS_API_TOKEN" in str(excinfo.value)
    _assert_no_secrets(str(excinfo.value), DUMMY_GENERATED_TOKEN)

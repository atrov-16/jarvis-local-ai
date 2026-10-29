"""Isolation from the developer's real credentials and data, for every test.

- The OS credential store (macOS login Keychain, Windows Credential Manager) is replaced by an
  in-memory backend before any test module runs, so nothing reads, writes or deletes real
  credentials, even through `SecretManager()` with its defaults.
- python-dotenv is disabled for the session (jarvis.app.terminal calls load_dotenv() at
  import), so a developer's real .env is never loaded, in this process or in child processes.
- Secret and configuration environment variables are removed for each test and restored
  exactly afterwards.
- The per-user data directory and the repository-root legacy database location resolve to
  temporary directories, so a default-configured app never opens the real database.
- httpx.AsyncClient has no network by default: model providers (e.g. a local Ollama the app
  falls back to) are never contacted for real. Tests that need responses patch the client or
  pass a mock transport.
- TestClient connects from 127.0.0.1 by default, a loopback peer for localhost_only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

# Before any jarvis.app module is imported (they load .env at import time).
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from jarvis.config import paths  # noqa: E402

ISOLATED_ENV_VARS = (
    "JARVIS_API_TOKEN",
    "OPENROUTER_API_KEY",
    "JARVIS_CONFIG_PATH",
    "JARVIS_DATABASE_PATH",
    "JARVIS_SERVER_HOST",
    "JARVIS_SERVER_PORT",
)


class _NoNetworkTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"network access is disabled in tests: {request.url}", request=request
        )


class OfflineAsyncClient(httpx.AsyncClient):
    """httpx.AsyncClient whose default transport refuses every connection."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("transport", _NoNetworkTransport())
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]


class IsolatedTestKeyring(KeyringBackend):
    """In-memory credential store; its priority keeps keyring from ever selecting it itself."""

    priority = 0

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.passwords[(service, username)]
        except KeyError:
            raise PasswordDeleteError(username) from None


TEST_KEYRING = IsolatedTestKeyring()
# Installed at import, before test modules are collected: the real backend is never initialised.
keyring.set_keyring(TEST_KEYRING)


@pytest.fixture(autouse=True)
def isolated_host_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[IsolatedTestKeyring]:
    # Re-installed per test, so a test that swaps the backend cannot leak it into the next.
    keyring.set_keyring(TEST_KEYRING)
    TEST_KEYRING.passwords.clear()

    for name in ISOLATED_ENV_VARS:
        # setenv first records the original value, so even a value that the test assigns to
        # os.environ directly is removed again on teardown.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)

    data_dir = tmp_path_factory.mktemp("user-data")
    monkeypatch.setattr(paths, "user_data_dir", lambda *args, **kwargs: str(data_dir / "Jarvis"))
    source_root = tmp_path_factory.mktemp("source-root")
    monkeypatch.setattr(paths, "source_root", lambda: Path(source_root))

    monkeypatch.setattr(httpx, "AsyncClient", OfflineAsyncClient)

    from starlette import testclient

    original_init = testclient.TestClient.__init__

    def loopback_init(self, *args, client=("127.0.0.1", 50000), **kwargs):  # type: ignore[no-untyped-def]
        original_init(self, *args, client=client, **kwargs)

    monkeypatch.setattr(testclient.TestClient, "__init__", loopback_init)

    yield TEST_KEYRING
    TEST_KEYRING.passwords.clear()

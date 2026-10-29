"""F-25: tests never use the real OS credential store or the developer's secrets.

The isolation itself lives in conftest.py; these tests prove it holds, including for code that
constructs `SecretManager()` with its defaults (environment + keyring).
"""

from __future__ import annotations

import os
from pathlib import Path

import keyring
import pytest
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.secrets import SecretManager, SecretStorageError

DUMMY_TOKEN = "DUMMY-isolation-token-3c9e"


class FailingBackend:
    def get_password(self, service_name: str, username: str) -> str | None:
        raise RuntimeError("store unavailable")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise RuntimeError(f"store unavailable while saving {password}")


def test_keyring_is_the_isolated_in_memory_store(isolated_host_state) -> None:
    assert keyring.get_keyring() is isolated_host_state
    assert type(isolated_host_state).__module__ == "conftest"


def test_default_secret_manager_reaches_only_the_isolated_store(isolated_host_state) -> None:
    manager = SecretManager()  # real defaults: os.environ, then keyring

    assert manager.get_api_token() is None
    manager.set_api_token(DUMMY_TOKEN)

    assert isolated_host_state.passwords == {("Jarvis", "API Token"): DUMMY_TOKEN}
    assert manager.get_api_token() == DUMMY_TOKEN


def test_default_app_startup_writes_its_token_only_to_the_isolated_store(
    isolated_host_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original F-25 scenario: create_app() with no injected secrets and no token."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "isolated.sqlite"
    monkeypatch.setenv("JARVIS_DATABASE_PATH", str(db_path))

    app = create_app()
    with TestClient(app):
        pass

    stored = isolated_host_state.passwords
    assert list(stored) == [("Jarvis", "API Token")]
    assert stored[("Jarvis", "API Token")]
    assert app.state.storage_status["database_path"] == str(db_path)
    assert not (tmp_path / ".jarvis").exists()


def test_store_is_emptied_between_tests(isolated_host_state) -> None:
    # The previous tests stored tokens; none survive into this one.
    assert isolated_host_state.passwords == {}


def test_secret_set_directly_in_os_environ_part_1() -> None:
    # Deliberately bypasses monkeypatch, like the test that used to leak JARVIS_API_TOKEN.
    os.environ["JARVIS_API_TOKEN"] = DUMMY_TOKEN
    os.environ["OPENROUTER_API_KEY"] = DUMMY_TOKEN


def test_secret_set_directly_in_os_environ_part_2() -> None:
    # Runs after part 1 (definition order); the fixture removed what part 1 left behind.
    assert "JARVIS_API_TOKEN" not in os.environ
    assert "OPENROUTER_API_KEY" not in os.environ


def test_injected_backend_is_used_instead_of_keyring(isolated_host_state) -> None:
    class Backend:
        def __init__(self) -> None:
            self.passwords: dict[tuple[str, str], str] = {}

        def get_password(self, service_name: str, username: str) -> str | None:
            return self.passwords.get((service_name, username))

        def set_password(self, service_name: str, username: str, password: str) -> None:
            self.passwords[(service_name, username)] = password

    backend = Backend()
    manager = SecretManager({}, backend=backend)
    manager.set_api_token(DUMMY_TOKEN)

    assert backend.passwords == {("Jarvis", "API Token"): DUMMY_TOKEN}
    assert isolated_host_state.passwords == {}


def test_storage_failures_are_reported_without_the_value() -> None:
    manager = SecretManager({}, backend=FailingBackend())

    assert manager.get_api_token() is None  # a read failure means "not configured"
    with pytest.raises(SecretStorageError, match="store unavailable") as excinfo:
        manager.set_api_token(DUMMY_TOKEN)
    assert DUMMY_TOKEN not in str(excinfo.value)

    with pytest.raises(SecretStorageError, match="disabled"):
        SecretManager({}, use_keyring=False).set_api_token(DUMMY_TOKEN)


def test_known_secret_values() -> None:
    manager = SecretManager(
        {"JARVIS_API_TOKEN": "token-value", "OPENROUTER_API_KEY": "key-value"}, use_keyring=False
    )
    assert manager.known_secret_values() == ["token-value", "key-value"]
    assert SecretManager({}, use_keyring=False).known_secret_values() == []

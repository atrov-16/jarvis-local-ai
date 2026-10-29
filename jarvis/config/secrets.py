"""Secret retrieval for Jarvis."""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Protocol

import keyring

JARVIS_SERVICE = "Jarvis"
API_TOKEN_ENV = "JARVIS_API_TOKEN"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
API_TOKEN_ACCOUNT = "API Token"
OPENROUTER_ACCOUNT = "OpenRouter"


class CredentialBackend(Protocol):
    """The part of the `keyring` API that SecretManager uses."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...


class SecretStorageError(RuntimeError):
    """Raised when a secret cannot be stored in the OS credential store."""


def tokens_match(supplied: str | None, expected: str | None) -> bool:
    """Constant-time token comparison; never matches a missing token."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


class SecretManager:
    """Reads secrets from environment variables first, then the OS credential store.

    The credential store defaults to `keyring` (Windows Credential Manager, macOS Keychain);
    pass `backend` to use another implementation, or `use_keyring=False` to disable it.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        use_keyring: bool = True,
        backend: CredentialBackend | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._use_keyring = use_keyring
        self._backend: CredentialBackend = backend if backend is not None else keyring

    def get_api_token(self) -> str | None:
        return self._get_secret(API_TOKEN_ENV, API_TOKEN_ACCOUNT)

    def get_openrouter_api_key(self) -> str | None:
        return self._get_secret(OPENROUTER_API_KEY_ENV, OPENROUTER_ACCOUNT)

    def set_api_token(self, token: str) -> None:
        """Store the API token in the credential store; raises SecretStorageError on failure."""
        self._set_secret(API_TOKEN_ACCOUNT, token)

    def known_secret_values(self) -> list[str]:
        """The configured secret values, for redacting them from output."""
        values = (self.get_api_token(), self.get_openrouter_api_key())
        return [value for value in values if value]

    def status(self) -> dict[str, bool]:
        return {
            "api_token_configured": self.get_api_token() is not None,
            "openrouter_api_key_configured": self.get_openrouter_api_key() is not None,
        }

    def _get_secret(self, env_name: str, account: str) -> str | None:
        value = self._env.get(env_name)
        if value:
            return value
        if not self._use_keyring:
            return None
        try:
            return self._backend.get_password(JARVIS_SERVICE, account)
        except Exception:
            return None

    def _set_secret(self, account: str, value: str) -> None:
        if not self._use_keyring:
            raise SecretStorageError("The OS credential store is disabled.")
        try:
            self._backend.set_password(JARVIS_SERVICE, account, value)
        except Exception as e:
            # The backend's message describes the store; strip the value in case it echoes it.
            detail = f"{type(e).__name__}: {e}".replace(value, "[REDACTED]")
            raise SecretStorageError(
                f"Could not store {account!r} in the OS credential store: {detail}"
            ) from e

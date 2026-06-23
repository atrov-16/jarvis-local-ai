"""Secret retrieval for Jarvis."""

from __future__ import annotations

import os
from collections.abc import Mapping

import keyring

JARVIS_SERVICE = "Jarvis"
API_TOKEN_ENV = "JARVIS_API_TOKEN"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"


class SecretManager:
    """Reads secrets from environment variables first, then keyring."""

    def __init__(self, env: Mapping[str, str] | None = None, use_keyring: bool = True) -> None:
        self._env = env if env is not None else os.environ
        self._use_keyring = use_keyring

    def get_api_token(self) -> str | None:
        return self._get_secret(API_TOKEN_ENV, "API Token")

    def get_openrouter_api_key(self) -> str | None:
        return self._get_secret(OPENROUTER_API_KEY_ENV, "OpenRouter")

    def set_api_token(self, token: str) -> None:
        self._set_secret(API_TOKEN_ENV, "API Token", token)

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
            return keyring.get_password(JARVIS_SERVICE, account)
        except Exception:
            return None

    def _set_secret(self, env_name: str, account: str, value: str) -> None:
        if self._use_keyring:
            try:
                keyring.set_password(JARVIS_SERVICE, account, value)
            except Exception:
                pass

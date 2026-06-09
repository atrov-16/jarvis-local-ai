from __future__ import annotations

from jarvis.config.secrets import SecretManager
from jarvis.logging.redaction import REDACTION, redact_text


def test_reads_secrets_from_environment_mapping() -> None:
    manager = SecretManager(
        {
            "JARVIS_API_TOKEN": "local-token",
            "OPENROUTER_API_KEY": "openrouter-key",
        }
    )

    assert manager.get_api_token() == "local-token"
    assert manager.get_openrouter_api_key() == "openrouter-key"
    assert manager.status() == {
        "api_token_configured": True,
        "openrouter_api_key_configured": True,
    }


def test_missing_secrets_return_status_without_crashing() -> None:
    manager = SecretManager({}, use_keyring=False)

    assert manager.get_api_token() is None
    assert manager.get_openrouter_api_key() is None
    assert manager.status() == {
        "api_token_configured": False,
        "openrouter_api_key_configured": False,
    }


def test_redaction_masks_known_secret_values() -> None:
    result = redact_text(
        "token local-token and key openrouter-key",
        ["local-token", "openrouter-key"],
    )

    assert "local-token" not in result
    assert "openrouter-key" not in result
    assert result.count(REDACTION) == 2

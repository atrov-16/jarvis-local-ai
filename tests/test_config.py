from __future__ import annotations

from pathlib import Path

from jarvis.config.manager import load_config


def test_loads_defaults_without_config(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIG_PATH", raising=False)
    config = load_config()

    assert config.server.host == "127.0.0.1"
    assert config.security.api_token_enabled is True
    assert config.memory.database_path == Path(".jarvis/dev/memory.sqlite")


def test_loads_explicit_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        [server]
        port = 9999

        [memory]
        database_path = "custom.sqlite"
        """,
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.server.port == 9999
    assert config.memory.database_path == Path("custom.sqlite")


def test_public_config_excludes_secrets() -> None:
    public = load_config().public_dict()

    assert "JARVIS_API_TOKEN" not in str(public)
    assert "OPENROUTER_API_KEY" not in str(public)
    assert public["security"]["api_token_enabled"] is True


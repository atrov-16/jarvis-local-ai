"""Configuration loading."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from jarvis.config.models import JarvisConfig
from jarvis.config.paths import expand_path

CONFIG_PATH_ENV = "JARVIS_CONFIG_PATH"


def load_config(path: str | Path | None = None) -> JarvisConfig:
    """Load Jarvis config from an explicit path, env path, or defaults."""
    config_path = _resolve_config_path(path)
    data: dict[str, Any] = {}
    if config_path is not None:
        if not config_path.is_file():
            # An explicitly named file that is missing is a mistake, not a request for defaults.
            raise FileNotFoundError(f"Config file not found: {config_path}")
        data = _read_toml(config_path)

    config = JarvisConfig.model_validate(data)
    return _apply_env_overrides(config)


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file written by any common editor.

    TOML must be UTF-8, but Windows editors often prepend a byte-order mark that tomllib
    rejects with an unhelpful "Invalid statement (at line 1, column 1)"; `utf-8-sig`
    accepts files with or without one. CRLF line endings are fine either way.
    """
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"Config file {path} is not UTF-8 text ({error.reason} at byte {error.start}). "
            "Re-save it as UTF-8 (Windows PowerShell 5.1's '>' redirection writes UTF-16)."
        ) from error
    return tomllib.loads(text)


def _resolve_config_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return expand_path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV)
    if env_path:
        return expand_path(env_path)
    return None


def _apply_env_overrides(config: JarvisConfig) -> JarvisConfig:
    updates: dict[str, Any] = {}

    if server_host := os.environ.get("JARVIS_SERVER_HOST"):
        updates.setdefault("server", {})["host"] = server_host
    if server_port := os.environ.get("JARVIS_SERVER_PORT"):
        updates.setdefault("server", {})["port"] = int(server_port)
    if database_path := os.environ.get("JARVIS_DATABASE_PATH"):
        updates.setdefault("memory", {})["database_path"] = database_path

    if not updates:
        return config

    sections: dict[str, Any] = {}
    for section, values in updates.items():
        current = getattr(config, section)
        # Re-validate only what was set explicitly, so `model_fields_set` still tells
        # configured values from defaults (see the database location check at startup).
        data = current.model_dump(include=current.model_fields_set)
        data.update(values)
        sections[section] = type(current).model_validate(data)
    return config.model_copy(update=sections)


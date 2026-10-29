"""Configuration loading."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from jarvis.config.models import JarvisConfig

CONFIG_PATH_ENV = "JARVIS_CONFIG_PATH"


def load_config(path: str | Path | None = None) -> JarvisConfig:
    """Load Jarvis config from an explicit path, env path, or defaults."""
    config_path = _resolve_config_path(path)
    data: dict[str, Any] = {}
    if config_path is not None and config_path.exists():
        with config_path.open("rb") as file:
            data = tomllib.load(file)

    config = JarvisConfig.model_validate(data)
    return _apply_env_overrides(config)


def _resolve_config_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path)
    env_path = os.environ.get(CONFIG_PATH_ENV)
    if env_path:
        return Path(env_path)
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


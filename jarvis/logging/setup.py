"""Logging setup."""

from __future__ import annotations

import copy
import logging
from typing import Any

from jarvis.logging.redaction import install_log_redaction


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    install_log_redaction()


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging config with secret redaction on its handlers.

    uvicorn logs request paths including the query string, so a WebSocket token passed as
    `?token=...` would otherwise be written to the log.
    """
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_secrets"] = {
        "()": "jarvis.logging.redaction.get_log_redaction_filter"
    }
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("redact_secrets")
    return config

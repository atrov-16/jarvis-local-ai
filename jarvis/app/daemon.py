"""Daemon entrypoint for the local Jarvis API."""

from __future__ import annotations

from pathlib import Path

import uvicorn

from jarvis.api.http import create_app
from jarvis.config.manager import load_config
from jarvis.logging.setup import setup_logging


def run_daemon(config_path: Path | None = None) -> None:
    """Start the local FastAPI daemon."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
        
    config = load_config(config_path)
    setup_logging(config.logging.level)
    app = create_app(config=config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)


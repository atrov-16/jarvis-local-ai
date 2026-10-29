"""Daemon entrypoint for the local Jarvis API."""

from __future__ import annotations

from pathlib import Path

import uvicorn

from jarvis.api.http import create_app
from jarvis.api.network import UnsafeBindError, check_bind_host
from jarvis.config.manager import load_config
from jarvis.logging.setup import setup_logging, uvicorn_log_config


def run_daemon(config_path: Path | None = None) -> None:
    """Start the local FastAPI daemon."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
        
    config = load_config(config_path)
    try:
        check_bind_host(
            config.server.host,
            config.security.localhost_only,
            api_token_enabled=config.security.api_token_enabled,
        )
    except UnsafeBindError as e:
        raise SystemExit(f"Refusing to start: {e}") from None
    setup_logging(config.logging.level)
    app = create_app(config=config)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_config=uvicorn_log_config(),
        # The client address is the TCP peer: X-Forwarded-For must not be able to make a
        # remote client look local (localhost_only checks the peer).
        proxy_headers=False,
    )


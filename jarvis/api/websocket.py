"""WebSocket helpers."""

from __future__ import annotations

from fastapi import WebSocket, status

from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager, tokens_match


async def authenticate_websocket(
    websocket: WebSocket,
    config: JarvisConfig,
    secret_manager: SecretManager,
) -> bool:
    """Authenticate a WebSocket with the local API token."""
    if not config.security.api_token_enabled:
        await websocket.accept()
        return True

    expected = secret_manager.get_api_token()
    # The query parameter is kept for compatibility; the log redaction filter strips it from
    # uvicorn's request logs. Prefer the Authorization header.
    supplied = websocket.query_params.get("token")
    authorization = websocket.headers.get("authorization")
    if supplied is None and authorization is not None and authorization.startswith("Bearer "):
        supplied = authorization[len("Bearer ") :]

    if not tokens_match(supplied, expected):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    await websocket.accept()
    return True


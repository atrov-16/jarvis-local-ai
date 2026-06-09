"""WebSocket helpers."""

from __future__ import annotations

from fastapi import WebSocket, status

from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager


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
    supplied = websocket.query_params.get("token")
    authorization = websocket.headers.get("authorization")
    if supplied is None and authorization is not None and authorization.startswith("Bearer "):
        supplied = authorization[len("Bearer ") :]

    if expected is None or supplied != expected:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    await websocket.accept()
    return True


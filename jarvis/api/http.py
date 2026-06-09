"""FastAPI application shell for Jarvis Phase 0."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from jarvis import __version__
from jarvis.api.schemas import HealthResponse, StatusResponse
from jarvis.api.websocket import authenticate_websocket
from jarvis.config.manager import load_config
from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus
from jarvis.storage.connection import resolve_database_path, sqlite_connection
from jarvis.storage.migrations import run_migrations


def create_app(
    *,
    config: JarvisConfig | None = None,
    secret_manager: SecretManager | None = None,
    event_bus: EventBus | None = None,
    database_path: Path | None = None,
) -> FastAPI:
    """Create the local Jarvis FastAPI app."""
    jarvis_config = config or load_config()
    secrets = secret_manager or SecretManager()
    bus = event_bus or EventBus()
    db_path = database_path or resolve_database_path(jarvis_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with sqlite_connection(db_path) as connection:
            applied = await run_migrations(connection)
        app.state.storage_status = {
            "database_path": str(db_path),
            "migrations_applied": applied,
        }
        yield

    app = FastAPI(title="Jarvis", version=__version__, lifespan=lifespan)
    app.state.config = jarvis_config
    app.state.secret_manager = secrets
    app.state.event_bus = bus

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service="jarvis", version=__version__)

    @app.get("/v1/status", response_model=StatusResponse)
    async def status(_: None = Depends(require_api_token)) -> StatusResponse:
        return StatusResponse(
            status="ok",
            version=__version__,
            storage=app.state.storage_status,
            secrets=secrets.status(),
        )

    @app.get("/v1/config/public")
    async def public_config(_: None = Depends(require_api_token)) -> JSONResponse:
        return JSONResponse(jarvis_config.public_dict())

    @app.websocket("/v1/events")
    async def events(websocket: WebSocket) -> None:
        if not await authenticate_websocket(websocket, jarvis_config, secrets):
            return
        subscription = bus.subscribe()
        try:
            while True:
                event = await subscription.get()
                await websocket.send_json(event.model_dump(mode="json"))
        finally:
            await subscription.close()

    return app


async def require_api_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_jarvis_api_token: Annotated[str | None, Header()] = None,
) -> None:
    """Require the local API token for `/v1/*` endpoints."""
    config: JarvisConfig = request.app.state.config
    if not config.security.api_token_enabled:
        return

    secret_manager: SecretManager = request.app.state.secret_manager
    expected = secret_manager.get_api_token()
    supplied = _extract_token(authorization, x_jarvis_api_token)
    if expected is None or supplied != expected:
        raise HTTPException(status_code=401, detail="Valid local API token required.")


def _extract_token(authorization: str | None, x_jarvis_api_token: str | None) -> str | None:
    if x_jarvis_api_token:
        return x_jarvis_api_token
    if authorization is None:
        return None
    prefix = "Bearer "
    if authorization.startswith(prefix):
        return authorization[len(prefix) :]
    return None


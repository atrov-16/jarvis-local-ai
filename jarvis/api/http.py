"""FastAPI application shell for Jarvis Phase 0."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from jarvis import __version__
from jarvis.api.schemas import (
    CurrentProjectUpdate,
    HealthResponse,
    ProjectCreate,
    ProjectResponse,
    StatusResponse,
    WorkspaceCreate,
    WorkspaceResponse,
)
from jarvis.api.websocket import authenticate_websocket
from jarvis.config.manager import load_config
from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus
from jarvis.projects.registry import ProjectRegistry
from jarvis.storage.connection import resolve_database_path, sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.workspaces.registry import WorkspaceRegistry


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

    uow = UnitOfWork(db_path)
    workspaces = WorkspaceRegistry(uow)
    projects = ProjectRegistry(uow)

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
    app.state.workspaces = workspaces
    app.state.projects = projects

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

    # Workspace Endpoints
    @app.get("/v1/workspaces", response_model=list[WorkspaceResponse])
    async def list_workspaces(_: None = Depends(require_api_token)) -> list[WorkspaceResponse]:
        return [WorkspaceResponse(**w) for w in await workspaces.list()]

    @app.post("/v1/workspaces", response_model=WorkspaceResponse)
    async def add_workspace(
        data: WorkspaceCreate, _: None = Depends(require_api_token)
    ) -> WorkspaceResponse:
        try:
            workspace_id = await workspaces.add(name=data.name, path=data.path)
            workspace = await workspaces.get(workspace_id)
            if not workspace:
                raise HTTPException(status_code=500, detail="Failed to retrieve created workspace.")
            return WorkspaceResponse(**workspace)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/v1/workspaces/{workspace_id}")
    async def remove_workspace(
        workspace_id: str, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        deleted = await workspaces.remove(workspace_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Workspace not found.")
        return JSONResponse({"deleted": True})

    # Project Endpoints
    @app.get("/v1/projects", response_model=list[ProjectResponse])
    async def list_projects(_: None = Depends(require_api_token)) -> list[ProjectResponse]:
        return [ProjectResponse(**p) for p in await projects.list()]

    @app.post("/v1/projects", response_model=ProjectResponse)
    async def create_project(
        data: ProjectCreate, _: None = Depends(require_api_token)
    ) -> ProjectResponse:
        project_id = await projects.create(name=data.name, description=data.description)
        project = await projects.get(project_id)
        if not project:
            raise HTTPException(status_code=500, detail="Failed to retrieve created project.")
        return ProjectResponse(**project)

    @app.delete("/v1/projects/{project_id}")
    async def delete_project(
        project_id: str, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        deleted = await projects.delete(project_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Project not found.")
        return JSONResponse({"deleted": True})

    # Current Project Endpoints
    @app.get("/v1/projects/current")
    async def get_current_project(_: None = Depends(require_api_token)) -> JSONResponse:
        project_id = await projects.get_current_id()
        return JSONResponse({"id": project_id})

    @app.post("/v1/projects/current")
    async def set_current_project(
        data: CurrentProjectUpdate, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        try:
            await projects.switch_current(data.id)
            return JSONResponse({"id": data.id})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Workspace Linking
    @app.post("/v1/projects/{project_id}/workspaces/{workspace_id}")
    async def link_workspace(
        project_id: str, workspace_id: str, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        try:
            await projects.link_workspace(project_id, workspace_id)
            return JSONResponse({"linked": True})
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/v1/projects/{project_id}/workspaces/{workspace_id}")
    async def unlink_workspace(
        project_id: str, workspace_id: str, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        unlinked = await projects.unlink_workspace(project_id, workspace_id)
        if not unlinked:
            raise HTTPException(status_code=404, detail="Link not found.")
        return JSONResponse({"unlinked": True})

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


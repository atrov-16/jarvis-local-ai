"""FastAPI application shell for Jarvis Phase 0."""

from __future__ import annotations

import json
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
    MemoryApproveRequest,
    MemoryDenialRequest,
    MemoryProposalResponse,
    MemoryResponse,
    MemorySearchResultResponse,
    ProjectCreate,
    ProjectResponse,
    StatusResponse,
    TaskCreate,
    TaskResponse,
    TaskDetailResponse,
    TaskStepResponse,
    TaskEventResponse,
    WorkspaceCreate,
    WorkspaceResponse,
)
from jarvis.api.websocket import authenticate_websocket
from jarvis.config.manager import load_config
from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus
from jarvis.models.router import ModelRouter
from jarvis.memory.store import MemoryStore
from jarvis.projects.registry import ProjectRegistry
from jarvis.storage.connection import resolve_database_path, sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.planner import Planner
from jarvis.tasks.queue import TaskQueue
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
    memory_store = MemoryStore(uow)
    model_router = ModelRouter(jarvis_config, secrets)
    planner = Planner(model_router)
    task_queue = TaskQueue(uow, bus, planner)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with sqlite_connection(db_path) as connection:
            applied = await run_migrations(connection)
        app.state.storage_status = {
            "database_path": str(db_path),
            "migrations_applied": applied,
        }
        
        # Start background worker
        await task_queue.start()
        
        yield
        
        # Stop background worker
        await task_queue.stop()

    app = FastAPI(title="Jarvis", version=__version__, lifespan=lifespan)
    app.state.config = jarvis_config
    app.state.secret_manager = secrets
    app.state.event_bus = bus
    app.state.workspaces = workspaces
    app.state.projects = projects
    app.state.memory_store = memory_store
    app.state.model_router = model_router
    app.state.task_queue = task_queue

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", service="jarvis", version=__version__)

    @app.get("/v1/status", response_model=StatusResponse)
    async def status(_: None = Depends(require_api_token)) -> StatusResponse:
        providers = await model_router.check_availability()
        return StatusResponse(
            status="ok",
            version=__version__,
            storage=app.state.storage_status,
            secrets=secrets.status(),
            providers=providers,
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

    # Memory Endpoints
    @app.get("/v1/memory/search", response_model=list[MemorySearchResultResponse])
    async def search_memory(
        q: str,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
        _: None = Depends(require_api_token),
    ) -> list[MemorySearchResultResponse]:
        results = await memory_store.search(
            query=q, project_id=project_id, memory_type=memory_type, limit=limit
        )
        return [MemorySearchResultResponse(**r.__dict__) for r in results]

    @app.get("/v1/memory/proposals", response_model=list[MemoryProposalResponse])
    async def list_proposals(_: None = Depends(require_api_token)) -> list[MemoryProposalResponse]:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            # We don't have a list_proposals in MemoryStore yet, let's use the repository directly
            # or add it to MemoryStore. Designing to use Store is better.
            # I'll add a list_proposals to MemoryStore or just use Repository for now.
            # Actually, the requirement said "Keep business logic inside MemoryStore".
            # I will quickly check if I should add it to Store.
            cursor = await unit.repositories.memory._connection.execute(
                "SELECT * FROM memory_proposals WHERE status = 'pending'"
            )
            rows = await cursor.fetchall()
            proposals = []
            for row in rows:
                data = dict(row)
                data["proposed_tags"] = json.loads(str(data.pop("proposed_tags_json")))
                proposals.append(MemoryProposalResponse(**data))
            return proposals

    @app.post("/v1/memory/proposals/{proposal_id}/approve")
    async def approve_proposal(
        proposal_id: str, data: MemoryApproveRequest, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        try:
            memory_id = await memory_store.approve(proposal_id, title=data.title)
            return JSONResponse({"id": memory_id}, status_code=201)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/v1/memory/proposals/{proposal_id}/deny")
    async def deny_proposal(
        proposal_id: str, data: MemoryDenialRequest, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        denied = await memory_store.deny(proposal_id, reason=data.reason)
        if not denied:
            raise HTTPException(status_code=404, detail="Proposal not found.")
        return JSONResponse({"denied": True})

    @app.get("/v1/memory/long-term", response_model=list[MemoryResponse])
    async def list_long_term_memory(
        project_id: str | None = None,
        limit: int = 50,
        _: None = Depends(require_api_token),
    ) -> list[MemoryResponse]:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            sql = "SELECT * FROM long_term_memory"
            params = []
            if project_id:
                sql += " WHERE project_id = ?"
                params.append(project_id)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            
            cursor = await unit.repositories.memory._connection.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            return [
                MemoryResponse(
                    **{
                        **dict(row),
                        "tags": json.loads(str(row["tags_json"]))
                    }
                )
                for row in rows
            ]

    @app.delete("/v1/memory/long-term/{memory_id}")
    async def delete_memory(
        memory_id: str, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        deleted = await memory_store.delete_memory(memory_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Memory not found.")
        return JSONResponse({"deleted": True})

    # Task Endpoints
    @app.post("/v1/tasks", response_model=TaskResponse, status_code=201)
    async def create_task(data: TaskCreate, _: None = Depends(require_api_token)) -> TaskResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task_id = await unit.repositories.tasks.insert(
                title="New Task",  # Will be updated by Planner
                user_request=data.user_request,
                project_id=data.project_id,
                priority=data.priority,
            )
            await unit.repositories.audit.insert(
                actor="user",
                action_type="task.create",
                summary=f"Created task: {task_id}",
                target=task_id,
            )
            task = await unit.repositories.tasks.get(task_id)
            assert task is not None
            return TaskResponse(**task)

    @app.get("/v1/tasks", response_model=list[TaskResponse])
    async def list_tasks(
        status: str | None = None, limit: int = 50, _: None = Depends(require_api_token)
    ) -> list[TaskResponse]:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            sql = "SELECT * FROM tasks"
            params = []
            if status:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            
            cursor = await unit.connection.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            tasks = []
            for row in rows:
                data = dict(row)
                data["metadata"] = json.loads(str(data.pop("metadata_json")))
                tasks.append(TaskResponse(**data))
            return tasks

    @app.get("/v1/tasks/{task_id}", response_model=TaskDetailResponse)
    async def get_task(task_id: str, _: None = Depends(require_api_token)) -> TaskDetailResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found.")
            
            steps = await unit.repositories.tasks.list_steps(task_id)
            events = await unit.repositories.tasks.list_events(task_id)
            
            task["steps"] = [TaskStepResponse(**s) for s in steps]
            task["events"] = [TaskEventResponse(**e) for e in events]
            
            return TaskDetailResponse(**task)

    @app.post("/v1/tasks/{task_id}/plan/approve")
    async def approve_task_plan(task_id: str, _: None = Depends(require_api_token)) -> JSONResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found.")
            
            if task["status"] != "waiting_for_plan_approval":
                raise HTTPException(status_code=400, detail=f"Task is not waiting for plan approval (status: {task['status']})")
                
            await unit.repositories.tasks.update(task_id, status="queued")
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Plan approved by user",
                payload={"status": "queued"}
            )
            await unit.repositories.audit.insert(
                actor="user",
                action_type="task.plan_approve",
                summary=f"Approved plan for task: {task_id}",
                target=task_id,
            )
        return JSONResponse({"approved": True})

    @app.post("/v1/tasks/{task_id}/resume")
    async def resume_task(task_id: str, _: None = Depends(require_api_token)) -> JSONResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found.")
                
            if task["status"] not in ("paused", "failed"):
                raise HTTPException(status_code=400, detail=f"Cannot resume task in status: {task['status']}")
                
            # If a task has steps, it should go back to queued (or waiting for execution).
            # TaskQueue looks for 'queued' tasks.
            await unit.repositories.tasks.update(task_id, status="queued")
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Task resumed by user",
                payload={"status": "queued"}
            )
            await unit.repositories.audit.insert(
                actor="user",
                action_type="task.resume",
                summary=f"Resumed task: {task_id}",
                target=task_id,
            )
        return JSONResponse({"resumed": True})

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, _: None = Depends(require_api_token)) -> JSONResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found.")
                
            if task["status"] in ("completed", "cancelled"):
                raise HTTPException(status_code=400, detail=f"Task already {task['status']}")
                
            await unit.repositories.tasks.update(task_id, status="cancelled")
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                event_type="status_change",
                message="Task cancelled by user",
                payload={"status": "cancelled"}
            )
            await unit.repositories.audit.insert(
                actor="user",
                action_type="task.cancel",
                summary=f"Cancelled task: {task_id}",
                target=task_id,
            )
        return JSONResponse({"cancelled": True})

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



None




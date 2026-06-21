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
    ApprovalDecisionRequest,
    ApprovalResponse,
    ApprovalStats,
    BulkApprovalRequest,
    ConflictResolveRequest,
    CurrentProjectUpdate,
    HealthResponse,
    MemoryApproveRequest,
    MemoryDenialRequest,
    MemoryDetailResponse,
    MemoryProposalResponse,
    MemoryResponse,
    ProjectCreate,
    ProjectResponse,
    StatusResponse,
    TaskCreate,
    TaskDecisionRequest,
    TaskDetailResponse,
    TaskEventResponse,
    TaskResponse,
    TaskStepResponse,
    TaskSummaryResponse,
    TaskTraceResponse,
    UnifiedApprovalItem,
    WorkspaceCreate,
    WorkspaceResponse,
)
from jarvis.api.services.approval_center import ApprovalCenterService
from jarvis.api.services.memory_browser import MemoryBrowserService
from jarvis.api.services.trace import TraceService
from jarvis.api.websocket import authenticate_websocket
from jarvis.approvals.broker import ApprovalBroker
from jarvis.config.manager import load_config
from jarvis.config.models import JarvisConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus
from jarvis.core.orphan_recovery import OrphanRecoveryService
from jarvis.core.process_registry import ProcessRegistryService
from jarvis.core.recovery import SystemRecoveryService
from jarvis.core.reflection import ReflectionService
from jarvis.memory.store import MemoryStore
from jarvis.models.router import ModelRouter
from jarvis.projects.registry import ProjectRegistry
from jarvis.storage.connection import resolve_database_path, sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks.command_runner import CommandRunner
from jarvis.tasks.planner import Planner
from jarvis.tasks.queue import TaskQueue
from jarvis.tools.build_runner import BuildTool
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.filesystem import (
    DeleteFileTool,
    ListDirectoryTool,
    PatchFileTool,
    ReadFileTool,
    RestoreFileTool,
    WriteFileTool,
)
from jarvis.tools.generic_command import GenericCommandTool
from jarvis.tools.git import GitTool
from jarvis.tools.memory import CreateMemoryProposalTool, SearchMemoryTool
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.tasks import GetTaskStatusTool
from jarvis.tools.test_runner import TestTool
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
    approval_broker = ApprovalBroker(uow, bus)
    reflection_service = ReflectionService(uow, bus, model_router, memory_store)
    trace_service = TraceService(uow, model_router)
    memory_browser = MemoryBrowserService(uow, memory_store)
    approval_center = ApprovalCenterService(uow, approval_broker, memory_store)
    process_registry = ProcessRegistryService(uow)
    orphan_recovery = OrphanRecoveryService(process_registry)
    system_recovery = SystemRecoveryService(uow, bus, orphan_recovery)

    # Tool System
    registry = ToolRegistry()
    command_runner = CommandRunner(process_registry)
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(PatchFileTool())
    registry.register(DeleteFileTool())
    registry.register(RestoreFileTool())
    registry.register(ListDirectoryTool())
    registry.register(GitTool(command_runner))
    registry.register(TestTool(command_runner))
    registry.register(BuildTool(command_runner))
    registry.register(GenericCommandTool(command_runner))
    registry.register(SearchMemoryTool())
    registry.register(CreateMemoryProposalTool())
    registry.register(GetTaskStatusTool())
    tool_executor = ToolExecutor(registry, approval_broker)

    task_queue = TaskQueue(uow, bus, planner, tool_executor, approval_broker)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with sqlite_connection(db_path) as connection:
            applied = await run_migrations(connection)
        app.state.storage_status = {
            "database_path": str(db_path),
            "migrations_applied": applied,
        }
        
        # Start background services
        await system_recovery.run_startup_recovery()
        await task_queue.start()
        await reflection_service.start()
        
        yield
        
        # Stop background services
        await reflection_service.stop()
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
    app.state.approval_broker = approval_broker

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
        return [WorkspaceResponse.model_validate(w) for w in await workspaces.list()]

    @app.post("/v1/workspaces", response_model=WorkspaceResponse)
    async def add_workspace(
        data: WorkspaceCreate, _: None = Depends(require_api_token)
    ) -> WorkspaceResponse:
        try:
            workspace_id = await workspaces.add(name=data.name, path=data.path)
            workspace = await workspaces.get(workspace_id)
            if not workspace:
                raise HTTPException(status_code=500, detail="Failed to retrieve created workspace.")
            return WorkspaceResponse.model_validate(workspace)
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
        return [ProjectResponse.model_validate(p) for p in await projects.list()]

    @app.post("/v1/projects", response_model=ProjectResponse)
    async def create_project(
        data: ProjectCreate, _: None = Depends(require_api_token)
    ) -> ProjectResponse:
        project_id = await projects.create(name=data.name, description=data.description)
        project = await projects.get(project_id)
        if not project:
            raise HTTPException(status_code=500, detail="Failed to retrieve created project.")
        return ProjectResponse.model_validate(project)

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

    # Approval Center Endpoints
    @app.get("/v1/approvals/center", response_model=list[UnifiedApprovalItem])
    async def list_unified_approvals(
        limit: int = 50, offset: int = 0, _: None = Depends(require_api_token)
    ) -> list[UnifiedApprovalItem]:
        return await approval_center.list_pending(limit=limit, offset=offset)

    @app.post("/v1/approvals/bulk")
    async def bulk_approval(
        data: BulkApprovalRequest, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        results = await approval_center.bulk_respond(data)
        return JSONResponse(results)

    @app.get("/v1/approvals/stats", response_model=ApprovalStats)
    async def get_approval_stats(_: None = Depends(require_api_token)) -> ApprovalStats:
        return await approval_center.get_stats()

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

    # Memory Browser & Proposal Endpoints
    @app.get("/v1/memories/proposals", response_model=list[MemoryProposalResponse])
    async def list_memory_proposals(
        project_id: str | None = None,
        status: str = "pending",
        limit: int = 50,
        offset: int = 0,
        _: None = Depends(require_api_token)
    ) -> list[MemoryProposalResponse]:
        return await memory_browser.list_proposals(project_id=project_id, status=status, limit=limit, offset=offset)

    @app.get("/v1/memories/proposals/{proposal_id}", response_model=MemoryProposalResponse)
    async def get_memory_proposal(proposal_id: str, _: None = Depends(require_api_token)) -> MemoryProposalResponse:
        try:
            return await memory_browser.get_proposal(proposal_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/v1/memories/proposals/{proposal_id}/approve", response_model=MemoryResponse)
    async def approve_memory_proposal(
        proposal_id: str, data: MemoryApproveRequest, _: None = Depends(require_api_token)
    ) -> MemoryResponse:
        try:
            memory_id = await memory_store.approve(proposal_id, title=data.title)
            async with uow.begin() as unit:
                assert unit.repositories is not None
                memory = await unit.repositories.memory.get_long_term(memory_id)
                assert memory is not None
                return MemoryResponse.model_validate(memory)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/v1/memories/proposals/{proposal_id}/deny")
    async def deny_memory_proposal(
        proposal_id: str, data: MemoryDenialRequest, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        success = await memory_store.deny(proposal_id, reason=data.reason)
        if not success:
            raise HTTPException(status_code=404, detail="Proposal not found.")
        return JSONResponse({"denied": True})

    @app.get("/v1/memories", response_model=list[MemoryResponse])
    async def list_memories(
        project_id: str | None = None,
        status: str | None = "active",
        memory_type: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        _: None = Depends(require_api_token)
    ) -> list[MemoryResponse]:
        return await memory_browser.list_memories(
            project_id=project_id, 
            status=status, 
            memory_type=memory_type, 
            q=q, 
            limit=limit, 
            offset=offset
        )

    @app.get("/v1/memories/{memory_id}", response_model=MemoryDetailResponse)
    async def get_memory_detail(
        memory_id: str, 
        lineage_depth: int = 3,
        _: None = Depends(require_api_token)
    ) -> MemoryDetailResponse:
        try:
            return await memory_browser.get_memory_detail(memory_id, lineage_depth=lineage_depth)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/v1/memories/{memory_id}", response_model=MemoryResponse)
    async def update_memory(
        memory_id: str, 
        data: dict[str, Any],
        _: None = Depends(require_api_token)
    ) -> MemoryResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            success = await unit.repositories.memory.update_long_term(memory_id, **data)
            if not success:
                raise HTTPException(status_code=404, detail="Memory not found.")
            m = await unit.repositories.memory.get_long_term(memory_id)
            assert m is not None
            return MemoryResponse.model_validate(m)

    @app.delete("/v1/memories/{memory_id}")
    async def delete_memory(
        memory_id: str, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        deleted = await memory_store.delete_memory(memory_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Memory not found.")
        return JSONResponse({"deleted": True})

    @app.post("/v1/memories/{memory_id}/resolve")
    async def resolve_memory_conflict(
        memory_id: str, 
        data: ConflictResolveRequest,
        _: None = Depends(require_api_token)
    ) -> JSONResponse:
        try:
            await memory_browser.resolve_conflict(
                memory_id, 
                action=data.action, 
                winner_id=data.winner_id,
                conflicting_ids=data.conflicting_ids,
                reason=data.reason
            )
            return JSONResponse({"resolved": True})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Approval Endpoints
    @app.get("/v1/approvals", response_model=list[ApprovalResponse])
    async def list_approvals(_: None = Depends(require_api_token)) -> list[ApprovalResponse]:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            rows = await unit.repositories.approvals.list_pending()
            return [ApprovalResponse.model_validate(r) for r in rows]

    @app.get("/v1/approvals/{approval_id}", response_model=ApprovalResponse)
    async def get_approval(approval_id: str, _: None = Depends(require_api_token)) -> ApprovalResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            request = await unit.repositories.approvals.get(approval_id)
            if not request:
                raise HTTPException(status_code=404, detail="Approval request not found.")
            return ApprovalResponse.model_validate(request)

    @app.post("/v1/approvals/{approval_id}/approve")
    async def approve_request(
        approval_id: str, data: ApprovalDecisionRequest, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        success = await approval_broker.approve(approval_id, reason=data.reason)
        if not success:
            raise HTTPException(status_code=404, detail="Approval request not found.")
        return JSONResponse({"approved": True})

    @app.post("/v1/approvals/{approval_id}/deny")
    async def deny_request(
        approval_id: str, data: ApprovalDecisionRequest, _: None = Depends(require_api_token)
    ) -> JSONResponse:
        success = await approval_broker.deny(approval_id, reason=data.reason)
        if not success:
            raise HTTPException(status_code=404, detail="Approval request not found.")
        return JSONResponse({"denied": True})

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
            return TaskResponse.model_validate(task)

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
            assert unit.connection is not None
            cursor = await unit.connection.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            tasks = []
            for row in rows:
                data = dict(row)
                data["metadata"] = json.loads(str(data.pop("metadata_json")))
                tasks.append(TaskResponse.model_validate(data))
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
            
            task["steps"] = [TaskStepResponse.model_validate(s) for s in steps]
            task["events"] = [TaskEventResponse.model_validate(e) for e in events]
            
            return TaskDetailResponse.model_validate(task)

    @app.get("/v1/tasks/{task_id}/trace", response_model=TaskTraceResponse)
    async def get_task_trace(task_id: str, include_system: bool = False, _: None = Depends(require_api_token)) -> TaskTraceResponse:
        try:
            return await trace_service.get_task_trace(task_id, include_system=include_system)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/v1/tasks/{task_id}/summary", response_model=TaskSummaryResponse)
    async def get_task_summary(task_id: str, request: Request, _: None = Depends(require_api_token)) -> TaskSummaryResponse:
        try:
            # Gather secrets for redaction
            secrets = []
            if hasattr(request.app.state, "secret_manager"):
                all_secrets = await request.app.state.secret_manager.list_secrets()
                for s in all_secrets:
                    val = await request.app.state.secret_manager.get_secret(s["name"])
                    if val:
                        secrets.append(val)
            
            return await trace_service.get_task_summary(task_id, secrets=secrets)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/v1/tasks/{task_id}/plan/approve")
    async def approve_task_plan(task_id: str, _: None = Depends(require_api_token)) -> JSONResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found.")
            
            if task["status"] != "waiting_for_plan_approval":
                raise HTTPException(status_code=400, detail=f"Task is not waiting for plan approval (status: {task['status']})")
                
            # Find the plan approval request
            assert unit.connection is not None
            cursor = await unit.connection.execute(
                "SELECT id FROM approval_requests WHERE task_id = ? AND action_type = 'plan' AND status = 'pending' LIMIT 1",
                (task_id,)
            )
            row = await cursor.fetchone()
            if row:
                await approval_broker.approve(row["id"], unit=unit)

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
                task_id=task_id,
            )
        return JSONResponse({"approved": True})

    @app.post("/v1/tasks/{task_id}/resume")
    async def resume_task(task_id: str, _: None = Depends(require_api_token)) -> JSONResponse:
        async with uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found.")

            if task["status"] not in ("paused", "failed", "interrupted"):
                raise HTTPException(status_code=400, detail=f"Task cannot be resumed from status: {task['status']}")

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
                task_id=task_id,
            )
        return JSONResponse({"resumed": True})

    @app.post("/v1/tasks/{task_id}/steps/{step_id}/approve", response_model=TaskStepResponse)
    async def approve_task_step(
        task_id: str,
        step_id: str,
        data: TaskDecisionRequest,
        _: None = Depends(require_api_token),
    ) -> TaskStepResponse:
        """Approve a paused task step."""
        async with uow.begin() as unit:
            assert unit.repositories is not None
            # Find the tool approval request
            assert unit.connection is not None
            cursor = await unit.connection.execute(
                "SELECT id FROM approval_requests WHERE step_id = ? AND status = 'pending' LIMIT 1",
                (step_id,)
            )
            row = await cursor.fetchone()
            if row:
                await approval_broker.approve(row["id"], reason=data.reason)
                
            # Update step and task status
            await unit.repositories.tasks.update_step(step_id, status="approved")
            await unit.repositories.tasks.update(task_id, status="queued")
            
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                step_id=step_id,
                event_type="approval_granted",
                message="Step approved by user",
                payload={"reason": data.reason}
            )
            
            updated_step = await unit.repositories.tasks.get_step(step_id)
            assert updated_step is not None
            return TaskStepResponse.model_validate(updated_step)

    @app.post("/v1/tasks/{task_id}/steps/{step_id}/deny", response_model=TaskStepResponse)
    async def deny_task_step(
        task_id: str,
        step_id: str,
        data: TaskDecisionRequest,
        _: None = Depends(require_api_token),
    ) -> TaskStepResponse:
        """Deny a paused task step, failing the task."""
        async with uow.begin() as unit:
            assert unit.repositories is not None
            # Find the approval request
            assert unit.connection is not None
            cursor = await unit.connection.execute(
                "SELECT id FROM approval_requests WHERE step_id = ? AND status = 'pending' LIMIT 1",
                (step_id,)
            )
            row = await cursor.fetchone()
            if row:
                await approval_broker.deny(row["id"], reason=data.reason)

            # Update status
            error_msg = f"Denied by user: {data.reason}" if data.reason else "Denied by user."
            await unit.repositories.tasks.update_step(step_id, status="failed", error=error_msg)
            await unit.repositories.tasks.update(task_id, status="failed")
            
            await unit.repositories.tasks.insert_event(
                task_id=task_id,
                step_id=step_id,
                event_type="approval_denied",
                message="Step denied by user",
                payload={"reason": data.reason}
            )
            
            updated_step = await unit.repositories.tasks.get_step(step_id)
            assert updated_step is not None
            return TaskStepResponse.model_validate(updated_step)

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




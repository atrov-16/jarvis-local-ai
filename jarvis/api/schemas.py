"""API response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


from jarvis.models.schemas import ProviderStatus


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class StatusResponse(BaseModel):
    status: str
    version: str
    storage: dict[str, object] = Field(default_factory=dict)
    secrets: dict[str, bool] = Field(default_factory=dict)
    providers: list[ProviderStatus] = Field(default_factory=list)


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    path: str
    enabled: bool
    read_policy: str
    write_policy: str
    created_at: str
    updated_at: str


class WorkspaceCreate(BaseModel):
    name: str
    path: str


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: str
    created_at: str
    updated_at: str


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class CurrentProjectUpdate(BaseModel):
    id: str | None = None


class MemoryResponse(BaseModel):
    id: str
    project_id: str | None = None
    memory_type: str
    title: str | None = None
    content: str
    tags: list[str] = Field(default_factory=list)
    source: str
    created_at: str
    updated_at: str


class MemorySearchResultResponse(MemoryResponse):
    relevance_score: float


class MemoryProposalResponse(BaseModel):
    id: str
    project_id: str | None = None
    task_id: str | None = None
    memory_type: str
    proposed_content: str
    proposed_tags: list[str] = Field(default_factory=list)
    reason: str
    status: str
    created_at: str
    decided_at: str | None = None


class MemoryApproveRequest(BaseModel):
    title: str | None = None


class MemoryDenialRequest(BaseModel):
    reason: str | None = None


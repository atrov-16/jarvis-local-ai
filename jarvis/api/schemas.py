"""API response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class StatusResponse(BaseModel):
    status: str
    version: str
    storage: dict[str, object] = Field(default_factory=dict)
    secrets: dict[str, bool] = Field(default_factory=dict)


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


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


"""Models for the Jarvis approval system."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class RiskLevel(str, Enum):
    """Risk levels for actions that might require approval."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalActionType(str, Enum):
    """Types of actions that can be approved."""
    TOOL = "tool"
    PLAN = "plan"
    STEP = "step"
    COMMAND = "command"
    EXTERNAL = "external"


class ProposedAction(BaseModel):
    """An action that might require approval."""
    action_type: ApprovalActionType
    summary: str
    action_json: str
    context_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    risk_level: RiskLevel = RiskLevel.LOW

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
    status: str
    importance: float = 0.5
    confidence_score: float = 1.0
    access_count: int = 0
    last_retrieved_at: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class MemorySearchResultResponse(MemoryResponse):
    relevance_score: float


class MemoryHealthMetrics(BaseModel):
    access_count: int
    confidence_score: float
    importance: float
    merge_count: int
    conflict_count: int
    last_retrieved_at: str | None


class LineageNode(BaseModel):
    id: str
    type: str  # "task", "memory", "reflection", "proposal"
    summary: str
    timestamp: str
    metadata: dict[str, object] = Field(default_factory=dict)
    children: list[LineageNode] = Field(default_factory=list)


class MemoryDetailResponse(MemoryResponse):
    metrics: MemoryHealthMetrics
    lineage: list[LineageNode] = Field(default_factory=list)


class ConflictResolveRequest(BaseModel):
    action: str = Field(..., pattern="^(pick_winner|merge_manual|ignore)$")
    winner_id: str | None = None
    conflicting_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class MemoryProposalResponse(BaseModel):
    id: str
    project_id: str | None = None
    task_id: str | None = None
    memory_type: str
    proposed_content: str
    proposed_tags: list[str] = Field(default_factory=list)
    reason: str
    status: str
    importance: float = 0.5
    confidence_score: float = 1.0
    source_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str
    decided_at: str | None = None



class MemoryApproveRequest(BaseModel):
    title: str | None = None


class MemoryDenialRequest(BaseModel):
    reason: str | None = None


class TaskCreate(BaseModel):
    user_request: str
    project_id: str | None = None
    priority: int = 100


class TaskStepResponse(BaseModel):
    id: str
    step_index: int
    title: str
    description: str | None = None
    tool_name: str | None = None
    input_json: str | None = None
    status: str
    requires_approval: bool
    attempt_count: int
    output_json: str | None = None
    error: str | None = None


class TaskEventResponse(BaseModel):
    id: str
    step_id: str | None = None
    event_type: str
    message: str | None = None
    payload_json: str | None = None
    created_at: str


class TaskResponse(BaseModel):
    id: str
    parent_task_id: str | None = None
    project_id: str | None = None
    title: str
    user_request: str
    status: str
    priority: int
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    claimed_at: str | None = None


class TraceEntry(BaseModel):
    timestamp: str
    type: str  # "event", "step", "audit", "approval", "memory"
    actor: str
    severity: str = "info" # "info", "warning", "error", "critical"
    summary: str
    details: dict[str, object] = Field(default_factory=dict)
    step_id: str | None = None
    correlation_id: str | None = None


class TaskTraceResponse(BaseModel):
    task_id: str
    entries: list[TraceEntry]


class TaskSummaryResponse(BaseModel):
    task_id: str
    status: str
    summary: str | None = None
    outcome: str | None = None
    tokens_used: int | None = None
    wall_time: str | None = None


class TaskDetailResponse(TaskResponse):
    steps: list[TaskStepResponse] = Field(default_factory=list)
    events: list[TaskEventResponse] = Field(default_factory=list)


class TaskDecisionRequest(BaseModel):
    reason: str | None = Field(None, description="Optional reason for the decision.")

class UnifiedApprovalItem(BaseModel):
    id: str
    type: str  # "action", "memory", "consolidation"
    subtype: str | None = None  # e.g., "tool", "plan", "fact", "merge"
    summary: str
    risk_level: str = "medium"
    status: str = "pending"
    task_id: str | None = None
    created_at: str
    metadata: dict[str, object] = Field(default_factory=dict)


class BulkApprovalItem(BaseModel):
    id: str
    type: str


class BulkApprovalRequest(BaseModel):
    action: str = Field(..., pattern="^(approve|deny)$")
    items: list[BulkApprovalItem]
    reason: str | None = None


class ApprovalStats(BaseModel):
    pending_count: int
    avg_decision_time_sec: float | None = None
    by_risk: dict[str, int] = Field(default_factory=dict)
    approval_rate: float | None = None


class ApprovalResponse(BaseModel):
    id: str
    task_id: str | None = None
    step_id: str | None = None
    action_type: str
    risk_level: str
    summary: str
    action_json: str
    action_hash: str
    context_id: str | None = None
    status: str
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    expires_at: str | None = None


class ApprovalDecisionRequest(BaseModel):
    reason: str | None = Field(None, description="Optional reason for the decision.")


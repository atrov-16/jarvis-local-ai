-- Migration 0008: Approval System
-- Description: Adds tables for centralized approval requests and rules.

CREATE TABLE approval_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    step_id TEXT,
    action_type TEXT NOT NULL, -- 'tool', 'plan', 'step', 'command', 'external'
    risk_level TEXT NOT NULL,
    summary TEXT NOT NULL,
    action_json TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    context_id TEXT, -- e.g., workspace_id
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    expires_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (step_id) REFERENCES task_steps(id) ON DELETE CASCADE
);

CREATE INDEX idx_approval_requests_status ON approval_requests(status);
CREATE INDEX idx_approval_requests_task_id ON approval_requests(task_id);

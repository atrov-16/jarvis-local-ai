-- Migration 0004: Tasks and Planning
-- Description: Adds tables for durable tasks, steps, and events.

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    project_id TEXT,
    title TEXT NOT NULL,
    user_request TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'planning', 'waiting_for_plan_approval', 'running', 'paused', 'completed', 'cancelled', 'failed')),
    priority INTEGER NOT NULL DEFAULT 100,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);

CREATE TABLE task_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    tool_name TEXT,
    input_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'waiting_for_approval', 'completed', 'skipped', 'failed')),
    requires_approval INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    output_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE task_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_id TEXT,
    event_type TEXT NOT NULL,
    message TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (step_id) REFERENCES task_steps(id) ON DELETE CASCADE
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_task_steps_task_id ON task_steps(task_id);
CREATE INDEX idx_task_events_task_id ON task_events(task_id);

-- Migration 0015: Add interrupted task status
-- Description: Updates the tasks table status constraint to include 'interrupted'

-- SQLite requires table recreation to alter CHECK constraints
CREATE TABLE tasks_new (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    project_id TEXT,
    title TEXT NOT NULL,
    user_request TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'planning', 'waiting_for_plan_approval', 'running', 'paused', 'completed', 'cancelled', 'failed', 'interrupted')),
    priority INTEGER NOT NULL DEFAULT 100,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    claimed_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);

-- Copy data
INSERT INTO tasks_new SELECT * FROM tasks;

-- Drop old table
DROP TABLE tasks;

-- Rename new table
ALTER TABLE tasks_new RENAME TO tasks;

-- Recreate index
CREATE INDEX idx_tasks_status ON tasks(status);

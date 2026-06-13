-- Migration: 0006_add_approved_status
-- Description: Updates the CHECK constraint on task_steps to include 'approved'.

-- Disable foreign key checks temporarily for table recreation
PRAGMA foreign_keys=OFF;

CREATE TABLE task_steps_new (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    tool_name TEXT,
    input_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'running', 'waiting_for_approval', 'completed', 'skipped', 'failed')),
    requires_approval INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    output_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

INSERT INTO task_steps_new SELECT * FROM task_steps;

DROP TABLE task_steps;

ALTER TABLE task_steps_new RENAME TO task_steps;

CREATE INDEX idx_task_steps_task_id ON task_steps(task_id);

PRAGMA foreign_keys=ON;

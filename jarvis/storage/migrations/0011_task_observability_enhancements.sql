-- Migration 0011: Task Observability Enhancements
-- Description: Adds correlation_id and severity to task_events for better tracing.

PRAGMA foreign_keys=OFF;

-- 1. Update task_events with correlation_id and severity
CREATE TABLE task_events_new (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_id TEXT,
    event_type TEXT NOT NULL,
    message TEXT,
    payload_json TEXT,
    severity TEXT NOT NULL DEFAULT 'info',
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (step_id) REFERENCES task_steps(id) ON DELETE CASCADE
);

INSERT INTO task_events_new (id, task_id, step_id, event_type, message, payload_json, created_at)
SELECT id, task_id, step_id, event_type, message, payload_json, created_at
FROM task_events;

DROP TABLE task_events;
ALTER TABLE task_events_new RENAME TO task_events;

CREATE INDEX idx_task_events_task_id ON task_events(task_id);
CREATE INDEX idx_task_events_correlation_id ON task_events(correlation_id);

PRAGMA foreign_keys=ON;

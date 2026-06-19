CREATE TABLE process_registry (
    id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    command_display TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

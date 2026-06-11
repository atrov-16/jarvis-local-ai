-- Migration: 0002_projects_and_workspaces
-- Description: Create tables for workspaces, projects, and related entities.

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    enabled INTEGER NOT NULL DEFAULT 1,
    read_policy TEXT NOT NULL DEFAULT 'auto_inside_workspace',
    write_policy TEXT NOT NULL DEFAULT 'approval_required',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_workspaces (
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, workspace_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_workspaces_workspace_id ON project_workspaces(workspace_id);

CREATE TABLE IF NOT EXISTS project_notes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_notes_project_id ON project_notes(project_id);

CREATE TABLE IF NOT EXISTS project_goals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_goals_project_id ON project_goals(project_id);

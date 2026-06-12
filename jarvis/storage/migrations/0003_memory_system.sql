-- Migration 0003: Memory System
-- Description: Adds tables for short-term, long-term, and proposed memories with FTS5 search.

-- Memory types: fact, preference, project, task_context, note
-- Memory statuses: pending, approved, denied, expired

CREATE TABLE short_term_context (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    source TEXT NOT NULL,
    role TEXT,
    content TEXT NOT NULL,
    tags_json TEXT DEFAULT '[]',
    importance INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE long_term_memory (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    memory_type TEXT NOT NULL CHECK (memory_type IN ('fact', 'preference', 'project', 'task_context', 'note')),
    title TEXT,
    content TEXT NOT NULL,
    tags_json TEXT DEFAULT '[]',
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE memory_proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    memory_type TEXT NOT NULL CHECK (memory_type IN ('fact', 'preference', 'project', 'task_context', 'note')),
    proposed_content TEXT NOT NULL,
    proposed_tags_json TEXT DEFAULT '[]',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- Full-Text Search (FTS5) for long_term_memory
-- Using internal content (standard FTS5 table) for V1 to ensure robust UUID handling
-- and avoid synchronization complexities with external content tables.
CREATE VIRTUAL TABLE long_term_memory_idx USING fts5(
    id UNINDEXED,
    title,
    content
);

-- Triggers to keep FTS5 index in sync with long_term_memory
CREATE TRIGGER long_term_memory_ai AFTER INSERT ON long_term_memory BEGIN
    INSERT INTO long_term_memory_idx(id, title, content) VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER long_term_memory_ad AFTER DELETE ON long_term_memory BEGIN
    DELETE FROM long_term_memory_idx WHERE id = old.id;
END;

CREATE TRIGGER long_term_memory_au AFTER UPDATE ON long_term_memory BEGIN
    UPDATE long_term_memory_idx SET title = new.title, content = new.content WHERE id = old.id;
END;

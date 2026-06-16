-- Migration 0012: Memory Health Metrics
-- Description: Adds confidence_score to long_term_memory and ensures all health metrics are available.

PRAGMA foreign_keys=OFF;

-- 1. Update long_term_memory with confidence_score
CREATE TABLE long_term_memory_new (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    memory_type TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    tags_json TEXT DEFAULT '[]',
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'flagged', 'archived', 'merged')),
    importance REAL NOT NULL DEFAULT 0.5,
    confidence_score REAL DEFAULT 1.0,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TEXT,
    source_ids_json TEXT DEFAULT '[]',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

INSERT INTO long_term_memory_new (
    id, project_id, task_id, memory_type, title, content, 
    tags_json, source, status, importance, access_count, 
    last_retrieved_at, source_ids_json, metadata_json, 
    created_at, updated_at, archived_at
)
SELECT 
    id, project_id, task_id, memory_type, title, content, 
    tags_json, source, status, importance, access_count, 
    last_retrieved_at, source_ids_json, metadata_json, 
    created_at, updated_at, archived_at 
FROM long_term_memory;

DROP TABLE long_term_memory;
ALTER TABLE long_term_memory_new RENAME TO long_term_memory;

-- 2. Re-create triggers for FTS5
CREATE TRIGGER long_term_memory_ai AFTER INSERT ON long_term_memory BEGIN
    INSERT INTO long_term_memory_idx(id, title, content) VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER long_term_memory_ad AFTER DELETE ON long_term_memory BEGIN
    DELETE FROM long_term_memory_idx WHERE id = old.id;
END;

CREATE TRIGGER long_term_memory_au AFTER UPDATE ON long_term_memory BEGIN
    UPDATE long_term_memory_idx SET title = new.title, content = new.content WHERE id = old.id;
END;

PRAGMA foreign_keys=ON;

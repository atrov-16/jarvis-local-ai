-- Migration 0007: Adaptive Intelligence
-- Description: Adds importance, access tracking, and source lineage to memory tables, and removes rigid memory_type CHECK constraints.

PRAGMA foreign_keys=OFF;

-- 1. Recreate long_term_memory
CREATE TABLE long_term_memory_new (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    memory_type TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    tags_json TEXT DEFAULT '[]',
    source TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TEXT,
    source_ids_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

INSERT INTO long_term_memory_new (id, project_id, task_id, memory_type, title, content, tags_json, source, created_at, updated_at, archived_at)
SELECT id, project_id, task_id, memory_type, title, content, tags_json, source, created_at, updated_at, archived_at 
FROM long_term_memory;

-- 2. Recreate memory_proposals
CREATE TABLE memory_proposals_new (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    memory_type TEXT NOT NULL,
    proposed_content TEXT NOT NULL,
    proposed_tags_json TEXT DEFAULT '[]',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    importance REAL NOT NULL DEFAULT 0.5,
    source_ids_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

INSERT INTO memory_proposals_new (id, project_id, task_id, memory_type, proposed_content, proposed_tags_json, reason, status, created_at, decided_at)
SELECT id, project_id, task_id, memory_type, proposed_content, proposed_tags_json, reason, status, created_at, decided_at 
FROM memory_proposals;

-- 3. Swap tables
DROP TABLE long_term_memory;
ALTER TABLE long_term_memory_new RENAME TO long_term_memory;

DROP TABLE memory_proposals;
ALTER TABLE memory_proposals_new RENAME TO memory_proposals;

-- 4. Recreate triggers for FTS5 (long_term_memory_idx already exists, just re-attach triggers)
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

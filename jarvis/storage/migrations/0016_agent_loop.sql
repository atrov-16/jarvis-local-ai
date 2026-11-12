-- Migration 0016: Agent loop storage
-- Description: Stores what the observe-act loop needs to resume after a pause or a restart:
--   * tasks.final_answer      - the model's closing reply to the user
--   * task_steps.tool_call_id - the model's id for the call a step runs, so the step's result
--                               is sent back addressed to that call
--   * task_messages           - the conversation with the model, in order
--
-- Only ADD COLUMN and new objects: no table is rebuilt and no CHECK constraint changes, so
-- existing rows (and the foreign keys that point at them) are untouched.

ALTER TABLE tasks ADD COLUMN final_answer TEXT;

ALTER TABLE task_steps ADD COLUMN tool_call_id TEXT;

-- A call id addresses exactly one step within a task. Steps written before this migration (and
-- any step that is not a model tool call) have NULL, which the partial index ignores.
CREATE UNIQUE INDEX idx_task_steps_tool_call_id
    ON task_steps(task_id, tool_call_id)
    WHERE tool_call_id IS NOT NULL;

CREATE TABLE task_messages (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL DEFAULT '',
    -- JSON list of the calls an assistant message made; NULL for every other message.
    tool_calls_json TEXT,
    -- The call a 'tool' message answers; NULL for every other message.
    tool_call_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, seq),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

-- Migration 0005: Task Claimed At
-- Description: Adds claimed_at to tasks for recovery diagnostics.

ALTER TABLE tasks ADD COLUMN claimed_at TEXT;

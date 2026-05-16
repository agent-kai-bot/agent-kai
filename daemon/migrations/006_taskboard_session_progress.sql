-- 006_taskboard_session_progress.sql
--
-- Track dispatcher-session progress separately from creation time so the
-- stuck-session sweeper can distinguish "no progress" from long active runs.

ALTER TABLE sessions
    ADD COLUMN last_progress_at TEXT;

UPDATE sessions
SET last_progress_at = COALESCE(updated_at, created_at)
WHERE last_progress_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_dispatcher_progress
    ON sessions (source, status, last_progress_at, created_at)
    WHERE session_id IS NOT NULL;

-- 002_taskboard_dispatcher_sessions.sql
--
-- Durable session index for taskboard auto-fire dispatcher de-duplication.
-- The dispatcher also performs a runtime column check so this migration can
-- coexist with a sessions table introduced by sibling webhook-ingress work.

CREATE TABLE IF NOT EXISTS sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT UNIQUE,
    taskboard_task_id   INTEGER,
    fire_generation     INTEGER,
    agent_id            TEXT,
    source              TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    webhook_pending_id  TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    aborted_at          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_taskboard_fire_agent
    ON sessions (taskboard_task_id, fire_generation, agent_id)
    WHERE taskboard_task_id IS NOT NULL
      AND fire_generation IS NOT NULL
      AND agent_id IS NOT NULL;

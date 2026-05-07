CREATE TABLE IF NOT EXISTS auto_evaluator_cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    evaluator_kind TEXT NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_auto_evaluator_cost_session_created
ON auto_evaluator_cost_events(session_name, created_at);

CREATE TABLE IF NOT EXISTS main_agent_cost_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_main_agent_cost_session_created
ON main_agent_cost_events(session_name, created_at);

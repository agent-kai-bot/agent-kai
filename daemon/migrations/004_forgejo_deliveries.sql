-- 004_forgejo_deliveries.sql
--
-- Durable receipt and Phase 1 dispatch queue stub for signed Forgejo
-- webhook ingress. Phase 2 dispatcher workers will consume
-- forgejo_pending rows.

CREATE TABLE IF NOT EXISTS forgejo_deliveries (
    delivery_id      TEXT PRIMARY KEY,
    event_type       TEXT NOT NULL,
    action           TEXT NOT NULL,
    repo             TEXT NOT NULL,
    pr_number        INTEGER NOT NULL,
    head_sha         TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    signature_sha256 TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    hmac_status      TEXT NOT NULL,
    dispatch_status  TEXT NOT NULL DEFAULT 'pending',
    duplicate_count  INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    locked_at        TEXT,
    completed_at     TEXT
);

CREATE TABLE IF NOT EXISTS forgejo_pending (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id      TEXT NOT NULL UNIQUE,
    event_type       TEXT NOT NULL,
    action           TEXT NOT NULL,
    repo             TEXT NOT NULL,
    pr_number        INTEGER NOT NULL,
    head_sha         TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    locked_at        TEXT,
    completed_at     TEXT,
    FOREIGN KEY (delivery_id)
        REFERENCES forgejo_deliveries(delivery_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_forgejo_deliveries_received_at
    ON forgejo_deliveries (received_at);

CREATE INDEX IF NOT EXISTS idx_forgejo_deliveries_dispatch_status
    ON forgejo_deliveries (dispatch_status, received_at);

CREATE INDEX IF NOT EXISTS idx_forgejo_pending_status
    ON forgejo_pending (status, received_at);

CREATE INDEX IF NOT EXISTS idx_forgejo_pending_pr
    ON forgejo_pending (repo, pr_number, head_sha);

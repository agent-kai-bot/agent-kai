-- 001_taskboard_webhook_deliveries.sql
--
-- Creates the durable receipt store for signed taskboard webhooks.
-- The same table doubles as a Phase 2 dispatch queue: rows with
-- dispatch_status='accepted' are pending hand-off to the dispatcher
-- worker introduced in a later phase.

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id      TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL UNIQUE,
    event_type       TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    event_timestamp  INTEGER NOT NULL,
    signature_sha256 TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    hmac_status      TEXT NOT NULL,
    dispatch_status  TEXT NOT NULL DEFAULT 'accepted',
    duplicate_count  INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    locked_at        TEXT,
    completed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_dispatch_status
    ON webhook_deliveries (dispatch_status, received_at);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received_at
    ON webhook_deliveries (received_at);

-- 003_taskboard_audit_comments.sql
--
-- Records when the dispatcher has successfully posted the taskboard audit
-- comment for a webhook delivery. A NULL value means the spawn/abort outcome
-- is known but the source task still needs its audit comment retried.

ALTER TABLE webhook_deliveries
    ADD COLUMN audit_posted_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_audit_pending
    ON webhook_deliveries (dispatch_status, audit_posted_at);

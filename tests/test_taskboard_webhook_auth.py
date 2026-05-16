"""Tests for the Phase 1 taskboard webhook ingress route."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from daemon.db import apply_migrations, connect
from daemon.secrets import StaticWebhookSecretProvider
from daemon.server import create_app
from daemon.webhook_auth import (
    DEFAULT_TIMESTAMP_SKEW_SECONDS,
    SIGNATURE_PREFIX,
    WebhookHeaderError,
    WebhookSignatureError,
    WebhookTimestampError,
    compute_signature,
    parse_headers,
    signed_string,
    verify_signature,
)


SECRET = b"unit-test-shared-hmac-secret"


class _FakeBus:
    """Minimal async bus stub for daemon lifecycle tests."""

    def __init__(self, url: str, agent_name: str):
        self.url = url
        self.agent_name = agent_name

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def _build_signed_request(
    *,
    secret: bytes = SECRET,
    body: bytes | None = None,
    timestamp: int | None = None,
    delivery_id: str | None = None,
    event_type: str = "task.status_changed",
) -> tuple[bytes, dict[str, str], str]:
    """Return ``(body, headers, delivery_id)`` for a fresh signed delivery."""

    if body is None:
        body = json.dumps(
            {
                "event_id": str(uuid.uuid4()),
                "event_type": event_type,
                "task": {"id": 10151, "status": "In Progress"},
            }
        ).encode("utf-8")
    if timestamp is None:
        timestamp = int(time.time())
    if delivery_id is None:
        delivery_id = str(uuid.uuid4())
    digest = compute_signature(secret, timestamp, delivery_id, body)
    headers = {
        "X-Taskboard-Event": event_type,
        "X-Taskboard-Delivery": delivery_id,
        "X-Taskboard-Timestamp": str(timestamp),
        "X-Taskboard-Signature": f"{SIGNATURE_PREFIX}{digest}",
        "Content-Type": "application/json",
    }
    return body, headers, delivery_id


class WebhookAuthUnitTests(unittest.TestCase):
    """Unit tests for the pure HMAC helpers in :mod:`daemon.webhook_auth`."""

    def test_signed_string_layout(self) -> None:
        body = b'{"a":1}'
        out = signed_string(123, "11111111-1111-1111-1111-111111111111", body)
        self.assertTrue(out.startswith(b"123.11111111-1111-1111-1111-111111111111."))
        self.assertTrue(out.endswith(body))

    def test_compute_signature_matches_manual_hmac(self) -> None:
        body = b"abc"
        timestamp = 1700000000
        delivery_id = "22222222-2222-2222-2222-222222222222"
        digest = compute_signature(SECRET, timestamp, delivery_id, body)
        manual = hmac.new(
            SECRET,
            f"{timestamp}.{delivery_id}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(digest, manual)

    def test_parse_headers_rejects_non_uuid_delivery(self) -> None:
        with self.assertRaises(WebhookHeaderError):
            parse_headers(
                event_header="task.status_changed",
                delivery_header="not-a-uuid",
                timestamp_header="1700000000",
                signature_header="sha256=" + "0" * 64,
            )

    def test_parse_headers_rejects_non_integer_timestamp(self) -> None:
        with self.assertRaises(WebhookHeaderError):
            parse_headers(
                event_header="task.status_changed",
                delivery_header=str(uuid.uuid4()),
                timestamp_header="not-a-number",
                signature_header="sha256=" + "0" * 64,
            )

    def test_parse_headers_rejects_non_hex_signature(self) -> None:
        with self.assertRaises(WebhookHeaderError):
            parse_headers(
                event_header="task.status_changed",
                delivery_header=str(uuid.uuid4()),
                timestamp_header="1700000000",
                signature_header="sha256=ZZZZ",
            )

    def test_verify_signature_raises_on_skew(self) -> None:
        body = b"{}"
        timestamp = 1700000000
        delivery_id = "33333333-3333-3333-3333-333333333333"
        digest = compute_signature(SECRET, timestamp, delivery_id, body)
        verified = parse_headers(
            event_header="task.status_changed",
            delivery_header=delivery_id,
            timestamp_header=str(timestamp),
            signature_header=f"sha256={digest}",
        )
        with self.assertRaises(WebhookTimestampError):
            verify_signature(
                secret=SECRET,
                body=body,
                headers=verified,
                now=timestamp + DEFAULT_TIMESTAMP_SKEW_SECONDS + 1,
            )

    def test_verify_signature_raises_on_bad_digest(self) -> None:
        body = b"{}"
        timestamp = 1700000000
        delivery_id = "44444444-4444-4444-4444-444444444444"
        verified = parse_headers(
            event_header="task.status_changed",
            delivery_header=delivery_id,
            timestamp_header=str(timestamp),
            signature_header="sha256=" + "f" * 64,
        )
        with self.assertRaises(WebhookSignatureError):
            verify_signature(
                secret=SECRET,
                body=body,
                headers=verified,
                now=timestamp,
            )


class WebhookRouteIntegrationTests(unittest.TestCase):
    """Drive the FastAPI route end to end with TestClient."""

    def _build_client(self, db_path: Path) -> TestClient:
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
            allow_unauthenticated_local=False,
            db_path=db_path,
            webhook_secret_provider=StaticWebhookSecretProvider(SECRET),
            include_taskboard_gateway=False,
        )
        return TestClient(app)

    def test_happy_path_returns_202_and_persists_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                body, headers, delivery_id = _build_signed_request()
                response = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers,
                    content=body,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(),
                    {"status": "accepted", "delivery_id": delivery_id},
                )

            conn = connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT delivery_id, event_type, hmac_status, dispatch_status,"
                    " duplicate_count, attempts FROM webhook_deliveries"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["delivery_id"], delivery_id)
            self.assertEqual(row["event_type"], "task.status_changed")
            self.assertEqual(row["hmac_status"], "verified")
            self.assertEqual(row["dispatch_status"], "accepted")
            self.assertEqual(row["duplicate_count"], 0)
            self.assertEqual(row["attempts"], 0)

    def test_bad_hmac_returns_401_and_does_not_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                body, headers, _ = _build_signed_request()
                headers["X-Taskboard-Signature"] = "sha256=" + ("a" * 64)
                response = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers,
                    content=body,
                )
                self.assertEqual(response.status_code, 401)
                self.assertIn("invalid", response.json()["detail"].lower())

            conn = connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM webhook_deliveries"
                ).fetchone()["c"]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_timestamp_skew_returns_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                stale = int(time.time()) - (DEFAULT_TIMESTAMP_SKEW_SECONDS + 60)
                body, headers, _ = _build_signed_request(timestamp=stale)
                response = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers,
                    content=body,
                )
                self.assertEqual(response.status_code, 401)
                self.assertIn("skew", response.json()["detail"].lower())

            conn = connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM webhook_deliveries"
                ).fetchone()["c"]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_replay_same_delivery_id_returns_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                body, headers, delivery_id = _build_signed_request()
                first = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers,
                    content=body,
                )
                self.assertEqual(first.status_code, 200)
                second = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers,
                    content=body,
                )
                self.assertEqual(second.status_code, 409)
                self.assertIn("delivery_id", second.json()["detail"])

            conn = connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT delivery_id, duplicate_count FROM webhook_deliveries"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["delivery_id"], delivery_id)
            self.assertEqual(rows[0]["duplicate_count"], 1)

    def test_malformed_json_returns_422(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                body = b"this is not json"
                timestamp = int(time.time())
                delivery_id = str(uuid.uuid4())
                digest = compute_signature(SECRET, timestamp, delivery_id, body)
                headers = {
                    "X-Taskboard-Event": "task.status_changed",
                    "X-Taskboard-Delivery": delivery_id,
                    "X-Taskboard-Timestamp": str(timestamp),
                    "X-Taskboard-Signature": f"sha256={digest}",
                    "Content-Type": "application/json",
                }
                response = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers,
                    content=body,
                )
                self.assertEqual(response.status_code, 422)

            conn = connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM webhook_deliveries"
                ).fetchone()["c"]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_missing_headers_return_422(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                response = client.post(
                    "/api/webhooks/taskboard",
                    content=b"{}",
                )
                self.assertEqual(response.status_code, 422)

    def test_bearer_middleware_is_bypassed_for_this_route(self) -> None:
        """Request without a daemon bearer token must reach the HMAC check.

        The daemon's other routes return ``daemon bearer token required``
        on 401 when ``allow_unauthenticated_local`` is disabled. The
        webhook route must instead reach HMAC verification, so the same
        request body posted with a *valid* signature should succeed.
        """

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                # Sanity-check: the daemon bearer guard is active for siblings.
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 401)
                self.assertIn(
                    "bearer",
                    health.json()["detail"].lower(),
                )

                # Same client, no Authorization header: webhook still works.
                body, headers, _ = _build_signed_request()
                accepted = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers,
                    content=body,
                )
                self.assertEqual(accepted.status_code, 200)

    def test_event_id_collision_returns_409(self) -> None:
        """Different delivery_id but same event_id is treated as a replay."""

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            with self._build_client(db_path) as client:
                shared_event_id = str(uuid.uuid4())
                body = json.dumps(
                    {"event_id": shared_event_id, "task": {"id": 1}}
                ).encode("utf-8")
                _, headers_a, _ = _build_signed_request(body=body)
                first = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers_a,
                    content=body,
                )
                self.assertEqual(first.status_code, 200)

                _, headers_b, _ = _build_signed_request(body=body)
                second = client.post(
                    "/api/webhooks/taskboard",
                    headers=headers_b,
                    content=body,
                )
                self.assertEqual(second.status_code, 409)


class WebhookMigrationTests(unittest.TestCase):
    """Validate the migration runner is idempotent and creates the table."""

    def test_first_apply_creates_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            applied = apply_migrations(db_path)
            self.assertIn(1, applied)
            conn = connect(db_path)
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type='table' AND name='webhook_deliveries'"
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)

    def test_second_apply_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            apply_migrations(db_path)
            applied_again = apply_migrations(db_path)
            self.assertEqual(applied_again, [])

    def test_taskboard_session_progress_migration_resumes_after_column_added(
        self,
    ) -> None:
        """Migration 006 tolerates a pre-existing progress column."""

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            apply_migrations(db_path)
            conn = connect(db_path)
            try:
                conn.execute("DELETE FROM schema_migrations WHERE version = 6")
                conn.execute("DROP INDEX IF EXISTS idx_sessions_dispatcher_progress")
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, taskboard_task_id, fire_generation, agent_id,
                        source, status, created_at, updated_at, last_progress_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        "partial-006-session",
                        10446,
                        2,
                        "developer",
                        "taskboard_dispatcher",
                        "running",
                        "2026-05-16T10:00:00Z",
                        "2026-05-16T10:05:00Z",
                    ),
                )
            finally:
                conn.close()

            applied = apply_migrations(db_path)

            self.assertEqual(applied, [6])
            conn = connect(db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM schema_migrations WHERE version = 6"
                ).fetchone()
                self.assertEqual(row["count"], 1)
                progress = conn.execute(
                    "SELECT last_progress_at FROM sessions WHERE session_id = ?",
                    ("partial-006-session",),
                ).fetchone()
                self.assertEqual(progress["last_progress_at"], "2026-05-16T10:05:00Z")
                index_row = conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'index'
                      AND name = 'idx_sessions_dispatcher_progress'
                    """
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(index_row)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for the Phase 1 Forgejo webhook ingress route."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from daemon.db import connect
from daemon.forgejo_webhook_auth import SIGNATURE_PREFIX, compute_signature
from daemon.secrets import StaticWebhookSecretProvider
from daemon.server import create_app

SECRET = b"forgejo-unit-test-secret"


class _FakeBus:
    """Minimal async bus stub for daemon lifecycle tests."""

    def __init__(self, url: str, agent_name: str) -> None:
        self.url = url
        self.agent_name = agent_name

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def subscribe(self, *_args: object, **_kwargs: object) -> None:
        return None


def _pull_request_payload(
    *,
    action: str = "opened",
    repo: str = "agent-kai-bot/agent-kai",
    pr_number: int = 17,
    head_sha: str = "a" * 40,
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": pr_number,
            "head": {"sha": head_sha, "ref": "feature-branch"},
            "title": "Add Forgejo webhook ingress",
            "body": "Webhook test payload",
        },
    }


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signed_headers(
    body: bytes,
    *,
    delivery_id: str | None = None,
    event_type: str = "pull_request",
    secret: bytes = SECRET,
) -> tuple[dict[str, str], str]:
    delivery = delivery_id or str(uuid.uuid4())
    digest = compute_signature(secret, body)
    return (
        {
            "X-Forgejo-Event": event_type,
            "X-Forgejo-Delivery": delivery,
            "X-Forgejo-Signature": f"{SIGNATURE_PREFIX}{digest}",
            "Content-Type": "application/json",
        },
        delivery,
    )


def _table_count(db_path: Path, table: str) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    finally:
        conn.close()
    return int(row["c"])


class ForgejoWebhookIngressTests(unittest.TestCase):
    """Drive the Forgejo webhook route end to end with TestClient."""

    def _build_client(self, db_path: Path) -> TestClient:
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
            token_path=db_path.parent / "daemon-token.txt",
            allow_unauthenticated_local=False,
            include_taskboard_gateway=False,
            db_path=db_path,
            forgejo_webhook_secret_provider=StaticWebhookSecretProvider(SECRET),
        )
        return TestClient(app)

    def test_happy_path_persists_delivery_and_pending_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            body = _json_body(_pull_request_payload())
            headers, delivery_id = _signed_headers(body)

            with self._build_client(db_path) as client:
                response = client.post(
                    "/api/webhooks/forgejo",
                    headers=headers,
                    content=body,
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {
                    "status": "accepted",
                    "delivery_id": delivery_id,
                    "event_type": "pull_request",
                },
            )
            conn = connect(db_path)
            try:
                delivery = conn.execute(
                    "SELECT delivery_id, event_type, action, repo, pr_number,"
                    " head_sha, hmac_status, dispatch_status, duplicate_count"
                    " FROM forgejo_deliveries"
                ).fetchone()
                pending = conn.execute(
                    "SELECT delivery_id, status, repo, pr_number, head_sha"
                    " FROM forgejo_pending"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(delivery["delivery_id"], delivery_id)
            self.assertEqual(delivery["event_type"], "pull_request")
            self.assertEqual(delivery["action"], "opened")
            self.assertEqual(delivery["repo"], "agent-kai-bot/agent-kai")
            self.assertEqual(delivery["pr_number"], 17)
            self.assertEqual(delivery["head_sha"], "a" * 40)
            self.assertEqual(delivery["hmac_status"], "verified")
            self.assertEqual(delivery["dispatch_status"], "pending")
            self.assertEqual(delivery["duplicate_count"], 0)
            self.assertEqual(pending["delivery_id"], delivery_id)
            self.assertEqual(pending["status"], "pending")

    def test_bad_hmac_returns_401_and_does_not_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            body = _json_body(_pull_request_payload())
            headers, _ = _signed_headers(body)
            headers["X-Forgejo-Signature"] = f"{SIGNATURE_PREFIX}{'f' * 64}"

            with self._build_client(db_path) as client:
                response = client.post(
                    "/api/webhooks/forgejo",
                    headers=headers,
                    content=body,
                )

            self.assertEqual(response.status_code, 401)
            self.assertIn("invalid", response.json()["detail"].lower())
            self.assertEqual(_table_count(db_path, "forgejo_deliveries"), 0)
            self.assertEqual(_table_count(db_path, "forgejo_pending"), 0)

    def test_replay_same_delivery_id_within_window_returns_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            body = _json_body(_pull_request_payload())
            headers, delivery_id = _signed_headers(body)

            with self._build_client(db_path) as client:
                first = client.post(
                    "/api/webhooks/forgejo",
                    headers=headers,
                    content=body,
                )
                second = client.post(
                    "/api/webhooks/forgejo",
                    headers=headers,
                    content=body,
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 409)
            self.assertIn("delivery_id", second.json()["detail"])
            conn = connect(db_path)
            try:
                row = conn.execute(
                    "SELECT delivery_id, duplicate_count FROM forgejo_deliveries"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["delivery_id"], delivery_id)
            self.assertEqual(row["duplicate_count"], 1)
            self.assertEqual(_table_count(db_path, "forgejo_pending"), 1)

    def test_malformed_json_returns_422_and_does_not_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            body = b"not-json"
            headers, _ = _signed_headers(body)

            with self._build_client(db_path) as client:
                response = client.post(
                    "/api/webhooks/forgejo",
                    headers=headers,
                    content=body,
                )

            self.assertEqual(response.status_code, 422)
            self.assertIn("json", response.json()["detail"].lower())
            self.assertEqual(_table_count(db_path, "forgejo_deliveries"), 0)
            self.assertEqual(_table_count(db_path, "forgejo_pending"), 0)

    def test_missing_headers_return_422_and_do_not_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"

            with self._build_client(db_path) as client:
                response = client.post(
                    "/api/webhooks/forgejo",
                    content=b"{}",
                )

            self.assertEqual(response.status_code, 422)
            self.assertIn("header", response.json()["detail"].lower())
            self.assertEqual(_table_count(db_path, "forgejo_deliveries"), 0)
            self.assertEqual(_table_count(db_path, "forgejo_pending"), 0)

    def test_non_pull_request_event_returns_422_and_does_not_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            body = _json_body({"ref": "refs/heads/main", "after": "b" * 40})
            headers, _ = _signed_headers(body, event_type="push")

            with self._build_client(db_path) as client:
                response = client.post(
                    "/api/webhooks/forgejo",
                    headers=headers,
                    content=body,
                )

            self.assertEqual(response.status_code, 422)
            self.assertIn("unsupported", response.json()["detail"].lower())
            self.assertEqual(_table_count(db_path, "forgejo_deliveries"), 0)
            self.assertEqual(_table_count(db_path, "forgejo_pending"), 0)

    def test_bearer_auth_is_bypassed_for_forgejo_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "daemon-state.sqlite3"
            body = _json_body(_pull_request_payload())
            headers, _ = _signed_headers(body)

            with self._build_client(db_path) as client:
                health = client.get("/api/health")
                accepted = client.post(
                    "/api/webhooks/forgejo",
                    headers=headers,
                    content=body,
                )

            self.assertEqual(health.status_code, 401)
            self.assertIn("bearer", health.json()["detail"].lower())
            self.assertEqual(accepted.status_code, 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

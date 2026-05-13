"""Tests for the raw taskboard service client."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from agent.taskboard_service_client import (
    TaskboardServiceClient,
    TaskboardServiceError,
)


_NO_JSON = object()


class _FakeResponse:
    """Minimal requests response double."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: object = _NO_JSON,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        """Return the configured JSON payload or raise like requests."""

        if self._payload is _NO_JSON:
            raise ValueError("no json")
        return self._payload


class TaskboardServiceClientTests(unittest.TestCase):
    """Validate raw taskboard request handling."""

    @mock.patch("agent.taskboard_service_client.requests.request")
    def test_fetch_task_returns_full_payload_for_large_response(self, request_mock) -> None:
        """Large task responses are returned intact without tool truncation."""

        large_comment = "x" * 40_000
        task = {
            "id": 10413,
            "title": "Large task",
            "agent": "Developer",
            "comments": [{"content": large_comment}],
        }
        request_mock.return_value = _FakeResponse(payload=task)
        client = TaskboardServiceClient(
            "http://taskboard.local",
            bearer_token="bearer-secret",
        )

        result = client.fetch_task(10413)

        self.assertEqual(result["comments"][0]["content"], large_comment)
        self.assertGreater(len(json.dumps(result)), 40_000)
        self.assertNotIn("body_preview", result)
        request_mock.assert_called_once()
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://taskboard.local/api/tasks/10413")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer bearer-secret")
        self.assertEqual(kwargs["timeout"], 20)

    @mock.patch("agent.taskboard_service_client.requests.request")
    def test_fetch_task_raises_on_non_2xx(self, request_mock) -> None:
        """Non-success HTTP responses raise a typed, redacted error."""

        request_mock.return_value = _FakeResponse(
            status_code=404,
            payload={"error": "missing bearer-secret task"},
        )
        client = TaskboardServiceClient(
            "http://taskboard.local",
            bearer_token="bearer-secret",
        )

        with self.assertRaises(TaskboardServiceError) as caught:
            client.fetch_task(10413)

        exc = caught.exception
        self.assertEqual(exc.status_code, 404)
        self.assertNotIn("bearer-secret", str(exc))
        self.assertIn("[REDACTED]", str(exc))
        self.assertNotIn("bearer-secret", str(exc.body))

    @mock.patch("agent.taskboard_service_client.requests.request")
    def test_post_audit_comment_request_shape(self, request_mock) -> None:
        """Audit comments match the live taskboard comment endpoint shape."""

        request_mock.return_value = _FakeResponse(
            status_code=201,
            payload={"id": 55, "content": "[System] spawned"},
        )
        client = TaskboardServiceClient(
            "http://taskboard.local/",
            bearer_token="bearer-secret",
            timeout_seconds=9,
        )

        result = client.post_audit_comment(10413, "[System] spawned")

        self.assertEqual(result["id"], 55)
        request_mock.assert_called_once()
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["url"], "http://taskboard.local/api/tasks/10413/comments")
        self.assertEqual(kwargs["params"], {})
        self.assertEqual(kwargs["json"], {"agent": "System", "content": "[System] spawned"})
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer bearer-secret")
        self.assertEqual(kwargs["timeout"], 9)

    @mock.patch("agent.taskboard_service_client.requests.request")
    def test_redacts_bearer_in_errors(self, request_mock) -> None:
        """Bearer tokens echoed in error text are masked before raising."""

        request_mock.return_value = _FakeResponse(
            status_code=500,
            text="upstream echoed bearer-secret in diagnostics",
        )
        client = TaskboardServiceClient(
            "http://taskboard.local",
            bearer_token="bearer-secret",
        )

        with self.assertRaises(TaskboardServiceError) as caught:
            client.fetch_task(10413)

        self.assertNotIn("bearer-secret", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))
        self.assertEqual(
            caught.exception.body,
            "upstream echoed [REDACTED] in diagnostics",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

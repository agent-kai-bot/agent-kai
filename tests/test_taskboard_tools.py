"""Tests for typed taskboard lifecycle tools."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from agent.taskboard_tools import (
    TaskboardClient,
    TaskboardContext,
    create_taskboard_tools,
)


class _FakeResponse:
    """Minimal requests response double.

    Args:
        status_code: HTTP status code.
        payload: JSON payload returned by ``json``.
        text: Text payload used when JSON is unavailable.
    """

    def __init__(
        self,
        status_code: int = 200,
        payload: dict | list | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        """Return the configured JSON payload.

        Returns:
            Configured JSON-compatible object.

        Raises:
            ValueError: If no JSON payload was configured.
        """

        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TaskboardToolsTests(unittest.TestCase):
    """Validate taskboard tool request shaping and redaction."""

    def setUp(self) -> None:
        """Create a client with deterministic tokens."""

        self.context = TaskboardContext(
            base_url="http://taskboard.local",
            bearer_token="bearer-secret",
            session_token="session-secret",
            session_generation=7,
            agent_name="Developer",
        )
        self.client = TaskboardClient(self.context)

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_comment_posts_with_auth_and_session_params(self, request_mock) -> None:
        """Comment tool sends bearer auth and session query parameters."""

        request_mock.return_value = _FakeResponse(
            payload={"id": 10, "token_echo": "session-secret"}
        )

        result = self.client.comment(
            123,
            "Developer",
            "done",
        )

        request_mock.assert_called_once()
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["url"], "http://taskboard.local/api/tasks/123/comments")
        self.assertEqual(kwargs["params"], {"token": "session-secret", "generation": 7})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer bearer-secret")
        self.assertEqual(kwargs["json"], {"agent": "Developer", "content": "done"})

        payload = json.loads(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["body"]["token_echo"], "[REDACTED]")

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_move_shapes_force_review_params(self, request_mock) -> None:
        """Move tool sends canonical SPEC v23 statuses and review flags unchanged."""

        request_mock.return_value = _FakeResponse(payload={"status": "moved"})

        result = self.client.move(
            123,
            "Code Review",
            reason="ready",
            force_code_review=True,
            force_security_audit=False,
        )

        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["params"]["status"], "Code Review")
        self.assertEqual(kwargs["params"]["reason"], "ready")
        self.assertEqual(kwargs["params"]["agent"], "Developer")
        self.assertEqual(kwargs["params"]["force_code_review"], "true")
        self.assertEqual(kwargs["params"]["force_security_audit"], "false")
        self.assertTrue(json.loads(result)["ok"])

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_move_accepts_all_spec_v23_canonical_statuses(self, request_mock) -> None:
        """Move tool forwards canonical staged-review statuses without remapping."""

        request_mock.return_value = _FakeResponse(payload={"status": "moved"})

        for status_name in (
            "Backlog",
            "In Progress",
            "Code Review",
            "Security Audit",
            "QA",
            "Ready to Merge",
            "Fixing",
            "Done",
        ):
            with self.subTest(status=status_name):
                self.client.move(123, status_name)
                _, kwargs = request_mock.call_args
                self.assertEqual(kwargs["params"]["status"], status_name)

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_request_exception_is_returned_as_redacted_json(self, request_mock) -> None:
        """Network errors return structured non-throwing tool output."""

        request_mock.side_effect = requests.Timeout("bearer-secret timed out")

        result = self.client.get_task(123)

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("[REDACTED]", payload["error"])
        self.assertNotIn("bearer-secret", result)

    def test_create_taskboard_tools_includes_lifecycle_tool_names(self) -> None:
        """Factory returns all expected taskboard tools."""

        tools = create_taskboard_tools(self.context)
        names = {tool.name for tool in tools}

        self.assertIn("taskboard_get_task", names)
        self.assertIn("taskboard_comment", names)
        self.assertIn("taskboard_move", names)
        self.assertIn("taskboard_stop_work", names)
        self.assertIn("taskboard_create_action_item", names)


if __name__ == "__main__":
    unittest.main()

"""Tests for typed taskboard lifecycle tools."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import requests

from agent.taskboard_tools import (
    REVIEWER_USER_BY_TYPE,
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
            task_id=123,
        )
        self.client = TaskboardClient(self.context)

    def _task_with_reviews(self, reviews: list[dict]) -> dict:
        """Build a minimal task payload with review rows."""

        return {"id": self.context.task_id, "reviews": reviews}

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

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_submit_review_verdict_posts_approve_payload(self, request_mock) -> None:
        """Structured verdict tool posts to the resolved review verdict URL."""

        request_mock.side_effect = [
            _FakeResponse(
                payload=self._task_with_reviews(
                    [
                        {
                            "id": 451,
                            "review_type": "code",
                            "status": "pending",
                            "cycle": 2,
                            "sequence": 1,
                        }
                    ]
                )
            ),
            _FakeResponse(payload={"status": "submitted"}),
        ]

        result = self.client.submit_review_verdict(
            "code",
            "APPROVE",
            "No blocking findings.",
        )

        self.assertEqual(request_mock.call_count, 2)
        _, kwargs = request_mock.call_args_list[1]
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(
            kwargs["url"],
            "http://taskboard.local/api/tasks/123/reviews/451/verdict",
        )
        self.assertEqual(kwargs["params"], {"token": "session-secret", "generation": 7})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer bearer-secret")
        self.assertEqual(
            kwargs["json"],
            {
                "gate_type": "code",
                "verdict": "APPROVE",
                "reviewer_user": REVIEWER_USER_BY_TYPE["code"],
                "evidence_url": None,
                "findings_summary_path": None,
            },
        )
        self.assertTrue(json.loads(result)["ok"])

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_submit_review_verdict_posts_request_changes_payload(self, request_mock) -> None:
        """Structured verdict tool sends gate_type and reviewer_user body shape."""

        request_mock.side_effect = [
            _FakeResponse(
                payload=self._task_with_reviews(
                    [
                        {
                            "id": 452,
                            "review_type": "qa",
                            "status": "pending",
                            "cycle": 1,
                            "sequence": 1,
                        }
                    ]
                )
            ),
            _FakeResponse(payload={"status": "submitted"}),
        ]

        result = self.client.submit_review_verdict(
            "qa",
            "REQUEST_CHANGES",
            "Regression observed in the focused test path.",
            evidence_url="https://example.test/evidence",
        )

        _, kwargs = request_mock.call_args_list[1]
        body = kwargs["json"]
        self.assertEqual(body["gate_type"], "qa")
        self.assertEqual(body["verdict"], "REQUEST_CHANGES")
        self.assertEqual(body["reviewer_user"], REVIEWER_USER_BY_TYPE["qa"])
        self.assertEqual(body["evidence_url"], "https://example.test/evidence")
        self.assertIsNone(body["findings_summary_path"])
        self.assertNotIn("review_type", body)
        self.assertNotIn("summary_md", body)
        self.assertTrue(json.loads(result)["ok"])

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_submit_review_verdict_resolves_highest_cycle_lowest_sequence(
        self,
        request_mock,
    ) -> None:
        """Review id resolution prefers highest cycle, then smallest sequence."""

        request_mock.side_effect = [
            _FakeResponse(
                payload=self._task_with_reviews(
                    [
                        {
                            "id": 520,
                            "review_type": "code",
                            "status": "pending",
                            "cycle": 2,
                            "sequence": 1,
                        },
                        {
                            "id": 521,
                            "review_type": "code",
                            "status": "pending",
                            "cycle": 2,
                            "sequence": 2,
                        },
                        {
                            "id": 522,
                            "review_type": "security",
                            "status": "pending",
                            "cycle": 2,
                            "sequence": 1,
                        },
                        {
                            "id": 620,
                            "review_type": "code",
                            "status": "pending",
                            "cycle": 3,
                            "sequence": 3,
                        },
                        {
                            "id": 621,
                            "review_type": "code",
                            "status": "pending",
                            "cycle": 3,
                            "sequence": 1,
                        },
                        {
                            "id": 622,
                            "review_type": "security",
                            "status": "pending",
                            "cycle": 3,
                            "sequence": 1,
                        },
                    ]
                )
            ),
            _FakeResponse(payload={"status": "submitted"}),
        ]

        self.client.submit_review_verdict(
            "code",
            "APPROVE",
            "Cycle three code review passes.",
        )

        _, kwargs = request_mock.call_args_list[1]
        self.assertEqual(
            kwargs["url"],
            "http://taskboard.local/api/tasks/123/reviews/621/verdict",
        )

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_submit_review_verdict_raises_when_no_pending_review(
        self,
        request_mock,
    ) -> None:
        """Submitted review rows are not eligible for verdict submission."""

        request_mock.return_value = _FakeResponse(
            payload=self._task_with_reviews(
                [
                    {
                        "id": 701,
                        "review_type": "security",
                        "status": "approved",
                        "cycle": 3,
                        "sequence": 1,
                    },
                    {
                        "id": 702,
                        "review_type": "security",
                        "status": "changes_requested",
                        "cycle": 2,
                        "sequence": 1,
                    },
                ]
            )
        )

        with self.assertRaisesRegex(ValueError, "no pending security review"):
            self.client.submit_review_verdict(
                "security",
                "APPROVE",
                "No pending review remains.",
            )

        request_mock.assert_called_once()
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["method"], "GET")

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_submit_review_verdict_redacts_request_errors(self, request_mock) -> None:
        """Verdict transport errors redact bearer and session tokens."""

        request_mock.side_effect = [
            _FakeResponse(
                payload=self._task_with_reviews(
                    [
                        {
                            "id": 453,
                            "review_type": "security",
                            "status": "pending",
                            "cycle": 1,
                            "sequence": 1,
                        }
                    ]
                )
            ),
            requests.Timeout("bearer-secret session-secret timed out"),
        ]

        result = self.client.submit_review_verdict(
            "security",
            "APPROVE",
            "No security findings.",
        )

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("[REDACTED]", payload["error"])
        self.assertNotIn("bearer-secret", result)
        self.assertNotIn("session-secret", result)

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_submit_review_verdict_rejects_invalid_args(self, request_mock) -> None:
        """Invalid review types, verdicts, summaries, and task binding raise."""

        with self.assertRaises(ValueError):
            self.client.submit_review_verdict("architecture", "APPROVE", "summary")
        with self.assertRaises(ValueError):
            self.client.submit_review_verdict("code", "APPROVED", "summary")
        with self.assertRaises(ValueError):
            self.client.submit_review_verdict("code", "APPROVE", " ")
        with self.assertRaises(ValueError):
            TaskboardClient(replace(self.context, task_id=None)).submit_review_verdict(
                "code",
                "APPROVE",
                "summary",
            )
        request_mock.assert_not_called()

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_get_task_still_truncates_envelope_for_large_response(self, request_mock) -> None:
        """LLM tool responses keep the 20K preview envelope for large bodies."""

        request_mock.return_value = _FakeResponse(
            payload={
                "id": 10413,
                "comments": [{"content": "x" * 40_000}],
            }
        )

        result = self.client.get_task(10413)

        envelope = json.loads(result)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["status_code"], 200)
        self.assertTrue(envelope["truncated"])
        self.assertIn("body_preview", envelope)
        self.assertNotIn("body", envelope)

    def test_create_taskboard_tools_includes_lifecycle_tool_names(self) -> None:
        """Factory returns all expected taskboard tools."""

        tools = create_taskboard_tools(self.context)
        names = {tool.name for tool in tools}

        self.assertIn("taskboard_get_task", names)
        self.assertIn("taskboard_comment", names)
        self.assertIn("taskboard_move", names)
        self.assertIn("taskboard_stop_work", names)
        self.assertIn("taskboard_create_action_item", names)

    def test_create_taskboard_tools_role_gates_submit_review_verdict(self) -> None:
        """Only review roles receive the structured verdict tool."""

        developer_names = {
            tool.name
            for tool in create_taskboard_tools(
                replace(self.context, agent_name="developer")
            )
        }
        self.assertNotIn("taskboard_submit_review_verdict", developer_names)

        for agent_name in ("code-reviewer", "security-auditor", "qa-agent"):
            with self.subTest(agent_name=agent_name):
                review_names = {
                    tool.name
                    for tool in create_taskboard_tools(
                        replace(self.context, agent_name=agent_name)
                    )
                }
                self.assertIn("taskboard_submit_review_verdict", review_names)

    def test_standard_toolset_role_gates_submit_review_verdict(self) -> None:
        """A normal session toolset exposes verdict submit only to review roles."""

        from agent.tools import create_tools

        cr_session = SimpleNamespace(
            taskboard_context=replace(self.context, agent_name="code-reviewer")
        )
        developer_session = SimpleNamespace(
            taskboard_context=replace(self.context, agent_name="developer")
        )

        cr_names = {tool.name for tool in create_tools(session=cr_session)}
        developer_names = {tool.name for tool in create_tools(session=developer_session)}

        self.assertIn("taskboard_submit_review_verdict", cr_names)
        self.assertNotIn("taskboard_submit_review_verdict", developer_names)

    @mock.patch("agent.taskboard_tools.requests.request")
    def test_explicit_taskboard_context_wins_over_process_env(self, request_mock) -> None:
        """Session TaskboardContext is used before process env fallback."""

        request_mock.return_value = _FakeResponse(payload={"id": 123})
        with mock.patch.dict(
            "os.environ",
            {
                "TASKBOARD_URL": "http://env-taskboard.local",
                "TASKBOARD_BEARER_TOKEN": "env-bearer",
                "TASKBOARD_SESSION_TOKEN": "env-session",
                "TASKBOARD_SESSION_GENERATION": "99",
            },
            clear=False,
        ):
            tools = create_taskboard_tools(self.context)
            get_task_tool = next(tool for tool in tools if tool.name == "taskboard_get_task")
            get_task_tool.func(123)

        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["url"], "http://taskboard.local/api/tasks/123")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer bearer-secret")


if __name__ == "__main__":
    unittest.main()

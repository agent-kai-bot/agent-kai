"""Tests for guarded Git and Forgejo tools."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from agent.forgejo_tools import (
    ForgejoContext,
    create_forgejo_tools,
    forgejo_create_pr,
    forgejo_find_pr_for_branch,
    forgejo_submit_review,
    git_push_branch,
    git_status,
)


class _FakeResponse:
    """Minimal HTTP response double.

    Args:
        status_code: HTTP status code.
        payload: JSON payload returned by ``json``.
        text: Text body used when payload is absent.
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
        """Return configured payload or raise when missing.

        Returns:
            JSON-compatible payload.

        Raises:
            ValueError: If no payload was configured.
        """

        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class ForgejoToolsTests(unittest.TestCase):
    """Validate guarded Git and Forgejo helper behavior."""

    def setUp(self) -> None:
        """Create an isolated task run root."""

        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            "os.environ",
            {
                "TASKBOARD_RUN_ROOT": self.temp_dir.name,
                "FORGEJO_URL": "http://forgejo.local",
                "FORGEJO_TOKEN": "forgejo-secret",
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        """Clean up environment patches and temporary files."""

        self.env.stop()
        self.temp_dir.cleanup()

    @mock.patch("agent.forgejo_tools.subprocess.run")
    def test_git_status_rejects_path_outside_run_root(self, run_mock) -> None:
        """Git commands refuse paths outside the configured run root."""

        result = git_status("/tmp/not-this-root")

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("outside task run root", payload["error"])
        run_mock.assert_not_called()

    def test_git_push_rejects_protected_branch(self) -> None:
        """Git push refuses direct protected branch pushes."""

        workspace = Path(self.temp_dir.name) / "task-1"
        workspace.mkdir()

        result = git_push_branch(str(workspace), "main")

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("protected branch", payload["error"])

    @mock.patch("agent.forgejo_tools.requests.request")
    def test_forgejo_create_pr_sends_token_and_body(self, request_mock) -> None:
        """Forgejo PR creation sends the expected request shape."""

        request_mock.return_value = _FakeResponse(
            status_code=201,
            payload={"url": "http://forgejo.local/pr/1"},
        )

        result = forgejo_create_pr(
            "owner",
            "repo",
            "Task PR",
            "feat/task",
            base="main",
            body="body",
        )

        request_mock.assert_called_once()
        args, kwargs = request_mock.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(
            args[1],
            "http://forgejo.local/api/v1/repos/owner/repo/pulls",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "token forgejo-secret")
        self.assertEqual(kwargs["json"]["head"], "feat/task")
        self.assertTrue(json.loads(result)["ok"])

    @mock.patch("agent.forgejo_tools.requests.request")
    def test_context_token_wins_for_create_pr_and_review(self, request_mock) -> None:
        """Session ForgejoContext token beats role and global env fallbacks."""

        request_mock.return_value = _FakeResponse(status_code=201, payload={"ok": True})
        with mock.patch.dict(
            "os.environ",
            {
                "FORGEJO_TOKEN_DEVELOPER": "role-env-secret",
                "FORGEJO_TOKEN": "global-secret",
            },
            clear=False,
        ):
            tools = create_forgejo_tools(
                ForgejoContext(
                    role="developer",
                    token="context-secret",
                    base_url="http://forgejo.local",
                )
            )
            create_pr_tool = next(tool for tool in tools if tool.name == "forgejo_create_pr")
            submit_review_tool = next(
                tool for tool in tools if tool.name == "forgejo_submit_review"
            )

            create_pr_tool.func("owner", "repo", "Task PR", "feat/task")
            submit_review_tool.func("owner", "repo", 1, "APPROVED", "ok")

        headers = [call.kwargs["headers"]["Authorization"] for call in request_mock.call_args_list]
        self.assertEqual(headers, ["token context-secret", "token context-secret"])

    @mock.patch("agent.forgejo_tools.requests.request")
    def test_role_env_token_wins_over_global_when_context_has_role(self, request_mock) -> None:
        """Role-scoped env token is preferred over legacy global env token."""

        request_mock.return_value = _FakeResponse(status_code=201, payload={"ok": True})
        with mock.patch.dict(
            "os.environ",
            {
                "FORGEJO_TOKEN_DEVELOPER": "role-env-secret",
                "FORGEJO_TOKEN": "global-secret",
            },
            clear=False,
        ):
            tools = create_forgejo_tools(
                ForgejoContext(role="developer", base_url="http://forgejo.local")
            )
            create_pr_tool = next(tool for tool in tools if tool.name == "forgejo_create_pr")
            create_pr_tool.func("owner", "repo", "Task PR", "feat/task")

        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "token role-env-secret")

    @mock.patch("agent.forgejo_tools.requests.request")
    def test_forgejo_errors_are_redacted(self, request_mock) -> None:
        """Forgejo request exceptions redact configured tokens."""

        request_mock.side_effect = requests.Timeout("forgejo-secret timed out")

        result = forgejo_submit_review(
            "owner",
            "repo",
            1,
            "APPROVED",
            "ok",
        )

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertIn("[REDACTED]", payload["error"])
        self.assertNotIn("forgejo-secret", result)

    @mock.patch("agent.forgejo_tools.requests.request")
    def test_find_pr_for_branch_filters_matches(self, request_mock) -> None:
        """PR lookup returns open pull requests matching the branch."""

        request_mock.return_value = _FakeResponse(
            payload=[
                {"number": 1, "head": {"ref": "feat/one"}},
                {"number": 2, "head": {"ref": "feat/two"}},
            ]
        )

        result = forgejo_find_pr_for_branch("owner", "repo", "feat/two")

        payload = json.loads(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["matches"][0]["number"], 2)

    def test_create_forgejo_tools_includes_expected_names(self) -> None:
        """Tool factory exposes guarded Git and Forgejo operations."""

        names = {tool.name for tool in create_forgejo_tools()}

        self.assertIn("git_prepare_task_workspace", names)
        self.assertIn("git_commit", names)
        self.assertIn("git_push_branch", names)
        self.assertIn("forgejo_create_pr", names)
        self.assertIn("forgejo_submit_review", names)


if __name__ == "__main__":
    unittest.main()

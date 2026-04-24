"""Tests for structured SDLC result helpers."""

from __future__ import annotations

import json
import unittest

from agent.sdlc_results import (
    create_sdlc_result_tools,
    forgejo_event_for_decision,
    parse_developer_completion,
    parse_review_verdict,
    render_review_comment,
)


class SDLCResultsTests(unittest.TestCase):
    """Validate structured SDLC result parsing and rendering."""

    def test_parse_review_verdict_accepts_valid_payload(self) -> None:
        """Valid review verdict JSON returns an OK payload."""

        result = parse_review_verdict(
            json.dumps(
                {
                    "decision": "CHANGES_REQUESTED",
                    "role": "Code Reviewer",
                    "summary": "Needs fixes",
                    "findings": [
                        {
                            "severity": "HIGH",
                            "category": "MUST_FIX",
                            "message": "Missing test",
                            "fix": "Add regression coverage.",
                            "file": "app.py",
                            "line": 12,
                        }
                    ],
                    "tests_reviewed": ["pytest -q"],
                    "residual_risk": "Low after fixes.",
                }
            )
        )

        payload = json.loads(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verdict"]["decision"], "CHANGES_REQUESTED")

    def test_render_review_comment_outputs_taskboard_markdown(self) -> None:
        """Structured verdict renders the canonical taskboard comment."""

        comment = render_review_comment(
            json.dumps(
                {
                    "decision": "APPROVED",
                    "role": "Security Auditor",
                    "summary": "No blocking risks.",
                    "findings": [],
                    "tests_reviewed": ["manual diff review"],
                    "residual_risk": "Token handling depends on env config.",
                }
            )
        )

        self.assertIn("[APPROVED] Security Auditor APPROVED", comment)
        self.assertIn("Decision: APPROVED", comment)
        self.assertIn("No blocking findings", comment)

    def test_forgejo_event_mapping(self) -> None:
        """Review decisions map to formal Forgejo review events."""

        approved = json.loads(forgejo_event_for_decision("APPROVED"))
        changes = json.loads(forgejo_event_for_decision("CHANGES_REQUESTED"))

        self.assertEqual(approved["event"], "APPROVED")
        self.assertEqual(changes["event"], "REQUEST_CHANGES")

    def test_parse_developer_completion_rejects_invalid_status(self) -> None:
        """Invalid developer completion statuses fail validation."""

        result = parse_developer_completion(
            json.dumps({"status": "done", "summary": "complete"})
        )

        payload = json.loads(result)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])

    def test_create_sdlc_result_tools_includes_expected_names(self) -> None:
        """Tool factory exposes structured SDLC helpers."""

        names = {tool.name for tool in create_sdlc_result_tools()}

        self.assertIn("sdlc_parse_review_verdict", names)
        self.assertIn("sdlc_render_review_comment", names)
        self.assertIn("sdlc_forgejo_event_for_decision", names)
        self.assertIn("sdlc_parse_developer_completion", names)


if __name__ == "__main__":
    unittest.main()

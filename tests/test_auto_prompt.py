"""Tests for autonomous-mode prompt helpers."""

from __future__ import annotations

import unittest

from agent.auto_prompt import build_auto_suffix, parse_auto_state


class AutoPromptTests(unittest.TestCase):
    """Validate suffix generation and AUTO_STATE parsing."""

    def test_build_auto_suffix_includes_budget_and_footer_contract(self):
        suffix = build_auto_suffix(17)

        self.assertIn("## AUTONOMOUS MODE ACTIVE", suffix)
        self.assertIn("Budget: 17 iterations remaining.", suffix)
        self.assertIn("[AUTO_STATE: done]", suffix)
        self.assertIn("[AUTO_STATE: continue]", suffix)
        self.assertIn("[AUTO_STATE: pause | reason: <why>]", suffix)

    def test_parse_auto_state_done(self):
        state, reason = parse_auto_state("Finished the task.\n[AUTO_STATE: done]")

        self.assertEqual(state, "done")
        self.assertIsNone(reason)

    def test_parse_auto_state_continue(self):
        state, reason = parse_auto_state("Need one more step.\n[AUTO_STATE: continue]")

        self.assertEqual(state, "continue")
        self.assertIsNone(reason)

    def test_parse_auto_state_pause_with_reason(self):
        state, reason = parse_auto_state(
            "Blocked on approval.\n[AUTO_STATE: pause | reason: requires approval for shell_exec]"
        )

        self.assertEqual(state, "pause")
        self.assertEqual(reason, "requires approval for shell_exec")

    def test_parse_auto_state_is_strict_about_final_footer(self):
        self.assertEqual(parse_auto_state("No footer here"), ("unknown", None))
        self.assertEqual(
            parse_auto_state("[AUTO_STATE: continue]\nextra trailing text"),
            ("unknown", None),
        )


if __name__ == "__main__":
    unittest.main()

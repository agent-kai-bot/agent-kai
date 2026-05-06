"""Tests for strict auto-response evaluator contracts."""

from __future__ import annotations

import json
import unittest

from agent.auto_evaluator import (
    AutoEvaluationDecision,
    AutoEvaluationInput,
    AutoResponseEvaluator,
    ToolCallSummary,
    parse_auto_evaluation_decision,
    render_auto_reply,
    validate_auto_evaluation_decision,
)


class AutoEvaluationDecisionParserTests(unittest.TestCase):
    def test_parse_accepts_valid_strict_json(self):
        decision = parse_auto_evaluation_decision(
            json.dumps(
                {
                    "decision": "CONTINUE",
                    "confidence": 0.91,
                    "reason": "main asked permission for read-only next step",
                    "pattern": "permission_deflection",
                    "auto_reply_template": "proceed_readonly_analysis",
                }
            )
        )

        self.assertEqual(decision.decision, "CONTINUE")
        self.assertEqual(decision.auto_reply_template, "proceed_readonly_analysis")

    def test_parse_rejects_freeform_or_malformed_output(self):
        decision = parse_auto_evaluation_decision("continue please")

        self.assertEqual(decision.decision, "STOP")
        self.assertIn("malformed", decision.reason)

    def test_parse_rejects_missing_fields_invalid_enum_and_confidence(self):
        cases = [
            {"decision": "CONTINUE", "confidence": 0.9, "reason": "x"},
            {
                "decision": "GO",
                "confidence": 0.9,
                "reason": "x",
                "pattern": "unknown",
            },
            {
                "decision": "STOP",
                "confidence": 1.5,
                "reason": "x",
                "pattern": "unknown",
            },
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(parse_auto_evaluation_decision(payload).decision, "STOP")

    def test_validate_blocks_low_confidence_invalid_template_and_readonly_mutation(self):
        low = AutoEvaluationDecision(
            "CONTINUE",
            0.2,
            "low",
            "unknown",
            "continue_next_safe_step",
        )
        self.assertEqual(
            validate_auto_evaluation_decision(low, readonly=False).reason,
            "auto evaluator confidence below threshold",
        )

        invalid = AutoEvaluationDecision(
            "CONTINUE",
            0.9,
            "invalid",
            "unknown",
            None,
        )
        self.assertEqual(
            validate_auto_evaluation_decision(invalid, readonly=False).reason,
            "auto evaluator returned invalid reply template",
        )

        non_readonly = AutoEvaluationDecision(
            "CONTINUE",
            0.9,
            "mutating template",
            "unknown",
            "finish_requested_artifact",
        )
        self.assertEqual(
            validate_auto_evaluation_decision(non_readonly, readonly=True).reason,
            "auto evaluator returned non-readonly reply template",
        )


class AutoResponseEvaluatorRuleTests(unittest.TestCase):
    def _input(self, **overrides):
        data = {
            "session_name": "alpha",
            "agent_name": "developer",
            "auto_mode": True,
            "readonly": False,
            "main_response": "Done.\n[AUTO_STATE: done]",
            "parsed_auto_state": "done",
            "parsed_auto_reason": None,
            "runtime_pause_reason": None,
            "turn_tool_calls": [ToolCallSummary("file_read", "{}")],
            "consecutive_no_tool_turns": 0,
            "repeated_final_detected": False,
            "iterations_remaining": 1,
            "elapsed_seconds": 1.0,
        }
        data.update(overrides)
        return AutoEvaluationInput(**data)

    def test_runtime_pause_reason_prevents_continuation(self):
        decision = AutoResponseEvaluator().evaluate(
            self._input(runtime_pause_reason="requires approval for shell_exec")
        )

        self.assertEqual(decision.decision, "PAUSE")
        self.assertEqual(decision.pattern, "safety_pause")

    def test_permission_deflection_gets_readonly_template(self):
        decision = AutoResponseEvaluator().evaluate(
            self._input(
                parsed_auto_state="unknown",
                main_response="I can run the existing tests next if you want.",
            )
        )

        self.assertEqual(decision.decision, "CONTINUE")
        self.assertEqual(decision.pattern, "permission_deflection")
        self.assertEqual(decision.auto_reply_template, "proceed_readonly_analysis")
        self.assertIn("read-only analysis", render_auto_reply(decision.auto_reply_template) or "")

    def test_done_with_declared_remaining_work_continues(self):
        decision = AutoResponseEvaluator().evaluate(
            self._input(
                main_response="The patch is started. Next I will update tests.\n[AUTO_STATE: done]",
                parsed_auto_state="done",
            )
        )

        self.assertEqual(decision.decision, "CONTINUE")
        self.assertEqual(decision.pattern, "declared_next_step")

    def test_normal_done_accepts_main_state(self):
        decision = AutoResponseEvaluator().evaluate(self._input())

        self.assertEqual(decision.decision, "ACCEPT_MAIN_STATE")
        self.assertEqual(decision.pattern, "main_done_accepted")


if __name__ == "__main__":
    unittest.main()

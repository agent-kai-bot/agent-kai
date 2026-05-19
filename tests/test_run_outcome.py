"""Tests for ``agent.run_outcome`` — failure-class derivation + audit comments.

The dispatcher relies on this module to turn agent-runtime events into
:class:`agent.run_outcome.RunOutcome` records that the taskboard
``agent_runs`` ledger accepts. A regression here re-introduces the silent
failure pattern that hid the recent 5-day outage, so coverage is dense.
"""

from __future__ import annotations

import unittest

from agent.run_outcome import (
    AGENT_RUN_FAILURE_CLASSES,
    AGENT_RUN_FAILURE_STATUSES,
    AGENT_RUN_STATUSES,
    AGENT_RUN_TERMINAL_STATUSES,
    RunOutcome,
    derive_outcome_from_agent_events,
    derive_outcome_from_exception,
    derive_outcome_from_duplicate,
    derive_outcome_from_manual_cancel,
    derive_outcome_from_outage_backfill,
    derive_outcome_from_preflight_failure,
    derive_outcome_from_stuck_session,
    format_terminal_comment,
    outcome_to_patch_body,
)


class RunOutcomeInvariantTests(unittest.TestCase):
    """Catch contract drift between RunOutcome fields and the closed enums."""

    def test_terminal_subset_of_statuses(self) -> None:
        self.assertTrue(AGENT_RUN_TERMINAL_STATUSES <= AGENT_RUN_STATUSES)

    def test_failure_subset_of_terminal(self) -> None:
        self.assertTrue(AGENT_RUN_FAILURE_STATUSES <= AGENT_RUN_TERMINAL_STATUSES)

    def test_run_outcome_rejects_unknown_status(self) -> None:
        with self.assertRaises(ValueError):
            RunOutcome(status="totally-fake", failure_class=None, failure_detail=None)

    def test_run_outcome_failure_requires_class(self) -> None:
        with self.assertRaises(ValueError):
            RunOutcome(status="endpoint_failed", failure_class=None, failure_detail="x")

    def test_run_outcome_non_failure_rejects_class(self) -> None:
        with self.assertRaises(ValueError):
            RunOutcome(
                status="succeeded",
                failure_class="endpoint_unreachable",
                failure_detail=None,
            )

    def test_run_outcome_unknown_failure_class_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RunOutcome(
                status="endpoint_failed",
                failure_class="speculative-class",
                failure_detail="x",
            )


class DeriveFromAgentEventsTests(unittest.TestCase):
    """Canonical event-stream → RunOutcome mappings."""

    def test_primary_endpoint_connection_error(self) -> None:
        events = [
            {"type": "error", "data": "Primary endpoint failed: Connection error."},
            {"type": "final", "data": "Error: agent returned an empty response."},
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "endpoint_failed")
        self.assertEqual(out.failure_class, "endpoint_unreachable")

    def test_primary_endpoint_unauthorized(self) -> None:
        events = [
            {"type": "error", "data": "Primary endpoint failed: 401 unauthorized"},
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "endpoint_failed")
        self.assertEqual(out.failure_class, "endpoint_unauthorized")

    def test_primary_endpoint_missing_placeholder_token(self) -> None:
        events = [
            {"type": "error", "data": "Primary endpoint failed: missing-kai-api-key was rejected"},
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.failure_class, "endpoint_unauthorized")

    def test_primary_endpoint_rate_limited(self) -> None:
        events = [
            {"type": "error", "data": "Primary endpoint failed: 429 Too Many Requests"},
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.failure_class, "endpoint_rate_limited")

    def test_primary_endpoint_timeout(self) -> None:
        events = [
            {"type": "error", "data": "Primary endpoint failed: read timeout after 60s"},
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.failure_class, "endpoint_timeout")

    def test_primary_endpoint_transport_drop(self) -> None:
        events = [
            {
                "type": "error",
                "data": (
                    "Primary endpoint failed: codex transport retry exhausted after "
                    "4 attempts: peer closed connection without sending complete "
                    "message body (incomplete chunked read)"
                ),
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "endpoint_failed")
        self.assertEqual(out.failure_class, "endpoint_transport_drop")

    def test_transport_drop_beats_malformed_footer_symptom(self) -> None:
        events = [
            {
                "type": "error",
                "data": (
                    "Primary endpoint failed: peer closed connection without sending "
                    "complete message body (incomplete chunked read)"
                ),
            },
            {"type": "final", "data": "Error: agent returned an empty response."},
            {
                "type": "auto_stopped",
                "data": {"reason": "missing or malformed AUTO_STATE footer"},
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "endpoint_failed")
        self.assertEqual(out.failure_class, "endpoint_transport_drop")

    def test_primary_endpoint_empty(self) -> None:
        events = [
            {"type": "error", "data": "Primary endpoint returned an empty response."},
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.failure_class, "endpoint_empty_response")

    def test_auto_stopped_requires_approval(self) -> None:
        events = [
            {
                "type": "auto_stopped",
                "data": {
                    "reason": "requires approval for shell_exec",
                    "iterations_remaining": 19,
                },
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "requires_approval_blocked")
        self.assertEqual(out.failure_class, "tool_approval_blocked")

    def test_auto_stopped_task_complete_is_success(self) -> None:
        """Agent's positive AUTO_STATE: done signal must map to succeeded.

        Regression: earlier versions mapped this to tool_unknown_failure,
        causing successful runs (#10240 e2e smoke) to be recorded as failures
        in the agent_runs ledger and posted as `[KAI] FAILED` audit comments.
        """
        for reason in ("task complete", "task completed", "done", "finished", "AUTO_STATE: done"):
            with self.subTest(reason=reason):
                events = [
                    {"type": "final", "data": "Task completed cleanly. AUTO_STATE: done"},
                    {"type": "auto_stopped", "data": {"reason": reason}},
                ]
                out = derive_outcome_from_agent_events(events)
                self.assertEqual(out.status, "succeeded", f"reason={reason!r}")
                self.assertIsNone(out.failure_class)

    def test_auto_stopped_iteration_budget(self) -> None:
        events = [
            {
                "type": "auto_stopped",
                "data": {
                    "reason": "exceeded iteration budget; iterations_remaining=0",
                    "iterations_remaining": 0,
                },
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "timeout")
        self.assertEqual(out.failure_class, "session_exceeded_iterations")

    def test_auto_stopped_wall_clock_budget(self) -> None:
        events = [
            {
                "type": "auto_stopped",
                "data": {
                    "reason": "wall-clock budget exceeded",
                    "elapsed_seconds": 181.234,
                },
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.failure_class, "wall_clock_budget_exceeded")
        self.assertIn("elapsed=181.2s", out.failure_detail)

    def test_final_auto_state_done_beats_late_wall_clock_sentinel(self) -> None:
        events = [
            {
                "type": "final",
                "data": "Wrote artifact.\n[AUTO_STATE: done]",
            },
            {
                "type": "auto_stopped",
                "data": {
                    "reason": "wall-clock budget exceeded",
                    "elapsed_seconds": 181.234,
                },
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "succeeded")
        self.assertIsNone(out.failure_class)

    def test_final_auto_state_done_beats_late_loop_auto_stops(self) -> None:
        for reason in (
            "loop detected: repeated tool call",
            "loop detected: consecutive no-tool turns",
            "loop detected: repeated final response",
        ):
            with self.subTest(reason=reason):
                events = [
                    {
                        "type": "final",
                        "data": "Wrote artifact.\n[AUTO_STATE: done]",
                    },
                    {"type": "auto_stopped", "data": {"reason": reason}},
                ]
                out = derive_outcome_from_agent_events(events)
                self.assertEqual(out.status, "succeeded")
                self.assertIsNone(out.failure_class)

    def test_auto_stopped_missing_auto_state_footer(self) -> None:
        events = [
            {
                "type": "auto_stopped",
                "data": {"reason": "missing or malformed AUTO_STATE footer"},
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "endpoint_failed")
        self.assertEqual(out.failure_class, "endpoint_invalid_response")

    def test_final_with_real_text_is_success(self) -> None:
        events = [{"type": "final", "data": "Verdict: APPROVED. All tests pass."}]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "succeeded")
        self.assertIsNone(out.failure_class)

    def test_final_with_empty_response_sentinel_is_failure(self) -> None:
        events = [{"type": "final", "data": "Error: agent returned an empty response."}]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "endpoint_failed")
        self.assertEqual(out.failure_class, "endpoint_empty_response")

    def test_no_terminal_event_means_stuck(self) -> None:
        events = [{"type": "tool_start", "data": {"tool": "shell_exec"}}]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "stuck_aborted")
        self.assertEqual(out.failure_class, "session_stuck_no_progress")

    def test_auto_stopped_wins_over_error(self) -> None:
        """auto_stopped is the most specific terminal signal."""
        events = [
            {"type": "error", "data": "transient blip"},
            {
                "type": "auto_stopped",
                "data": {"reason": "requires approval for file_write"},
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "requires_approval_blocked")

    def test_wrapped_event_dict_format(self) -> None:
        """Run JSON files wrap events as ``{"created_at": ..., "event": {...}}``."""
        events = [
            {
                "created_at": "2026-04-30T17:40:35Z",
                "event": {
                    "type": "error",
                    "data": "Primary endpoint failed: Connection error.",
                },
            },
            {
                "created_at": "2026-04-30T17:40:35Z",
                "event": {
                    "type": "final",
                    "data": "Error: agent returned an empty response.",
                },
            },
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.failure_class, "endpoint_unreachable")

    def test_subprocess_crash_surfaces_runtime_exception_class(self) -> None:
        out = derive_outcome_from_exception(RuntimeError("codex CLI exited with status 1"))
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.failure_class, "tool_runtime_exception")
        self.assertIn("RuntimeError", out.failure_detail)
        self.assertIn("codex CLI exited with status 1", out.failure_detail)

    def test_error_event_with_raw_exception_class_is_runtime_exception(self) -> None:
        events = [
            {"type": "error", "data": "RuntimeError: codex CLI exited with status 1: boom"},
        ]
        out = derive_outcome_from_agent_events(events)
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.failure_class, "tool_runtime_exception")
        self.assertIn("RuntimeError", out.failure_detail)


class DeriveFromOtherSourcesTests(unittest.TestCase):
    def test_preflight_config_stale(self) -> None:
        out = derive_outcome_from_preflight_failure(
            reason="config_stale",
            detail="agent-config.json sha mismatch (loaded ab12, on-disk cd34)",
        )
        self.assertEqual(out.status, "preflight_failed")
        self.assertEqual(out.failure_class, "config_stale")

    def test_preflight_unknown_reason_falls_back(self) -> None:
        out = derive_outcome_from_preflight_failure(
            reason="something-new",
            detail="speculative",
        )
        self.assertEqual(out.failure_class, "config_missing_required")

    def test_stuck_session(self) -> None:
        out = derive_outcome_from_stuck_session("no events for 30m")
        self.assertEqual(out.status, "stuck_aborted")
        self.assertEqual(out.failure_class, "session_stuck_no_progress")

    def test_manual_cancel(self) -> None:
        out = derive_outcome_from_manual_cancel("operator stopped via kaictl")
        self.assertEqual(out.status, "cancelled")
        self.assertIsNone(out.failure_class)

    def test_duplicate(self) -> None:
        out = derive_outcome_from_duplicate(
            "task=10213 generation=8 role=code-reviewer already in flight"
        )
        self.assertEqual(out.status, "duplicate_suppressed")
        self.assertIsNone(out.failure_class)

    def test_outage_backfill(self) -> None:
        out = derive_outcome_from_outage_backfill("config drift 2026-04-25 → 2026-04-30")
        self.assertEqual(out.status, "endpoint_failed")
        self.assertEqual(out.failure_class, "outage_period_silent_failure")


class FormatTerminalCommentTests(unittest.TestCase):
    def test_success_format(self) -> None:
        comment = format_terminal_comment(
            role="qa-agent",
            outcome=RunOutcome(status="succeeded", failure_class=None, failure_detail=None),
            session_id="taskboard-10213-9-qa-agent",
            fire_generation=9,
            elapsed_seconds=72.4,
        )
        self.assertTrue(comment.startswith("[KAI] COMPLETED qa-agent: ok in 72.4s"))
        self.assertIn("session=taskboard-10213-9-qa-agent", comment)
        self.assertIn("generation=9", comment)

    def test_failure_format(self) -> None:
        comment = format_terminal_comment(
            role="code-reviewer",
            outcome=RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_unreachable",
                failure_detail="Primary endpoint failed: Connection error.",
            ),
            session_id="taskboard-10213-8-code-reviewer",
            fire_generation=8,
            elapsed_seconds=1.67,
        )
        self.assertTrue(comment.startswith("[KAI] FAILED code-reviewer: endpoint_unreachable:"))
        self.assertIn("Connection error", comment)
        self.assertIn("elapsed=1.7s", comment)

    def test_duplicate_format(self) -> None:
        comment = format_terminal_comment(
            role="developer",
            outcome=RunOutcome(
                status="duplicate_suppressed",
                failure_class=None,
                failure_detail="dispatcher saw same fire_generation already in flight",
            ),
            session_id=None,
            fire_generation=4,
            elapsed_seconds=None,
        )
        self.assertTrue(comment.startswith("[KAI] DUPLICATE-SUPPRESSED developer:"))


class OutcomeToPatchBodyTests(unittest.TestCase):
    def test_success_body(self) -> None:
        body = outcome_to_patch_body(
            RunOutcome(status="succeeded", failure_class=None, failure_detail=None)
        )
        self.assertEqual(body, {"status": "succeeded"})

    def test_failure_body(self) -> None:
        body = outcome_to_patch_body(
            RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_unauthorized",
                failure_detail="401 from chatgpt.com/backend-api/codex",
            )
        )
        self.assertEqual(body["status"], "endpoint_failed")
        self.assertEqual(body["failure_class"], "endpoint_unauthorized")
        self.assertIn("401", body["failure_detail"])


class TaskboardEnumLockstepTests(unittest.TestCase):
    """Sanity check the closed enum spelling matches the expected wire format.

    The taskboard side defines its own copies of these sets in app.py. A
    test in the taskboard repo asserts Python-side equality. This test
    locks the set down on the KAI side so a typo here can't quietly drift.
    """

    def test_status_count(self) -> None:
        self.assertEqual(len(AGENT_RUN_STATUSES), 16)

    def test_failure_class_count(self) -> None:
        self.assertEqual(len(AGENT_RUN_FAILURE_CLASSES), 26)

    def test_terminal_statuses_documented(self) -> None:
        # Specifically the cases that bit us today must be terminal.
        for s in (
            "endpoint_failed",
            "requires_approval_blocked",
            "config_invalid",
            "preflight_failed",
            "stuck_aborted",
        ):
            self.assertIn(s, AGENT_RUN_TERMINAL_STATUSES)


if __name__ == "__main__":
    unittest.main()

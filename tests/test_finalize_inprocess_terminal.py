"""Tests for Phase 0 of epic #10030 (#10247) Change 3.

Covers `_finalize_dispatcher_inprocess_run`: the asyncio task done-callback
the dispatcher uses to walk the agent_runs ledger row from `spawning` →
`running` → terminal for in-process runs (CR/SA/QA fan-out path that
doesn't go through the gateway run-JSON reaper).

Each test wires a stub AgentRunsClient + a finished/cancelled/failed
asyncio.Task so we can assert the PATCH body the dispatcher writes for
every terminal class the run_outcome derivation produces.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from agent import taskboard_dispatcher as td


@dataclass
class _StubInputRunResult:
    final_text: str = ""
    error: str | None = None
    auto_stopped_reason: str | None = None
    auto_stopped_data: dict[str, Any] | None = None


@dataclass
class _StubAgentRunsClient:
    """Captures the PATCH bodies the finalize callback writes."""

    enabled: bool = True
    rows: list[dict[str, Any]] = field(default_factory=list)
    patches: list[tuple[int, dict[str, Any]]] = field(default_factory=list)

    def list_for_task(self, task_id: int, limit: int = 200) -> list[dict[str, Any]]:
        return list(self.rows)

    def patch(self, run_id: int, body: dict[str, Any]) -> None:
        self.patches.append((run_id, dict(body)))


@dataclass
class _StubSessionStore:
    terminal_marks: list[tuple[str, str]] = field(default_factory=list)

    def mark_session_terminal(self, *, session_id: str, outcome_status: str) -> None:
        self.terminal_marks.append((session_id, outcome_status))


def _daemon_with_store(store: _StubSessionStore) -> SimpleNamespace:
    return SimpleNamespace(
        taskboard_dispatcher=SimpleNamespace(
            _store=store,
        )
    )


def _make_task(
    *,
    result: _StubInputRunResult | None = None,
    exc: BaseException | None = None,
    cancelled: bool = False,
) -> asyncio.Task[Any]:
    """Build a finished asyncio.Task with the requested terminal state.

    Run a tiny coroutine inside a fresh event loop so the task is *done*
    by the time the test inspects it.
    """

    async def _ok() -> Any:
        return result

    async def _raise() -> Any:
        raise exc  # type: ignore[misc]

    async def _cancel_self() -> Any:
        await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    try:
        if cancelled:
            t = loop.create_task(_cancel_self())
            t.cancel()
            try:
                loop.run_until_complete(t)
            except asyncio.CancelledError:
                pass
        elif exc is not None:
            t = loop.create_task(_raise())
            try:
                loop.run_until_complete(t)
            except BaseException:  # noqa: BLE001
                pass
        else:
            t = loop.create_task(_ok())
            loop.run_until_complete(t)
        return t
    finally:
        loop.close()


class FinalizeInprocessRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cleanup_calls: list[str] = []
        self.client = _StubAgentRunsClient(
            rows=[
                {"id": 7, "session_id": "sess-x", "status": "spawning"},
            ]
        )
        # Patch the lazy `from_env` import inside the function.
        self._patcher = patch(
            "agent.agent_runs_client.AgentRunsClient.from_env",
            return_value=self.client,
        )
        self._cleanup_patcher = patch(
            "agent.taskboard_dispatcher._cleanup_dispatcher_worktree",
            side_effect=lambda daemon_server, session_id: self.cleanup_calls.append(session_id),
        )
        self._patcher.start()
        self._cleanup_patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._cleanup_patcher.stop()

    def _terminal(self) -> dict[str, Any]:
        """Return the second PATCH body — the terminal write."""
        self.assertGreaterEqual(len(self.client.patches), 2)
        # First PATCH is spawning → running.
        self.assertEqual(self.client.patches[0], (7, {"status": "running"}))
        return self.client.patches[1][1]

    # ------------------------------------------------------------------
    # Terminal classes
    # ------------------------------------------------------------------

    def test_succeeded_when_final_text_present(self) -> None:
        task = _make_task(
            result=_StubInputRunResult(final_text="task complete: opened PR #99")
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body, {"status": "succeeded"})
        self.assertEqual(self.cleanup_calls, ["sess-x"])

    def test_endpoint_failed_on_primary_endpoint_error(self) -> None:
        task = _make_task(
            result=_StubInputRunResult(
                error="Primary endpoint failed: connection error to api.kai"
            )
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body["status"], "endpoint_failed")
        self.assertEqual(body["failure_class"], "endpoint_unreachable")
        self.assertIn("connection error", body["failure_detail"])

    def test_failed_when_task_raises(self) -> None:
        task = _make_task(exc=RuntimeError("kaboom"))
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["failure_class"], "tool_runtime_exception")
        self.assertIn("RuntimeError", body["failure_detail"])
        self.assertIn("kaboom", body["failure_detail"])

    def test_cancelled_maps_to_manual_cancellation(self) -> None:
        task = _make_task(cancelled=True)
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body["status"], "cancelled")
        # cancelled has no failure_class
        self.assertNotIn("failure_class", body)
        self.assertIn("session_id=sess-x", body["failure_detail"])

    # ------------------------------------------------------------------
    # auto_stopped — codex CR follow-up
    # ------------------------------------------------------------------

    def test_auto_stopped_iteration_budget_maps_to_timeout(self) -> None:
        """run_outcome routes 'iteration_budget exhausted' → status=timeout."""
        task = _make_task(
            result=_StubInputRunResult(
                final_text="task complete",  # final present but auto_stopped wins
                auto_stopped_reason="iteration_budget exhausted; iterations_remaining=0",
            )
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body["status"], "timeout")
        self.assertEqual(body["failure_class"], "session_exceeded_iterations")

    def test_auto_stopped_requires_approval_blocks(self) -> None:
        task = _make_task(
            result=_StubInputRunResult(
                auto_stopped_reason="requires approval for write_file: /etc/hosts",
            )
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="dev"
        )
        body = self._terminal()
        self.assertEqual(body["status"], "requires_approval_blocked")
        self.assertEqual(body["failure_class"], "tool_approval_blocked")

    def test_auto_stopped_task_complete_maps_to_succeeded(self) -> None:
        """The agent's positive AUTO_STATE: done signal must NOT misrecord as failed."""
        task = _make_task(
            result=_StubInputRunResult(
                auto_stopped_reason="AUTO_STATE: done — task complete",
            )
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body, {"status": "succeeded"})

    def test_done_final_text_wins_over_late_wall_clock_auto_stop(self) -> None:
        task = _make_task(
            result=_StubInputRunResult(
                final_text="Wrote final artifact.\n[AUTO_STATE: done]",
                auto_stopped_data={
                    "reason": "wall-clock budget exceeded",
                    "elapsed_seconds": 241.7,
                },
            )
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body, {"status": "succeeded"})

    def test_wall_clock_auto_stop_maps_specific_failure_class(self) -> None:
        task = _make_task(
            result=_StubInputRunResult(
                final_text="Still working.\n[AUTO_STATE: continue]",
                auto_stopped_data={
                    "reason": "wall-clock budget exceeded",
                    "elapsed_seconds": 181.2,
                },
            )
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["failure_class"], "wall_clock_budget_exceeded")
        self.assertIn("elapsed=181.2s", body["failure_detail"])

    def test_both_final_and_error_picks_error(self) -> None:
        """run_outcome precedence: error beats final when both present.

        run_input can populate both fields if the stream errors after a final
        event. The synthesized event order must respect that precedence.
        """
        task = _make_task(
            result=_StubInputRunResult(
                final_text="partial output before crash",
                error="Primary endpoint failed: connection error",
            )
        )
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        body = self._terminal()
        self.assertEqual(body["status"], "endpoint_failed")
        self.assertEqual(body["failure_class"], "endpoint_unreachable")

    def test_shallow_succeeded_when_neither_final_nor_error(self) -> None:
        # Both fields empty — InputRunResult collapsed nothing useful.
        task = _make_task(result=_StubInputRunResult(final_text="", error=None))
        with self.assertLogs("agent.taskboard_dispatcher", level="WARNING") as cm:
            td._finalize_dispatcher_inprocess_run(
                task,
                daemon_server=None,
                session_id="sess-x",
                task_id=10247,
                role="cr",
            )
        body = self._terminal()
        self.assertEqual(body, {"status": "succeeded"})
        self.assertTrue(
            any("shallow-inferred succeeded" in line for line in cm.output),
            cm.output,
        )

    # ------------------------------------------------------------------
    # Routing / no-op cases
    # ------------------------------------------------------------------

    def test_noop_when_client_disabled(self) -> None:
        self.client.enabled = False
        task = _make_task(result=_StubInputRunResult(final_text="ok"))
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        self.assertEqual(self.client.patches, [])

    def test_marks_local_terminal_when_client_disabled(self) -> None:
        self.client.enabled = False
        store = _StubSessionStore()
        task = _make_task(result=_StubInputRunResult(final_text="ok"))
        td._finalize_dispatcher_inprocess_run(
            task,
            daemon_server=_daemon_with_store(store),
            session_id="sess-x",
            task_id=10247,
            role="qa-agent",
        )
        self.assertEqual(self.client.patches, [])
        self.assertEqual(store.terminal_marks, [("sess-x", "succeeded")])

    def test_noop_when_no_matching_spawning_row(self) -> None:
        self.client.rows = [
            {"id": 7, "session_id": "other-session", "status": "spawning"},
            {"id": 8, "session_id": "sess-x", "status": "succeeded"},
        ]
        task = _make_task(result=_StubInputRunResult(final_text="ok"))
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        self.assertEqual(self.client.patches, [])

    def test_marks_local_terminal_when_no_matching_ledger_row(self) -> None:
        self.client.rows = [
            {"id": 7, "session_id": "other-session", "status": "spawning"},
        ]
        store = _StubSessionStore()
        task = _make_task(result=_StubInputRunResult(final_text="ok"))
        td._finalize_dispatcher_inprocess_run(
            task,
            daemon_server=_daemon_with_store(store),
            session_id="sess-x",
            task_id=10247,
            role="qa-agent",
        )
        self.assertEqual(self.client.patches, [])
        self.assertEqual(store.terminal_marks, [("sess-x", "succeeded")])

    def test_callback_swallows_internal_exceptions(self) -> None:
        # Force list_for_task to blow up — callback must not raise.
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("ledger 5xx")

        self.client.list_for_task = _boom  # type: ignore[assignment]
        store = _StubSessionStore()
        task = _make_task(result=_StubInputRunResult(final_text="ok"))
        td._finalize_dispatcher_inprocess_run(
            task,
            daemon_server=_daemon_with_store(store),
            session_id="sess-x",
            task_id=10247,
            role="cr",
        )
        # No PATCH performed — but no exception escaped the callback either,
        # and the local session still leaves the active set.
        self.assertEqual(self.client.patches, [])
        self.assertEqual(store.terminal_marks, [("sess-x", "succeeded")])

    def test_terminal_from_already_running_row_does_not_rewrite_started_at(self) -> None:
        self.client.rows = [
            {"id": 7, "session_id": "sess-x", "status": "running"},
        ]
        task = _make_task(result=_StubInputRunResult(final_text="ok"))
        td._finalize_dispatcher_inprocess_run(
            task, daemon_server=None, session_id="sess-x", task_id=10247, role="cr"
        )
        self.assertEqual(self.client.patches, [(7, {"status": "succeeded"})])


if __name__ == "__main__":
    unittest.main()

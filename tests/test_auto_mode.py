"""Tests for autonomous-mode session control flow and hidden turns."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
import unittest
from contextlib import contextmanager
from unittest.mock import patch

try:
    from langchain_core.messages import AIMessage, HumanMessage
except ModuleNotFoundError:  # pragma: no cover - lightweight CI fallback
    class HumanMessage:  # type: ignore[no-redef]
        def __init__(self, content: str) -> None:
            self.content = content

    class AIMessage(HumanMessage):  # type: ignore[no-redef]
        pass

from agent.auto_evaluator import AutoEvaluationDecision
from agent.auto_loop_brain import AutoLoopBrainConfig, LLMResult, TokenUsage, build_auto_response_evaluator
from agent.core import AgentRunner
from agent.tools import file_read, file_write, shell_exec
from daemon.core import Session


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _StaticEvaluator:
    def __init__(self, decisions: list[AutoEvaluationDecision]) -> None:
        self.decisions = list(decisions)
        self.inputs = []

    def evaluate(self, data):
        self.inputs.append(data)
        if self.decisions:
            return self.decisions.pop(0)
        return AutoEvaluationDecision(
            "ACCEPT_MAIN_STATE",
            1.0,
            "main state accepted",
            "unknown",
        )


class _FakeLLMClient:
    def __init__(self, result: LLMResult) -> None:
        self.result = result
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _FakeRunner:
    """Minimal agent-runner stub for session auto-loop tests."""

    def __init__(self, turns: list[dict] | None = None) -> None:
        self.turns = list(turns or [])
        self.chat_history = []
        self.inputs: list[str] = []
        self.continuation_flags: list[bool] = []
        self.auto_mode_calls: list[tuple[bool, int]] = []
        self._auto_readonly = False
        self._is_auto_continuation = False
        self._pause_reason: str | None = None

    def set_auto_mode(self, enabled: bool, max_iterations: int = 40):
        self.auto_mode_calls.append((enabled, max_iterations))

    @contextmanager
    def override_max_iterations(self, _limit):
        yield

    async def run(self, user_input: str):
        index = len(self.inputs)
        self.inputs.append(user_input)
        self.continuation_flags.append(bool(self._is_auto_continuation))
        turn = self.turns[index] if index < len(self.turns) else {}

        for tool_name, tool_input in turn.get("tools", []):
            yield {"type": "tool_start", "data": {"tool": tool_name, "input": tool_input}}
            yield {"type": "tool_end", "data": {"tool": tool_name, "output": "ok"}}

        final_text = turn.get("final")
        if final_text is not None:
            yield {"type": "final", "data": final_text}

        self._pause_reason = turn.get("pause_reason")

    def consume_auto_pause_reason(self) -> str | None:
        reason = self._pause_reason
        self._pause_reason = None
        return reason


async def _collect_events(session: Session, text: str) -> list[dict]:
    return [event async for event in session.stream_agent_events(text)]


class SessionAutoModeTests(unittest.IsolatedAsyncioTestCase):
    """Validate autonomous loop continuation and stop conditions."""

    def _make_session(self, turns: list[dict]) -> tuple[Session, _FakeRunner]:
        session = Session("alpha")
        runner = _FakeRunner(turns)
        session.agent_runner = runner
        return session, runner

    async def test_continue_footer_injects_hidden_continuation(self):
        session, runner = self._make_session(
            [
                {"final": "Step one complete.\n[AUTO_STATE: continue]"},
                {"final": "Task complete.\n[AUTO_STATE: done]"},
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Do the task")

        self.assertEqual(runner.inputs, ["Do the task", "Continue with the next step."])
        self.assertEqual(runner.continuation_flags, [False, True])
        self.assertEqual([event["type"] for event in events].count("auto_progress"), 2)
        self.assertEqual(events[-1]["type"], "auto_stopped")
        self.assertEqual(events[-1]["data"]["reason"], "task complete")
        self.assertFalse(session.auto_mode)

    async def test_done_footer_stops_after_one_turn(self):
        session, runner = self._make_session(
            [{"final": "Done.\n[AUTO_STATE: done]"}]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Do it")

        self.assertEqual(runner.inputs, ["Do it"])
        self.assertEqual(events[-1]["type"], "auto_stopped")
        self.assertEqual(events[-1]["data"]["reason"], "task complete")

    async def test_pause_footer_stops_with_reason(self):
        session, _runner = self._make_session(
            [{"final": "Need approval.\n[AUTO_STATE: pause | reason: requires approval for place_order]"}]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Trade")

        self.assertEqual(events[-1]["data"]["reason"], "requires approval for place_order")

    async def test_missing_footer_stops_conservatively(self):
        session, runner = self._make_session([{"final": "No footer here."}])
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Analyze")

        self.assertEqual(runner.inputs, ["Analyze"])
        self.assertEqual(events[-1]["data"]["reason"], "missing or malformed AUTO_STATE footer")

    async def test_evaluator_continue_overrides_done_and_injects_template(self):
        session, runner = self._make_session(
            [
                {"final": "Next I will update tests.\n[AUTO_STATE: done]"},
                {"final": "Task complete.\n[AUTO_STATE: done]"},
            ]
        )
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = False
        session.auto_response_evaluator = _StaticEvaluator(
            [
                AutoEvaluationDecision(
                    "CONTINUE",
                    0.95,
                    "main stopped prematurely",
                    "declared_next_step",
                    "finish_requested_artifact",
                ),
                AutoEvaluationDecision(
                    "ACCEPT_MAIN_STATE",
                    1.0,
                    "task complete",
                    "main_done_accepted",
                ),
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Do the task")

        self.assertEqual(
            runner.inputs,
            ["Do the task", "Finish the artifact or final answer requested by the task."],
        )
        self.assertEqual(runner.continuation_flags, [False, True])
        self.assertIn("auto_evaluation", [event["type"] for event in events])
        self.assertIn("auto_reply", [event["type"] for event in events])
        self.assertEqual(events[-1]["data"]["reason"], "task complete")

    async def test_evaluator_recovers_malformed_footer(self):
        session, runner = self._make_session(
            [
                {"final": "I can run the existing tests next if you want."},
                {"final": "Done.\n[AUTO_STATE: done]"},
            ]
        )
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = False
        session.auto_response_evaluator = _StaticEvaluator(
            [
                AutoEvaluationDecision(
                    "CONTINUE",
                    0.91,
                    "permission deflection",
                    "permission_deflection",
                    "proceed_readonly_analysis",
                ),
                AutoEvaluationDecision(
                    "ACCEPT_MAIN_STATE",
                    1.0,
                    "task complete",
                    "main_done_accepted",
                ),
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Analyze")

        self.assertEqual(
            runner.inputs,
            ["Analyze", "Proceed with the read-only analysis you just described."],
        )
        self.assertEqual(events[-1]["data"]["reason"], "task complete")

    async def test_evaluator_shadow_emits_event_without_altering_control_flow(self):
        session, runner = self._make_session([{"final": "No footer here."}])
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = True
        session.auto_response_evaluator = _StaticEvaluator(
            [
                AutoEvaluationDecision(
                    "CONTINUE",
                    0.95,
                    "would continue if active",
                    "permission_deflection",
                    "continue_next_safe_step",
                )
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Analyze")

        self.assertEqual(runner.inputs, ["Analyze"])
        self.assertIn("auto_evaluation", [event["type"] for event in events])
        self.assertNotIn("auto_reply", [event["type"] for event in events])
        self.assertEqual(events[-1]["data"]["reason"], "missing or malformed AUTO_STATE footer")

    async def test_auto_loop_brain_llm_fallback_injects_hidden_reply_and_telemetry(self):
        session, runner = self._make_session(
            [
                {"final": "Proceed?"},
                {"final": "Task complete.\n[AUTO_STATE: done]"},
            ]
        )
        runner.chat_history = [HumanMessage(content="Implement the feature without asking for safe-step permission.")]
        client = _FakeLLMClient(LLMResult(json.dumps({
            "decision": "CONTINUE",
            "confidence": 0.94,
            "reason": "main asked permission for a safe in-scope next step",
            "pattern": "permission_deflection",
            "auto_reply_template": "clarify_misread_main",
        }), "claude-sonnet-4-6", TokenUsage(100, 12, 0.002)))
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = False
        session.auto_response_evaluator = build_auto_response_evaluator(
            chat_history_provider=lambda: tuple(runner.chat_history),
            telemetry=session,
            config=AutoLoopBrainConfig(enabled=True),
            llm_client=client,
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Do the task")

        self.assertEqual(runner.inputs[1], "It looks like the main agent misread the request — re-read the original task and proceed with the safe next step you described.")
        evaluation = next(event["data"] for event in events if event["type"] == "auto_evaluation")
        self.assertEqual(evaluation["evaluator_kind"], "llm")
        self.assertEqual(evaluation["model_id"], "claude-sonnet-4-6")
        self.assertEqual(evaluation["escalated_from"], "unknown")
        self.assertIn("llm_usage", evaluation)
        self.assertIn("auto_reply", [event["type"] for event in events])
        self.assertEqual(len(client.calls), 1)

    async def test_auto_loop_brain_disabled_regex_passthrough_has_no_llm_call(self):
        session, runner = self._make_session([{"final": "I can inspect logs next if you want."}])
        client = _FakeLLMClient(LLMResult("{}", "claude-sonnet-4-6"))
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = False
        session.auto_response_evaluator = build_auto_response_evaluator(
            chat_history_provider=lambda: tuple(runner.chat_history),
            telemetry=session,
            config=AutoLoopBrainConfig(enabled=False),
            llm_client=client,
        )
        session.start_auto_mode(max_iterations=5, readonly=True)

        events = await _collect_events(session, "Analyze")

        self.assertEqual(runner.inputs, ["Analyze", "Proceed with the read-only analysis you just described."])
        evaluation = next(event["data"] for event in events if event["type"] == "auto_evaluation")
        self.assertEqual(evaluation["evaluator_kind"], "regex")
        self.assertEqual(client.calls, [])

    async def test_auto_loop_brain_cost_metrics_persist_and_alert(self):
        session, _runner = self._make_session([])
        with tempfile.TemporaryDirectory() as tmpdir, patch("daemon.auto_cost.db.DEFAULT_DB_PATH", Path(tmpdir) / "state.sqlite3"):
            event = session.publish_event("auto.evaluator_call_metrics", {
                "evaluator_kind": "llm",
                "model_id": "claude-sonnet-4-6",
                "escalated_from": "unknown",
                "latency_ms": 12,
                "success": True,
                "malformed": False,
                "llm_usage": {"input_tokens": 100, "output_tokens": 10, "estimated_cost_usd": 0.01},
            })
            self.assertEqual(event.topic, "auto.evaluator_call_metrics")
            queue = session.subscribe_events("auto.evaluator_cost_alert")
            session.publish_event("llm.usage", {
                "model_id": "main-model",
                "estimated_cost_usd": 0.10,
            })
            session.publish_event("auto.evaluator_call_metrics", {
                "evaluator_kind": "llm",
                "model_id": "claude-sonnet-4-6",
                "llm_usage": {"estimated_cost_usd": 0.02},
            })
            alert = await asyncio.wait_for(queue.get(), timeout=1)
            self.assertEqual(alert.topic, "auto.evaluator_cost_alert")
            self.assertGreater(alert.payload["session_auto_evaluator_cost_usd"], 0)
            self.assertEqual(alert.payload["session_main_agent_cost_usd"], 0.10)

    async def test_agent_runner_llm_usage_event_feeds_cost_alert(self):
        session, _runner = self._make_session([])
        runner = AgentRunner.__new__(AgentRunner)
        runner.bus = None
        runner.telemetry = session
        runner.chat_history = []
        runner.agent_name = "nano"
        runner.log = _DummyLogger()
        runner._active_llm_chat_history = []

        class _Executor:
            async def astream_events(self, _payload, version="v2"):
                yield {
                    "event": "on_chat_model_end",
                    "data": {
                        "output": type("LLMOutput", (), {
                            "llm_output": {
                                "token_usage": {"prompt_tokens": 1000, "completion_tokens": 100},
                                "model_name": "main-model",
                            }
                        })()
                    },
                }
                yield {"event": "on_chain_end", "name": "AgentExecutor", "data": {"output": {"output": "done"}}}

        with tempfile.TemporaryDirectory() as tmpdir, patch("daemon.auto_cost.db.DEFAULT_DB_PATH", Path(tmpdir) / "state.sqlite3"):
            _events = [event async for event in runner._stream_executor(_Executor(), "go")]
            queue = session.subscribe_events("auto.evaluator_cost_alert")
            session.publish_event("auto.evaluator_call_metrics", {
                "evaluator_kind": "llm",
                "model_id": "claude-sonnet-4-6",
                "llm_usage": {"estimated_cost_usd": 0.01},
            })
            alert = await asyncio.wait_for(queue.get(), timeout=1)
            self.assertEqual(alert.topic, "auto.evaluator_cost_alert")
            self.assertGreater(alert.payload["session_main_agent_cost_usd"], 0)

    async def test_evaluator_continuation_quota_exhausted_stops(self):
        session, runner = self._make_session([{"final": "No footer here."}])
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = False
        session.auto_response_evaluator = _StaticEvaluator(
            [
                AutoEvaluationDecision(
                    "CONTINUE",
                    0.95,
                    "continue",
                    "permission_deflection",
                    "continue_next_safe_step",
                )
            ]
        )
        session.start_auto_mode(max_iterations=5)
        session.auto_evaluator_max_continuations = 0

        events = await _collect_events(session, "Analyze")

        self.assertEqual(runner.inputs, ["Analyze"])
        self.assertEqual(
            events[-1]["data"]["reason"],
            "auto evaluator continuation quota exhausted",
        )

    async def test_evaluator_low_confidence_stops_conservatively(self):
        session, runner = self._make_session([{"final": "No footer here."}])
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = False
        session.auto_response_evaluator = _StaticEvaluator(
            [
                AutoEvaluationDecision(
                    "CONTINUE",
                    0.1,
                    "too low",
                    "permission_deflection",
                    "continue_next_safe_step",
                )
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Analyze")

        self.assertEqual(runner.inputs, ["Analyze"])
        self.assertEqual(
            events[-1]["data"]["reason"],
            "auto evaluator confidence below threshold",
        )

    async def test_evaluator_tool_input_key_is_digest_not_raw_payload(self):
        secret = "super-secret-token-123"
        session, runner = self._make_session(
            [
                {
                    "tools": [("file_read", {"path": "/tmp/data", "api_key": secret})],
                    "final": "Done.\n[AUTO_STATE: done]",
                }
            ]
        )
        session.auto_evaluator_enabled = True
        session.auto_evaluator_shadow = True
        evaluator = _StaticEvaluator(
            [
                AutoEvaluationDecision(
                    "ACCEPT_MAIN_STATE",
                    1.0,
                    "accepted",
                    "main_done_accepted",
                )
            ]
        )
        session.auto_response_evaluator = evaluator
        session.start_auto_mode(max_iterations=5)

        await _collect_events(session, "Analyze")

        self.assertEqual(runner.inputs, ["Analyze"])
        self.assertEqual(len(evaluator.inputs), 1)
        tool_call = evaluator.inputs[0].turn_tool_calls[0]
        self.assertEqual(tool_call.name, "file_read")
        self.assertRegex(tool_call.input_key, r"^[0-9a-f]{16}$")
        self.assertNotIn(secret, tool_call.input_key)
        self.assertNotIn("api_key", tool_call.input_key)
        self.assertNotIn("path", tool_call.input_key)
        self.assertNotIn("{", tool_call.input_key)

    async def test_budget_exhaustion_stops_autonomous_loop(self):
        session, runner = self._make_session(
            [
                {"final": "Keep going.\n[AUTO_STATE: continue]"},
                {"final": "Still going.\n[AUTO_STATE: continue]"},
                {"final": "Should never run.\n[AUTO_STATE: done]"},
            ]
        )
        session.start_auto_mode(max_iterations=2)

        events = await _collect_events(session, "Budget test")

        self.assertEqual(runner.inputs, ["Budget test", "Continue with the next step."])
        self.assertEqual(events[-1]["data"]["reason"], "iteration budget exhausted")

    async def test_wall_clock_exceeded_stops_autonomous_loop(self):
        session, runner = self._make_session(
            [{"final": "Keep going.\n[AUTO_STATE: continue]"}]
        )
        session.start_auto_mode(max_iterations=5)
        session.auto_max_duration = 0.0

        events = await _collect_events(session, "Slow task")

        self.assertEqual(runner.inputs, ["Slow task"])
        self.assertEqual(events[-1]["data"]["reason"], "wall-clock budget exceeded")

    async def test_loop_detection_stops_on_repeated_final_text(self):
        repeated = "Same answer.\n[AUTO_STATE: continue]"
        session, runner = self._make_session(
            [{"final": repeated}, {"final": repeated}]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Loop")

        self.assertEqual(runner.inputs, ["Loop", "Continue with the next step."])
        self.assertEqual(events[-1]["data"]["reason"], "loop detected: repeated final response")

    async def test_loop_detection_stops_on_two_no_tool_turns(self):
        session, _runner = self._make_session(
            [
                {"final": "No tools.\n[AUTO_STATE: continue]"},
                {"final": "Still no tools.\n[AUTO_STATE: continue]"},
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "No tools")

        self.assertEqual(events[-1]["data"]["reason"], "loop detected: consecutive no-tool turns")

    async def test_loop_detection_stops_on_repeated_tool_call(self):
        repeated_tool = [("query_ohlcv", {"symbol": "BTC"})]
        session, runner = self._make_session(
            [
                {"tools": repeated_tool, "final": "One.\n[AUTO_STATE: continue]"},
                {"tools": repeated_tool, "final": "Two.\n[AUTO_STATE: continue]"},
                {"tools": repeated_tool, "final": "Three.\n[AUTO_STATE: continue]"},
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Repeat tool")

        self.assertEqual(len(runner.inputs), 3)
        self.assertEqual(events[-1]["data"]["reason"], "loop detected: repeated tool call")

    async def test_runtime_pause_reason_stops_the_loop(self):
        session, runner = self._make_session([{"pause_reason": "requires approval for shell_exec"}])
        session.start_auto_mode(max_iterations=5)

        events = await _collect_events(session, "Blocked")

        self.assertEqual(runner.inputs, ["Blocked"])
        self.assertEqual(events[-1]["data"]["reason"], "requires approval for shell_exec")

    async def test_auto_off_stops_before_next_turn(self):
        session, runner = self._make_session(
            [
                {"final": "Step one.\n[AUTO_STATE: continue]"},
                {"final": "Should never run.\n[AUTO_STATE: done]"},
            ]
        )
        session.start_auto_mode(max_iterations=5)

        events: list[dict] = []
        async for event in session.stream_agent_events("Stop after one"):
            events.append(event)
            if event["type"] == "auto_progress":
                session.stop_auto_mode("stopped by user")

        self.assertEqual(runner.inputs, ["Stop after one"])
        self.assertFalse(session.auto_mode)
        self.assertEqual(events[-1]["type"], "auto_progress")


class AgentRunnerHiddenTurnTests(unittest.IsolatedAsyncioTestCase):
    """Validate hidden continuation persistence behavior in AgentRunner."""

    async def test_auto_continuation_skips_human_message_persistence(self):
        runner = AgentRunner.__new__(AgentRunner)
        runner.bus = None
        runner.tools = []
        runner.chat_history = []
        runner.agent_name = "nano"
        runner.log = _DummyLogger()
        runner.executor = object()
        runner.fallback_executor = None
        runner.fallback_executors = []
        runner._auto_mode = True
        runner._is_auto_continuation = True

        async def fake_stream(_executor, _user_input):
            yield {"type": "final", "data": "continued\n[AUTO_STATE: done]"}

        runner._stream_executor = fake_stream

        _events = [event async for event in runner.run("Continue with the next step.")]

        self.assertEqual(len(runner.chat_history), 1)
        self.assertIsInstance(runner.chat_history[0], AIMessage)
        self.assertNotIsInstance(runner.chat_history[0], HumanMessage)


class ToolPolicyEnforcementTests(unittest.TestCase):
    """Validate runner-side tool policy enforcement."""

    def _make_runner(self, *, readonly: bool) -> AgentRunner:
        runner = AgentRunner.__new__(AgentRunner)
        runner._auto_mode = True
        runner._auto_readonly = readonly
        runner.agent_name = "nano"
        return runner

    def test_tool_requires_approval_in_auto(self):
        runner = self._make_runner(readonly=False)
        wrapped = AgentRunner._wrap_tool(runner, shell_exec)

        with patch.dict(os.environ, {"KAI_TRUSTED_AUTONOMOUS": "0"}):
            with self.assertRaisesRegex(RuntimeError, "requires approval for shell_exec"):
                wrapped.invoke({"command": "echo hi"})

    def test_auto_readonly_blocks_non_read_only_tools(self):
        runner = self._make_runner(readonly=True)
        wrapped_write = AgentRunner._wrap_tool(runner, file_write)
        wrapped_read = AgentRunner._wrap_tool(runner, file_read)

        with self.assertRaisesRegex(RuntimeError, "auto readonly blocks non-read-only tool: file_write"):
            wrapped_write.invoke({"path": "/tmp/blocked.txt", "content": "x"})

        result = wrapped_read.invoke({"path": __file__})
        self.assertIn("SessionAutoModeTests", result)


if __name__ == "__main__":
    unittest.main()

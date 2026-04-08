"""Unit tests for sub-agent fallback behavior."""

import unittest

from agent.sub_agents import SubAgent


class _DummyLogger:
    """Minimal logger stub for unit tests."""

    def warning(self, *_args, **_kwargs):
        """Ignore warning logs in tests."""


class SubAgentFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Validate direct sub-agent execution behavior."""

    async def test_run_once_uses_fallback_executor_after_primary_error(self):
        """Direct ``run_once`` calls should honor the configured fallback executor."""
        agent = SubAgent.__new__(SubAgent)
        primary = object()
        fallback = object()

        agent.name = "analyst"
        agent.bus = None
        agent.executor = primary
        agent.fallback_executor = fallback
        agent.log = _DummyLogger()

        async def fake_invoke(executor, _task):
            if executor is primary:
                return "Error: Authentication required"
            return "Fallback success"

        agent._invoke = fake_invoke

        result = await agent.run_once("analyze BTC")

        self.assertEqual(result, "Fallback success")

    async def test_run_once_retries_blank_output_with_final_answer_prompt(self):
        """Direct ``run_once`` calls should retry once when the model returns blank output."""
        agent = SubAgent.__new__(SubAgent)
        primary = object()

        agent.name = "analyst"
        agent.bus = None
        agent.executor = primary
        agent.fallback_executor = None
        agent.log = _DummyLogger()

        calls = []

        async def fake_invoke(executor, task):
            calls.append((executor, task))
            if len(calls) == 1:
                return "Error: agent returned an empty response."
            return "Recovered final answer"

        agent._invoke = fake_invoke

        result = await agent.run_once("analyze BTC")

        self.assertEqual(result, "Recovered final answer")
        self.assertEqual(len(calls), 2)
        self.assertIn("Provide the final written answer", calls[1][1])


if __name__ == "__main__":
    unittest.main()

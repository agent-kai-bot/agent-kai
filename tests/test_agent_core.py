"""Unit tests for agent streaming behavior."""

import unittest

from agent.core import AgentRunner
from agent.runtime_utils import EMPTY_RESPONSE_ERROR


class _DummyLogger:
    """Minimal logger stub for unit tests."""

    def info(self, *_args, **_kwargs):
        """Ignore info logs in tests."""

    def warning(self, *_args, **_kwargs):
        """Ignore warning logs in tests."""

    def error(self, *_args, **_kwargs):
        """Ignore error logs in tests."""


class AgentRunnerStreamTests(unittest.IsolatedAsyncioTestCase):
    """Validate final-event normalization in the agent stream."""

    async def test_run_emits_normalized_final_after_blank_result(self):
        """Blank final outputs should still surface a stable final response event."""
        runner = AgentRunner.__new__(AgentRunner)
        runner.bus = None
        runner.tools = []
        runner.chat_history = []
        runner.agent_name = "nano"
        runner.log = _DummyLogger()
        runner.executor = object()
        runner.fallback_executor = None

        async def fake_stream(_executor, _user_input):
            yield {"type": "final", "data": ""}

        runner._stream_executor = fake_stream

        events = [event async for event in runner.run("hello")]
        final_events = [event for event in events if event["type"] == "final"]

        self.assertTrue(final_events)
        self.assertEqual(final_events[-1]["data"], EMPTY_RESPONSE_ERROR)


if __name__ == "__main__":
    unittest.main()

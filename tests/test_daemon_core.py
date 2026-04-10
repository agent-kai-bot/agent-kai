"""Unit tests for daemon core session primitives."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from daemon.core import DEFAULT_WATCHLIST_SYMBOLS, Session
from config import WORKSPACES_DIR


class SessionTests(unittest.TestCase):
    """Validate per-session isolation and shared template loading."""

    def test_session_isolates_mutable_state(self):
        first = Session("alpha")
        second = Session("beta")

        first.chat_history.append("hello")
        first.input_queue.append("/analyze BTC")
        first.ui_state.watchlist_symbols.append("DOGE")
        first.sub_agent_pool.get("analyst").chat_history.append("session-a")

        self.assertEqual(second.chat_history, [])
        self.assertEqual(second.input_queue, [])
        self.assertEqual(second.ui_state.watchlist_symbols, list(DEFAULT_WATCHLIST_SYMBOLS))
        self.assertEqual(second.sub_agent_pool.get("analyst").chat_history, [])

    def test_sessions_share_sub_agent_templates(self):
        first = Session("alpha")
        second = Session("beta")

        self.assertIs(
            first.sub_agent_pool.get("analyst").template,
            second.sub_agent_pool.get("analyst").template,
        )

    def test_session_paths_follow_phase1_layout(self):
        session = Session("swing-trader")
        workspaces_dir = Path(WORKSPACES_DIR)

        self.assertEqual(
            session.paths.state_path,
            workspaces_dir / "sessions" / "swing-trader.json",
        )
        self.assertEqual(
            session.sub_agent_pool.get("analyst").buffer_path,
            workspaces_dir / "sessions" / "swing-trader" / "sub_agents" / "analyst.json",
        )
        self.assertEqual(
            session.paths.memory_dir,
            workspaces_dir / "sessions" / "swing-trader" / "memory",
        )

    @mock.patch("daemon.core.AgentRunner")
    @mock.patch("daemon.core.create_tools")
    @mock.patch("daemon.core.SignalConsumer")
    def test_attach_runtime_builds_session_runtime(
        self,
        signal_consumer_cls,
        create_tools,
        agent_runner_cls,
    ):
        session = Session("alpha")
        bus = object()
        signal_consumer = object()
        runner = mock.Mock(chat_history=[])

        signal_consumer_cls.return_value = signal_consumer
        create_tools.return_value = ["tool-a", "tool-b"]
        agent_runner_cls.return_value = runner

        attached = session.attach_runtime(bus=bus, agent_name="kai")

        self.assertIs(attached, runner)
        self.assertIs(session.signal_consumer, signal_consumer)
        create_tools.assert_called_once_with(
            bus,
            session.sub_agent_registry,
            signal_consumer=signal_consumer,
        )
        agent_runner_cls.assert_called_once_with(
            tools=["tool-a", "tool-b"],
            bus=bus,
            agent_name="kai",
        )
        self.assertIs(session.agent_runner, runner)
        self.assertIs(runner.chat_history, session.chat_history)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for daemon core session primitives."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from daemon.core import (
    DEFAULT_WATCHLIST_SYMBOLS,
    Session,
    SessionPaths,
    SessionSubAgentPool,
    get_indexed_session,
    list_indexed_sessions,
    upsert_indexed_session,
)
from config import WORKSPACES_DIR
from langchain_core.messages import AIMessage, HumanMessage


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
            scheduler=None,
            session=session,
        )
        agent_runner_cls.assert_called_once_with(
            tools=["tool-a", "tool-b"],
            bus=bus,
            agent_name="kai",
        )
        self.assertIs(session.agent_runner, runner)
        self.assertIs(runner.chat_history, session.chat_history)


class SessionEventBusTests(unittest.IsolatedAsyncioTestCase):
    """Validate per-session event delivery."""

    async def test_status_updates_publish_to_session_bus(self):
        session = Session("alpha")
        any_events = session.subscribe_events()
        status_events = session.subscribe_events("status.updated")

        session.set_activity_status("thinking")

        any_event = await asyncio.wait_for(any_events.get(), timeout=0.1)
        status_event = await asyncio.wait_for(status_events.get(), timeout=0.1)

        self.assertEqual(any_event.topic, "status.updated")
        self.assertEqual(any_event.payload["status"], "thinking")
        self.assertEqual(status_event.topic, "status.updated")
        self.assertEqual(status_event.session_name, "alpha")

    async def test_stream_agent_events_republishes_runner_events(self):
        session = Session("alpha")
        token_events = session.subscribe_events("agent.token")

        async def fake_run(_user_input):
            yield {"type": "token", "data": "hello"}
            yield {"type": "final", "data": "done"}

        session.agent_runner = mock.Mock()
        session.agent_runner.run = fake_run

        events = [event async for event in session.stream_agent_events("ping")]
        token_event = await asyncio.wait_for(token_events.get(), timeout=0.1)

        self.assertEqual(events[0]["type"], "token")
        self.assertEqual(token_event.payload["value"], "hello")


class SessionPersistenceTests(unittest.TestCase):
    """Validate Phase 1 session persistence layout and serialization."""

    @staticmethod
    def _patch_storage(base_dir: Path):
        return mock.patch.multiple(
            "daemon.core",
            SESSIONS_ROOT_DIR=base_dir,
            SESSION_INDEX_PATH=base_dir / "index.json",
        )

    @staticmethod
    def _retarget_session(session: Session, base_dir: Path) -> None:
        session.paths = SessionPaths(
            root_dir=base_dir / session.name,
            state_path=base_dir / f"{session.name}.json",
            sub_agents_dir=base_dir / session.name / "sub_agents",
            memory_dir=base_dir / session.name / "memory",
        )
        session.sub_agent_pool = SessionSubAgentPool(
            session_name=session.name,
            paths=session.paths,
            templates=session.sub_agent_pool.templates,
        )
        session.sub_agent_registry.pool = session.sub_agent_pool

    def test_save_and_load_round_trip_session_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with self._patch_storage(base_dir):
                session = Session("alpha")
                self._retarget_session(session, base_dir)
                session.chat_history.extend(
                    [HumanMessage(content="hello"), AIMessage(content="world")]
                )
                session.input_queue.extend(["/analyze BTC"])
                session.ui_state.chart_symbol = "ETH"
                session.ui_state.watchlist_symbols = ["ETH", "SOL"]
                session.sub_agent_pool.get("analyst").chat_history.append(
                    HumanMessage(content="sub-agent note")
                )
                session.save()

                state_payload = session.paths.state_path.read_text(encoding="utf-8")
                self.assertIn('"chart_symbol": "ETH"', state_payload)
                self.assertTrue(session.paths.memory_dir.is_dir())

                restored = Session("alpha")
                self._retarget_session(restored, base_dir)
                restored.load()

                self.assertEqual(restored.ui_state.chart_symbol, "ETH")
                self.assertEqual(restored.ui_state.watchlist_symbols, ["ETH", "SOL"])
                self.assertEqual(restored.input_queue, ["/analyze BTC"])
                self.assertEqual(len(restored.chat_history), 2)
                self.assertEqual(
                    restored.sub_agent_pool.get("analyst").chat_history[0].content,
                    "sub-agent note",
                )

    def test_save_merges_existing_state_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with self._patch_storage(base_dir):
                session = Session("alpha")
                self._retarget_session(session, base_dir)
                session.paths.state_path.parent.mkdir(parents=True, exist_ok=True)
                session.paths.state_path.write_text(
                    '{"custom":"keep-me","ui_state":{"chart_symbol":"BTC"}}',
                    encoding="utf-8",
                )

                session.ui_state.chart_symbol = "SOL"
                session.save()

                payload = session.paths.state_path.read_text(encoding="utf-8")
                self.assertIn('"custom": "keep-me"', payload)
                self.assertIn('"chart_symbol": "SOL"', payload)


class SessionIndexTests(unittest.TestCase):
    """Validate Phase 3 session index persistence."""

    @staticmethod
    def _patch_storage(base_dir: Path):
        return mock.patch.multiple(
            "daemon.core",
            SESSIONS_ROOT_DIR=base_dir,
            SESSION_INDEX_PATH=base_dir / "index.json",
        )

    def test_touch_index_creates_session_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with self._patch_storage(base_dir):
                session = Session("alpha")
                entry = session.touch_index()

                self.assertEqual(entry.name, "alpha")
                self.assertEqual(entry.state_path, str(base_dir / "alpha.json"))
                indexed = get_indexed_session("alpha")
                self.assertIsNotNone(indexed)
                self.assertEqual(indexed.state_path, str(base_dir / "alpha.json"))
                self.assertEqual([item.name for item in list_indexed_sessions()], ["alpha"])

    def test_upsert_preserves_created_at_while_refreshing_last_activity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with self._patch_storage(base_dir):
                original = upsert_indexed_session(
                    "alpha",
                    last_activity="2026-04-10T01:00:00Z",
                )
                refreshed = upsert_indexed_session(
                    "alpha",
                    last_activity="2026-04-10T02:00:00Z",
                )

                self.assertEqual(original.created_at, refreshed.created_at)
                self.assertEqual(refreshed.last_activity, "2026-04-10T02:00:00Z")

    def test_reserved_index_name_is_rejected(self):
        with self.assertRaises(ValueError):
            Session("index")


if __name__ == "__main__":
    unittest.main()

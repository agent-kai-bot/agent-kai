"""Unit tests for terminal session slash commands."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from daemon.core import Session, get_indexed_session
from tui.terminal import TradingTerminal


class _RemoteSessionStub:
    """Minimal async remote-session stub for slash-command tests."""

    is_remote = True

    def __init__(self, name: str, sessions: list[dict] | None = None):
        self.name = name
        self._sessions = sessions or []
        self.deleted: list[str] = []

    async def list_sessions(self) -> list[dict]:
        return list(self._sessions)

    async def delete_session(self, name: str) -> dict:
        self.deleted.append(name)
        return {"deleted": True, "name": name}


class _FakeResponseWidget:
    """Minimal response widget stub for remote streaming tests."""

    def __init__(self) -> None:
        self.updates: list[str] = []

    def update(self, text: str) -> None:
        self.updates.append(text)


class _FakeChatPanel:
    """Minimal chat panel stub for remote streaming tests."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.rendered: list[tuple[_FakeResponseWidget, str]] = []
        self.widgets: list[_FakeResponseWidget] = []

    def append_message(self, text: str, _style: str | None = None) -> None:
        self.messages.append(text)

    def create_response_widget(self) -> _FakeResponseWidget:
        widget = _FakeResponseWidget()
        self.widgets.append(widget)
        return widget

    def render_widget_as_markdown(self, widget: _FakeResponseWidget, text: str) -> None:
        self.rendered.append((widget, text))

    def scroll_end(self, animate: bool = False) -> None:
        del animate


class TradingTerminalSessionCommandTests(unittest.IsolatedAsyncioTestCase):
    """Validate session listing, switching, and deletion commands."""

    @staticmethod
    def _make_terminal(session) -> TradingTerminal:
        terminal = TradingTerminal.__new__(TradingTerminal)
        terminal.session = session
        terminal._agent_working = False
        terminal._input_queue = []
        terminal._activity_status = "idle"
        terminal._chat_lines: list[str] = []
        terminal._chat_msg = terminal._chat_lines.append
        terminal._save_chat_history = mock.Mock()
        terminal.exit = mock.Mock()
        return terminal

    async def test_session_switch_exits_with_requested_session(self):
        terminal = self._make_terminal(SimpleNamespace(name="alpha", is_remote=True))

        handled = await terminal._handle_session_command(
            ["/session", "switch", "beta"]
        )

        self.assertTrue(handled)
        terminal._save_chat_history.assert_called_once_with()
        terminal.exit.assert_called_once_with(
            {"action": "switch_session", "session": "beta"}
        )

    async def test_session_switch_rejects_busy_terminal(self):
        terminal = self._make_terminal(SimpleNamespace(name="alpha", is_remote=True))
        terminal._agent_working = True

        handled = await terminal._handle_session_command(
            ["/session", "switch", "beta"]
        )

        self.assertTrue(handled)
        terminal.exit.assert_not_called()
        self.assertIn("Wait for the current turn", terminal._chat_lines[0])

    async def test_sessions_lists_remote_results(self):
        session = _RemoteSessionStub(
            "alpha",
            sessions=[
                {
                    "name": "alpha",
                    "last_activity": "2026-04-10T01:00:00Z",
                    "activity_status": "idle",
                    "queued_inputs": 0,
                },
                {
                    "name": "beta",
                    "last_activity": "2026-04-10T02:00:00Z",
                    "activity_status": "thinking...",
                    "queued_inputs": 1,
                },
            ],
        )
        terminal = self._make_terminal(session)

        handled = await terminal._handle_sessions_command(["/sessions"])

        self.assertTrue(handled)
        self.assertIn("Sessions:", terminal._chat_lines[0])
        self.assertTrue(any("alpha" in line for line in terminal._chat_lines))
        self.assertTrue(any("beta" in line for line in terminal._chat_lines))

    async def test_session_kill_calls_remote_delete(self):
        session = _RemoteSessionStub("alpha")
        terminal = self._make_terminal(session)

        handled = await terminal._handle_session_command(["/session", "kill", "beta"])

        self.assertTrue(handled)
        self.assertEqual(session.deleted, ["beta"])
        self.assertIn("Deleted session beta", terminal._chat_lines[-1])

    async def test_remote_event_stream_renders_broadcast_turn(self):
        terminal = self._make_terminal(_RemoteSessionStub("alpha"))
        chat = _FakeChatPanel()
        terminal.query_one = lambda _selector, _widget_type=None: chat
        terminal._nats_log = mock.Mock()
        terminal._set_status = mock.Mock()
        terminal._refresh_positions = mock.AsyncMock()
        terminal._save_chat_history = mock.Mock()
        terminal._drain_input_queue = mock.Mock()
        terminal._debug_enabled = False
        terminal._tool_start_times = {}
        terminal._remote_response_widget = None
        terminal._remote_response_text = ""

        await terminal._handle_remote_session_event(
            {"type": "status", "data": "thinking..."}
        )
        await terminal._handle_remote_session_event(
            {"type": "token", "data": "partial"}
        )
        await terminal._handle_remote_session_event(
            {"type": "final", "data": "answer"}
        )
        await terminal._handle_remote_session_event(
            {"type": "status", "data": "idle"}
        )

        self.assertEqual(chat.rendered[-1][1], "answer")
        terminal._refresh_positions.assert_awaited_once_with()
        terminal._save_chat_history.assert_called_once_with()
        terminal._drain_input_queue.assert_called_once_with()


class TradingTerminalLocalSessionTests(unittest.TestCase):
    """Validate standalone session helpers used by the slash commands."""

    def test_delete_local_session_removes_state_and_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                session = Session("alpha")
                session.save()

                self.assertTrue((base_dir / "alpha.json").exists())
                self.assertIsNotNone(get_indexed_session("alpha"))

                deleted = TradingTerminal._delete_local_session("alpha")

                self.assertTrue(deleted)
                self.assertFalse((base_dir / "alpha.json").exists())
                self.assertIsNone(get_indexed_session("alpha"))


if __name__ == "__main__":
    unittest.main()

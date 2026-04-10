"""Tests for the daemon-backed terminal client adapter."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from tui.client_adapter import RemoteSession


class _FakeWebSocket:
    """Minimal async websocket stub for adapter tests."""

    def __init__(self, incoming_messages: list[dict]):
        self.incoming = [json.dumps(message) for message in incoming_messages]
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if not self.incoming:
            raise AssertionError("adapter read past the scripted websocket input")
        return self.incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


class RemoteSessionTests(unittest.IsolatedAsyncioTestCase):
    """Validate attach/bootstrap and stream translation behavior."""

    async def test_connect_attaches_and_applies_snapshot(self):
        websocket = _FakeWebSocket(
            [
                {
                    "type": "session_attached",
                    "session": "terminal",
                    "state": {
                        "chart_symbol": "ETH",
                        "chart_timeframe": "4h",
                        "chart_source": "kai-api",
                        "chart_layout_mode": "dashboard",
                        "chart_color_scheme": "classic",
                        "watchlist_symbols": ["ETH", "SOL"],
                        "autotrade_enabled": True,
                        "activity_status": "idle",
                        "chat_history": [
                            {"role": "human", "content": "hello"},
                            {"role": "ai", "content": "world"},
                        ],
                    },
                },
                {"type": "status", "activity": "idle", "queue": 0},
            ]
        )

        async def factory(_url: str):
            return websocket

        session = RemoteSession("ws://127.0.0.1:8765", connection_factory=factory)
        await session.connect()

        self.assertEqual(session.ui_state.chart_symbol, "ETH")
        self.assertEqual(session.ui_state.chart_timeframe, "4h")
        self.assertEqual(session.ui_state.watchlist_symbols, ["ETH", "SOL"])
        self.assertEqual(len(session.chat_history), 2)
        attach = json.loads(websocket.sent[0])
        self.assertEqual(attach["type"], "attach")
        self.assertEqual(attach["session"], "terminal")

    async def test_connect_uses_named_session_in_attach_envelope(self):
        websocket = _FakeWebSocket(
            [
                {
                    "type": "session_attached",
                    "session": "btc-scalper",
                    "state": {
                        "chart_symbol": "BTC",
                        "chart_timeframe": "1m",
                        "chart_source": "kai-api",
                        "chart_layout_mode": "dashboard",
                        "chart_color_scheme": "classic",
                        "watchlist_symbols": ["BTC", "ETH"],
                        "autotrade_enabled": False,
                        "activity_status": "idle",
                        "chat_history": [],
                    },
                },
                {"type": "status", "activity": "idle", "queue": 0},
            ]
        )

        async def factory(_url: str):
            return websocket

        session = RemoteSession(
            "ws://127.0.0.1:8765",
            session_name="btc-scalper",
            connection_factory=factory,
        )
        await session.connect()

        attach = json.loads(websocket.sent[0])
        self.assertEqual(attach["type"], "attach")
        self.assertEqual(attach["session"], "btc-scalper")

    async def test_stream_agent_events_translates_protocol_messages(self):
        websocket = _FakeWebSocket(
            [
                {
                    "type": "session_attached",
                    "session": "terminal",
                    "state": {
                        "chart_symbol": "BTC",
                        "chart_timeframe": "1m",
                        "chart_source": "kai-api",
                        "chart_layout_mode": "dashboard",
                        "chart_color_scheme": "classic",
                        "watchlist_symbols": ["BTC", "ETH", "SOL"],
                        "autotrade_enabled": False,
                        "activity_status": "idle",
                        "chat_history": [],
                    },
                },
                {"type": "status", "activity": "idle", "queue": 0},
                {"type": "status", "activity": "thinking...", "queue": 0},
                {"type": "token", "text": "partial"},
                {"type": "tool_start", "tool": "lookup", "args": {"symbol": "BTC"}},
                {"type": "tool_end", "tool": "lookup", "elapsed_ms": 12, "ok": True},
                {"type": "final", "text": "answer"},
                {"type": "status", "activity": "idle", "queue": 0},
            ]
        )

        async def factory(_url: str):
            return websocket

        session = RemoteSession("ws://127.0.0.1:8765", connection_factory=factory)
        events = [event async for event in session.stream_agent_events("hello")]

        self.assertEqual(
            [event["type"] for event in events],
            ["status", "token", "tool_start", "tool_end", "final", "status"],
        )
        self.assertEqual(events[1]["data"], "partial")
        self.assertEqual(events[2]["data"]["tool"], "lookup")
        self.assertEqual(events[3]["data"]["output"], "")
        self.assertEqual(events[4]["data"], "answer")
        self.assertEqual(session.chat_history[0].content, "hello")
        self.assertEqual(session.chat_history[1].content, "answer")
        sent_messages = [json.loads(payload) for payload in websocket.sent]
        self.assertEqual(sent_messages[0]["type"], "attach")
        self.assertEqual(sent_messages[1], {"type": "input", "text": "hello"})

    async def test_list_sessions_uses_http_api_helper(self):
        session = RemoteSession("ws://127.0.0.1:8765/ws")
        request_json = mock.AsyncMock(
            return_value={"sessions": [{"name": "alpha"}]}
        )
        session._request_json = request_json

        sessions = await session.list_sessions()

        self.assertEqual(sessions, [{"name": "alpha"}])
        request_json.assert_awaited_once_with("GET", "/api/sessions")

    async def test_delete_session_quotes_the_session_name(self):
        session = RemoteSession("ws://127.0.0.1:8765/ws")
        request_json = mock.AsyncMock(return_value={"deleted": True})
        session._request_json = request_json

        await session.delete_session("swing trader")

        request_json.assert_awaited_once_with(
            "DELETE",
            "/api/sessions/swing%20trader",
        )

    async def test_translate_scheduled_job_envelopes(self):
        websocket = _FakeWebSocket(
            [
                {
                    "type": "session_attached",
                    "session": "terminal",
                    "state": {
                        "chart_symbol": "BTC",
                        "chart_timeframe": "1m",
                        "chart_source": "kai-api",
                        "chart_layout_mode": "dashboard",
                        "chart_color_scheme": "classic",
                        "watchlist_symbols": ["BTC", "ETH", "SOL"],
                        "autotrade_enabled": False,
                        "activity_status": "idle",
                        "chat_history": [],
                    },
                },
                {"type": "status", "activity": "idle", "queue": 0},
                {"type": "scheduled_job_created", "job": {"id": "job-1", "type": "absolute"}},
            ]
        )

        async def factory(_url: str):
            return websocket

        session = RemoteSession("ws://127.0.0.1:8765", connection_factory=factory)
        await session.connect()

        event = await session.next_event()

        self.assertEqual(event["type"], "scheduled_job_created")
        self.assertEqual(event["data"]["job"]["id"], "job-1")


if __name__ == "__main__":
    unittest.main()

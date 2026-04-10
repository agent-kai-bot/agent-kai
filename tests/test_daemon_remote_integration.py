"""End-to-end smoke test for the daemon WS server and remote client adapter."""

from __future__ import annotations

import asyncio
import socket
import unittest
from unittest import mock

import uvicorn

from daemon.server import create_app
from tui.client_adapter import RemoteSession


class _FakeBus:
    """Minimal shared-bus stub for the live daemon smoke test."""

    def __init__(self, url: str, agent_name: str):
        self.url = url
        self.agent_name = agent_name

    async def connect(self) -> None:
        """No-op async connect."""

    async def disconnect(self) -> None:
        """No-op async disconnect."""


class _FakeRunner:
    """Predictable runner used to exercise the protocol over a real socket."""

    def __init__(self) -> None:
        self.chat_history = []

    async def run(self, user_input: str):
        yield {"type": "token", "data": f"live:{user_input}"}
        yield {
            "type": "tool_start",
            "data": {"tool": "lookup", "input": {"text": user_input}},
        }
        yield {
            "type": "tool_end",
            "data": {"tool": "lookup", "output": "ok"},
        }
        yield {"type": "final", "data": f"done:{user_input}"}


def _fake_attach_runtime(
    session,
    *,
    bus=None,
    agent_name="kai",
    signal_consumer=None,
):
    runner = _FakeRunner()
    session.agent_runner = runner
    session.agent_name = agent_name
    runner.chat_history = session.chat_history
    return runner


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DaemonRemoteIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Verify the daemon and remote adapter work together over a live websocket."""

    @staticmethod
    async def _collect_turn(session: RemoteSession) -> list[dict]:
        events: list[dict] = []
        saw_terminal_event = False
        while True:
            event = await asyncio.wait_for(session.next_event(), timeout=2)
            events.append(event)
            if event["type"] in {"final", "error"}:
                saw_terminal_event = True
            if saw_terminal_event and event["type"] == "status" and event["data"] == "idle":
                return events

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_live_remote_session_round_trip(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime
        port = _reserve_port()
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
        )
        config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())
        session = RemoteSession(f"ws://127.0.0.1:{port}")

        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("uvicorn server did not start in time")

            events = [event async for event in session.stream_agent_events("hello")]

            self.assertEqual(
                [event["type"] for event in events],
                ["status", "token", "tool_start", "tool_end", "final", "status"],
            )
            self.assertEqual(events[1]["data"], "live:hello")
            self.assertEqual(events[2]["data"]["tool"], "lookup")
            self.assertEqual(events[4]["data"], "done:hello")
            self.assertEqual(session.chat_history[0].content, "hello")
            self.assertEqual(session.chat_history[1].content, "done:hello")
        finally:
            await session.close()
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_live_multi_attach_broadcasts_to_all_clients(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime
        port = _reserve_port()
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
        )
        config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())
        first = RemoteSession(f"ws://127.0.0.1:{port}", session_name="shared")
        second = RemoteSession(f"ws://127.0.0.1:{port}", session_name="shared")

        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("uvicorn server did not start in time")

            await first.connect()
            await second.connect()

            first_events = asyncio.create_task(self._collect_turn(first))
            second_events = asyncio.create_task(self._collect_turn(second))
            await first.send_input("fanout")

            received_first = await asyncio.wait_for(first_events, timeout=5)
            received_second = await asyncio.wait_for(second_events, timeout=5)

            self.assertEqual(
                [event["type"] for event in received_first],
                ["status", "token", "tool_start", "tool_end", "final", "status"],
            )
            self.assertEqual(
                [event["type"] for event in received_second],
                ["status", "token", "tool_start", "tool_end", "final", "status"],
            )
            self.assertEqual(received_first[1]["data"], "live:fanout")
            self.assertEqual(received_second[1]["data"], "live:fanout")
            self.assertEqual(received_first[4]["data"], "done:fanout")
            self.assertEqual(received_second[4]["data"], "done:fanout")
        finally:
            await first.close()
            await second.close()
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)


if __name__ == "__main__":
    unittest.main()

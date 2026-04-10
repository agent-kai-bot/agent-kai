"""Tests for the Phase 2 FastAPI WebSocket daemon server."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from daemon.server import DaemonServer, create_app
from daemon.scheduler import Scheduler, _utc_now


class _FakeBus:
    """Minimal async bus stub for server lifecycle tests."""

    def __init__(self, url: str, agent_name: str):
        self.url = url
        self.agent_name = agent_name
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


class _FakeRunner:
    """Predictable runner that emits a short agent stream."""

    def __init__(self) -> None:
        self.chat_history = []

    async def run(self, user_input: str):
        yield {"type": "token", "data": f"echo:{user_input}"}
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


class DaemonServerTests(unittest.TestCase):
    """Validate attach/input flow and event relay over WebSocket."""

    def _make_client(self) -> TestClient:
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
        )
        return TestClient(app)

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_attach_and_input_stream_agent_events(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with self._make_client() as client:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(
                    {
                        "type": "attach",
                        "session": "terminal",
                        "create_if_missing": True,
                    }
                )

                attached = websocket.receive_json()
                status = websocket.receive_json()

                self.assertEqual(attached["type"], "session_attached")
                self.assertEqual(attached["session"], "terminal")
                self.assertEqual(attached["state"]["chart_symbol"], "BTC")
                self.assertEqual(status["type"], "status")

                websocket.send_json({"type": "input", "text": "hello"})

                received = [websocket.receive_json() for _ in range(6)]
                self.assertEqual(received[0]["type"], "status")
                self.assertEqual(received[0]["activity"], "thinking...")
                self.assertEqual(received[1], {"type": "token", "text": "echo:hello"})
                self.assertEqual(received[2]["type"], "tool_start")
                self.assertEqual(received[2]["tool"], "lookup")
                self.assertEqual(received[3]["type"], "tool_end")
                self.assertEqual(received[3]["tool"], "lookup")
                self.assertTrue(received[3]["ok"])
                self.assertEqual(received[4], {"type": "final", "text": "done:hello"})
                self.assertEqual(received[5]["type"], "status")
                self.assertEqual(received[5]["activity"], "idle")

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_subscribed_signal_and_chart_events_are_forwarded(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with self._make_client() as client:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(
                    {
                        "type": "attach",
                        "session": "terminal",
                        "create_if_missing": True,
                    }
                )
                websocket.receive_json()
                websocket.receive_json()

                websocket.send_json({"type": "subscribe", "channel": "signals"})
                websocket.send_json(
                    {
                        "type": "subscribe",
                        "channel": "chart",
                        "symbol": "BTC-USD",
                        "tf": "1h",
                    }
                )

                session = client.app.state.daemon_server.sessions["terminal"].session
                session.publish_event(
                    "signal.received",
                    {"signal": {"symbol": "ETH", "side": "long", "score": 0.82}},
                )
                session.publish_event(
                    "chart.bar",
                    {
                        "symbol": "BTC-USD",
                        "tf": "1h",
                        "bar": {"ts": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 99},
                    },
                )

                signal = websocket.receive_json()
                chart_bar = websocket.receive_json()

                self.assertEqual(signal["type"], "signal")
                self.assertEqual(signal["signal"]["symbol"], "ETH")
                self.assertEqual(chart_bar["type"], "chart_bar")
                self.assertEqual(chart_bar["symbol"], "BTC-USD")
                self.assertEqual(chart_bar["tf"], "1h")

    def test_websocket_requires_attach_as_first_message(self):
        with self._make_client() as client:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json({"type": "input", "text": "hello"})
                error = websocket.receive_json()

                self.assertEqual(error["type"], "error")
                self.assertEqual(error["code"], "bad_request")
                self.assertIn("attach envelope", error["message"])


class DaemonServerIndexTests(unittest.IsolatedAsyncioTestCase):
    """Validate Phase 3 session lookup through the persisted index."""

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_indexed_session_can_reopen_without_state_file(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                server = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=None,
                )

                managed = await server.get_or_create_session(
                    "swing-trader",
                    create_if_missing=True,
                )
                self.assertTrue((base_dir / "index.json").exists())
                self.assertFalse(managed.session.paths.state_path.exists())

                server.sessions.clear()
                reopened = await server.get_or_create_session(
                    "swing-trader",
                    create_if_missing=False,
                )

                self.assertEqual(reopened.session.name, "swing-trader")
                self.assertFalse(reopened.session.paths.state_path.exists())

    async def test_daemon_event_bus_triggers_matching_scheduler_jobs(self):
        fired: list[str] = []
        tmpdir = tempfile.TemporaryDirectory()

        def scheduler_factory(*, dispatch_callback, event_bus, **_kwargs):
            del dispatch_callback

            async def capture(job, _fired_at):
                fired.append(job.id)

            return Scheduler(
                dispatch_callback=capture,
                event_bus=event_bus,
                jobs_path=Path(tmpdir.name) / "scheduler" / "jobs.json",
            )

        server = DaemonServer(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
            scheduler_factory=scheduler_factory,
        )

        await server.startup()
        try:
            self.assertIsNotNone(server.scheduler)
            server.scheduler.schedule_job(
                {
                    "id": "job-signal",
                    "type": "event",
                    "spec": {
                        "channel": "signals",
                        "filter": {"symbol": "BTC", "score": {"gt": 0.9}},
                    },
                    "prompt": "Summarize the signal",
                    "owner_session": "terminal",
                    "created_at": "2026-04-10T00:00:00+00:00",
                    "created_by": "agent",
                },
                persist=False,
            )

            await server.publish_daemon_event("signals", {"symbol": "ETH", "score": 0.95})
            await server.publish_daemon_event("signals", {"symbol": "BTC", "score": 0.95})

            self.assertEqual(fired, ["job-signal"])
        finally:
            await server.shutdown()
            tmpdir.cleanup()

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_scheduled_job_dispatch_runs_in_target_session(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)

            def scheduler_factory(*, dispatch_callback, event_bus, **_kwargs):
                return Scheduler(
                    dispatch_callback=dispatch_callback,
                    event_bus=event_bus,
                    jobs_path=base_dir / "scheduler" / "jobs.json",
                )

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                server = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=_FakeBus,
                    scheduler_factory=scheduler_factory,
                )

                await server.startup()
                try:
                    managed = await server.get_or_create_session(
                        "btc-scalper",
                        create_if_missing=True,
                    )
                    self.assertIsNotNone(server.scheduler)

                    server.scheduler.schedule_job(
                        {
                            "id": "job-turn",
                            "type": "absolute",
                            "spec": {"at": "2026-04-10T00:01:00+00:00"},
                            "prompt": "Check BTC",
                            "owner_session": "btc-scalper",
                            "created_at": "2026-04-10T00:00:00+00:00",
                            "created_by": "user",
                        },
                        persist=False,
                    )

                    job = server.scheduler.get_job("job-turn")
                    self.assertIsNotNone(job)
                    await server._handle_scheduled_job_trigger(job, _utc_now())

                    updated = server.scheduler.get_job("job-turn")
                    self.assertEqual(updated.run_count, 1)
                    self.assertEqual(updated.status, "completed")
                    self.assertEqual(updated.last_result_preview, "done:Check BTC")
                    self.assertIn(
                        "[scheduled job: job-turn]",
                        [message.content for message in managed.session.chat_history],
                    )
                finally:
                    await server.shutdown()


class DaemonServerRestTests(unittest.TestCase):
    """Validate the Phase 3 REST session lifecycle endpoints."""

    @staticmethod
    def _make_client() -> TestClient:
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
        )
        return TestClient(app)

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_rest_session_lifecycle(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with self._make_client() as client:
                    listed = client.get("/api/sessions")
                    self.assertEqual(listed.status_code, 200)
                    self.assertEqual(listed.json(), {"sessions": []})

                    created = client.post(
                        "/api/sessions",
                        json={"name": "btc-scalper"},
                    )
                    self.assertEqual(created.status_code, 201)
                    payload = created.json()["session"]
                    self.assertEqual(payload["name"], "btc-scalper")
                    self.assertEqual(payload["activity_status"], "idle")
                    self.assertTrue((base_dir / "btc-scalper.json").exists())

                    listed = client.get("/api/sessions")
                    self.assertEqual(listed.status_code, 200)
                    sessions = listed.json()["sessions"]
                    self.assertEqual([item["name"] for item in sessions], ["btc-scalper"])
                    self.assertIn("last_activity", sessions[0])

                    deleted = client.delete("/api/sessions/btc-scalper")
                    self.assertEqual(deleted.status_code, 200)
                    self.assertEqual(
                        deleted.json(),
                        {"deleted": True, "name": "btc-scalper"},
                    )
                    self.assertFalse((base_dir / "btc-scalper.json").exists())

                    listed = client.get("/api/sessions")
                    self.assertEqual(listed.status_code, 200)
                    self.assertEqual(listed.json(), {"sessions": []})

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_rest_rejects_duplicate_and_missing_sessions(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with self._make_client() as client:
                    created = client.post("/api/sessions", json={"name": "alpha"})
                    self.assertEqual(created.status_code, 201)

                    duplicate = client.post("/api/sessions", json={"name": "alpha"})
                    self.assertEqual(duplicate.status_code, 409)
                    self.assertIn("already exists", duplicate.json()["detail"])

                    missing = client.delete("/api/sessions/missing")
                    self.assertEqual(missing.status_code, 404)
                    self.assertIn("does not exist", missing.json()["detail"])


if __name__ == "__main__":
    unittest.main()

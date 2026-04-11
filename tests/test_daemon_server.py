"""Tests for the Phase 2 FastAPI WebSocket daemon server."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
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
        self.auto_mode_calls: list[tuple[bool, int]] = []

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

    def set_auto_mode(self, enabled: bool, max_iterations: int = 40):
        self.auto_mode_calls.append((enabled, max_iterations))

    def consume_auto_pause_reason(self):
        return None


def _fake_attach_runtime(
    session,
    *,
    bus=None,
    agent_name="kai",
    signal_consumer=None,
    scheduler=None,
):
    del bus, signal_consumer, scheduler
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

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_subscribed_nats_events_are_forwarded(self, attach_runtime):
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

                websocket.send_json({"type": "subscribe", "channel": "nats"})

                client.app.state.daemon_server._handle_nats_message(
                    "pub",
                    "agent.broadcast",
                    {"message": "hello"},
                )

                nats_event = websocket.receive_json()
                self.assertEqual(nats_event["type"], "nats_event")
                self.assertEqual(nats_event["direction"], "pub")
                self.assertEqual(nats_event["subject"], "agent.broadcast")
                self.assertEqual(nats_event["payload"]["message"], "hello")

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_scheduled_job_events_are_forwarded(self, attach_runtime):
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

                session = client.app.state.daemon_server.sessions["terminal"].session
                session.publish_event(
                    "scheduled_job.created",
                    {
                        "job": {
                            "id": "job-1",
                            "status": "active",
                            "owner_session": "terminal",
                        }
                    },
                )
                session.publish_event(
                    "scheduled_job.completed",
                    {
                        "job_id": "job-1",
                        "result_preview": "done",
                    },
                )

                created = websocket.receive_json()
                completed = websocket.receive_json()

                self.assertEqual(created["type"], "scheduled_job_created")
                self.assertEqual(created["job"]["id"], "job-1")
                self.assertEqual(completed["type"], "scheduled_job_completed")
                self.assertEqual(completed["job_id"], "job-1")
                self.assertEqual(completed["result_preview"], "done")

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_auto_slash_command_enables_status_and_stops(self, attach_runtime):
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

                websocket.send_json({"type": "input", "text": "/auto readonly 3"})
                enable_messages = [websocket.receive_json() for _ in range(3)]
                started = next(
                    message for message in enable_messages if message["type"] == "auto_started"
                )
                final = next(
                    message for message in enable_messages if message["type"] == "final"
                )
                self.assertTrue(started["readonly"])
                self.assertIn("readonly", final["text"])

                websocket.send_json({"type": "input", "text": "/auto status"})
                status_messages = [websocket.receive_json() for _ in range(2)]
                status_final = next(
                    message for message in status_messages if message["type"] == "final"
                )
                self.assertIn("Auto mode readonly", status_final["text"])

                websocket.send_json({"type": "input", "text": "/auto off"})
                off_messages = [websocket.receive_json() for _ in range(3)]
                stopped = next(
                    message for message in off_messages if message["type"] == "auto_stopped"
                )
                off_final = next(
                    message for message in off_messages if message["type"] == "final"
                )
                self.assertEqual(stopped["reason"], "stopped by user")
                self.assertIn("Auto mode stopped", off_final["text"])

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_schedule_slash_commands_create_and_pause_jobs(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)

            def scheduler_factory(*, dispatch_callback, event_bus, event_callback, **_kwargs):
                return Scheduler(
                    dispatch_callback=dispatch_callback,
                    event_bus=event_bus,
                    event_callback=event_callback,
                    jobs_path=base_dir / "scheduler" / "jobs.json",
                )

            app = create_app(
                agent_name="kai",
                nats_url="nats://unit-test",
                bus_factory=_FakeBus,
                scheduler_factory=scheduler_factory,
            )

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with TestClient(app) as client:
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

                        websocket.send_json(
                            {
                                "type": "input",
                                "text": '/schedule add at "in 1 minute" "Check BTC"',
                            }
                        )

                        add_messages = [websocket.receive_json() for _ in range(3)]
                        created = next(
                            message for message in add_messages if message["type"] == "scheduled_job_created"
                        )
                        final = next(
                            message for message in add_messages if message["type"] == "final"
                        )
                        job_id = created["job"]["id"]

                        self.assertIn("Scheduled", final["text"])

                        websocket.send_json({"type": "input", "text": "/schedule list"})
                        listed = [websocket.receive_json() for _ in range(2)]
                        list_final = next(
                            message for message in listed if message["type"] == "final"
                        )
                        self.assertIn(job_id, list_final["text"])

                        websocket.send_json({"type": "input", "text": "/schedule pause all"})
                        paused = [websocket.receive_json() for _ in range(3)]
                        pause_final = next(
                            message for message in paused if message["type"] == "final"
                        )
                        self.assertIn("Paused 1 scheduled jobs", pause_final["text"])

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

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_restart_catchup_replays_recent_absolute_job(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            jobs_path = base_dir / "scheduler" / "jobs.json"

            def scheduler_factory(*, dispatch_callback, event_bus, event_callback, **_kwargs):
                return Scheduler(
                    dispatch_callback=dispatch_callback,
                    event_bus=event_bus,
                    event_callback=event_callback,
                    jobs_path=jobs_path,
                )

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                seed = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=_FakeBus,
                    scheduler_factory=scheduler_factory,
                )
                await seed.startup()
                try:
                    managed = await seed.get_or_create_session("alpha", create_if_missing=True)
                    managed.session.save()
                finally:
                    await seed.shutdown()

                fired_at = (_utc_now() - timedelta(minutes=2)).replace(microsecond=0).isoformat()
                jobs_path.parent.mkdir(parents=True, exist_ok=True)
                jobs_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "jobs": {
                                "job-catchup": {
                                    "id": "job-catchup",
                                    "type": "absolute",
                                    "spec": {"at": fired_at},
                                    "prompt": "Catch up BTC",
                                    "owner_session": "alpha",
                                    "created_at": "2026-04-10T00:00:00+00:00",
                                    "created_by": "user",
                                    "next_run": fired_at,
                                    "run_count": 0,
                                    "status": "active",
                                    "concurrency": "queue",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                server = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=_FakeBus,
                    scheduler_factory=scheduler_factory,
                )
                await server.startup()
                try:
                    for _ in range(50):
                        job = server.scheduler.get_job("job-catchup")
                        if job.run_count == 1:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("catch-up job did not replay after restart")

                    managed = server.sessions["alpha"]
                    self.assertEqual(job.status, "completed")
                    self.assertEqual(job.last_result_preview, "done:Catch up BTC")
                    self.assertIn(
                        "[scheduled job: job-catchup]",
                        [message.content for message in managed.session.chat_history],
                    )
                finally:
                    await server.shutdown()

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    async def test_event_jobs_survive_restart_and_list_from_slash_command(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            jobs_path = base_dir / "scheduler" / "jobs.json"

            def scheduler_factory(*, dispatch_callback, event_bus, event_callback, **_kwargs):
                return Scheduler(
                    dispatch_callback=dispatch_callback,
                    event_bus=event_bus,
                    event_callback=event_callback,
                    jobs_path=jobs_path,
                )

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                first = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=_FakeBus,
                    scheduler_factory=scheduler_factory,
                )
                await first.startup()
                try:
                    managed = await first.get_or_create_session("alpha", create_if_missing=True)
                    managed.session.save()
                    job = first.scheduler.create_event_job(
                        condition={
                            "channel": "signals",
                            "filter": {"symbol": "BTC", "score": {"gt": 0.9}},
                        },
                        prompt="Summarize signal",
                        owner_session="alpha",
                        created_by="agent",
                    )
                finally:
                    await first.shutdown()

                second = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=_FakeBus,
                    scheduler_factory=scheduler_factory,
                )
                await second.startup()
                try:
                    managed = await second.get_or_create_session("alpha", create_if_missing=False)
                    listed = await second.handle_schedule_command(managed, "/schedule list")
                    self.assertIn(job.id, listed)

                    await second.publish_daemon_event("signals", {"symbol": "BTC", "score": 0.95})

                    for _ in range(50):
                        updated = second.scheduler.get_job(job.id)
                        if updated.run_count == 1:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("event-triggered job did not fire after restart")

                    self.assertEqual(updated.last_result_preview, "done:Summarize signal")
                    self.assertIn(
                        "[scheduled job: " + job.id + "]",
                        [message.content for message in managed.session.chat_history],
                    )
                finally:
                    await second.shutdown()


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

    @mock.patch("daemon.server._fetch_watchlist_quote")
    def test_rest_watchlist_quotes_and_portfolio_snapshot(self, fetch_watchlist_quote):
        fetch_watchlist_quote.side_effect = [
            {
                "symbol": "BTC",
                "price": 101_000.0,
                "volume_24h": 1234.5,
                "price_change_24h_pct": 2.5,
            },
            {
                "symbol": "ETH",
                "price": 5_200.0,
                "volume_24h": 4321.0,
                "price_change_24h_pct": -1.2,
            },
        ]

        with mock.patch(
            "daemon.server._load_portfolio_snapshot",
            return_value={
                "positions": [
                    {
                        "symbol": "BTC",
                        "side": "long",
                        "quantity": 0.25,
                        "entry_price": 95_000.0,
                        "current_price": 101_000.0,
                        "unrealized_pnl": 1_500.0,
                        "pnl_pct": 6.3,
                    }
                ],
                "pnl": {
                    "total_value": 105_000.0,
                    "total_pnl": 5_000.0,
                    "total_pnl_pct": 5.0,
                },
            },
        ):
            with self._make_client() as client:
                watchlist = client.get("/api/market/watchlist?symbols=BTC,ETH")
                self.assertEqual(watchlist.status_code, 200)
                quotes = watchlist.json()["quotes"]
                self.assertEqual([item["symbol"] for item in quotes], ["BTC", "ETH"])
                self.assertEqual(quotes[0]["price"], 101_000.0)

                portfolio = client.get("/api/portfolio")
                self.assertEqual(portfolio.status_code, 200)
                payload = portfolio.json()
                self.assertEqual(payload["positions"][0]["symbol"], "BTC")
                self.assertEqual(payload["pnl"]["total_value"], 105_000.0)

    @mock.patch(
        "daemon.server._load_chart_history",
        return_value=[{"ts": "2026-04-10T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 42}],
    )
    def test_rest_chart_history_snapshot(self, load_chart_history):
        with self._make_client() as client:
            response = client.get(
                "/api/market/ohlcv",
                params={
                    "symbol": "BTC",
                    "interval": "1h",
                    "source": "coinbase",
                    "limit": 120,
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["bars"][0]["close"], 1.5)
            load_chart_history.assert_called_once_with("BTC", "1h", "coinbase", 120)


class DaemonServerAuthTests(unittest.TestCase):
    """Validate Phase 7 token enforcement on REST and websocket paths."""

    @staticmethod
    def _make_client(
        token_path: Path,
        *,
        allow_unauthenticated_local: bool = False,
    ) -> TestClient:
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
            token_path=token_path,
            allow_unauthenticated_local=allow_unauthenticated_local,
        )
        return TestClient(app)

    def test_rest_requires_bearer_token_when_local_bypass_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir) / "sessions"
            token_path = Path(tmpdir) / "daemon-token.txt"
            token_path.write_text("secret-token\n", encoding="utf-8")

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with self._make_client(token_path) as client:
                    unauthorized = client.get("/api/sessions")
                    self.assertEqual(unauthorized.status_code, 401)
                    self.assertEqual(
                        unauthorized.json()["detail"],
                        "daemon bearer token required",
                    )

                    authorized = client.get(
                        "/api/sessions",
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    self.assertEqual(authorized.status_code, 200)
                    self.assertEqual(authorized.json(), {"sessions": []})

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_websocket_rejects_unauthorized_client(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir) / "sessions"
            token_path = Path(tmpdir) / "daemon-token.txt"
            token_path.write_text("secret-token\n", encoding="utf-8")

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with self._make_client(token_path) as client:
                    with client.websocket_connect("/ws") as websocket:
                        error = websocket.receive_json()

                        self.assertEqual(error["type"], "error")
                        self.assertEqual(error["code"], "unauthorized")
                        self.assertIn("bearer token", error["message"])

    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_websocket_accepts_token_query_parameter(self, attach_runtime):
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir) / "sessions"
            token_path = Path(tmpdir) / "daemon-token.txt"
            token_path.write_text("secret-token\n", encoding="utf-8")

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with self._make_client(token_path) as client:
                    with client.websocket_connect("/ws?token=secret-token") as websocket:
                        websocket.send_json(
                            {
                                "type": "attach",
                                "session": "terminal",
                                "create_if_missing": True,
                            }
                        )

                        attached = websocket.receive_json()
                        self.assertEqual(attached["type"], "session_attached")
                        self.assertEqual(attached["session"], "terminal")


class DaemonServerHealthTests(unittest.TestCase):
    """Validate the Phase 7 health and metrics payloads."""

    @staticmethod
    def _make_client(token_path: Path) -> TestClient:
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
            token_path=token_path,
            allow_unauthenticated_local=False,
        )
        return TestClient(app)

    @mock.patch("daemon.server._process_memory_bytes", return_value=2_097_152)
    @mock.patch("daemon.server.Session.attach_runtime", autospec=True)
    def test_health_and_metrics_report_daemon_runtime_state(
        self,
        attach_runtime,
        _process_memory_bytes,
    ):
        del _process_memory_bytes
        attach_runtime.side_effect = _fake_attach_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir) / "sessions"
            token_path = Path(tmpdir) / "daemon-token.txt"
            token_path.write_text("secret-token\n", encoding="utf-8")
            headers = {"Authorization": "Bearer secret-token"}

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with self._make_client(token_path) as client:
                    created = client.post(
                        "/api/sessions",
                        headers=headers,
                        json={"name": "alpha"},
                    )
                    self.assertEqual(created.status_code, 201)

                    health = client.get("/api/health", headers=headers)
                    self.assertEqual(health.status_code, 200)
                    health_payload = health.json()
                    self.assertEqual(health_payload["status"], "ok")
                    self.assertEqual(health_payload["agent_name"], "kai")
                    self.assertEqual(health_payload["session_count"], 1)
                    self.assertEqual(health_payload["memory_rss_bytes"], 2_097_152)
                    self.assertEqual(health_payload["agent_queue_depth"], 0)
                    self.assertEqual(health_payload["scheduler_job_count"], 0)
                    self.assertGreaterEqual(health_payload["uptime_seconds"], 0)

                    metrics = client.get("/api/metrics", headers=headers)
                    self.assertEqual(metrics.status_code, 200)
                    metrics_payload = metrics.json()
                    self.assertEqual(metrics_payload["agent_name"], "kai")
                    self.assertTrue(metrics_payload["bus_connected"])
                    self.assertEqual(metrics_payload["process"]["memory_rss_bytes"], 2_097_152)
                    self.assertEqual(metrics_payload["sessions"]["live_count"], 1)
                    self.assertEqual(metrics_payload["sessions"]["indexed_count"], 1)
                    self.assertEqual(metrics_payload["sessions"]["queue_depth"]["total"], 0)
                    self.assertEqual(
                        metrics_payload["sessions"]["queue_depth"]["per_session"],
                        {"alpha": 0},
                    )
                    self.assertEqual(metrics_payload["sessions"]["activity"], {"alpha": "idle"})
                    self.assertEqual(metrics_payload["scheduler"]["job_count"], 0)
                    self.assertEqual(metrics_payload["scheduler"]["status_counts"], {})


class DaemonServerWebAssetTests(unittest.TestCase):
    """Validate the Phase 6 static web asset mounting."""

    @staticmethod
    def _make_client(build_dir: Path | None = None) -> TestClient:
        app = create_app(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
            web_build_dir=build_dir,
        )
        return TestClient(app)

    def test_root_and_spa_routes_serve_built_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = Path(tmpdir)
            asset_dir = build_dir / "_app" / "immutable"
            asset_dir.mkdir(parents=True)
            (build_dir / "index.html").write_text(
                "<!doctype html><html><body>web shell</body></html>",
                encoding="utf-8",
            )
            (asset_dir / "app.js").write_text("console.log('web shell');", encoding="utf-8")

            with self._make_client(build_dir) as client:
                root = client.get("/")
                self.assertEqual(root.status_code, 200)
                self.assertIn("web shell", root.text)

                deep_link = client.get("/session/terminal")
                self.assertEqual(deep_link.status_code, 200)
                self.assertIn("web shell", deep_link.text)

                asset = client.get("/_app/immutable/app.js")
                self.assertEqual(asset.status_code, 200)
                self.assertIn("console.log", asset.text)

                missing_asset = client.get("/_app/immutable/missing.js")
                self.assertEqual(missing_asset.status_code, 404)

    def test_missing_build_returns_placeholder_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = Path(tmpdir) / "missing-build"

            with self._make_client(build_dir) as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 503)
                self.assertIn("Web UI build not found", response.text)
                self.assertIn(str(build_dir), response.text)


if __name__ == "__main__":
    unittest.main()

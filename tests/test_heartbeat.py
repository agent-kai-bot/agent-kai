"""Tests for daemon-owned heartbeat ticks."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from daemon.heartbeat import HeartbeatConfig, HeartbeatService, load_heartbeat_config
from daemon.scheduler import Scheduler
from daemon.server import DaemonServer, create_app
from tests.test_daemon_server import _FakeBus, _fake_attach_runtime


class HeartbeatServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_emit_once_builds_tick_and_invokes_callback(self):
        received = []

        async def callback(tick):
            received.append(tick)

        service = HeartbeatService(
            interval_seconds=5,
            tick_callback=callback,
            clock=lambda: datetime(2026, 5, 6, 16, 30, tzinfo=timezone.utc),
        )

        tick = await service.emit_once()

        self.assertEqual(tick.seq, 1)
        self.assertEqual(tick.emitted_at, "2026-05-06T16:30:00Z")
        self.assertEqual(tick.interval_seconds, 5)
        self.assertEqual(tick.source, "daemon")
        self.assertEqual(tick.reason, "periodic")
        self.assertEqual(received, [tick])
        self.assertEqual(service.tick_count, 1)
        self.assertEqual(service.last_tick, tick)

    async def test_shutdown_cancels_background_task(self):
        async def callback(_tick):
            return None

        service = HeartbeatService(
            interval_seconds=60,
            tick_callback=callback,
        )
        await service.start()
        self.assertTrue(service.running)
        await service.shutdown()
        self.assertFalse(service.running)


class HeartbeatConfigTests(unittest.TestCase):
    def test_daemon_server_loads_agent_config_daemon_heartbeat(self):
        config = {
            "endpoint": None,
            "fallback_endpoint": None,
            "fallback_endpoints": [],
            "daemon": {
                "heartbeat": {
                    "enabled": False,
                    "interval_seconds": 9,
                    "publish_session_events": False,
                }
            },
        }

        with mock.patch("daemon.server.get_agent_config", return_value=config):
            server = DaemonServer(
                agent_name="kai",
                nats_url="nats://unit-test",
                bus_factory=_FakeBus,
            )

        self.assertFalse(server.heartbeat_config.enabled)
        self.assertEqual(server.heartbeat_config.interval_seconds, 9)
        self.assertFalse(server.heartbeat_config.publish_session_events)

    def test_env_overrides_config(self):
        with mock.patch.dict(
            "os.environ",
            {
                "KAI_HEARTBEAT_ENABLED": "0",
                "KAI_HEARTBEAT_INTERVAL_SECONDS": "12.5",
                "KAI_HEARTBEAT_PUBLISH_SESSION_EVENTS": "0",
            },
        ):
            config = load_heartbeat_config(
                {
                    "daemon": {
                        "heartbeat": {
                            "enabled": True,
                            "interval_seconds": 60,
                            "publish_session_events": True,
                        }
                    }
                }
            )

        self.assertFalse(config.enabled)
        self.assertEqual(config.interval_seconds, 12.5)
        self.assertFalse(config.publish_session_events)


class DaemonHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_daemon_heartbeat_publishes_to_event_bus_and_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)

            def scheduler_factory(*, dispatch_callback, event_bus, event_callback, **_kwargs):
                return Scheduler(
                    dispatch_callback=dispatch_callback,
                    event_bus=event_bus,
                    event_callback=event_callback,
                    jobs_path=base_dir / "scheduler" / "jobs.json",
                )

            server = DaemonServer(
                agent_name="kai",
                nats_url="nats://unit-test",
                bus_factory=_FakeBus,
                scheduler_factory=scheduler_factory,
                heartbeat_config=HeartbeatConfig(enabled=False, interval_seconds=60),
            )
            daemon_events = []
            server.event_bus.subscribe(lambda channel, payload: daemon_events.append((channel, payload)))

            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ), mock.patch("daemon.server.Session.attach_runtime", autospec=True) as attach_runtime:
                attach_runtime.side_effect = _fake_attach_runtime
                await server.startup()
                try:
                    managed = await server.get_or_create_session(
                        "terminal",
                        create_if_missing=True,
                    )
                    session_events = managed.session.subscribe_events("heartbeat.tick")

                    tick = await server.heartbeat_service.emit_once()  # type: ignore[union-attr]
                    session_event = await asyncio.wait_for(session_events.get(), timeout=1)

                    self.assertEqual(daemon_events[-1][0], "heartbeat")
                    self.assertEqual(daemon_events[-1][1]["seq"], tick.seq)
                    self.assertEqual(session_event.topic, "heartbeat.tick")
                    self.assertEqual(session_event.payload["seq"], tick.seq)
                    self.assertFalse(session_event.payload["agent_wakeup_enabled"])
                    self.assertFalse(session_event.payload["pending"])
                    self.assertEqual(managed.session.input_queue, [])
                    self.assertIsNone(managed.current_input_task)
                finally:
                    await server.shutdown()

    def test_health_and_metrics_include_heartbeat(self):
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
                heartbeat_config=HeartbeatConfig(enabled=False, interval_seconds=33),
            )
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                with TestClient(app) as client:
                    metrics = client.get("/api/metrics").json()
                    health = client.get("/api/health").json()

            self.assertEqual(metrics["heartbeat"]["interval_seconds"], 33)
            self.assertFalse(metrics["heartbeat"]["enabled"])
            self.assertIn("heartbeat", health)
            self.assertEqual(health["heartbeat"]["interval_seconds"], 33)


if __name__ == "__main__":
    unittest.main()

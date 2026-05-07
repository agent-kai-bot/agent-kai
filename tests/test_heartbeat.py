"""Tests for daemon-owned heartbeat ticks."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from daemon.heartbeat import (
    HeartbeatConfig,
    HeartbeatPromptTemplate,
    HeartbeatService,
    HeartbeatTick,
    load_heartbeat_config,
)
from daemon.scheduler import Scheduler
from daemon.server import DaemonServer, create_app
from tests.test_daemon_server import _FakeBus, _fake_attach_runtime


class _HeartbeatFakeRunner:
    """Minimal runner that supports pre-injected heartbeat inputs."""

    def __init__(self, turns: list[dict] | None = None) -> None:
        self.turns = list(turns or [])
        self.chat_history = []
        self.inputs: list[str] = []
        self._is_auto_continuation = False
        self._auto_readonly = False
        self._pause_reason: str | None = None
        self.tool_call_active = False

    def set_auto_mode(self, _enabled: bool, max_iterations: int = 40):
        return None

    async def run(self, user_input: str, *, pre_injected_input: bool = False):
        if not pre_injected_input:
            self.chat_history.append(HumanMessage(content=user_input))
        index = len(self.inputs)
        self.inputs.append(user_input)
        turn = self.turns[index] if index < len(self.turns) else {}
        final_text = turn.get("final")
        if final_text is not None:
            yield {"type": "final", "data": final_text}
        self._pause_reason = turn.get("pause_reason")

    def consume_auto_pause_reason(self) -> str | None:
        reason = self._pause_reason
        self._pause_reason = None
        return reason


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
                    "prompt_template_path": "prompts/heartbeat/main.md.tmpl",
                    "max_injected_turns_per_hour": 3,
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
        self.assertEqual(server.heartbeat_config.prompt_template_path, "prompts/heartbeat/main.md.tmpl")
        self.assertEqual(server.heartbeat_config.max_injected_turns_per_hour, 3)

    def test_env_overrides_config(self):
        with mock.patch.dict(
            "os.environ",
            {
                "KAI_HEARTBEAT_ENABLED": "0",
                "KAI_HEARTBEAT_INTERVAL_SECONDS": "12.5",
                "KAI_HEARTBEAT_PUBLISH_SESSION_EVENTS": "0",
                "KAI_HEARTBEAT_PROMPT_TEMPLATE_PATH": "prompts/heartbeat/main.md.tmpl",
                "KAI_HEARTBEAT_MAX_INJECTED_TURNS_PER_HOUR": "7",
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
        self.assertEqual(config.prompt_template_path, "prompts/heartbeat/main.md.tmpl")
        self.assertEqual(config.max_injected_turns_per_hour, 7)

    def test_default_config_is_passive_cutover_cadence(self):
        config = load_heartbeat_config({})

        self.assertEqual(config.interval_seconds, 1800.0)
        self.assertEqual(config.max_injected_turns_per_hour, 0)

    def test_kill_switch_forces_zero_injection_cap(self):
        with mock.patch.dict(
            "os.environ",
            {
                "KAI_HEARTBEAT_MAX_INJECTED_TURNS_PER_HOUR": "4",
                "KAI_HEARTBEAT_INJECTION_KILL_SWITCH": "1",
            },
        ):
            config = load_heartbeat_config({})

        self.assertEqual(config.max_injected_turns_per_hour, 0)

    def test_template_safe_format_keeps_unknown_placeholders(self):
        tick = HeartbeatTick(
            seq=8,
            emitted_at="2026-05-06T16:30:00Z",
            monotonic_seconds=1.0,
            interval_seconds=1800,
        )
        template = HeartbeatPromptTemplate(
            name="unit",
            path=Path("unit"),
            content="tick={seq} session={session_name} missing={unknown}",
        )

        rendered = template.render(tick, session_name="terminal", agent_name="kai")

        self.assertEqual(rendered, "tick=8 session=terminal missing={unknown}")


class DaemonHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribed_auto_session_injects_one_human_message_per_tick(self):
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
                heartbeat_config=HeartbeatConfig(
                    enabled=False,
                    interval_seconds=60,
                    max_injected_turns_per_hour=4,
                ),
            )
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                await server.startup()
                try:
                    managed = await server.get_or_create_session(
                        "terminal",
                        create_if_missing=True,
                    )
                    runner = _HeartbeatFakeRunner(
                        [{"final": "Continuing.\n[AUTO_STATE: continue]"}]
                    )
                    managed.session.agent_runner = runner
                    managed.session.start_auto_mode(max_iterations=5)
                    events = managed.session.subscribe_events("*")

                    tick = await server.heartbeat_service.emit_once()  # type: ignore[union-attr]
                    await asyncio.sleep(0.05)

                    heartbeat_messages = [
                        msg
                        for msg in managed.session.chat_history
                        if "Heartbeat tick" in getattr(msg, "content", "")
                    ]
                    self.assertEqual(len(heartbeat_messages), 1)
                    self.assertEqual(runner.inputs, [heartbeat_messages[0].content])
                    self.assertTrue(managed.session.auto_mode)
                    topics = []
                    while not events.empty():
                        topics.append(events.get_nowait().topic)
                    self.assertIn("auto.heartbeat_injected", topics)
                    self.assertEqual(tick.seq, 1)
                finally:
                    await server.shutdown()

    async def test_heartbeat_decision_drops_while_tool_call_active(self):
        server = DaemonServer(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=_FakeBus,
            heartbeat_config=HeartbeatConfig(
                enabled=False,
                interval_seconds=60,
                max_injected_turns_per_hour=4,
            ),
        )
        managed = await server.get_or_create_session("terminal", create_if_missing=True)
        runner = _HeartbeatFakeRunner()
        runner.tool_call_active = True
        managed.session.agent_runner = runner
        managed.session.start_auto_mode(max_iterations=5)
        tick = HeartbeatTick(
            seq=1,
            emitted_at="2026-05-06T16:30:00Z",
            monotonic_seconds=1.0,
            interval_seconds=60,
        )

        ok, reason = server._heartbeat_injection_decision(managed, tick)

        self.assertFalse(ok)
        self.assertEqual(reason, "mid_tool_call")

    async def test_heartbeat_injection_decision_suppresses_busy_and_tool_active_states(self):
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
                heartbeat_config=HeartbeatConfig(
                    enabled=False,
                    interval_seconds=60,
                    max_injected_turns_per_hour=1,
                ),
            )
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                await server.startup()
                try:
                    managed = await server.get_or_create_session(
                        "terminal",
                        create_if_missing=True,
                    )
                    runner = _HeartbeatFakeRunner()
                    managed.session.agent_runner = runner
                    managed.session.start_auto_mode(max_iterations=5)
                    tick = HeartbeatTick(
                        seq=1,
                        emitted_at="2026-05-06T16:30:00Z",
                        monotonic_seconds=1_000.0,
                        interval_seconds=60,
                    )

                    async with managed.input_lock:
                        self.assertEqual(
                            server._heartbeat_injection_decision(managed, tick),
                            (False, "busy"),
                        )

                    managed.current_input_task = asyncio.create_task(asyncio.sleep(60))
                    try:
                        self.assertEqual(
                            server._heartbeat_injection_decision(managed, tick),
                            (False, "busy"),
                        )
                    finally:
                        managed.current_input_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await managed.current_input_task
                        managed.current_input_task = None

                    runner.tool_call_active = True
                    self.assertEqual(
                        server._heartbeat_injection_decision(managed, tick),
                        (False, "mid_tool_call"),
                    )
                    runner.tool_call_active = False

                    runner._active_recorder = object()
                    self.assertEqual(
                        server._heartbeat_injection_decision(managed, tick),
                        (False, "mid_tool_call"),
                    )
                    runner._active_recorder = None

                    runner._is_auto_continuation = True
                    self.assertEqual(
                        server._heartbeat_injection_decision(managed, tick),
                        (False, "auto_continuing"),
                    )
                    runner._is_auto_continuation = False

                    managed.session.record_heartbeat_injection(tick.monotonic_seconds)
                    self.assertEqual(
                        server._heartbeat_injection_decision(managed, tick),
                        (False, "rate_limited"),
                    )
                finally:
                    await server.shutdown()

    async def test_heartbeat_rate_limit_drops_without_queueing(self):
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
                heartbeat_config=HeartbeatConfig(
                    enabled=False,
                    interval_seconds=60,
                    max_injected_turns_per_hour=1,
                ),
            )
            with mock.patch("daemon.core.SESSIONS_ROOT_DIR", base_dir), mock.patch(
                "daemon.core.SESSION_INDEX_PATH", base_dir / "index.json"
            ):
                await server.startup()
                try:
                    managed = await server.get_or_create_session(
                        "terminal",
                        create_if_missing=True,
                    )
                    managed.session.agent_runner = _HeartbeatFakeRunner(
                        [
                            {"final": "Continue.\n[AUTO_STATE: continue]"},
                            {"final": "Continue.\n[AUTO_STATE: continue]"},
                        ]
                    )
                    managed.session.start_auto_mode(max_iterations=5)

                    await server.heartbeat_service.emit_once()  # type: ignore[union-attr]
                    await asyncio.sleep(0.05)
                    await server.heartbeat_service.emit_once()  # type: ignore[union-attr]
                    await asyncio.sleep(0.05)

                    heartbeat_messages = [
                        msg
                        for msg in managed.session.chat_history
                        if "Heartbeat tick" in getattr(msg, "content", "")
                    ]
                    self.assertEqual(len(heartbeat_messages), 1)
                    self.assertEqual(managed.session.input_queue, [])
                finally:
                    await server.shutdown()

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
                    self.assertEqual(managed.session.chat_history, [])
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
            self.assertEqual(metrics["heartbeat"]["subscribers_count"], 0)
            self.assertIn("heartbeat", health)
            self.assertEqual(health["heartbeat"]["interval_seconds"], 33)
            self.assertEqual(health["heartbeat"]["subscribers_count"], 0)


if __name__ == "__main__":
    unittest.main()

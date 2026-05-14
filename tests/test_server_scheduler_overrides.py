"""Focused daemon server scheduler override tests for #10427."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from agent.core import _apply_reasoning_effort_override
from agent.runtime_utils import current_session_env_overlay
from daemon.core import Session, SessionEvent
from daemon.protocol import encode_envelope
from daemon.scheduler import Scheduler, _utc_now
from daemon.server import DaemonServer


class _FakeBus:
    def __init__(self, url: str, agent_name: str):
        self.url = url
        self.agent_name = agent_name
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


class _DefaultRunner:
    def __init__(self, *, label: str = "default") -> None:
        self.label = label
        self.chat_history = []
        self.auto_mode_calls: list[tuple[bool, int]] = []

    async def run(self, user_input: str, **_kwargs):
        yield {"type": "final", "data": f"{self.label}:{user_input}"}

    def set_auto_mode(self, enabled: bool, max_iterations: int = 40):
        self.auto_mode_calls.append((enabled, max_iterations))

    def consume_auto_pause_reason(self):
        return None


class _OverrideRunner(_DefaultRunner):
    instances: list["_OverrideRunner"] = []

    def __init__(
        self,
        *,
        tools=None,
        bus=None,
        agent_name=None,
        reasoning_effort_override=None,
    ) -> None:
        del tools, bus
        super().__init__(label="override")
        self.agent_name = agent_name
        self.reasoning_effort_override = reasoning_effort_override
        self.env_seen: dict[str, str] = {}
        self.__class__.instances.append(self)

    async def run(self, user_input: str, **_kwargs):
        self.env_seen = current_session_env_overlay()
        yield {
            "type": "final",
            "data": (
                f"override:{self.agent_name}:{self.reasoning_effort_override}:"
                f"{self.env_seen.get('KAI_TEST_MODE')}:{user_input}"
            ),
        }


def _fake_attach_runtime(
    session,
    *,
    bus=None,
    agent_name="kai",
    signal_consumer=None,
    scheduler=None,
):
    del bus, signal_consumer, scheduler
    runner = _DefaultRunner()
    session.agent_runner = runner
    session.agent_name = agent_name
    runner.chat_history = session.chat_history
    return runner


class ServerSchedulerOverrideTests(unittest.IsolatedAsyncioTestCase):
    def test_agent_runner_reasoning_override_updates_primary_and_fallback_configs(self):
        cfg = {
            "endpoint": {"provider": "codex-cli", "model": "gpt-5.5"},
            "fallback_endpoints": [
                {"provider": "openai", "model": "gpt-5.4"},
            ],
        }

        updated = _apply_reasoning_effort_override(cfg, "x-high")

        self.assertEqual(updated["endpoint"]["reasoning_effort"], "xhigh")
        self.assertEqual(updated["fallback_endpoints"][0]["reasoning_effort"], "xhigh")
        self.assertEqual(updated["fallback_endpoint"]["reasoning_effort"], "xhigh")
        self.assertNotIn("reasoning_effort", cfg["endpoint"])

    async def test_scheduled_job_override_uses_per_call_runner_inside_owner_session(self):
        _OverrideRunner.instances.clear()
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
            ), mock.patch(
                "daemon.server.Session.attach_runtime",
                autospec=True,
                side_effect=_fake_attach_runtime,
            ), mock.patch(
                "daemon.server.AgentRunner",
                _OverrideRunner,
            ), mock.patch(
                "agent.sub_agents.SubAgentManager.spawn",
                autospec=True,
            ) as spawn:
                server = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=_FakeBus,
                    scheduler_factory=scheduler_factory,
                )
                await server.startup()
                try:
                    managed = await server.get_or_create_session(
                        "owner",
                        create_if_missing=True,
                    )
                    when = (_utc_now() + timedelta(minutes=1)).replace(microsecond=0)
                    server.scheduler.schedule_job(
                        {
                            "id": "job-override",
                            "type": "absolute",
                            "spec": {"at": when.isoformat()},
                            "prompt": "Check BTC",
                            "owner_session": "owner",
                            "created_at": _utc_now().replace(microsecond=0).isoformat(),
                            "created_by": "agent",
                            "target_agent_role": "analyst",
                            "reasoning_effort": "xhigh",
                            "extra_env": {"KAI_TEST_MODE": "1"},
                        },
                        persist=False,
                    )

                    job = server.scheduler.get_job("job-override")
                    await server._handle_scheduled_job_trigger(job, _utc_now())

                    updated = server.scheduler.get_job("job-override")
                    self.assertEqual(updated.status, "completed")
                    self.assertEqual(
                        updated.last_result_preview,
                        "override:analyst:xhigh:1:Check BTC",
                    )
                    self.assertEqual(managed.session.agent_name, "kai")
                    self.assertIsInstance(managed.session.agent_runner, _DefaultRunner)
                    self.assertEqual(len(_OverrideRunner.instances), 1)
                    self.assertEqual(_OverrideRunner.instances[0].agent_name, "analyst")
                    self.assertEqual(
                        _OverrideRunner.instances[0].reasoning_effort_override,
                        "xhigh",
                    )
                    self.assertEqual(
                        _OverrideRunner.instances[0].env_seen.get("KAI_TEST_MODE"),
                        "1",
                    )
                    self.assertFalse(spawn.called)
                finally:
                    await server.shutdown()

    async def test_scheduled_job_without_overrides_uses_default_owner_runner(self):
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
            ), mock.patch(
                "daemon.server.Session.attach_runtime",
                autospec=True,
                side_effect=_fake_attach_runtime,
            ), mock.patch(
                "daemon.server.AgentRunner",
                side_effect=AssertionError("override runner should not be created"),
            ):
                server = DaemonServer(
                    agent_name="kai",
                    nats_url="nats://unit-test",
                    bus_factory=_FakeBus,
                    scheduler_factory=scheduler_factory,
                )
                await server.startup()
                try:
                    await server.get_or_create_session("owner", create_if_missing=True)
                    when = (_utc_now() + timedelta(minutes=1)).replace(microsecond=0)
                    server.scheduler.schedule_job(
                        {
                            "id": "job-default",
                            "type": "absolute",
                            "spec": {"at": when.isoformat()},
                            "prompt": "Check BTC",
                            "owner_session": "owner",
                            "created_at": _utc_now().replace(microsecond=0).isoformat(),
                            "created_by": "agent",
                        },
                        persist=False,
                    )

                    job = server.scheduler.get_job("job-default")
                    await server._handle_scheduled_job_trigger(job, _utc_now())

                    updated = server.scheduler.get_job("job-default")
                    self.assertEqual(updated.status, "completed")
                    self.assertEqual(updated.last_result_preview, "default:Check BTC")
                finally:
                    await server.shutdown()

    def test_websocket_scheduled_job_envelopes_include_overrides_only_when_set(self):
        server = DaemonServer(
            agent_name="kai",
            nats_url="nats://unit-test",
            bus_factory=None,
        )
        session = Session("terminal")

        with_overrides = server._event_to_message(
            session=session,
            event=SessionEvent(
                session_name="terminal",
                topic="scheduled_job.triggered",
                payload={
                    "job_id": "job-override",
                    "fired_at": "2026-04-10T00:00:00+00:00",
                    "target_agent_role": "analyst",
                    "reasoning_effort": "xhigh",
                    "extra_env": {"KAI_TEST_MODE": "1"},
                },
            ),
            subscriptions={},
            tool_start_times={},
        )
        encoded = encode_envelope(with_overrides)
        self.assertEqual(encoded["target_agent_role"], "analyst")
        self.assertEqual(encoded["reasoning_effort"], "xhigh")
        self.assertEqual(encoded["extra_env"], {"KAI_TEST_MODE": "1"})

        without_overrides = server._event_to_message(
            session=session,
            event=SessionEvent(
                session_name="terminal",
                topic="scheduled_job.triggered",
                payload={
                    "job_id": "job-default",
                    "fired_at": "2026-04-10T00:00:00+00:00",
                },
            ),
            subscriptions={},
            tool_start_times={},
        )
        encoded_default = encode_envelope(without_overrides)
        self.assertNotIn("target_agent_role", encoded_default)
        self.assertNotIn("reasoning_effort", encoded_default)
        self.assertNotIn("thinking_level", encoded_default)
        self.assertNotIn("extra_env", encoded_default)


if __name__ == "__main__":
    unittest.main()

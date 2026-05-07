from __future__ import annotations

import asyncio
import unittest
from collections import deque
from dataclasses import dataclass, field

from daemon.event_injector import EventInjectionPolicy, EventInjectionRequest, EventInjectionTemplate, EventInjector


class _Runner:
    def __init__(self) -> None:
        self.chat_history = []
        self._is_auto_continuation = False
        self.tool_call_active = False


class _Session:
    def __init__(self) -> None:
        self.name = "unit"
        self.agent_name = "kai"
        self.auto_mode = True
        self.subscribed = True
        self.agent_runner = _Runner()
        self.chat_history = []
        self.events = []
        self._event_turn_active = False
        self.event_injection_timestamps = deque()

    def publish_event(self, topic, payload):
        self.events.append((topic, payload))


@dataclass
class _Managed:
    session: _Session
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_input_task: asyncio.Task | None = None


class EventInjectorTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, template=None):
        return EventInjectionRequest(
            event={"seq": 7},
            template=template or EventInjectionTemplate("unit.tmpl", __file__, "hello {name}"),
            policy=EventInjectionPolicy(
                source="unit",
                drop_topic="auto.unit_dropped",
                injected_topic="auto.unit_injected",
                active_attr="_event_turn_active",
                timestamp_attr="event_injection_timestamps",
                max_injected_turns_per_hour=1,
                require_subscription_attr="subscribed",
                active_reason="unit_turn_active",
            ),
            render_values={"name": "kai"},
            seq=7,
            monotonic_seconds=100.0,
            job_id="unit:7",
            task_name="unit-task",
        )

    async def test_run_turn_appends_human_message_and_invokes_target_session(self):
        calls = []

        async def run_input(managed, prompt, **kwargs):
            calls.append((managed.session.name, prompt, kwargs))

        managed = _Managed(_Session())
        injector = EventInjector(run_input=run_input)

        await injector.run_turn(managed, self._request())

        self.assertEqual(managed.session.chat_history[-1].content, "hello kai")
        self.assertEqual(calls[0][1], "hello kai")
        self.assertTrue(calls[0][2]["pre_injected_input"])
        self.assertEqual(managed.session.events[-1][0], "auto.unit_injected")
        self.assertEqual(managed.session.events[-1][1]["seq"], 7)
        self.assertFalse(managed.session._event_turn_active)
        self.assertEqual(list(managed.session.event_injection_timestamps), [100.0])

    async def test_decision_drops_when_rate_limited(self):
        async def run_input(*_args, **_kwargs):
            raise AssertionError("should not run")

        session = _Session()
        session.event_injection_timestamps.append(99.0)
        managed = _Managed(session)
        injector = EventInjector(run_input=run_input)
        request = self._request()

        decision = await injector.handle(managed, request)

        self.assertFalse(decision.ok)
        self.assertEqual(decision.reason, "rate_limited")
        self.assertEqual(session.events[-1], ("auto.unit_dropped", {"seq": 7, "reason": "rate_limited"}))
        self.assertEqual(session.chat_history, [])

    async def test_template_render_failure_drops_without_input(self):
        async def run_input(*_args, **_kwargs):
            raise AssertionError("should not run")

        class _BadTemplate(EventInjectionTemplate):
            def render_map(self, _values):
                raise RuntimeError("boom")

        session = _Session()
        managed = _Managed(session)
        injector = EventInjector(run_input=run_input)

        await injector.run_turn(managed, self._request(_BadTemplate("bad", __file__, "")))

        self.assertEqual(session.events[-1], ("auto.unit_dropped", {"seq": 7, "reason": "template_render_failed"}))
        self.assertEqual(session.chat_history, [])
        self.assertFalse(session._event_turn_active)

    async def test_handle_template_render_failure_clears_active_flag(self):
        async def run_input(*_args, **_kwargs):
            raise AssertionError("should not run")

        class _BadTemplate(EventInjectionTemplate):
            def render_map(self, _values):
                raise RuntimeError("boom")

        session = _Session()
        managed = _Managed(session)
        injector = EventInjector(run_input=run_input)
        request = self._request(_BadTemplate("bad", __file__, ""))

        decision = await injector.handle(managed, request)
        self.assertTrue(decision.ok)
        for _ in range(20):
            if not session._event_turn_active:
                break
            await asyncio.sleep(0.01)

        self.assertFalse(session._event_turn_active)
        self.assertEqual(session.events[-1], ("auto.unit_dropped", {"seq": 7, "reason": "template_render_failed"}))
        self.assertEqual(session.chat_history, [])


if __name__ == "__main__":
    unittest.main()

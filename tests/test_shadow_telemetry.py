from __future__ import annotations

import asyncio

from daemon.scheduler import DaemonEventBus
from daemon.signal_router import ActionDescriptor, Route, SignalRouter
from daemon.signal_router.actions.base import ActionResult
from daemon.signal_router.shadow import ShadowRunner


class StatusExecutor:
    kind = "fake"

    def __init__(self, status: str, detail: str | None, metrics=None) -> None:
        self.status = status
        self.detail = detail
        self.metrics = dict(metrics or {})

    def validate(self, action):
        return []

    def execute(self, action, envelope, context):
        return ActionResult(action.kind, action.target, self.status, self.detail, self.metrics)


def _router(tmp_path, executor) -> SignalRouter:
    route = Route(
        name="r1",
        channel="trade_signals",
        match={},
        actions=[ActionDescriptor("fake", "target", {})],
        pre_action=None,
        enabled=True,
    )
    router = SignalRouter(
        {"mode": "shadow", "dedup_table_path": str(tmp_path / "d.sqlite3")},
        routes=[route],
    )
    router.action_executors = {"fake": executor}
    return router


def _envelope() -> dict:
    return {"subject": "signals.any.BTC", "channel": "trade_signals", "payload": {"symbol": "BTC"}}


def test_shadow_would_fire_event_name_emitted(tmp_path) -> None:
    events = []
    runner = ShadowRunner(
        _router(tmp_path, StatusExecutor("suppressed_dry_run", "would_fire", {"would_fire": True})),
        telemetry_emitter=lambda topic, payload: events.append((topic, payload)),
    )

    asyncio.run(runner.process_envelope(_envelope(), lambda _envelope: True))

    assert "auto.signal_router.shadow.fake.would_fire" in [topic for topic, _ in events]


def test_shadow_suppressed_reason_event_name_emitted(tmp_path) -> None:
    events = []
    runner = ShadowRunner(
        _router(tmp_path, StatusExecutor("skipped", "cooldown")),
        telemetry_emitter=lambda topic, payload: events.append((topic, payload)),
    )

    asyncio.run(runner.process_envelope(_envelope(), lambda _envelope: False))

    assert "auto.signal_router.shadow.fake.suppressed_cooldown" in [topic for topic, _ in events]


def test_shadow_diff_event_name_emitted_on_divergence(tmp_path) -> None:
    events = []
    runner = ShadowRunner(
        _router(tmp_path, StatusExecutor("suppressed_dry_run", "would_fire", {"would_fire": True})),
        telemetry_emitter=lambda topic, payload: events.append((topic, payload)),
    )

    asyncio.run(runner.process_envelope(_envelope(), lambda _envelope: False))

    assert "auto.signal_router.shadow.diff" in [topic for topic, _ in events]


def test_shadow_telemetry_delivers_to_daemon_event_bus(tmp_path) -> None:
    bus = DaemonEventBus()
    delivered = []
    bus.subscribe(lambda channel, payload: delivered.append((channel, payload)))
    runner = ShadowRunner(
        _router(tmp_path, StatusExecutor("suppressed_dry_run", "would_fire", {"would_fire": True})),
        telemetry_emitter=bus.publish,
    )

    asyncio.run(runner.process_envelope(_envelope(), lambda _envelope: True))
    asyncio.run(asyncio.sleep(0))

    assert delivered[-1][0] == "auto.signal_router.shadow.fake.would_fire"

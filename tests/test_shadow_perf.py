from __future__ import annotations

import asyncio
import statistics
import time

from daemon.signal_router import ActionDescriptor, Route, SignalRouter
from daemon.signal_router.actions.base import ActionResult
from daemon.signal_router.shadow import ShadowRunner


class FastExecutor:
    kind = "fast"

    def validate(self, action):
        return []

    def execute(self, action, envelope, context):
        return ActionResult(action.kind, action.target, "suppressed_dry_run", "would_fire", {"would_fire": True})


def _router(tmp_path) -> SignalRouter:
    route = Route(
        name="fast-route",
        channel="trade_signals",
        match={},
        actions=[ActionDescriptor("fast", "noop", {})],
        pre_action=None,
        enabled=True,
    )
    router = SignalRouter(
        {"mode": "shadow", "dedup_table_path": str(tmp_path / "d.sqlite3")},
        routes=[route],
    )
    router.action_executors = {"fast": FastExecutor()}
    return router


def test_shadow_added_latency_p99_under_5ms(tmp_path) -> None:
    runner = ShadowRunner(_router(tmp_path))
    envelopes = [
        {
            "subject": f"signals.synthetic.BTC{i}",
            "channel": "trade_signals",
            "payload": {"symbol": f"BTC{i}", "signal_type": "BUY"},
        }
        for i in range(1000)
    ]
    latencies = []

    for envelope in envelopes:
        started = time.perf_counter()
        asyncio.run(runner.process_envelope(envelope, lambda _envelope: True))
        latencies.append((time.perf_counter() - started) * 1000)

    p99 = statistics.quantiles(latencies, n=100)[98]

    assert p99 < 5.0

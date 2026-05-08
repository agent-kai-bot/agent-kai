from __future__ import annotations

import asyncio
import time
from typing import Any

from daemon.signal_router import (
    ActionDescriptor,
    ExecutionContext,
    MatchResult,
    Route,
    RouteDecision,
    SignalRouter,
)
from daemon.signal_router.actions.base import ActionResult
from daemon.signal_router.actions.spawn_agent import SpawnAgentExecutor
from daemon.signal_router.dedup_table import RouterDedupTable
from daemon.signal_router.diff_metrics import DivergenceKind
from daemon.signal_router.shadow import LegacyDecision, ShadowRunner


class FakeExecutor:
    kind = "fake"

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[ExecutionContext] = []
        self.side_effects = 0

    def validate(self, action):
        return []

    def execute(self, action, envelope, context):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(context)
        if not context.dry_run:
            self.side_effects += 1
        return ActionResult(action.kind, action.target, "suppressed_dry_run", "would_fire", {"would_fire": True})


class FakeAuditWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, decision: dict[str, Any], path_template: str | None = None) -> None:
        self.rows.append({**decision, "path_template": path_template})


def _route(action: ActionDescriptor | None = None) -> Route:
    return Route(
        name="r1",
        channel="trade_signals",
        match={"symbol": "BTC"},
        actions=[action or ActionDescriptor("fake", "target", {})],
        pre_action=None,
        enabled=True,
    )


def _router(tmp_path, route: Route | None = None) -> SignalRouter:
    return SignalRouter(
        {"mode": "shadow", "dedup_table_path": str(tmp_path / "d.sqlite3")},
        routes=[route or _route()],
    )


def _envelope() -> dict[str, Any]:
    return {
        "subject": "signals.clucmay02.BTC",
        "channel": "trade_signals",
        "payload": {"symbol": "BTC", "signal_type": "BUY"},
    }


def test_shadow_runner_only_runs_in_shadow_mode(tmp_path) -> None:
    router = SignalRouter({"mode": "legacy", "dedup_table_path": str(tmp_path / "d.sqlite3")}, routes=[_route()])
    router.decide = lambda envelope: (_ for _ in ()).throw(AssertionError("router should be dormant"))  # type: ignore[method-assign]
    legacy_calls = []
    runner = ShadowRunner(router, mode="legacy")

    result = asyncio.run(
        runner.process_envelope(_envelope(), lambda envelope: legacy_calls.append(envelope))
    )

    assert result.ran_shadow is False
    assert legacy_calls == [_envelope()]


def test_legacy_and_router_dispatch_in_parallel(tmp_path) -> None:
    router = _router(tmp_path)
    executor = FakeExecutor(delay=0.05)
    router.action_executors = {"fake": executor}
    runner = ShadowRunner(router)

    def legacy(_envelope):
        time.sleep(0.05)
        return LegacyDecision(fired=True)

    started = time.perf_counter()
    result = asyncio.run(runner.process_envelope(_envelope(), legacy))
    elapsed = time.perf_counter() - started

    assert result.ran_shadow is True
    assert elapsed < 0.09
    assert result.diff_metrics[0].divergence_kind == DivergenceKind.AGREED_FIRE


def test_router_executors_called_with_dry_run_true(tmp_path) -> None:
    router = _router(tmp_path)
    executor = FakeExecutor()
    router.action_executors = {"fake": executor}
    runner = ShadowRunner(router)

    asyncio.run(runner.process_envelope(_envelope(), lambda _envelope: True))

    assert executor.calls
    assert all(context.dry_run for context in executor.calls)


def test_no_duplicate_side_effects_from_router_executor(tmp_path) -> None:
    router = _router(tmp_path)
    executor = FakeExecutor()
    router.action_executors = {"fake": executor}
    legacy_side_effects = []
    runner = ShadowRunner(router)

    asyncio.run(
        runner.process_envelope(
            _envelope(),
            lambda _envelope: legacy_side_effects.append("legacy"),
        )
    )

    assert legacy_side_effects == ["legacy"]
    assert executor.side_effects == 0


def test_shadow_decision_writes_separate_audit_row(tmp_path) -> None:
    router = _router(tmp_path)
    router.action_executors = {"fake": FakeExecutor()}
    audit = FakeAuditWriter()
    runner = ShadowRunner(router, audit_writer=audit, audit_path_template="/tmp/router_shadow_{date}.jsonl")

    asyncio.run(runner.process_envelope(_envelope(), lambda _envelope: True))

    assert audit.rows[0]["mode"] == "shadow"
    assert audit.rows[0]["route"] == "r1"
    assert audit.rows[0]["decision"] == "would_fire"
    assert audit.rows[0]["path_template"] == "/tmp/router_shadow_{date}.jsonl"


def test_spawn_agent_shadow_does_not_call_sub_agent_manager_spawn(tmp_path) -> None:
    class Manager:
        def __init__(self) -> None:
            self.spawn_calls = []

        async def spawn(self, role_name: str) -> None:
            self.spawn_calls.append(role_name)

    manager = Manager()
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    action = ActionDescriptor(
        "spawn_agent",
        None,
        {"pack": "kai-alert-response", "cooldown_seconds": 0, "timeout_seconds": 1},
    )
    router = _router(tmp_path, _route(action))
    router.action_executors = {
        "spawn_agent": SpawnAgentExecutor(
            sub_agent_manager=manager,
            dedup_table=table,
        )
    }
    runner = ShadowRunner(
        router,
        context_factory=lambda envelope: ExecutionContext(
            dedup_table=table,
            sub_agent_manager=manager,
        ),
    )

    asyncio.run(runner.process_envelope(_envelope(), lambda _envelope: True))

    assert manager.spawn_calls == []


def test_router_decide_builds_route_decision(tmp_path) -> None:
    router = _router(tmp_path)

    decision = router.route(_envelope())

    assert isinstance(decision, RouteDecision)
    assert decision.route_name == "r1"
    assert decision.match_result == MatchResult(True, "matched", {"route_name": "r1"})

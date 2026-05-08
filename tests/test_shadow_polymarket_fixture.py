from __future__ import annotations

import asyncio
from typing import Any

from daemon.signal_router import ActionDescriptor, ExecutionContext, Route, SignalRouter
from daemon.signal_router.actions.spawn_agent import SpawnAgentExecutor
from daemon.signal_router.dedup_table import RouterDedupTable
from daemon.signal_router.diff_metrics import DivergenceKind
from daemon.signal_router.shadow import ShadowRunner


class FakeSubAgentManager:
    def __init__(self) -> None:
        self.spawn_calls: list[str] = []

    async def spawn(self, role_name: str) -> str:
        self.spawn_calls.append(role_name)
        return f"spawned {role_name}"


class FakeAuditWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, decision: dict[str, Any], path_template: str | None = None) -> None:
        self.rows.append({**decision, "path_template": path_template})


def _payload() -> dict[str, Any]:
    return {
        "rule_id": "cross_above_0_65",
        "token_id": "8241718812592733105087127440430758937570795867372479359494709768084953782222",
        "title": "Polymarket edge crossed above 0.65",
        "summary": "Sentinel matched on candidate market.",
        "severity": "critical",
    }


def _envelope() -> dict[str, Any]:
    return {
        "subject": "polymarket.alpha.alarm.cross_above_0_65",
        "payload": _payload(),
    }


def test_polymarket_fire_alarm_shadow_would_spawn_without_spawning(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    manager = FakeSubAgentManager()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    audit = FakeAuditWriter()
    action = ActionDescriptor(
        kind="spawn_agent",
        target=None,
        params={
            "pack": "kai-alert-response",
            "timeout_seconds": 300,
            "cooldown_key_template": "{rule_id}:{token_id}",
            "cooldown_seconds": 0,
            "daily_cap": 50,
            "hourly_cap": 0,
        },
    )
    route = Route(
        name="polymarket-alarm-response",
        channel="polymarket_alarms",
        match={"rule_id": "cross_above_0_65"},
        actions=[action],
        pre_action=None,
        enabled=True,
        config={"subject_pattern": "polymarket.alpha.alarm.>"},
    )
    router = SignalRouter(
        {"mode": "shadow", "dedup_table_path": str(tmp_path / "d.sqlite3")},
        routes=[route],
        dedup_table=table,
    )
    router.action_executors = {
        "spawn_agent": SpawnAgentExecutor(
            sub_agent_manager=manager,
            dedup_table=table,
            audit_writer=audit,
        )
    }
    legacy_dispatches = []
    runner = ShadowRunner(
        router,
        telemetry_emitter=lambda topic, payload: telemetry.append((topic, payload)),
        context_factory=lambda envelope: ExecutionContext(
            dedup_table=table,
            sub_agent_manager=manager,
        ),
    )

    result = asyncio.run(
        runner.process_envelope(
            _envelope(),
            lambda envelope: legacy_dispatches.append(envelope),
        )
    )

    assert legacy_dispatches == [_envelope()]
    assert result.router_evaluations[0].decision.route_name == "polymarket-alarm-response"
    assert any(topic == "auto.signal_router.shadow.spawn_agent.would_fire" for topic, _ in telemetry)
    assert manager.spawn_calls == []
    assert result.diff_metrics[0].divergence_kind == DivergenceKind.AGREED_FIRE
    assert runner.diff_store.snapshot()["rolling_1h"]["agreed_fire"] == 1

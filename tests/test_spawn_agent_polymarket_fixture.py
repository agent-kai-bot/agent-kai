from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from daemon.signal_router.actions.base import ExecutionContext
from daemon.signal_router.actions.spawn_agent import (
    STATUS_SUPPRESSED_COOLDOWN,
    STATUS_SUPPRESSED_DAILY_CAP,
    SpawnAgentExecutor,
)
from daemon.signal_router.agent_pack import default_agent_packs_dir, load_pack
from daemon.signal_router.dedup_table import RouterDedupTable
from daemon.signal_router.domain_model import ActionDescriptor


class FakeSubAgentManager:
    def __init__(self) -> None:
        self.spawn_calls: list[str] = []

    async def spawn(self, role_name: str) -> str:
        self.spawn_calls.append(role_name)
        return f"spawned {role_name}"


class FakeNatsRequest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def __call__(self, role_name: str, task: str, timeout_seconds: int = 0) -> str:
        self.calls.append((role_name, task, timeout_seconds))
        await asyncio.sleep(0)
        return "ok"


class FakeAuditWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, decision: dict[str, Any], path_template: str | None = None) -> None:
        self.rows.append({**decision, "path_template": path_template})


def _action() -> ActionDescriptor:
    return ActionDescriptor(
        kind="spawn_agent",
        target=None,
        params={
            "pack": "kai-alert-response",
            "timeout_seconds": 300,
            "cooldown_key_template": "{rule_id}:{token_id}",
            "cooldown_seconds": 600,
            "daily_cap": 50,
            "hourly_cap": 0,
            "audit_path_template": "/tmp/codex_audit/audit_{date}.jsonl",
            "env_passthrough": ["KAI_PUSHOVER_USER", "KAI_PUSHOVER_TOKEN", "KAI_NTFY_TOPIC"],
        },
    )


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": f"polymarket.alpha.alarm.{payload['rule_id']}",
        "channel": "polymarket_alarms",
        "payload": payload,
    }


def _context(table: RouterDedupTable, audit: FakeAuditWriter) -> ExecutionContext:
    return ExecutionContext(
        channel="polymarket_alarms",
        subject="polymarket.alpha.alarm.cross_above_0_65",
        route_name="polymarket-alarm-response",
        dedup_table=table,
        audit_writer=audit,
        telemetry_emitter=lambda topic, payload: None,
    )


def test_polymarket_fixture_fires_then_cooldown_and_daily_cap(tmp_path) -> None:
    pack_dir = default_agent_packs_dir() / "kai-alert-response"
    fixture_path = pack_dir / "example_alarms" / "01_cross_above_0_65_sentinel_match.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    pack = load_pack("kai-alert-response")
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    audit = FakeAuditWriter()
    manager = FakeSubAgentManager()
    nats = FakeNatsRequest()
    executor = SpawnAgentExecutor(
        manager,
        table,
        audit,
        lambda name: pack,
        nats_request=nats,
        role_registry={"kai-alert-response": {"system_prompt": pack.system_prompt}},
    )

    first = executor.execute(_action(), _envelope(payload), _context(table, audit))

    expected_key = (
        "cross_above_0_65:"
        "8241718812592733105087127440430758937570795867372479359494709768084953782222"
    )
    assert first.status == "fired"
    assert first.metrics["cooldown_key"] == expected_key
    assert manager.spawn_calls == ["kai-alert-response"]
    assert nats.calls[0][0] == "kai-alert-response"

    second = executor.execute(_action(), _envelope(payload), _context(table, audit))

    assert second.status == STATUS_SUPPRESSED_COOLDOWN
    assert manager.spawn_calls == ["kai-alert-response"]

    for index in range(1, 50):
        next_payload = {**payload, "token_id": f"different-token-{index}"}
        result = executor.execute(_action(), _envelope(next_payload), _context(table, audit))
        assert result.status == "fired"

    final_payload = {**payload, "token_id": "daily-cap-token-51"}
    final = executor.execute(_action(), _envelope(final_payload), _context(table, audit))

    assert final.status == STATUS_SUPPRESSED_DAILY_CAP
    assert len(manager.spawn_calls) == 50
    assert len(nats.calls) == 50

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from daemon.signal_router.actions.base import ExecutionContext
from daemon.signal_router.actions.spawn_agent import (
    STATUS_SUPPRESSED_COOLDOWN,
    STATUS_SUPPRESSED_DAILY_CAP,
    STATUS_SUPPRESSED_HOURLY_CAP,
    STATUS_TIMED_OUT,
    SpawnAgentExecutor,
)
from daemon.signal_router.agent_pack import AgentPack, AgentPackError
from daemon.signal_router.dedup_table import RouterDedupTable
from daemon.signal_router.domain_model import ActionDescriptor


class FakeSubAgentManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.spawn_calls: list[str] = []

    async def spawn(self, role_name: str) -> str:
        self.spawn_calls.append(role_name)
        if self.fail:
            raise RuntimeError("spawn boom")
        return f"spawned {role_name}"


class FakeNatsRequest:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[tuple[str, str, int]] = []

    async def __call__(self, role_name: str, task: str, timeout_seconds: int = 0) -> str:
        self.calls.append((role_name, task, timeout_seconds))
        if self.delay:
            await asyncio.sleep(self.delay)
        return "ok"


class FakeAuditWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, decision: dict[str, Any], path_template: str | None = None) -> None:
        self.rows.append({**decision, "path_template": path_template})


def _pack(name: str = "kai-alert-response") -> AgentPack:
    return AgentPack(
        name=name,
        root_path=Path("/tmp") / name,
        system_prompt="prompt",
        decision_logic="",
        tools_reference="",
        schema_path=None,
        manifest={},
    )


def _action(**overrides: Any) -> ActionDescriptor:
    params = {
        "pack": "kai-alert-response",
        "timeout_seconds": 5,
        "cooldown_key_template": "{rule_id}:{token_id}",
        "cooldown_seconds": 600,
        "daily_cap": 50,
        "hourly_cap": 10,
        "audit_path_template": "/tmp/audit_{date}.jsonl",
        "env_passthrough": [],
    }
    params.update(overrides)
    return ActionDescriptor(kind="spawn_agent", target=None, params=params)


def _envelope(rule_id: str = "cross_above_0_65", token_id: str = "824") -> dict[str, Any]:
    return {
        "subject": f"polymarket.alpha.alarm.{rule_id}",
        "channel": "polymarket_alarms",
        "payload": {"rule_id": rule_id, "token_id": token_id},
    }


def _context(
    table: RouterDedupTable,
    audit: FakeAuditWriter,
    telemetry: list[tuple[str, dict[str, Any]]],
    *,
    dry_run: bool = False,
) -> ExecutionContext:
    return ExecutionContext(
        dry_run=dry_run,
        channel="polymarket_alarms",
        subject="polymarket.alpha.alarm.cross_above_0_65",
        route_name="polymarket-alarm-response",
        dedup_table=table,
        audit_writer=audit,
        telemetry_emitter=lambda topic, payload: telemetry.append((topic, payload)),
    )


def test_validate_missing_pack_returns_validation_error() -> None:
    executor = SpawnAgentExecutor(pack_loader=lambda name: (_ for _ in ()).throw(AgentPackError("missing pack")))

    errors = executor.validate(_action())

    assert errors[0].field == "pack"
    assert "missing pack" in errors[0].message


def test_validate_bad_cooldown_and_caps_return_validation_errors() -> None:
    executor = SpawnAgentExecutor(pack_loader=lambda name: _pack(), role_registry={"kai-alert-response": {}})

    errors = executor.validate(
        _action(cooldown_seconds=-1, daily_cap=-2, hourly_cap=-3, timeout_seconds=0)
    )

    assert {error.field for error in errors} == {
        "cooldown_seconds",
        "daily_cap",
        "hourly_cap",
        "timeout_seconds",
    }


def test_validate_unknown_role_without_registrar_returns_validation_error() -> None:
    executor = SpawnAgentExecutor(
        pack_loader=lambda name: _pack(),
        role_registry={},
        role_registrar=None,
    )

    errors = executor.validate(_action())

    assert errors[0].field == "pack"
    assert "cannot be auto-registered" in errors[0].message


def test_execute_happy_path_spawns_requests_records_and_audits(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    audit = FakeAuditWriter()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    manager = FakeSubAgentManager()
    nats = FakeNatsRequest()
    executor = SpawnAgentExecutor(manager, table, audit, lambda name: _pack(), nats_request=nats)

    result = executor.execute(_action(), _envelope(), _context(table, audit, telemetry))

    assert result.status == "fired"
    assert manager.spawn_calls == ["kai-alert-response"]
    assert nats.calls[0][0] == "kai-alert-response"
    assert nats.calls[0][2] == 5
    assert table.check_daily_cap("polymarket-alarm-response", 1) is False
    assert audit.rows[-1]["decision"] == "fired"
    assert audit.rows[-1]["cooldown_key"] == "cross_above_0_65:824"
    assert [topic for topic, _ in telemetry] == [
        "auto.signal_router.spawn_agent.fired",
        "auto.signal_router.spawn_agent.exit_ok",
    ]


def test_execute_cooldown_hit_skips_spawn(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    assert table.check_and_record_cooldown("cross_above_0_65:824", 600) is True
    audit = FakeAuditWriter()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    manager = FakeSubAgentManager()
    executor = SpawnAgentExecutor(manager, table, audit, lambda name: _pack(), nats_request=FakeNatsRequest())

    result = executor.execute(_action(), _envelope(), _context(table, audit, telemetry))

    assert result.status == STATUS_SUPPRESSED_COOLDOWN
    assert manager.spawn_calls == []
    assert audit.rows[-1]["decision"] == STATUS_SUPPRESSED_COOLDOWN


def test_execute_daily_cap_hit_skips_spawn(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    table.record_fire("polymarket-alarm-response", None)
    audit = FakeAuditWriter()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    manager = FakeSubAgentManager()
    executor = SpawnAgentExecutor(manager, table, audit, lambda name: _pack(), nats_request=FakeNatsRequest())

    result = executor.execute(
        _action(daily_cap=1),
        _envelope(token_id="new-token"),
        _context(table, audit, telemetry),
    )

    assert result.status == STATUS_SUPPRESSED_DAILY_CAP
    assert manager.spawn_calls == []


def test_execute_hourly_cap_hit_skips_spawn(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    table.record_fire("polymarket-alarm-response", None)
    audit = FakeAuditWriter()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    manager = FakeSubAgentManager()
    executor = SpawnAgentExecutor(manager, table, audit, lambda name: _pack(), nats_request=FakeNatsRequest())

    result = executor.execute(
        _action(daily_cap=0, hourly_cap=1),
        _envelope(token_id="new-token"),
        _context(table, audit, telemetry),
    )

    assert result.status == STATUS_SUPPRESSED_HOURLY_CAP
    assert manager.spawn_calls == []


def test_execute_dry_run_skips_spawn(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    audit = FakeAuditWriter()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    manager = FakeSubAgentManager()
    executor = SpawnAgentExecutor(manager, table, audit, lambda name: _pack(), nats_request=FakeNatsRequest())

    result = executor.execute(
        _action(cooldown_seconds=0),
        _envelope(),
        _context(table, audit, telemetry, dry_run=True),
    )

    assert result.status == "suppressed_dry_run"
    assert manager.spawn_calls == []
    assert audit.rows[-1]["decision"] == "suppressed_dry_run"
    assert telemetry[-1][0] == "auto.signal_router.shadow.spawn_agent.would_fire"


def test_execute_spawn_failure_returns_failed_and_audits(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    audit = FakeAuditWriter()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    executor = SpawnAgentExecutor(
        FakeSubAgentManager(fail=True),
        table,
        audit,
        lambda name: _pack(),
        nats_request=FakeNatsRequest(),
    )

    result = executor.execute(_action(), _envelope(), _context(table, audit, telemetry))

    assert result.status == "failed"
    assert "spawn boom" in result.detail
    assert audit.rows[-1]["decision"] == "failed"
    assert telemetry[-1][0] == "auto.signal_router.spawn_agent.exit_failed"


def test_execute_nats_request_timeout_returns_timed_out(tmp_path) -> None:
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    audit = FakeAuditWriter()
    telemetry: list[tuple[str, dict[str, Any]]] = []
    executor = SpawnAgentExecutor(
        FakeSubAgentManager(),
        table,
        audit,
        lambda name: _pack(),
        nats_request=FakeNatsRequest(delay=2),
    )

    result = executor.execute(
        _action(timeout_seconds=1),
        _envelope(),
        _context(table, audit, telemetry),
    )

    assert result.status == STATUS_TIMED_OUT
    assert audit.rows[-1]["decision"] == STATUS_TIMED_OUT
    assert telemetry[-1][0] == "auto.signal_router.spawn_agent.timed_out"

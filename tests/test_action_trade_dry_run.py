from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from daemon.runtime_config_store import RuntimeConfigStore
from daemon.signal_router.actions.base import ActionResult, ExecutionContext
from daemon.signal_router.actions.trade import TradeExecutor
from daemon.signal_router.domain_model import ActionDescriptor


def _store(tmp_path: Path, *, live_trades_enabled: bool) -> RuntimeConfigStore:
    base_path = tmp_path / "agent-config.json"
    base_path.write_text(
        json.dumps({"daemon": {"signal_router": {"mode": "new", "routes": []}}}),
        encoding="utf-8",
    )
    store = RuntimeConfigStore(
        base_config_path=base_path,
        overrides_path=tmp_path / "runtime_config.json",
    )
    store.update_signal_router_live_trades_enabled(live_trades_enabled)
    return store


def _action() -> ActionDescriptor:
    return ActionDescriptor(
        kind="trade",
        target="autotrade",
        params={"side": "buy", "size_usd": 10},
    )


def _envelope() -> dict[str, Any]:
    return {
        "subject": "signals.BTC",
        "channel": "trade_signals",
        "payload": {"symbol": "BTC", "signal_type": "buy"},
    }


def test_trade_dry_run_emits_event_and_skips_adapter(tmp_path: Path) -> None:
    calls: list[str] = []
    telemetry: list[tuple[str, dict[str, Any]]] = []
    executor = TradeExecutor(
        _store(tmp_path, live_trades_enabled=False),
        execution_adapter=lambda action, envelope, context: calls.append("adapter"),
    )

    result = executor.execute(
        _action(),
        _envelope(),
        ExecutionContext(
            route_name="route-a",
            channel="trade_signals",
            autotrade_enabled=lambda: True,
            telemetry_emitter=lambda topic, payload: telemetry.append((topic, payload)),
        ),
    )

    assert result.status == "suppressed_dry_run"
    assert calls == []
    assert telemetry == [
        (
            "auto.signal_router.trade.dry_run",
            {
                "route": "route-a",
                "intended_action": {
                    "kind": "trade",
                    "target": "autotrade",
                    "params": {"side": "buy", "size_usd": 10},
                },
                "reason": "live_trades_disabled",
            },
        )
    ]


def test_trade_live_enabled_calls_adapter(tmp_path: Path) -> None:
    calls: list[str] = []

    def adapter(
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        calls.append(f"{action.kind}:{envelope['payload']['symbol']}:{context.route_name}")
        return ActionResult("trade", "autotrade", "fired", "adapter_ok", {})

    executor = TradeExecutor(
        _store(tmp_path, live_trades_enabled=True),
        execution_adapter=adapter,
    )

    result = executor.execute(
        _action(),
        _envelope(),
        ExecutionContext(
            route_name="route-a",
            channel="trade_signals",
            autotrade_enabled=lambda: True,
        ),
    )

    assert result.status == "fired"
    assert result.detail == "adapter_ok"
    assert calls == ["trade:BTC:route-a"]

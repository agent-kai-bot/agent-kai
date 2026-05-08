from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.registry import EXECUTORS
from daemon.signal_router.actions.trade import TradeExecutor


def test_trade_requires_autotrade_gate() -> None:
    result = TradeExecutor().execute(
        ActionDescriptor(kind="trade", target="autotrade", params={}),
        {"payload": {"symbol": "BTC", "signal_type": "BUY"}},
        ExecutionContext(autotrade_enabled=lambda: False),
    )

    assert result.status == "skipped"
    assert result.detail == "autotrade_disabled"


def test_trade_emits_diff_metric_stub() -> None:
    telemetry = []

    result = TradeExecutor().execute(
        ActionDescriptor(kind="trade", target="autotrade", params={}),
        {"payload": {"symbol": "BTC", "signal_type": "BUY"}},
        ExecutionContext(
            autotrade_enabled=lambda: True,
            telemetry_emitter=lambda topic, payload: telemetry.append((topic, payload)),
        ),
    )

    assert result.status == "fired"
    assert result.metrics["diff_metric_stub"] is True
    assert telemetry[0][0] == "auto.signal_router.trade.diff_metric_stub"


def test_buy_signal_alone_is_not_inferred_as_trade() -> None:
    assert "BUY" not in EXECUTORS
    assert EXECUTORS["trade"].kind == "trade"

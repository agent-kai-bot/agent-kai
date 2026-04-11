"""Tests for session-scoped ASO strategy agent tools."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.strategy_agent_tools import (
    get_strategy_lineage,
    get_strategy_report,
    list_strategies,
    move_strategy,
    optimizer_pause,
    optimizer_report,
    optimizer_start,
    optimizer_status,
    propose_strategy,
    render_strategy_command_result,
    show_strategy,
)
from agent.strategy_ir import (
    Condition,
    ConditionOperator,
    ConstantValue,
    CostsSpec,
    EntrySpec,
    ExitSpec,
    IndicatorSpec,
    IndicatorType,
    RiskSpec,
    StrategyIR,
    TimeExitSpec,
)
from agent.strategy_metrics import (
    BenchmarkMetrics,
    CostAnalysisMetrics,
    DrawdownMetrics,
    MetricsReport,
    ReturnMetrics,
    RiskAdjustedMetrics,
    StabilityMetrics,
    TailRiskMetrics,
    TradeMetrics,
)
from agent.strategy_store import DEFAULT_DB_PATH, StrategyStore


def _build_ir(name: str) -> StrategyIR:
    return StrategyIR(
        name=name,
        symbol="BTC-USD",
        timeframe="1h",
        indicators=[IndicatorSpec(type=IndicatorType.SMA, period=5, alias="sma_fast")],
        entry=EntrySpec(
            conditions=[
                Condition(
                    indicator="sma_fast",
                    operator=ConditionOperator.ABOVE,
                    target=ConstantValue(value=100.0),
                )
            ]
        ),
        exit=ExitSpec(time_exit=TimeExitSpec(max_bars=3)),
        risk=RiskSpec(max_position_pct=0.1, max_drawdown_pct=0.2),
        costs=CostsSpec(),
        max_warmup=5,
        warmup_bars=StrategyIR.compute_warmup_bars(5),
    )


def _metrics(sharpe: float = 1.2, trades: int = 64) -> MetricsReport:
    return MetricsReport(
        returns=ReturnMetrics(total_pct=10.0, annualized_pct=15.0, monthly_returns={"2025-01": 2.0}),
        risk_adjusted=RiskAdjustedMetrics(sharpe_ratio=sharpe, sortino_ratio=1.5, calmar_ratio=0.8),
        drawdown=DrawdownMetrics(
            max_drawdown_pct=-7.0,
            max_drawdown_duration_days=3.0,
            avg_drawdown_pct=-2.0,
            recovery_factor=1.2,
            underwater_curve=[0.0, -1.0, 0.0],
        ),
        trades=TradeMetrics(
            total=trades,
            winners=38,
            losers=26,
            win_rate_pct=59.0,
            profit_factor=1.4,
            avg_win_pct=2.1,
            avg_loss_pct=-1.2,
            largest_win_pct=5.0,
            largest_loss_pct=-4.0,
            avg_duration_bars=2.0,
        ),
        benchmark=BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=4.0,
            alpha_pct=6.0,
            beta=0.2,
            correlation=0.1,
        ),
        tail_risk=TailRiskMetrics(cvar_95_pct=-2.0, cvar_99_pct=-3.0, time_under_water_bars=8, time_under_water_pct=12.0),
        stability=StabilityMetrics(monthly_return_stddev=1.0, positive_months_pct=66.0, longest_losing_streak=2),
        cost_analysis=CostAnalysisMetrics(gross_return_pct=12.0, net_return_pct=10.0, fee_burden_pct_of_gross=6.0),
    )


class _FakeRuntime:
    def __init__(self) -> None:
        self.started_with: list[int] = []
        self.pause_calls = 0
        self.running = False
        self.paused = False
        self.last_cycle_result = {
            "status": "accepted",
            "reason": "accepted: child improved walk-forward Sharpe and passed lockbox",
            "completed_at": "2026-04-11T10:00:00+00:00",
        }
        self.cycles = [self.last_cycle_result]

    def optimizer_state(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "started_at": "2026-04-11T09:00:00+00:00" if self.started_with else None,
            "last_completed_at": "2026-04-11T10:00:00+00:00" if self.cycles else None,
            "last_stop_reason": "completed" if self.cycles else None,
            "last_error": None,
            "requested_cycles": self.started_with[-1] if self.started_with else None,
            "last_cycle_result": self.last_cycle_result if self.cycles else None,
        }

    def start_optimizer(self, store: StrategyStore, max_cycles: int) -> dict:
        del store
        self.started_with.append(max_cycles)
        self.running = True
        self.paused = False
        return {"ok": True, "kind": "optimizer_start", "max_cycles": max_cycles, "running": True}

    def pause_optimizer(self) -> dict:
        self.pause_calls += 1
        self.running = False
        self.paused = True
        return {"ok": True, "kind": "optimizer_pause", "paused": True}

    def recent_cycle_results(self, limit: int = 5) -> list[dict]:
        return list(self.cycles[-limit:])


class StrategyAgentToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root_dir = Path(self.temp_dir.name) / "session"
        store_path = root_dir / "strategies" / DEFAULT_DB_PATH.name
        self.session = SimpleNamespace(
            name="alpha",
            paths=SimpleNamespace(root_dir=root_dir),
            strategy_store_path=str(store_path),
            strategy_runtime=_FakeRuntime(),
            publish_event=lambda *_args, **_kwargs: None,
        )
        self.store = StrategyStore(store_path)
        self.store.init_db()

        self.root_id = self.store.save_strategy(
            _build_ir("momentum"),
            "momentum",
            1,
            pool="candidates",
            created_by="human",
            yaml_source="name: momentum",
        )
        self.store.save_run(
            self.root_id,
            "in_sample",
            None,
            "hash-1",
            _metrics(sharpe=1.1),
            True,
        )
        self.child_id = self.store.save_strategy(
            _build_ir("momentum"),
            "momentum",
            2,
            parent_id=self.root_id,
            pool="active",
            created_by="optimizer",
        )
        self.store.save_run(
            self.child_id,
            "walk_forward",
            0,
            "hash-2",
            _metrics(sharpe=1.6),
            True,
        )
        self.store.save_mutation(
            self.root_id,
            self.child_id,
            [{"path": "risk.max_position_pct", "old": 0.1, "new": 0.08}],
            accepted=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_strategies_includes_latest_run_summary(self) -> None:
        result = list_strategies(self.session, pool="all")

        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "list_strategies")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["strategies"][1]["latest_run"]["sharpe_ratio"], 1.6)

    def test_show_strategy_returns_yaml_metrics_and_lineage_summary(self) -> None:
        result = show_strategy(self.session, "momentum")

        self.assertTrue(result["ok"])
        strategy = result["strategy"]
        self.assertEqual(strategy["version"], 2)
        self.assertIn("symbol: BTC-USD", strategy["yaml"])
        self.assertEqual(strategy["latest_run"]["stage"], "walk_forward")
        self.assertEqual(strategy["lineage_summary"]["versions"], 2)

    def test_propose_strategy_compiles_and_saves_candidate(self) -> None:
        yaml_str = """
name: breakout
symbol: BTC-USD
timeframe: 1h
indicators:
  - type: sma
    period: 10
    alias: sma_fast
entry:
  conditions:
    - indicator: sma_fast
      operator: above
      value: 100
exit:
  time_exit:
    max_bars: 2
risk:
  max_position_pct: 0.1
  max_drawdown_pct: 0.2
"""

        result = propose_strategy(self.session, yaml_str)
        listed = list_strategies(self.session, pool="candidates")

        self.assertTrue(result["ok"])
        self.assertEqual(result["strategy"]["name"], "breakout")
        self.assertTrue(any(item["name"] == "breakout" for item in listed["strategies"]))

    def test_optimizer_status_start_pause_and_report_return_expected_shapes(self) -> None:
        started = optimizer_start(self.session, max_cycles=3)
        status = optimizer_status(self.session)
        paused = optimizer_pause(self.session)
        report = optimizer_report(self.session, limit=5)

        self.assertTrue(started["ok"])
        self.assertEqual(started["max_cycles"], 3)
        self.assertTrue(status["ok"])
        self.assertIn("pool_counts", status)
        self.assertTrue(paused["ok"])
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["cycles"]), 1)

    def test_strategy_lineage_report_and_pool_moves(self) -> None:
        lineage = get_strategy_lineage(self.session, "momentum")
        report = get_strategy_report(self.session, "momentum")
        demoted = move_strategy(self.session, "momentum", "candidates")
        rendered = render_strategy_command_result(report)

        self.assertTrue(lineage["ok"])
        self.assertEqual(len(lineage["versions"]), 2)
        self.assertTrue(report["ok"])
        self.assertEqual(report["best_version"]["version"], 2)
        self.assertTrue(demoted["ok"])
        self.assertIn("Strategy report for momentum", rendered)


if __name__ == "__main__":
    unittest.main()

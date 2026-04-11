"""Unit tests for the autonomous ASO strategy optimizer loop."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from agent.strategy_executor import BacktestResult
from agent.strategy_ir import (
    AtrExitSpec,
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
from agent.strategy_optimizer import OptimizerConfig, StrategyOptimizer
from agent.strategy_store import DEFAULT_DB_PATH, StrategyStore
from agent.strategy_walkforward import FoldResult, WalkForwardResult


def _build_ir(name: str = "optimizer_test") -> StrategyIR:
    max_warmup = 20
    return StrategyIR(
        name=name,
        symbol="BTC-USD",
        timeframe="1h",
        indicators=[
            IndicatorSpec(type=IndicatorType.RSI, period=14, alias="rsi"),
            IndicatorSpec(type=IndicatorType.EMA, period=20, alias="ema_fast"),
            IndicatorSpec(type=IndicatorType.ATR, period=14, alias="atr"),
        ],
        entry=EntrySpec(
            conditions=[
                Condition(
                    indicator="rsi",
                    operator=ConditionOperator.ABOVE,
                    target=ConstantValue(value=50.0),
                )
            ]
        ),
        exit=ExitSpec(
            stop_loss=AtrExitSpec(atr_indicator="atr", multiple=2.0),
            take_profit=AtrExitSpec(atr_indicator="atr", multiple=3.0),
        ),
        risk=RiskSpec(max_position_pct=0.1, max_drawdown_pct=0.2),
        costs=CostsSpec(),
        max_warmup=max_warmup,
        warmup_bars=StrategyIR.compute_warmup_bars(max_warmup),
    )


def _frame(total_bars: int = 120) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=total_bars, freq="h")
    return pd.DataFrame(
        {
            "Open": range(total_bars),
            "High": [value + 1 for value in range(total_bars)],
            "Low": [value - 1 for value in range(total_bars)],
            "Close": [value + 0.5 for value in range(total_bars)],
            "Volume": [100] * total_bars,
        },
        index=index,
    )


def _backtest(frame: pd.DataFrame, warmup_bars: int) -> BacktestResult:
    equity = pd.Series([100_000.0, 101_000.0], index=frame.index[-2:])
    benchmark = pd.Series([100.0, 101.0], index=frame.index[-2:])
    return BacktestResult(
        trades=[],
        equity_curve=equity,
        gross_equity_curve=equity,
        benchmark_prices=benchmark,
        warmup_bars=warmup_bars,
        initial_cash=100_000.0,
    )


def _metrics(
    *,
    trades: int = 60,
    sharpe: float | None = 1.0,
    sortino: float | None = 1.4,
    drawdown: float = -8.0,
    win_rate: float | None = 55.0,
    profit_factor: float | None = 1.5,
) -> MetricsReport:
    return MetricsReport(
        returns=ReturnMetrics(total_pct=8.0, annualized_pct=12.0, monthly_returns={}),
        risk_adjusted=RiskAdjustedMetrics(sharpe_ratio=sharpe, sortino_ratio=sortino, calmar_ratio=1.0),
        drawdown=DrawdownMetrics(
            max_drawdown_pct=drawdown,
            max_drawdown_duration_days=2.0,
            avg_drawdown_pct=-2.0,
            recovery_factor=1.0,
            underwater_curve=[0.0, -1.0, 0.0],
        ),
        trades=TradeMetrics(
            total=trades,
            winners=int(trades * 0.6),
            losers=trades - int(trades * 0.6),
            win_rate_pct=win_rate,
            profit_factor=profit_factor,
            avg_win_pct=2.0,
            avg_loss_pct=-1.0,
            largest_win_pct=4.0,
            largest_loss_pct=-3.0,
            avg_duration_bars=3.0,
        ),
        benchmark=BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=1.0,
            alpha_pct=7.0,
            beta=0.1,
            correlation=0.2,
        ),
        tail_risk=TailRiskMetrics(cvar_95_pct=-2.0, cvar_99_pct=-3.0, time_under_water_bars=5, time_under_water_pct=10.0),
        stability=StabilityMetrics(monthly_return_stddev=1.0, positive_months_pct=50.0, longest_losing_streak=2),
        cost_analysis=CostAnalysisMetrics(gross_return_pct=9.0, net_return_pct=8.0, fee_burden_pct_of_gross=5.0),
    )


def _walk_forward(sharpe: float, *, all_folds_pass: bool = True, rejection_reason: str | None = None) -> WalkForwardResult:
    fold_metrics = _metrics(trades=60, sharpe=sharpe)
    return WalkForwardResult(
        folds=[FoldResult(0, fold_metrics, True, "ok", all_folds_pass, rejection_reason or "ok")],
        median_sharpe=sharpe,
        median_sortino=1.5,
        median_calmar=1.0,
        worst_max_drawdown_pct=-6.0,
        total_trades=60,
        all_folds_pass=all_folds_pass,
        rejection_reason=rejection_reason,
    )


class _MockFetcher:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    async def fetch(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        del symbol, timeframe, bars
        return self.frame.copy()


class _MockLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.model_name = "mock-llm"

    async def complete(self, prompt: str) -> str:
        del prompt
        return json.dumps(self.payload)


class StrategyOptimizerTests(unittest.IsolatedAsyncioTestCase):
    """Validate accepted, rejected, skipped, and stagnation-reset flows."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StrategyStore(Path(self.temp_dir.name) / DEFAULT_DB_PATH.name)
        self.store.init_db()
        self.root_id = self.store.save_strategy(_build_ir(), "optimizer_test", 1, pool="candidates")
        self.fetcher = _MockFetcher(_frame())
        self.config = OptimizerConfig(cycle_interval_seconds=0, history_bars=120, param_tune_fraction=1.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_run_one_cycle_accepts_child_when_validation_improves(self):
        optimizer = StrategyOptimizer(self.store, _MockLLM({"mutations": []}), self.fetcher, self.config)

        with (
            patch("agent.strategy_optimizer.execute_strategy", side_effect=lambda ir, frame: _backtest(frame, ir.warmup_bars)),
            patch(
                "agent.strategy_optimizer.compute_metrics",
                side_effect=[_metrics(sharpe=0.8), _metrics(sharpe=1.1), _metrics(sharpe=0.9)],
            ),
            patch("agent.strategy_optimizer.walk_forward_evaluate", side_effect=[_walk_forward(1.4), _walk_forward(1.0)]),
            patch("agent.strategy_optimizer.check_sample_size", return_value=(True, "ok")),
        ):
            result = await optimizer.run_one_cycle()

        self.assertEqual(result.status, "accepted")
        self.assertTrue(result.accepted)
        self.assertEqual(result.mutation_mode, "parameter_tune")
        self.assertIsNotNone(result.child_strategy_id)
        self.assertEqual(len(self.store.get_lineage("optimizer_test")), 2)

    async def test_run_one_cycle_rejects_child_when_walk_forward_does_not_improve(self):
        optimizer = StrategyOptimizer(self.store, _MockLLM({"mutations": []}), self.fetcher, self.config)

        with (
            patch("agent.strategy_optimizer.execute_strategy", side_effect=lambda ir, frame: _backtest(frame, ir.warmup_bars)),
            patch(
                "agent.strategy_optimizer.compute_metrics",
                side_effect=[_metrics(sharpe=0.8), _metrics(sharpe=0.7), _metrics(sharpe=0.6)],
            ),
            patch("agent.strategy_optimizer.walk_forward_evaluate", side_effect=[_walk_forward(0.9), _walk_forward(1.0)]),
            patch("agent.strategy_optimizer.check_sample_size", return_value=(True, "ok")),
        ):
            result = await optimizer.run_one_cycle()

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.accepted)
        self.assertIn("did not exceed parent", result.reason)
        self.assertEqual(self.store.get_lineage("optimizer_test")[-1].mutation.accepted, False)

    async def test_run_one_cycle_skips_converged_strategy(self):
        optimizer = StrategyOptimizer(self.store, _MockLLM({"mutations": []}), self.fetcher, self.config)

        with (
            patch("agent.strategy_optimizer.execute_strategy", side_effect=lambda ir, frame: _backtest(frame, ir.warmup_bars)),
            patch("agent.strategy_optimizer.compute_metrics", return_value=_metrics(sharpe=2.2, sortino=2.6, drawdown=-9.0)),
        ):
            result = await optimizer.run_one_cycle()

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "CONVERGED")
        self.assertEqual(len(self.store.get_lineage("optimizer_test")), 1)

    async def test_run_one_cycle_skips_insufficient_evidence(self):
        optimizer = StrategyOptimizer(self.store, _MockLLM({"mutations": []}), self.fetcher, self.config)

        with (
            patch("agent.strategy_optimizer.execute_strategy", side_effect=lambda ir, frame: _backtest(frame, ir.warmup_bars)),
            patch("agent.strategy_optimizer.compute_metrics", return_value=_metrics(trades=20)),
        ):
            result = await optimizer.run_one_cycle()

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(len(self.store.get_lineage("optimizer_test")), 1)

    async def test_stagnation_forces_structural_reset_from_best_historical_version(self):
        best_id = self.root_id
        self.store.save_run(best_id, "in_sample", None, "hash-best", _metrics(sharpe=1.8), True, sample_size_detail="ok")

        parent_id = best_id
        for version in range(2, 12):
            child_id = self.store.save_strategy(_build_ir(), "optimizer_test", version, parent_id=parent_id, pool="candidates", created_by="optimizer")
            self.store.save_mutation(
                parent_id,
                child_id,
                [{"path": "risk.max_position_pct", "old": 0.1, "new": 0.09}],
                accepted=False,
                rejection_reason="stagnated",
            )
            parent_id = child_id
        self.store.save_run(parent_id, "in_sample", None, "hash-latest", _metrics(sharpe=0.4), True, sample_size_detail="ok")

        optimizer = StrategyOptimizer(
            self.store,
            _MockLLM(
                {
                    "mutations": [
                        {
                            "description": "slow down EMA",
                            "yaml_path": "indicators[1].period",
                            "old_value": 20,
                            "new_value": 25,
                            "rationale": "reduce noise",
                        }
                    ]
                }
            ),
            self.fetcher,
            OptimizerConfig(cycle_interval_seconds=0, history_bars=120, param_tune_fraction=0.0, stagnation_threshold=10),
        )

        with (
            patch("agent.strategy_optimizer.execute_strategy", side_effect=lambda ir, frame: _backtest(frame, ir.warmup_bars)),
            patch(
                "agent.strategy_optimizer.compute_metrics",
                side_effect=[_metrics(sharpe=0.8), _metrics(sharpe=1.2), _metrics(sharpe=0.9)],
            ),
            patch("agent.strategy_optimizer.walk_forward_evaluate", side_effect=[_walk_forward(1.3), _walk_forward(0.9)]),
            patch("agent.strategy_optimizer.check_sample_size", return_value=(True, "ok")),
        ):
            result = await optimizer.run_one_cycle()

        self.assertEqual(result.mutation_mode, "llm_structural")
        self.assertEqual(result.parent_strategy_id, best_id)

    async def test_duplicate_rejected_mutation_is_filtered_as_non_novel(self):
        child_id = self.store.save_strategy(_build_ir(), "optimizer_test", 2, parent_id=self.root_id, pool="graveyard", created_by="optimizer")
        self.store.save_mutation(
            self.root_id,
            child_id,
            [{"path": "indicators[1].period", "old": 20, "new": 25}],
            accepted=False,
            rejection_reason="already failed",
        )
        optimizer = StrategyOptimizer(
            self.store,
            _MockLLM(
                {
                    "mutations": [
                        {
                            "description": "repeat failed EMA change",
                            "yaml_path": "indicators[1].period",
                            "old_value": 20,
                            "new_value": 25,
                            "rationale": "duplicate",
                        }
                    ]
                }
            ),
            self.fetcher,
            OptimizerConfig(cycle_interval_seconds=0, history_bars=120, param_tune_fraction=0.0),
        )

        with (
            patch("agent.strategy_optimizer.execute_strategy", side_effect=lambda ir, frame: _backtest(frame, ir.warmup_bars)),
            patch("agent.strategy_optimizer.compute_metrics", return_value=_metrics(sharpe=0.8)),
        ):
            result = await optimizer.run_one_cycle()

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "No novel supported mutation candidates")

    async def test_invalid_llm_mutation_is_rejected_gracefully(self):
        optimizer = StrategyOptimizer(
            self.store,
            _MockLLM(
                {
                    "mutations": [
                        {
                            "description": "unsupported field",
                            "yaml_path": "entry.position_size",
                            "old_value": None,
                            "new_value": 0.2,
                            "rationale": "unsupported",
                        }
                    ]
                }
            ),
            self.fetcher,
            OptimizerConfig(cycle_interval_seconds=0, history_bars=120, param_tune_fraction=0.0),
        )

        with (
            patch("agent.strategy_optimizer.execute_strategy", side_effect=lambda ir, frame: _backtest(frame, ir.warmup_bars)),
            patch("agent.strategy_optimizer.compute_metrics", return_value=_metrics(sharpe=0.8)),
        ):
            result = await optimizer.run_one_cycle()

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "No novel supported mutation candidates")


if __name__ == "__main__":
    unittest.main()

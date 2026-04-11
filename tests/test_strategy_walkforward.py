"""Unit tests for walk-forward strategy validation."""

import unittest
from unittest.mock import patch

import pandas as pd

from agent.strategy_executor import BacktestResult
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
from agent.strategy_walkforward import walk_forward_evaluate


def _build_ir(max_warmup: int = 2) -> StrategyIR:
    return StrategyIR(
        name="walk_forward_test",
        symbol="BTC-USD",
        timeframe="1h",
        indicators=[IndicatorSpec(type=IndicatorType.SMA, period=2, alias="sma_fast")],
        entry=EntrySpec(
            conditions=[
                Condition(
                    indicator="sma_fast",
                    operator=ConditionOperator.ABOVE,
                    target=ConstantValue(value=100.0),
                )
            ]
        ),
        exit=ExitSpec(time_exit=TimeExitSpec(max_bars=1)),
        risk=RiskSpec(max_position_pct=0.1, max_drawdown_pct=0.2),
        costs=CostsSpec(),
        max_warmup=max_warmup,
        warmup_bars=StrategyIR.compute_warmup_bars(max_warmup),
    )


def _frame(total_bars: int = 24) -> pd.DataFrame:
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


def _metrics(*, sharpe: float | None, sortino: float | None, calmar: float | None, max_drawdown_pct: float, trades: int) -> MetricsReport:
    return MetricsReport(
        returns=ReturnMetrics(total_pct=5.0, annualized_pct=10.0, monthly_returns={}),
        risk_adjusted=RiskAdjustedMetrics(sharpe_ratio=sharpe, sortino_ratio=sortino, calmar_ratio=calmar),
        drawdown=DrawdownMetrics(
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_duration_days=2.0,
            avg_drawdown_pct=-1.0,
            recovery_factor=1.0,
            underwater_curve=[0.0, -1.0, 0.0],
        ),
        trades=TradeMetrics(
            total=trades,
            winners=max(trades - 10, 0),
            losers=min(trades, 10),
            win_rate_pct=60.0 if trades else None,
            profit_factor=1.2 if trades else None,
            avg_win_pct=1.0 if trades else None,
            avg_loss_pct=-1.0 if trades else None,
            largest_win_pct=2.0 if trades else None,
            largest_loss_pct=-2.0 if trades else None,
            avg_duration_bars=3.0 if trades else None,
        ),
        benchmark=BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=1.0,
            alpha_pct=4.0,
            beta=0.1,
            correlation=0.2,
        ),
        tail_risk=TailRiskMetrics(cvar_95_pct=-2.0, cvar_99_pct=-3.0, time_under_water_bars=5, time_under_water_pct=10.0),
        stability=StabilityMetrics(monthly_return_stddev=1.0, positive_months_pct=50.0, longest_losing_streak=2),
        cost_analysis=CostAnalysisMetrics(gross_return_pct=6.0, net_return_pct=5.0, fee_burden_pct_of_gross=5.0),
    )


class StrategyWalkForwardTests(unittest.TestCase):
    """Validate fold math, warmup prepending, and rejection rules."""

    def test_walk_forward_uses_expanding_folds_and_prepends_warmup_bars(self):
        ir = _build_ir()
        ohlcv = _frame(24)
        seen_slices: list[pd.DataFrame] = []

        def fake_execute(_, exec_slice):
            seen_slices.append(exec_slice.copy())
            equity = pd.Series([100_000.0, 101_000.0], index=exec_slice.index[-2:])
            benchmark = pd.Series([100.0, 101.0], index=exec_slice.index[-2:])
            return BacktestResult(
                trades=[],
                equity_curve=equity,
                gross_equity_curve=equity,
                benchmark_prices=benchmark,
                warmup_bars=ir.warmup_bars,
                initial_cash=100_000.0,
            )

        metrics = _metrics(sharpe=1.0, sortino=1.2, calmar=0.8, max_drawdown_pct=-5.0, trades=55)
        with (
            patch("agent.strategy_walkforward.execute_strategy", side_effect=fake_execute),
            patch("agent.strategy_walkforward.compute_metrics", return_value=metrics),
            patch("agent.strategy_walkforward.check_sample_size", return_value=(True, "ok")) as mock_sample,
        ):
            result = walk_forward_evaluate(ir, ohlcv, n_folds=5)

        self.assertEqual(len(result.folds), 5)
        self.assertEqual([len(frame) for frame in seen_slices], [7, 7, 7, 7, 7])
        self.assertEqual(list(seen_slices[0].index), list(ohlcv.index[1:8]))
        self.assertEqual(list(seen_slices[1].index), list(ohlcv.index[5:12]))
        self.assertEqual(list(seen_slices[-1].index), list(ohlcv.index[17:24]))
        self.assertEqual([call.args for call in mock_sample.call_args_list], [(55, 3.0, 4, "walk_forward")] * 5)

    def test_walk_forward_aggregates_medians_total_trades_and_rejection_reason(self):
        ir = _build_ir()
        ohlcv = _frame(24)
        metric_payloads = [
            _metrics(sharpe=2.0, sortino=3.0, calmar=1.0, max_drawdown_pct=-4.0, trades=60),
            _metrics(sharpe=1.0, sortino=2.0, calmar=0.9, max_drawdown_pct=-8.0, trades=70),
            _metrics(sharpe=3.0, sortino=None, calmar=1.1, max_drawdown_pct=-6.0, trades=80),
        ]
        sample_payloads = [(True, "ok"), (False, "Need ≥50 trades (overlap-adjusted), got 40"), (True, "ok")]

        with (
            patch("agent.strategy_walkforward.execute_strategy") as mock_execute,
            patch("agent.strategy_walkforward.compute_metrics", side_effect=metric_payloads),
            patch("agent.strategy_walkforward.check_sample_size", side_effect=sample_payloads),
        ):
            mock_execute.return_value = BacktestResult(
                trades=[],
                equity_curve=pd.Series([100_000.0, 101_000.0], index=ohlcv.index[:2]),
                gross_equity_curve=pd.Series([100_000.0, 101_000.0], index=ohlcv.index[:2]),
                benchmark_prices=pd.Series([100.0, 101.0], index=ohlcv.index[:2]),
                warmup_bars=ir.warmup_bars,
                initial_cash=100_000.0,
            )
            result = walk_forward_evaluate(ir, ohlcv.iloc[:16], n_folds=3)

        self.assertAlmostEqual(result.median_sharpe, 2.0)
        self.assertAlmostEqual(result.median_sortino, 2.5)
        self.assertAlmostEqual(result.median_calmar, 1.0)
        self.assertEqual(result.worst_max_drawdown_pct, -8.0)
        self.assertEqual(result.total_trades, 210)
        self.assertFalse(result.all_folds_pass)
        self.assertEqual(result.rejection_reason, "fold 1 failed: Need ≥50 trades (overlap-adjusted), got 40")

    def test_walk_forward_rejects_drawdown_breaches_even_when_sample_size_passes(self):
        ir = _build_ir()
        ohlcv = _frame(16)

        with (
            patch("agent.strategy_walkforward.execute_strategy") as mock_execute,
            patch(
                "agent.strategy_walkforward.compute_metrics",
                return_value=_metrics(sharpe=1.0, sortino=1.0, calmar=1.0, max_drawdown_pct=-25.0, trades=60),
            ),
            patch("agent.strategy_walkforward.check_sample_size", return_value=(True, "ok")),
        ):
            mock_execute.return_value = BacktestResult(
                trades=[],
                equity_curve=pd.Series([100_000.0, 101_000.0], index=ohlcv.index[:2]),
                gross_equity_curve=pd.Series([100_000.0, 101_000.0], index=ohlcv.index[:2]),
                benchmark_prices=pd.Series([100.0, 101.0], index=ohlcv.index[:2]),
                warmup_bars=ir.warmup_bars,
                initial_cash=100_000.0,
            )
            result = walk_forward_evaluate(ir, ohlcv, n_folds=3)

        self.assertFalse(result.all_folds_pass)
        self.assertIn("max drawdown 25.00% exceeds 20.00%", result.rejection_reason)
        self.assertFalse(result.folds[0].hard_constraint_pass)

    def test_walk_forward_rejects_too_small_dataset_or_train_warmup_overlap(self):
        with self.assertRaises(ValueError):
            walk_forward_evaluate(_build_ir(), _frame(5), n_folds=5)

        with self.assertRaises(ValueError):
            walk_forward_evaluate(_build_ir(max_warmup=4), _frame(12), n_folds=3)


if __name__ == "__main__":
    unittest.main()

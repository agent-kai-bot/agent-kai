"""Unit tests for the ASO validation ladder orchestrator."""

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
from agent.strategy_validator import validate_strategy
from agent.strategy_walkforward import FoldResult, WalkForwardResult


def _build_ir() -> StrategyIR:
    max_warmup = 2
    return StrategyIR(
        name="validator_test",
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


def _frame(start: str, total_bars: int) -> pd.DataFrame:
    index = pd.date_range(start, periods=total_bars, freq="h")
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


def _metrics(*, trades: int, max_drawdown_pct: float) -> MetricsReport:
    return MetricsReport(
        returns=ReturnMetrics(total_pct=4.0, annualized_pct=7.0, monthly_returns={}),
        risk_adjusted=RiskAdjustedMetrics(sharpe_ratio=1.0, sortino_ratio=1.2, calmar_ratio=0.8),
        drawdown=DrawdownMetrics(
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_duration_days=2.0,
            avg_drawdown_pct=-1.0,
            recovery_factor=1.0,
            underwater_curve=[0.0, -1.0, 0.0],
        ),
        trades=TradeMetrics(
            total=trades,
            winners=max(trades - 5, 0),
            losers=min(trades, 5),
            win_rate_pct=50.0 if trades else None,
            profit_factor=1.1 if trades else None,
            avg_win_pct=1.0 if trades else None,
            avg_loss_pct=-1.0 if trades else None,
            largest_win_pct=2.0 if trades else None,
            largest_loss_pct=-2.0 if trades else None,
            avg_duration_bars=3.0 if trades else None,
        ),
        benchmark=BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=1.0,
            alpha_pct=3.0,
            beta=0.2,
            correlation=0.1,
        ),
        tail_risk=TailRiskMetrics(cvar_95_pct=-2.0, cvar_99_pct=-3.0, time_under_water_bars=5, time_under_water_pct=10.0),
        stability=StabilityMetrics(monthly_return_stddev=1.0, positive_months_pct=50.0, longest_losing_streak=2),
        cost_analysis=CostAnalysisMetrics(gross_return_pct=5.0, net_return_pct=4.0, fee_burden_pct_of_gross=5.0),
    )


def _backtest_result(index: pd.DatetimeIndex, warmup_bars: int) -> BacktestResult:
    equity = pd.Series([100_000.0, 101_000.0], index=index[-2:])
    benchmark = pd.Series([100.0, 101.0], index=index[-2:])
    return BacktestResult(
        trades=[],
        equity_curve=equity,
        gross_equity_curve=equity,
        benchmark_prices=benchmark,
        warmup_bars=warmup_bars,
        initial_cash=100_000.0,
    )


def _passing_walk_forward() -> WalkForwardResult:
    fold_metrics = _metrics(trades=55, max_drawdown_pct=-5.0)
    return WalkForwardResult(
        folds=[
            FoldResult(0, fold_metrics, True, "ok", True, "ok"),
            FoldResult(1, fold_metrics, True, "ok", True, "ok"),
        ],
        median_sharpe=1.0,
        median_sortino=1.2,
        median_calmar=0.8,
        worst_max_drawdown_pct=-5.0,
        total_trades=110,
        all_folds_pass=True,
        rejection_reason=None,
    )


class StrategyValidatorTests(unittest.TestCase):
    """Validate stage orchestration and ladder gating behavior."""

    def test_validator_runs_in_sample_walk_forward_and_lockbox_with_warmup_prefix(self):
        ir = _build_ir()
        full = _frame("2025-01-01", 30)
        lockbox = _frame("2025-02-01", 8)
        seen_slices: list[pd.DataFrame] = []

        def fake_execute(_, frame):
            seen_slices.append(frame.copy())
            return _backtest_result(frame.index, ir.warmup_bars)

        with (
            patch("agent.strategy_validator.execute_strategy", side_effect=fake_execute),
            patch(
                "agent.strategy_validator.compute_metrics",
                side_effect=[_metrics(trades=30, max_drawdown_pct=-6.0), _metrics(trades=60, max_drawdown_pct=-8.0)],
            ),
            patch("agent.strategy_validator.walk_forward_evaluate", return_value=_passing_walk_forward()) as mock_wf,
            patch("agent.strategy_validator.check_sample_size", return_value=(True, "ok")) as mock_sample,
        ):
            result = validate_strategy(ir, full, lockbox)

        self.assertTrue(result.overall_pass)
        self.assertIsNotNone(result.lockbox)
        self.assertEqual(len(seen_slices), 2)
        self.assertEqual(len(seen_slices[0]), 18)
        self.assertEqual(list(seen_slices[0].index), list(full.index[:18]))
        self.assertEqual(list(seen_slices[1].index[: ir.warmup_bars]), list(full.index[15:18]))
        self.assertEqual(list(seen_slices[1].index[ir.warmup_bars :]), list(lockbox.index))
        self.assertEqual(mock_wf.call_args.args[1].index.tolist(), full.index[:18].tolist())
        self.assertEqual(mock_sample.call_args.args, (60, 3.0, 8, "lockbox"))

    def test_validator_stops_before_lockbox_when_walk_forward_fails(self):
        ir = _build_ir()
        full = _frame("2025-01-01", 30)
        lockbox = _frame("2025-02-01", 8)
        failing_walk_forward = WalkForwardResult(
            folds=[],
            median_sharpe=None,
            median_sortino=None,
            median_calmar=None,
            worst_max_drawdown_pct=0.0,
            total_trades=0,
            all_folds_pass=False,
            rejection_reason="fold 0 failed: Need ≥50 trades (overlap-adjusted), got 20",
        )

        with (
            patch("agent.strategy_validator.execute_strategy", return_value=_backtest_result(full.index[:18], ir.warmup_bars)) as mock_execute,
            patch("agent.strategy_validator.compute_metrics", return_value=_metrics(trades=30, max_drawdown_pct=-6.0)),
            patch("agent.strategy_validator.walk_forward_evaluate", return_value=failing_walk_forward),
            patch("agent.strategy_validator.check_sample_size") as mock_sample,
        ):
            result = validate_strategy(ir, full, lockbox)

        self.assertFalse(result.overall_pass)
        self.assertIsNone(result.lockbox)
        self.assertEqual(result.rejection_reason, failing_walk_forward.rejection_reason)
        self.assertEqual(mock_execute.call_count, 1)
        mock_sample.assert_not_called()

    def test_validator_rejects_lockbox_hard_constraint_failures(self):
        ir = _build_ir()
        full = _frame("2025-01-01", 30)
        lockbox = _frame("2025-02-01", 8)

        with (
            patch("agent.strategy_validator.execute_strategy", return_value=_backtest_result(lockbox.index, ir.warmup_bars)),
            patch(
                "agent.strategy_validator.compute_metrics",
                side_effect=[_metrics(trades=30, max_drawdown_pct=-6.0), _metrics(trades=45, max_drawdown_pct=-25.0)],
            ),
            patch("agent.strategy_validator.walk_forward_evaluate", return_value=_passing_walk_forward()),
            patch("agent.strategy_validator.check_sample_size", return_value=(False, "Need ≥50 trades (overlap-adjusted), got 45")),
        ):
            result = validate_strategy(ir, full, lockbox)

        self.assertFalse(result.overall_pass)
        self.assertIsNotNone(result.lockbox)
        assert result.lockbox is not None
        self.assertFalse(result.lockbox.passed)
        self.assertIn("Need ≥50 trades (overlap-adjusted), got 45", result.lockbox.rejection_reason)
        self.assertIn("max drawdown 25.00% exceeds 20.00%", result.lockbox.rejection_reason)


if __name__ == "__main__":
    unittest.main()

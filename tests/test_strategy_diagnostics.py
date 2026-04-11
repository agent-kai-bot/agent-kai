"""Unit tests for ASO pre-LLM diagnostics."""

import unittest

from agent.strategy_diagnostics import diagnose_strategy
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


def _metrics(
    *,
    trades: int = 60,
    sharpe: float | None = 1.0,
    sortino: float | None = 1.5,
    drawdown: float = -8.0,
    win_rate: float | None = 55.0,
    profit_factor: float | None = 1.6,
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


class StrategyDiagnosticsTests(unittest.TestCase):
    """Validate failure-mode classification and skip conditions."""

    def test_insufficient_evidence_skips_before_llm(self):
        result = diagnose_strategy(_metrics(trades=29), [])

        self.assertFalse(result.proceed)
        self.assertEqual(result.reason, "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(result.failure_mode)

    def test_converged_skips_when_quality_thresholds_are_met(self):
        result = diagnose_strategy(_metrics(sharpe=2.2, sortino=2.6, drawdown=-9.0), [])

        self.assertFalse(result.proceed)
        self.assertEqual(result.reason, "CONVERGED")
        self.assertIsNone(result.failure_mode)

    def test_drawdown_has_highest_priority(self):
        result = diagnose_strategy(_metrics(drawdown=-25.0, sharpe=0.2, win_rate=30.0, profit_factor=1.0, sortino=0.5), [])

        self.assertTrue(result.proceed)
        self.assertEqual(result.failure_mode, "drawdown")

    def test_classifies_each_remaining_failure_mode_in_priority_order(self):
        self.assertEqual(diagnose_strategy(_metrics(sharpe=0.4), []).failure_mode, "poor_risk_adjusted")
        self.assertEqual(diagnose_strategy(_metrics(win_rate=35.0), []).failure_mode, "noisy_entries")
        self.assertEqual(diagnose_strategy(_metrics(profit_factor=1.2), []).failure_mode, "poor_win_loss")
        self.assertEqual(diagnose_strategy(_metrics(sortino=0.9), []).failure_mode, "downside_volatility")
        self.assertEqual(diagnose_strategy(_metrics(trades=45), []).failure_mode, "low_signal")
        self.assertEqual(diagnose_strategy(_metrics(trades=600), []).failure_mode, "over_trading")

    def test_low_trade_high_win_rate_triggers_bundled_hypothesis(self):
        result = diagnose_strategy(_metrics(trades=45, win_rate=67.0), [])

        self.assertTrue(result.proceed)
        self.assertTrue(result.bundled_hypothesis)


if __name__ == "__main__":
    unittest.main()

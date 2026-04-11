"""Unit tests for ASO metrics computation."""

import unittest

import pandas as pd

from agent.strategy_executor import ExitReason, TradeRecord
from agent.strategy_metrics import compute_metrics


class StrategyMetricsTests(unittest.TestCase):
    """Validate metrics against hand-built reference data."""

    def test_compute_metrics_matches_known_equity_and_trade_stats(self):
        equity = pd.Series(
            [100_000.0, 110_000.0, 105_000.0, 120_000.0],
            index=pd.to_datetime(["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"]),
        )
        benchmark = pd.Series(
            [100.0, 105.0, 103.0, 110.0],
            index=equity.index,
        )
        trades = [
            TradeRecord(
                entry_time=equity.index[0],
                exit_time=equity.index[1],
                entry_bar=0,
                exit_bar=1,
                entry_price=100.0,
                exit_price=115.0,
                gross_entry_price=100.0,
                gross_exit_price=116.0,
                quantity=100.0,
                entry_notional=10_000.0,
                exit_notional=11_500.0,
                gross_pnl=16_000.0,
                net_pnl=15_000.0,
                fees_paid=400.0,
                bars_held=10,
                return_pct=0.15,
                exit_reason=ExitReason.TIME_EXIT,
                entry_equity_before=100_000.0,
            ),
            TradeRecord(
                entry_time=equity.index[1],
                exit_time=equity.index[2],
                entry_bar=1,
                exit_bar=2,
                entry_price=110.0,
                exit_price=104.5,
                gross_entry_price=110.0,
                gross_exit_price=106.7,
                quantity=100.0,
                entry_notional=11_000.0,
                exit_notional=10_450.0,
                gross_pnl=-3_000.0,
                net_pnl=-5_000.0,
                fees_paid=300.0,
                bars_held=5,
                return_pct=-0.05,
                exit_reason=ExitReason.STOP_LOSS,
                entry_equity_before=110_000.0,
            ),
            TradeRecord(
                entry_time=equity.index[2],
                exit_time=equity.index[3],
                entry_bar=2,
                exit_bar=3,
                entry_price=105.0,
                exit_price=115.5,
                gross_entry_price=105.0,
                gross_exit_price=117.6,
                quantity=100.0,
                entry_notional=10_500.0,
                exit_notional=11_550.0,
                gross_pnl=12_000.0,
                net_pnl=10_000.0,
                fees_paid=300.0,
                bars_held=8,
                return_pct=0.10,
                exit_reason=ExitReason.TAKE_PROFIT,
                entry_equity_before=105_000.0,
            ),
        ]

        report = compute_metrics(equity, trades, benchmark)

        self.assertAlmostEqual(report.returns.total_pct, 20.0)
        self.assertAlmostEqual(report.drawdown.max_drawdown_pct, -4.545454545454546)
        self.assertAlmostEqual(report.drawdown.avg_drawdown_pct, -4.545454545454546)
        self.assertEqual(report.trades.total, 3)
        self.assertEqual(report.trades.winners, 2)
        self.assertEqual(report.trades.losers, 1)
        self.assertAlmostEqual(report.trades.win_rate_pct, 66.66666666666666)
        self.assertAlmostEqual(report.trades.profit_factor, 5.0)
        self.assertAlmostEqual(report.trades.avg_win_pct, 12.5)
        self.assertAlmostEqual(report.trades.avg_loss_pct, -5.0)
        self.assertAlmostEqual(report.trades.largest_win_pct, 15.0)
        self.assertAlmostEqual(report.trades.largest_loss_pct, -5.0)
        self.assertAlmostEqual(report.trades.avg_duration_bars, 23.0 / 3.0)
        self.assertAlmostEqual(report.benchmark.buy_and_hold_return_pct, 10.0)
        self.assertAlmostEqual(report.tail_risk.time_under_water_pct, 25.0)
        self.assertAlmostEqual(report.stability.positive_months_pct, 66.66666666666666)
        self.assertEqual(report.stability.longest_losing_streak, 1)
        self.assertAlmostEqual(report.cost_analysis.gross_return_pct, 25.0)
        self.assertAlmostEqual(report.cost_analysis.net_return_pct, 20.0)
        self.assertAlmostEqual(report.cost_analysis.fee_burden_pct_of_gross, 4.0)
        self.assertEqual(set(report.returns.monthly_returns), {"2024-02", "2024-03", "2024-04"})
        self.assertAlmostEqual(report.returns.monthly_returns["2024-02"], 10.0)
        self.assertAlmostEqual(report.returns.monthly_returns["2024-03"], -4.545454545454546)
        self.assertAlmostEqual(report.returns.monthly_returns["2024-04"], 14.285714285714285)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for ASO metrics computation."""

import unittest

import pandas as pd

from agent.strategy_executor import ExitReason, TradeRecord
from agent.strategy_metrics import compute_metrics


def _trade(
    *,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    net_pnl: float,
    return_pct: float,
    exit_reason: ExitReason = ExitReason.TIME_EXIT,
) -> TradeRecord:
    return TradeRecord(
        entry_time=entry_time,
        exit_time=exit_time,
        entry_bar=0,
        exit_bar=1,
        entry_price=100.0,
        exit_price=110.0,
        gross_entry_price=100.0,
        gross_exit_price=111.0,
        quantity=100.0,
        entry_notional=10_000.0,
        exit_notional=11_000.0,
        gross_pnl=1_100.0,
        net_pnl=net_pnl,
        fees_paid=100.0,
        bars_held=1,
        return_pct=return_pct,
        exit_reason=exit_reason,
        entry_equity_before=100_000.0,
    )


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

    def test_sortino_with_all_positive_returns(self):
        equity = pd.Series(
            [100_000.0, 101_000.0, 102_500.0, 104_000.0],
            index=pd.date_range("2024-01-01", periods=4, freq="D"),
        )

        report = compute_metrics(equity, [], None)

        self.assertIsNone(report.risk_adjusted.sortino_ratio)

    def test_zero_trade_metrics(self):
        equity = pd.Series(
            [100_000.0, 100_000.0, 100_000.0],
            index=pd.date_range("2024-01-01", periods=3, freq="D"),
        )

        report = compute_metrics(equity, [], None)

        self.assertEqual(report.trades.total, 0)
        self.assertEqual(report.trades.winners, 0)
        self.assertEqual(report.trades.losers, 0)
        self.assertIsNone(report.trades.win_rate_pct)
        self.assertIsNone(report.trades.profit_factor)
        self.assertIsNone(report.trades.avg_duration_bars)
        self.assertAlmostEqual(report.cost_analysis.gross_return_pct, 0.0)
        self.assertAlmostEqual(report.cost_analysis.net_return_pct, 0.0)
        self.assertIsNone(report.cost_analysis.fee_burden_pct_of_gross)

    def test_single_trade_metrics(self):
        equity = pd.Series(
            [100_000.0, 105_000.0],
            index=pd.date_range("2024-01-01", periods=2, freq="D"),
        )
        trade = _trade(
            entry_time=equity.index[0],
            exit_time=equity.index[1],
            net_pnl=5_000.0,
            return_pct=0.05,
            exit_reason=ExitReason.TAKE_PROFIT,
        )

        report = compute_metrics(equity, [trade], None)

        self.assertEqual(report.trades.total, 1)
        self.assertEqual(report.trades.winners, 1)
        self.assertEqual(report.trades.losers, 0)
        self.assertAlmostEqual(report.trades.win_rate_pct, 100.0)
        self.assertEqual(report.trades.profit_factor, float("inf"))
        self.assertAlmostEqual(report.trades.avg_win_pct, 5.0)
        self.assertAlmostEqual(report.trades.largest_win_pct, 5.0)
        self.assertAlmostEqual(report.trades.largest_loss_pct, 5.0)
        self.assertAlmostEqual(report.trades.avg_duration_bars, 1.0)
        self.assertEqual(report.stability.longest_losing_streak, 0)

    def test_cvar_with_few_data_points(self):
        equity = pd.Series(
            [100_000.0, 90_000.0, 95_000.0],
            index=pd.date_range("2024-01-01", periods=3, freq="D"),
        )

        report = compute_metrics(equity, [], None)

        self.assertAlmostEqual(report.tail_risk.cvar_95_pct, -10.0)
        self.assertAlmostEqual(report.tail_risk.cvar_99_pct, -10.0)


if __name__ == "__main__":
    unittest.main()

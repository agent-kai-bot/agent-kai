"""Unit tests for the ASO strategy executor."""

import unittest

import pandas as pd

from agent.strategy_executor import ExitReason, execute_strategy
from agent.strategy_ir import (
    Condition,
    ConditionOperator,
    ConstantValue,
    CostsSpec,
    EntrySpec,
    ExitSpec,
    IndicatorSpec,
    IndicatorType,
    PercentExitSpec,
    RiskSpec,
    StrategyIR,
    TimeExitSpec,
)


def _base_ir(exit_spec: ExitSpec) -> StrategyIR:
    indicators = [IndicatorSpec(type=IndicatorType.SMA, period=2, alias="sma_fast")]
    return StrategyIR(
        name="executor_test",
        symbol="BTC-USD",
        timeframe="1h",
        indicators=indicators,
        entry=EntrySpec(
            conditions=[
                Condition(
                    indicator="sma_fast",
                    operator=ConditionOperator.ABOVE,
                    target=ConstantValue(value=10.5),
                )
            ]
        ),
        exit=exit_spec,
        risk=RiskSpec(max_position_pct=0.5, max_drawdown_pct=0.2),
        costs=CostsSpec(fee_pct=0.01, slippage_pct=0.02, spread_pct=0.01),
        max_warmup=2,
        warmup_bars=StrategyIR.compute_warmup_bars(2),
    )


def _frame(closes, lows=None, highs=None):
    lows = lows or [price - 0.5 for price in closes]
    highs = highs or [price + 0.5 for price in closes]
    index = pd.date_range("2025-01-01", periods=len(closes), freq="h")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": [100] * len(closes),
        },
        index=index,
    )


class StrategyExecutorTests(unittest.TestCase):
    """Validate exact v1 execution semantics."""

    def test_executor_applies_entry_exit_fees_and_fill_prices(self):
        ir = _base_ir(ExitSpec(time_exit=TimeExitSpec(max_bars=1)))
        frame = _frame([10.0, 10.0, 10.0, 12.0, 12.0, 12.0], highs=[10.2, 10.2, 10.2, 12.2, 12.2, 12.2])

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        expected_entry_price = 12.0 * 1.03
        expected_exit_price = 12.0 * 1.02
        expected_qty = 50_000.0 / expected_entry_price
        expected_exit_notional = expected_qty * expected_exit_price
        expected_entry_fee = 50_000.0 * 0.01
        expected_exit_fee = expected_exit_notional * 0.01

        self.assertAlmostEqual(trade.entry_price, expected_entry_price)
        self.assertAlmostEqual(trade.exit_price, expected_exit_price)
        self.assertAlmostEqual(trade.quantity, expected_qty)
        self.assertAlmostEqual(trade.fees_paid, expected_entry_fee + expected_exit_fee)
        self.assertEqual(trade.exit_reason, ExitReason.TIME_EXIT)

    def test_executor_respects_warmup_and_force_closes_boundary(self):
        ir = _base_ir(ExitSpec())
        frame = _frame([10.0, 10.0, 10.0, 12.0, 13.0, 14.0], highs=[10.2, 10.2, 10.2, 12.2, 13.2, 14.2])

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        self.assertEqual(trade.entry_bar, ir.warmup_bars)
        self.assertEqual(trade.exit_bar, len(frame) - 1)
        self.assertEqual(trade.exit_reason, ExitReason.DATA_BOUNDARY)
        self.assertEqual(len(result.equity_curve), len(frame) - ir.warmup_bars)

    def test_executor_fills_stop_loss_at_stop_level_plus_slippage(self):
        ir = _base_ir(ExitSpec(stop_loss=PercentExitSpec(percent=0.10)))
        frame = _frame(
            [10.0, 10.0, 10.0, 12.0, 12.0, 12.0],
            lows=[9.8, 9.8, 9.8, 11.5, 10.0, 11.0],
            highs=[10.2, 10.2, 10.2, 12.2, 12.2, 12.2],
        )

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        expected_stop = 12.0 * (1.0 - 0.10)
        self.assertAlmostEqual(trade.exit_price, expected_stop * 1.02)
        self.assertEqual(trade.exit_reason, ExitReason.STOP_LOSS)


if __name__ == "__main__":
    unittest.main()

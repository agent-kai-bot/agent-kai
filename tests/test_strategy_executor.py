"""Unit tests for the ASO strategy executor."""

import unittest

import pandas as pd

from agent.strategy_executor import ExitReason, _conditions_met, execute_strategy
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
    RangeValue,
    RiskSpec,
    StrategyIR,
    TrailingStopSpec,
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
        expected_exit_price = 12.0 * 0.98
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
        self.assertAlmostEqual(trade.exit_price, 14.0 * 0.98)
        self.assertEqual(len(result.equity_curve), len(frame) - ir.warmup_bars)

    def test_executor_fills_stop_loss_at_stop_level_minus_slippage(self):
        ir = _base_ir(ExitSpec(stop_loss=PercentExitSpec(percent=0.10)))
        frame = _frame(
            [10.0, 10.0, 10.0, 12.0, 12.0, 12.0],
            lows=[9.8, 9.8, 9.8, 11.5, 10.0, 11.0],
            highs=[10.2, 10.2, 10.2, 12.2, 12.2, 12.2],
        )

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        expected_stop = 12.0 * (1.0 - 0.10)
        self.assertAlmostEqual(trade.exit_price, expected_stop * 0.98)
        self.assertEqual(trade.exit_reason, ExitReason.STOP_LOSS)

    def test_trailing_stop_activation_and_fill(self):
        ir = _base_ir(
            ExitSpec(
                trailing_stop=TrailingStopSpec(
                    type="percent",
                    distance=0.05,
                    activation=0.05,
                )
            )
        )
        frame = _frame(
            [10.0, 10.0, 10.0, 12.0, 12.4, 12.1],
            lows=[9.8, 9.8, 9.8, 11.5, 12.2, 11.9],
            highs=[10.2, 10.2, 10.2, 12.2, 13.0, 12.3],
        )

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        expected_trail = 13.0 * 0.95
        self.assertEqual(trade.exit_reason, ExitReason.TRAILING_STOP)
        self.assertAlmostEqual(trade.gross_exit_price, expected_trail)
        self.assertAlmostEqual(trade.exit_price, expected_trail * 0.98)

    def test_time_exit_after_max_bars(self):
        ir = _base_ir(ExitSpec(time_exit=TimeExitSpec(max_bars=2)))
        frame = _frame(
            [10.0, 10.0, 10.0, 12.0, 12.5, 13.0, 13.5],
            highs=[10.2, 10.2, 10.2, 12.2, 12.7, 13.2, 13.7],
        )

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        self.assertEqual(trade.exit_reason, ExitReason.TIME_EXIT)
        self.assertEqual(trade.bars_held, 2)
        self.assertAlmostEqual(trade.exit_price, 13.0 * 0.98)

    def test_between_operator(self):
        frame = pd.DataFrame(
            {"signal": [5.0, 7.0, 9.0]},
            index=pd.date_range("2025-01-01", periods=3, freq="h"),
        )
        condition = Condition(
            indicator="signal",
            operator=ConditionOperator.BETWEEN,
            target=RangeValue(
                lower=ConstantValue(value=6.0),
                upper=ConstantValue(value=8.0),
            ),
        )

        self.assertFalse(_conditions_met(frame, 0, [condition]))
        self.assertTrue(_conditions_met(frame, 1, [condition]))
        self.assertFalse(_conditions_met(frame, 2, [condition]))

    def test_crosses_below_operator(self):
        frame = pd.DataFrame(
            {"signal": [10.0, 8.0]},
            index=pd.date_range("2025-01-01", periods=2, freq="h"),
        )
        condition = Condition(
            indicator="signal",
            operator=ConditionOperator.CROSSES_BELOW,
            target=ConstantValue(value=9.0),
        )

        self.assertFalse(_conditions_met(frame, 0, [condition]))
        self.assertTrue(_conditions_met(frame, 1, [condition]))

    def test_strategy_with_no_stop_loss(self):
        ir = _base_ir(ExitSpec(take_profit=PercentExitSpec(percent=0.10)))
        frame = _frame(
            [10.0, 10.0, 10.0, 12.0, 12.0, 12.0],
            highs=[10.2, 10.2, 10.2, 12.2, 13.3, 12.2],
        )

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        self.assertEqual(trade.exit_reason, ExitReason.TAKE_PROFIT)
        self.assertAlmostEqual(trade.exit_price, (12.0 * 1.10) * 0.98)

    def test_strategy_with_no_take_profit(self):
        ir = _base_ir(ExitSpec(stop_loss=PercentExitSpec(percent=0.10)))
        frame = _frame(
            [10.0, 10.0, 10.0, 12.0, 12.0, 12.0],
            lows=[9.8, 9.8, 9.8, 11.5, 10.0, 11.0],
            highs=[10.2, 10.2, 10.2, 12.2, 12.2, 12.2],
        )

        result = execute_strategy(ir, frame)
        trade = result.trades[0]

        self.assertEqual(trade.exit_reason, ExitReason.STOP_LOSS)
        self.assertAlmostEqual(trade.exit_price, (12.0 * 0.90) * 0.98)

    def test_all_exit_types_apply_sell_side_slippage(self):
        cases = [
            (
                "stop_loss",
                _base_ir(ExitSpec(stop_loss=PercentExitSpec(percent=0.10))),
                _frame(
                    [10.0, 10.0, 10.0, 12.0, 12.0, 12.0],
                    lows=[9.8, 9.8, 9.8, 11.5, 10.0, 11.0],
                    highs=[10.2, 10.2, 10.2, 12.2, 12.2, 12.2],
                ),
                ExitReason.STOP_LOSS,
                12.0 * 0.90,
            ),
            (
                "take_profit",
                _base_ir(ExitSpec(take_profit=PercentExitSpec(percent=0.10))),
                _frame(
                    [10.0, 10.0, 10.0, 12.0, 12.0, 12.0],
                    highs=[10.2, 10.2, 10.2, 12.2, 13.3, 12.2],
                ),
                ExitReason.TAKE_PROFIT,
                12.0 * 1.10,
            ),
            (
                "trailing_stop",
                _base_ir(
                    ExitSpec(
                        trailing_stop=TrailingStopSpec(
                            type="percent",
                            distance=0.05,
                            activation=0.05,
                        )
                    )
                ),
                _frame(
                    [10.0, 10.0, 10.0, 12.0, 12.4, 12.1],
                    lows=[9.8, 9.8, 9.8, 11.5, 12.2, 11.9],
                    highs=[10.2, 10.2, 10.2, 12.2, 13.0, 12.3],
                ),
                ExitReason.TRAILING_STOP,
                13.0 * 0.95,
            ),
            (
                "time_exit",
                _base_ir(ExitSpec(time_exit=TimeExitSpec(max_bars=1))),
                _frame([10.0, 10.0, 10.0, 12.0, 12.0, 12.0], highs=[10.2, 10.2, 10.2, 12.2, 12.2, 12.2]),
                ExitReason.TIME_EXIT,
                12.0,
            ),
            (
                "data_boundary",
                _base_ir(ExitSpec()),
                _frame([10.0, 10.0, 10.0, 12.0, 13.0, 14.0], highs=[10.2, 10.2, 10.2, 12.2, 13.2, 14.2]),
                ExitReason.DATA_BOUNDARY,
                14.0,
            ),
        ]

        for name, ir, frame, expected_reason, expected_gross_exit_price in cases:
            with self.subTest(name=name):
                result = execute_strategy(ir, frame)
                trade = result.trades[0]

                self.assertEqual(trade.exit_reason, expected_reason)
                self.assertAlmostEqual(trade.gross_exit_price, expected_gross_exit_price)
                self.assertAlmostEqual(trade.exit_price, expected_gross_exit_price * 0.98)
                self.assertLess(trade.exit_price, trade.gross_exit_price)


if __name__ == "__main__":
    unittest.main()

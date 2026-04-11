"""Unit tests for the ASO strategy IR."""

import unittest

from pydantic import ValidationError

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
    RangeValue,
    RiskSpec,
    StrategyIR,
    TimeExitSpec,
)


def _build_ir(**overrides):
    base = {
        "name": "trend_follow",
        "symbol": "BTC-USD",
        "timeframe": "4h",
        "indicators": [
            IndicatorSpec(type=IndicatorType.RSI, period=14, alias="rsi"),
            IndicatorSpec(type=IndicatorType.ATR, period=14, alias="atr"),
        ],
        "entry": EntrySpec(
            conditions=[
                Condition(
                    indicator="rsi",
                    operator=ConditionOperator.BELOW,
                    target=ConstantValue(value=30.0),
                )
            ]
        ),
        "exit": ExitSpec(
            stop_loss=AtrExitSpec(atr_indicator="atr", multiple=2.0),
            time_exit=TimeExitSpec(max_bars=5),
        ),
        "risk": RiskSpec(max_position_pct=0.05, max_drawdown_pct=0.15),
        "costs": CostsSpec(fee_pct=0.001, slippage_pct=0.001, spread_pct=0.0005),
        "max_warmup": 14,
        "warmup_bars": StrategyIR.compute_warmup_bars(14),
    }
    base.update(overrides)
    return StrategyIR(**base)


class StrategyIRTests(unittest.TestCase):
    """Validate the typed v1 IR contract."""

    def test_ir_accepts_valid_strategy(self):
        ir = _build_ir()
        self.assertEqual(ir.max_warmup, 14)
        self.assertEqual(ir.warmup_bars, 21)
        self.assertEqual(ir.exit.stop_loss.atr_indicator, "atr")

    def test_ir_rejects_duplicate_indicator_aliases(self):
        with self.assertRaises(ValidationError) as ctx:
            _build_ir(
                indicators=[
                    IndicatorSpec(type=IndicatorType.RSI, period=14, alias="dup"),
                    IndicatorSpec(type=IndicatorType.ATR, period=14, alias="dup"),
                ]
            )
        self.assertIn("indicator aliases must be unique", str(ctx.exception))

    def test_ir_rejects_unknown_condition_references(self):
        with self.assertRaises(ValidationError) as ctx:
            _build_ir(
                entry=EntrySpec(
                    conditions=[
                        Condition(
                            indicator="missing",
                            operator=ConditionOperator.ABOVE,
                            target=ConstantValue(value=1.0),
                        )
                    ]
                )
            )
        self.assertIn("unknown indicator reference: missing", str(ctx.exception))

    def test_ir_requires_range_target_for_between(self):
        with self.assertRaises(ValidationError) as ctx:
            Condition(
                indicator="rsi",
                operator=ConditionOperator.BETWEEN,
                target=ConstantValue(value=40.0),
            )
        self.assertIn("between requires a lower and upper target", str(ctx.exception))

    def test_ir_accepts_between_with_range_target(self):
        condition = Condition(
            indicator="rsi",
            operator=ConditionOperator.BETWEEN,
            target=RangeValue(
                lower=ConstantValue(value=40.0),
                upper=ConstantValue(value=60.0),
            ),
        )
        self.assertEqual(condition.operator, ConditionOperator.BETWEEN)

    def test_ir_rejects_non_atr_exit_reference(self):
        with self.assertRaises(ValidationError) as ctx:
            _build_ir(
                exit=ExitSpec(
                    stop_loss=AtrExitSpec(atr_indicator="rsi", multiple=2.0),
                    time_exit=TimeExitSpec(max_bars=5),
                )
            )
        self.assertIn("ATR exit references unknown ATR indicator: rsi", str(ctx.exception))

    def test_ir_rejects_incorrect_warmup_buffer(self):
        with self.assertRaises(ValidationError) as ctx:
            _build_ir(warmup_bars=14)
        self.assertIn("warmup_bars must equal ceil(max_warmup * 1.5)", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

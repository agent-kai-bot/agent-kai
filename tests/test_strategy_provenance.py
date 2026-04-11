"""Unit tests for strategy evaluation provenance helpers."""

import json
import unittest

import pandas as pd

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
from agent.strategy_provenance import EXECUTOR_VERSION, compute_dataset_hash, get_fee_model_json


def _build_ir(costs: CostsSpec | None = None) -> StrategyIR:
    return StrategyIR(
        name="provenance_test",
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
        costs=costs or CostsSpec(fee_pct=0.0015, slippage_pct=0.002, spread_pct=0.0008),
        max_warmup=2,
        warmup_bars=StrategyIR.compute_warmup_bars(2),
    )


def _frame() -> pd.DataFrame:
    index = pd.to_datetime(["2025-01-03 00:00:00", "2025-01-01 00:00:00", "2025-01-02 00:00:00"])
    return pd.DataFrame(
        {
            "open": [101.0, 100.0, 102.0],
            "high": [102.0, 101.0, 103.0],
            "low": [100.0, 99.0, 101.0],
            "close": [101.5, 100.5, 102.5],
            "volume": [10.0, 20.0, 30.0],
        },
        index=index,
    )


class StrategyProvenanceTests(unittest.TestCase):
    """Validate stable dataset hashing and provenance metadata."""

    def test_hash_is_stable_for_same_data_regardless_of_row_order(self):
        frame = _frame()
        shuffled = frame.iloc[[1, 2, 0]]

        self.assertEqual(compute_dataset_hash(frame), compute_dataset_hash(shuffled))

    def test_hash_changes_when_any_ohlcv_value_changes(self):
        frame = _frame()
        modified = frame.copy()
        modified.iloc[0, modified.columns.get_loc("close")] = 999.0

        self.assertNotEqual(compute_dataset_hash(frame), compute_dataset_hash(modified))

    def test_fee_model_json_tracks_strategy_costs_and_executor_version_constant(self):
        payload = json.loads(get_fee_model_json(_build_ir()))

        self.assertEqual(EXECUTOR_VERSION, "1.0.0")
        self.assertEqual(payload["order_type"], "market")
        self.assertEqual(payload["entry_timing"], "bar_close")
        self.assertEqual(payload["exit_timing"], "bar_close")
        self.assertAlmostEqual(payload["fee_pct"], 0.0015)
        self.assertAlmostEqual(payload["slippage_pct"], 0.002)
        self.assertAlmostEqual(payload["spread_pct"], 0.0008)
        self.assertFalse(payload["partial_fills"])
        self.assertTrue(payload["reduce_only_exits"])


if __name__ == "__main__":
    unittest.main()

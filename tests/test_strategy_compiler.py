"""Unit tests for YAML strategy compilation."""

import textwrap
import unittest

from agent.strategy_compiler import compile_strategy


class StrategyCompilerTests(unittest.TestCase):
    """Validate v1 YAML compilation and rejection semantics."""

    def test_compile_strategy_returns_ir_and_computes_warmup(self):
        strategy_yaml = textwrap.dedent(
            """
            strategy:
              name: momentum_rsi
              universe: [BTC-USD]
              timeframe: 4h
              indicators:
                - name: rsi
                  period: 14
                - name: ema
                  period: 20
                  alias: ema_fast
                - name: atr
                  period: 10
              entry:
                long:
                  conditions:
                    - indicator: rsi
                      operator: below
                      value: 35
                    - indicator: ema_fast
                      operator: above
                      value: 100
              exit:
                stop_loss:
                  type: atr_multiple
                  multiplier: 2.0
                take_profit:
                  type: percent
                  percent: 0.05
                time_exit:
                  max_bars: 6
              risk:
                max_position_pct: 0.05
                max_drawdown_pct: 0.15
              costs:
                fee_pct: 0.001
                slippage_pct: 0.001
                spread_pct: 0.0005
            """
        )

        ir = compile_strategy(strategy_yaml)

        self.assertEqual(ir.symbol, "BTC-USD")
        self.assertEqual(ir.timeframe, "4h")
        self.assertEqual(ir.max_warmup, 20)
        self.assertEqual(ir.warmup_bars, 30)
        self.assertEqual(ir.exit.stop_loss.atr_indicator, "atr")

    def test_compile_strategy_rejects_short_entries(self):
        strategy_yaml = textwrap.dedent(
            """
            strategy:
              name: invalid_short
              symbol: BTC-USD
              timeframe: 1h
              indicators:
                - name: rsi
                  period: 14
              entry:
                short:
                  conditions:
                    - indicator: rsi
                      operator: below
                      value: 30
              exit: {}
              risk:
                max_position_pct: 0.05
                max_drawdown_pct: 0.15
            """
        )

        with self.assertRaisesRegex(ValueError, "v1 is long-only"):
            compile_strategy(strategy_yaml)

    def test_compile_strategy_rejects_execution_block(self):
        strategy_yaml = textwrap.dedent(
            """
            strategy:
              name: invalid_execution
              symbol: BTC-USD
              timeframe: 1h
              execution:
                order_type: market
              indicators:
                - name: rsi
                  period: 14
              entry:
                conditions:
                  - indicator: rsi
                    operator: below
                    value: 30
              exit: {}
              risk:
                max_position_pct: 0.05
                max_drawdown_pct: 0.15
            """
        )

        with self.assertRaisesRegex(ValueError, "v1 uses fixed market-on-close execution"):
            compile_strategy(strategy_yaml)

    def test_compile_strategy_rejects_multiple_symbols(self):
        strategy_yaml = textwrap.dedent(
            """
            strategy:
              name: invalid_universe
              universe: [BTC-USD, ETH-USD]
              timeframe: 1h
              indicators:
                - name: rsi
                  period: 14
              entry:
                conditions:
                  - indicator: rsi
                    operator: below
                    value: 30
              exit: {}
              risk:
                max_position_pct: 0.05
                max_drawdown_pct: 0.15
            """
        )

        with self.assertRaisesRegex(ValueError, "v1 is single-symbol"):
            compile_strategy(strategy_yaml)

    def test_compile_strategy_rejects_multiple_timeframes(self):
        strategy_yaml = textwrap.dedent(
            """
            strategy:
              name: invalid_timeframes
              symbol: BTC-USD
              timeframes: [1h, 4h]
              indicators:
                - name: rsi
                  period: 14
              entry:
                conditions:
                  - indicator: rsi
                    operator: below
                    value: 30
              exit: {}
              risk:
                max_position_pct: 0.05
                max_drawdown_pct: 0.15
            """
        )

        with self.assertRaisesRegex(ValueError, "v1 is single-timeframe"):
            compile_strategy(strategy_yaml)

    def test_compile_strategy_rejects_boolean_logic(self):
        strategy_yaml = textwrap.dedent(
            """
            strategy:
              name: invalid_logic
              symbol: BTC-USD
              timeframe: 1h
              indicators:
                - name: rsi
                  period: 14
              entry:
                or:
                  - indicator: rsi
                    operator: below
                    value: 30
              exit: {}
              risk:
                max_position_pct: 0.05
                max_drawdown_pct: 0.15
            """
        )

        with self.assertRaisesRegex(ValueError, "v1 supports AND-only conditions"):
            compile_strategy(strategy_yaml)

    def test_compile_strategy_rejects_unknown_indicator_names(self):
        strategy_yaml = textwrap.dedent(
            """
            strategy:
              name: invalid_indicator
              symbol: BTC-USD
              timeframe: 1h
              indicators:
                - name: macd
                  period: 12
              entry:
                conditions:
                  - indicator: macd
                    operator: above
                    value: 0
              exit: {}
              risk:
                max_position_pct: 0.05
                max_drawdown_pct: 0.15
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported indicator: macd"):
            compile_strategy(strategy_yaml)


if __name__ == "__main__":
    unittest.main()

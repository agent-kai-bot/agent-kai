"""Unit tests for ASO mutation generation and application."""

import json
import unittest

from agent.strategy_diagnostics import DiagnosticResult
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
    RiskSpec,
    StrategyIR,
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
from agent.strategy_mutator import Mutation, apply_mutations, parameter_tune, structural_mutate


def _build_ir() -> StrategyIR:
    max_warmup = 20
    return StrategyIR(
        name="mutator_test",
        symbol="BTC-USD",
        timeframe="1h",
        indicators=[
            IndicatorSpec(type=IndicatorType.RSI, period=14, alias="rsi"),
            IndicatorSpec(type=IndicatorType.EMA, period=20, alias="ema_fast"),
            IndicatorSpec(type=IndicatorType.ATR, period=14, alias="atr"),
        ],
        entry=EntrySpec(
            conditions=[
                Condition(
                    indicator="rsi",
                    operator=ConditionOperator.ABOVE,
                    target=ConstantValue(value=50.0),
                )
            ]
        ),
        exit=ExitSpec(
            stop_loss=AtrExitSpec(atr_indicator="atr", multiple=2.0),
            take_profit=AtrExitSpec(atr_indicator="atr", multiple=3.0),
        ),
        risk=RiskSpec(max_position_pct=0.1, max_drawdown_pct=0.2),
        costs=CostsSpec(),
        max_warmup=max_warmup,
        warmup_bars=StrategyIR.compute_warmup_bars(max_warmup),
    )


def _metrics() -> MetricsReport:
    return MetricsReport(
        returns=ReturnMetrics(total_pct=10.0, annualized_pct=14.0, monthly_returns={}),
        risk_adjusted=RiskAdjustedMetrics(sharpe_ratio=0.8, sortino_ratio=1.2, calmar_ratio=1.0),
        drawdown=DrawdownMetrics(
            max_drawdown_pct=-8.0,
            max_drawdown_duration_days=2.0,
            avg_drawdown_pct=-2.0,
            recovery_factor=1.0,
            underwater_curve=[0.0, -1.0, 0.0],
        ),
        trades=TradeMetrics(
            total=60,
            winners=35,
            losers=25,
            win_rate_pct=58.0,
            profit_factor=1.4,
            avg_win_pct=2.0,
            avg_loss_pct=-1.0,
            largest_win_pct=4.0,
            largest_loss_pct=-3.0,
            avg_duration_bars=3.0,
        ),
        benchmark=BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=1.0,
            alpha_pct=9.0,
            beta=0.1,
            correlation=0.2,
        ),
        tail_risk=TailRiskMetrics(cvar_95_pct=-2.0, cvar_99_pct=-3.0, time_under_water_bars=5, time_under_water_pct=10.0),
        stability=StabilityMetrics(monthly_return_stddev=1.0, positive_months_pct=50.0, longest_losing_streak=2),
        cost_analysis=CostAnalysisMetrics(gross_return_pct=11.0, net_return_pct=10.0, fee_burden_pct_of_gross=5.0),
    )


class _MockLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    async def complete(self, prompt: str) -> str:
        del prompt
        return json.dumps(self.payload)


class StrategyMutatorTests(unittest.IsolatedAsyncioTestCase):
    """Validate parameter tuning, structural filtering, and IR application."""

    async def test_parameter_tune_generates_valid_single_parameter_candidates(self):
        mutations = parameter_tune(
            _build_ir(),
            _metrics(),
            DiagnosticResult(proceed=True, reason="PROCEED:drawdown", failure_mode="drawdown", bundled_hypothesis=False),
        )

        self.assertGreaterEqual(len(mutations), 3)
        self.assertLessEqual(len(mutations), 5)
        self.assertTrue(all(mutation.source == "parameter_tune" for mutation in mutations))

        for mutation in mutations:
            mutated = apply_mutations(_build_ir(), [mutation])
            self.assertNotEqual(mutated.model_dump(mode="json"), _build_ir().model_dump(mode="json"))

    async def test_apply_mutations_rejects_invalid_edits_individually(self):
        ir = _build_ir()
        mutations = [
            Mutation(
                description="size up slightly",
                yaml_path="risk.max_position_pct",
                old_value=0.1,
                new_value=0.11,
                rationale="test",
                source="parameter_tune",
            ),
            Mutation(
                description="invalid stop",
                yaml_path="exit.stop_loss.multiple",
                old_value=2.0,
                new_value=-1.0,
                rationale="test",
                source="parameter_tune",
            ),
        ]

        mutated = apply_mutations(ir, mutations)

        self.assertEqual(mutated.risk.max_position_pct, 0.11)
        assert isinstance(mutated.exit.stop_loss, AtrExitSpec)
        self.assertEqual(mutated.exit.stop_loss.multiple, 2.0)

    async def test_structural_mutate_filters_invalid_and_unsupported_mutations(self):
        ir = _build_ir()
        llm = _MockLLM(
            {
                "analysis": "test",
                "mutations": [
                    {
                        "description": "slow down EMA",
                        "yaml_path": "indicators[1].period",
                        "old_value": 20,
                        "new_value": 25,
                        "rationale": "reduce noise",
                    },
                    {
                        "description": "unsupported field",
                        "yaml_path": "entry.position_size",
                        "old_value": None,
                        "new_value": 0.2,
                        "rationale": "not in IR",
                    },
                    {
                        "description": "invalid type",
                        "yaml_path": "exit.stop_loss.multiple",
                        "old_value": 2.0,
                        "new_value": -5.0,
                        "rationale": "invalid",
                    },
                ],
            }
        )

        mutations = await structural_mutate(
            ir,
            _metrics(),
            DiagnosticResult(proceed=True, reason="PROCEED:poor_risk_adjusted", failure_mode="poor_risk_adjusted", bundled_hypothesis=False),
            iteration_history=[],
            lessons_learned=[],
            llm_client=llm,
        )

        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0].yaml_path, "indicators[1].period")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the ASO SQLite strategy provenance store."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

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
from agent.strategy_store import (
    DEFAULT_DB_PATH,
    get_latest_run,
    get_lineage,
    get_strategy,
    init_db,
    list_strategies,
    promote_strategy,
    save_mutation,
    save_run,
    save_strategy,
)


def _build_ir(name: str) -> StrategyIR:
    return StrategyIR(
        name=name,
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
        costs=CostsSpec(fee_pct=0.001, slippage_pct=0.001, spread_pct=0.0005),
        max_warmup=2,
        warmup_bars=StrategyIR.compute_warmup_bars(2),
    )


def _metrics(total_trades: int = 60, max_drawdown_pct: float = -8.0) -> MetricsReport:
    return MetricsReport(
        returns=ReturnMetrics(total_pct=12.0, annualized_pct=18.0, monthly_returns={"2025-01": 3.0}),
        risk_adjusted=RiskAdjustedMetrics(sharpe_ratio=1.4, sortino_ratio=1.8, calmar_ratio=0.9),
        drawdown=DrawdownMetrics(
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_duration_days=4.0,
            avg_drawdown_pct=-2.0,
            recovery_factor=1.5,
            underwater_curve=[0.0, -1.0, 0.0],
        ),
        trades=TradeMetrics(
            total=total_trades,
            winners=35,
            losers=25,
            win_rate_pct=58.33,
            profit_factor=1.3,
            avg_win_pct=2.0,
            avg_loss_pct=-1.2,
            largest_win_pct=5.0,
            largest_loss_pct=-4.0,
            avg_duration_bars=2.5,
        ),
        benchmark=BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=5.0,
            alpha_pct=7.0,
            beta=0.2,
            correlation=0.1,
        ),
        tail_risk=TailRiskMetrics(cvar_95_pct=-3.0, cvar_99_pct=-4.0, time_under_water_bars=10, time_under_water_pct=12.0),
        stability=StabilityMetrics(monthly_return_stddev=1.1, positive_months_pct=66.0, longest_losing_streak=3),
        cost_analysis=CostAnalysisMetrics(gross_return_pct=14.0, net_return_pct=12.0, fee_burden_pct_of_gross=8.0),
    )


class StrategyStoreTests(unittest.TestCase):
    """Validate strategy provenance storage and lineage queries."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / DEFAULT_DB_PATH.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_init_db_creates_schema(self):
        init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertEqual(tables, {"strategies", "runs", "mutations", "approvals"})

    def test_strategy_run_and_lineage_crud(self):
        root_id = save_strategy(_build_ir("momentum_rsi"), "momentum_rsi", 1, db_path=self.db_path, yaml_source="name: momentum_rsi")
        child_id = save_strategy(
            _build_ir("momentum_rsi"),
            "momentum_rsi",
            2,
            parent_id=root_id,
            created_by="optimizer",
            db_path=self.db_path,
        )
        save_mutation(
            root_id,
            child_id,
            [{"path": "risk.max_position_pct", "old": 0.1, "new": 0.2, "rationale": "raise sizing"}],
            accepted=True,
            llm_model="gpt-5.4",
            llm_prompt_hash="abc123",
            db_path=self.db_path,
        )

        self.assertEqual(get_strategy(child_id, self.db_path).name, "momentum_rsi")

        lineage = get_lineage("momentum_rsi", self.db_path)
        self.assertEqual([entry.strategy.version for entry in lineage], [1, 2])
        self.assertIsNone(lineage[0].mutation)
        self.assertEqual(lineage[1].mutation.parent_strategy_id, root_id)
        self.assertTrue(lineage[1].mutation.accepted)
        self.assertEqual(lineage[1].mutation.mutations[0]["path"], "risk.max_position_pct")

    def test_get_latest_run_returns_most_recent_stage_run(self):
        strategy_id = save_strategy(_build_ir("wf_strategy"), "wf_strategy", 1, db_path=self.db_path)
        save_run(
            strategy_id,
            "walk_forward",
            0,
            "hash-a",
            _metrics(total_trades=55),
            True,
            sample_size_detail="ok",
            created_at="2025-01-01T00:00:00+00:00",
            db_path=self.db_path,
        )
        latest_run_id = save_run(
            strategy_id,
            "walk_forward",
            1,
            "hash-b",
            _metrics(total_trades=75),
            False,
            sample_size_detail="Need ≥100 trades (overlap-adjusted), got 75",
            created_at="2025-01-02T00:00:00+00:00",
            db_path=self.db_path,
        )

        latest = get_latest_run(strategy_id, "walk_forward", self.db_path)

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.id, latest_run_id)
        self.assertEqual(latest.fold_index, 1)
        self.assertEqual(latest.dataset_hash, "hash-b")
        self.assertFalse(latest.sample_size_pass)
        self.assertEqual(latest.metrics.trades.total, 75)

    def test_promote_strategy_updates_pool_and_records_approval_transactionally(self):
        strategy_id = save_strategy(_build_ir("pool_strategy"), "pool_strategy", 1, db_path=self.db_path)
        approval_id = promote_strategy(strategy_id, "candidates", "active", "human", db_path=self.db_path)

        strategies = list_strategies("active", self.db_path)
        self.assertEqual([strategy.id for strategy in strategies], [strategy_id])

        conn = sqlite3.connect(self.db_path)
        try:
            approval_row = conn.execute(
                "SELECT id, action, from_pool, to_pool, approved_by FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(approval_row, (approval_id, "promote", "candidates", "active", "human"))

    def test_failed_pool_transition_rolls_back_without_partial_write(self):
        strategy_id = save_strategy(_build_ir("rollback_strategy"), "rollback_strategy", 1, db_path=self.db_path)

        with self.assertRaises(ValueError):
            promote_strategy(strategy_id, "active", "graveyard", "human", db_path=self.db_path)

        self.assertEqual(list_strategies("candidates", self.db_path)[0].id, strategy_id)

        conn = sqlite3.connect(self.db_path)
        try:
            approval_count = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(approval_count, 0)


if __name__ == "__main__":
    unittest.main()

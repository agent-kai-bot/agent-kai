"""Unit tests for ASO lineage selection."""

import random
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
from agent.strategy_selector import select_next_strategy
from agent.strategy_store import DEFAULT_DB_PATH, StrategyStore


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
        costs=CostsSpec(),
        max_warmup=2,
        warmup_bars=StrategyIR.compute_warmup_bars(2),
    )


class StrategySelectorTests(unittest.TestCase):
    """Validate exploration, Thompson bias, and lineage caps."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StrategyStore(Path(self.temp_dir.name) / DEFAULT_DB_PATH.name)
        self.store.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_lineage(self, name: str, successes: int, failures: int) -> str:
        parent_id = self.store.save_strategy(_build_ir(name), name, 1, pool="candidates")
        version = 2
        current_parent = parent_id
        for _ in range(successes):
            child_id = self.store.save_strategy(_build_ir(name), name, version, parent_id=current_parent, pool="candidates", created_by="optimizer")
            self.store.save_mutation(current_parent, child_id, [{"path": "risk.max_position_pct", "old": 0.1, "new": 0.11}], accepted=True)
            current_parent = child_id
            version += 1
        for _ in range(failures):
            child_id = self.store.save_strategy(_build_ir(name), name, version, parent_id=current_parent, pool="graveyard", created_by="optimizer")
            self.store.save_mutation(
                current_parent,
                child_id,
                [{"path": "risk.max_position_pct", "old": 0.1, "new": 0.09}],
                accepted=False,
                rejection_reason="failed",
            )
            current_parent = child_id
            version += 1
        latest_candidate = self.store.list_strategies("candidates")[-1]
        return latest_candidate.id

    def test_thompson_sampling_prefers_lineages_with_more_success(self):
        better_id = self._seed_lineage("better", successes=8, failures=1)
        worse_id = self._seed_lineage("worse", successes=1, failures=8)

        counts = {better_id: 0, worse_id: 0}
        for seed in range(200):
            selected = select_next_strategy(self.store, exploration_rate=0.0, rng=random.Random(seed))
            counts[selected] += 1

        self.assertGreater(counts[better_id], counts[worse_id])

    def test_exploration_rate_can_force_random_selection(self):
        better_id = self._seed_lineage("alpha", successes=8, failures=1)
        worse_id = self._seed_lineage("beta", successes=1, failures=8)

        seen = {
            select_next_strategy(self.store, exploration_rate=1.0, rng=random.Random(seed))
            for seed in range(30)
        }

        self.assertEqual(seen, {better_id, worse_id})

    def test_lineage_cap_excludes_exhausted_lineages(self):
        self._seed_lineage("capped", successes=25, failures=25)
        viable_id = self._seed_lineage("viable", successes=1, failures=0)

        selected = select_next_strategy(self.store, exploration_rate=0.0, max_lineage_iterations=50, rng=random.Random(7))

        self.assertEqual(selected, viable_id)

    def test_returns_none_when_pool_is_exhausted(self):
        self._seed_lineage("capped_only", successes=25, failures=25)

        selected = select_next_strategy(self.store, exploration_rate=0.0, max_lineage_iterations=50, rng=random.Random(1))

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()

"""Strategy lineage selection for autonomous optimization."""

from __future__ import annotations

import random

from agent.strategy_store import StrategyStore


def select_next_strategy(
    store: StrategyStore,
    *,
    exploration_rate: float = 0.2,
    max_lineage_iterations: int = 50,
    rng: random.Random | None = None,
) -> str | None:
    """Select the next candidate strategy id using Thompson sampling."""
    rng = rng or random.Random()
    latest_by_name: dict[str, tuple[str, int, int, int]] = {}

    for strategy in store.list_strategies("candidates"):
        lineage = store.get_lineage(strategy.name)
        mutations = [entry.mutation for entry in lineage if entry.mutation is not None]
        if len(mutations) >= max_lineage_iterations:
            continue
        successes = sum(1 for mutation in mutations if mutation.accepted)
        failures = sum(1 for mutation in mutations if not mutation.accepted)
        current = latest_by_name.get(strategy.name)
        if current is None or strategy.version > current[1]:
            latest_by_name[strategy.name] = (strategy.id, strategy.version, successes, failures)

    candidates = list(latest_by_name.values())
    if not candidates:
        return None

    if rng.random() < exploration_rate:
        return rng.choice(candidates)[0]

    scored = [
        (strategy_id, rng.betavariate(successes + 1, failures + 1))
        for strategy_id, _version, successes, failures in candidates
    ]
    return max(scored, key=lambda item: item[1])[0]

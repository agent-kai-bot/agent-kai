# ASO v3 — Resolving the 5 Blockers

## Blocker 1: Explicit validation contract

```
Stage 1: IN-SAMPLE TUNING
  - Data: first 60% of available history
  - Purpose: parameter search (hill-climb or LLM mutation)
  - Gate: none — all mutations get tested here
                    │
Stage 2: WALK-FORWARD EVALUATION
  - Data: rolling 3-month train / 1-month test windows
    across the 60% in-sample slice (e.g. 5 folds)
  - Purpose: verify the improvement isn't regime-specific
  - Gate: median fold DSR_child > DSR_parent
  - DSR computed on: net-of-cost equity curve returns
  - Trial count N for DSR: total accepted + rejected
    mutations on this lineage (tracks researcher degrees
    of freedom)
  - Hard constraints checked per fold (via check_sample_size):
    - max drawdown ≤ 20%
    - total trades ≥ 50 per fold (100 if overlap-adjusted)
    - turnover ≤ 500% annualized
  - If ANY fold violates a hard constraint: REJECT
                    │
Stage 3: LOCKBOX AUDIT
  - Data: next 20% of history (never seen during tuning)
  - Purpose: catch overfitting that survives walk-forward
  - Gate: DSR_child > 0 on lockbox data (absolute, not
    relative — must show positive risk-adjusted return
    on truly unseen data)
  - Hard constraints: same as Stage 2
  - Usage cap: each lockbox slice can be used for ≤ 5
    acceptance decisions. After 5, rotate to a new time
    slice (shift forward, shrink in-sample if needed)
                    │
Stage 4: SHADOW PAPER TRADING
  - Data: live market prices, simulated fills
  - Duration: minimum 14 days
  - Purpose: verify the strategy works in real-time, not
    just on historical data
  - Gate: positive net return over shadow period, max
    drawdown ≤ hard limit, ≥ 10 trades (via check_sample_size
    with stage="shadow"; extend shadow if < 10 rather than
    auto-passing)
  - No LLM mutation during shadow — the strategy runs
    as-is
                    │
Stage 5: PROMOTION
  - Human approves via `/strategy promote NAME`
  - Immutable evaluation artifact attached (all stage
    results, data hashes, timestamps)
  - Artifact expires after 30 days — re-shadow required
    if stale
```

Acceptance happens at Stage 2 (walk-forward) for adding to
the candidate pool. Stage 3 (lockbox) gates entry to shadow.
Stage 4 (shadow) gates promotion to active.

## Blocker 2: Sample-size rule

**Resolution: drop "effective N" from v1, use a defensible
alternative.**

v1 uses three sample-size guards instead of one:

1. **Raw trade count ≥ 50 per fold** — coarse but unambiguous
2. **Holding-period overlap penalty**: if average bars held >
   (total bars / total trades × 0.5), flag as "clustered
   bets" and raise the trade count threshold to 100
3. **Symbol concentration guard**: if >80% of trades are on
   one symbol in a multi-symbol universe, flag as
   "undiversified" and require the single-symbol trade
   count alone to exceed 50

This is simpler than a full effective-N estimator but catches
the two most common crypto clustering modes (long holds +
single-symbol concentration). v2 can add formal effective-N
via holding-time overlap correction.

The `min effective N` language is removed from the v2 response.
Hard constraints in the acceptance gate use the three guards
above.

## Blocker 3: Freeze v1 execution semantics

v1 execution is a hardcoded constant, not a configurable block:

```python
V1_EXECUTION = {
    "order_type": "market",
    "entry_timing": "bar_close",   # trade at the close of the signal bar
    "exit_timing": "bar_close",    # stop/TP checked at close, filled at close
    "fee_pct": 0.001,              # 10 bps taker
    "slippage_pct": 0.001,         # 10 bps slippage
    "spread_pct": 0.0005,          # 5 bps half-spread
    "partial_fills": False,        # all-or-nothing
    "reduce_only_exits": True,
}
```

The YAML `costs` section exists but is ONLY for the three
fee/slippage/spread fields. No `execution` block in v1 YAML.
The IR validator rejects any YAML that includes `execution`,
`order_type`, `limit`, `stop`, or `maker_taker` — these are
v2 extensions.

Fill simulation in the executor:
- Entry: trade at signal-bar close price + slippage + spread
- Exit (stop): trade at stop price + slippage (if bar low
  breaches stop level, fill at stop level, not bar close)
- Exit (TP): trade at TP price - slippage (if bar high
  breaches TP level, fill at TP level)
- Exit (time): trade at bar close + slippage
- Fee: applied to both entry and exit notional

This is explicit, testable, and covers the v1 subset without
pretending to simulate limit orders or maker fills.

## Blocker 4: Pool selection + exploration policy

### Selection: Thompson Sampling with lineage caps

Each cycle selects a strategy to iterate on:

1. **Score each candidate lineage** by:
   - Recent DSR (quality signal)
   - Iterations since last improvement (staleness)
   - Total iterations on this lineage (diminishing returns)
2. **Thompson Sampling**: sample from Beta(successes+1,
   failures+1) per lineage, pick the highest sample. This
   balances exploitation (iterate on the best) vs exploration
   (try neglected lineages).
3. **Per-lineage iteration cap**: max 50 iterations on one
   lineage before forced reset or retirement to graveyard.
4. **Exploration budget**: 20% of cycles are forced-random:
   pick a random candidate, not the Thompson winner. This
   prevents lock-in.

### Novelty scoring

When the LLM proposes a structural mutation, score its
novelty against the lineage history:
- If the same indicator was already added and removed on
  this lineage: novelty = 0 (circular)
- If the mutation type hasn't been tried on this lineage:
  novelty = 1.0
- Reject mutations with novelty < 0.2

### Branch reset

After 10 consecutive rejected mutations on a lineage:
- Fork a new lineage from the best historical version of
  this strategy (not the current version)
- OR seed a completely new strategy template from the LLM
  ("design a mean-reversion strategy for ETH-USD 4h")
- The stale lineage goes to graveyard

### Pool diversity

Maintain ≥ 3 active lineages with pairwise daily-return
correlation < 0.7. If all candidates converge to correlated
strategies, force-seed a new orthogonal lineage.

## Blocker 5: Split boundaries + warm-up

### Warm-up protocol

1. All indicators require warm-up bars (e.g. RSI_14 needs ≥ 14
   bars, EMA_50 needs ≥ 50 bars)
2. The IR computes `max_warmup = max(indicator.period for all
   indicators)` and adds a 50% safety buffer:
   `warmup_bars = ceil(max_warmup * 1.5)`
3. The executor prepends `warmup_bars` to every data slice but
   excludes them from scoring:
   - Indicators are computed on the full slice (warmup + scoring)
   - Trades can only open after bar `warmup_bars`
   - Metrics are computed only on trades after `warmup_bars`

### Split boundaries

- In-sample / lockbox / shadow boundaries are defined by
  timestamp, not bar index
- Each boundary has an explicit `warmup_buffer` of
  `warmup_bars × timeframe_minutes` added before the scoring
  window
- No trade from the previous split can carry over into the
  next split (positions are force-closed at the boundary)
- Benchmark returns are computed on the same scoring window
  (after warmup), aligned to the same timestamps

### Benchmark alignment

- For each split, benchmark returns use the same start/end
  timestamps as the scored portion of the strategy
- Cash benchmark is always 0% (trivial but useful as a floor)
- Symbol-matched buy-and-hold starts at the first bar of the
  scoring window
- No look-ahead in benchmark computation

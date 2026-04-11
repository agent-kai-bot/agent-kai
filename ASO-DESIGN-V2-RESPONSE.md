# ASO Design v2 — Response to Critic Review

## Accepted changes (agree, will integrate)

### 1. Narrow v1 aggressively (Section 1, 7)
**Agree completely.** v1 scope locks to:
- Single-symbol, single-timeframe, long-only
- Market-entry only (no limit orders)
- Fixed fee + slippage model (configurable, not dynamic)
- YAML fields outside this scope → compile-time error
- The optimizer cannot propose mutations outside the executor's
  capability set

### 2. Strategy IR layer (Section 1, 7)
**Agree.** Insert: `YAML → IR (typed, validated) → Executor`.
The LLM mutates the IR, not raw YAML strings. The IR has a
capability matrix — the optimizer can only propose mutations the
executor knows how to run. This is the single highest-value
change from the review.

### 3. Move walk-forward into Phase 3 (Section 5, 9)
**Agree.** The reviewer is right that autonomy without nested
validation is dangerous. Reordered rollout:
- Phase 1: YAML + IR + executor + metrics
- Phase 2: LLM analyst + manual iteration
- Phase 3: Walk-forward validation + formal acceptance test +
  THEN autonomous loop
- Phase 4: Monte Carlo, regime testing, advanced features

### 4. Formal acceptance test (Section 5, 7)
**Agree.** Replace "beat parent on 3/5 metrics" with:
- Deflated Sharpe Ratio (DSR) as primary acceptance gate
- Hard constraints: max drawdown, turnover, min effective N
- Accept iff DSR_child > DSR_parent AND all constraints pass
- No arbitrary metric counting

### 5. Data provenance (Section 1)
**Agree.** Every backtest run persists:
- Dataset hash (SHA256 of the OHLCV used)
- Source + fetch timestamp
- Fee/slippage model version
- Executor version
- LLM model + prompt version
- This is non-negotiable for reproducibility

### 6. SQLite over JSON files (Section 6, 7)
**Agree.** Move from `lineage.json` / `iteration_log.jsonl` to
SQLite. Tables: strategies, runs, artifacts, mutations, approvals.
Immutable run records. Optimistic locking for concurrent access.

### 7. Pre-LLM diagnostic gate (Section 3, 7)
**Agree.** Before the LLM sees anything:
- Data completeness check (missing bars, stale prices)
- Benchmark integrity check
- Minimum effective sample size check
- Strategy sanity check (does it even trade?)
- If any fail → `INSUFFICIENT_EVIDENCE`, skip cycle

### 8. Split optimization modes (Section 7)
**Agree.** Two mutation modes:
- **Parameter tuning**: hill-climbing without LLM (grid/random
  search, cheaper, faster)
- **Structural mutations**: LLM proposes indicator/filter
  changes, timeframe changes, entry/exit logic changes
Alternate between them. Parameter tuning is 10x cheaper.

### 9. OOS lockbox rotation (Section 2)
**Agree.** The same OOS window used 100 times becomes training
data. Fix: rotating lockbox with a cap (N=5 uses per window),
then rotate to a new time slice.

### 10. Stagnation detection (Section 6)
**Agree.** If the optimizer has rejected N consecutive mutations
(default 10) on the same lineage, trigger a structural reset:
force the LLM to propose a fundamentally different approach
(new indicators, new timeframe, new entry logic), not another
parameter tweak.

### 11. Add cost model to YAML (Section 4)
**Agree.** Add:
```yaml
costs:
  fee_pct: 0.001      # 10 bps maker
  slippage_pct: 0.001  # 10 bps slippage
  spread_pct: 0.0005   # 5 bps spread
```
Gross vs net P&L reported separately. Fee burden as % of gross
edge is a key diagnostic.

### 12. Crypto-specific benchmarks (Section 5)
**Agree.** Replace single BTC benchmark with:
- Cash (0% return — "does this beat doing nothing?")
- Symbol-matched buy-and-hold
- Equal-weight universe buy-and-hold
Selected based on strategy type.

### 13. Tail-risk metrics (Section 5)
**Agree.** Add: CVaR/expected shortfall (95th, 99th),
time-under-water, rolling drawdown by window. These matter
more than Sharpe in crypto.

### 14. Execution block in YAML (Section 4)
**Agree for v2.** v1 assumes market-on-close execution. v2 adds:
```yaml
execution:
  order_type: market       # market | limit | stop
  entry_timing: bar_close  # bar_close | next_open
  maker_taker: taker       # maker | taker
```

### 15. Paper-trading shadow before promotion (Section 7)
**Agree.** Candidate → Shadow (paper trading for N days) →
Active. No direct candidate-to-active path.

### 16. Diversity-aware pool management (Section 7)
**Agree.** Keep orthogonal strategies by correlation, regime
behavior, and turnover profile. Don't converge the entire pool
onto one market regime.

---

## Partially accepted (agree with caveat)

### 17. Boolean expression trees for entry/exit (Section 4)
Agree this is needed eventually, but NOT in v1. v1 uses implicit
AND (all conditions must be true). v2 adds OR/NOT/grouping.
The IR layer makes this a clean future extension.

### 18. Multi-timeframe (Section 4)
Explicitly blocked in v1. Compile-time error if the YAML
references multiple timeframes. v2 design question.

### 19. Scale-in/scale-out/partial exits (Section 4)
Out of scope for v1. v1 is all-in/all-out. The position
management block is a v2 feature.

### 20. Effective independent observations (Section 5)
Agree this is better than raw trade count. But implementing
proper effective-N estimation is a research project. v1 uses
raw trade count ≥ 50 (raised from 30 per the review's concern).
v2 adds holding-time overlap correction.

---

## Disagreements (with rationale)

### 21. "Single weakest metric" is too simplistic (Section 3)
**Partially disagree.** The reviewer says the LLM should handle
"coherent failure modes spanning multiple parameters." I agree
in principle, but in practice, letting the LLM propose bundled
hypotheses increases the mutation search space and makes it
harder to attribute improvement to specific changes. Fix:
keep "focus on one weakness" as the default, but add an explicit
"bundled hypothesis" mode that the pre-LLM diagnostic can
trigger when it detects correlated failures (e.g. low trade
count + high win rate = overly strict entry).

### 22. Convergence rule is fragile (Section 3)
The reviewer wants "no material improvement across N iterations"
instead of point thresholds. I agree the thresholds alone are
insufficient, but "N iterations of no improvement" can also be
gamed (tiny improvements that clear the bar but don't compound).
Fix: use BOTH — point thresholds as a "good enough" check AND
N-iteration stagnation as a "stop trying" check. Converged =
either the strategy meets quality thresholds on OOS data OR 15
consecutive iterations failed to improve DSR by > 0.05.

---

## Key design changes for v2

1. **IR layer**: YAML → typed IR → validator → executor
2. **Formal acceptance**: DSR-based, not metric counting
3. **Walk-forward in Phase 3**: no autonomy without nested validation
4. **SQLite state**: replace JSON files
5. **Pre-LLM diagnostics**: data + sample quality gate
6. **Dual mutation mode**: parameter tuning (no LLM) + structural (LLM)
7. **Narrowed v1**: single-symbol, single-tf, long-only, market-only
8. **Cost model**: fees + slippage in YAML, gross/net reporting
9. **Crypto benchmarks**: cash, symbol-matched, equal-weight
10. **Tail-risk metrics**: CVaR, time-under-water, rolling drawdown
11. **Data provenance**: hash + version + timestamp on every run
12. **Rotating lockbox**: cap OOS window reuse
13. **Stagnation detection**: force structural reset after N rejects
14. **Paper-trading shadow**: required before promotion
15. **Diversity-aware pool**: correlation-based orthogonality

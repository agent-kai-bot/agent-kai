# ASO v3 — Sample-Size Rule Fix

## The contradiction

Stage 2 hard constraints said "total trades ≥ 30 per fold".
Blocker 2 fix said "raw trade count ≥ 50 per fold".
The symbol concentration guard is irrelevant in single-symbol v1.

## Unified v1 sample-size rule

One rule, one number, one place:

**v1 minimum trade count: 50 trades per walk-forward fold.**

Applied at:
- Stage 2 (walk-forward): each fold must have ≥ 50 trades.
  If any fold has < 50, REJECT.
- Stage 3 (lockbox): the lockbox period must have ≥ 50 trades.
  If < 50, REJECT.
- Stage 4 (shadow): the shadow period must have ≥ 10 trades
  (lower bar because shadow is only 14 days — 50 would be
  unreasonable for most timeframes).

**The "30 trades" language is deleted everywhere.** 50 is the
single v1 threshold.

### Holding-period overlap guard (v1)

In addition to the raw 50-trade minimum:

If `avg_bars_held > (fold_total_bars / fold_trades) × 0.5`:
→ raise the threshold to 100 trades for that fold.

This catches strategies that hold positions for so long that
50 "trades" are really 10 independent bets with overlapping
exposure.

### Symbol concentration guard — REMOVED from v1

v1 is single-symbol. The guard is meaningless. It returns in v2
when multi-symbol support lands.

### Where this lives in the codebase

One function: `check_sample_size(fold_metrics) -> bool`
Called by the walk-forward evaluator and lockbox auditor.
No ambiguity, no alternative paths, no conflicting constants.

```python
MIN_TRADES_V1 = 50
MIN_TRADES_CLUSTERED_V1 = 100
MIN_TRADES_SHADOW_V1 = 10
OVERLAP_THRESHOLD = 0.5

def check_sample_size(
    total_trades: int,
    avg_bars_held: float,
    total_bars: int,
    stage: str,  # "walk_forward" | "lockbox" | "shadow"
) -> tuple[bool, str]:
    if stage == "shadow":
        if total_trades < MIN_TRADES_SHADOW_V1:
            return False, f"Shadow needs ≥{MIN_TRADES_SHADOW_V1} trades, got {total_trades}"
        return True, "ok"

    min_required = MIN_TRADES_V1
    if total_trades > 0:
        avg_trade_spacing = total_bars / total_trades
        if avg_bars_held > avg_trade_spacing * OVERLAP_THRESHOLD:
            min_required = MIN_TRADES_CLUSTERED_V1

    if total_trades < min_required:
        return False, f"Need ≥{min_required} trades (overlap-adjusted), got {total_trades}"
    return True, "ok"
```

This is the ONLY place sample-size decisions are made. No
other part of the system checks trade counts independently.

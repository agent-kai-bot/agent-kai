---
name: portfolio-heat-check
description: Sum total concurrent open risk across all positions and block new trades if aggregate risk is too high
category: risk
tags: [risk, portfolio, heat, aggregate, exposure]
---
# Portfolio heat check

## When to use
Run this before approving ANY new trade, regardless of which sizing skill is being used. It's a cross-check on whether the portfolio as a whole can afford one more position at all.

## The rule
Total concurrent dollar-at-risk across all open positions plus the proposed new position must not exceed **4% of current equity**.

"Dollar at risk" for a position = `|entry - stop| * position_qty`. It's NOT the notional exposure — it's the maximum loss if every stop fires. A portfolio can have 20% of equity in open notional exposure but only 3% of equity in stop-gated risk, and that's fine; what matters for heat is the stop-gated number.

## Inputs needed
1. All open positions with their entry prices, current quantities, and stop levels: `get_positions()`. If the positions tool doesn't store stops, read them from the trader's workspace portfolio.json or the paper trading engine.
2. The proposed trade's entry, stop, and quantity.
3. Current equity: from `get_positions()` aggregate or the paper trading engine state.

## Calculation
```
current_heat = sum(|entry_i - stop_i| * qty_i for each open position i)
proposed_heat = |entry_new - stop_new| * qty_new
total_heat = current_heat + proposed_heat
heat_pct = total_heat / equity
```

## Decision procedure
1. Compute `heat_pct`.
2. If `heat_pct <= 0.04` → approve the heat check (the sizing skill still has to approve too).
3. If `0.04 < heat_pct <= 0.05` → approve with a warning: "Heat is 4.1% post-trade, room for only 1 more scale-in before the cap." Log the warning.
4. If `heat_pct > 0.05` → reject. Suggest specific mitigations:
   - "Close position X (risk $Y) to free up heat"
   - "Reduce proposed size to Q so heat stays under 4%"
   - "Move the stop on position Z to break-even (eliminates its heat contribution)"
5. If any open position has no stop, treat its risk as infinite for this calculation and reject the new trade until that position has a stop set.

## Logging
```
{timestamp} HEAT_CHECK {result} current_heat={current_usd} proposed={proposed_usd} total={total_usd} pct={heat_pct}
```

## Pitfalls
- **Counting break-even positions as heat.** Once a stop is at or past break-even, the position's dollar risk is zero (or negative — a free ride). Don't count these in the heat sum. A position whose stop is above entry for a long trade has `|entry - stop|` but that's a guaranteed profit, not risk.
- **Forgetting to include the proposed trade.** The check is pre-trade, so the new trade's risk must be added to current heat, not checked alone.
- **Using notional instead of risk.** 20% notional exposure with tight stops can be safer than 10% notional exposure with loose stops. The heat check is about stop-gated risk, not notional.
- **Allowing approval when a legacy position has no stop.** A position with no stop has unknown downside and must block the heat check until addressed.

## Verification
Decision output must include:
- [ ] Current heat (USD and %)
- [ ] Proposed incremental heat
- [ ] Total post-trade heat (USD and %)
- [ ] Specific positions flagged as missing stops, if any
- [ ] The decision (approve / approve_with_warning / reject) with the reason

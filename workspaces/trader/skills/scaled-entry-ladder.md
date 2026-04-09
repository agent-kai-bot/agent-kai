---
name: scaled-entry-ladder
description: Build a position in 2-3 scaled entries across a pullback zone instead of one lump entry
category: execution
tags: [execution, scaling, position-building, entry]
---
# Scaled entry ladder

## When to use
The analyst has given you a target entry zone (not a single point), and volatility is high enough that a single limit might miss the fill or buy the worst price in the zone. Scaling lets you average into the zone while keeping total risk constant.

## Preconditions
- Analyst signal defines an entry zone with both an ideal entry and a worst-acceptable entry.
- Stop loss level is defined (this is critical — it's how you compute risk per unit regardless of fill price).
- Cash available ≥ full intended position size.

## Sizing math
Your total position risk stays constant regardless of the number of ladder rungs. Given:
- `risk_budget_usd` = the dollar risk you're allowed to take on this trade
- `stop` = the stop loss price
- Ladder prices: `p1, p2, p3` (top of zone, middle, bottom)

For each rung, the per-rung risk is `(rung_price - stop) * rung_qty`. Solve so the sum equals `risk_budget_usd`:

```
rung_qty_i = (risk_budget_usd / 3) / (p_i - stop)
```

Each rung has the same dollar risk. Rungs closer to the stop get more size; rungs further from the stop get less. This is mathematically correct — do not equal-size the rungs.

## Steps
1. `get_latest_price(symbol)` — confirm price is still inside or above the entry zone.
2. Compute `p1, p2, p3` from the analyst's zone:
   - p1 = top of zone (first / worst entry)
   - p2 = middle of zone
   - p3 = bottom of zone (best entry)
3. Compute per-rung qty via the formula above.
4. Place three limits simultaneously (each with the same stop, each with the same take profit):
   - `place_order(symbol, side='buy', type='limit', price=p1, qty=q1, sl=stop, tp=target)`
   - `place_order(symbol, side='buy', type='limit', price=p2, qty=q2, sl=stop, tp=target)`
   - `place_order(symbol, side='buy', type='limit', price=p3, qty=q3, sl=stop, tp=target)`
5. Wait for fills. You now have three possible states: 1 filled, 2 filled, or all 3 filled.
6. After each fill, `nats_publish("portfolio.positions", ...)` with the new average entry.
7. When price breaks above the zone (trade working) without all 3 filled: cancel the unfilled rungs. Do NOT chase.
8. When price breaks the stop (trade invalidated): all unfilled rungs auto-cancel when the stop fires on the filled portion. Confirm with `get_positions`.

## Failure handling
- **Only the top rung fills, then price runs.** Accept it. You have a smaller position than planned; the reward is also proportionally smaller, but the risk is proportionally smaller too. Do not add chase orders.
- **All three fill but the stop fires.** This is the designed worst case. Total loss = `risk_budget_usd`. Verify the stop executed correctly via `get_positions`.
- **Broker/paper engine rejects simultaneous orders.** Fall back to sequential placement (p1 first; when filled, place p2; when filled, place p3). Document this in the skill if it keeps happening.

## Clean unwind
If the setup invalidates before any fill:
1. Cancel all three rungs.
2. `nats_publish("trade.execution", {result: "unwound", filled: 0})`.

If the setup invalidates after partial fills:
1. Cancel unfilled rungs.
2. Close the filled portion at market (or let the stop take care of it if already close).
3. `nats_publish("trade.execution", {result: "unwound", filled: N})`.

## Pitfalls
- **Equal-sizing the rungs.** This makes your per-rung risk unequal and means a shallow pullback costs you more than a deep one. Use the formula.
- **Different stops on different rungs.** Don't. The invalidation level is the invalidation level. A single stop keeps the math clean.
- **Skipping the cancel step when the trade works.** Leaving unfilled rungs active after a clean breakout means a deep retest will fill you way below your cost basis and blow the risk budget.
- **Using this in chop.** In a tangled regime, every rung fills as price oscillates and you end up with max size at the worst average. Require a defined regime before scaling.

## Verification
- [ ] Each rung has the same stop loss.
- [ ] Per-rung sizes satisfy `(p_i - stop) * q_i ≈ risk_budget_usd / 3`.
- [ ] All fills published to NATS.
- [ ] Unfilled rungs cancelled when trade breaks out of the zone.

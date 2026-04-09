---
name: break-even-management
description: Move a filled trade's stop to break-even once the trade reaches a defined profit milestone
category: execution
tags: [execution, stop, break-even, position-management]
---
# Break-even stop management

## When to use
A filled long or short trade has moved in your favor and hit the first defined "safety" milestone (usually 1R of profit, where R = initial risk per unit). Moving the stop to break-even at this point turns the trade into a risk-free hold. Doing it too early gets you stopped out by noise; doing it too late leaves free risk on the table.

## Preconditions
- The trade is currently open (`get_positions(symbol)` returns a non-zero position).
- The entry price and initial stop are known (stored at trade placement time — log them in your chat history or a workspace file).
- Current price has moved favorable by at least 1R from entry.

## Definitions
- `entry` = average entry price (get from `get_positions`)
- `initial_stop` = the stop level at the time of entry
- `R` = absolute value of `(entry - initial_stop)` — dollar risk per unit
- `be_level` = entry ± a small buffer (0.1-0.25 R) to avoid break-even wicks
- `be_trigger` = price at which you move to break-even (usually `entry + 1R` for longs, `entry - 1R` for shorts)

## Steps
1. `get_positions(symbol)` — confirm the position is still open and get the current quantity.
2. `get_latest_price(symbol)` — get current price.
3. Check whether the price has reached `be_trigger`:
   - For longs: `current_price >= entry + 1R`
   - For shorts: `current_price <= entry - 1R`
4. If the trigger is hit, compute `be_level`:
   - For longs: `be_level = entry + 0.15 * R`
   - For shorts: `be_level = entry - 0.15 * R`
   - The small offset protects against wick fills around entry.
5. Move the stop via `place_order` (modify/cancel-and-replace the existing stop, or issue a new stop-loss order):
   - `place_order(symbol, side=exit_side, type='stop', price=be_level, qty=current_qty)`
6. Cancel the old stop if the paper engine requires explicit cancellation.
7. `nats_publish("portfolio.positions", {symbol, event: "break_even", new_stop: be_level})` so other agents know the trade is now risk-free.

## Failure handling
- **Stop move rejected (invalid price).** This usually means price already moved past `be_level` in the wrong direction since you checked. Re-fetch `get_latest_price` and skip the move — the trade is back in the risk zone and break-even doesn't apply right now.
- **Can't cancel the old stop.** Verify in `get_positions` that both stops aren't now active. Having two active stops on the same position is a bug that will cause a double-exit.
- **Position size changed while you were computing.** A partial exit (scale-out) or the take-profit rung hit. Re-fetch and rerun; the skill is safe to re-enter.

## Pitfalls
- **Moving to exact break-even.** Exact entry gets wicked. The 0.15R buffer is cheap insurance.
- **Moving too early (at 0.5R instead of 1R).** Half-R noise is the most common cause of unneeded stop-outs in crypto. Wait for a full R before moving.
- **Forgetting to move at all.** The whole point of this skill is discipline. If you keep forgetting, add a callback or cron to check the position every minute.
- **Running this on a position you didn't open with a clear initial stop.** You need the initial risk to compute R. If the initial stop wasn't logged, skip this skill and use a trailing stop instead.

## Verification
- [ ] Position is still open, size is non-zero.
- [ ] Current price has reached `entry ± 1R` in the correct direction.
- [ ] New stop is at `entry ± 0.15R`.
- [ ] Old stop order is cancelled.
- [ ] Break-even event is published to NATS.

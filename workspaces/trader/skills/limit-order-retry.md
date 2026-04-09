---
name: limit-order-retry
description: Retry a limit order at progressively better prices when the first placement doesn't fill
category: execution
tags: [execution, limit-order, retry, fill]
---
# Limit order retry ladder

## When to use
You placed a limit order at a specific level (e.g. the pullback level from a breakout skill), the market moved past it without filling, and the setup is still valid. Rather than chase at market, step the limit toward the market in small increments.

## Preconditions
- The setup is still valid per the analyst signal (check that the invalidation level hasn't been hit).
- Available cash ≥ the intended position size.
- The original limit has either expired or been cancelled.

## Steps
1. `get_positions(symbol)` — confirm you don't already have a partial fill from the original attempt.
2. `get_latest_price(symbol)` — establish the current market price, call it `px_now`.
3. Compute the retry ladder. If `original_limit` was the first attempt:
   - attempt 2 price = `original_limit + 0.25 * (px_now - original_limit)` (25% closer to market)
   - attempt 3 price = `original_limit + 0.50 * (px_now - original_limit)` (50% closer)
   - attempt 4 price = `original_limit + 0.75 * (px_now - original_limit)` (75% closer)
4. Place attempt 2: `place_order(symbol, side, type='limit', price=attempt_2_price, qty, sl, tp)`.
5. Wait 60 seconds. If not filled, `place_order` cancel (or place a new one at the next ladder step and cancel the previous).
6. After attempt 4 fails, STOP. Do not go to market. A 4-step ladder that didn't fill is a market that's moving faster than your entry assumption — the signal is probably stale.

## Failure handling
- **Partial fill during the ladder.** Stop the ladder. You have a position now. Move to position-management (check break-even, set stops).
- **Price breaks the invalidation level during the ladder.** Cancel all pending orders immediately. Do not enter.
- **All 4 attempts fail.** Log the failure, broadcast `nats_publish("trade.execution", {result: "no_fill", ladder: [...]})`, and return to the analyst for a refreshed signal.

## Clean unwind
If the setup becomes invalid mid-ladder:
1. Cancel any open limit order (`place_order` with a cancel action, or equivalent).
2. If a partial fill exists, close it at market: `place_order(symbol, opposite_side, type='market', qty=partial_qty)`.
3. `nats_publish("trade.execution", {result: "unwound", reason: "setup_invalidated"})`.

## Pitfalls
- **Skipping straight to attempt 4.** The ladder exists so you don't overpay. Jumping to 75% closer after attempt 1 defeats the point.
- **Forgetting to cancel the previous attempt.** Leaving a stack of 4 working limits can fill in the wrong order and give you 4x the intended position. Always cancel before placing the next rung.
- **Ladder in the wrong direction.** On a short, "closer to market" means the price goes UP toward current price. Double-check sign conventions.

## Verification
Before considering the ladder a success:
- [ ] One rung filled completely.
- [ ] Stop loss and take profit are attached to the filled position (via the same `place_order` call or a follow-up).
- [ ] The fill is broadcast via `nats_publish` to `portfolio.positions`.
- [ ] `get_positions(symbol)` shows the expected position size.

---
name: volume-profile-poc-reject
description: Use the prior session's point of control as a rejection level for mean-reversion fades
category: analysis
tags: [ta, volume, poc, vwap, mean-reversion]
---
# Volume point-of-control rejection

## When to use
Price is approaching the previous day's point of control (PoC) from outside the prior day's value area, the broader daily bias is neutral, and you want to fade the approach back to value. Works in range regimes, fails in trending regimes.

## Quick proxy for PoC when you don't have full volume profile
Our `calculate_indicator` tool doesn't yet have a direct volume profile call. Use this proxy:
- `query_ohlcv(symbol, "1h", limit=30)` — pull the last ~30 1h bars (roughly 1-1.5 days of data).
- PoC proxy = the price level of the 1h bar with the highest volume in the previous session (24 bars back to 0).
- VWAP proxy = `calculate_indicator(symbol, "VWAP", interval="1h")`.

It's an approximation — a true PoC is computed from tick/volume bins, not OHLC — but for fade setups on a rough rejection it's good enough.

## Steps
1. Establish the prior session's PoC proxy using the method above. Note the level.
2. `calculate_indicator(symbol, "ATR", interval="1h", period=14)` — get the average true range so you can reason about whether the approach is "normal" or violent.
3. Check that current price is outside value — a simple test is that current price is more than 1 ATR away from the PoC level when the approach begins. If already inside value, this skill doesn't apply.
4. Wait for price to move toward PoC. When it gets within 0.3 ATR, watch for:
   - A rejection wick on the 15m or 1h (candle whose body is < 40% of total range, on the side facing away from PoC).
   - A volume spike on the rejection bar (> 1.5x 20-bar average).
5. Check the daily bias: `calculate_indicator(symbol, "EMA", interval="4h", period=50)`. If price is pushing sharply through the 50 EMA, the regime is trending and this skill doesn't apply — PoCs get run through in trending regimes.
6. Entry = close of the rejection candle. Stop = 1 ATR beyond PoC (to account for the approximation error).
7. Target = VWAP first, then the opposite side of the value area (the far edge of the "accepted" range).

## Pitfalls
- **Trending regime.** The single biggest failure mode. In a trending day, PoCs from yesterday are just levels the market tears through. Always check regime before calling the skill applicable.
- **PoC proxy is not real PoC.** The proxy will sometimes point at a 1h high-volume bar that wasn't actually the session's highest-traded price. Treat the level as a 0.5 ATR zone, not a precise line.
- **Fading without a rejection candle.** If price trades into PoC without a clear rejection (just drifts in and drifts out), there's no fade — it's acceptance, and you should be targeting the other side of value, not fading the approach.
- **Ignoring volume on the rejection bar.** A rejection on declining volume is weak. Require the volume spike.

## Verification
- [ ] Prior-session PoC proxy is established.
- [ ] Price is currently more than 1 ATR away from PoC (outside value).
- [ ] Daily bias is neutral (not a strong trend day).
- [ ] A rejection candle with a volume spike has printed within 0.3 ATR of PoC.
- [ ] Stop can be placed 1 ATR beyond PoC with reward/risk of at least 1.5:1 to VWAP.

---
name: support-retest-bounce
description: Time a bounce entry on a retest of a recently broken-and-reclaimed support level
category: analysis
tags: [ta, support, resistance, retest, structure]
---
# Support retest bounce

## When to use
Price broke below a well-defined 1h support level within the last 10 bars, then reclaimed it on the next 1-3 bars, and is now pulling back toward it. This is the classic "broken support becomes resistance, then resistance becomes support again" sequence. The setup is invalid if price never reclaimed cleanly — a failed reclaim is a short, not a long.

## How to define "well-defined support"
A level is well-defined if it was tested at least 2 times (tops or bottoms within 0.3% of the same price) on the 1h or 4h chart over the last 100 bars. Round numbers (5000, 50000) and prior weekly highs/lows count as half a test. One touch and a guess doesn't count.

## Steps
1. `query_ohlcv(symbol, "4h", limit=50)` — check the higher-timeframe structure first.
2. Identify candidate support levels: horizontal areas where price wicked or closed at least twice.
3. `query_ohlcv(symbol, "1h", limit=50)` — confirm the level held on the lower timeframe.
4. Look for the break-reclaim sequence in the last 10 bars:
   - A 1h close below the level (the fakeout break).
   - A 1h close back above the level within 1-3 bars (the reclaim).
5. `calculate_indicator(symbol, "EMA", interval="1h", period=21)` — the 21 EMA should be above or converging with the support level during the reclaim.
6. Wait for price to pull back to within 0.5% of the reclaimed level. Entry = when the 1h forms a bullish close (any green candle with a higher low than the previous bar) at that pullback.
7. Stop = below the absolute low of the fakeout break. No exceptions.
8. First target = the swing high that formed between the reclaim and the pullback (measured move).

## Pitfalls
- **No real reclaim.** If price dipped below, then closed above on a single long-wick candle without follow-through, that's a rejection, not a reclaim. Require 2 closes above before calling it reclaimed.
- **Counter-trend.** A support bounce in a 4h downtrend is a counter-trend trade. Either reduce size dramatically or pass unless you also have a bullish 4h signal (RSI divergence, volume spike).
- **Over-eager entry.** Entering at the level without waiting for the bullish close means you're front-running a setup that hasn't triggered yet. Wait for the candle.
- **Stop too tight.** Placing the stop at the level itself gets you wicked out. The stop belongs below the fakeout wick because that wick defined "too far".

## Verification
- [ ] Support level has 2+ prior touches on 1h or 4h.
- [ ] There's a fakeout break + reclaim within the last 10 1h bars.
- [ ] 1h 21 EMA is supporting the level (at or above).
- [ ] Price has pulled back within 0.5% of the reclaimed level.
- [ ] A bullish 1h candle has closed (the trigger).
- [ ] 4h trend is neutral or up (not a downtrend).

---
name: rsi-divergence-hunt
description: Find and validate RSI bullish/bearish divergences on the 1h and 4h timeframes
category: analysis
tags: [ta, rsi, divergence, reversal, swing]
---
# RSI divergence hunt

## When to use
Looking for an exhaustion / reversal setup in a market that's been trending for at least 15 bars in the same direction. Works best at key structural levels (prior highs/lows, round numbers, daily pivot). Meaningless in chop.

## What counts as a valid divergence
- **Bullish regular divergence**: price makes a lower low, RSI makes a higher low. Seen at the bottom of a downtrend.
- **Bearish regular divergence**: price makes a higher high, RSI makes a lower high. Seen at the top of an uptrend.
- **Hidden divergences** (trend continuation) exist but are out of scope — this skill is only for regular reversal divergences.

The two price lows (or highs) must be **at least 5 bars apart** and **not more than 30 bars apart**. Closer than 5 is noise, wider than 30 is weak.

## Steps
1. `query_ohlcv(symbol, "1h", limit=60)` — get enough history to see two candidate swings.
2. `calculate_indicator(symbol, "RSI", interval="1h", period=14)` — fetch the RSI series.
3. Identify the two most recent swing lows (for bullish) or highs (for bearish) in the last 40 bars.
4. Compare price vs RSI at those two points:
   - Bullish: price[later] < price[earlier] AND rsi[later] > rsi[earlier].
   - Bearish: price[later] > price[earlier] AND rsi[later] < rsi[earlier].
5. Confirm the RSI value at the second point is in the extreme zone: <35 for bullish, >65 for bearish. Divergences outside these zones are weak.
6. Check higher timeframe context: `calculate_indicator(symbol, "EMA", interval="4h", period=50)`. Taking a bullish divergence into a strong 4h downtrend is a counter-trend trade — reduce size or pass.
7. Trigger = a 1h candle close that breaks the micro-structure formed by the second swing (break of the swing-low candle's high for bullish).
8. Stop = below/above the absolute swing extreme.
9. First target = the 1h 20 EMA (mean reversion target).

## Pitfalls
- **Two lows, same value, tiny RSI gap.** A divergence needs a meaningful price difference AND a meaningful RSI difference. If either is less than 1% price or 3 RSI points, it's noise.
- **Chasing divergences in strong trends.** A bearish divergence in a parabolic uptrend is textbook correct and still loses money. Require a structural level too — not just the indicator pattern.
- **Only looking at one timeframe.** A 1h divergence against a 4h impulse has low expectancy. Always cross-check.
- **Counting swings that aren't real swings.** A swing low needs lower lows on both sides (at least 2 bars of left-right confirmation). An in-progress low isn't a swing yet.

## Verification
Before calling the divergence tradeable:
- [ ] Two swings, 5-30 bars apart, are confirmed (not in progress).
- [ ] Price and RSI disagree in the right direction.
- [ ] RSI at the second swing is <35 (bullish) or >65 (bearish).
- [ ] 4h trend is neutral OR aligned with the divergence direction.
- [ ] There's a structural level within 1 ATR of the second swing.
- [ ] A 1h close has broken the second-swing micro-structure (the actual trigger).

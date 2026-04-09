---
name: bb-squeeze-breakout
description: Identify Bollinger Band squeezes and time the directional breakout on 1h/4h crypto
category: analysis
tags: [ta, bbands, squeeze, volatility, breakout]
---
# Bollinger Band squeeze breakout

## When to use
The 1h chart shows Bollinger Band width in its lowest ~20th percentile over the last 100 bars AND the 4h trend is defined (clear above/below the 50 EMA). This is the precondition — if either is missing, the setup doesn't exist and a breakout signal is meaningless.

## Steps
1. `query_ohlcv(symbol, "1h", limit=120)` — fetch enough bars to see historical squeeze widths.
2. `calculate_indicator(symbol, "BBANDS", interval="1h", period=20, std=2)` — get upper, middle, lower.
3. Compute band width = (upper - lower) / middle. Compare the current value to the last 100 bars. A squeeze = current width in the bottom 20%.
4. `calculate_indicator(symbol, "EMA", interval="4h", period=50)` — confirm the higher-timeframe regime.
   - Price > 50 EMA → bullish bias, look for upside breakouts only.
   - Price < 50 EMA → bearish bias, look for downside breakouts only.
5. Wait for a 1h close **outside** the BB in the direction of the bias.
6. Check volume: `calculate_indicator(symbol, "VWAP", interval="1h")` and compare the breakout bar's volume to the 20-bar average. Breakout volume must be >1.5x average.
7. Entry = pullback to the broken band on the next 1-2 bars, NOT chase the breakout close.
8. Initial stop = the opposite band (middle band for tight, opposite band for loose).

## Pitfalls
- **Squeeze inside a news window.** A squeeze right before a macro print is not a tradeable squeeze — the first move after the news is volatility expansion for its own sake, not a trend. Skip any squeeze where the breakout would fire within 30 min of a high-impact news release.
- **First squeeze after a multi-week trend.** These are usually fakeouts and mean-reversion traps. The first clean squeeze after a range forms (not after a trend exhausts) is the one with the best expectancy.
- **Low-volume breakout.** A breakout on declining volume is a bait. The volume check in step 6 is not optional.
- **Picking a side because "it looks like it's about to go".** The 4h EMA bias exists so you don't have to guess direction. If the 4h EMA is flat (price is coiled around it), the skill doesn't apply — wait.

## Verification
All of the following must be true before placing a trade:
- [ ] BB width on 1h is in the bottom 20% of the last 100 bars.
- [ ] 4h price is clearly above OR below the 50 EMA (not hugging it).
- [ ] 1h candle closed outside the band in the direction of the 4h bias.
- [ ] Breakout bar volume > 1.5x the 20-bar average.
- [ ] No high-impact news in the next 30 min.

If any box is unchecked, pass on the trade.

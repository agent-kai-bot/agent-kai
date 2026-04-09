---
name: macd-zero-cross
description: Trade the MACD line crossing zero as a momentum-shift confirmation, not a trigger
category: analysis
tags: [ta, macd, momentum, trend-change]
---
# MACD zero-line cross

## When to use
You already have a thesis that the trend is changing (structure break, divergence, broken moving-average ribbon) and you want an objective confirmation that momentum has actually flipped. MACD crossing zero is a **confirmation**, not a standalone entry. If you use it as a standalone trigger, you'll get whipsawed in chop.

## Steps
1. `calculate_indicator(symbol, "MACD", interval="1h", fast=12, slow=26, signal=9)` — fetch the full MACD, signal, and histogram.
2. Look at the last 5 1h bars:
   - Bullish cross: MACD line (not the signal line) goes from negative to positive, preferably with the histogram already expanding positive for 2+ bars before the cross.
   - Bearish cross: mirror.
3. Check that the cross happened ABOVE the noise band: if |MACD - signal| is smaller than the median |MACD - signal| of the last 50 bars, the cross is in chop and doesn't count.
4. Confirm with higher-timeframe momentum: `calculate_indicator(symbol, "MACD", interval="4h", ...)`. The 4h histogram should be at least leaning in the same direction as the 1h cross (not strictly positive, but trending toward it).
5. Your trigger is the close of the 1h bar where the cross completed. Entry = next bar open.
6. Stop = the swing low (for longs) or swing high (for shorts) that formed immediately before the cross.

## Pitfalls
- **Cross in chop.** Flat price + flat MACD near zero produces a dozen crosses in a row, none tradeable. The noise-band check in step 3 is the guardrail.
- **Histogram shape.** A cross with a shrinking histogram is suspicious — momentum should be expanding as the line pushes through zero, not collapsing into it. If the histogram peaks 3 bars before the cross, the cross is a lagging signal of a move that's already over.
- **Using MACD alone.** This skill is explicitly NOT a standalone trigger. You must have an independent reason to believe the trend is changing. If you don't, the cross is just noise.
- **Ignoring the 4h.** Trading a 1h bullish cross into a sharply-down 4h MACD is fighting the larger-timeframe momentum. Either skip or treat as a scalp only.

## Verification
- [ ] You already have a separate thesis (structure break / divergence / MA break) that the trend is changing.
- [ ] 1h MACD line crossed zero in the last 1-3 bars.
- [ ] |MACD - signal| on the cross bar is at or above the 50-bar median.
- [ ] The histogram has been expanding for at least 2 bars before the cross.
- [ ] 4h MACD is leaning in the same direction (histogram improving).

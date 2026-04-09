---
name: moving-average-ribbon-stack
description: Read the stacking order of a 5/10/20/50 EMA ribbon to classify trend health and phase
category: analysis
tags: [ta, ema, ribbon, trend, regime]
---
# Moving average ribbon stack

## When to use
At the start of any analysis task, before you do anything else, to quickly classify the current regime. The ribbon stack is a **regime filter**, not a trade signal — it tells you which skills are even applicable on the current chart.

## The four states
Given a ribbon of EMAs (5, 10, 20, 50) on the 1h chart:

1. **Bullish stacked** — 5 > 10 > 20 > 50, all rising. Regime = trending up. Only trend-following and pullback-buy skills apply. Do not take counter-trend fades.
2. **Bearish stacked** — 5 < 10 < 20 < 50, all falling. Regime = trending down. Mirror of #1.
3. **Compressed** — ribbon is bunched within a small range (max - min < 0.3 ATR), slopes roughly flat. Regime = consolidation / pre-breakout. Breakout and squeeze skills apply; counter-trend and pullback skills don't.
4. **Tangled** — EMAs are crossing each other repeatedly, no clear order. Regime = chop. No directional skill applies. Stand down, or only take pure mean-reversion fades at extremes.

## Steps
1. `calculate_indicator(symbol, "EMA", interval="1h", period=5)`
2. `calculate_indicator(symbol, "EMA", interval="1h", period=10)`
3. `calculate_indicator(symbol, "EMA", interval="1h", period=20)`
4. `calculate_indicator(symbol, "EMA", interval="1h", period=50)`
5. Take the most recent value of each. Check the ordering:
   - If sorted high-to-low gives `[5, 10, 20, 50]` → bullish stacked.
   - If sorted high-to-low gives `[50, 20, 10, 5]` → bearish stacked.
   - Otherwise, check compression vs tangled.
6. `calculate_indicator(symbol, "ATR", interval="1h", period=14)` — get ATR for the compression check.
7. Compute ribbon spread = `max(emas) - min(emas)`. If `spread < 0.3 * ATR`, it's compressed. Otherwise, it's tangled (mixed ordering with significant spread = chop).
8. State the regime explicitly in your analysis output: "Regime: bullish stacked, pullback buys apply." This forces downstream logic to stay consistent.

## Pitfalls
- **Classifying a transition as a regime.** When price is flipping from trending to compressed, you'll see `[5, 10]` close together and `[20, 50]` close together, but with a gap between the two pairs. That's a weakening trend, not a clean state. Call it "transitioning from bullish to compressed" — don't pretend it's either.
- **Using ribbon on the wrong timeframe.** 1h is the default for intraday swings. For scalps use 5m. For position trades use 4h. Mixing timeframes ("the 1h is stacked bullish so I'll fade on 5m") is how you get run over.
- **Ignoring the 4h context.** A bullish 1h stack inside a bearish 4h stack is a counter-trend bounce. Call this out explicitly; don't let the 1h stack imply "trending up" without qualifying it.
- **Confusing compression with bullish stacked.** A ribbon that's tightly bunched but ordered 5>10>20>50 is still compressed, not trending — the rising order is an artifact of slow-moving averages. Require spread > 0.3 ATR to call it a real stack.

## Verification
Output should include:
- The four 1h EMA values (numeric).
- The ribbon spread divided by ATR.
- One of the four regime labels.
- A list of which skill categories are applicable and which aren't.

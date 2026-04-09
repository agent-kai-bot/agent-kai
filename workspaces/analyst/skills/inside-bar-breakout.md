---
name: inside-bar-breakout
description: Trade the break of a 1h or 4h inside bar as a continuation signal in a defined trend
category: analysis
tags: [ta, price-action, inside-bar, continuation]
---
# Inside bar breakout

## When to use
You've already classified the regime as bullish or bearish stacked (see `moving-average-ribbon-stack`), and you want a clean low-risk entry for a continuation trade. Inside bars are pause bars; breaking them in the direction of the trend is a high-probability continuation trigger.

## What counts as an inside bar
A 1h (or 4h) candle whose high is ≤ the previous bar's high AND whose low is ≥ the previous bar's low. The entire range of the inside bar is contained within the prior bar.

Two extra qualifiers:
- **Depth**: the inside bar's range should be ≤ 60% of the prior bar's range. Marginal inside bars (95% of the prior range) don't really compress volatility.
- **Position**: the inside bar should sit in the top half of the prior bar (for longs) or bottom half (for shorts). An inside bar floating in the middle is indecisive, not continuation.

## Steps
1. Confirm regime via `moving-average-ribbon-stack` — only proceed if bullish or bearish stacked on 1h.
2. `query_ohlcv(symbol, "1h", limit=30)` — fetch recent bars.
3. Scan the last 3 1h candles for an inside-bar pattern meeting the depth + position qualifiers.
4. Identify the breakout level:
   - For longs: the inside bar's high.
   - For shorts: the inside bar's low.
5. Entry trigger = a 1h close beyond the breakout level. **Do not enter on a wick through** — require the close.
6. Stop = the opposite side of the inside bar (its low for longs, high for shorts).
7. First target = 1x the prior bar's range, projected from the breakout level.

## Pitfalls
- **Taking inside bars in chop.** An inside bar in a tangled regime is just a small bar — it carries no continuation signal. The regime filter is mandatory.
- **Entering on a wick.** Intrabar wicks through the breakout level are traps, especially on crypto where wicks are violent. Always wait for the 1h close.
- **Stop too tight.** Placing the stop just beyond the inside bar's midpoint instead of the opposite side gets you shaken out by the first retest. The opposite side IS the invalidation level — honor it.
- **Trading the 5th inside bar in a row.** Multiple stacked inside bars signal coiling / compression, not continuation. If you see 3+ inside bars in a row, switch to the squeeze skill (`bb-squeeze-breakout`).

## Verification
- [ ] Regime is bullish or bearish stacked on 1h.
- [ ] The most recent 1h candle meets the inside-bar definition (range contained in the prior bar).
- [ ] Inside bar depth ≤ 60% of prior bar's range.
- [ ] Inside bar sits in the top/bottom half of the prior bar (matching trade direction).
- [ ] A close beyond the inside bar has printed (the trigger).
- [ ] Stop at the opposite side of the inside bar gives a reward/risk of at least 1.5:1 to the first target.

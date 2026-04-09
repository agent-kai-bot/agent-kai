---
name: news-window-hedge
description: Reduce or flatten exposure before a known high-impact news release, restore after the dust settles
category: execution
tags: [execution, risk, news, hedging, event-driven]
---
# News window hedge

## When to use
A high-impact news release (CPI, FOMC, NFP, major exchange announcement, major protocol event) is scheduled within the next 30 minutes and you have open positions that would be blown out by a 2-3% instantaneous gap. This skill is about surviving the event, not trading it. Event trading is a different skill entirely.

## Preconditions
- News time is known and within the next 30 min.
- You have at least one open position in a crypto that typically reacts to macro events (BTC, ETH, SOL, large caps). Pure alt/memecoin positions usually lag macro news by 30+ minutes, so the hedge is less urgent.
- Portfolio state is known via `get_positions()`.

## Three valid responses
You do not have to pick the same one every time. Choose based on conviction and size.

1. **Flatten.** Close all exposed positions at market. Highest-certainty protection. Use when position size is large relative to portfolio (>5% of equity) or you have low conviction.
2. **Tighten.** Move all stops to tighter levels (e.g. 0.5 ATR instead of 1.5 ATR) before the event. Use when you have moderate conviction and want to let the position breathe but cap downside.
3. **Hedge.** Open an opposite position in a correlated instrument, sized to offset expected event delta. Use when positions are too illiquid to close cleanly or closing would incur significant slippage. Rarely the right choice in paper — mostly here for future real-money use.

## Steps (flatten — the default)
1. `get_positions()` — enumerate open positions.
2. For each position, capture entry price and size.
3. `get_latest_price(symbol)` for each — this is your exit benchmark.
4. `place_order(symbol, side=opposite, type='market', qty=position_qty)` for each.
5. Wait for fills, then `get_positions()` again to verify zero exposure.
6. `nats_publish("portfolio.positions", {event: "news_flatten", reason: <news_event_name>})`.
7. Record entry prices, exit prices, and intended re-entries in a workspace file so you can restore cleanly after the event.

## Steps (tighten)
1. `get_positions()`.
2. For each position, compute a tighter stop = `entry ± 0.5 * ATR` (you'll need the symbol's current 1h ATR from the analyst).
3. `place_order` to modify / replace the stop at the tighter level.
4. Set a calendar reminder to re-widen stops 30 min after the event.

## Post-event restore (if flattened)
1. Wait at least 10 minutes after the scheduled news time so initial volatility calms.
2. Re-run the analyst's original signal check — is the setup still valid?
3. If yes, re-enter per the analyst's refreshed entry zone.
4. If no, log the closed trade and move on. Don't force re-entry just because you exited.

## Pitfalls
- **Flattening inside the news window itself.** Market-order slippage during a news spike is terrible. Close BEFORE the release, not during. 5+ minutes of runway is the minimum.
- **Hedging with uncorrelated assets.** Buying ETH to hedge BTC works; buying a random memecoin to hedge BTC is just opening a second bad position.
- **Tightening stops into the noise floor.** A 0.1 ATR stop during a news release will fire on pre-release chop. 0.5 ATR is about as tight as makes sense.
- **Forgetting to re-widen stops after the event.** Leaving news-tight stops on after the event makes every subsequent trade vulnerable to normal retracement. Set a reminder.
- **Re-entering based on FOMO.** Post-event re-entry should be signal-driven. If the signal isn't back, neither are you.

## Verification
- [ ] News event time confirmed and within the hedge window.
- [ ] Chosen response (flatten / tighten / hedge) is appropriate for the position size.
- [ ] Execution completed at least 5 minutes before the scheduled release.
- [ ] Post-event state captured in a workspace file for restoration.
- [ ] NATS event published.

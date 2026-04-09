# Watchlist, positions, and alerts

The three side panels of the trading terminal — what they show, how they update, and how to interact with them. All three live in the left and right columns of the grid layout.

## Layout

```
┌────────────┬─────────────────────────────────┬─────────────┐
│ Watchlist  │ Chart                           │ Alerts      │
├────────────┼─────────────────────────────────┼─────────────┤
│ Positions  │ Chat                            │ NATS log    │
└────────────┴─────────────────────────────────┴─────────────┘
                       Status bar
                       Input
```

- **Left column:** Watchlist (top), Positions (bottom)
- **Right column:** Alerts (top), NATS log (bottom)
- **Center column:** Chart, Chat — see [chart-panel.md](chart-panel.md)

---

## Watchlist panel

A live table of crypto symbols with current price, 24h change, and the active timeframe context. Backed by `tui/panels/watchlist.py`.

### What it shows

| Column | Source |
|---|---|
| Symbol | The ticker (`BTC`, `ETH`, `SOL`, …) |
| Price | Latest close from the cloud `kai-api` market data endpoint |
| 24h Change | Percent change vs 24h ago |
| Timeframe | The user's currently-active chart timeframe (for context) |

### Default symbols

`BTC`, `ETH`, `SOL`. Configurable via the `KAI_TRACKED_SYMBOLS` environment variable (comma-separated). Set in your shell or `.env` file:

```bash
export KAI_TRACKED_SYMBOLS="BTC,ETH,SOL,DOGE,XRP,ADA"
```

### Adding a symbol

Two ways:

```
/watch DOGE                # via slash command
```

or use the `Ctrl+W` keyboard shortcut, which focuses the chat input with `/watch ` prefix so you just type the symbol and hit Enter.

The new symbol is appended to the watchlist immediately. The watchlist polls the cloud price endpoint on a fixed interval to keep prices fresh.

### Removing a symbol

Type `/watch SYMBOL` again — toggling. The handler removes the symbol if it's already in the list, otherwise adds it. (See `_handle_slash_command` `/watch` branch in `tui/terminal.py`.)

### Click-to-load on the chart

Clicking a row in the watchlist fires a `RowSelected` event that the TUI handles by loading that symbol on the chart panel at the current timeframe. This is the fastest way to flip through several symbols without typing `/chart` for each.

The row key is set to the symbol string when the row is built, so `event.row_key.value` gives the symbol directly without needing to read cell text.

### Update cadence

The watchlist polls the cloud price endpoint roughly every 10-20 seconds (the exact interval is in `tui/terminal.py` — search for `_refresh_watchlist`). For real-time updates instead of polling, the chart panel uses the WebSocket API — that's documented in [data-sources.md](data-sources.md#kai-api).

### When the watchlist is empty

A fresh TUI on a brand-new machine with no `KAI_TRACKED_SYMBOLS` set still gets `BTC, ETH, SOL` because those are the default. To start truly empty, set `KAI_TRACKED_SYMBOLS=""` and restart.

---

## Positions panel

A live table of open paper trading positions with P&L. Backed by `tui/panels/positions.py`.

### What it shows

| Column | Meaning |
|---|---|
| Symbol | The position's ticker |
| Side | LONG or SHORT |
| Qty | Position size (e.g. `0.5000` BTC) |
| Entry | Average entry price |
| Price | Current market price |
| P&L | Unrealized profit/loss in dollars (`$+125.40` / `$-75.20`) |
| P&L% | Unrealized P&L as a percent of entry value |

### When it updates

The positions panel auto-refreshes after every:

- `/buy` — once the trader sub-agent reports the order filled
- `/sell` — same
- Default agent run that mounted any of the trading tools — `_refresh_positions()` is called from the `finally` block of `_process_agent`, `_run_agent_task`, and `_run_learn_command`
- Manual `/positions` (or `/pos` alias)

There's no fixed polling interval — refreshes are triggered by events that change position state.

### How positions get there

The agent calls the `place_order` tool which writes to the paper trading portfolio (`data_api.paper_trading.portfolio` — currently a stub for the open-source agent, with a fuller implementation on the closed-source side that handles fills, fees, slippage, etc.). The TUI then calls `get_positions` and renders the result into this panel.

### Cursor

`cursor_type = "row"` — you can navigate with arrow keys when the panel has focus. There are no row-click actions yet (no "click to close position" or similar) because order management is done through `/sell` from the chat input.

---

## Alerts panel

A scrolling log of trading signals and scanner alerts. Backed by `tui/panels/alerts.py`. Implemented as a `RichLog` (rich-text scrollback).

### What it shows

Three categories of messages, each with timestamp + color coding:

| Type | Color | Source |
|---|---|---|
| Signal | Green (BUY/LONG/BULLISH) or Red (SELL/SHORT/BEARISH) | Signal scanners (clucmay02, double_top, etc.) and the AI token analyzer, arriving via NATS topics `signals.{strategy}.{symbol}` and `ai.analysis.completed` |
| Alert | Yellow (`pump`) or Cyan (other) | Pump.fun scanner notifications |
| Risk warning | Red `⚠ RISK` | The risk-manager sub-agent or position-monitor cron |

### Format

```
14:23:01 BUY BTC [clucmay02]
  Mean reversion bounce — 1h RSI(14) at 28.4, price under EMA20…
14:23:08 SELL ETH [double_top]
  Confirmed double top at $3,524 with neckline break…
14:23:14 [pump] $PEPE2 — 142% in 8 minutes, MC $1.2M
14:24:02 ⚠ RISK Position concentration — 73% of equity in BTC alone
```

Each entry is one or two lines. Long messages are truncated to 120-150 chars to keep the panel readable.

### Where alerts come from

- **NATS signals.>** — the `SignalConsumer` subscribes to this wildcard at TUI startup. Any external signal scanner (vpn-stack, custom strategies, anything that publishes to `signals.*`) is picked up automatically. The consumer ingests the message into a ring buffer (so the agent can query history via `get_signals`) AND fires a callback that adds it to this panel.
- **NATS ai.analysis.completed** — the AI token analyzer publishes `result_id`-keyed events here. The consumer normalizes them into the same `Signal` shape as scanner alerts.
- **Direct calls** from sub-agents — the analyst, scanner, and risk-manager call `nats_publish("signals.{strategy}.{symbol}", payload)` from their tools when they identify a setup. The TUI sees their own publishes via the subscription too.

See [data-sources.md#signals](data-sources.md#signals) for the signal consumer in detail.

### When the panel is empty

Without any signal scanners running and without sub-agents publishing, the alerts panel stays empty. That's normal — it's a passive consumer of events from elsewhere. To test the wiring, add a manual signal from the agent:

```
Add a manual buy signal for BTC at the current price using the get_signals tool's manual injection helper.
```

(or just publish to NATS directly: `nats pub signals.test.BTC '{"signal_type":"BUY","price":71000}'`)

---

## NATS log panel

A scrolling log of NATS message bus traffic. Mostly for debugging and observability — shows every publish, subscribe, request, and reply the agent's NATS client touches.

### What it shows

```
14:23:01 [pub] agent.kai.status {"state":"thinking"}
14:23:02 [sub] signals.clucmay02.BTC ← (incoming)
14:23:08 [req] agent.analyst.request → "Analyze BTC 1h"
14:23:14 [rep] agent.analyst.request ← "BTC analysis: …"
```

Useful for confirming sub-agents are alive, watching signals come in, and diagnosing "why isn't my agent responding" issues. If this panel is empty, NATS isn't connected — see [troubleshooting.md#nats-not-connecting](troubleshooting.md#nats-not-connecting).

---

## Interactions between panels

### Chart updates from watchlist click

Watchlist row click → `_load_chart(symbol, current_timeframe)` → ChartPanel.set_data(...) → re-render.

### Positions refresh after trades

`/buy` / `/sell` → trader sub-agent → `place_order` tool → portfolio updated → `_refresh_positions()` → `get_positions` tool → PositionsPanel.update_positions(...) → re-render.

### Alerts feed sub-agent context

Sub-agents (especially the analyst and the scanner) start tasks by calling `get_signals(...)` to check the live signal feed. They see whatever the alerts panel sees, so a signal that just fired in the panel can immediately influence the next `/analyze` run.

### NATS log shadows everything

Every panel update that goes through NATS also shows up in the NATS log panel. This makes the NATS log a useful "did the message actually fire" debugger when something else looks wrong.

---

## Adding a new panel

The TUI's grid layout is defined in `tui/terminal_styles.tcss` (3 cols × 3 rows + a status bar + an input dock). To add a new panel:

1. Create `tui/panels/your_panel.py` — subclass `Widget`, `Static`, `RichLog`, or `DataTable`
2. Add it to `compose()` in `tui/terminal.py` between the other `yield` calls
3. Add a CSS rule for it in `tui/terminal_styles.tcss` with an `#id` selector
4. The grid auto-flows new children left-to-right, top-to-bottom — so you may need to adjust `grid-size` or use explicit `column-span`/`row-span` attributes if you want a specific position

See [architecture.md#textual-tui](architecture.md#textual-tui) for the full extension guide.

---

## What to read next

- [chart-panel.md](chart-panel.md) — the central chart with all its commands
- [commands.md#watch](commands.md#watch) — the `/watch` command reference
- [data-sources.md#signals](data-sources.md#signals) — signal consumer internals

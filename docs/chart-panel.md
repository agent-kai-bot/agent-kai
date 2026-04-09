# Chart panel

The chart panel renders ASCII candlestick charts using Unicode block characters. It supports two data sources, six color schemes, multiple timeframes, runtime visibility toggle, and persistence across TUI restarts. This doc covers all of it.

## Layout

The chart panel sits in row 2, column 2 of the grid layout (`tui/terminal_styles.tcss`). It's the largest panel by area:

```
┌────────────┬─────────────────────────────────┬─────────────┐
│ Watchlist  │ Chart panel                     │ Alerts      │
├────────────┼─────────────────────────────────┼─────────────┤
│ Positions  │ Chat panel                      │ NATS log    │
└────────────┴─────────────────────────────────┴─────────────┘
                       Status bar
                       Input box
```

## Default state

On first launch:

- Symbol: `BTC`
- Timeframe: `1m`
- Source: `kai-api` (cloud agent-k.ai market data)
- Color scheme: `classic` (TradingView teal/red)
- Visible: yes

State is loaded from `workspaces/terminal/state.json` if it exists, otherwise these defaults are used and a fresh state.json is created on the first save.

## Symbols and timeframes

### Supported symbols

The cloud `kai-api` accepts any standard crypto ticker. The `coinbase` source accepts any product ID Coinbase carries (with or without the `-USD` / `-USDC` suffix). Common symbols are documented in [data-sources.md](data-sources.md).

### Supported timeframes

`1m`, `5m`, `15m`, `1h`, `4h`, `6h`, `1d`

The cycle order for `Ctrl+T` is: `1m → 5m → 15m → 1h → 4h → 6h → 1d → 1m → …`

## Commands

All chart manipulation goes through the `/chart` slash command. Forms:

```
/chart                          # Reload current symbol + timeframe
/chart SYMBOL                   # Switch symbol, keep timeframe
/chart SYMBOL TIMEFRAME         # Switch both
/chart symbol SYMBOL            # Change just the symbol (alias)
/chart source kai-api|coinbase  # Switch data source
/chart color SCHEME             # Change color scheme
/chart color                    # Show current scheme + available
/chart on                       # Show the chart panel
/chart off                      # Hide the chart panel
```

Examples:

```
/chart                          # Reload BTC 1m
/chart ETH                      # Switch to ETH at current timeframe
/chart SOL 4h                   # SOL on the 4h
/chart symbol DOGE              # Change just the symbol
/chart source coinbase          # Switch data source to Coinbase
/chart source kai-api           # Switch back to cloud
/chart color neon               # Bright ANSI green/red
/chart color                    # See "Current: classic | Available: classic, neon, ansi, mono, ocean, ember"
/chart off                      # Hide the chart, keep the slot reserved
/chart on                       # Show it again
```

### Keyboard shortcuts

- `Ctrl+T` cycles to the next timeframe
- `Ctrl+S` cycles to the next tracked symbol (`BTC`, `ETH`, `SOL` by default)

Tracked symbols come from the `KAI_TRACKED_SYMBOLS` env var (comma-separated). Default: `BTC,ETH,SOL`.

## Data sources

### kai-api (default)

The cloud `agent-k.ai` market data endpoint. REST + WebSocket. Bearer-authenticated via `AGENT_KAI_API_KEY`. Carries hundreds of pairs from BingX. Streaming WebSocket updates push new bars in real time so the chart auto-refreshes without polling.

```
/chart source kai-api
```

### coinbase

Direct Coinbase Advanced Trade public API. No authentication required. Useful when:

- The cloud doesn't carry the symbol you want
- You want a sanity check that BTC looks the same on a major US venue
- You're hitting cloud rate limits and want to offload

```
/chart source coinbase
/chart symbol BTC-USD          # Coinbase product IDs accepted
/chart symbol BTC              # Bare symbols are auto-suffixed to -USD
```

The coinbase source also has a streaming WebSocket implementation for real-time bar updates.

See [data-sources.md](data-sources.md) for the full client APIs and the underlying REST/WebSocket protocols.

## Color schemes

Six built-in schemes covering most terminal aesthetics. Switch with `/chart color SCHEME`. Persisted to `workspaces/terminal/state.json`.

### classic (default)

```
bar_up    rgb(38,166,154)   ← TradingView teal-green
bar_down  rgb(239,83,80)    ← TradingView soft-red
wick      grey62
doji      grey78
```

The exact RGB triplets TradingView uses for its default candle palette. Readable on dark terminals, not eye-stabbing, doesn't look like an ANSI demo from 1992. **The right default for traders.**

### neon

```
bar_up    bold bright_green
bar_down  bold bright_red
wick      grey70
doji      yellow
```

Loud bright-ANSI green/red. High-contrast vintage terminal look. Use this if `classic` looks washed out on your terminal's color profile.

### ansi

```
bar_up    green
bar_down  red
wick      dim
doji      dim yellow
```

Plain ANSI 8-color green/red. Works on the cheapest terminal — falls back gracefully when SSH'd in over a dumb tty that doesn't speak truecolor.

### mono

```
bar_up    bold white
bar_down  dim white
wick      grey50
doji      grey62
```

White-on-grey. No color at all. Useful when you have screen-share / streaming considerations or just prefer minimal aesthetics.

### ocean

```
bar_up    bold cyan
bar_down  bold magenta
wick      blue
doji      bright_blue
```

Cyan / magenta. Easier on the eyes than red/green for some users (and for protanopia / deuteranopia color-blindness, where red and green are hard to distinguish).

### ember

```
bar_up    bold bright_yellow
bar_down  bold bright_red
wick      dark_orange
doji      yellow
```

Yellow / red / orange. Warm "ember" palette. Personal taste.

### Showing the current scheme

```
/chart color
```

Prints `Current: classic | Available: classic, neon, ansi, mono, ocean, ember`.

### Adding a new scheme

Edit `tui/panels/chart.py`. Define a new `ChartColorScheme` instance and add it to the `SCHEMES` dict. The scheme is picked up automatically — no other changes needed.

```python
SCHEMES["sunset"] = ChartColorScheme(
    name="sunset",
    bar_up="bold rgb(255,140,0)",
    bar_down="bold rgb(160,32,240)",
    wick="rgb(80,40,60)",
    doji="rgb(200,200,160)",
    header_up="bold rgb(255,140,0)",
    header_down="bold rgb(160,32,240)",
    header_symbol="bold white",
    header_dim="grey50",
    axis="grey42",
)
```

Then `/chart color sunset` switches to it.

## Visibility toggle

`/chart off` hides the chart panel without scrambling the layout.

### Why `visible` and not `display`

Textual has two ways to hide a widget:

- `widget.display = False` — CSS `display: none`. Removes the widget from layout. Other grid cells reflow to fill the gap. **This is the wrong choice for a fixed grid like ours** because the chat panel below would auto-flow leftward and the entire 3×3 layout scrambles.
- `widget.visible = False` — CSS `visibility: hidden`. Keeps the slot reserved (empty box) but doesn't render content. Layout stays intact.

The chart panel uses `widget.visible` for `/chart on` / `/chart off`. Bug fixed in commit `70a62ed` after the original implementation used `display` and the user reported the chat moving left when they hid the chart.

## Persistence

Chart state is saved to `workspaces/terminal/state.json` on every change. Format:

```json
{
  "chart_symbol": "BTC",
  "chart_timeframe": "1h",
  "chart_color_scheme": "classic",
  "chart_source": "kai-api"
}
```

Loaded at TUI start via `_load_terminal_state()`. Falls back to defaults on missing file or corrupt JSON.

## Click-to-navigate from the watchlist

Clicking a row in the watchlist panel loads that symbol on the chart at the current timeframe. The watchlist panel raises a `RowSelected` event which the TUI's `on_data_table_row_selected` handler routes to `_load_chart(symbol, tf)`. See [watchlist-and-positions.md](watchlist-and-positions.md#interactions).

## How the rendering works

The chart panel reads `self.bars` (a list of `{ts, open, high, low, close, volume}` dicts) and renders each bar as a stack of Unicode block characters: `▌`, `█`, `▐`, `▆`, `▂`, etc. The wick is a single `│` character. Bullish bars use `bar_up`, bearish bars use `bar_down`, doji bars (open == close within tolerance) use `doji`. The vertical axis is computed from the min/max of the visible bars with a few percent padding.

Most of the rendering complexity is in `tui/panels/chart.py:render_line` — it streams the rendered chart line by line via Textual's `Strip` API rather than building a full string in memory.

## Performance

The chart panel re-renders on every `set_data` or `append_bar` call. With 200 bars (the cap) the render takes <5ms in `classic` mode on a typical Linux terminal. Live WebSocket updates from `kai-api` arrive at 1-bar/minute on the 1m timeframe, so the cost is negligible.

## What to read next

- [data-sources.md](data-sources.md) — kai-api and coinbase clients in detail, WebSocket protocols
- [watchlist-and-positions.md](watchlist-and-positions.md) — the side panels and how they interact with the chart
- [commands.md#chart](commands.md#chart) — quick command reference

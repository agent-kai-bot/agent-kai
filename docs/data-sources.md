# Data sources

The four places agent KAI gets data from: the cloud `agent-k.ai` market data API, the Coinbase Advanced Trade public API, the live signal feed over NATS, and the backtesting engine. This doc covers all of them with the actual tool signatures, the underlying protocols, and worked examples.

## TL;DR

| Source | What | Tool |
|---|---|---|
| **kai-api** (cloud) | OHLCV, prices, indicators — bearer auth | `query_ohlcv`, `get_latest_price`, `calculate_indicator` |
| **Coinbase** | OHLCV, prices, products — no auth | `get_coinbase_candles`, `get_coinbase_price`, `list_coinbase_products` |
| **Signal feed** (NATS) | Live alerts from scanners + AI token analyzer | `get_signals` |
| **Backtest engine** | Run a declarative strategy over historical data | `run_backtest` |

All four are exposed as LangChain tools that any agent (main or sub) can call.

---

<a id="kai-api"></a>
## kai-api — the cloud agent-k.ai market data endpoint

The default data source. REST + WebSocket. Bearer-authenticated via `AGENT_KAI_API_KEY`.

### Base URLs

- REST: `https://agent-k.ai/v1` (overridable via `AGENT_KAI_BASE_URL`)
- WebSocket: `wss://agent-k.ai/v1/ws` (overridable via `AGENT_KAI_WS_URL`)

### Authentication

All requests require an `Authorization: Bearer <key>` header. The key is loaded by `config.py` at startup from (in priority order):

1. `AGENT_KAI_API_KEY` env var
2. `.env` file in project root
3. `AGENT-KAI-API-KEY.txt` file in project root

If no key is found at startup, REST calls fail with 401 and WebSocket connections refuse the upgrade. The chart panel will show "Chart load error" — see [troubleshooting.md](troubleshooting.md#chart-load-error).

### Endpoints used

| Endpoint | Used for |
|---|---|
| `GET /v1/market/ohlcv?symbol=BTC&interval=1h&limit=200` | Historical candles for `query_ohlcv`, the chart panel, and the backtest engine |
| `GET /v1/market/price?symbol=BTC` | Latest price (sometimes derived from a 1-bar OHLCV fetch instead) |
| `WS /v1/ws` | Real-time bar updates for the chart panel |

The cloud feed pulls from BingX upstream and exposes hundreds of pairs. There's no `/symbols` enumeration endpoint — just try the symbol you want via `query_ohlcv`. The `list_symbols` tool returns a hint list of common large-caps but isn't authoritative.

### Bar shape

Each bar comes back from `/v1/market/ohlcv` as a positional array:

```json
[
  [1712669400000, 71245.40, 71547.50, 71241.10, 71498.60, 252.2225],
  [1712670000000, 71498.60, 71506.10, 71307.40, 71343.50, 133.8504],
  ...
]
```

Format: `[ts_ms, open, high, low, close, volume]`. The agent's `agent.data_sources.kai_api.fetch_candles` normalizes these into the dict shape every tool expects:

```python
[
  {"ts": "2026-04-09T10:00:00Z", "open": 71498.60, "high": 71506.10, "low": 71307.40, "close": 71343.50, "volume": 133.8504},
  ...
]
```

### WebSocket protocol

The chart panel uses `KaiApiCandleStream` which:

1. Connects to `wss://agent-k.ai/v1/ws` with the bearer token in the upgrade headers
2. Subscribes to a channel like `market.BTC.1m` via `{"op": "subscribe", "channel": "market.BTC.1m"}`
3. Receives an initial `snapshot` event with the recent N bars
4. Receives ongoing `event` updates as new bars close
5. Sends periodic `ping` keepalives, expects `pong` replies
6. Reconnects on disconnect with exponential backoff

When you `/chart BTC 1m`, the TUI subscribes to `market.BTC.1m`. Switching symbol or timeframe unsubscribes the old channel and subscribes the new one. This avoids polling and keeps the chart at real-time accuracy.

### Tool reference

```python
query_ohlcv(symbol: str, interval: str = "1m", limit: int = 100, start: str = "", end: str = "") -> str
```

Returns a formatted text table of recent bars. The LLM uses this to "see" the data shape rather than reasoning over raw arrays.

```python
get_latest_price(symbol: str) -> str
```

Returns `"BTC: $70,785.40 (as of 2026-04-09T10:42:00)"`. The cloud doesn't expose a separate `/price` endpoint, so this is derived from a 1-bar 1m OHLCV fetch.

```python
calculate_indicator(symbol: str, indicator: str = "RSI", period: int = 14, interval: str = "1m", limit: int = 200) -> str
```

Computes one of `RSI`, `SMA`, `EMA`, `MACD`, `BBANDS`, `ATR`, `VWAP` and returns a formatted summary with interpretation. Uses `pandas-ta` under the hood. MACD uses standard 12/26/9 settings.

Example output:

```
BTC RSI(14) on 1h:
  RSI(14) = 45.8 [neutral]
  Last 5: 53.3, 51.1, 53.4, 45.5, 45.8
```

---

## Coinbase — direct public API (no auth)

A second OHLCV/price source backed by Coinbase Advanced Trade's public REST API. No authentication required, no rate-limit juggling, independent of the cloud `kai-api`.

### Why it exists

Three reasons:

1. **Cross-venue validation.** "Does BTC look the same on Coinbase as it does on the cloud feed?" This matters when you're about to publish a signal — venue-specific liquidity quirks can produce false positives if you only look at one source.
2. **Pairs the cloud doesn't carry.** Coinbase has thousands of products. Sometimes you want a specific one that BingX doesn't have.
3. **Independence.** When the cloud is rate-limited or down, Coinbase keeps the agent productive.

### Base URL

`https://api.exchange.coinbase.com` (no auth)

### Endpoints used

| Endpoint | Used for |
|---|---|
| `GET /products/{product_id}/candles` | OHLCV bars |
| `GET /products/{product_id}/ticker` | Latest price + 24h volume + 24h change |
| `GET /products` | Discoverable list of all spot products |

### Product ID format

Coinbase uses `BASE-QUOTE` format like `BTC-USD`, `ETH-USDC`, `SOL-USD`. Our tools accept either:

- **Bare ticker** — `BTC` is automatically suffixed to `BTC-USD`
- **Qualified product ID** — `BTC-USD`, `ETH-USDC`, etc.

`agent.data_sources.coinbase.normalize_product_id` does the conversion.

### Tool reference

```python
get_coinbase_candles(symbol: str, interval: str = "1h", limit: int = 200) -> str
```

Returns a JSON object with:

- `summary` — human-readable summary (count, first/last bar, change %, high/low)
- `product_id` — the normalized product ID
- `interval` — the requested timeframe
- `count` — number of bars
- `bars` — full bar list (`[{ts, open, high, low, close, volume}, ...]`)

Supported intervals: `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `6h`, `1d`. Max ~350 bars per call (Coinbase API limit).

```python
get_coinbase_price(symbol: str) -> str
```

Returns latest price + 24h volume + 24h percent change as JSON.

```python
list_coinbase_products(quote: str = "USD", limit: int = 50) -> str
```

Returns discoverable list of spot products filtered by quote currency. Useful before querying specific candles.

### Coinbase WebSocket

The chart panel can also stream Coinbase candles in real time. Implemented in `agent/data_sources/coinbase.py:CoinbaseCandleStream`. Switch the chart source with `/chart source coinbase`.

---

<a id="signals"></a>
## Signal feed (NATS) — live alerts from scanners

A bounded ring buffer of trading signals received via NATS, queryable from any agent via the `get_signals` tool.

### What feeds it

The signal consumer (`agent/signal_consumer.py:SignalConsumer`) subscribes to two NATS subject patterns:

| Subject | Source |
|---|---|
| `signals.>` | Wildcard for all signal scanners. Subjects look like `signals.{strategy}.{symbol}` (e.g. `signals.clucmay02.BTC`, `signals.double_top.ETH`) |
| `ai.analysis.completed` | The AI token analyzer publishes here when an analysis completes |

The vpn-stack signal scanners ship with names like `clucmay02`, `double_top`, `ewo` and publish to `signals.{name}.{symbol}` whenever they see a setup. Other scanners just need to publish to the same subject pattern to be picked up.

### Signal shape

Each signal is normalized into a `Signal` dataclass:

```python
@dataclass
class Signal:
    source: str          # "signal-scanner", "ai-token-analyzer", "manual"
    strategy: str        # "clucmay02", "double_top", "ai_daily", ...
    symbol: str          # "BTC", "ETH", "SOL"
    signal_type: str     # "BUY", "SELL", "ANALYSIS", ...
    price: float
    timestamp: str       # ISO 8601
    received_at: float   # local epoch when we received it
    details: dict        # extra payload fields, scanner-specific
```

The `details` dict carries scanner-specific extras (entry zone, stop, target, confidence, etc.) which get flattened into the top-level dict on `to_dict()` for cleaner tool output.

### Ring buffer

The consumer keeps the last 200 signals in a `collections.deque(maxlen=200)`. Older signals are evicted on overflow. Tunable via `SignalConsumer(max_signals=N)` in `main.py`.

### Tool reference

```python
get_signals(symbol: str = "", strategy: str = "", signal_type: str = "", limit: int = 10) -> str
```

Returns recent signals from the ring buffer matching the optional filters. All filters are AND-combined. Results are ordered newest-first.

Example agent usage:

```python
# Check for any recent BTC signals before doing TA
get_signals(symbol="BTC", limit=5)

# Find every clucmay02 BUY signal in the buffer
get_signals(strategy="clucmay02", signal_type="BUY", limit=20)
```

The analyst sub-agent's SOUL explicitly calls this at the start of every analysis: "check the live signal feed first to see if any scanner already flagged the symbol."

### Live display

The TUI's alerts panel registers itself as a callback on the signal consumer (`consumer.on_signal = lambda sig: alerts_panel.add_signal(...)`). Every signal that lands in the buffer also lands in the alerts panel in real time. See [watchlist-and-positions.md](watchlist-and-positions.md#alerts-panel).

### Manual injection

For testing and scripting:

```python
consumer.add_manual(strategy="test", symbol="BTC", signal_type="BUY", price=71000)
```

Or publish directly to NATS from the shell:

```bash
nats pub signals.test.BTC '{"signal_type":"BUY","price":71000,"strategy":"test"}'
```

The consumer picks it up automatically.

---

## Backtesting — `run_backtest` tool

The agent can validate trading hypotheses by running them over historical OHLCV before recommending them. Backed by the `backtesting.py` library with a declarative spec layer that lets the LLM build strategies via JSON instead of generating Python code.

### Why declarative

Two reasons:

1. **Safety.** LLM-generated code in a backtest hot loop is a recipe for `eval(...)` shenanigans. The declarative path generates a `Strategy` subclass internally from a vetted spec — no LLM strings touch the runtime.
2. **Speed.** Indicators are pre-computed as DataFrame columns BEFORE the strategy runs, so `next()` is just column comparisons. Fast even on long histories.

For complex strategies that don't fit the declarative model, the agent should write Python code and route it through `docker_sandbox` (see [agents.md](agents.md)).

### The declarative spec

```python
{
  "symbol": "BTC",
  "interval": "1h",
  "bars": 500,
  "cash": 100000,
  "commission_pct": 0.1,
  "buy_when": [
    {"indicator": "RSI_14", "op": "<", "value": 30},
    {"indicator": "close", "op": ">", "ref": "EMA_50"}
  ],
  "sell_when": [
    {"indicator": "RSI_14", "op": ">", "value": 70}
  ],
  "stop_loss_pct": null,
  "take_profit_pct": null,
  "source": "kai-api"
}
```

Field reference:

- `symbol` — trading symbol
- `interval` — candle interval (`1m`, `5m`, `15m`, `1h`, `4h`, `6h`, `1d`)
- `bars` — number of historical bars to test over
- `cash` — starting capital
- `commission_pct` — commission per trade as a percent (`0.1` = 0.1%)
- `buy_when` — array of entry conditions, ALL must be true to enter
- `sell_when` — array of exit conditions, ANY triggers an exit
- `stop_loss_pct` / `take_profit_pct` — optional fixed percent stops
- `source` — `"kai-api"` (default) or `"coinbase"`

### Condition format

```json
{
  "indicator": "RSI_14",      // any supported indicator name
  "op": "<",                   // <, >, <=, >=, ==, crosses_above, crosses_below
  "value": 30                  // numeric threshold
}
```

OR comparing two indicators:

```json
{
  "indicator": "close",
  "op": ">",
  "ref": "EMA_50"              // another indicator name
}
```

### Supported indicators

Pre-computed and added as DataFrame columns BEFORE the strategy runs:

| Pattern | Example | What |
|---|---|---|
| `RSI_{period}` | `RSI_14` | Relative strength index |
| `EMA_{period}` | `EMA_20`, `EMA_50`, `EMA_200` | Exponential moving average |
| `SMA_{period}` | `SMA_50` | Simple moving average |
| `ATR_{period}` | `ATR_14` | Average true range |
| `BBANDS_upper_{period}` | `BBANDS_upper_20` | Bollinger upper band (also adds middle and lower as side effect) |
| `BBANDS_middle_{period}` | `BBANDS_middle_20` | Bollinger middle |
| `BBANDS_lower_{period}` | `BBANDS_lower_20` | Bollinger lower |
| `MACD` | `MACD` | MACD line (also adds `MACD_signal` and `MACD_hist`) |
| `MACD_signal` | `MACD_signal` | MACD signal line |
| `MACD_hist` | `MACD_hist` | MACD histogram |
| `VWAP` | `VWAP` | Volume-weighted average price |
| `close` / `open` / `high` / `low` / `volume` | (raw OHLCV columns) | Direct price/volume access |

### Tool signature

```python
run_backtest(
    symbol: str = "BTC",
    interval: str = "1h",
    bars: int = 500,
    buy_when: str = "",         # JSON string of conditions
    sell_when: str = "",        # JSON string of conditions
    stop_loss_pct: float = 0,
    take_profit_pct: float = 0,
    source: str = "kai-api",
) -> str
```

Returns a JSON string with the results.

### Sample call

```python
run_backtest(
    symbol="BTC",
    interval="1h",
    bars=500,
    buy_when='[{"indicator":"RSI_14","op":"<","value":30},{"indicator":"close","op":">","ref":"EMA_50"}]',
    sell_when='[{"indicator":"RSI_14","op":">","value":70}]',
    stop_loss_pct=2.0,
    take_profit_pct=5.0
)
```

### Sample result

```json
{
  "success": true,
  "symbol": "BTC",
  "interval": "1h",
  "bars_used": 487,
  "period": "2026-03-20T08:00:00 to 2026-04-09T15:00:00",
  "total_return_pct": 8.42,
  "sharpe_ratio": 0.61,
  "max_drawdown_pct": -8.4,
  "num_trades": 24,
  "win_rate_pct": 58.3,
  "avg_trade_pct": 0.35,
  "best_trade_pct": 4.2,
  "worst_trade_pct": -2.1,
  "exposure_pct": 41.2,
  "buy_and_hold_return_pct": 6.1,
  "interpretation": "Promising edge. Consider saving this as a validated skill."
}
```

The `interpretation` field is a heuristic the tool produces to nudge the agent: "Promising edge" if `win_rate >= 55%` AND `sharpe > 0.5`, "Too few trades to draw conclusions" if `num_trades < 5`, "No trades were triggered" if 0, otherwise "Weak or negative edge."

The analyst's `how-to-write-a-ta-skill` template explicitly says: only call a strategy "tradeable" if `win_rate > 55%` AND `sharpe > 0.5` AND it survives an out-of-sample window. The skills `btc-mtf-analysis-with-validation` and `ema-workflow-search-with-oos-validation` (both in `workspaces/kai/skills/`) demonstrate this validation pattern.

### Sample working backtest prompt

```
Backtest a simple RSI(14) < 30 oversold bounce strategy on BTC 1h: enter when RSI drops below 30 AND close is above EMA(50), exit when RSI crosses back above 70. Use 500 bars. Add a 2% stop loss and 5% take profit. Tell me the win rate, Sharpe, drawdown, and whether this has any edge.
```

The agent will:

1. Parse the description into a buy_when / sell_when spec
2. Call `run_backtest` with the spec
3. Read the result
4. Synthesize a written interpretation (not just echoing the JSON)

Try it.

### When the backtest is empty

If the spec produces 0 trades, `interpretation` says so explicitly. Common causes:

- Conditions too strict (e.g. `RSI_14 < 5` — almost never happens)
- Indicator warm-up consumed too many bars (you asked for 100 bars but RSI(14) needs 14 to start, so the first 14 are dropped)
- Comparing indicators that don't exist (typo in `ref` field)

The agent should re-read the spec, loosen the conditions, and try again.

---

## Adding a new data source

Create a new module under `agent/data_sources/` mirroring `kai_api.py` or `coinbase.py`:

```python
# agent/data_sources/my_source.py

import requests

BASE_URL = "https://my-source.example.com"

def fetch_candles(symbol: str, interval: str = "1h", limit: int = 200) -> list[dict]:
    resp = requests.get(f"{BASE_URL}/candles", params={...}, timeout=10)
    resp.raise_for_status()
    raw = resp.json()
    return [
        {"ts": bar["timestamp"], "open": bar["o"], "high": bar["h"], "low": bar["l"], "close": bar["c"], "volume": bar["v"]}
        for bar in raw
    ]
```

Then expose it as a tool in `agent/crypto_tools.py`:

```python
def _get_my_source_candles(symbol: str, interval: str = "1h", limit: int = 200) -> str:
    from agent.data_sources.my_source import fetch_candles
    bars = fetch_candles(symbol, interval, limit)
    return json.dumps({"count": len(bars), "bars": bars})

get_my_source_candles = StructuredTool.from_function(
    func=_get_my_source_candles,
    name="get_my_source_candles",
    description="Fetch OHLCV from my custom source. Inputs: symbol, interval, limit.",
)

ALL_CRYPTO_TOOLS.append(get_my_source_candles)
```

The tool is auto-registered to every agent the next time `_get_crypto_tools()` is called (which happens at agent construction).

To add it as a data source for the chart panel and the backtest engine, add a new branch to `_fetch_ohlcv` in `agent/backtest_tool.py` and to `_load_chart` in `tui/terminal.py`.

See [architecture.md#tools](architecture.md#tools-system) for the full extension guide.

---

## What to read next

- [chart-panel.md](chart-panel.md) — how the chart consumes WebSocket updates
- [agents.md](agents.md) — sub-agent prompts that reference these data tools
- [learning-and-skills.md](learning-and-skills.md) — the validation pattern that uses `run_backtest`

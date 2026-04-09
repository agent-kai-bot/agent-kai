# Slash commands

Every slash command available in the trading terminal, with syntax, examples, and links to deeper docs where applicable. Anything in the chat input that does NOT start with `/` is sent to the main kai agent as a chat prompt.

## Quick reference

| Command | What it does |
|---|---|
| [`/buy`](#buy) | Place a buy order on the paper trading engine |
| [`/sell`](#sell) | Place a sell order |
| [`/analyze`](#analyze) | Run technical analysis via the analyst sub-agent |
| [`/scan`](#scan) | Scan pump.fun for new / trending / graduated tokens |
| [`/risk`](#risk) | Portfolio exposure review via the risk-manager |
| [`/chart`](#chart) | Change chart symbol, timeframe, source, color, visibility |
| [`/watch`](#watch) | Add a symbol to the watchlist |
| [`/positions`](#positions) | Refresh positions panel (alias `/pos`) |
| [`/learn`](#learn) | Mentor reflection on the most recent sub-agent session |
| [`/login`](#login) | Authenticate against your ChatGPT subscription via OAuth |
| [`/model`](#model) | Switch any agent's LLM endpoint at runtime |
| [`/think`](#think) | Set the reasoning effort (thinking level) for any agent |
| [`/queue`](#queue) | Inspect or modify the type-ahead input queue |

Anything not starting with `/` goes to the main kai agent as a chat prompt.

---

## Trading commands

### /buy

Place a paper-trading buy order.

```
/buy SYMBOL QTY [limit PRICE]
```

Examples:

```
/buy BTC 0.1
/buy ETH 2.5
/buy SOL 50 limit 145.50
```

The command spawns the `trader` sub-agent which calls `place_order`. The trader is a long-lived sub-agent — repeated `/buy` commands reuse the same instance. The order goes through the paper trading portfolio (no real funds).

### /sell

Same as `/buy` but the other direction.

```
/sell SYMBOL QTY [limit PRICE]
```

Examples:

```
/sell BTC 0.05
/sell SOL 25 limit 160.00
```

### /risk

Run a portfolio exposure review.

```
/risk
```

Spawns the `risk-manager` sub-agent. It calls `get_positions`, computes total exposure, position concentration, drawdown vs initial capital, and flags anything outside conventional risk thresholds. No arguments — the risk-manager reviews whatever is currently open.

---

## Analysis commands

### /analyze

Run a technical analysis via the analyst sub-agent.

```
/analyze SYMBOL [TIMEFRAME]
```

Default timeframe is `1m`. Supported timeframes: `1m`, `5m`, `15m`, `1h`, `4h`, `6h`, `1d`.

Examples:

```
/analyze BTC
/analyze SOL 15m
/analyze ETH 1h
/analyze DOGE 4h
```

The analyst calls `query_ohlcv`, `calculate_indicator` for RSI / MACD / Bollinger Bands / ATR, and produces a structured report with: current price, indicator values + interpretation, key support/resistance, multi-timeframe context, trading bias, entry/stop/target zones, and a confidence level. See [agents.md#analyst](agents.md#analyst) for the analyst's full prompt and skill library.

### /scan

Scan pump.fun for tokens.

```
/scan [trending|new|graduated]
```

Default filter is `trending`. Examples:

```
/scan
/scan trending
/scan new
/scan graduated
```

Spawns the `scanner` sub-agent. It hits the pump.fun API and returns a table of tokens with name, symbol, and market cap. The `scanner` runs on the cheaper `kai-fast` endpoint by default (large volume, fast turnaround).

---

## Chart commands

### /chart

The chart command has multiple forms.

```
/chart                          # Reload current symbol + timeframe
/chart SYMBOL [TIMEFRAME]       # Switch symbol + timeframe
/chart symbol SYMBOL            # Change just the symbol
/chart source kai-api|coinbase  # Switch data source
/chart color SCHEME             # Change color scheme
/chart color                    # Show current scheme + available
/chart on                       # Show the chart panel
/chart off                      # Hide the chart panel
```

Examples:

```
/chart BTC 1h
/chart ETH 4h
/chart symbol SOL
/chart source coinbase
/chart color neon
/chart color classic
/chart off
```

Color schemes: `classic` (TradingView teal/red, the default), `neon` (bright ANSI), `ansi` (plain 8-color), `mono` (white-on-grey), `ocean` (cyan/magenta), `ember` (orange/blue). See [chart-panel.md](chart-panel.md) for screenshots and the full source/timeframe matrix.

Chart state (symbol, timeframe, source, color scheme, visibility) persists across TUI restarts in `workspaces/terminal/state.json`.

### /watch

Add a symbol to the watchlist panel.

```
/watch SYMBOL
```

Example:

```
/watch DOGE
/watch SHIB
```

The watchlist panel polls the cloud price endpoint and shows real-time prices for everything you've added. To remove, see [watchlist-and-positions.md](watchlist-and-positions.md).

### /positions

Refresh the positions panel.

```
/positions
/pos          # alias
```

The positions panel auto-refreshes after every trade. Use this to force a refresh.

---

## Self-learning commands

### /learn

Run the mentor reflection on the most recent sub-agent session.

```
/learn               # Reflect on the most recent sub-agent
/learn AGENT_NAME    # Reflect on a specific sub-agent
```

Examples:

```
/learn               # If you just ran /analyze, reflects on analyst
/learn analyst
/learn trader
```

The mentor sub-agent reads the target agent's last task, the tool calls it made, the chat context, and the existing skill library. It returns one of three decisions:

- **`create`** — a new skill is drafted and saved to `workspaces/{agent}/skills/{name}.md`
- **`patch`** — an existing skill is updated (string-replace) to fix a number, add a pitfall, or extend coverage
- **`no_skill`** — the session was routine and produced no novelty (an honest valid outcome)

The reflection is also saved to `eval_results/reflection-{ts}-{agent}.json` for later review.

See [learning-and-skills.md](learning-and-skills.md) for the full reflection loop with a real working example end to end.

> **Note:** Currently `/learn` only works for sub-agents, not the main kai agent. Adding `/learn kai` is on the roadmap — see `docs/proposals/learn-on-main-kai-agent.md` (gitignored).

---

## Endpoint and model commands

### /login

Authenticate against your ChatGPT subscription via OAuth so the agent can use the Codex Responses API.

```
/login codex
```

Opens a browser tab to `https://auth.openai.com/oauth/authorize`, spins up a temporary localhost callback server on port 1455, and writes the resulting tokens to `~/.codex/auth.json`. After this completes, any agent configured for the `codex-cli` endpoint can hit the `https://chatgpt.com/backend-api/codex/responses` endpoint with your subscription quota — no API key needed.

Tokens auto-refresh when they're within 5 minutes of expiry. The auth file is shared with the official `codex` CLI, so if you've already run `codex login` once you can skip `/login codex` entirely.

### /model

Switch any agent's LLM endpoint at runtime, without restarting the TUI.

```
/model                            # Show every agent's current model
/model AGENT                      # Show that agent's current model + available choices
/model AGENT ENDPOINT/MODEL       # Switch the agent to a new (endpoint, model) pair
```

Examples:

```
/model
/model kai
/model kai codex-cli/gpt-5.4
/model analyst kai-smart
/model trader kai-local/qwen35-gptq
/model mentor codex-cli/gpt-5.3
```

For the main kai agent, `reload_llm()` rebuilds the executor + fallback chain in place — chat history, memory, skills, and the prompt are all preserved. For sub-agents, `/model` does a stop+spawn, which is fast but the sub-agent's chat history (which is per-task and short-lived anyway) is reset.

Available endpoints and models are defined in `agent-config.json`. See [models-and-thinking.md](models-and-thinking.md) for the endpoint registry and how to add new ones.

The override is **in-memory only** — restart the TUI to revert to the on-disk default. To make a swap permanent, edit `agent-config.json`.

### /think

Set the reasoning effort for any agent. Only affects endpoints that support a `reasoning_effort` parameter (Codex Responses API for GPT-5.x; OpenAI direct for GPT-5.x).

```
/think                            # Show kai's current thinking level
/think LEVEL                      # Set kai's thinking level
/think AGENT                      # Show AGENT's current level
/think AGENT LEVEL                # Set AGENT's thinking level
```

Valid levels (case-insensitive, with aliases):

| Canonical | Aliases | Effect |
|---|---|---|
| `none` | `off` | No reasoning |
| `minimal` | `min` | Lowest reasoning |
| `low` | | Light reasoning |
| `medium` | (default) | Standard reasoning |
| `high` | | More reasoning, slower, better answers on hard problems |
| `xhigh` | `x-high`, `extreme`, `max`, `extra` | Maximum reasoning, slowest, best answers |

Examples:

```
/think                            # Show kai's level
/think high
/think analyst xhigh
/think trader medium
/think mentor x-high              # alias resolves to xhigh
```

Higher levels burn more hidden chain-of-thought tokens (which are billed) so use sparingly. If you set `/think` on an agent whose current endpoint doesn't honor reasoning_effort (e.g. `kai-smart` which passes through to vLLM/qwen3), you'll get a yellow warning that the override is stored but won't take effect until you `/model` swap to a thinking-capable endpoint.

See [models-and-thinking.md](models-and-thinking.md) for the full reasoning levels reference.

---

## Queue commands

### /queue

Inspect or modify the type-ahead input queue. The queue catches anything you type while an agent is busy, FIFO. Capped at 10 items.

```
/queue                            # Show pending items with positions
/queue clear                      # Drop everything in the queue (aliases: flush, wipe)
/queue drop N                     # Drop the item at position N (1-indexed)
```

Examples:

```
/queue                            # Lists "7 items queued: #1: ..., #2: ..."
/queue clear
/queue drop 3                     # Removes the 3rd queued item
```

Each queued item also gets a clickable `[X]` button in the chat panel — clicking it drops just that item without affecting the others. After any drop, the remaining items renumber (so removing #2 makes #3 become #2).

See [chat-input.md#queue](chat-input.md#type-ahead-queue) for the full queue mechanics including how it interacts with sub-agent dispatches and synchronous slash commands.

---

## Plain chat (no slash)

Anything you type that doesn't start with `/` goes to the main kai agent. Kai has access to the full tool registry (file ops, shell, python, web fetch, all crypto tools, all sub-agent coordination tools, the Codex/Claude escalation tools, the Docker sandbox, memory + skills) so it can do quite a lot directly.

Examples:

```
What's the current BTC price and how does it compare to its 1d EMA50?

Pull recent ETH OHLCV on the 4h, calculate RSI/MACD/BBANDS, and tell me which is showing the cleanest setup right now.

I'm thinking about going long SOL. Use the analyst sub-agent for the 1h technical read, the risk-manager for sizing on a $10k account, then synthesize.

Backtest a simple RSI(14) < 30 oversold bounce strategy on BTC 1h over the last 500 bars. Tell me if it has any edge.

Save to memory that I prefer 1h analyses over 1m and that I keep my risk per trade at 1.5%.
```

The main agent uses memory and skills the same way sub-agents do, so over time it will get faster at the kinds of prompts you use most.

---

## What to read next

- [keybindings.md](keybindings.md) — keyboard shortcuts (which are NOT slash commands)
- [chat-input.md](chat-input.md) — input box features (history, paste, queue)
- [agents.md](agents.md) — which sub-agent to delegate to and why

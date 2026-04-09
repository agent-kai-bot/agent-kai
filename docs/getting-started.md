# Getting started

Install Agent KAI, get an API key, and have your first conversation with the trading terminal in under five minutes.

## What you need

- Python 3.13+
- Docker with Compose (for the local NATS message bus)
- A free account at [`https://agent-k.ai/`](https://agent-k.ai/) to get an API key
- Linux only: a system clipboard helper — `wl-clipboard` (Wayland) or `xclip` (X11)

The agent talks to the cloud `agent-k.ai` API for both market data and LLM inference, so you don't need to run vLLM, llama.cpp, or any other model server locally. Local model fallbacks are supported but optional — see [models-and-thinking.md](models-and-thinking.md).

## Step 1 — Clone and create a venv

```bash
git clone git@github.com:agent-kai-bot/agent-kai.git
cd agent-kai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Step 2 — Start NATS

The sub-agent message bus runs in a single container.

```bash
docker compose up -d
```

Verify it's up:

```bash
docker compose ps   # nats container should be "running"
```

## Step 3 — Install the clipboard helper (Linux only)

The TUI's copy shortcuts (`Ctrl+Y`, `Ctrl+Shift+C`, mouse-drag-to-copy) need a CLI clipboard tool because most Linux terminals (gnome-terminal, Tilix, Konsole) silently drop OSC 52 escape sequences for security reasons. One command:

```bash
# Wayland (default on modern Ubuntu / Fedora / Pop!_OS)
sudo apt install wl-clipboard

# X11
sudo apt install xclip
```

If you skip this step, copy still appears to work but pasting in your editor will return whatever you previously copied from a browser. See [troubleshooting.md](troubleshooting.md#clipboard-says-copied-but-paste-shows-old-content) if you hit this.

## Step 4 — Get an API key

1. Visit [`https://agent-k.ai/`](https://agent-k.ai/)
2. Sign in (or create an account)
3. Navigate to the dashboard / account area
4. Generate an API key
5. Save it to a file in the project root or export it:

```bash
# Option A: environment variable (best for Docker / systemd / CI)
export AGENT_KAI_API_KEY="kai-..."

# Option B: .env file in the project root (auto-loaded via python-dotenv)
echo 'AGENT_KAI_API_KEY=kai-...' > .env

# Option C: token file at the project root (the path .gitignore covers)
echo 'kai-...' > AGENT-KAI-API-KEY.txt
```

All three are checked at startup in that priority order. See [configuration.md](configuration.md#secret-loading) for the full secret-loading rules.

## Step 5 — Launch the trading terminal

```bash
python main.py --terminal
```

You should see a Textual UI with a chart panel, a chat panel, watchlist, positions, and an alerts panel. The status bar shows "Status: idle".

If you get a "Could not connect to NATS" warning, the agent runs without the message bus — sub-agents won't work but plain chat with kai will. Bring NATS up with `docker compose up -d` and restart.

## Step 6 — Your first conversation

Click in the chat input at the bottom and type:

```
What's the current BTC price and how does it compare to the 1h EMA50?
```

Hit Enter. You'll see kai stream tokens as it calls `get_latest_price` and `calculate_indicator`, then synthesize a written answer.

## Step 7 — Your first sub-agent

Type:

```
/analyze BTC 1h
```

Hit Enter. The TUI will show:

```
> /analyze BTC 1h
Spawning analyst...
[analyst] BTC 1h Technical Analysis Report
... full report follows ...
```

The analyst sub-agent spun up in the background, ran 5-10 tool calls (price, RSI, MACD, Bollinger Bands, support/resistance), and returned a structured report. It stays alive — your next `/analyze` reuses the same instance, which is faster.

## Step 8 — Watch the agent learn

After the analyst finishes, look for a yellow tip in chat:

```
Tip: analyst used 8 tools. Run /learn to distill this session into a skill.
```

Type:

```
/learn
```

The mentor sub-agent will spawn, read the analyst's session, and either propose a new skill, patch an existing one, or honestly return "no_skill" if the session was routine. New skills land in `workspaces/analyst/skills/<name>.md` and get loaded the next time the analyst sees a similar setup.

See [learning-and-skills.md](learning-and-skills.md) for the full reflection loop, including a real working example with the exact mentor reply and the resulting skill file.

## Sample prompts that showcase what KAI can do

Try these in order, in a fresh session, to see the platform's range:

### 1. Plain chat — multi-tool orchestration

```
Compare BTC and ETH on the 1h timeframe. Pull the price, RSI, MACD, and ATR for each, and tell me which one has the cleaner technical setup right now. Be honest if neither does.
```

Kai will call ~10 tools across both symbols, weigh the indicators, and give you a direct answer.

### 2. Sub-agent delegation — analyst

```
/analyze SOL 15m
```

Spins up the analyst sub-agent (separate workspace, separate skill library, separate prompt) to do a focused technical analysis.

### 3. Backtesting — validate a hypothesis before trading it

```
Backtest a simple strategy on BTC 1h: buy when RSI(14) drops below 30 and the close is above EMA(50), sell when RSI crosses back above 70. Use 500 bars. Tell me if this has any edge.
```

Kai will use the `run_backtest` tool with the right declarative spec, run it over real OHLCV, and return win-rate / Sharpe / drawdown / num-trades. If the backtest is weak, it'll say so honestly.

### 4. Multi-agent — kai delegating to multiple specialists

```
I'm thinking about a long position on ETH. Get the analyst to do a 1h + 4h technical breakdown, get the risk-manager to size the position assuming I have a $10k account and want max 2% risk per trade, and synthesize their two reports into one recommendation.
```

Kai will `spawn_agent("analyst", ...)`, send it the TA task, then `spawn_agent("risk-manager", ...)` and combine the results.

### 5. Self-learning — the full loop

```
Run a full multi-timeframe analysis on BTC. Use the 1d for macro, 6h for swing structure, and 1h for tactical. Validate any candidate trade ideas with run_backtest before recommending them. The goal is to walk away with a reusable workflow, not just commentary.
```

Then `/learn`. The mentor will write up the workflow as a skill in `workspaces/analyst/skills/`. Run the same prompt the next day — the analyst will load the skill via `skill_view` first and follow it.

### 6. Switch to a stronger model for hard problems

If you have a ChatGPT subscription, log in:

```
/login codex
```

Then route the analyst to GPT-5.4 with maximum reasoning:

```
/model analyst codex-cli/gpt-5.4
/think analyst xhigh
```

Now repeat the multi-timeframe prompt above. The analyst will spend more reasoning tokens before answering and produce a noticeably more thorough report.

## What to read next

- [commands.md](commands.md) — full command reference
- [agents.md](agents.md) — every built-in sub-agent and what each one is best at
- [learning-and-skills.md](learning-and-skills.md) — the self-improvement loop in detail
- [troubleshooting.md](troubleshooting.md) — when things go wrong

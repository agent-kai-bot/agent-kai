# Agent KAI

Terminal-first crypto analysis, paper-trading, and self-improving multi-agent platform powered by [`agent-k.ai`](https://agent-k.ai).

![Agent KAI terminal screenshot](docs/agent-kai-tui.png)

## What it is

Agent KAI is a Textual TUI for talking to a network of LLM-driven crypto agents. The main agent (`kai`) coordinates a roster of specialist sub-agents — analyst, trader, risk-manager, scanner, onchain, and a full org chart of org-roles (CEO, CTO, architect, developer, QA, project-manager, UX, SEO, sales-marketing) — over a local NATS message bus. Each agent has its own persistent memory, its own skill library, and its own LLM endpoint with a fallback chain.

The cloud `agent-k.ai` API serves both market data (REST + WebSocket) and LLM inference (OpenAI-compatible) so the open-source agent works out of the box without you running your own model server. Local vLLM and the OpenAI Codex Responses API (your ChatGPT subscription) are first-class fallback endpoints.

## What makes it different

- **Real sub-agent runtime, not a chatbot router.** Every sub-agent is a long-lived LangChain executor with its own workspace, system prompt, memory, skills, and request/reply NATS subject. The main agent delegates and synthesizes — it doesn't impersonate.
- **Self-learning loop.** After any non-trivial sub-agent task, you run `/learn` and the mentor sub-agent reads the session's tool calls + chat + existing skills, then drafts a new skill or patches an existing one. Skills are markdown recipes that get loaded on demand the next time the same setup appears.
- **Multi-endpoint with thinking levels.** Swap any agent to any endpoint at runtime via `/model agent endpoint/model`. Crank reasoning effort with `/think agent xhigh` for hard problems on GPT-5.x via Codex. Fallback chains automatically roll over on errors.
- **Backtesting tool the agent can call.** Agents validate trading hypotheses with `run_backtest` over real OHLCV before recommending them. Win-rate, Sharpe, drawdown — all returned as structured data the LLM can reason about.
- **Type-ahead queue, multi-line paste, history, click-to-copy.** The TUI is designed for fast iteration: paste a stack trace, hit Enter, queue 10 things while the agent runs, drop any of them with an `[X]` click.

## Quick start

```bash
# 1. NATS for the sub-agent message bus
docker compose up -d

# 2. Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Linux clipboard helper (one of these — see docs/troubleshooting.md)
sudo apt install wl-clipboard   # Wayland
# or
sudo apt install xclip          # X11

# 4. Get an API key from https://agent-k.ai/ and export it
export AGENT_KAI_API_KEY="kai-..."

# 5. Launch the trading terminal
python main.py --terminal
```

Then type `/analyze BTC` and watch the analyst sub-agent spin up.

## Documentation

Everything is in [`docs/`](docs/) — start with the index.

| Doc | What it covers |
|---|---|
| [docs/README.md](docs/README.md) | Index of every doc with one-line descriptions |
| [docs/getting-started.md](docs/getting-started.md) | Install, API key, first run, first chat, sample prompts |
| [docs/commands.md](docs/commands.md) | Every slash command (`/analyze`, `/buy`, `/think`, `/learn`, `/queue`, …) |
| [docs/keybindings.md](docs/keybindings.md) | Every keyboard shortcut |
| [docs/chat-input.md](docs/chat-input.md) | History, multi-line paste, queue, copy/paste |
| [docs/chart-panel.md](docs/chart-panel.md) | Symbols, timeframes, sources, color schemes |
| [docs/watchlist-and-positions.md](docs/watchlist-and-positions.md) | Watchlist, positions, alerts panels |
| [docs/agents.md](docs/agents.md) | Sub-agent runtime, the 14 built-in agents, NATS, workspaces |
| [docs/models-and-thinking.md](docs/models-and-thinking.md) | `/model`, `/think`, endpoints, fallbacks, Codex OAuth |
| [docs/learning-and-skills.md](docs/learning-and-skills.md) | `/learn`, mentor reflection, memory, skills (with a working example) |
| [docs/configuration.md](docs/configuration.md) | `agent-config.json` schema, env vars, secrets |
| [docs/data-sources.md](docs/data-sources.md) | kai-api, Coinbase, signals, backtesting |
| [docs/architecture.md](docs/architecture.md) | Process model, NATS topics, storage layout, contributor guide |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common issues + fixes |

## Requirements

- Python 3.13+
- Docker with Compose (for NATS)
- An API key from [`https://agent-k.ai/`](https://agent-k.ai/)
- Linux only: `wl-clipboard` (Wayland) or `xclip` (X11) for the copy shortcuts

See [docs/getting-started.md](docs/getting-started.md) for the complete walkthrough.

## Run modes

```bash
python main.py --terminal              # Full trading terminal (recommended)
python main.py                         # Plain agent TUI without trading panels
python main.py --no-tui                # Headless — NATS only, for daemonized deployment
python main.py --log-level DEBUG       # Override log level
```

## Tests

```bash
.venv/bin/python test_learning_pipeline.py     # Self-learning pipeline (10 tests)
.venv/bin/python test_signal_pipeline.py       # Signal consumer + tools (15 tests)
.venv/bin/python -m unittest discover -s tests -v
```

See [docs/architecture.md](docs/architecture.md#testing) for the full test layout.

## Project status

Branch `kai/self-learning-platform` is the active development branch. Recent additions:

- Bash-style up/down history in the chat input with disk persistence
- Multi-line paste capture (paste a stack trace, send the whole thing as one prompt)
- Type-ahead queue with `[X]` click-to-drop and `/queue clear` / `/queue drop N`
- `/think agent level` runtime reasoning-effort overrides for GPT-5.x via Codex
- `/model agent endpoint/model` runtime endpoint switching with fallback rebuild
- `/login codex` to authenticate against your ChatGPT subscription via OAuth
- Clipboard backend auto-detection (wl-copy → xclip → xsel → OSC 52)

See `git log --oneline kai/self-learning-platform` for the full history.

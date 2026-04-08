# Agent KAI

Terminal-first crypto analysis and paper-trading app powered by `agent-k.ai`.

![Agent KAI terminal screenshot](docs/agent-kai-tui.png)

## What it is

Agent KAI combines:

- a Textual terminal UI
- an OpenAI-compatible agent runtime
- local NATS messaging for agent coordination
- live market data and AI inference from `https://agent-k.ai/`

The repo ships a small local adapter process for the TUI and tools, but market data and model calls come from `agent-k.ai`, not a local database.

## Requirements

- Python 3.13+
- Docker with Compose
- An API key from `https://agent-k.ai/`

## Get an API key

1. Visit `https://agent-k.ai/`
2. Create an account or sign in
3. Open the account/dashboard area
4. Generate an API key
5. Export it as `AGENT_KAI_API_KEY`

Example:

```bash
export AGENT_KAI_API_KEY="kai-..."
```

## Quick start

Start NATS:

```bash
docker compose up -d
```

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Launch the full terminal:

```bash
python main.py --terminal
```

## Run modes

```bash
python main.py --terminal
python main.py
python main.py --no-tui
python main.py --log-level DEBUG
```

## Key environment variables

```bash
export AGENT_KAI_API_KEY="kai-..."
export AGENT_KAI_BASE_URL="https://agent-k.ai"
export AGENT_KAI_WS_URL="wss://agent-k.ai/v1/ws"
```

Optional local settings:

```bash
export KAI_API_PORT=8877
export KAI_NATS_URL="nats://localhost:4222"
export KAI_TRACKED_SYMBOLS="BTC,ETH,SOL"
```

## Commands

- `/analyze BTC`
- `/analyze SOL 15m`
- `/buy BTC 0.1`
- `/sell BTC 0.05`
- `/scan trending`
- `/risk`
- `/chart BTC 1h`
- `/watch DOGE`

## Sub-agent system

One of the strongest features in this repo is the built-in sub-agent runtime.

Agent KAI can spawn specialized background agents, keep them running, and send them targeted tasks over NATS. Each sub-agent gets:

- its own agent identity
- its own workspace directory
- role-specific instructions from `SOUL.md`
- a limited toolset appropriate for delegated work
- request/reply messaging over `agent.{name}.request`

Predefined specialist agents include:

- `analyst`
- `trader`
- `risk-manager`
- `scanner`
- `onchain`
- organization agents like `ceo`, `cto`, `architect`, `developer`, and `qa`

The main agent can use these coordination tools:

- `spawn_agent`
- `nats_request`
- `nats_publish`
- `list_agents`

In practice, this means the system can keep long-lived specialists around for monitoring, scanning, analysis, or delegated execution instead of forcing a single agent to do everything inline.

Examples:

- spawn an `analyst` and keep routing chart-analysis tasks to it
- run a `scanner` in the background for token discovery
- hand portfolio checks to `risk-manager`
- have the main agent orchestrate multiple specialists in parallel over NATS

## Architecture

```text
User
  -> Textual terminal
  -> local tool + data adapter
  -> NATS message bus
  -> agent-k.ai REST + WebSocket APIs
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python regression_harness.py
```

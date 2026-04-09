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
- Linux only: a system clipboard helper for the TUI's copy shortcuts (see [Copy & paste](#copy--paste))
  - Wayland: `sudo apt install wl-clipboard`
  - X11: `sudo apt install xclip`

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

## Copy & paste

The TUI supports three ways to get text out of the chat into your system clipboard:

- **Click and drag** any panel — releases the mouse button to auto-copy the selection
- **Ctrl+Shift+C** — copy the current mouse selection (with a chat confirmation showing the backend used)
- **Ctrl+Y** — copy the most recent agent response in one keystroke, no selection needed

All three routes write to the system clipboard via a runtime-detected backend, in this order: `wl-copy` (Wayland) → `xclip` → `xsel` → OSC 52 (terminal escape sequence). The detected backend is shown in the chat confirmation message — for example:

```
Copied 312 chars to clipboard via wl-copy — The market is currently…
```

### Linux setup

VTE-based terminals on Linux (gnome-terminal, Tilix, Terminator, Konsole, MATE Terminal, XFCE Terminal) **disable OSC 52 clipboard writes by default for security reasons**, so the OSC 52 fallback is unreliable on most default Linux terminals — the bytes leave the program but the terminal silently drops them and your system clipboard never changes. Install one of the native clipboard CLIs and the TUI will pick it up automatically:

```bash
# Wayland sessions (the default on modern Ubuntu / Fedora / Pop!_OS)
sudo apt install wl-clipboard

# X11 sessions
sudo apt install xclip
# or
sudo apt install xsel
```

If the chat confirmation says `via osc52`, your system clipboard probably did **not** receive the text — install one of the packages above and try again. The TUI also logs a warning at startup if no clipboard CLI is found.

### macOS / Windows / SSH

- macOS: OSC 52 works on **iTerm2** (enable in Preferences → General → Selection → "Applications in terminal may access clipboard"), **Kitty**, **Alacritty**, **WezTerm**. Apple's stock Terminal.app does not honor OSC 52 — use one of the alternatives.
- Windows Terminal: OSC 52 works out of the box.
- Over SSH: OSC 52 is the right path because there is no local CLI on the remote box — it tunnels through the SSH session and your local terminal handles the actual write. Same terminal-support caveats apply.

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

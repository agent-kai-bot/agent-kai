# Architecture

For developers and contributors. Process model, NATS topics, storage layout, the tools system, the panel system, the test harness, and how to add new components. File:line references point at source so you can navigate from this doc into code.

## Process model

Agent KAI runs as a single Python process that hosts:

- **The main agent (`kai`)** — a `LangChain AgentExecutor` driven by `agent.core.AgentRunner`
- **Sub-agent instances** — one `agent.sub_agents.SubAgent` per spawned role, each its own LangChain executor with its own tools/prompt/workspace
- **The Textual TUI** — `tui.terminal.TradingTerminal` (or `tui.app.AgentTUI` for the non-trading mode)
- **The NATS bus client** — `nats_bus.bus.NatsBus` connecting to the local NATS server
- **The signal consumer** — `agent.signal_consumer.SignalConsumer` subscribing to `signals.>` topics
- **The chart WebSocket consumer** — `agent.data_sources.kai_api.KaiApiCandleStream` (when the chart source is `kai-api`)

All of this runs in one async event loop. Sub-agents are NOT separate processes — they're separate `SubAgent` instances inside the same Python process, communicating with each other via NATS request/reply (which Just Works in-process via the local NATS server).

The decision to keep everything in one process is intentional: it makes the development loop fast, the failure surface small, and the memory footprint reasonable. The architecture is also a strong fit for a future daemonized deployment where one always-on core agent owns sub-agent lifecycle and terminals connect in as clients — we just haven't built that path yet.

### Process startup

`main.py` is the entry point:

1. Parse CLI args (`--terminal`, `--no-tui`, `--name`, `--nats-url`, `--log-level`)
2. Connect to NATS — if connection fails, the process keeps going without NATS (sub-agents won't work but plain chat with kai will)
3. Create the `SignalConsumer`
4. Create the `SubAgentManager` (only if NATS is available)
5. Build the main agent's tool list via `agent.tools.create_tools(bus, sub_agent_manager, signal_consumer)`
6. Construct the `AgentRunner` with those tools
7. Branch on the run mode:
   - `--terminal` → construct `TradingTerminal`, attach the sub-agent manager, run async
   - `--no-tui` → register a NATS handler for `agent.{name}.request` and loop forever
   - default → construct `AgentTUI` (the simpler chat-only mode) and run async
8. On exit, `SubAgentManager.stop_all()` then `bus.disconnect()`

See `main.py:57` (`async def main`) for the full sequence.

## Directory layout

```
.
├── main.py                          # Entry point
├── config.py                        # Loads agent-config.json + env vars + secret files
├── agent-config.json                # Endpoints, agents, fallback chains, memory, skills, tool_safety
├── docker-compose.yml               # NATS container
├── requirements.txt                 # Python deps
│
├── agent/                           # Agent runtime + tools
│   ├── core.py                      # AgentRunner — the main agent executor
│   ├── sub_agents.py                # SubAgent + SubAgentManager
│   ├── tools.py                     # Tool registry, sandbox, NATS tools, agent tools
│   ├── crypto_tools.py              # query_ohlcv, get_latest_price, calculate_indicator, etc.
│   ├── backtest_tool.py             # run_backtest with the declarative spec
│   ├── memory_store.py              # MemoryStore — per-agent persistent facts
│   ├── memory_tool.py               # LangChain wrapper exposing the memory tool
│   ├── skills_store.py              # SkillStore — per-agent procedural memory
│   ├── skills_tool.py               # LangChain wrappers for skills_list, skill_view, skill_manage
│   ├── learning.py                  # ToolCallRecorder, SessionRecord, parse_mentor_reply, save_reflection_record
│   ├── prompts.py                   # SYSTEM_PROMPT, build_main_system_prompt, build_sub_agent_system_prompt
│   ├── codex_auth.py                # OAuth flow + token management for the codex-cli endpoint
│   ├── signal_consumer.py           # NATS signal subscriber + ring buffer + query interface
│   ├── runtime_utils.py             # ensure_non_empty_response, EMPTY_RESPONSE_ERROR
│   └── data_sources/
│       ├── kai_api.py               # Cloud REST + WebSocket client
│       └── coinbase.py              # Coinbase Advanced Trade public REST + WS client
│
├── tui/                             # Textual TUI
│   ├── app.py                       # AgentTUI — simple chat-only mode (used without --terminal)
│   ├── terminal.py                  # TradingTerminal — full trading mode (used with --terminal)
│   ├── styles.tcss                  # Styles for the simple AgentTUI
│   ├── terminal_styles.tcss         # 3×3 grid layout + per-panel styles for TradingTerminal
│   └── panels/
│       ├── agent_chat.py            # ChatPanel — scrollable chat
│       ├── chart.py                 # ChartPanel — ASCII candlestick + 6 color schemes
│       ├── watchlist.py             # WatchlistPanel — DataTable of tracked symbols
│       ├── positions.py             # PositionsPanel — DataTable of open positions
│       ├── alerts.py                # AlertsPanel — RichLog of signals and alerts
│       ├── history_input.py         # HistoryInput — Input subclass with bash-style history + multi-line paste
│       └── queue_row.py             # QueuedInputRow — Horizontal row with [X] button for the input queue
│
├── nats_bus/                        # NATS client wrapper
│   └── bus.py                       # NatsBus — connect, publish, subscribe, request
│
├── workspaces/                      # Per-agent state (gitignored except SOULs and committed skills)
│   ├── user.md                      # SHARED user profile across all agents
│   ├── analyst/
│   │   ├── SOUL.md                  # Role prompt (committed)
│   │   ├── memories/MEMORY.md       # Per-agent memory (gitignored)
│   │   └── skills/                  # Per-agent skill library
│   │       ├── how-to-write-a-ta-skill.md     # committed meta-skill
│   │       ├── rsi-divergence-hunt.md         # committed example skill
│   │       └── ...
│   ├── trader/                      # Same shape
│   ├── kai/                         # Main agent's workspace
│   └── ...                          # 14 agents total
│
├── eval_results/                    # Reflection records from /learn (gitignored)
│   └── reflection-{ts}-{agent}.json
│
├── logs/                            # Per-agent log files (gitignored)
│   └── {agent_name}_YYYY-MM-DD.log
│
├── tests/                           # Unit tests
│   ├── test_agent_core.py
│   ├── test_agent_kai_client.py
│   ├── test_config.py
│   ├── test_crypto_tools.py
│   ├── test_prompts.py
│   ├── test_runtime_utils.py
│   └── test_sub_agents.py
│
├── test_learning_pipeline.py        # Self-learning regression suite (10 tests)
├── test_signal_pipeline.py          # Signal consumer + tools regression suite (15 tests)
├── regression_harness.py            # Aggregate harness
│
└── docs/                            # User and developer documentation
    ├── README.md                    # Doc index
    ├── getting-started.md
    └── ...                          # 13 doc files total
```

## NATS topics

| Topic | Direction | Purpose |
|---|---|---|
| `agent.{name}.request` | inbound to sub-agent | Task dispatch from anywhere — `nats_request` from another agent or the TUI |
| `agent.{name}.response` | outbound from sub-agent | Reply published after the task completes |
| `agent.{name}.status` | outbound from sub-agent | `{"state": "thinking"\|"fallback"\|"idle", ...}` lifecycle updates |
| `system.registry` | manager broadcasts | `{"agent": "...", "status": "online"\|"offline"}` for spawn/stop events |
| `signals.{strategy}.{symbol}` | inbound to consumer | Live trading signals from external scanners |
| `ai.analysis.completed` | inbound to consumer | AI token analyzer completion events |
| `agent.broadcast` | broadcast | One-to-many messages — every subscriber sees them |
| `market.{symbol}.{type}` | optional | Reserved for market data multiplexing — not heavily used |

All topics are stable. The signal consumer's ring buffer (`agent/signal_consumer.py`) and the alerts panel both subscribe to them.

NATS request/reply uses the standard NATS request pattern: the requester subscribes to a temporary inbox subject, includes the inbox in the message, and the receiver replies to that subject. `nats_bus.bus.NatsBus.request` wraps this with a timeout.

## Storage layout

### Workspaces

Each agent has its own directory under `workspaces/{agent}/`:

```
workspaces/{agent}/
├── SOUL.md              # role prompt (committed for built-in agents)
├── memories/
│   └── MEMORY.md        # per-agent persistent facts (gitignored, written by the memory tool)
└── skills/
    ├── *.md             # one file per skill
```

Plus a shared `workspaces/user.md` (gitignored) that holds the cross-agent user profile.

### Why per-agent isolation

The trader's playbooks shouldn't pollute the analyst's skill catalog. Each agent has its own skill library, its own memory, its own working directory. The `docker_sandbox` tool bind-mounts the agent's workspace as `/work` so the agent can write a script and run it sandboxed without host paths leaking into the LLM's view.

### Eval results

`eval_results/reflection-{ts}-{agent}.json` — one file per `/learn` invocation. Format:

```json
{
  "timestamp": "20260409-104214",
  "bundle": { ... },
  "mentor_reply": "DECISION: create\n...",
  "outcome": { ... }
}
```

Useful for understanding what your agents are learning. See [learning-and-skills.md](learning-and-skills.md).

### Logs

`logs/{agent_name}_YYYY-MM-DD.log` — one log file per agent per day. Each agent's `get_logger(name)` returns a Python logger that writes to this file with timestamps + log level + message. Used for post-mortems on agent runs.

The TUI also writes to `logs/tui_YYYY-MM-DD.log` for chart load errors, signal feed errors, clipboard backend detection, etc.

<a id="tools-system"></a>
## The tools system

Tools are LangChain `StructuredTool` instances built from Python functions. They're created by `agent/tools.py:create_tools` (for the main agent) and `agent/tools.py:create_sub_agent_tools` (for sub-agents — same list minus the spawning + listing tools).

### Tool registry

```python
def create_tools(bus=None, sub_agent_manager=None, signal_consumer=None):
    tools = [
        file_read, file_write, file_edit,
        shell_exec, python_exec,
        web_fetch,
        codex_exec, claude_exec,
    ]
    tools.append(create_docker_sandbox_tool(workspace_host_path=None))  # main agent: no workspace
    tools.extend(_get_crypto_tools(signal_consumer=signal_consumer))    # query_ohlcv, etc.
    if bus:
        tools.append(create_nats_publish_tool(bus))
        tools.append(create_nats_request_tool(bus))
    if sub_agent_manager:
        tools.append(create_spawn_agent_tool(sub_agent_manager))
        tools.append(create_list_agents_tool(sub_agent_manager))
    return tools
```

`_get_crypto_tools` returns `[query_ohlcv, get_latest_price, list_symbols, calculate_indicator, place_order, get_positions, scan_tokens, get_coinbase_candles, get_coinbase_price, list_coinbase_products]` plus `get_signals` (if a signal_consumer is provided) plus `run_backtest` (if `agent/backtest_tool.py` imports cleanly).

### Per-agent extensions

The main agent and sub-agents both get `memory` and the three skill tools (`skills_list`, `skill_view`, `skill_manage`) appended at construction time inside `AgentRunner.__init__` (`agent/core.py:382`) and `SubAgent.__init__` (`agent/sub_agents.py:34`). These tools are bound to each agent's individual `MemoryStore` / `SkillStore`, so they can't accidentally read or write the wrong agent's data.

### Tool difference between main and sub-agents

| Tool | Main | Sub-agent |
|---|---|---|
| `nats_request` | yes | NO (sub-agents shouldn't request other sub-agents — cycles risk) |
| `spawn_agent` | yes | NO (only the main agent spawns) |
| `list_agents` | yes | NO |
| `nats_publish` | yes | yes (for fire-and-forget) |
| Everything else | yes | yes |

This is enforced in `create_sub_agent_tools` (`agent/tools.py:725`) which builds a deliberately smaller list.

### Adding a new tool

1. Write the function:

```python
# agent/tools.py or a new module

def _my_tool(arg1: str, arg2: int = 5) -> str:
    """Do the thing."""
    return f"did the thing with {arg1} {arg2}"

my_tool = StructuredTool.from_function(
    func=_my_tool,
    name="my_tool",
    description="Does the thing. Inputs: arg1 (str), arg2 (int, default 5).",
)
```

2. Add it to the appropriate registry:

- `agent/tools.py:create_tools` for main-agent-only tools
- `agent/tools.py:create_sub_agent_tools` for sub-agent-accessible tools (or both)
- `agent/crypto_tools.py:ALL_CRYPTO_TOOLS` if it's a crypto/market tool (auto-registers everywhere)

3. Restart the TUI. The next agent construction picks up the new tool. Sub-agents need to be respawned via `/model agent kai-smart` (or any model swap) for them to see the new tool.

4. Update the `SYSTEM_PROMPT` in `agent/prompts.py` if the tool needs an explicit one-line description in the LLM's system prompt. Most tools don't — the LangChain tool description is enough.

<a id="textual-tui"></a>
## The panel system (Textual TUI)

The TUI is a 3×3 grid layout defined in `tui/terminal_styles.tcss`:

```
grid-size: 3 3;
grid-columns: 1fr 3fr 1fr;
grid-rows: auto 3fr 2fr;
```

Plus a status bar docked to the bottom and an input box docked above it.

### Panel widgets

Each panel is a Textual `Widget` (or `DataTable` / `RichLog` / `Static`) under `tui/panels/`. The `compose()` method on `TradingTerminal` yields them in order:

```python
def compose(self) -> ComposeResult:
    yield Static("KAI Trading Terminal", id="header")
    yield WatchlistPanel(id="watchlist-panel")
    yield ChartPanel(id="chart-panel")
    yield AlertsPanel(id="alerts-panel")
    yield PositionsPanel(id="positions-panel")
    yield ChatPanel(id="chat-panel")
    yield RichLog(id="nats-panel", markup=True, wrap=True)
    yield Static("Status: idle | Portfolio: $100,000.00", id="status-bar")
    yield HistoryInput(
        placeholder="...",
        id="input-area",
        history_path=Path("workspaces/terminal/input_history.txt"),
    )
```

The grid auto-flows children left-to-right, top-to-bottom. The `id` selectors in `terminal_styles.tcss` apply per-panel CSS (borders, colors, padding).

### Adding a new panel

1. Create `tui/panels/your_panel.py`:

```python
from textual.widget import Widget

class YourPanel(Widget):
    DEFAULT_CSS = """
    YourPanel {
        height: 1fr;
    }
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
```

2. Add it to `compose()` in `tui/terminal.py` between the other `yield` calls. The grid will auto-place it.

3. Add a CSS rule for it in `tui/terminal_styles.tcss`:

```css
#your-panel {
    border: solid $accent;
    padding: 0 1;
}
```

4. If you want to control where it lands in the grid, use `column-span`/`row-span` in the CSS or change `grid-size` to add a row/column.

5. Wire it to data — the panel needs an `update_*` method the TUI calls when state changes.

See [watchlist-and-positions.md#adding-a-new-panel](watchlist-and-positions.md#adding-a-new-panel) for an example.

### Slash command handling

`tui/terminal.py:_handle_slash_command` is the dispatcher. It splits on whitespace, picks `parts[0]` as the command name, and routes to a per-command handler. Adding a new slash command:

1. Add an `elif cmd == "/yourcmd":` branch with the dispatch logic
2. Optionally add a per-command handler method (`_handle_yourcmd_command(parts)`) if it has multiple forms
3. Update the welcome banner string in `on_mount` to mention the new command

### Keybindings

Class-level `BINDINGS` list on `TradingTerminal`. Each entry is `(key, action_name, description)`. The action method is auto-dispatched by Textual via the `action_*` naming convention.

```python
BINDINGS = [
    ("ctrl+y", "copy_last_response", "Copy last reply"),
    ...
]

def action_copy_last_response(self) -> None:
    ...
```

See [keybindings.md](keybindings.md) for the full list and [keybindings.md#adding-new-keybindings](keybindings.md#adding-new-keybindings) for the extension guide.

### Event handling and Textual MRO

Textual's message pump dispatches events by walking the receiving widget's MRO and calling every `_on_*` method in order. For overrides where you want to REPLACE the parent's behavior (not extend it), call `event.prevent_default()` in your override. `event.stop()` only blocks bubbling to ancestor widgets, NOT the within-widget MRO walk.

This is documented in `tui/panels/history_input.py:_on_paste` — the multi-line paste fix uses `prevent_default()` to keep `Input._on_paste` from running after the override and leaking the first line into the visible value.

## Testing

### Unit tests

Standard `unittest` suite under `tests/`:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Test files:

| File | Covers |
|---|---|
| `tests/test_agent_core.py` | `AgentRunner`, `create_llm`, `_create_codex_chat_model`, `_flatten_chat_message` |
| `tests/test_agent_kai_client.py` | The cloud REST + WS client adapter |
| `tests/test_config.py` | `get_endpoint`, `get_agent_config`, `set_agent_reasoning_effort`, secret loading |
| `tests/test_crypto_tools.py` | `query_ohlcv`, `calculate_indicator`, `_extract_bbands_levels`, etc. |
| `tests/test_prompts.py` | `build_main_system_prompt`, `build_sub_agent_system_prompt` |
| `tests/test_runtime_utils.py` | `ensure_non_empty_response` |
| `tests/test_sub_agents.py` | `SubAgent.__init__`, `SubAgentManager.spawn`, fallback chain construction |

### Regression harnesses

Two end-to-end harnesses live at the project root:

```bash
.venv/bin/python test_learning_pipeline.py    # 10 tests
.venv/bin/python test_signal_pipeline.py      # 15 tests
```

`test_learning_pipeline.py` exercises the full reflection loop:

1. Build a `ToolCallRecorder` and verify it captures `on_tool_start` / `on_tool_end`
2. Build a `SessionRecord` and verify `to_bundle` shape
3. Parse a synthetic mentor reply with `parse_mentor_reply` (covers `create`, `patch`, `no_skill`)
4. Verify `save_reflection_record` writes a valid JSON file
5. Verify `SkillStore.create` and `.list_skills` round-trip
6. Verify `MemoryStore.add` / `.replace` / `.remove` semantics
7. Verify the security scan rejects injection patterns

`test_signal_pipeline.py` exercises the live signal path:

1. `SignalConsumer.add_manual` ingests a signal into the ring buffer
2. `SignalConsumer.query` filters by symbol / strategy / signal_type
3. `SignalConsumer.subscribe` connects to a real NATS server and receives a real publish
4. `get_signals` tool returns the right payload
5. The AI analysis event handler normalizes correctly
6. The signal summary format

Both harnesses are standalone (no pytest) and exit non-zero on failure. CI runs them.

### Integration testing

There's no automated end-to-end TUI test (Textual's pilot mode supports it but is fiddly to wire up for the full grid layout). Manual testing for the TUI is documented in each PR's "Verified" section.

## Logging

Per-agent loggers via `agent_logger.get_logger(name)`. Each returns a Python logger that writes to `logs/{name}_YYYY-MM-DD.log` with format:

```
2026-04-09 14:23:01 [INFO] kai: REQUEST agent=kai task=Run a full technical analysis...
2026-04-09 14:23:08 [WARNING] analyst: PRIMARY_FAILED attempt=1/2 trying fallback
2026-04-09 14:23:14 [INFO] analyst: RESPONSE length=2313
```

The level is `DEBUG` by default (from `agent-config.json`), overridable via `--log-level`.

There's also `agent_logger.log_agent_event(name, event_type, payload)` for structured one-line events that go to the same log file. Used for `init`, `started`, `stopped`, `request`, `response`, `fallback_N`, `reload_llm`.

## Configuration loading

`config.py` is loaded once at module import time. It:

1. Reads `agent-config.json`
2. Loads `.env` if present (via python-dotenv)
3. Reads secret files (`AGENT-KAI-API-KEY.txt`)
4. Exports module-level constants (`AGENTS`, `ENDPOINTS`, `NATS_URL`, `DOCKER_SANDBOX_*`, etc.)
5. Defines `get_endpoint(name, model_name=None)` and `get_agent_config(agent_name)` for runtime lookup

Changes to `agent-config.json` require a TUI restart. The exception: `/model` and `/think` mutate `AGENTS` in memory directly so you can iterate without restart.

See [configuration.md](configuration.md) for the full schema.

## Common dev workflows

### Running a single agent in headless mode

```bash
python main.py --no-tui --name analyst --log-level DEBUG
```

Subscribes to `agent.analyst.request` and idles. From another shell:

```bash
nats req agent.analyst.request '{"task": "Analyze BTC 1h", "from": "test"}'
```

The output streams to stdout in real time.

### Testing a tool in isolation

```python
from agent.tools import _query_ohlcv
print(_query_ohlcv("BTC", "1h", 50))
```

Tools are plain Python functions wrapped in `StructuredTool` — you can call them directly without going through LangChain.

### Triggering the learning loop without the TUI

```python
from agent.sub_agents import SubAgent
from nats_bus.bus import NatsBus
import asyncio

async def main():
    bus = NatsBus(url="nats://localhost:4222", agent_name="test")
    await bus.connect()
    analyst = SubAgent("analyst", bus)
    await analyst.start()
    output = await analyst.run_once("Run a quick TA on BTC 1h")
    print("Response:", output[:200])
    print("Tool calls:", len(analyst.last_session.tool_calls))
    print("Bundle:", analyst.last_session.to_bundle(chat_turns=[], existing_skills=analyst.list_existing_skills()))

asyncio.run(main())
```

### Inspecting a reflection record

```bash
ls eval_results/
cat eval_results/reflection-20260409-104214-analyst.json | jq '.bundle.tool_calls[] | {tool, error}'
```

### Tail the agent's log file

```bash
tail -f logs/analyst_$(date +%F).log
```

### Force-reload a sub-agent's config

```python
# In the TUI:
/model analyst kai-smart    # any model swap rebuilds the executor
```

## Anti-patterns

Things that look reasonable but break the architecture:

- **Sub-agents calling `nats_request` to other sub-agents.** Cycles risk. Use `nats_publish` for fire-and-forget coordination, or have the main agent coordinate the request/reply chain.
- **Mutating `agent-config.json` from within the agent.** It's loaded once at startup. Use `/model` and `/think` for runtime overrides, or document in the relevant doc that the user must restart.
- **Storing per-session state on the `SubAgent` instance.** It survives across tasks. Use `self.last_session` for the per-task slot, and reset it at the start of `_handle_request`.
- **Calling LLM APIs directly from a tool.** Tools should return data, not chat. If you need a sub-task LLM call, use `codex_exec` / `claude_exec` or spawn a sub-agent.
- **Reading from the TUI's chat panel from inside an agent.** The agent has its own `chat_history` — use that. Reading from the panel is a layering violation.
- **Hardcoding prompts in code.** They go in `agent/prompts.py` (for the shared base) or in `workspaces/{agent}/SOUL.md` (for the role).

## Where to make changes

| You want to... | Edit |
|---|---|
| Add a new slash command | `tui/terminal.py:_handle_slash_command` |
| Add a new keybinding | `tui/terminal.py:BINDINGS` + an `action_*` method |
| Add a new tool | `agent/tools.py` or a new module, then register in `create_tools` / `_get_crypto_tools` |
| Add a new sub-agent | `agent-config.json` + `workspaces/{name}/SOUL.md` (no code) |
| Add a new endpoint / model | `agent-config.json` (no code) |
| Add a new chart color scheme | `tui/panels/chart.py:SCHEMES` |
| Add a new data source | `agent/data_sources/` + a tool wrapper in `agent/crypto_tools.py` |
| Change the system prompt | `agent/prompts.py:SYSTEM_PROMPT` |
| Tighten the docker sandbox | `agent-config.json:tool_safety.docker_sandbox` |
| Add a new test | `tests/test_*.py` for unit, or extend `test_learning_pipeline.py` / `test_signal_pipeline.py` for regression |

## Performance notes

- **LLM token budget is the bottleneck.** Tool outputs are truncated at `max_output_chars` (5000 by default) to prevent one tool call from blowing the context. The `max_file_read_chars` cap (10000) does the same for `file_read`.
- **Memory and skills are loaded once at agent construction.** The frozen-snapshot pattern means mutations don't trigger prompt rebuilds — the LLM sees the live state in tool responses but the system prompt stays stable for prefix-cache stability.
- **Skill catalog is intentionally tiny in the prompt.** Only `name + description` per skill. Full bodies are loaded on demand via `skill_view`. Progressive disclosure.
- **The chat panel doesn't paginate.** Scrolling 10k lines of agent output through Rich is fine in practice — Textual handles virtualized rendering well — but if you push it past ~50k lines you'll feel it.
- **The chart re-renders on every bar update.** With 200 bars (the cap) the render takes <5ms. WebSocket updates arrive once per minute on the 1m timeframe so cost is negligible.

## Where to go from here

- [getting-started.md](getting-started.md) — try it
- [configuration.md](configuration.md) — change it
- [agents.md](agents.md) — extend it
- [troubleshooting.md](troubleshooting.md) — debug it

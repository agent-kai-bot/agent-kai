# Daemon + Multi-Session + Web UI Migration Plan

Status: **DESIGN — awaiting sign-off on the 8 open questions before Phase 1.**

This document captures the architectural redesign for turning the current
single-process TUI into a daemon-backed system with multi-session support
and a web UI. It is the canonical reference for the migration — update
it as decisions land instead of re-litigating them in chat.

---

## 1. TL;DR / Core Mental Model

Three runtime components, one shared core:

1. **`kaid`** — the daemon. An always-on background process that owns:
   - Every active **session** (named conversation contexts)
   - The **sub-agent registry** and dispatcher
   - The **signal consumer** (NATS subscriptions, signal fan-out)
   - **Market data** streams (Coinbase WS, kai-api polls, fan-out to sessions)
   - The **scheduler** (cron + event triggers; dispatches prompts into sessions on schedule)
   - Persistent stores: watchlists, chat history, autotrade state, ledger
   - A **WebSocket server** exposing a JSON protocol to clients
   - An **HTTP/REST API** for session listing, config, health, web UI assets

2. **Terminal client** — a thin Textual TUI that connects to `kaid` over
   WebSocket, attaches to a named session, and renders events. Everything
   that is currently in `tui/terminal.py` that touches network, agents,
   NATS, or market data moves to the daemon. What remains is rendering,
   keyboard handling, and the WS client adapter.

3. **Web UI client** — a Svelte SPA that speaks the **same WS protocol**
   as the terminal client. It gets the same events, the same chart bars,
   the same sub-agent updates. One protocol to maintain, two renderers.

Everything important — agent state, sub-agents, memory, market data,
autotrade — lives in the daemon. Clients are viewports. Closing a
terminal does not interrupt the agent. Opening a new terminal on the
same session picks up where the previous one left off.

```
         ┌───────────────────────────────────────────────┐
         │                  kaid (daemon)                │
         │                                               │
         │  ┌──────────┐  ┌──────────┐  ┌──────────┐     │
         │  │ session1 │  │ session2 │  │ sessionN │ ... │
         │  └─────┬────┘  └─────┬────┘  └─────┬────┘     │
         │        │             │             │          │
         │        └─────────────┼─────────────┘          │
         │                      │                        │
         │  ┌──────────────────┴──────────────────┐      │
         │  │  shared: sub-agents, signal bus,    │      │
         │  │  market data, memory, skills, NATS  │      │
         │  └─────────────────────────────────────┘      │
         │                      │                        │
         │              ┌───────┴───────┐                 │
         │              │  WebSocket +  │                 │
         │              │  REST server  │                 │
         │              └───────┬───────┘                 │
         └──────────────────────┼─────────────────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
    ┌─────┴──────┐       ┌──────┴──────┐       ┌──────┴──────┐
    │ terminal 1 │       │  terminal 2 │       │   web UI    │
    │ (session1) │       │  (session2) │       │ (sessionN)  │
    └────────────┘       └─────────────┘       └─────────────┘
```

---

## 2. What is a "session"?

A **session** is a named conversation context with its own agent runner
and persisted UI state. Named so the user can have multiple independent
lines of work (e.g. `--session btc-scalper`, `--session alts-research`)
without them stomping on each other's chat history or chart state.

### Per-session state (isolated)

- `chat_history` — list of user/agent/tool messages for the session
- `input_queue` — pending user inputs the agent hasn't processed yet
- UI state: `chart_symbol`, `chart_timeframe`, `chart_source`,
  `chart_size_mode`, `chart_color_scheme`
- `watchlist_symbols` — the left-column tracked list
- `autotrade_enabled` — per-session kill switch
- `activity_status` — the current "what is the agent doing" string
- A dedicated **`AgentRunner` instance** (or equivalent) holding the
  per-session LLM conversation state, tool budget, and cancellation
  handle
- **Sub-agent pool** — each session owns its own instances of every
  sub-agent in the registry. Sub-agents have long-lived per-session
  memory (planned feature), so isolation is mandatory: session A's
  `researcher` remembers what session A discussed, session B's does
  not. See §6 Q1 for the rationale.
- **Per-session memory store** — semantic memory scoped to this
  session. Sub-agents write to and read from their session's memory,
  not a global one.

Persisted at `workspaces/sessions/{name}.json` (session-level state)
plus `workspaces/sessions/{name}/memory/` (session-scoped memory
store) plus `workspaces/sessions/{name}/sub_agents/{agent}.json`
(per-sub-agent conversation buffers). Load-then-merge writes
everywhere so concurrent sessions don't clobber each other.

### Shared state (global to the daemon)

- **Sub-agent registry / definitions** — the *templates* (prompts,
  tool lists, model endpoints) loaded once from `agent-config.json`.
  Sessions instantiate sub-agents *from* these templates; the
  templates themselves are read-only and shared.
- **Market data streams** — one Coinbase WS connection, one kai-api
  poller, fan out to every session subscribed to a given symbol.
- **Signal consumer** — one NATS subscriber; signal events get
  broadcast to every session (or only sessions that opted in via
  `react: true` in their config).
- **Skills / tools / config** — loaded once at daemon boot.
- **Model clients** — the underlying HTTP clients to local LLM
  endpoints, kai-api, codex, etc. Sub-agents *across* sessions share
  the same connection pool so we don't multiply outbound sockets.
- **Scheduler** — one APScheduler instance + a persistent job store.
  Jobs are tagged with their owning session and dispatched into that
  session's input queue when they fire. The scheduler must be
  daemon-global (not per-session) because triggers have to fire even
  when no client is attached. See §5 for the full design.

The split is: **definitions and I/O resources are global; conversation
state and memory are per-session.** Sub-agents are cheap to instantiate
from a shared template (a Python object holding a prompt + a reference
to a shared model client), expensive to give long-lived memory — and
the memory is exactly what we need to isolate.

---

## 3. Wire protocol

**Transport:** WebSocket. JSON payloads. One envelope type per
message, with a `type` field for dispatch. The same protocol is used
by the Textual client and the Svelte web UI.

### Why WebSocket (not gRPC, not raw NATS exposure)

- **Bidirectional** — daemon → client token streaming and client →
  daemon inputs both need to work without polling.
- **Native browser support** — the web UI can connect with zero extra
  libraries.
- **Native Python support** — `websockets` or FastAPI's built-in WS
  handlers, no codegen.
- **Streaming-friendly** — per-token agent output, per-bar chart
  updates, sub-agent progress events all flow naturally.
- **Observable** — you can point a browser at a WS debugger or
  `websocat` and see every message. gRPC would require tooling.
- **NATS is internal infrastructure**. Exposing NATS to browsers
  would require a bridge, auth layer, and subject ACLs — all of which
  we'd have to build from scratch. Keep NATS for daemon-internal
  sub-agent dispatch and signal fan-out.

### Client → daemon messages

```jsonc
// Attach to (or create) a named session. First message after connect.
{"type": "attach", "session": "btc-scalper", "create_if_missing": true}

// Send a user input to the attached session.
{"type": "input", "text": "analyze BTC 4h and tell me what you see"}

// Interrupt the currently-running agent turn (Ctrl+C equivalent).
{"type": "interrupt"}

// Subscribe to a per-symbol chart feed (client decides what to render).
{"type": "subscribe", "channel": "chart", "symbol": "BTC-USD", "tf": "4h"}
{"type": "unsubscribe", "channel": "chart", "symbol": "BTC-USD", "tf": "4h"}

// Subscribe to signal events for this session.
{"type": "subscribe", "channel": "signals"}

// Keepalive.
{"type": "heartbeat"}

// Execute a slash command on the server side (e.g. /status, /autotrade).
{"type": "slash", "command": "/autotrade", "args": "on"}
```

### Daemon → client messages

```jsonc
// Acknowledgement after attach, with session state snapshot.
{
  "type": "session_attached",
  "session": "btc-scalper",
  "state": {
    "chart_symbol": "BTC-USD",
    "chart_timeframe": "4h",
    "watchlist_symbols": ["BTC", "ETH", "SOL"],
    "autotrade_enabled": false,
    "chat_history": [ /* last N messages */ ]
  }
}

// One LLM token (or a small batch of tokens) for the current turn.
{"type": "token", "text": " consolidation"}

// Tool-call lifecycle.
{"type": "tool_start", "tool": "get_ohlcv", "args": {"symbol": "BTC", "tf": "4h"}}
{"type": "tool_end",   "tool": "get_ohlcv", "elapsed_ms": 412, "ok": true}

// The final assembled response text (for markdown re-render).
{"type": "final", "text": "## Analysis\n\nBTC is in a ..."}

// Activity status string for the status bar.
{"type": "status", "activity": "calling get_ohlcv", "queue": 0}

// Signal from the signal consumer (broadcast to subscribed sessions).
{"type": "signal", "signal": {"symbol": "ETH", "side": "long", "score": 0.82, ...}}

// Chart bar update.
{"type": "chart_bar", "symbol": "BTC-USD", "tf": "4h",
 "bar": {"ts": 1712668800, "o": 68000, "h": 68450, "l": 67920, "c": 68320, "v": 1240.5}}

// Watchlist price update.
{"type": "watchlist_price", "symbol": "ETH", "price": 3421.50,
 "volume": 12453.2, "change_24h_pct": 1.8}

// Sub-agent dispatch lifecycle.
{"type": "sub_agent_event", "agent": "researcher", "phase": "started",
 "task_id": "abc123"}
{"type": "sub_agent_event", "agent": "researcher", "phase": "finished",
 "task_id": "abc123", "result_preview": "..."}

// Out-of-band error.
{"type": "error", "code": "llm_timeout", "message": "Upstream LLM timed out after 60s"}
```

### Multi-attach semantics

Multiple clients can attach to the same session simultaneously
(think `screen -x`). All attached clients receive all events. All
inputs from any attached client are enqueued on the same session.
This enables:

- Running a terminal on the desktop and a web UI on a phone for the
  **same** trading session, both seeing the same state.
- Leaving a session running overnight with no client attached, then
  reconnecting in the morning and seeing the full chat history
  including anything the agent produced while unattended.

---

## 4. Backwards compatibility

### `--terminal` should "just work"

The current UX is `python -m src.main --terminal`. After the
migration that command should still work — transparently. Specifically:

1. When the user runs `--terminal`, the client checks whether `kaid`
   is reachable on `localhost:PORT` (default from config).
2. If not reachable, the client **auto-spawns** the daemon as a
   detached background process, waits until its health endpoint
   returns OK, then connects.
3. If no `--session` flag is passed, it attaches to a session named
   `default` (auto-created on first use).
4. Everything from the user's perspective works the same as today,
   except the agent keeps running when they `q` out of the TUI.

### `--session NAME`

Add a flag: `python -m src.main --terminal --session btc-scalper`.
Same flow as above, attaches to the named session. Creates it if
it doesn't exist.

### `--standalone` escape hatch

Keep the current in-process mode available under `--standalone`
for:
- Development (faster iteration, single-process debugging)
- Environments where you don't want a long-running daemon
- Regression testing the old code path

Both modes share the same `terminal.py` render layer; only the data
source (in-process agent vs WS client) differs. That's the key
refactor: extracting a clean data-source interface that both modes
implement.

### Existing CLI entry points

- `--terminal` → auto-spawn + connect + attach to `default`
- `--terminal --session NAME` → same, named session
- `--terminal --remote ws://host:port` → connect to a remote daemon
- `--daemon` → run the daemon in the foreground (for systemd or debugging)
- `--standalone` → in-process mode, no daemon
- `--chat`, `--once`, etc. — likely become thin clients or stay
  in-process depending on user preference (ask during phase 2)

---

## 5. Scheduling system

### What it is

A daemon-resident scheduler that lets the user (or the agent on the
user's behalf) schedule prompts to run at a specific time, on a
recurring schedule, or in response to an event. Triggered prompts
are dispatched into a target session's input queue exactly as if the
user had typed them — the agent processes them as a normal turn,
output streams to attached clients, and the result lands in chat
history with a `[scheduled job: JOB_ID]` marker.

The scheduler lives in the daemon for one reason: it must survive
client disconnects. A scheduler that ran inside the terminal would
stop firing the moment you `q` out of the TUI. The whole point is to
wake the daemon at 3am to do something useful even if no client is
attached, then show you the result the next time you connect.

### Two trigger families

**Time triggers** — "at 7am tomorrow", "every weekday at 9:30 ET",
"in 30 minutes", "the first Friday of every month at noon UTC".
Implemented via APScheduler with croniter parsing cron expressions.
Natural-language time strings are normalized by the agent (LLM)
inside the tool call, so by the time the scheduler stores a job,
the spec is either an absolute ISO timestamp or a clean cron string.

**Event triggers** — "if BTC drops 5% in an hour", "when the
long-signal scanner publishes a score above 0.8 for ETH", "when my
custodial balance falls below $100". Implemented as predicate
subscribers on the daemon's internal event bus (the same bus that
fans out signals + market data + balance changes to sessions).
The predicate is a **structured filter object**, not a Python
expression — no `eval`, no DSL parser:

```jsonc
{
  "channel": "signals",
  "filter": {
    "score": {"gt": 0.8},
    "symbol": "ETH",
    "side": "long"
  }
}
```

Filter operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`,
`contains`, `regex`. The agent constructs the filter object from
natural language at tool-call time; the scheduler validates it
against a small JSON Schema before persisting.

### Job model

```jsonc
{
  "id": "job_2026_04_09_a3f1",
  "type": "cron" | "absolute" | "event",
  "spec": {
    // type=cron:    {"cron": "0 7 * * *", "tz": "America/Chicago"}
    // type=absolute:{"at": "2026-04-10T07:00:00-05:00"}
    // type=event:   {"channel": "signals", "filter": {...}}
  },
  "prompt": "Check overnight news for BTC and summarize",
  "owner_session": "btc-scalper",
  "created_at": "2026-04-09T14:32:00Z",
  "created_by": "user" | "agent",
  "last_run": null,
  "next_run": "2026-04-10T12:00:00Z",
  "run_count": 0,
  "max_runs": null,                  // null = unbounded
  "status": "active" | "paused" | "completed" | "failed" | "cancelled",
  "last_result_preview": null,
  "concurrency": "skip" | "queue",   // when previous run still in flight
  "tool_budget": null                // overrides session default if set
}
```

### Persistence

Jobs live at `workspaces/scheduler/jobs.json`, atomic writes,
load-then-merge so concurrent updates don't clobber each other.
Re-loaded on daemon boot. Catch-up policy: missed triggers within a
configurable window (default **5 minutes**) are re-dispatched on
boot; older missed triggers are logged and skipped to prevent waking
up to a flood of stale work. The window is short on purpose — long
catch-up windows turn restart-after-vacation into a fire-hose.

### Agent tools

The agent gains new tools so it can create schedules from natural
language inside a normal chat turn:

- `schedule_at(when, prompt, session=None)` — one-shot at a specific
  time. `when` must be an ISO 8601 timestamp; the LLM normalizes
  user phrasing ("tomorrow 7am") before calling the tool.
- `schedule_recurring(cron, prompt, session=None, max_runs=None)` —
  recurring on a cron expression. The LLM converts "every weekday
  at market open" → `30 13 * * 1-5` (UTC) before calling.
- `schedule_when(condition, prompt, session=None, max_runs=None)` —
  event-based; condition is the structured filter object.
- `list_scheduled_jobs(session=None)` — list active jobs for the
  current session (or another, with the session arg).
- `cancel_scheduled_job(job_id)` — cancel a job. By default the
  caller can only cancel jobs in its own session; cross-session
  cancel requires an admin flag.
- `pause_scheduled_job(job_id)` / `resume_scheduled_job(job_id)` —
  temporary pause without losing the job definition.

### Slash commands

For users who want to skip the agent and create jobs directly:

- `/schedule list` — list active jobs in this session
- `/schedule list all` — list active jobs across all sessions
- `/schedule add at "tomorrow 7am" "Check BTC overnight news"`
- `/schedule add cron "0 9 * * 1-5" "Pre-market summary"`
- `/schedule cancel JOB_ID`
- `/schedule pause JOB_ID` / `/schedule resume JOB_ID`
- `/schedule show JOB_ID` — full details + last result

### Wire protocol additions

New envelopes broadcast to all clients attached to the owning
session:

```jsonc
{"type": "scheduled_job_created",   "job": {...}}
{"type": "scheduled_job_triggered", "job_id": "...", "fired_at": "..."}
{"type": "scheduled_job_completed", "job_id": "...", "result_preview": "..."}
{"type": "scheduled_job_failed",    "job_id": "...", "error": "..."}
{"type": "scheduled_job_cancelled", "job_id": "..."}
{"type": "scheduled_job_paused",    "job_id": "..."}
{"type": "scheduled_job_resumed",   "job_id": "..."}
```

The web UI uses these to update a "scheduled jobs" panel live; the
terminal client renders them inline in the chat as small status
lines.

### Trigger execution flow

1. Scheduler wakes — APScheduler tick (time trigger), event-bus
   match (event trigger), or REST POST (manual fire).
2. Looks up `owner_session` by name; if missing, mark job `failed`,
   emit `scheduled_job_failed`, log it.
3. Constructs an input envelope:
   `{"type": "input", "text": prompt, "source": "scheduler", "job_id": "..."}`
4. Enqueues it on the session's input queue.
5. Session processes it as a normal turn — agent runs, sub-agents
   dispatch, output streams to attached clients, all chat history
   gets the `[scheduled job: JOB_ID]` marker.
6. On completion: scheduler updates `last_run`, `next_run`,
   `run_count`, `last_result_preview`, persists, and emits
   `scheduled_job_completed`. If `run_count >= max_runs`, job
   transitions to `completed`.
7. Concurrency: if `concurrency == "skip"` and the session is busy,
   the trigger is logged but not enqueued (and a warning event is
   emitted). If `"queue"`, it's enqueued normally.

### Safety rails

- **Per-session active-job soft cap** (configurable, default **50**)
  to catch runaway schedule loops before they multiply.
- **Tool budget per triggered run** — each scheduled run starts with
  the session's normal tool budget, configurable per job. A runaway
  scheduled job can't drain the LLM beyond its allotted call count.
- **Self-scheduling loop detection** — if a triggered run attempts
  to call `schedule_*` and the new job would target the same session
  with similar parameters, surface a warning event and require the
  user to confirm via slash command before persisting. Catches the
  classic "the agent scheduled the agent which scheduled the agent"
  failure mode.
- **Pause-all kill switch** — `/schedule pause all` immediately
  pauses every active job in the session (or daemon-wide with
  `--global`). Useful when something is going wrong and you want
  to stop the bleeding without thinking.

### What scheduling does NOT do

- Not a workflow engine. No DAGs, no fan-out, no
  conditional-job-creates-job. If you need that, the agent itself
  can call `schedule_*` from inside a triggered run, but the
  scheduler stays a flat list of jobs.
- Not a cron replacement for OS-level tasks. It only triggers
  prompts inside the agent. If you need to restart a service at
  4am, use systemd.
- No email/SMS/Slack notifications on job completion as a built-in.
  If you want a Discord ping when a job finishes, the agent's
  triggered run can call the existing Discord/Telegram tools (when
  those land).

---

## 6. Web UI stack

### Requirements

- **Not Streamlit** (user requirement — stated explicitly)
- Financial chart rendering that feels native (candlesticks, volume,
  indicators, crosshair, zoom/pan)
- Dark theme by default
- Low runtime footprint (this is a personal tool, not a product, so
  every kb of JS matters)
- Fast iteration — we'll be tweaking panels constantly
- Reuses the same WS protocol as the terminal client

### Recommendation

- **Framework: Svelte + SvelteKit**
  - ~10 kb runtime vs React's ~40 kb+
  - Reactivity is built in, no Redux/Zustand needed
  - SvelteKit gives us routing, server-side adapter for FastAPI
    integration, and a dev server with HMR
  - Less boilerplate than React for small projects
  - Downside: smaller ecosystem than React, but every library we
    actually need (charts, markdown, WS client) exists

- **Charts: TradingView Lightweight Charts**
  - Free, MIT-licensed
  - Purpose-built for OHLCV — candlestick, line, volume, area series
  - Financial conventions (log scale, right-aligned, crosshair,
    time axis) out of the box
  - Tiny (~40 kb gzipped)
  - Battle-tested on every crypto exchange web UI you've ever used

- **Styling: Tailwind CSS**
  - Fast iteration without context-switching to a separate CSS file
  - Dark mode support via `dark:` variants
  - Composable; easy to build a small set of primitive panels and
    reuse them

- **Markdown: `svelte-markdown` or a port of the Rich renderable**
  - We need code blocks, tables, lists — ReactMarkdown-equivalent
  - Same content the Textual ChatPanel renders via Rich Markdown

- **Auth:** bearer token stored in `workspaces/daemon-token.txt`,
  generated on first daemon boot. Localhost-only by default. If
  the user exposes the daemon to the LAN, they set a config flag
  and the token gates every WS/REST request. Rotate on demand.

### Web UI layout (mirrors the terminal)

```
┌─────────────────────────────────────────────────────────────┐
│  header: session name · symbol · activity · autotrade       │
├────────┬──────────────────────────────────────────┬─────────┤
│ watch  │                                          │ alerts  │
│ list   │                                          │         │
│        │              chart panel                 ├─────────┤
├────────┤              (lightweight-charts)        │ nats    │
│ pos-   │                                          │ bus     │
│ itions │                                          │         │
│        ├──────────────────────────────────────────┤         │
│        │              chat / agent output          │         │
│        │              (markdown)                    │         │
├────────┴──────────────────────────────────────────┴─────────┤
│ input box                                                   │
└─────────────────────────────────────────────────────────────┘
```

Same information density as the terminal, responsive to window
resize, but with mouse interaction for the chart and hyperlinks in
the chat.

### REST endpoints (alongside WS)

Some things are request/response and don't need streaming:

- `GET /api/health` — daemon status, uptime, version
- `GET /api/sessions` — list of sessions + last_activity
- `POST /api/sessions` — create a new session
- `DELETE /api/sessions/:name` — kill a session
- `GET /api/agents` — sub-agent registry
- `GET /api/skills` — loaded skills list
- `GET /api/config` — current config (redacted: no API keys)
- `POST /api/config` — update config (restricted fields)

All REST endpoints share the same bearer-token auth as the WS
handler.

---

## 7. Open questions awaiting user sign-off

These need answers before I start Phase 1. Recommendations in
parentheses — override as needed.

1. **Sub-agent scoping.** Global across sessions, or one sub-agent
   pool per session?
   ***DECIDED: per-session pool.*** Sub-agents will gain long-lived
   per-session memory (planned feature), so isolation has to be
   designed in from the start, not retrofitted. Templates +
   underlying model-client connection pools stay global; the
   instantiated sub-agents and their memory live inside the session.

2. **Daemon transport.** TCP on `localhost:PORT` or Unix domain socket?
   ***DECIDED: TCP, with configurable bind address.*** Default
   `127.0.0.1:PORT` for local-only safety. Config flag flips it to
   `0.0.0.0:PORT` so the user can hit the daemon from another
   machine on the LAN (or via VPN). Bearer-token auth becomes
   non-optional whenever the bind address is non-localhost — the
   daemon refuses to start with `0.0.0.0` and no token configured.

3. **Web UI framework.** Svelte + SvelteKit vs React + Next.js vs
   SolidJS vs plain HTML + HTMX?
   ***DECIDED: Svelte + SvelteKit.*** Smallest footprint, fastest
   iteration, native reactivity.

4. **Backwards compat cutover.** Auto-spawn the daemon from
   `--terminal` (transparent) or require explicit `--daemon &` first
   (manual)?
   ***DECIDED: auto-spawn.*** Zero friction; falls back to
   `--standalone` if the auto-spawn fails or the user explicitly
   opts out.

5. **Multi-attach semantics.** Multi-controller (any attached client
   can send inputs, all see events) or single-controller (first
   attacher owns inputs, others are read-only observers)?
   ***DECIDED: multi-controller.*** Any attached client can send
   inputs; all attached clients see all events. Treat it like
   `screen -x`. Single human in the loop, so input-collision risk is
   minimal and recoverable; the lock-out cost of single-controller
   would hurt every day.

6. **Daemon process management.** Auto-spawn only, systemd unit only,
   or support both?
   ***DECIDED: both.*** Auto-spawn for development and casual use;
   systemd unit at `deploy/kaid.service` for the homedevbox so the
   daemon survives reboots.

7. **Persistence boundary.** Should the client be 100% stateless
   (every setting lives in the daemon) or keep client-local
   preferences (theme, window size, keybindings)?
   ***DECIDED: client is stateless for daemon-relevant state.***
   Pure display preferences (terminal theme, window geometry,
   web UI dark/light toggle, keybinding overrides) stay
   client-local. Anything that affects the agent or trading stays
   in the daemon.

8. **Scope / pace.** Phased rollout (6 phases, ships incrementally)
   or big-bang (one branch, one merge, one cutover)?
   ***DECIDED: phased.*** Each phase leaves the current workflows
   working. Task list lives in the project task tracker, one task
   per phase deliverable.

---

## 8. Phased rollout

Each phase produces a working system. Don't start phase N+1 until
phase N is committed, pushed, and smoke-tested.

### Phase 1 — Extract the daemon-runnable core
**Goal:** factor the in-process code so it can run headless.

- Create `daemon/` package at the repo root
- Move the **agent runner**, **sub-agent registry**, **signal
  consumer**, **market data fan-out**, and **persistent stores**
  out of `tui/terminal.py` into `daemon/core.py`
- Define an **event bus** inside the daemon (plain Python
  `asyncio.Queue` per session, or a single bus with topic
  subscriptions — TBD during implementation)
- Introduce a `Session` class: `chat_history`, `input_queue`,
  `agent_runner`, `ui_state`, `event_bus`
- Persist sessions to `workspaces/sessions/{name}.json`
- Wire `terminal.py` to use the `Session` abstraction in-process
  (no WS yet — this phase should land without any behavior change
  from the user's perspective)

**Exit criteria:** `--terminal` still works exactly as today,
backed by a `Session` object living in `daemon/`. All 62 existing
pytest tests pass.

### Phase 2 — Client adapter + WS server
**Goal:** the terminal can connect over WS.

- Add `daemon/server.py` — FastAPI app with WS handlers
- Implement the wire protocol from §3 (attach, input, token,
  tool_start/end, final, status, chart_bar, signal)
- Add `tui/client_adapter.py` — a WS client that mimics the
  in-process `Session` interface so `terminal.py` doesn't care
  which mode it's in
- Add CLI flag: `--terminal --remote ws://localhost:PORT`
- Daemon binary: `python -m src.main --daemon`

**Exit criteria:** in one window run `--daemon`, in another run
`--terminal --remote …`, everything works. In-process mode still
works via `--standalone`.

### Phase 3 — Multi-session support
**Goal:** `--session NAME` works, sessions persist.

- Sessions are created on first attach
- Session list is persisted (daemon writes `workspaces/sessions/index.json`)
- REST: `GET /api/sessions`
- `/sessions` slash command in the terminal lists sessions and
  lets you switch
- Kill a session via `/session kill NAME` or REST DELETE

**Exit criteria:** open two terminals at once on two sessions;
both work independently; close both; reopen one; chat history and
UI state are restored.

### Phase 4 — Daemon becomes the default
**Goal:** the old in-process mode becomes the opt-out, not the default.

- `--terminal` (no flags) auto-spawns the daemon if not running
- `--standalone` is the escape hatch
- Add a systemd unit file at `deploy/kaid.service`
- Add a `bin/kaictl` wrapper: `kaictl start | stop | status | logs`
- Document the CLI in the README

**Exit criteria:** fresh clone → `--terminal` → daemon auto-spawns,
client attaches, chat works. A reboot + `kaictl start` also works.

### Phase 5 — Scheduler
**Goal:** the agent can schedule prompts at a time or on an event.

- Add `daemon/scheduler.py` with the `Scheduler` class wrapping
  APScheduler + croniter
- Persist jobs at `workspaces/scheduler/jobs.json` with
  load-then-merge atomic writes
- Hook the scheduler into the daemon's internal event bus for
  event triggers (signals, market data, balance changes)
- JSON-Schema validator for the structured filter object
- Add the new agent tools (`schedule_at`, `schedule_recurring`,
  `schedule_when`, `list_scheduled_jobs`, `cancel_scheduled_job`,
  `pause_scheduled_job`, `resume_scheduled_job`)
- Add the new wire protocol envelopes (`scheduled_job_*`)
- Add `/schedule` slash commands
- Catch-up window (5 min default) for missed triggers on boot
- Per-session active-job soft cap + tool budget per run +
  self-scheduling loop detection

**Exit criteria:** Schedule a job 1 minute from now via the agent
("remind me in 1 minute to check BTC"); the trigger fires, the
result lands in chat with the `[scheduled job: ...]` marker.
Schedule an event-based job ("if any signal scanner publishes a
score above 0.9, summarize it for me"); publish a fake high-score
signal; watch it fire. Restart the daemon while a near-future
trigger is pending; confirm the catch-up window re-fires it within
5 minutes. `/schedule list` shows active jobs across restarts.

### Phase 6 — Web UI
**Goal:** browser client reaches feature parity with the terminal.

- `web/` directory at the repo root (SvelteKit project)
- FastAPI mounts the built web assets under `/`
- Svelte panels: watchlist, positions, chart, chat, alerts, nats,
  status bar, input
- TradingView Lightweight Charts for the chart panel
- Markdown rendering for chat
- Slash commands as a command palette (Ctrl+K)
- Keyboard shortcuts mirror the terminal where it makes sense

**Exit criteria:** on a phone in incognito, navigate to the web
UI, enter the bearer token, attach to an existing session, type
a prompt, see it stream back. Chart updates in real time.

### Phase 7 — Polish + auth + docs
**Goal:** ship-quality.

- Bearer-token auth on both WS and REST
- Token rotation: `kaictl token rotate`
- Health + metrics: `/api/health` returns uptime, session count,
  memory, agent queue depth
- README overhaul: daemon vs standalone, web UI setup, systemd,
  remote usage
- Regression test pass: all 62 tests + new daemon + web-UI smoke
  tests

**Exit criteria:** clean README walkthrough, fresh machine, working
web UI on LAN with token auth. Daemon survives a reboot via systemd.

---

## 9. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Protocol churn during Phase 2 invalidates the web UI work in Phase 5 | Medium | High | Freeze the protocol at the end of Phase 2; any additions are new message types, never breaking changes to existing ones |
| Daemon crash kills every attached session at once | Low | High | `--standalone` stays available; auto-restart via systemd; persist session state on every input so a restart loses ≤1 turn |
| Race conditions between multi-attached clients sending conflicting inputs | Medium | Medium | Per-session input is a single `asyncio.Queue`; first input in wins; the other client sees it in its `token` stream too so the UI stays consistent |
| Per-session sub-agent pools multiply RAM and model-client connections as sessions grow | Medium | Medium | Templates are shared; only conversation buffers and memory are duplicated. Underlying HTTP clients to local LLM / kai-api are pooled globally so sockets don't multiply with sessions |
| Per-session memory grows unbounded over weeks of use | Medium | Medium | Memory store enforces a per-session size cap with LRU eviction; user can `/memory compact` from a slash command |
| Self-scheduling loop: agent creates a job whose triggered run creates more jobs | Medium | High | Loop detection in the schedule_* tools — same session + similar params requires user confirmation; per-session active-job soft cap; `/schedule pause all` kill switch |
| Daemon restart loses near-future scheduled triggers | Medium | Medium | 5-minute catch-up window on boot re-fires recent missed triggers; older misses are logged + skipped to avoid wake-up flood |
| Triggered job runs while user is mid-conversation in the same session | Medium | Low | `concurrency: skip` default for new jobs; explicit `queue` mode is opt-in. Either way the chat history clearly marks scheduler-sourced turns |
| Web UI maintenance burden competes with agent work | Medium | Low | Keep the web UI small — same protocol, ~5 components, TradingView handles the hardest piece |
| Backwards compat breaks existing workflows (`--chat`, `--once`, etc.) | Medium | Medium | All sub-commands stay in `--standalone` mode until explicitly ported; document which are daemon-native and which are not |
| Port conflicts on shared dev machines | Low | Low | Config default + env var override + fail-loud if bind fails |
| State sync drift between daemon's in-memory state and persisted JSON | Medium | Medium | Single-writer per session; every input triggers a persist on the session boundary; use load-then-merge for `state.json` |
| vLLM streaming token timing differs between in-process and WS clients, confuses users | Low | Low | Token streaming goes straight from the agent runner to the per-session bus; both clients see the same delta stream |

---

## 10. What does NOT change

Explicitly out of scope for this migration:

- **Agent runtime.** `AgentRunner`, the LLM endpoint chain,
  tool dispatch, memory store, NATS internal bus — all stay as-is.
  The daemon is a thin shell around the existing core.
- **`kai-api` cloud gateway.** The remote endpoint at
  `agent-k.ai` is untouched. We're re-wrapping the local TUI, not
  the cloud API.
- **`agent-config.json` schema.** Same file, same format, loaded
  once at daemon boot instead of once at terminal boot.
- **Sub-agents.** Same registry, same prompts, same tool lists.
  They move from "owned by the Textual app" to "owned by the
  daemon".
- **Signal consumer + market data + balance change events.** The
  existing fan-out streams stay as-is. The scheduler subscribes to
  the same internal event bus that sessions already use; it's a
  new consumer of existing data, not a new data source.
- **Slash commands.** `/chart`, `/status`, `/autotrade`, `/debug`,
  `/react`, `/login`, `/model`, `/sessions` — all keep working.
  Some move from the terminal to the daemon (`/autotrade` is
  per-session state, `/status` queries the daemon), but the syntax
  doesn't change.
- **Existing tests.** All 62 pytest tests must stay green through
  every phase. New daemon + web tests land alongside new code.
- **File locations the user already knows.** `workspaces/`,
  `agent-config.json`, `state.json`, chat history JSONs — same
  paths. `workspaces/sessions/` is additive.

---

## 11. Next action

Answer the 8 open questions in §6, then start Phase 1. Recommended
defaults are listed; reply with overrides or `"defaults"` and I'll
begin the extraction.

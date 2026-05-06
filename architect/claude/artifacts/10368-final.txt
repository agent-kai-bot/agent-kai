Task 10368 — ARCH: heartbeat tick that feeds into the main agent loop

Status: architecture complete
Author: Architect
Date: 2026-05-06

Context
- KAI now runs primarily as an always-on daemon (`python main.py --daemon`) that owns sessions, scheduler jobs, NATS integration, taskboard dispatch, and shared terminal/web clients.
- `daemon/protocol.py` already defines a client `HeartbeatEnvelope`, but `daemon/server.py` currently ignores it in the WebSocket receive loop. That heartbeat is transport/client liveness only.
- The main agent currently runs only when `DaemonServer.run_input()` calls `Session.stream_agent_events(text, source=...)` for a user input, slash-command forwarding, scheduler job, or taskboard prompt.
- `Session.stream_agent_events()` already contains the autonomous continuation loop (`AUTO_STATE: continue`) but it is still scoped to one initiated run. There is no daemon-owned idle tick that can wake the agent loop.
- The daemon already has useful infrastructure for this feature:
  - per-session `ManagedSession.input_lock` to serialize agent runs,
  - `SessionEventBus` for UI/status events,
  - `DaemonEventBus` for daemon-scoped scheduler event jobs,
  - persisted session UI/activity state,
  - scheduler event jobs for explicit event-driven prompts.

Problem statement
We need a heartbeat tick that can feed into the main agent loop without confusing transport heartbeats with agent work, without burning LLM/tool budget on every liveness ping, and without racing user/scheduler/taskboard runs.

Recommendation summary
Add a daemon-owned heartbeat service that emits structured internal heartbeat ticks. Each live session receives coalesced ticks in a small session tick mailbox. The main agent loop consumes ticks only when policy allows: by default ticks update liveness/status and scheduler/event subscribers; optionally, configured sessions may turn a coalesced idle tick into a synthetic low-priority `run_input(..., source="heartbeat")` prompt.

Chosen design
1. Keep client WebSocket `HeartbeatEnvelope` transport-only.
   - Rename semantics in docs/tests to “client_ping” if desired later, but do not use arbitrary client pings as agent-loop triggers.
   - On receipt, update per-connection/session last-seen metadata and optionally emit a WebSocket heartbeat ack/status in a future wire-protocol extension.

2. Introduce a daemon-owned `HeartbeatService`.
   - Starts in `DaemonServer.startup()` after scheduler startup.
   - Stops in `DaemonServer.shutdown()` before bus disconnect.
   - Emits monotonic, UTC-stamped ticks at a configurable interval.
   - Publishes ticks to:
     - `DaemonEventBus` channel `heartbeat` for event scheduler jobs,
     - every live session via `Session.publish_event("heartbeat.tick", payload)`,
     - each session’s tick mailbox for main-loop consumption.

3. Add a session-local tick mailbox, not a chat message queue.
   - `SessionHeartbeatState` stores the latest unconsumed tick, last tick timestamp, counters, and dropped/coalesced count.
   - Ticks coalesce: if the agent is busy, keep only the newest tick and increment `coalesced_count`.
   - This avoids unbounded queue growth and prevents backlog storms after long-running agent/tool calls.

4. Add a low-priority heartbeat run path in `DaemonServer`.
   - A daemon task may attempt to run one heartbeat prompt per session only when:
     - session runtime is attached,
     - `managed.input_lock` is not locked,
     - no user input is queued,
     - heartbeat agent wakeups are enabled for that session/config,
     - minimum interval since last heartbeat agent run has elapsed,
     - daily/hourly heartbeat run budget has not been exceeded.
   - If any condition fails, the tick remains coalesced or is marked skipped; it must not block user work.

5. Extend `Session.stream_agent_events()` to make heartbeat context explicit.
   - Accept `source="heartbeat"` and optional `heartbeat_tick` metadata.
   - For heartbeat runs, prepend a lightweight `SystemMessage` marker such as `[heartbeat tick: 2026-05-06T16:30:00Z seq=42]` or pass metadata through a run context field.
   - Use a bounded tool budget by default (recommend 1–3 iterations/tool calls) and a distinct prompt template that tells the agent to either do a tiny maintenance action or reply with no-op.

6. Make heartbeat behavior policy-driven and off by default for LLM wakeups.
   - Always-on status/event heartbeat is safe by default.
   - Agent-consuming heartbeat wakeups should require config, slash command, or session setting because each wakeup can spend tokens/tools and may create autonomous behavior.

Architecture

```mermaid
flowchart TD
    A[DaemonServer.startup] --> B[HeartbeatService.start]
    B --> C{interval elapsed}
    C --> D[Build HeartbeatTick]
    D --> E[DaemonEventBus.publish('heartbeat')]
    D --> F[For each live ManagedSession]
    F --> G[Session.record_heartbeat_tick]
    G --> H[SessionEventBus: heartbeat.tick]
    G --> I{agent wakeup enabled?}
    I -->|no| C
    I -->|yes| J{input_lock free and no queued user input?}
    J -->|no| K[coalesce/skip]
    J -->|yes| L[run_input(source='heartbeat', tool_budget=small)]
    L --> M[Session.stream_agent_events]
    M --> N[AgentRunner.run]
    N --> O[heartbeat result + metrics]
```

Recommended components

1. `daemon.heartbeat.HeartbeatService`
- Owns the periodic async task.
- Has no direct LLM dependency.
- Receives callbacks/interfaces instead of importing server internals where possible.

Suggested constructor:
```python
class HeartbeatService:
    def __init__(
        self,
        *,
        interval_seconds: float,
        tick_callback: Callable[[HeartbeatTick], Awaitable[None]],
        clock: Callable[[], datetime] = utc_now,
        enabled: bool = True,
    ): ...
```

2. `HeartbeatTick` data model
```json
{
  "type": "heartbeat.tick",
  "seq": 42,
  "emitted_at": "2026-05-06T16:30:00Z",
  "monotonic_seconds": 123456.789,
  "interval_seconds": 60.0,
  "source": "daemon",
  "reason": "periodic"
}
```

3. `SessionHeartbeatState`
```json
{
  "last_tick_seq": 42,
  "last_tick_at": "2026-05-06T16:30:00Z",
  "pending_tick": { "seq": 42, "emitted_at": "..." },
  "coalesced_count": 3,
  "last_agent_run_at": "2026-05-06T16:25:00Z",
  "agent_runs": 5,
  "skipped_busy": 12,
  "skipped_disabled": 120
}
```

4. Session/runtime APIs
- `Session.record_heartbeat_tick(tick: HeartbeatTick) -> dict`
  - updates heartbeat state,
  - publishes `heartbeat.tick`,
  - returns snapshot for metrics/tests.
- `Session.consume_pending_heartbeat_tick() -> HeartbeatTick | None`
  - returns and clears latest tick.
- `DaemonServer._handle_heartbeat_tick(tick: HeartbeatTick) -> None`
  - fans out daemon/session events and optionally schedules idle heartbeat agent runs.
- `DaemonServer._maybe_run_session_heartbeat(managed, tick) -> None`
  - enforces gating and calls `run_input()`.

5. Configuration contract
Add a small config section, preferably in `agent-config.json` with env overrides:
```json
{
  "daemon": {
    "heartbeat": {
      "enabled": true,
      "interval_seconds": 60,
      "publish_session_events": true,
      "agent_wakeup_enabled": false,
      "agent_wakeup_min_interval_seconds": 300,
      "agent_wakeup_tool_budget": 2,
      "agent_wakeup_prompt": "Heartbeat tick. Check for lightweight maintenance only. If no action is needed, reply exactly: no-op."
    }
  }
}
```

Environment override examples:
- `KAI_HEARTBEAT_ENABLED=0|1`
- `KAI_HEARTBEAT_INTERVAL_SECONDS=60`
- `KAI_HEARTBEAT_AGENT_WAKEUP_ENABLED=0|1`
- `KAI_HEARTBEAT_AGENT_WAKEUP_MIN_INTERVAL_SECONDS=300`
- `KAI_HEARTBEAT_AGENT_WAKEUP_TOOL_BUDGET=2`

Data/event contracts

Daemon event bus channel: `heartbeat`
```json
{
  "seq": 42,
  "emitted_at": "2026-05-06T16:30:00Z",
  "interval_seconds": 60.0,
  "source": "daemon",
  "reason": "periodic"
}
```

Session event topic: `heartbeat.tick`
```json
{
  "seq": 42,
  "emitted_at": "2026-05-06T16:30:00Z",
  "pending": true,
  "coalesced_count": 1,
  "agent_wakeup_enabled": false
}
```

Optional WebSocket server envelope, phase 2 if UI needs visibility:
```json
{
  "type": "heartbeat_tick",
  "seq": 42,
  "emitted_at": "2026-05-06T16:30:00Z"
}
```
This can be omitted initially if `StatusEnvelope`/metrics are sufficient.

Heartbeat-sourced agent run
- `run_input(..., source="heartbeat", tool_budget=configured_small_budget)`
- Prompt should be synthetic and bounded. Recommended default:
```text
[heartbeat tick seq=42 at 2026-05-06T16:30:00Z]
This is a daemon heartbeat tick, not a user request. Perform only lightweight maintenance that is already configured or obviously due. Do not place trades or make external side effects unless an explicit policy/tool requires it. If no action is needed, respond exactly: no-op
```

Rejected alternatives

1. Treat client WebSocket `HeartbeatEnvelope` as the agent tick.
   - Rejected because clients are untrusted/variable, multiple clients can multiply ticks, and transport liveness should not spend LLM budget.

2. Implement heartbeat as a hidden recurring scheduler job per session.
   - Rejected as the primary primitive because scheduler jobs are user-visible persisted jobs with prompt semantics. Heartbeat is daemon infrastructure and should not pollute user job lists. Scheduler event jobs can still subscribe to `heartbeat` for explicit workflows.

3. Push every heartbeat tick directly into `Session.input_queue`.
   - Rejected because it can starve user inputs, create unbounded backlog, and blur UI queue semantics.

4. Run the LLM on every heartbeat interval.
   - Rejected due to token cost, runaway side effects, and operational noise. Heartbeat must be coalesced, budgeted, and policy-gated.

5. Put the periodic loop inside `AgentRunner`.
   - Rejected because `AgentRunner` should remain a single-run executor wrapper. The daemon/session layer owns lifecycle, scheduling, concurrency, and policy.

Implementation phases

Phase 1 — Internal heartbeat event, no LLM wakeup
1. Add `daemon/heartbeat.py` with `HeartbeatTick` and `HeartbeatService`.
2. Add config/env loading defaults.
3. Start/stop service in `DaemonServer.startup()` / `shutdown()`.
4. Fan out each tick to `DaemonEventBus.publish("heartbeat", ...)` and `Session.publish_event("heartbeat.tick", ...)`.
5. Add metrics fields to `/api/health` or `/api/metrics`: heartbeat enabled, interval, last tick, tick count.
6. Tests: service emits ticks, shutdown cancels task, event bus receives heartbeat, no agent run occurs.

Phase 2 — Session mailbox and coalescing
1. Add `SessionHeartbeatState` to `daemon/core.py`.
2. Implement `record_heartbeat_tick()` and `consume_pending_heartbeat_tick()`.
3. Persist only lightweight counters/last timestamp if useful; do not persist every tick.
4. Map `heartbeat.tick` to a status/optional WebSocket envelope if UI should display it.
5. Tests: repeated ticks coalesce while busy; newest tick wins; no unbounded queue growth.

Phase 3 — Optional idle heartbeat agent wakeup
1. Add daemon gating function `_maybe_run_session_heartbeat()`.
2. Default `agent_wakeup_enabled=false`.
3. If enabled, create an async task per eligible session that calls `run_input(source="heartbeat", tool_budget=small)` only when input lock is free and queue is empty.
4. Update `Session.stream_agent_events()` to handle `source="heartbeat"` and add safe context marker.
5. Tests: busy sessions skip/coalesce, idle sessions run once, user input wins over heartbeat, tool budget override is applied.

Phase 4 — Operator controls and rollout
1. Add slash/API controls if needed: `/heartbeat status`, `/heartbeat on|off`, `/heartbeat wakeups on|off`.
2. Add UI/metrics visibility.
3. Enable status/event heartbeat by default; keep agent wakeups disabled until soak tests pass.

Failure modes and handling

1. Heartbeat task crashes
   - Log exception, increment failure metric, and either restart with backoff or mark heartbeat unhealthy in `/api/health`.

2. Agent is busy for many intervals
   - Coalesce ticks; do not enqueue all missed ticks. Record `coalesced_count`/`skipped_busy`.

3. User input arrives while heartbeat wakeup is being considered
   - Recheck `input_lock` and `input_queue` immediately before `run_input()`. User input has priority.

4. Long heartbeat-triggered run
   - Apply small `tool_budget`, normal cancellation/interrupt support, and minimum interval between heartbeat runs.

5. Multiple clients connected
   - No multiplication of agent ticks because only the daemon-owned service emits agent heartbeat ticks.

6. Daemon suspended or event loop blocked
   - Next tick should include actual emitted timestamp; do not attempt to replay every missed interval unless a future explicit catch-up policy is added.

7. Token/cost runaway
   - Agent wakeups off by default, min interval, per-session budget, metrics, and no LLM call for pure status heartbeat.

8. Side-effect risk
   - Heartbeat prompt must state that it is not a user request and must prohibit trades/external side effects unless separately authorized. Tool policy still applies.

Security and safety notes
- Client heartbeat remains transport liveness and must not become a remote trigger for hidden agent work.
- Heartbeat payloads must not contain secrets or raw Authorization headers.
- Heartbeat wakeup prompts must be deterministic infrastructure text, not client-provided text.
- If heartbeat events are exposed over WebSocket, they should be low-detail operational metadata only.

Testing plan

Unit tests
- `HeartbeatService` emits a monotonic sequence at configured interval using a fake clock/sleeper.
- `HeartbeatService.shutdown()` cancels cleanly without leaked tasks.
- Config parsing rejects zero/negative intervals and falls back safely.
- `Session.record_heartbeat_tick()` updates latest tick and coalesced counters.
- `Session.consume_pending_heartbeat_tick()` clears pending tick.
- Scheduler event jobs can match `channel="heartbeat"` and structured filters.

Daemon/server tests
- Startup creates heartbeat service when enabled and not when disabled.
- Shutdown stops it.
- `_handle_heartbeat_tick()` publishes daemon event and session event.
- With `agent_wakeup_enabled=false`, `run_input()` is never called.
- With wakeups enabled and session idle, `run_input(source="heartbeat")` is called once with configured tool budget.
- With `input_lock.locked()` or non-empty `input_queue`, heartbeat run is skipped/coalesced.
- User input submitted near the same time as a heartbeat wins and is not reordered behind heartbeat work.

Integration tests
- Run daemon with short heartbeat interval, connect terminal/web client, verify status/events without LLM calls.
- Enable heartbeat wakeups in a test session with fake AgentRunner; verify one synthetic run and no backlog during a simulated long run.
- Verify `/api/health` or `/api/metrics` reports tick count/last tick.

Rollout guardrails
- Default: heartbeat service enabled for status/events only; agent wakeups disabled.
- Minimum allowed heartbeat interval: recommend 10 seconds in code; operational default 60 seconds.
- Minimum heartbeat agent wakeup interval: recommend 300 seconds.
- Heartbeat agent tool budget: default 2, max configurable but capped.
- Emit logs with tick sequence and skip reason at debug/info, not every tick at warning.
- Add metrics before enabling wakeups in production.
- Provide one environment kill switch: `KAI_HEARTBEAT_ENABLED=0`.

Recommended implementation sequence for developer agents
1. Implement `daemon/heartbeat.py` with tests first.
2. Add configuration helpers and env overrides.
3. Wire `HeartbeatService` into `DaemonServer.startup()`/`shutdown()` and publish daemon/session events.
4. Add session heartbeat state/coalescing APIs.
5. Add metrics/health visibility.
6. Only after phases 1–2 pass, add optional heartbeat agent wakeup path behind disabled-by-default config.
7. Add docs describing the distinction between client heartbeat and daemon heartbeat tick.

Acceptance criteria
- The daemon emits internal heartbeat ticks on a configurable interval while running.
- Client WebSocket heartbeat pings do not trigger agent work.
- Heartbeat ticks are visible to daemon event subscribers and live sessions.
- Ticks coalesce while a session is busy; no unbounded queue or chat-history growth occurs.
- User, scheduler, and taskboard inputs retain priority over heartbeat wakeups.
- Optional heartbeat-driven agent runs are disabled by default and, when enabled, use `source="heartbeat"`, a bounded tool budget, a minimum interval, and a safe synthetic prompt.
- Tests cover service lifecycle, event fanout, coalescing, wakeup gating, and shutdown.
- Operational metrics expose enabled state, last tick time, tick count, skipped/coalesced counts, and last error if any.

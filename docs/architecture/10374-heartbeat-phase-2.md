Task 10374 — ARCH: heartbeat phase 2 — main-agent subscribes + injects prompt on tick

Status: architecture complete
Author: Architect (orchestrator-written; original architect spawn lost its session worktree before committing — see `Process notes` below)
Date: 2026-05-06

Context
- Phase 1 (#10370, merged) added a daemon-owned `HeartbeatService` that emits monotonic UTC ticks at a configurable interval and publishes them to (a) the `DaemonEventBus` `heartbeat` channel and (b) every live session's event stream as `heartbeat.tick`. Phase 1 is intentionally tick-only: `_handle_heartbeat_tick` in `daemon/server.py:655` carries the comment "Fan out one daemon-owned heartbeat tick without waking the agent." Nothing today consumes those ticks to drive agent behavior.
- Phase 1 also defined `SessionHeartbeatState` per `10368-heartbeat-tick.md` — the per-session tick mailbox the main loop is supposed to consume. That mailbox exists but no consumer is wired.
- Dan's stated goal (2026-05-06 conversation): "ability to inject messages into the main agent kai loop from a heartbeat ... runs every 30mins and is configurable." Today's default is 60 s with no injection at all.

Problem statement
Phase 1 ships a clock that nobody reads. Phase 2 must wire the main-agent loop to that clock so the agent self-prompts on cadence — without racing user/scheduler/taskboard runs, without burning LLM/tool budget on every tick, and without leaking heartbeat ticks into spawn sessions (taskboard-fire CR/SA/QA/dev) that should not be self-prompting.

Recommendation summary
Add a per-session `HeartbeatSubscriber` that drains the existing `SessionHeartbeatState` mailbox and, when policy allows, calls `Session.run_input(prompt, source="heartbeat")` with a rendered prompt template. Default the daemon cadence to 30 min. Default subscription on for the kai main agent and off for taskboard-fire spawn sessions. Cap injected runs per hour to bound cost. Make the prompt template git-tracked at `prompts/heartbeat/main.md.tmpl`.

Chosen design

1. Configuration
   - Extend `HeartbeatConfig` in `daemon/heartbeat.py`:
     - `interval_seconds: float = 1800.0` (was 60.0). Existing env override `KAI_HEARTBEAT_INTERVAL_SECONDS` stays.
     - `prompt_template_path: str = "prompts/heartbeat/main.md.tmpl"` (relative to repo root). Env override: `KAI_HEARTBEAT_PROMPT_TEMPLATE_PATH`.
     - `max_injected_turns_per_hour: int = 4` (matches 30 min cadence with one safety skip headroom). Env: `KAI_HEARTBEAT_MAX_INJECTED_TURNS_PER_HOUR`.
   - All values clamped at config-load time. Floor on `interval_seconds` stays 0.1 s; floor on `max_injected_turns_per_hour` is 0 (0 ⇒ ticks fan out but injection is disabled, equivalent to phase 1 today).
   - Top-level config layout in `agent-config.json`:
     ```json
     {
       "daemon": {
         "heartbeat": {
           "enabled": true,
           "interval_seconds": 1800,
           "publish_session_events": true,
           "prompt_template_path": "prompts/heartbeat/main.md.tmpl",
           "max_injected_turns_per_hour": 4
         }
       }
     }
     ```

2. Prompt template
   - New file `prompts/heartbeat/main.md.tmpl` (git-tracked, NOT in Vault — there are no secrets in this prompt).
   - Variables passed to the renderer: `tick_seq`, `tick_emitted_at`, `session_name`, `agent_name`, `last_active_at`, `idle_seconds`.
   - Default body should give the agent enough orientation to self-route ("you are Kai; the daemon just fired a 30 min heartbeat at $tick_emitted_at; you've been idle $idle_seconds seconds; resume the highest-priority work in flight or, if there is none, run a system health check"). The literal default text is part of the impl ticket, not this spec.

3. Subscription model
   - New per-session field `heartbeat_subscribed: bool` on `Session`. Default value comes from session source:
     - `source == "kai-main"` (the always-on top-level Kai session): `true`.
     - `source.startswith("taskboard-")` (spawn sessions for CR/SA/QA/dev): `false`.
     - Other / future sources: `false` until explicitly opted in.
   - `Session` exposes `set_heartbeat_subscribed(bool, reason: str)` for runtime overrides; that call publishes a `session.heartbeat_subscription_changed` event so the UI/health endpoint can reflect it.
   - Subscription is cheap; it adds the session to `HeartbeatService._subscribers` so phase-1 fanout is the same loop that delivers to the mailbox.

4. The injection step
   - Each subscribed session gets a `HeartbeatSubscriber` task that awaits new ticks from its `SessionHeartbeatState` mailbox.
   - On tick:
     1. Acquire `Session.input_lock` non-blocking. If already held (user/scheduler/taskboard run in flight, or another auto-mode iteration): drop the tick. Heartbeat is a fresh signal, never queued.
     2. Check rate limit: if injections in the last 3600 s ≥ `max_injected_turns_per_hour`: drop and emit `auto.heartbeat_rate_limited`.
     3. Check auto-mode state: if `Session.auto_mode` is on AND `auto_iterations_remaining > 0`: drop (the agent is already self-driving).
     4. Render `prompt_template_path` with the tick variables. Get back a single string.
     5. Append a `HumanMessage` with that string to `Session.chat_history`.
     6. Call `Session.run_input(rendered_prompt, source="heartbeat", auto_iterations=1)` — exactly **one** auto iteration. Heartbeats wake the agent; they don't start a long auto-mode session. If the agent's response sets `AUTO_STATE: continue`, the existing auto-evaluator path handles the rest within its own quota; phase 2 doesn't extend that quota.
     7. Release `input_lock` after the iteration completes. Increment a per-session injection counter with rolling 1-hour window.
   - All steps inside a `try/except` that increments `Session.heartbeat_failure_count` and emits `auto.heartbeat_error` with the exception class name — never the message (sensitive content protection).

5. Telemetry & observability
   - Per-tick events:
     - `auto.heartbeat_tick_received` (subscriber-side, before policy checks): `{session_name, tick_seq, idle_seconds}`.
     - `auto.heartbeat_dropped`: `{session_name, tick_seq, reason: "input_locked"|"rate_limited"|"auto_mode_active"|"render_failed"}`.
     - `auto.heartbeat_injected`: `{session_name, tick_seq, template_name, chars_injected, prompt_id}`.
     - `auto.heartbeat_error`: `{session_name, tick_seq, exception_kind}`.
   - `/api/health` heartbeat block (already exists in `daemon/server.py:770`) gets two new fields:
     - `subscribers_count: int` — current subscribed sessions.
     - `injections_last_hour_total: int` — sum across sessions.

6. Backward compatibility
   - Phase 1 ticks-only behavior is preserved for non-subscribed sessions. They still receive `heartbeat.tick` events on their event bus; nothing else changes.
   - Default for `prompts/heartbeat/main.md.tmpl` ships with the impl PR. Operators can override path/contents per env or config.
   - If `prompts/heartbeat/main.md.tmpl` is missing at startup, the service logs an error, sets `subscribers_count = 0`, and falls back to phase-1 ticks-only behavior. **No silent partial-functionality.**

Test plan
- Unit (`tests/test_heartbeat_subscriber.py`):
  - Template loader handles missing file, malformed Jinja, valid render.
  - Suppression rules: input_lock held → drop; auto_mode + iterations remaining → drop; rate limit hit → drop.
  - Counter rolling-window correctness across mocked clocks.
- Integration (`tests/test_heartbeat_phase2_integration.py`):
  - Start daemon with `interval_seconds=0.5`, `max_injected_turns_per_hour=10`, subscribe a fake `Session` whose `run_input` records calls.
  - Wait for two ticks; assert `run_input` called exactly twice with `source="heartbeat"`.
  - Confirm `chat_history` got the rendered prompt as a `HumanMessage`.
- E2E (`tests/test_heartbeat_phase2_e2e.py`):
  - Real daemon, real Kai session, `interval_seconds=2.0`, prompt template that asks the agent to emit a single sentinel string. Assert sentinel appears in agent response within one tick boundary.

Failure modes & rejected alternatives
- **Queue ticks instead of dropping**: rejected. Heartbeat is a freshness signal, not a backlog; a 4-hour-old tick is meaningless.
- **Inject directly without going through `run_input`**: rejected. Bypasses auto-evaluator, tool gates, and event publication; produces a divergent execution path.
- **Use a separate "heartbeat agent" with its own LLM call**: rejected for phase 2. The injection target IS the main agent. A separate critic agent is what `#10375 auto-loop-brain` is for.
- **Couple cadence to user activity** (e.g. fire after N minutes of UI silence): rejected for phase 2 — cadence is daemon-owned and global. Per-session adaptive cadence is a future ticket if needed.

Rollout guardrails
- Ship with `max_injected_turns_per_hour: 0` as the default for the FIRST production cutover. Operator flips it to 4 once telemetry confirms the injection path is healthy.
- Add a `KAI_HEARTBEAT_INJECTION_KILL_SWITCH=1` env var that forces `max_injected_turns_per_hour=0` regardless of config. Quick break-glass.

Implementation sequence
1. Land config schema + 30 min default + tests for `load_heartbeat_config` (small, low-risk).
2. Add `prompts/heartbeat/main.md.tmpl` + Jinja renderer integration + unit tests.
3. Add `Session.heartbeat_subscribed` + `set_heartbeat_subscribed` + integration tests.
4. Add `HeartbeatSubscriber` worker + the suppression/rate-limit logic + `auto.heartbeat_*` events.
5. Wire subscriber into `HeartbeatService` startup. E2E test.
6. Ship with kill-switch on, flip kill-switch off after one hour of green telemetry.

Acceptance criteria mapping (against the parent ticket #10374)
- AC1 (default cadence 30 min): step 1.
- AC2 (configurable prompt template): step 2.
- AC3 (main agent subscribes; tick injects HumanMessage; single auto iteration): steps 3 + 4.
- AC4 (suppression rules — drop never queue): step 4.
- AC5 (per-session opt-out; default true for kai-main, false for spawn): step 3.
- AC6 (telemetry events + `/api/health` subscribers_count): step 4 + 5.
- AC7 (unit / integration / e2e tests): all steps.

Risks
- Token cost on Kai-main if the prompt template is verbose. Mitigation: keep the default tight; rate-limit cap is the safety net.
- Re-entrancy from a heartbeat-injected turn invoking auto-mode that runs longer than 30 min and gets clipped by the next tick. Mitigation: the rate limit + the `auto_mode_active` drop rule together prevent this.
- Worktree-cleanup pattern (the bug that lost the original architect spawn's artifact) is not in this spec's scope but should not be allowed to lose dev/CR/SA/QA artifacts in the impl phase. File a separate ticket if the impl dev sees the same loss.

Process notes
- The original architect spawn for #10374 ran but its session worktree was reaped before any commit landed in the consolidated branch. Only a taskboard summary comment was preserved. This spec was written by the orchestrator from the in-conversation design with Dan plus the architect's summary, so it could be re-used for impl. The artifact-loss issue is a separate process bug worth filing if it recurs.

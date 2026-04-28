# Agent Health Monitoring and Capacity-Aware Routing

Phase 0 discovery and design for Agent KAI epic #10023, task #10176.

Date: 2026-04-28
Branch: `research/kai-agent-health-10176`

## Executive Summary

KAI does not currently track persistent per-agent health. The canonical `origin/main`
tree has no `agents.yaml`; the active registry is `agent-config.json`, loaded through
`config.AGENTS`. Runtime sub-agent presence exists in memory through
`SubAgentManager.agents` and NATS `system.registry` online/offline announcements, but
there is no background probe runner, no SQLite health table, no cooldown state, no
Prometheus exposition, and no health gate before spawn.

`/api/health` is daemon readiness only. `/api/metrics` is a JSON diagnostic payload
for daemon/runtime state, not Prometheus. Codex OAuth state is managed by
`agent/codex_auth.py`, but it only loads and refreshes credentials; it does not probe
capacity or record rate limits. Codex or Claude capacity failures today surface as
exceptions, CLI output, or operator observation, then the local fallback chain may try
another endpoint for that one request. Nothing marks the backend unhealthy before the
next dispatch.

The design below introduces a health-aware executor registry, `agent/health_monitor.py`,
an `agents_health` SQLite table, `/api/agents/health` routes, capacity feedback from
the taskboard gateway and execution path, dispatcher pre-flight checks, operator
wrappers, and Prometheus metrics.

## Discovery Inventory

### Existing Health Surface

| Question | Finding |
| --- | --- |
| Is there a per-agent registry today? | Yes, but it is a config registry, not a health registry. `config.py` loads `agent-config.json` into `AGENTS` at lines 82-86. Runtime sub-agents are tracked only in memory by `SubAgentManager.agents` in `agent/sub_agents.py:310-326`. |
| Does `agents.yaml` exist or include health/capacity fields? | No. `agents.yaml` is absent on `origin/main`. No health/capacity fields exist in `agent-config.json` agent entries. |
| Is there a probe runner? | No. Searches for health/capacity/probe found only daemon readiness, daemon control polling, market-data probes, and docs. No asyncio/threaded per-agent probe loop exists. |
| Does `/api/health` surface daemon state or per-agent state? | Daemon state only. `DaemonServer.health_snapshot()` returns status, daemon agent name, NATS bus connection, session count, uptime, RSS, input queue depth, and scheduler job count (`daemon/server.py:593-605`). Route is `GET /api/health` (`daemon/server.py:1760-1763`). |
| Is there any Codex or Claude capacity tracking? | No persistent tracking. Codex OAuth credentials load/refresh in `agent/codex_auth.py`; `ChatCodex` sends requests through the ChatGPT Codex Responses endpoint; `_codex_exec` and `_claude_exec` shell out and return text. Failures become error strings or exceptions, not health state. |
| Does KAI emit metrics? | Yes as JSON at `/api/metrics`, from `DaemonServer.metrics_snapshot()` (`daemon/server.py:549-591`). No Prometheus client or text exposition exists. |
| Does KAI emit NATS presence? | Yes. `NatsBus.connect()` publishes `system.registry` online/offline for the main agent (`nats_bus/bus.py:27-47`). `SubAgent.start()` and `stop()` publish online/offline for sub-agents (`agent/sub_agents.py:144-164`). No NATS health alerts exist. |

### Current API Surface

KAI daemon routes in `daemon/server.py`:

- `GET /api/health`
- `GET /api/metrics`
- `GET /api/sessions`
- `GET /api/models`
- `POST /api/models/{agent_name}`
- Market, portfolio, session, UI state, and websocket routes

Embedded OpenClaw/taskboard gateway routes in `taskboard_gateway/app.py`:

- `GET /api/status`
- `POST /tools/invoke`
- `POST /api/sessions/{session_key:path}/abort`
- `POST /api/cron/wake`

There are no routes under `/api/agents/health`.

### Current Spawn Flow

There is no class named `SessionManager` in the current tree. The equivalent spawn
path is:

1. Main-agent tool `spawn_agent` in `agent/tools.py:661-714`.
2. `SessionSubAgentRegistry.spawn()` in `daemon/core.py:502-517`.
3. `SubAgentManager.spawn()` in `agent/sub_agents.py:317-341`.
4. `SubAgent.__init__()` builds the primary executor and fallback executors from
   `get_agent_config(name)`, then `agent.start()` subscribes on NATS.

The only pre-flight checks are:

- return if the agent name already exists in `self.agents`;
- reject spawning an agent with the same name as the main bus agent.

There is no health/cooldown/capacity check before constructing `SubAgent`.

Taskboard-driven spawns currently flow through `POST /tools/invoke`:

1. `taskboard_gateway.app.tools_invoke()` handles `sessions_spawn`.
2. `resolve_agent_id(args.agentId)` maps external ids to local agent names.
3. A durable run is created.
4. `schedule_run(run)` starts async execution.
5. `execute_run_with_local_session()` attaches a runtime and starts auto mode.

This path also has no health gate. Unknown agent ids fail closed, but known unhealthy
agents are not distinguishable.

Current TUI signal handling has a separate dispatcher:

- `tui/terminal.py:3718-3757` spawns a named sub-agent if missing and sends a NATS
  request.
- `agent/signal_handlers.py:240-270` has in-memory signal cooldowns, but they are
  handler/symbol cooldowns, not agent health cooldowns.

Files named `taskboard_dispatcher` and `forgejo_dispatcher` are not present on
`origin/main`. If those modules are introduced by another branch from epic #10021/#10152,
the Phase 5 health gate should integrate there; until then, the current taskboard
spawn surface is `taskboard_gateway.app`.

### Current `agent-config.json` Agent Structure

`agents.yaml` does not exist today. The actual per-agent structure is
`agent-config.json["agents"]`. Secrets live in endpoint configuration, not in the
agent entries below.

```json
{
  "kai": {
    "description": "KAI \\u2014 primary trading assistant, the user's main interface",
    "endpoint": "codex-cli",
    "fallback_endpoints": [
      {
        "endpoint": "kai-local",
        "model": "qwen35-gptq"
      }
    ],
    "max_iterations": 2000
  },
  "ceo": {
    "description": "Strategic leader \\u2014 sets vision, makes priority decisions, aligns all agents",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "ceo",
    "max_iterations": 2000
  },
  "cto": {
    "description": "Technical leader \\u2014 architecture decisions, engineering standards, tech strategy",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "cto",
    "max_iterations": 2000
  },
  "architect": {
    "description": "System designer \\u2014 blueprints, API contracts, data models, design docs",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "architect",
    "max_iterations": 2000
  },
  "developer": {
    "description": "Builder \\u2014 writes, edits, debugs, and ships code",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "developer",
    "max_iterations": 200
  },
  "qa": {
    "description": "Quality gatekeeper \\u2014 tests, bug reports, code review, coverage",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "qa",
    "max_iterations": 2000
  },
  "qa-agent": {
    "description": "Taskboard QA verifier \\u2014 functional testing, regression checks, evidence capture",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "qa-agent",
    "max_iterations": 2000
  },
  "code-reviewer": {
    "description": "Taskboard Code Reviewer \\u2014 code quality, maintainability, tests, and review verdicts",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "code-reviewer",
    "max_iterations": 2000
  },
  "security-auditor": {
    "description": "Taskboard Security Auditor \\u2014 vulnerability review, secret handling, tenant isolation, and security verdicts",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "security-auditor",
    "max_iterations": 2000
  },
  "deep-research": {
    "description": "Taskboard Deep Research \\u2014 source-grounded investigation, options analysis, and research handoffs",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "deep-research",
    "max_iterations": 2000
  },
  "ux-manager": {
    "description": "UX champion \\u2014 user flows, usability, accessibility, design system",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "ux-manager",
    "max_iterations": 2000
  },
  "project-manager": {
    "description": "Coordinator \\u2014 task tracking, priorities, schedules, cross-agent communication",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "project-manager",
    "max_iterations": 2000
  },
  "seo": {
    "description": "Search optimizer \\u2014 keywords, meta tags, technical SEO, content strategy",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "seo",
    "max_iterations": 2000
  },
  "sales-marketing": {
    "description": "Growth driver \\u2014 messaging, copy, go-to-market, campaigns, lead gen",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "sales-marketing",
    "max_iterations": 2000
  },
  "trader": {
    "description": "Execution specialist \\u2014 places trades, manages positions, order lifecycle",
    "endpoint": "codex-cli",
    "fallback_endpoints": [
      {
        "endpoint": "kai-local",
        "model": "qwen35-gptq"
      }
    ],
    "workspace": "trader",
    "max_iterations": 200
  },
  "analyst": {
    "description": "Technical analysis \\u2014 indicators, patterns, chart reading, trading signals",
    "endpoint": "codex-cli",
    "fallback_endpoints": [
      {
        "endpoint": "kai-local",
        "model": "qwen35-gptq"
      }
    ],
    "workspace": "analyst",
    "max_iterations": 200
  },
  "risk-manager": {
    "description": "Capital guardian \\u2014 position sizing, stop losses, exposure limits, drawdown monitoring",
    "endpoint": "codex-cli",
    "fallback_endpoints": [
      {
        "endpoint": "kai-local",
        "model": "qwen35-gptq"
      }
    ],
    "workspace": "risk-manager",
    "max_iterations": 200
  },
  "scanner": {
    "description": "Market radar \\u2014 pump.fun monitoring, new token alerts, breakout detection",
    "endpoint": "kai-fast",
    "fallback_endpoint": null,
    "workspace": "scanner",
    "max_iterations": 200
  },
  "onchain": {
    "description": "Blockchain investigator \\u2014 wallet tracking, contract analysis, liquidity checks",
    "endpoint": "kai-smart",
    "fallback_endpoint": null,
    "workspace": "onchain",
    "max_iterations": 200
  },
  "mentor": {
    "description": "Reflection coach \\u2014 reads other agents' sessions and authors reusable skills for them",
    "endpoint": "kai-smart",
    "fallback_endpoints": [
      {
        "endpoint": "codex-cli",
        "model": "gpt-5.4"
      },
      {
        "endpoint": "kai-local",
        "model": "qwen35-gptq"
      }
    ],
    "workspace": "mentor",
    "max_iterations": 200
  }
}
```

Fields in current per-agent entries:

- `description`
- `endpoint`
- `model` only via optional endpoint refs or runtime model switch
- `fallback_endpoint` legacy singular
- `fallback_endpoints` plural chain
- `workspace`
- `max_iterations`
- `reasoning_effort` supported by `get_agent_config()` but absent from current entries
- `system_prompt` supported by `get_agent_config()` but absent from current entries

No current field represents health probe command, probe cadence, timeout, capacity
feedback codes, cooldown seconds, default executor, or overflow executor.

## Target Model

Discovery shows two concepts that should remain separate:

1. Logical KAI roles: `architect`, `developer`, `qa-agent`, `trader`, etc. These
   have prompts, workspaces, and taskboard routing identities.
2. Physical execution backends: `codex-xhigh`, `claude-high`, local LLM, z.ai, etc.
   These have capacity, probes, and cooldowns.

The implementation should introduce `agents.yaml` as the health/capacity registry
without breaking `agent-config.json`. Recommended merge rule:

- `agent-config.json` remains the source of role prompts, endpoint refs, workspaces,
  and max iterations for Phase 1.
- `agents.yaml` adds health/capacity metadata by logical agent id and executor id.
- When a logical agent does not define its own `health_probe`, it inherits health
  from `default_executor`.
- Executor entries are first-class health subjects because capacity exhaustion is
  usually backend-wide, not role-specific.

This keeps the epic's requested `agents.yaml` path while avoiding a risky migration
of the existing config loader in the same phase.

## Proposed `agents.yaml` Schema

```yaml
version: 1

executors:
  codex-xhigh:
    provider: codex-cli
    endpoint: codex-cli
    model: gpt-5.4
    reasoning_effort: xhigh
    health_probe:
      command: "python -m agent.health_probes codex --model gpt-5.4 --effort xhigh"
      interval_seconds: 60
      timeout_seconds: 10
    capacity_feedback_codes: [429, 503]
    cooldown_seconds: 300
    overflow_executor: claude-high

  claude-high:
    provider: claude-cli
    model: sonnet
    health_probe:
      command: "python -m agent.health_probes claude --model sonnet"
      interval_seconds: 60
      timeout_seconds: 10
    capacity_feedback_codes: [429, 503]
    cooldown_seconds: 300
    overflow_executor: codex-xhigh

agents:
  architect:
    default_executor: codex-xhigh
    overflow_executor: claude-high
  developer:
    default_executor: codex-xhigh
    overflow_executor: claude-high
  qa-agent:
    default_executor: codex-xhigh
    overflow_executor: claude-high
```

Validation rules:

- `version` is required and must be `1`.
- `executors` is required. Each executor id must be slug-safe.
- `health_probe.command` is optional but, when present, must be a non-empty string
  executed without shell interpolation unless the implementation explicitly uses
  `create_subprocess_shell` with documented quoting.
- `health_probe.interval_seconds` default: `60`; minimum: `5`.
- `health_probe.timeout_seconds` default: `10`; minimum: `1`; must be less than
  `interval_seconds`.
- `capacity_feedback_codes` default: `[429, 503]`; values must be integers from
  `100` to `599`.
- `cooldown_seconds` default: `300`; minimum: `0`.
- `default_executor` must reference an executor id.
- `overflow_executor` may be null, but if set must reference an executor id.
- Do not register Haiku.
- Probe secrets must be resolved from Vault-backed commands or process environment
  already provisioned by deployment; do not write secrets into `agents.yaml`.

## `agent/health_monitor.py`

Add a new module that owns in-memory health state, persistent state, and probe tasks.

Primary objects:

- `AgentHealthStatus`: dataclass or Pydantic model containing `agent_id`,
  `executor_id`, `status`, `last_probe_at`, `last_success_at`, `last_error`,
  `cooldown_until`, `source`, and `updated_at`.
- `AgentHealthConfig`: normalized config from `agents.yaml` plus merged defaults.
- `AgentHealthStore`: SQLite persistence wrapper.
- `AgentHealthMonitor`: lifecycle owner for probe tasks and capacity feedback.

Status enum:

- `unknown`: no successful probe has run yet.
- `healthy`: last probe passed and no active cooldown.
- `unhealthy`: last probe failed or capacity feedback marked it down.
- `cooldown`: status is unhealthy until `cooldown_until`.
- `disabled`: probe disabled; dispatch may allow it only if config says so.

Core APIs:

```python
class AgentHealthMonitor:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def probe_once(self, agent_or_executor_id: str) -> AgentHealthStatus: ...
    def snapshot(self) -> list[AgentHealthStatus]: ...
    def get(self, agent_or_executor_id: str) -> AgentHealthStatus: ...
    def can_dispatch(self, agent_id: str) -> DispatchDecision: ...
    def mark_capacity_error(self, agent_or_executor_id: str, *, code: int, error: str) -> AgentHealthStatus: ...
```

`DispatchDecision` should include:

- requested logical agent id
- selected executor id
- selected local KAI agent name, when applicable
- status
- fallback used boolean
- reason
- cooldown_until

Probe runner:

- On daemon startup, load config and initialize the SQLite table.
- Start one asyncio task per executor that has a probe command.
- Run commands with `asyncio.create_subprocess_shell` or `create_subprocess_exec`.
  Prefer `exec` if command parsing is structured in config; if the schema stores a
  single command string, use shell execution but document that only trusted local
  operators edit `agents.yaml`.
- Capture stdout/stderr, truncate stored errors to a bounded length, and persist one
  row per executor.
- A failed probe should not crash the runner. One bad probe must not block other
  agents.
- Cooldown from capacity feedback wins over probe success until `cooldown_until`
  unless an operator explicitly clears it.

## SQLite Table

Use a small daemon-local SQLite database, initially under the existing workspaces
root:

`workspaces/daemon-state.sqlite3`

Schema:

```sql
CREATE TABLE IF NOT EXISTS agents_health (
    agent_id TEXT PRIMARY KEY,
    executor_id TEXT NOT NULL,
    last_probe_at TEXT,
    last_success_at TEXT,
    last_status TEXT NOT NULL DEFAULT 'unknown',
    last_error TEXT,
    cooldown_until TEXT,
    source TEXT NOT NULL DEFAULT 'startup',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_health_executor
    ON agents_health (executor_id);

CREATE INDEX IF NOT EXISTS idx_agents_health_cooldown
    ON agents_health (cooldown_until);
```

`agent_id` may be a logical role or executor id. To avoid ambiguous updates, store
executor health rows with `agent_id == executor_id`; logical role snapshots can be
computed by joining config to executor rows unless a future phase needs per-role
overrides.

## Health Routes

Add daemon routes in `daemon/server.py`:

- `GET /api/agents/health`
- `GET /api/agents/{id}/health`

Response shape:

```json
{
  "agents": [
    {
      "id": "architect",
      "default_executor": "codex-xhigh",
      "selected_executor": "codex-xhigh",
      "overflow_executor": "claude-high",
      "status": "healthy",
      "last_probe_at": "2026-04-28T20:40:00Z",
      "last_error": null,
      "cooldown_until": null
    }
  ]
}
```

The single-agent endpoint should return `404` for unknown ids and `200` for known ids
with `unknown` status when no probe has run yet. Keep payload generation in the
monitor, not in the route function, so dispatchers and wrappers use the same decision
logic.

## Capacity Feedback Hook

Current `/tools/invoke` accepts a spawn, schedules async work, and usually returns
`200` with `ok: true` before any LLM request is made. Therefore a pure response-code
hook in `/tools/invoke` is not enough. Implement both hooks:

1. Synchronous request hook in `taskboard_gateway.app.tools_invoke()`:
   - before `sessions_spawn`, call `health_monitor.can_dispatch(route.local_agent_name)`;
   - if no healthy executor exists, return `503` or `ok: false` with a clear cooldown
     body, matching gateway compatibility expectations;
   - if a fallback is selected, record that selected executor in the run metadata.
2. Execution hook in `execute_run_with_local_session()` and direct spawn paths:
   - when a backend raises a typed provider error or an HTTP status in
     `capacity_feedback_codes`, call `mark_capacity_error()`;
   - persist cooldown and emit an audit event.

Add a small error classifier near the health module:

```python
def classify_capacity_error(exc_or_text: object) -> int | None:
    ...
```

It should recognize explicit HTTP status codes first. Text parsing of CLI output
should be a fallback and should be tested conservatively to avoid marking an agent
unhealthy from unrelated prose.

## Dispatcher Pre-Flight Gate

Add one shared decision function and make every spawn surface use it:

```python
decision = health_monitor.can_dispatch(agent_id)
if not decision.allowed:
    raise AgentUnavailable(decision)
```

Integration points present today:

- `taskboard_gateway.app.tools_invoke()` for `sessions_spawn`.
- `agent/tools.py:create_spawn_agent_tool()` or, better, the underlying
  `SessionSubAgentRegistry.spawn()` / `SubAgentManager.spawn()` so TUI, LLM tool, and
  direct code paths share the same gate.
- `tui/terminal.py:dispatch_agent()` for signal handlers if it bypasses the shared
  spawn path in any future refactor.

Integration points expected by #10023 but not present on `origin/main`:

- `taskboard_dispatcher`
- `forgejo_dispatcher`

When those files land, they should call the same `can_dispatch()` function before
creating a taskboard/gateway run. If primary is unhealthy and overflow is healthy,
they should route to overflow and post an audit comment. If neither is healthy, post:

`[System] no healthy executor for role <role>; primary <id> is in cooldown until <ts>`

and emit a NATS alert.

## Spawn Wrapper `--check-health`

No `claude-spawn.sh` or `codex-spawn.sh` exists in this canonical tree. The only
current `bin` entry is `bin/kaictl`. Phase 6 should either add the wrappers here or
patch the deployment repo that owns them.

Recommended behavior:

```bash
bin/codex-spawn.sh --check-health --agent architect -- task text...
bin/claude-spawn.sh --check-health --agent qa-agent -- task text...
```

`--check-health` should:

- call `GET /api/agents/{id}/health`;
- skip spawn when status is `unhealthy` or `cooldown`;
- print selected fallback when available;
- return exit code `75` (`EX_TEMPFAIL`) for cooldown/no-capacity cases.

## Prometheus Metrics

KAI currently has `/api/metrics` as JSON. Add either:

- `GET /api/prometheus`, or
- content negotiation on `/api/metrics` when `Accept: text/plain` is sent.

Do not break existing JSON tests for `/api/metrics`.

Metrics:

```text
kai_agent_health{agent="codex-xhigh",status="healthy"} 1
kai_agent_health{agent="codex-xhigh",status="unhealthy"} 0
kai_agent_cooldown_until_seconds{agent="codex-xhigh"} 1777409100
kai_agent_probe_duration_seconds_bucket{agent="codex-xhigh",le="0.5"} 1
kai_agent_probe_errors_total{agent="codex-xhigh"} 0
kai_agent_capacity_errors_total{agent="codex-xhigh",code="429"} 1
kai_agent_dispatch_fallback_total{agent="architect",from="codex-xhigh",to="claude-high"} 1
```

If `prometheus_client` is added, keep it optional or add it to requirements with
tests. If handwritten exposition is used, keep labels escaped and deterministic.

## Existing Codex and Claude Registration

Phase 8 should add concrete registrations for supported tiers:

```yaml
executors:
  codex-xhigh:
    provider: codex-cli
    endpoint: codex-cli
    model: gpt-5.4
    reasoning_effort: xhigh
    health_probe:
      command: "python -m agent.health_probes codex --model gpt-5.4 --effort xhigh"
      interval_seconds: 60
      timeout_seconds: 10
    capacity_feedback_codes: [429, 503]
    cooldown_seconds: 300
    overflow_executor: claude-high

  claude-high:
    provider: claude-cli
    model: sonnet
    health_probe:
      command: "python -m agent.health_probes claude --model sonnet"
      interval_seconds: 60
      timeout_seconds: 10
    capacity_feedback_codes: [429, 503]
    cooldown_seconds: 300
    overflow_executor: codex-xhigh
```

Probe implementation detail:

- Codex probe should first verify `~/.codex/auth.json` via
  `agent.codex_auth.get_valid_credentials()`. That checks login/refresh, not quota.
  If quota verification is required, use a minimal Codex Responses probe and budget
  it deliberately.
- Claude probe should verify the CLI is present and credentials are usable through a
  lightweight command or a dedicated provider probe module. Do not store Anthropic
  API keys in config; use Vault-backed runtime environment.
- Local LLM and z.ai probes are TBD entries with disabled status until their actual
  backend contracts are known.

## Operator Wrapper and Guide

Add:

- `bin/agent-health.sh`
- `docs/AGENT-HEALTH-GUIDE.md`

`bin/agent-health.sh` behavior:

```bash
bin/agent-health.sh
bin/agent-health.sh codex-xhigh
bin/agent-health.sh --json
bin/agent-health.sh --watch 5
```

Guide contents:

- What health statuses mean.
- How capacity cooldowns are triggered.
- How to add a new backend to `agents.yaml`.
- How to write safe probe commands.
- How to use Vault-provisioned credentials.
- How to clear a false cooldown.
- How to read Prometheus metrics.

## Phase Dev-Fire Prompts

### Phase 1 - `agents.yaml` Schema Additions

Prompt:

```text
Implement Phase 1 of epic #10023 in KAI. Discovery shows there is no agents.yaml on origin/main; the existing role registry is agent-config.json loaded by config.py. Add a new agents.yaml loader that preserves agent-config.json compatibility and validates health/capacity metadata.

Scope:
- Add agent/agent_registry.py or config-local helpers to load agents.yaml from repo root.
- Support version: 1, executors, and agents maps.
- Validate health_probe.command, interval_seconds default 60, timeout_seconds default 10, capacity_feedback_codes default [429, 503], cooldown_seconds default 300, default_executor, and overflow_executor.
- Merge logical agent health config with existing config.AGENTS without changing current get_agent_config behavior for prompt/model/workspace.
- Add sane defaults for existing roles when agents.yaml is absent.
- Reject malformed entries with clear ValueError messages.

Tests:
- New tests for absent agents.yaml, valid schema, defaults, malformed codes/timeouts, unknown default_executor, unknown overflow_executor.
- Existing config and daemon tests must keep passing.

Do not push.
```

### Phase 2 - Background Probe Runner

Prompt:

```text
Implement Phase 2 of epic #10023. Add agent/health_monitor.py with an asyncio probe runner per registered executor and SQLite persistence.

Scope:
- Create AgentHealthMonitor, AgentHealthStore, AgentHealthStatus, and DispatchDecision.
- Add agents_health table with agent_id, executor_id, last_probe_at, last_success_at, last_status, last_error, cooldown_until, source, updated_at.
- Run configured health_probe.command on interval_seconds with timeout_seconds.
- Persist status transitions and keep an in-memory snapshot.
- One bad probe must not stop other probe tasks.
- Cooldown state from capacity feedback must not be cleared by a successful probe until cooldown_until expires unless explicitly cleared.

Tests:
- Stub probe command returns 0 -> healthy.
- Stub probe command returns nonzero -> unhealthy with truncated error.
- Timeout -> unhealthy.
- Multi-agent probes do not deadlock.
- State reloads from SQLite.

Do not push.
```

### Phase 3 - Health API Routes

Prompt:

```text
Implement Phase 3 of epic #10023. Expose health monitor state through daemon routes without changing existing /api/health readiness semantics.

Scope:
- Attach AgentHealthMonitor to DaemonServer lifecycle.
- Start it during daemon startup and stop it during shutdown.
- Add GET /api/agents/health and GET /api/agents/{id}/health.
- Preserve existing auth behavior via daemon_server.require_http_auth(request).
- Return logical agents with default_executor, selected_executor, overflow_executor, status, last_probe_at, last_error, cooldown_until.
- Unknown id returns 404.

Tests:
- Existing /api/health and /api/metrics tests unchanged.
- New route returns configured agents within bounded payload shape.
- Single route 404s for unknown id.
- Auth required consistently.

Do not push.
```

### Phase 4 - Capacity Feedback Hook

Prompt:

```text
Implement Phase 4 of epic #10023. Feed capacity failures back into AgentHealthMonitor.

Scope:
- Add classify_capacity_error(exc_or_text) that recognizes explicit HTTP 429/503 provider errors first, with conservative text fallback.
- In taskboard_gateway.app /tools/invoke sessions_spawn, preflight route.local_agent_name with health_monitor.can_dispatch().
- During execute_run_with_local_session and direct agent execution, mark capacity errors on configured response codes.
- mark_capacity_error must set last_status cooldown/unhealthy, last_error, cooldown_until now + cooldown_seconds, source capacity_feedback.
- Return or persist a clear "agent X in cooldown until Y" message.

Tests:
- 429 marks configured executor unhealthy/cooldown.
- Non-capacity error does not mark cooldown.
- /tools/invoke rejects dispatch when no healthy executor exists.
- Existing gateway tests still pass or are updated to account for injected healthy default monitor.

Do not push.
```

### Phase 5 - Dispatcher Pre-Flight Health Gate

Prompt:

```text
Implement Phase 5 of epic #10023. Add a shared pre-flight health gate to every KAI spawn dispatcher.

Discovery note:
- origin/main does not contain taskboard_dispatcher.py or forgejo_dispatcher.py.
- Current taskboard spawn surface is taskboard_gateway.app sessions_spawn.
- Current TUI signal dispatcher is tui/terminal.py dispatch_agent.

Scope:
- Add a shared gate function around AgentHealthMonitor.can_dispatch(agent_id).
- Wire it into taskboard_gateway sessions_spawn.
- Wire it into SubAgentManager.spawn or SessionSubAgentRegistry.spawn so TUI/tool spawns are gated.
- If taskboard_dispatcher or forgejo_dispatcher files exist on the implementation branch, wire the same gate there too.
- If primary unhealthy and overflow healthy, route to overflow and record fallback in run metadata/audit event.
- If no executor healthy, post a [System] no healthy executor comment where source ticket context exists and publish a NATS alert.

Tests:
- Healthy primary dispatches normally.
- Unhealthy primary selects overflow.
- Both unhealthy blocks spawn with clear reason.
- Missing future dispatcher modules does not fail tests on origin/main.

Do not push.
```

### Phase 6 - Spawn Wrapper `--check-health`

Prompt:

```text
Implement Phase 6 of epic #10023. Add operator-side health preflight for spawn wrappers.

Discovery note:
- origin/main has bin/kaictl only; no claude-spawn.sh or codex-spawn.sh exists.

Scope:
- If deployment-owned wrappers exist in the target branch, add --check-health to them.
- Otherwise add bin/codex-spawn.sh and bin/claude-spawn.sh with --check-health and documented minimal behavior.
- --check-health calls GET /api/agents/{id}/health using the same auth style as kaictl/daemon control.
- Skip spawn on unhealthy/cooldown and show fallback recommendation.
- Return exit code 75 for temporary capacity failures.

Tests:
- Shell tests or Python subprocess tests for healthy, cooldown, and unknown-agent responses.
- Verify wrappers do not leak auth tokens in logs.

Do not push.
```

### Phase 7 - Prometheus Metrics

Prompt:

```text
Implement Phase 7 of epic #10023. Add Prometheus-format agent health metrics while preserving existing JSON /api/metrics behavior.

Scope:
- Add GET /api/prometheus or content negotiation on /api/metrics for text/plain.
- Emit kai_agent_health, kai_agent_cooldown_until_seconds, kai_agent_probe_errors_total, kai_agent_probe_duration_seconds, kai_agent_capacity_errors_total, and kai_agent_dispatch_fallback_total.
- Keep labels deterministic and escaped.
- Existing /api/metrics JSON tests must keep passing.

Tests:
- Prometheus endpoint returns text/plain.
- Healthy/unhealthy statuses produce 1/0 gauges.
- Capacity error increments counter.
- No duplicate series for repeated snapshots.

Do not push.
```

### Phase 8 - Register Existing Codex and Claude

Prompt:

```text
Implement Phase 8 of epic #10023. Register supported Codex and Claude executors in agents.yaml and add probe commands.

Scope:
- Add agents.yaml entries for codex-xhigh and claude-high.
- Map existing high-value KAI roles such as architect, developer, qa-agent, code-reviewer, security-auditor, and deep-research to default_executor codex-xhigh and overflow_executor claude-high unless product direction says otherwise.
- Do not register Haiku.
- Add agent/health_probes.py CLI with codex and claude subcommands.
- Codex probe validates OAuth credentials via agent.codex_auth.get_valid_credentials() and optionally a deliberately tiny backend request if enabled by config.
- Claude probe validates CLI/API credential availability using Vault-provisioned runtime secrets, not files committed to git.
- z.ai and local-LLM entries may be disabled/TBD placeholders only if tests cover disabled behavior.

Tests:
- agents.yaml loads and validates.
- Probe CLI returns 0 for mocked healthy credentials and nonzero for mocked failures.
- No secrets are committed.

Do not push.
```

### Phase 9 - Operator Wrapper and Guide

Prompt:

```text
Implement Phase 9 of epic #10023. Add the operator health CLI and guide.

Scope:
- Add bin/agent-health.sh.
- Add docs/AGENT-HEALTH-GUIDE.md.
- CLI supports table output, --json, single agent id, and --watch seconds.
- Guide explains statuses, cooldowns, adding a backend probe, Vault secret expectations, Prometheus metrics, and clearing false cooldowns.
- Link the guide from docs/README.md if appropriate.

Tests:
- CLI formats healthy/cooldown/unknown rows from mocked API JSON.
- --json passes raw JSON through.
- Missing daemon gives clear nonzero output.

Do not push.
```

## Blockers and Sequencing

Unblocked immediately on `origin/main`:

- Phase 1: schema loader and validation.
- Phase 2: health monitor and SQLite table.
- Phase 3: health API routes.
- Phase 4: capacity feedback foundation and taskboard gateway preflight.
- Phase 7: Prometheus metrics, as a new endpoint.
- Phase 8: initial codex/claude config and probe module.
- Phase 9: `bin/agent-health.sh` and guide.

Partially blocked or dependent:

- Phase 5: current tree lacks `taskboard_dispatcher` and `forgejo_dispatcher`; wire
  the current `taskboard_gateway` now and wire those modules when their branches land.
- Phase 6: current tree lacks `codex-spawn.sh` and `claude-spawn.sh`; decide whether
  KAI owns new wrappers or another deployment repo owns them.

Implementation order should stay 1 -> 2 -> 3 -> 4 -> 5 -> 7 -> 8 -> 9, with Phase 6
placed wherever wrapper ownership is resolved. Phase 5 needs the health monitor API
from Phase 2 and should not invent parallel state.

## Acceptance Mapping

- `GET /api/agents/health` returns per-agent status table within 200 ms:
  Phase 3, backed by in-memory snapshot from Phase 2.
- Dispatcher and spawn wrappers honor health gate:
  Phase 5 and Phase 6.
- Codex 429 marks unhealthy with five-minute cooldown:
  Phase 4 using `capacity_feedback_codes` and `cooldown_seconds`.
- System audit comment on source ticket:
  Phase 5 when source context exists; otherwise NATS alert plus logs.
- Prometheus metrics scrapable:
  Phase 7.
- Operator can run `agent-health` and add probes by editing YAML:
  Phase 9 plus Phase 1 validation.

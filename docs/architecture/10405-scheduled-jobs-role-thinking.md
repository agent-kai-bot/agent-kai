# Architecture Artifact - Task 10405

## Title
Scheduled jobs target role and reasoning/thinking level support

## Status
Read-only implementation specification. No implementation code, commits, or pushes were produced.

## Date
2026-05-10

## Operator Request

> "I have another Claude agent working with agent kai scheduling jobs and one thing we noticed was there we can't set roles or thinking levels.. let's have codex xhigh review and put together what's required to implement"

## Executive Recommendation

Implement job-level execution overrides as a first-class scheduler contract:

1. Add optional `target_agent_role`, `reasoning_effort`, `thinking_level`, and `extra_env` fields to the logical `ScheduledJob` model.
2. Dispatch each scheduled fire through a per-call `AgentRunner` override inside the owner session when those fields are set.
3. Keep `owner_session` as the UI/event/persistence owner. The target role changes only the agent identity, system prompt, memory/skills, model endpoint, and reasoning/thinking configuration used for that fire.
4. Do not use `SubAgentManager.spawn()` for this path. It is a NATS/sub-agent lifecycle boundary, not a deterministic in-session scheduler execution primitive.
5. Persist v2 routing fields in a rollback-safe root sidecar map in `workspaces/scheduler/jobs.json`, not inline under `jobs.<id>`, until the older v1 loader has been relaxed. The current v1 `ScheduledJob` uses `extra="forbid"` and will drop records with unknown per-job fields.

This lets a single session keep its normal `kai` default while a five-minute BIO/BLEND Wyckoff job runs as `analyst` at `xhigh` reasoning on every fire.

## Files Inspected

- [daemon/scheduler.py](/home/atc/git/claude-local-ai-agent/daemon/scheduler.py:203)
  - `ScheduledJob` model, strict `extra="forbid"`, lifecycle, JSON persistence, APScheduler registration.
- [daemon/server.py](/home/atc/git/claude-local-ai-agent/daemon/server.py:1367)
  - `run_input()` serializes a session turn through `managed.input_lock`.
- [daemon/server.py](/home/atc/git/claude-local-ai-agent/daemon/server.py:1726)
  - `_handle_scheduled_job_trigger()` currently attaches `owner_session` and runs `job.prompt` through that session's bound agent.
- [daemon/protocol.py](/home/atc/git/claude-local-ai-agent/daemon/protocol.py:219)
  - Current scheduled-job WebSocket envelopes.
- [agent/tools.py](/home/atc/git/claude-local-ai-agent/agent/tools.py:830)
  - `create_scheduler_tools()` exposes `schedule_at`, `schedule_recurring`, `schedule_when`, list, pause, resume, cancel.
- [agent/auto_loop_brain.py](/home/atc/git/claude-local-ai-agent/agent/auto_loop_brain.py:149)
  - Codex CLI precedent: `codex exec -c model_reasoning_effort=<level>`.
- [agent/auto_loop_brain.py](/home/atc/git/claude-local-ai-agent/agent/auto_loop_brain.py:124)
  - Claude CLI precedent: `claude -p --append-system-prompt ... --model ...`.
- [agent/core.py](/home/atc/git/claude-local-ai-agent/agent/core.py:208)
  - Main agent `codex-cli` endpoint is implemented through `ChatCodex` and the Responses API, not by shelling out to `codex exec`.
- [agent/core.py](/home/atc/git/claude-local-ai-agent/agent/core.py:236)
  - `ChatCodex` already sends `extra_body.reasoning.effort`.
- [config.py](/home/atc/git/claude-local-ai-agent/config.py:425)
  - Existing `normalize_reasoning_effort()` and supported canonical reasoning values.
- [config.py](/home/atc/git/claude-local-ai-agent/config.py:640)
  - `get_agent_config()` resolves agent endpoint/fallback configuration and applies per-agent reasoning overrides.
- [agent-config.json](/home/atc/git/claude-local-ai-agent/agent-config.json:220)
  - `analyst` is a configured agent role using `codex-cli` with local fallback.
- [web/src/routes/+page.svelte](/home/atc/git/claude-local-ai-agent/web/src/routes/+page.svelte:712)
  - Scheduler activity summary in the dashboard.
- [web/src/lib/daemon/types.ts](/home/atc/git/claude-local-ai-agent/web/src/lib/daemon/types.ts:292)
  - Current scheduled-job envelope TypeScript union.
- OpenAI official Responses API reference
  - Reasoning models use `reasoning.effort`; current supported values include `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`.

## Current State Summary

`ScheduledJob` currently contains only schedule mechanics and prompt content:

```python
id, type, spec, prompt, owner_session,
created_at, created_by, last_run, next_run, run_count,
max_runs, status, last_result_preview,
concurrency, tool_budget
```

The scheduler persists records under `workspaces/scheduler/jobs.json`. `_persist_jobs()` writes `version = 1` and a `jobs` object. `load_jobs()` drops invalid persisted jobs after logging a warning.

Dispatch is session-bound:

1. APScheduler calls `Scheduler._fire_scheduled_job()`.
2. `Scheduler._dispatch()` calls the daemon's `dispatch_callback`.
3. `DaemonServer._handle_scheduled_job_trigger()` loads `job.owner_session`.
4. `DaemonServer.run_input()` calls `managed.session.stream_agent_events()`.
5. `Session.stream_agent_events()` uses the session's existing `agent_runner`.

So a job created by `kai` in session `terminal` always runs as that session's current bound agent. It cannot say "run this as `analyst` at `xhigh`" without changing the session default.

## 1. Schema Changes

Add these logical fields to `ScheduledJob`:

```python
ScheduledReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]
ScheduledThinkingLevel = Literal["normal", "hard", "ultra"]

target_agent_role: NonEmptyString | None = None
reasoning_effort: ScheduledReasoningEffort | None = None
thinking_level: ScheduledThinkingLevel | None = None
extra_env: dict[str, str] | None = None
```

Also extend `JobStatus`:

```python
JobStatus = Literal["active", "paused", "completed", "failed", "cancelled", "quarantine"]
```

Field semantics:

- `target_agent_role = None`: preserve current behavior. The job fires through `owner_session`'s bound/default agent.
- `target_agent_role = "analyst"`: the job fires through the `analyst` role for that turn only. It does not mutate or persist the owner session's default agent.
- `reasoning_effort = None`: inherit from the resolved role's endpoint/model/agent config.
- `reasoning_effort = "xhigh"`: override the Codex/OpenAI reasoning effort for that scheduled fire.
- `thinking_level = None`: inherit from the resolved Claude role/config.
- `thinking_level = "hard"`: override Claude CLI effort for that scheduled fire.
- `extra_env = None`: no environment override.
- `extra_env = {...}`: scoped, non-secret runtime hints for the job fire. Do not mutate process-global `os.environ`.

Use `None`, not `"default"`, to mean inherit. If a tool or UI accepts aliases like `x-high`, `max`, or `min`, normalize them before storage.

Reasoning value decision:

- Existing repo docs/config support `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`.
- Job-level `None` already means inherit/default, so the job schema should not store `"none"` initially.
- Include both `minimal` and `low` because the repo already exposes both and the requested UI mentions `low`.
- If an operator explicitly asks for "no reasoning" later, add `"none"` as an explicit value in a separate compatibility review.

Claude thinking decision:

- Keep the user-facing field as `thinking_level: normal | hard | ultra`.
- The current local Claude CLI help exposes `--effort <level>` with `low`, `medium`, `high`, `xhigh`, and `max`.
- Map `normal -> medium`, `hard -> high`, and `ultra -> xhigh` for Claude CLI. Reserve `max` until the operator explicitly wants a slower/costlier ultra-plus mode.

## 2. Validation Rules

Validation should be split into two layers.

### 2.1 Model-local validation

The `ScheduledJob.@model_validator` should enforce rules that do not need runtime config:

- Existing schedule validation remains:
  - absolute jobs require `spec={"at": ISO_TIMESTAMP}`.
  - cron jobs require valid `cron` and optional string `tz`.
  - event jobs require `channel` and structured `filter`.
- `run_count >= 0`.
- `max_runs is None or max_runs >= 1`.
- `tool_budget is None or tool_budget >= 1`.
- `target_agent_role` is stripped; empty string becomes `None` or is rejected.
- `reasoning_effort` is canonicalized through the existing reasoning normalizer.
- `"none"` from tool/UI input should normalize to `None` for job storage.
- `thinking_level` is lowercase canonical.
- `reasoning_effort` and `thinking_level` are mutually exclusive for Phase 1.
- `extra_env` keys must be env-var safe: `^[A-Z_][A-Z0-9_]*$`.
- `extra_env` values must be strings, no NUL bytes, no newlines, bounded length.
- `extra_env` must reject secret-shaped names by default: `*SECRET*`, `*TOKEN*`, `*PASSWORD*`, `*API_KEY*`.
- `extra_env` should default to an allowlist prefix, initially `KAI_`, configurable under `daemon.scheduler.allowed_extra_env_prefixes`.

### 2.2 Runtime-aware validation

Role and provider compatibility require `agent-config.json`. Do this through a shared helper used by scheduler create/update, tools, REST, migration, and dispatch:

```python
validate_scheduled_job_execution_options(
    target_agent_role,
    reasoning_effort,
    thinking_level,
    extra_env,
    owner_session_agent=None,
    agents=AGENTS,
    endpoints=ENDPOINTS,
)
```

Rules:

- If `target_agent_role` is set, it must exist in `AGENTS`.
- If `target_agent_role` is missing and the caller knows the owner session's current/default agent, validate against that role.
- If the owner session does not exist yet, defer compatibility validation until dispatch. This preserves existing create/load behavior for jobs that target sessions not currently live.
- `reasoning_effort` is valid only when the effective role's primary endpoint is a reasoning-capable provider:
  - `provider == "codex-cli"`: supported by `ChatCodex`.
  - `provider == "openai"` with a known reasoning model family, for example GPT-5 or o-series, once `create_llm()` is wired to pass `reasoning`.
- Do not silently accept `reasoning_effort` for `kai-fast`, `kai-smart`, `kai-local`, or generic OpenAI-compatible local endpoints. Return a clear error that the endpoint does not honor reasoning effort.
- `thinking_level` is valid only for a `claude-cli` provider/adapter.
- If a role's primary endpoint is unsupported but a fallback is supported, reject the job-level override in Phase 1. Fallbacks are only used on runtime failure, so accepting the override would make the common path a no-op.
- If both `reasoning_effort` and `thinking_level` are set, reject with a message that the job can target one provider-control family at a time.

Backward compatibility:

- Existing jobs without these fields load with all four fields as `None`.
- Pydantic validation in migration mode must allow legacy records and quarantined invalid records without crashing daemon startup.
- Persisted invalid records must be surfaced as quarantine entries rather than dropped.

## 3. Dispatch Path Changes

### 3.1 Effective execution plan

Add a small execution-plan resolver near `DaemonServer._handle_scheduled_job_trigger()`:

```python
ScheduledJobExecutionPlan(
    owner_session=job.owner_session,
    effective_agent_role=job.target_agent_role or managed.session.agent_name or self.agent_name,
    requested_reasoning_effort=job.reasoning_effort,
    effective_reasoning_effort=resolved endpoint effort after override/inheritance,
    requested_thinking_level=job.thinking_level,
    effective_thinking_effort=resolved Claude CLI --effort value,
    provider=...,
    endpoint=...,
    model=...,
    extra_env=job.extra_env or {},
)
```

Resolve this after attaching the owner session and before emitting the triggered event. If resolution fails, mark the job failed or quarantined depending on whether the failure is transient or configuration-invalid.

### 3.2 Recommended runner boundary

Use a per-call in-process `AgentRunner` override, not a spawned sub-agent:

- `SubAgentManager.spawn()` is NATS/bus lifecycle management. It is long-lived, less direct to observe, and does not naturally return the scheduled job's final text for `record_completion()`.
- A temporary `AgentRunner` can reuse the owner session event bus, lock, chat history, tools, telemetry, and `tool_budget` behavior.
- The owner session remains the place where the UI sees tokens, scheduled job events, and final output.

Implementation shape:

1. Extend `DaemonServer.run_input()`:

```python
async def run_input(
    managed,
    text,
    *,
    source="user",
    job_id=None,
    tool_budget=None,
    single_auto_iteration=False,
    pre_injected_input=False,
    target_agent_role=None,
    reasoning_effort=None,
    thinking_level=None,
    extra_env=None,
)
```

2. Extend `Session.stream_agent_events()` with the same override parameters.
3. Add a session helper, for example:

```python
Session.agent_runner_for_turn(
    target_agent_role=None,
    reasoning_effort=None,
    thinking_level=None,
    extra_env=None,
) -> contextmanager[AgentRunner]
```

4. If no override is present, yield the existing `self.agent_runner`.
5. If an override is present:
   - Build a new `AgentRunner` for `target_agent_role`.
   - Reuse the current runner's raw tool set, not its already wrapped tools.
   - Set `runner.telemetry = self`.
   - Set `runner.chat_history = self.chat_history`.
   - Apply an endpoint config patch for `reasoning_effort` or `thinking_level`.
   - Do not mutate `self.agent_name`.
   - Do not mutate `self.agent_runner`.
   - Do not mutate global `AGENTS`.
   - Do not persist the target role into `state.json`.

6. Add a scheduled-job system marker with useful routing context:

```text
[scheduled job: job_... target_agent_role=analyst reasoning_effort=xhigh]
```

The existing tests already assert the scheduled-job marker is inserted in chat history. Extend that assertion instead of removing it.

### 3.3 Reasoning propagation

There are three distinct paths. Keep them explicit.

#### Main agent Codex Responses path

This repo's `codex-cli` endpoint is implemented in `agent/core.py` through `ChatCodex`, not a CLI subprocess.

Required change:

- Add a non-mutating endpoint override path in `AgentRunner`.
- When `provider == "codex-cli"` and `reasoning_effort` is set, write it into the resolved endpoint dict:

```python
endpoint_cfg["reasoning_effort"] = job.reasoning_effort
```

- `_create_codex_chat_model()` already reads `endpoint_cfg["reasoning_effort"]` and sends:

```python
"reasoning": {"effort": reasoning_effort, "summary": "auto"}
```

#### Codex CLI subprocess path

For tool-less clients or future subprocess-backed Codex adapters, mirror `auto_loop_brain`:

```text
codex exec --dangerously-bypass-approvals-and-sandbox \
  -c model_reasoning_effort=<level> \
  -c model="<model>" \
  "<prompt>"
```

The existing `CodexCLIToollessLLMClient` is the closest precedent.

#### OpenAI-compatible path

For true OpenAI Responses-compatible endpoints, pass:

```json
{"reasoning": {"effort": "xhigh", "summary": "auto"}}
```

Implementation detail depends on the LangChain version:

- Prefer `use_responses_api=True` plus `extra_body={"reasoning": {"effort": effort, "summary": "auto"}}` for GPT-5/o-series OpenAI direct models.
- Do not send this body to generic vLLM/local endpoints unless the endpoint declares support.
- Add a provider capability helper rather than guessing from only `provider == "openai"`.

#### Claude CLI path

Extend the Claude CLI adapter to accept a mapped effort:

```text
claude -p --append-system-prompt <system> --model <model> --effort <medium|high|xhigh> <user>
```

Mapping:

- `normal -> medium`
- `hard -> high`
- `ultra -> xhigh`

If a deployed Claude CLI does not expose `--effort`, tool-call validation should fail with a clear action: update Claude CLI or omit `thinking_level`.

### 3.4 Extra env propagation

Do not implement `extra_env` by temporarily mutating `os.environ`. That is process-global and unsafe while other sessions are running.

Use a scoped per-task mechanism:

- Add a `ContextVar[dict[str, str]]`, for example `current_job_extra_env`.
- `run_input()` sets it for the duration of the scheduled job turn.
- Subprocess-backed clients/tools merge it into `subprocess.run(env={**os.environ, **current_job_extra_env.get({})})`.
- In-process code that needs job-specific flags must read the context var explicitly.
- Redact `extra_env` values from logs and telemetry. Emit only keys unless an allowlist says a value is safe.

### 3.5 Telemetry

Emit daemon/session telemetry on every fire:

- `auto.scheduled_job.triggered`
- `auto.scheduled_job.completed`
- `auto.scheduled_job.failed`
- `auto.scheduled_job.skipped`
- `auto.scheduled_job.quarantined`

Payload fields:

```json
{
  "job_id": "job_...",
  "owner_session": "terminal",
  "target_agent_role": "analyst",
  "effective_agent_role": "analyst",
  "requested_reasoning_effort": "xhigh",
  "effective_reasoning_effort": "xhigh",
  "requested_thinking_level": null,
  "effective_thinking_effort": null,
  "provider": "codex-cli",
  "endpoint": "codex-cli",
  "model": "gpt-5.5",
  "fired_at": "...",
  "run_count_before": 12,
  "concurrency": "queue",
  "extra_env_keys": ["KAI_FOO"]
}
```

Also make `llm.usage` attribution respect the effective role for the turn. Today `Session.publish_event("llm.usage", ...)` records `session.agent_name`; scheduled role overrides need the payload to include `effective_agent_role` or a temporary session-level context so cost telemetry does not misattribute analyst runs to `kai`.

## 4. Tool Surface Changes

There is no current tool named `create_scheduled_job`. The current create surface is:

- `schedule_at`
- `schedule_recurring`
- `schedule_when`

Recommended implementation:

1. Add a new generic `create_scheduled_job` tool for clear LLM behavior.
2. Keep the existing three tools as compatibility wrappers.
3. Add the new fields to all create paths.

New generic inputs:

```python
type: Literal["absolute", "cron", "event"]
spec: dict
prompt: str
session: str | None = None
max_runs: int | None = None
tool_budget: int | None = None
concurrency: Literal["skip", "queue"] = "queue"
target_agent_role: str | None = None
reasoning_effort: str | None = None
thinking_level: str | None = None
extra_env: dict[str, str] | None = None
```

Existing tool additions:

- `schedule_at(when, prompt, session=None, tool_budget=None, target_agent_role=None, reasoning_effort=None, thinking_level=None, extra_env=None)`
- `schedule_recurring(cron, prompt, session=None, max_runs=None, tool_budget=None, target_agent_role=None, reasoning_effort=None, thinking_level=None, extra_env=None)`
- `schedule_when(condition, prompt, session=None, max_runs=None, tool_budget=None, target_agent_role=None, reasoning_effort=None, thinking_level=None, extra_env=None)`

Tool descriptions must explicitly guide the LLM:

- Use `target_agent_role` when the user asks the scheduled job to run as `architect`, `developer`, `qa`, `analyst`, `trader`, etc.
- Use `reasoning_effort` for Codex/OpenAI reasoning roles. Valid values: `minimal`, `low`, `medium`, `high`, `xhigh`.
- Use `thinking_level` for Claude CLI roles. Valid values: `normal`, `hard`, `ultra`.
- Omit effort fields to inherit role defaults.
- For "run BIO Wyckoff analysis every 5 minutes as analyst with max thinking", use:

```json
{
  "cron": "*/5 * * * *",
  "target_agent_role": "analyst",
  "reasoning_effort": "xhigh"
}
```

Tool-call validation should happen before constructing `ScheduledJob`, so the LLM/user gets actionable errors:

- `unknown target_agent_role 'foo'; available roles: analyst, architect, developer, ...`
- `reasoning_effort='xhigh' is not supported for role 'qa' on endpoint 'kai-smart'; choose a codex/openai reasoning role or omit reasoning_effort`
- `thinking_level is only supported for claude-cli roles; role 'analyst' uses provider 'codex-cli'`
- `extra_env key 'OPENAI_API_KEY' is rejected; scheduled job extra_env is for non-secret runtime flags`

Update `_format_scheduled_job_summary()` and `/schedule list/show` output to include:

```text
role=analyst reasoning=xhigh thinking=inherit
```

## 5. WebSocket Protocol Envelopes

Additive only. Existing clients must continue to work when fields are missing.

Introduce a typed scheduled job wire shape:

```python
class ScheduledJobWire(ProtocolModel):
    id: str
    type: Literal["absolute", "cron", "event"]
    spec: dict[str, Any]
    prompt: str
    owner_session: str
    created_at: str
    created_by: Literal["user", "agent"]
    last_run: str | None = None
    next_run: str | None = None
    run_count: int = 0
    max_runs: int | None = None
    status: Literal["active", "paused", "completed", "failed", "cancelled", "quarantine"]
    last_result_preview: str | None = None
    concurrency: Literal["skip", "queue"]
    tool_budget: int | None = None
    target_agent_role: str | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env_keys: list[str] = []
```

Do not send raw `extra_env` values over WS.

Envelope updates:

- `ScheduledJobCreatedEnvelope.job`: use `ScheduledJobWire`.
- `ScheduledJobTriggeredEnvelope`: add optional routing fields:
  - `target_agent_role`
  - `effective_agent_role`
  - `reasoning_effort`
  - `effective_reasoning_effort`
  - `thinking_level`
  - `effective_thinking_effort`
  - `provider`
  - `model`
- `ScheduledJobCompletedEnvelope` and `ScheduledJobFailedEnvelope`: add the same optional routing fields so the UI can render what actually ran even if it missed the triggered event.
- Add `ScheduledJobQuarantinedEnvelope` if migration/load can quarantine jobs visible to the UI.
- Consider `ScheduledJobSkippedEnvelope` for `concurrency="skip"`; current skip behavior only logs and does not record a lifecycle event.

Server mapping changes:

- `_handle_scheduler_event()` should include `job.model_dump(mode="json")` for lifecycle events where useful.
- `_event_to_server_envelope()` should pass the new fields through to the protocol models.

TypeScript changes:

- Add `ScheduledJobWire` to `web/src/lib/daemon/types.ts`.
- Extend the `ScheduledJobEnvelope` union with the new optional fields.
- UI must treat missing fields as legacy/inherited.

## 6. UI Implications

Current web UI has scheduler event summaries and a command palette, not a first-class schedule-create form.

Recommended production UI:

1. Add REST endpoints for structured scheduler management:
   - `GET /api/scheduler/jobs?session=<name>&include_all=false`
   - `POST /api/scheduler/jobs`
   - `PATCH /api/scheduler/jobs/{job_id}`
   - `DELETE /api/scheduler/jobs/{job_id}` or keep cancel semantics with `POST /cancel`

2. Add a small schedule panel or modal in `web/src/routes/+page.svelte`:
   - schedule type segmented control: absolute, cron, event.
   - schedule spec inputs.
   - prompt textarea.
   - owner session defaulting to active session.
   - role dropdown sourced from `GET /api/models` `agents[]`.
   - reasoning dropdown: inherit, minimal, low, medium, high, xhigh.
   - thinking dropdown: inherit, normal, hard, ultra.
   - hide or disable incompatible effort controls based on selected role provider.
   - optional max runs, tool budget, concurrency.

3. Schedule list view:
   - show `owner_session`.
   - show `target_agent_role ?? "inherit"`.
   - show `reasoning_effort ?? "default"`.
   - show `thinking_level ?? "default"`.
   - show status, next run, run count, last result preview.
   - show quarantine rows with a clear error and no resume button until fixed.

4. Event panel:
   - update `schedulerSummary()` to include role/effort when present.
   - legacy events without fields should render exactly as today.

5. Command palette:
   - keep `/schedule list` suggestions.
   - add examples for role/effort once slash parsing supports flags:

```text
/schedule add cron "*/5 * * * *" "Run BIO Wyckoff analysis" --role analyst --reasoning xhigh
```

Maintain backward compatibility:

- Jobs without new fields display `role=inherit`, `reasoning=default`, `thinking=default`.
- Old daemons will not provide the new fields; new UI must not crash or hide scheduler activity.

## 7. Persistence and Migration

### 7.1 Current persistence

The scheduler currently persists to:

```text
workspaces/scheduler/jobs.json
```

The path is defined by `SCHEDULER_ROOT_DIR` and `SCHEDULER_JOBS_PATH`. `_persist_jobs()` writes:

```json
{
  "version": 1,
  "jobs": {
    "job_id": { "...": "ScheduledJob v1 fields" }
  }
}
```

### 7.2 Rollback-safe v2 store shape

Do not write the new fields inline under `jobs.<id>` yet.

Reason: the current v1 model has `extra="forbid"`. If a v2 job record contains unknown fields inline, a rollback to the current v1 daemon will reject/drop the job.

Use this v2 shape instead:

```json
{
  "version": 1,
  "schema_version": 2,
  "jobs": {
    "job_1": {
      "id": "job_1",
      "type": "cron",
      "spec": {"cron": "*/5 * * * *", "tz": "UTC"},
      "prompt": "Run BIO Wyckoff analysis",
      "owner_session": "terminal",
      "created_at": "2026-05-10T00:00:00+00:00",
      "created_by": "agent",
      "status": "active",
      "concurrency": "queue"
    }
  },
  "job_routing": {
    "job_1": {
      "target_agent_role": "analyst",
      "reasoning_effort": "xhigh",
      "thinking_level": null,
      "extra_env": null
    }
  },
  "quarantined_jobs": {}
}
```

The logical `ScheduledJob` model still has the new fields. The persistence adapter merges `jobs[job_id]` and `job_routing[job_id]` before validating the logical model.

Rollback behavior:

- v1 daemon ignores root-level `schema_version` and `job_routing`.
- v1 daemon validates `jobs.<id>` because it remains v1-shaped.
- v1 `_persist_jobs()` preserves unknown root keys because it reads the whole payload and only updates `version` and `jobs`.
- v2 daemon later re-merges surviving `job_routing` entries.
- If v1 removes a job, v2 should garbage-collect orphan `job_routing` entries on the next v2 persist.

This is the only way to satisfy both "persist v2 routing data" and "rollback to current v1 loads records" without first backporting a v1 compatibility patch.

### 7.3 Forward read

Read rules:

- Missing `schema_version` means v1.
- `schema_version == 1` means v1-compatible.
- `schema_version == 2` means merge `job_routing`.
- If `schema_version > SUPPORTED_SCHEDULER_SCHEMA_VERSION`, refuse to load the scheduler store and log a critical error. Do not partially schedule unknown future records.
- Accept inline v2 fields if an operator manually created them, but persist back to sidecar form.
- Routing sidecar wins over inline fields when both exist, because sidecar is the supported rollback-safe source.

### 7.4 Default backfill

On first v2 daemon start:

- Add `"schema_version": 2`.
- Add `job_routing[job_id]` for every existing job:

```json
{
  "target_agent_role": null,
  "reasoning_effort": null,
  "thinking_level": null,
  "extra_env": null
}
```

- Do not rewrite `jobs.<id>` with unknown v2 fields.
- Do not change job status, next run, run count, or prompt.
- The operation must be idempotent.

Existing BIO/BLEND every-five-minute Wyckoff jobs therefore keep firing as before after upgrade. They inherit the owner session agent until the operator edits them to add `target_agent_role="analyst"` and `reasoning_effort="xhigh"`.

### 7.5 One-shot migrator

Add:

```text
python -m daemon.scheduler migrate [--jobs-path PATH] [--dry-run]
```

Behavior:

- Acquire the same JSON file lock used by scheduler persistence.
- Read the whole store.
- Write a snapshot next to the store before any mutation:

```text
jobs.before-YYYYMMDD-HHMMSS.json
```

- Validate every core job plus sidecar routing against the v2 logical model.
- Add missing `job_routing` entries.
- Move structurally invalid jobs into `quarantined_jobs` while preserving raw JSON and validation errors.
- Never delete operator data. If a job is removed from the active `jobs` map because it cannot be scheduled, the raw record must exist in the snapshot and `quarantined_jobs`.
- Print a report:
  - total jobs scanned
  - migrated jobs
  - already-current jobs
  - quarantined jobs
  - orphan routing entries
  - output snapshot path
- Exit non-zero if any jobs were quarantined.
- Re-running with no changes must produce no diff except an optional report timestamp.

### 7.6 Schema versioning

Constants:

```python
SCHEDULER_STORE_SCHEMA_VERSION = 2
SCHEDULER_STORE_MIN_READ_VERSION = 1
```

Root fields:

- Keep `version: 1` for old reader compatibility.
- Add `schema_version: 2` for new reader compatibility.

Load policy:

- `schema_version` absent: treat as 1.
- `schema_version <= 2`: load.
- `schema_version > 2`: refuse to load; scheduler starts disabled or daemon startup fails according to operator preference.

### 7.7 Quarantine semantics

Add `status="quarantine"` for records that must not schedule.

Cases:

- Recoverable runtime validation failure:
  - Example: `target_agent_role="foo"` no longer exists.
  - Load as `ScheduledJob(status="quarantine")`.
  - Keep prompt/spec/routing visible.
- Schedule-shape validation failure:
  - Example: malformed cron expression from a lax v1 record.
  - Retry model validation with `status="quarantine"` and relaxed schedule validation so the operator can see and fix the record.
- Unrecoverable structural failure:
  - Example: missing `id` or `prompt`.
  - Store under `quarantined_jobs[job_id]` with raw JSON and errors.
  - Expose through REST/WS as a quarantine row.

The daemon must not crash because of one bad persisted job.

### 7.8 Fixture requirement

Add:

```text
tests/data/scheduled_jobs_v1.json
```

Include at least five legacy jobs:

- cron active every five minutes.
- cron paused.
- absolute future one-shot.
- absolute completed historical job.
- event job matching a `signals` channel/filter.

The v2 reader and migrator must load this fixture without operator intervention.

## 8. Tests

Add or extend these tests:

- `ScheduledJob` validates `target_agent_role`, `reasoning_effort`, `thinking_level`, and `extra_env`.
- Existing legacy job dicts without new fields still validate with fields as `None`.
- Validation rejects unknown `target_agent_role`.
- Validation rejects `reasoning_effort` on a non-reasoning endpoint.
- Validation rejects `thinking_level` on a non-Claude endpoint.
- Validation rejects both `reasoning_effort` and `thinking_level` on the same job.
- Dispatcher uses `target_agent_role` override and does not mutate `managed.session.agent_name`.
- Dispatcher falls back to owner session agent when `target_agent_role` is `None`.
- Dispatcher passes `reasoning_effort="xhigh"` into the Codex/ChatCodex endpoint config.
- Codex CLI subprocess adapter command includes `-c model_reasoning_effort=xhigh` where that adapter is used.
- Claude CLI adapter command includes `--effort high` or `--effort xhigh` for `hard`/`ultra`.
- OpenAI direct reasoning model path sends `reasoning.effort`.
- Backward compatibility: v1 jobs without new fields keep firing.
- Tool calls create jobs with `target_agent_role` and `reasoning_effort`.
- Tool-call validation returns actionable error strings.
- `/schedule list` and `/schedule show` include role/effort fields.
- WS envelope shape includes new optional fields and remains compatible when they are missing.
- TypeScript scheduled-job envelope types accept new fields.
- Migration: load `tests/data/scheduled_jobs_v1.json`, all jobs valid post-load.
- Migration: v1 -> v2 -> current v1 rollback loads the core `jobs` map because v2 fields are sidecar-rooted.
- Migration: malformed v1 cron becomes quarantine, daemon does not crash.
- Migration: re-running migrator is idempotent.
- Migration: higher `schema_version` refuses load with a clear error.
- Migration: `job_routing` orphan cleanup does not delete raw job data.

## 9. Acceptance Criteria

A scheduled job created with:

```json
{
  "target_agent_role": "analyst",
  "reasoning_effort": "xhigh"
}
```

must:

- Persist core job data and routing metadata correctly.
- Survive daemon restart.
- Fire on schedule.
- Run as the `analyst` role, not as `kai`.
- Use Codex/OpenAI reasoning effort `xhigh` for that turn.
- Leave the owner session's default agent unchanged.
- Emit telemetry showing owner session, target role, effective role, provider, model, requested effort, and effective effort.
- Include the new fields in created/triggered/completed/failed WS envelopes.
- Show role and effort in the web schedule list and scheduler event summary.
- Keep existing legacy jobs firing with inherited owner-session behavior.
- Quarantine invalid persisted jobs without crashing the daemon.

The BIO/BLEND Wyckoff jobs already visible today must survive the upgrade with no manual operator fix-up.

## 10. Phased Rollout

### Phase 1 - Schema, rollback-safe persistence, dispatcher

Scope:

- Logical `ScheduledJob` fields.
- `JobStatus` quarantine support.
- v2 store reader/writer with root `job_routing`.
- Migrator CLI and fixture.
- Runtime validation helper.
- Per-call runner override path.
- Reasoning/thinking propagation into runner endpoint config.
- Core telemetry.

Exit criteria:

- Existing jobs survive restart.
- New `analyst` + `xhigh` jobs dispatch correctly in tests.
- v1 rollback test passes through sidecar persistence.
- No UI dependency required.

Estimated implementation time: 2.5 to 3.5 focused dev days, plus review.

### Phase 2 - Tools, REST API, WebSocket shape

Scope:

- Add generic `create_scheduled_job` tool.
- Extend existing create tools.
- Add structured scheduler REST endpoints.
- Extend WS envelopes and TypeScript types.
- Extend slash `/schedule` flags and summaries.

Exit criteria:

- Kai can create the BIO/BLEND jobs with role/effort using tools.
- WebSocket clients receive role/effort metadata.
- Legacy command palette and slash workflows keep working.

Estimated implementation time: 1.5 to 2.5 dev days.

### Phase 3 - Web UI and hardening

Scope:

- Schedule create/edit UI.
- Schedule list role/effort display.
- Quarantine repair UI or at least clear read-only quarantine display.
- Environment-gated smoke tests for local Codex/Claude CLI flags.
- Telemetry/cost attribution verification.

Exit criteria:

- Operator can create analyst/xhigh jobs without asking the LLM to infer flags.
- UI displays inherited/default values cleanly for legacy jobs.
- Smoke tests prove CLI flags on the target host.

Estimated implementation time: 1.5 to 2.5 dev days.

## 11. Open Questions for Operator

1. Should job-level `reasoning_effort` be strict, as recommended here, or should it mimic `/think` and allow pre-staging on endpoints that do not currently honor reasoning?
2. Should `thinking_level="ultra"` map to Claude CLI `--effort xhigh` or `--effort max`?
3. Should target-role scheduled output share the owner session chat history, as recommended here, or write into the role's sub-agent buffer and only summarize back to the owner session?
4. Is `extra_env` allowed only for `KAI_*` non-secret flags, or does the operator need broader prefixes such as `CODEX_*` or `CLAUDE_*`?
5. Should a missing target role at dispatch time fail the job once, or quarantine it until the operator fixes config?
6. Should `concurrency="skip"` emit a first-class `scheduled_job_skipped` WS event? This spec recommends yes.

## Implementation Risks

1. Rollback compatibility is easy to break if implementers write new fields inline under `jobs.<id>`. Use root `job_routing` until v1 loaders are known to ignore unknown job fields.
2. Per-call role overrides must not mutate `AGENTS`, `Session.agent_name`, `Session.agent_runner`, or process-global environment. Any mutation here will leak role/effort into normal user turns.
3. Provider capability detection must be explicit. Sending `reasoning` to generic OpenAI-compatible/local endpoints can fail requests; accepting reasoning on unsupported endpoints silently defeats the feature.

## Concrete Example

Create:

```json
{
  "type": "cron",
  "spec": {"cron": "*/5 * * * *", "tz": "America/New_York"},
  "prompt": "Run BIO and BLEND Wyckoff analysis and summarize actionability.",
  "owner_session": "terminal",
  "target_agent_role": "analyst",
  "reasoning_effort": "xhigh",
  "thinking_level": null,
  "extra_env": null,
  "concurrency": "skip",
  "tool_budget": 25
}
```

Expected fire:

- owner session: `terminal`
- effective role: `analyst`
- provider: `codex-cli`
- model: `gpt-5.5` or configured analyst model
- reasoning: `xhigh`
- owner session default agent after run: unchanged
- job record after run: `run_count += 1`, `last_run` set, `last_result_preview` set

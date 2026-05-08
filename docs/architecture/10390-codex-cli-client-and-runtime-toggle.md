# Architecture Artifact — Task 10390

## Title
Codex-CLI auto-loop-brain client adapter and runtime daemon UI toggles

## Status
Read-only architecture specification. No implementation code, commits, or tickets were created.

## Context inspected

Relevant repository artifacts reviewed:

- `agent/auto_loop_brain.py`
  - Existing `ToollessLLMClient` protocol.
  - Existing `ClaudeCLIToollessLLMClient`, `OpenAICompatToollessLLMClient`, and `AnthropicToollessLLMClient`.
  - Existing `AutoLoopBrainConfig`, `build_toolless_llm_client()`, strict JSON parser/validator path, redaction, caps, and fail-closed behavior.
  - Current constants: `VALID_AUTO_LOOP_BRAIN_CLIENTS=("claude-cli", "openai", "anthropic")`, default client `claude-cli`, default model `sonnet`.
- `tests/test_auto_loop_brain.py`
  - Existing test shape for CLI subprocess command construction, mocked subprocess failures, OpenAI-compatible payloads with no tool keys, routing validation, and fail-closed evaluator behavior.
- `agent-config.json`
  - Current `daemon.auto_loop_brain` block is disabled by default with `client: "claude-cli"`, `model_id: "sonnet"`.
  - `endpoints.codex-cli` exists with provider `codex-cli` and default model `gpt-5.5`.
  - `daemon.alert_subscriber` exists with global and per-subscription enabled flags, disabled by default.
- `docs/architecture/10381-auto-loop-brain-client-redesign.md`
  - Prior architecture deliberately added config-driven client routing while keeping Claude CLI, OpenAI-compatible, and Anthropic clients valid.
  - Prior spec punted Codex CLI as optional Phase 3.
- `docs/architecture/10387-alert-subscriber.md`, `daemon/alert_subscriber.py`, `daemon/event_injector.py`, `daemon/server.py`
  - Alert subscriber config model exists, but current repo has config/loading scaffolding rather than full subscriber service lifecycle.
  - Heartbeat and `EventInjector` are the operational pattern for daemon-owned hot services.
- `daemon/server.py`, `daemon/core.py`
  - `Session` constructs `auto_response_evaluator` at session construction time.
  - `DaemonServer.require_http_auth()` is the existing REST auth gate for `/api/sessions`, `/api/models`, health, metrics, and UI state endpoints.
- `web/src/routes/+page.svelte`, `web/src/lib/daemon/client.ts`, `web/src/lib/daemon/types.ts`
  - Current KAI web UI has a dashboard command bar with model controls and existing authenticated REST client helpers.
- `~/git/CODEX-AGENT-USAGE.md` and local `codex exec --help`
  - Guide recommends `codex exec --dangerously-bypass-approvals-and-sandbox -c model_reasoning_effort=<level> -c model="<model>" <prompt>` for non-interactive automation.
  - Current CLI help exposes `--json`, `--output-schema <FILE>`, and `--output-last-message <FILE>`; it does **not** expose `--output-format json`.

## Executive recommendation

Ship the two requested capabilities in **separate implementation PRs** behind one architecture umbrella:

1. **PR A — Codex CLI client adapter and default flip**
   - Add `CodexCLIToollessLLMClient` as a fourth `ToollessLLMClient` implementation.
   - Extend config routing to `client ∈ {claude-cli, openai, anthropic, codex-cli}`.
   - Add `daemon.auto_loop_brain.codex_reasoning_effort` and `KAI_AUTO_LOOP_BRAIN_CLIENT=codex-cli` support.
   - Flip default `daemon.auto_loop_brain.client` from `claude-cli` to `codex-cli` and default `model_id` from `sonnet` to `gpt-5.5`.
   - Keep `enabled: false` by default. Operators without a valid Codex CLI login can still run the daemon; they just cannot enable the brain until they either authenticate Codex CLI or switch the client to `claude-cli`, `openai`, or `anthropic`.

2. **PR B — Runtime persisted daemon toggle + minimal UI**
   - Add a daemon runtime configuration layer using a separate `runtime_overrides.json` file.
   - Add authenticated `GET/PATCH /api/daemon/config/auto_loop_brain`.
   - Optionally add authenticated `GET/PATCH /api/daemon/config/alert_subscriber` and per-subscription toggle endpoints in a follow-up slice.
   - Add a minimal KAI web command-bar/settings toggle backed by those endpoints.
   - Hot-flip live sessions without daemon restart.

This phasing reduces blast radius. PR A is mostly local to the auto-loop-brain client factory and tests. PR B touches daemon lifecycle, persistence, REST API, concurrency, and Svelte UI state.

---

# Feature 1 — Codex CLI client adapter

## Goal

Make `codex-cli` the default backend for the auto-loop-brain critic while preserving every existing backend and fail-closed safety property from #10381/#10388.

The auto-loop-brain remains a classifier, not an actor. It receives a bounded, redacted prompt and must return one strict JSON decision. The daemon still validates and gates the decision.

## Public configuration contract

Update constants and config model:

```python
VALID_AUTO_LOOP_BRAIN_CLIENTS = ("claude-cli", "openai", "anthropic", "codex-cli")
DEFAULT_AUTO_LOOP_BRAIN_CLIENT = "codex-cli"
DEFAULT_CRITIC_MODEL = "gpt-5.5"
DEFAULT_CODEX_REASONING_EFFORT = "medium"
VALID_CODEX_REASONING_EFFORTS = ("medium", "high", "xhigh")
```

Extend `AutoLoopBrainConfig`:

```python
@dataclass(frozen=True)
class AutoLoopBrainConfig:
    enabled: bool = False
    client: str = DEFAULT_AUTO_LOOP_BRAIN_CLIENT
    endpoint: str | None = None
    model_id: str = DEFAULT_CRITIC_MODEL
    codex_reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT
    ... existing fields ...
```

Add environment override:

- `KAI_AUTO_LOOP_BRAIN_CLIENT=codex-cli`
- `KAI_AUTO_LOOP_BRAIN_CODEX_REASONING_EFFORT=medium|high|xhigh`

Config block target:

```json
{
  "daemon": {
    "auto_loop_brain": {
      "enabled": false,
      "client": "codex-cli",
      "endpoint": null,
      "model_id": "gpt-5.5",
      "codex_reasoning_effort": "medium",
      "max_history_tokens": 16000,
      "temperature": 0.0,
      "min_continue_confidence": 0.85,
      "timeout_seconds": 20.0,
      "max_output_tokens": 512,
      "max_llm_critic_calls_per_session": 20,
      "max_consecutive_llm_critic_calls": 5
    }
  }
}
```

## Routing contract

`build_toolless_llm_client(config, raw_config=None)` should route as follows:

```python
if client == "claude-cli":
    return ClaudeCLIToollessLLMClient()
if client == "codex-cli":
    return CodexCLIToollessLLMClient(reasoning_effort=config.codex_reasoning_effort)
if client == "anthropic":
    return AnthropicToollessLLMClient()
if client == "openai":
    ... existing endpoint validation ...
```

Additional routing validation:

- `client=openai` must require `daemon.auto_loop_brain.endpoint` and reject endpoints whose configured `provider` is not `openai` when a provider field is present. In particular, `endpoint=codex-cli` must not accidentally be treated as an OpenAI-compatible endpoint.
- `client=codex-cli` should not require `endpoint`; it uses the local `codex` executable/session.
- Unknown client names remain startup/config errors when enabled, and should fail closed if discovered during a runtime enable probe.

## New class: `CodexCLIToollessLLMClient`

Add to `agent/auto_loop_brain.py`, mirroring `ClaudeCLIToollessLLMClient`:

```python
class CodexCLIToollessLLMClient:
    """Local Codex CLI client for one non-interactive JSON classifier call."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT,
    ) -> None: ...

    def build_command(self, *, model: str, prompt: str) -> list[str]: ...

    def complete_json(... ) -> LLMResult: ...
```

### Prompt construction

Unlike Claude CLI, Codex `exec` does not have an `--append-system-prompt` equivalent. Build one complete classifier prompt from the existing `system` and `user` arguments:

```text
<System instructions>
{system}

<User payload>
{user}
```

The implementation should pass this as the non-interactive prompt. Prefer `subprocess.run(..., input=prompt, text=True, capture_output=True, shell=False, timeout=timeout, check=False)` and use `-` as the prompt argument where supported so very large classifier prompts are not placed on the process argv. If implementation agents choose to pass the prompt as the final positional argument to exactly match the guide, they must still avoid `shell=True` and must test large-prompt behavior.

### Subprocess invocation

The user-requested baseline, from `~/git/CODEX-AGENT-USAGE.md`, is:

```bash
codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  -c model_reasoning_effort=<level> \
  -c model="<model>" \
  <prompt>
```

Recommended command builder:

```python
[
    executable,
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
    "-c", f"model_reasoning_effort={reasoning_effort}",
    "-c", f"model={json.dumps(model)}",  # or --model model if tested
    "-",
]
```

Notes:

- The current local `codex exec --help` also supports `--model <MODEL>`, `--ephemeral`, `--ignore-rules`, `--output-schema <FILE>`, `--output-last-message <FILE>`, and `--json`.
- There is no `--output-format json` flag in the inspected CLI. Do **not** design around `--output-format json` unless a future implementation confirms it exists.
- `--json` means event-stream JSONL, not necessarily “the model’s final classifier JSON object.” It is not the primary classifier response channel.
- If implementation uses `--output-last-message <tempfile>`, parse that file as the final assistant response and treat stdout/stderr as diagnostic only. This is safer if current Codex emits banners or JSONL events. If implementation captures stdout directly, tests must cover stdout contamination and fail closed.
- If implementation uses `--output-schema`, use a checked-in minimal schema for the final response shape, but still run the existing `parse_auto_evaluation_decision()` and `validate_auto_evaluation_decision()` because provider-side schema enforcement is not a substitute for daemon-side validation.

### Tool-less guarantee and caveat

Codex CLI `exec` is non-interactive, but it is still an agent CLI. The inspected help does not expose a true “disable all tools/shell” flag. Therefore the practical guarantee is:

- The daemon implementation does not pass any tool-enabling flags.
- The prompt states the classifier has no tools and must return only JSON.
- The daemon supplies no task requiring filesystem/network interaction.
- The subprocess is bounded by timeout.
- The daemon accepts only the final JSON text and ignores any attempted tool/action content.
- Any non-JSON, tool-attempt marker, nonzero exit, timeout, missing binary, or schema/validation failure collapses to the existing fail-closed STOP path.

Security tradeoff: `--dangerously-bypass-approvals-and-sandbox` matches the agents-authored guide and avoids non-interactive approval hangs, but it is more permissive than ideal for a classifier. If future Codex CLI adds a no-tool/no-shell flag, adopt it immediately and add a regression test that the command includes it.

### Failure behavior

`CodexCLIToollessLLMClient.complete_json()` must raise provider/client exceptions, not return partial decisions, for:

- `FileNotFoundError` / missing `codex` executable.
- `subprocess.TimeoutExpired` -> `TimeoutError` with a safe message.
- Nonzero return code.
- Empty final response.
- Stderr that indicates authentication/session failure. Do not print secrets or raw auth headers.
- Output schema/result-file read errors if that path is used.

`LLMCriticEvaluator.evaluate()` already catches generic exceptions and returns a conservative STOP. Keep that public behavior unchanged.

### Boot probe

For runtime enable and startup validation, add a cheap probe path:

- Build configured client through the same factory.
- Call `complete_json()` with a tiny classifier prompt and short timeout, for example 10 seconds:

```json
{"decision":"STOP","confidence":1.0,"reason":"boot probe","pattern":"unknown","auto_reply_template":null}
```

- Require the response to parse and validate.
- If probe fails while enabling, reject the enable operation and leave effective config unchanged.
- If probe fails on daemon startup and `enabled=true`, log a redacted error and start with the brain disabled or fail startup only if an explicit strict env var is introduced later. Default should favor daemon availability.

## Implementation touch points

- `agent/auto_loop_brain.py`
  - Constants/defaults.
  - `AutoLoopBrainConfig.codex_reasoning_effort` parsing/validation.
  - `CodexCLIToollessLLMClient`.
  - `build_toolless_llm_client()` routing.
  - Optional provider validation for OpenAI-compatible endpoints.
- `agent-config.json`
  - Default client/model/reasoning-effort update while keeping `enabled: false`.
- `tests/test_auto_loop_brain.py`
  - Add Codex CLI tests matching the existing Claude CLI test style.

## Tests for Feature 1

Add unit tests with mocked subprocess:

1. Command shape:
   - Starts with `codex exec`.
   - Includes `--dangerously-bypass-approvals-and-sandbox`.
   - Includes configured `model_reasoning_effort`.
   - Includes configured model.
   - Uses `shell=False`.
   - Does not include any explicit tool-enabling flag.
2. Stdin/response contract:
   - System and user content both reach the subprocess prompt/input.
   - Successful JSON response returns `LLMResult(text=..., model_id=model, usage=TokenUsage())`.
3. Failure paths:
   - Timeout raises `TimeoutError`.
   - Nonzero exit raises `RuntimeError` with redacted/safe text.
   - Missing executable raises a provider error that evaluator converts to STOP.
   - Stdout/stderr noise or malformed JSON is fail-closed by evaluator.
4. Config/routing:
   - `AutoLoopBrainConfig.from_sources()` accepts `client=codex-cli` and env override.
   - Invalid `codex_reasoning_effort` rejects or normalizes to `medium` with a warning; prefer reject on explicit invalid config.
   - Factory returns `CodexCLIToollessLLMClient` for `client=codex-cli`.
   - Existing `claude-cli`, `openai`, and `anthropic` tests still pass.
5. Default flip:
   - Default config is `client=codex-cli`, `model_id=gpt-5.5`, `codex_reasoning_effort=medium`.
   - `enabled=false` config does not require a working Codex session for daemon/session construction if PR B lazy-init change is included. If PR A ships first without lazy-init, keep the client construction behavior documented and test startup expectations carefully.

---

# Feature 2 — Runtime persisted UI toggle

## Goal

Allow an operator to turn `daemon.auto_loop_brain.enabled` on/off from the KAI web UI and REST API without restarting the daemon, while persisting the setting across daemon restarts.

Secondary goal: use the same state model for `daemon.alert_subscriber.enabled` and per-subscription `enabled` toggles, but ship those after the auto-loop-brain toggle unless implementation bandwidth permits.

## State model decision

Choose **option (b): separate `runtime_overrides.json`**.

### Why not option (a), write-through to `agent-config.json`?

Pros:

- Simple mental model: one file contains the operator-visible config.
- Existing config loader already knows `agent-config.json`.

Cons:

- Rewrites a tracked source/config file on every UI toggle.
- Creates race conditions with human edits, deploy updates, and git status.
- `config.py` loads parts of `agent-config.json` at import time into module globals, so write-through alone is not a reliable hot-reload mechanism.
- Requires careful atomic file write and formatting preservation to avoid config churn.

### Why not option (c), in-memory only?

Pros:

- Fastest to implement.
- No file races.

Cons:

- Explicitly fails the requirement: config change must persist across restarts.
- UI state would surprise operators after daemon restart.

### Recommended model: `runtime_overrides.json`

Pros:

- Clean separation between baseline config and operator runtime state.
- Persistence without modifying the source-controlled baseline file.
- Easy to audit and reset.
- Can support future runtime toggles without broad config rewrites.

Cons:

- New file, merge layer, and tests.
- Must define precedence and atomic write behavior.

## Runtime override file contract

Default path:

```text
workspaces/runtime_overrides.json
```

Allow env override for tests/deployments:

```text
KAI_RUNTIME_OVERRIDES_PATH=/path/to/runtime_overrides.json
```

Schema v1:

```json
{
  "version": 1,
  "updated_at": "2026-05-08T00:00:00Z",
  "updated_by": "api",
  "daemon": {
    "auto_loop_brain": {
      "enabled": true
    },
    "alert_subscriber": {
      "enabled": false,
      "subscriptions": {
        "polymarket-default": {
          "enabled": true
        }
      }
    }
  }
}
```

Precedence:

```text
code defaults < agent-config.json < runtime_overrides.json < environment overrides / kill switches
```

Environment variables remain authoritative. In particular:

- `KAI_AUTO_LOOP_BRAIN_KILL_SWITCH=true` forces effective disabled, even if runtime override says enabled.
- `KAI_ALERT_SUBSCRIBER_KILL_SWITCH=true` forces effective disabled.

Atomic persistence:

- Serialize with deterministic JSON indentation.
- Write to a temp file in the same directory.
- `fsync` file, `os.replace(temp, target)`, then best-effort `fsync` directory.
- Protect read/modify/write with an `asyncio.Lock` or server-thread lock.
- On corrupt JSON at startup, rename to `runtime_overrides.json.corrupt.<ts>`, log, and continue with baseline config disabled rather than crashing the daemon.

## New daemon component: `RuntimeConfigStore`

Add `daemon/runtime_config.py`:

```python
@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    version: int
    updated_at: str | None
    updated_by: str | None
    overrides: dict[str, Any]

class RuntimeConfigStore:
    def __init__(self, path: Path): ...
    def load(self) -> RuntimeConfigSnapshot: ...
    def effective_agent_config(self, base: dict[str, Any]) -> dict[str, Any]: ...
    async def patch_auto_loop_brain(self, *, enabled: bool, actor: str) -> RuntimeConfigSnapshot: ...
    async def patch_alert_subscriber(...): ...
```

Keep the store small and typed around the toggles; do not build a generic arbitrary config mutation API in v1.

## Effective config API

### Auto-loop-brain

`GET /api/daemon/config/auto_loop_brain`

Response:

```json
{
  "auto_loop_brain": {
    "enabled": false,
    "effective_enabled": false,
    "enabled_source": "agent-config|runtime-overrides|env|kill-switch",
    "client": "codex-cli",
    "endpoint": null,
    "model_id": "gpt-5.5",
    "codex_reasoning_effort": "medium",
    "kill_switch_active": false,
    "runtime_override": {"enabled": false},
    "last_updated_at": "2026-05-08T00:00:00Z",
    "last_updated_by": "api"
  }
}
```

`PATCH /api/daemon/config/auto_loop_brain`

Request:

```json
{"enabled": true}
```

Response: same shape as GET after the flip.

Validation:

- Body forbids extra fields in v1.
- `enabled` is required and must be boolean.
- If enabling and a kill switch/env override forces disabled, return `409 Conflict` with safe detail or return `200` with `effective_enabled=false` and `kill_switch_active=true`. Prefer `409` because the operator action did not take effect.
- If enabling and client probe fails, return `503 Service Unavailable` or `400 Bad Request` with redacted reason and leave prior state unchanged. Prefer `503` for missing local CLI/auth/transient backend failure.
- Disabling should not require backend probe.

### Alert subscriber follow-up endpoint

If included in PR B or a follow-up PR:

`GET /api/daemon/config/alert_subscriber`

`PATCH /api/daemon/config/alert_subscriber`

Global body:

```json
{"enabled": true}
```

Per-subscription body:

```json
{"subscriptions": {"polymarket-default": {"enabled": true}}}
```

Alternative REST shape for per-subscription toggles:

```http
PATCH /api/daemon/config/alert_subscriber/subscriptions/{name}
{"enabled": true}
```

Prefer the explicit per-subscription endpoint for UI simplicity and clearer audit events.

## Authorization

Use the existing daemon REST authorization path:

```python
daemon_server.require_http_auth(request)
```

That matches current `/api/sessions`, `/api/models`, health, metrics, chart UI, and watchlist UI behavior.

Accepted actors today are:

- Local unauthenticated calls when `allow_unauthenticated_local=True`.
- Daemon bearer token.
- Existing gateway tokens accepted by `_token_is_accepted()`.

Taskboard bearer/session-token scoping is not a distinct daemon auth primitive in the inspected code. If deployment maps the taskboard bearer into `AGENT_GATEWAY_TOKEN` or equivalent, it works through the existing path. If multi-tenant scoping is introduced later, this endpoint needs role-scoped authorization and tenant/session ownership checks.

Never log or return bearer tokens, session tokens, HMAC secrets, Authorization headers, or raw signed webhook bodies.

Actor derivation for audit:

- If authenticated via local bypass: `actor="local"`.
- If bearer/gateway token: `actor="api"` unless future auth middleware supplies a principal.
- If Web UI calls the same API, actor remains `api` in v1; include optional header `X-KAI-Actor: web-ui` only if validated/sanitized and never trusted for authorization.

## Hot-flip mechanism: auto-loop-brain

Current issue: `Session.__init__` constructs `auto_response_evaluator` once with a frozen config. A persisted toggle cannot simply change a file; it must update live sessions and affect new sessions.

Add a daemon-owned coordinator, either as methods on `DaemonServer` or a small `AutoLoopBrainRuntime` class:

```python
class AutoLoopBrainRuntime:
    def __init__(self, store: RuntimeConfigStore, base_config_loader: Callable[[], dict]): ...
    def effective_config(self) -> AutoLoopBrainConfig: ...
    async def enable(self, actor: str) -> AutoLoopBrainConfig: ...
    async def disable(self, actor: str) -> AutoLoopBrainConfig: ...
```

### Enable flow: false -> true

1. Acquire runtime-config lock.
2. Load base `agent-config.json` and runtime overrides, then compute candidate `AutoLoopBrainConfig(enabled=True)` with env/kill-switch applied.
3. If kill switch forces disabled, reject with `409`.
4. Instantiate configured client via existing factory.
5. Run boot probe.
6. Persist runtime override `daemon.auto_loop_brain.enabled=true` atomically.
7. Build a fresh evaluator for each live session:
   - `build_auto_response_evaluator(chat_history_provider=lambda session=session: tuple(session.chat_history), telemetry=session, config=candidate_config)`
   - Assign `session.auto_response_evaluator = new_evaluator` under the session/input lock or a dedicated evaluator lock.
8. Update daemon effective config cache.
9. Publish telemetry event `auto.loop_brain_toggle` with `{from:false,to:true,actor,ts}`.
10. Return effective config response.

### Disable flow: true -> false

1. Acquire runtime-config lock.
2. Persist runtime override `daemon.auto_loop_brain.enabled=false` atomically.
3. For each live session, either:
   - Replace evaluator with a new `LLMCriticEvaluator` using `config.enabled=false`; or
   - Call a `session.set_auto_response_evaluator()` helper that swaps to disabled config.
4. Drain in-flight critic calls:
   - Because the current evaluator call is synchronous inside an agent loop, do not cancel a thread/process mid-call in v1.
   - Use a generation counter: disable sets `auto_loop_brain_generation += 1`; any result from an older generation is ignored unless it is a conservative STOP.
   - New evaluations after the swap must not call the LLM.
   - If implementation can reliably acquire the session `input_lock`, do so for a clean drain; otherwise use generation gating to avoid UI/API deadlock while an LLM call times out.
5. Publish telemetry event `auto.loop_brain_toggle` with `{from:true,to:false,actor,ts}`.
6. Return effective config response.

### New sessions

In `DaemonServer.get_or_create_session()` after constructing `Session`, apply the current effective auto-loop-brain config before exposing the session:

```python
session.set_auto_response_evaluator_config(self.auto_loop_brain_runtime.effective_config())
```

This prevents newly created sessions from reverting to stale import-time config.

### Startup

At daemon startup:

1. Create `RuntimeConfigStore`.
2. Load baseline config and overrides.
3. Compute effective auto-loop-brain config.
4. If effective enabled, run boot probe before accepting it.
5. If probe fails, log redacted warning, expose `effective_enabled=false`, and include `last_error` in health/config response without crashing daemon by default.

## Hot-flip mechanism: alert subscriber

Alert subscriber can use the same runtime override state model, but it has a different lifecycle:

- Global false -> true:
  - Reload effective `AlertSubscriberConfig`.
  - Validate enabled subscription templates.
  - Instantiate/start `AlertSubscriberService` if fully implemented.
  - Subscribe to NATS subjects for enabled subscriptions.
- Global true -> false:
  - Stop subscriber service.
  - Unsubscribe/drain callbacks.
  - Do not process new alerts.
- Per-subscription false -> true:
  - Validate one subscription.
  - Subscribe it if global enabled and bus connected.
- Per-subscription true -> false:
  - Unsubscribe it.
  - Let any already scheduled `EventInjector` turn finish or be dropped by its normal busy-session gates.

Given the current inspected repo only has alert subscriber config/prompt scaffolding, do not block auto-loop-brain toggle on full alert subscriber toggles. Spec it as Phase B2/B3.

## Telemetry

Emit daemon/session event on every successful flip:

Topic:

```text
auto.loop_brain_toggle
```

Payload:

```json
{
  "from": false,
  "to": true,
  "actor": "api",
  "ts": "2026-05-08T00:00:00Z",
  "client": "codex-cli",
  "model_id": "gpt-5.5",
  "codex_reasoning_effort": "medium",
  "source": "runtime-overrides"
}
```

For alert subscriber:

```text
alert_subscriber.toggle
```

Payload:

```json
{
  "from": false,
  "to": true,
  "actor": "api",
  "ts": "2026-05-08T00:00:00Z",
  "subscription": "polymarket-default"
}
```

Expose current effective state in `/api/health` and `/api/metrics`:

```json
{
  "auto_loop_brain": {
    "enabled": true,
    "effective_enabled": true,
    "client": "codex-cli",
    "model_id": "gpt-5.5",
    "last_toggle_at": "...",
    "last_error": null
  }
}
```

## UI design

Minimal UI placement: KAI web dashboard command bar, near the existing model picker and Stop/Disconnect controls.

Interaction:

- Label: `Brain` or `Auto-loop brain`.
- Toggle state reflects `GET /api/daemon/config/auto_loop_brain` effective state.
- Tooltip/subtext shows client/model: `codex-cli / gpt-5.5 / medium`.
- If kill switch active, toggle is disabled and label shows `Brain locked off`.
- On click:
  - Disable control and show `Enabling...` or `Disabling...`.
  - Call `PATCH /api/daemon/config/auto_loop_brain` with `{enabled: next}`.
  - On success, refresh effective state from response.
  - On failure, revert UI to server state and show a short safe error such as `Codex CLI auth unavailable`.
- Avoid optimistic permanent state. Temporary optimistic animation is fine only if failure reverts immediately.

Implementation touch points:

- `web/src/lib/daemon/types.ts`
  - Add `AutoLoopBrainConfigResponse` and request types.
- `web/src/lib/daemon/client.ts`
  - Add `fetchAutoLoopBrainConfig(token)` and `updateAutoLoopBrainConfig(enabled, token)`.
- `web/src/routes/+page.svelte`
  - Add state variables: `autoLoopBrainConfig`, `isTogglingBrain`, `brainToggleError`.
  - Fetch on mount and after reconnect.
  - Render toggle in `.dashboard-actions` or a compact settings drawer.
- Tests:
  - `web/src/lib/daemon/client.test.ts` for GET/PATCH URLs/auth/body.
  - `web/src/routes/page.test.ts` for toggle disabled/loading/error states if current test harness supports it.

## Backend API implementation touch points

- `daemon/server.py`
  - Pydantic request model:

    ```python
    class AutoLoopBrainConfigPatch(BaseModel):
        model_config = ConfigDict(extra="forbid")
        enabled: bool
    ```

  - Routes:

    ```python
    @app.get("/api/daemon/config/auto_loop_brain")
    async def get_auto_loop_brain_config(request: Request): ...

    @app.patch("/api/daemon/config/auto_loop_brain")
    async def patch_auto_loop_brain_config(request: Request, payload: AutoLoopBrainConfigPatch): ...
    ```

  - Use `require_http_auth()`.
  - Return only safe config fields.
- `daemon/core.py`
  - Add a session method for evaluator replacement, or make evaluator construction lazy from a daemon-supplied provider.
- `daemon/runtime_config.py`
  - Runtime override store and atomic persistence.
- `tests/test_daemon_runtime_config.py` or similar
  - Store merge/persistence/corrupt-file behavior.
- `tests/test_daemon_server.py`
  - Auth, GET/PATCH shape, hot propagation to live and new sessions, probe failure behavior.

---

# Phased ticket plan

Do not file these automatically; this is the recommended implementation sequence.

## Phase 0 — Preflight and CLI contract spike

Scope:

- Confirm exact Codex CLI flags in the target runtime.
- Decide final response channel: stdout-only vs `--output-last-message` file.
- If using schema, check in a minimal JSON schema and verify CLI supports `--output-schema` in deployed version.

Decision criteria:

- `codex exec` can return a one-shot JSON classifier response in a test environment.
- Timeout/nonzero/missing auth failures are deterministic and redacted.

ETA: 0.5 dev day + quick CR.

## Phase A — Codex CLI client adapter and default flip

Scope:

- Add `CodexCLIToollessLLMClient`.
- Extend config/env/defaults.
- Add routing tests and fail-closed tests.
- Update `agent-config.json` default client/model while preserving `enabled:false` and existing clients.

Decision criteria:

- Unit tests for all four clients pass.
- Existing #10388 behavior for `claude-cli`, `openai`, `anthropic` remains valid.
- Daemon can start with `enabled:false` even if Codex CLI auth is absent.

ETA: 1 dev day + 0.5 CR + 0.5 SA/QA = ~2 working days serial.

## Phase B1 — Runtime override store and auto-loop-brain API

Scope:

- Add `RuntimeConfigStore`.
- Add GET/PATCH auto-loop-brain endpoints.
- Add hot propagation to live/new sessions.
- Add boot probe on enable.
- Add telemetry and health/metrics fields.

Decision criteria:

- Toggle persists across daemon restart.
- `agent-config.json` is not modified by toggles.
- Enable failure leaves prior state intact.
- Disable prevents new LLM critic calls without daemon restart.
- Kill switch remains authoritative.

ETA: 2 dev days + 1 CR + 0.5 SA + 1 QA = ~4.5 working days serial.

## Phase B2 — Minimal KAI web UI toggle

Scope:

- Add client types/methods.
- Add command-bar/settings toggle.
- Add loading/error/kill-switch states.
- Add UI tests.

Decision criteria:

- UI reflects effective backend state after page load, after successful toggle, and after failed toggle.
- Button cannot desync permanently from server state.
- Auth failures show safe error.

ETA: 1 dev day + 0.5 CR + 0.5 QA = ~2 working days serial.

## Phase B3 — Alert subscriber runtime toggles (optional follow-up)

Scope:

- Extend runtime overrides to global and per-subscription alert subscriber enabled flags.
- Add REST endpoints.
- Wire to actual subscriber lifecycle once `AlertSubscriberService` is complete.
- Add UI controls in settings drawer rather than crowding command bar.

Decision criteria:

- Global and per-subscription toggles persist.
- NATS subscriptions start/stop without restart.
- No malformed/backlogged alerts are injected during disable.

ETA: 2-3 dev days + 1 CR + 0.5 SA + 1 QA = ~4.5-5.5 working days serial.

## Total ETA

For Codex client + auto-loop-brain runtime UI toggle only, assuming serial dev/CR/SA/QA and no fix loops:

- Phase 0: ~0.5-1 day
- Phase A: ~2 days
- Phase B1: ~4.5 days
- Phase B2: ~2 days

Realistic total: **~9 working days**.

If alert subscriber global/per-subscription toggles are included in the same release: **~13-15 working days**.

---

# Risks and mitigations

## Codex CLI flag drift

Risk: Codex CLI flags change; `--output-format json` does not exist in the inspected version; `--json` semantics may change.

Mitigations:

- Centralize command construction in `CodexCLIToollessLLMClient.build_command()`.
- Unit test exact argv shape.
- Add a small `codex exec --help` compatibility note to release docs.
- Prefer `--output-last-message` if stable; otherwise strict stdout parser with fail-closed behavior.
- Keep `claude-cli`, `openai`, and `anthropic` clients as supported fallbacks.

## Codex CLI is not a pure tool-less API

Risk: `codex exec` is an agent CLI and no explicit no-tools flag was found.

Mitigations:

- Do not pass tool-enabling flags.
- Bound prompt, timeout, and output.
- Existing strict JSON parser/validator is the security boundary.
- Fail closed on anything except valid classifier JSON.
- Revisit if Codex CLI adds a no-tool/no-shell flag.

## Runtime config-file race conditions

Risk: Concurrent toggles or human edits corrupt/overwrite `runtime_overrides.json`.

Mitigations:

- Single daemon lock around read/modify/write.
- Atomic temp-file + `os.replace`.
- Version field and deterministic JSON.
- Corrupt-file quarantine and safe fallback.
- Do not edit source-controlled `agent-config.json`.

## UI button state desync

Risk: UI shows enabled while server rejected enable or kill switch forced disabled.

Mitigations:

- Treat server response as source of truth.
- Disable toggle during in-flight request.
- Refetch after failure.
- Show `effective_enabled`, `enabled_source`, and `kill_switch_active`.

## In-flight evaluator drain

Risk: Disable is clicked while a critic subprocess is running.

Mitigations:

- Do not start new critic calls after disable.
- Use generation gating or evaluator lock so stale CONTINUE decisions from old generation are ignored.
- Keep existing timeout cap to bound the old call.

## Startup availability

Risk: Default `codex-cli` backend breaks daemon startup on machines without Codex auth.

Mitigations:

- Keep `enabled:false` default.
- Avoid constructing/probing external clients when disabled.
- If enabled and probe fails, disable effective state with visible health/config error rather than crash by default.
- Document fallback to `client=claude-cli`, `openai`, or `anthropic`.

---

# Acceptance criteria

## Feature 1 acceptance

- `AutoLoopBrainConfig.client` accepts exactly `claude-cli`, `openai`, `anthropic`, and `codex-cli`.
- `KAI_AUTO_LOOP_BRAIN_CLIENT=codex-cli` selects the Codex CLI client.
- `daemon.auto_loop_brain.codex_reasoning_effort` and env override accept `medium|high|xhigh`, default `medium`.
- `agent-config.json` default is `client=codex-cli`, `model_id=gpt-5.5`, `enabled=false`.
- `CodexCLIToollessLLMClient` uses mocked subprocess in tests, no shell, bounded timeout, and fail-closed exceptions.
- Existing clients remain supported and tested.
- No secret-bearing stderr/stdout is logged or returned unredacted.

## Feature 2 acceptance

- `PATCH /api/daemon/config/auto_loop_brain {"enabled": bool}` requires existing daemon HTTP auth.
- Toggle persists to `runtime_overrides.json` and survives daemon restart.
- Toggle does not modify `agent-config.json`.
- false -> true validates configured client with boot probe before committing.
- true -> false stops new critic calls without daemon restart and drains/ignores stale in-flight results safely.
- Live sessions and newly created sessions see the effective config.
- Telemetry emits `auto.loop_brain_toggle` with `{from,to,actor,ts}` plus safe backend metadata.
- UI toggle reflects server effective state, handles loading/failure, and cannot remain permanently desynced.
- Kill switch remains authoritative and visibly locks the toggle off.

## Non-regression acceptance

- `claude-cli` remains a valid client option.
- OpenAI-compatible endpoint routing remains valid.
- Direct Anthropic remains valid.
- Existing regex-first/fail-closed auto-loop-brain behavior remains unchanged.
- Alert subscriber config from #10387 is not regressed; alert toggles are either explicitly included with lifecycle tests or deferred.

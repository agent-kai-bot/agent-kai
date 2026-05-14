# Architecture Spec - Task 10429

## Title
Claude transport via Anthropic Agent SDK and OAuth subscription

## Status
Spec only. No implementation code in this change.

## Date
2026-05-14

## Operator Request
> "For Claude we need to implement correctly using the agent SDK and OAuth like we did for chatgpt subscriptions and codex-cli-gpt-5.5"

## Preamble
This spec defines a Claude transport that mirrors the working Codex subscription pattern while replacing ad hoc Claude CLI subprocess use with the Anthropic Agent SDK.

The current Codex reference pattern is explicit in the repo:

- `agent-config.json` defines endpoint `codex-cli` with `provider: "codex-cli"` at `agent-config.json:49-50`.
- It uses `base_url: "https://chatgpt.com/backend-api/codex"` at `agent-config.json:51`.
- It defaults to `gpt-5.5` at `agent-config.json:52`.
- Its `gpt-5.5` model entry sets `reasoning_effort: "xhigh"` at `agent-config.json:54-58`.
- Its `gpt-5.4` model entry also uses `reasoning_effort: "xhigh"` at `agent-config.json:60-64`.
- `agents.kai` routes to endpoint `codex-cli` at `agent-config.json:106-109`.
- `_create_codex_chat_model()` loads OAuth credentials and fails clearly when they are absent at `agent/core.py:220-241`.
- It sends Responses API reasoning and tool parameters at `agent/core.py:243-255`.
- It returns `ChatCodex` with OAuth access token and account headers at `agent/core.py:257-270`.
- `ChatCodex` subclasses `ChatOpenAI` at `agent/core.py:273`, adapts Codex-specific payload behavior at `agent/core.py:273-302`, retries auth failures at `agent/core.py:308-372`, and flattens structured content for LangChain at `agent/core.py:374-430`.

The ticket's required sections are restated here:

1. Survey current Claude usage with file:line citations and mark each touchpoint keep, migrate, or delete.
2. Describe the Anthropic Agent SDK package, capabilities, latest version, Python floor, streaming, tools, and model handling.
3. Specify OAuth subscription auth, bootstrap, Vault storage, refresh/expiry handling, and API-key fallback.
4. Define a new `agent-config.json` endpoint schema for `claude-sdk`.
5. Define an interim `ChatClaudeSDK` LangChain wrapper for legacy `AgentExecutor`/`AgentRunner`.
6. Define the post-#10428 LangGraph node integration.
7. Define tool exposure by mapping existing LangChain tools to SDK tool schemas without forking tool definitions.
8. Define migration of current Claude touchpoints.
9. Define five delivery phases.
10. Define tests.
11. Maintain a risk register.
12. Enforce memory hard rules: never Haiku, Sonnet 4.6 minimum, Opus 4.7 for high-stakes roles, Codex default for non-Claude paths.

The ticket also asks for open operator questions, included as section 13.

Conflict resolution:

- The ticket JSON mentions `claude-haiku-4-5` in an endpoint example.
- The operator constraint says never Haiku and Sonnet 4.6 minimum.
- The hard rule wins: Haiku must not appear in the KAI Claude picker, endpoint table, fallback chain, or automatic downgrade path.

## External References Checked
- PyPI `claude-agent-sdk` 0.1.81, released 2026-05-11, Python `>=3.10`: https://pypi.org/project/claude-agent-sdk/
- Repository `anthropics/claude-agent-sdk-python`: https://github.com/anthropics/claude-agent-sdk-python
- Python SDK reference: https://platform.claude.com/docs/en/agent-sdk/python
- Agent SDK overview: https://code.claude.com/docs/en/agent-sdk/overview
- Streaming output: https://code.claude.com/docs/en/agent-sdk/streaming-output
- Claude Code auth: https://code.claude.com/docs/en/team
- Claude Code CLI reference: https://code.claude.com/docs/en/cli-usage
- Claude Code model config: https://code.claude.com/docs/en/model-config
- Claude Code legal/credential notes: https://code.claude.com/docs/en/legal-and-compliance
- Claude Agent SDK subscription credit article: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan

## TOC
1. Survey current Claude usage
2. Anthropic Agent SDK overview
3. OAuth subscription flow
4. Endpoint config schema
5. ChatClaudeSDK LangChain wrapper
6. LangGraph node integration
7. Tool exposure
8. Migration of existing Claude touchpoints
9. Phased delivery
10. Tests
11. Risk register
12. Memory hard-rule enforcement
13. Open operator questions

## 1. Survey Current Claude Usage

### 1.1 Production Touchpoint Table
| Touchpoint | Current state claim | Action | Rationale |
|---|---|---|---|
| `agent-config.json` | No `claude-sdk` endpoint appears in the endpoint block that includes `codex-cli` and `openai-direct` at `agent-config.json:49-103`; `kai` routes to `codex-cli` at `agent-config.json:106-109`. | Keep Codex, add Claude | Claude should be a new endpoint, not a default replacement. |
| `requirements.txt` | Current dependencies list LangChain/OpenAI/NATS/FastAPI/requests/etc. and no `anthropic`, `langchain-anthropic`, or `claude-agent-sdk` at `requirements.txt:1-18`. | Migrate | Add `claude-agent-sdk` in Phase 1. |
| `agent/core.py` | `create_llm()` routes `provider == "codex-cli"` to `_create_codex_chat_model()` at `agent/core.py:186-207` and otherwise returns `ChatOpenAI` at `agent/core.py:209-217`. | Migrate | Add `provider == "claude-sdk"` branch returning `ChatClaudeSDK`. |
| `agent/core.py` | `_apply_reasoning_effort_override()` normalizes and applies reasoning overrides to endpoint/fallback configs at `agent/core.py:651-670`; `AgentRunner` stores resolved endpoint/fallback configs at `agent/core.py:676-700`. | Keep | Reuse override path for Claude reasoning/thinking. |
| `agent/auto_loop_brain.py` constants | Valid clients are `claude-cli`, `openai`, `anthropic`, `codex-cli`; default client is `codex-cli`; default critic model is `gpt-5.5` at `agent/auto_loop_brain.py:32-35`. | Keep default, migrate Claude | Add `claude-sdk`, keep Codex default. |
| `ClaudeCLIToollessLLMClient` | Uses executable `claude`, builds `claude -p --append-system-prompt ... --model ...`, calls `subprocess.run()`, and returns stdout at `agent/auto_loop_brain.py:124-147`. | Migrate then delete | Replace with SDK-backed `ToollessLLMClient`; keep CLI rollback until cleanup. |
| `AnthropicToollessLLMClient` | Reads `ANTHROPIC_API_KEY`, posts to `/v1/messages`, and manually parses content/tool blocks at `agent/auto_loop_brain.py:279-339`. | Keep as break-glass fallback | Direct API key is not subscription OAuth and not the primary Claude transport. |
| Minimum-tier gate | `AutoLoopBrainConfig.from_sources()` disables `claude-cli`/`anthropic` when model fails minimum tier at `agent/auto_loop_brain.py:382-385`; `_model_meets_minimum_tier()` accepts Sonnet/Opus variants at `agent/auto_loop_brain.py:729-731`. | Keep and strengthen | Extend to `claude-sdk`; reject all Haiku/unknown Claude models. |
| Auto-loop factory | `build_toolless_llm_client()` returns CLI Claude for `claude-cli`, Codex for `codex-cli`, direct Anthropic for `anthropic`, and endpoint OpenAI for `openai` at `agent/auto_loop_brain.py:609-625`. | Migrate | Add `client == "claude-sdk"`. |
| `agent/tool_policy.py` | `claude_exec` is not read-only, externally side-effecting, long-running, and approval-gated in auto mode at `agent/tool_policy.py:77-83`. | Keep | SDK transport does not make the tool low-risk. |
| `agent/prompts.py` | Main prompt describes `claude_exec` as Claude Code CLI at `agent/prompts.py:43-44` and recommends `codex_exec`/`claude_exec` at `agent/prompts.py:74-79`; sub-agent prompt lists and gates the same escalation tools at `agent/prompts.py:92-102`. | Keep name, update text later | Public tool name stays; implementation wording changes in Phase 4. |
| `agent/prompt_renderer.py` | Task and PR review output targets default under `claude/artifacts` at `agent/prompt_renderer.py:221-228` and `agent/prompt_renderer.py:663-673`. | Keep | Artifact path, not transport. |
| `agent/tools.py` | `CLAUDE_PATH` is `~/.local/bin/claude`; `_claude_exec()` shells out with `--dangerously-skip-permissions`; tool object is named `claude_exec` at `agent/tools.py:289-344`. | Migrate | Keep public tool shape, replace internals with SDK. |
| Tool registration | Main tool list includes `claude_exec` at `agent/tools.py:1233-1242`; sub-agent tool list includes it at `agent/tools.py:1281-1290`. | Keep | Tool remains available after implementation rewiring. |
| `agents.yaml` | `codex` overflows to logical `claude`; logical `claude` uses `provider: claude-cli` and `model: sonnet` at `agents.yaml:16-27`. | Migrate | Logical `claude` should map to `claude-sdk` with full model pins. |
| `config.py` executor registry | Default executor is `codex`; default overflow executor is `claude`; valid executors include `codex`, `claude`, `local-llm` at `config.py:27-29`. | Keep | Logical executor names stay stable. |
| `config.py` reasoning | Valid efforts and aliases are defined at `config.py:404-421`; `normalize_reasoning_effort()` is at `config.py:425-435`; endpoint flattening carries `reasoning_effort` through at `config.py:529-542`. | Keep | Claude adapter should reuse normalization before Claude-specific validation. |
| `taskboard_dispatcher.py` | Code Reviewer, Security Auditor, and QA Agent route to logical `model="claude"` at `agent/taskboard_dispatcher.py:286-302`; docstring says Code Reviewer resolves to `"claude"` at `agent/taskboard_dispatcher.py:307-321`. | Keep logical route | Change executor behind `claude`, not taskboard role semantics. |
| `run_outcome.py` | Text containing "timed out after" plus "claude cli" is classified as wall-clock budget exceeded at `agent/run_outcome.py:492-497`. | Keep then extend | Preserve old classifier until CLI cleanup; add SDK timeout classification. |
| `daemon/secrets.py` | Vault env vars and default paths are at `daemon/secrets.py:35-40`; `VaultWebhookSecretProvider` expands KV v2 paths and reads/caches secrets at `daemon/secrets.py:132-223`; default providers prefer env then Vault at `daemon/secrets.py:248-328`. | Reuse pattern | Claude OAuth token provider should follow the same Vault discipline. |
| `daemon/server.py` | Server imports and stores `RuntimeConfigResolver` at `daemon/server.py:28` and `daemon/server.py:649-665`; startup logs resolver diagnostics and initializes Vault-backed webhook secret providers at `daemon/server.py:971-999`. | Reuse pattern | Add Claude OAuth diagnostics without logging secret material. |
| `daemon/server.py` reasoning API | Scheduled overrides normalize `reasoning_effort` and `thinking_level` and require equality when both exist at `daemon/server.py:233-255`. | Keep | Claude endpoint should apply same synonym contract. |
| `runtime_config_resolver.py` | Local Vault defaults include `http://localhost:8484`; resolver has role/path maps, TTL cache, and Vault read helpers at `agent/runtime_config_resolver.py:19-35`, `agent/runtime_config_resolver.py:141-193`, and `agent/runtime_config_resolver.py:196-225`. | Reuse pattern | Claude token rotation should fit this hot-config/Vault style. |

### 1.2 Keep/Migrate/Delete Summary
- Keep Codex as default for `kai`, current executor names, taskboard logical `claude`, `claude_exec` public name, `claude/artifacts` paths, and conservative tool policy.
- Migrate `ClaudeCLIToollessLLMClient`, direct `claude_exec` subprocess internals, logical `claude` executor provider, prompt wording, and timeout classification.
- Delete only after burn-in: hard-coded `CLAUDE_PATH`, direct manual `claude -p` runtime scaffolding, and `ClaudeCLIToollessLLMClient` rollback.
- Do not delete direct `AnthropicToollessLLMClient` immediately; retain as explicit break-glass API-key fallback.

## 2. Anthropic Agent SDK Overview

### 2.1 Package Identity
- Canonical PyPI package: `claude-agent-sdk`.
- Import package: `claude_agent_sdk`.
- Latest stable observed on 2026-05-14: `0.1.81`.
- Release date observed: 2026-05-11.
- Minimum Python: `>=3.10`.
- Canonical repo: `https://github.com/anthropics/claude-agent-sdk-python`.
- The old Claude Code SDK name has been replaced by Claude Agent SDK in current docs.
- Do not use `anthropic-agent-sdk`, `claude-agents`, or old `claude-code-sdk` names unless Anthropic publishes another official rename.

### 2.2 What It Provides
The SDK provides `query()` for one-shot sessions, `ClaudeSDKClient` for interactive sessions, SDK message types, session resume/fork, subagents, hooks, permissions, built-in Claude Code tools, MCP integration, in-process custom tools, and streaming via partial message events.

The Python SDK package currently bundles a Claude Code binary. KAI should still treat the SDK API as the transport boundary: no manual `~/.local/bin/claude` discovery, no homegrown argv assembly, no stdout/stderr parsing as the primary runtime path.

### 2.3 Why Agent SDK Instead Of Raw Anthropic API
The raw Anthropic Messages API requires KAI to own the tool loop, tool-result protocol, streaming event aggregation, session behavior, permission gating, and retry semantics. The current direct client in `agent/auto_loop_brain.py` shows that shape: it posts to `/v1/messages` and parses text/tool blocks manually at `agent/auto_loop_brain.py:305-327`.

The Agent SDK is the right Claude agent transport because it uses the Claude Code agent harness and tool loop. The direct `AnthropicToollessLLMClient` can remain for a no-tool classifier fallback, but not for `claude_exec` or Claude role execution.

### 2.4 SDK Model Handling
Claude Code/Agent SDK can accept aliases like `sonnet` and `opus` or full names like `claude-sonnet-4-6` and `claude-opus-4-7`. KAI should use full names in config.

Current upstream docs say API `opus` resolves to Opus 4.7 and API `sonnet` resolves to Sonnet 4.6. KAI still pins full names because aliases update over time and the memory rule forbids accidental downgrade.

The SDK exposes `model` and `fallback_model` options. KAI should own the model picker list and policy; do not depend on an SDK model-list API for enforcement.

### 2.5 SDK Streaming
The SDK yields complete assistant messages by default. Setting `include_partial_messages=True` yields `StreamEvent` objects containing raw Claude API streaming events. The wrapper must extract `content_block_delta` events with `delta.type == "text_delta"` and convert them to LangChain chunks, while avoiding duplicate final text when complete messages arrive.

### 2.6 SDK Tools
The Python SDK defines custom tools with a `tool(name, description, input_schema, annotations)` decorator and returns `SdkMcpTool` objects containing `name`, `description`, `input_schema`, `handler`, and optional annotations. KAI should wrap existing LangChain tools into SDK MCP tools instead of maintaining a second catalog.

### 2.7 Effort And Thinking
Claude Code effort levels observed in docs are `low`, `medium`, `high`, `xhigh`, and `max`. Opus 4.7 supports `xhigh`; Opus 4.6 and Sonnet 4.6 support high effort tiers but may downgrade unsupported levels. `CLAUDE_CODE_EFFORT_LEVEL` is a documented session control.

KAI should first call `normalize_reasoning_effort()` from `config.py:425-435`, then apply Claude-specific policy:

- `none` and `minimal`: reject for Claude agent roles.
- `low`: allow only for explicit low-stakes operator requests.
- `medium`: allow for routine Claude utility work.
- `high`: default for Sonnet 4.6.
- `xhigh`: default for Opus 4.7 and high-stakes work.
- `max`: do not introduce as a default; consider later as a Claude-only extension.

### 2.8 Dependency
Phase 1 should add:

```text
claude-agent-sdk>=0.1.81,<0.2
```

Do not add `langchain-anthropic` for this path. Do not add the basic `anthropic` package unless the SDK or tests require it.

## 3. OAuth Subscription Flow

### 3.1 Goal
Use the operator's Claude subscription through OAuth, analogous to the Codex subscription path. The primary path must not require `ANTHROPIC_API_KEY`.

### 3.2 Bootstrap Commands
Current Claude Code docs show these relevant commands:

```bash
claude auth login
claude auth status
claude setup-token
```

Two bootstrap modes:

- Local manual smoke: operator runs `claude auth login`, then SDK can use local Claude Code credentials for manual verification.
- Daemon-ready bootstrap: operator runs `claude setup-token`, receives a long-lived OAuth token, and stores it in Vault.

Proposed KAI helper:

```bash
python -m agent.claude_oauth login --vault-path claude/oauth-token
python -m agent.claude_oauth status --vault-path claude/oauth-token
python -m agent.claude_oauth smoke --model claude-sonnet-4-6
```

The helper may invoke `claude setup-token` during bootstrap only. Runtime model calls use `claude_agent_sdk`, not subprocess `claude -p`.

### 3.3 Vault Storage
Required Vault server: `http://localhost:8484`.

Default Vault path: `claude/oauth-token`.

KV v2 expanded path: `claude/data/oauth-token`.

Environment override: `KAI_CLAUDE_OAUTH_VAULT_PATH=claude/oauth-token`.

Expected payload:

```json
{
  "oauth_token": "opaque-token",
  "token_type": "oauth",
  "source": "claude setup-token",
  "subscription": true,
  "created_at": "2026-05-14T00:00:00Z",
  "expires_at": "2027-05-14T00:00:00Z",
  "account_hint": "operator-confirmed",
  "sdk_package": "claude-agent-sdk"
}
```

Read keys in this order: `oauth_token`, `CLAUDE_CODE_OAUTH_TOKEN`, `token`, `secret`, `value`.

Diagnostics may expose `configured`, `vault_path`, `source`, `expires_at`, `expires_in_seconds`, and `cache_ttl_seconds`. Diagnostics must not expose token material.

### 3.4 Runtime Injection
Inject auth through SDK per-call environment, not global process mutation:

```python
ClaudeAgentOptions(
    model=model,
    system_prompt=system_prompt,
    cwd=working_directory,
    env={
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "CLAUDE_CODE_EFFORT_LEVEL": effort,
        "ANTHROPIC_MODEL": model
    }
)
```

The child SDK environment should remove `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, and `CLAUDE_CODE_USE_FOUNDRY` unless explicitly configured. Claude Code auth precedence gives API keys priority over subscription OAuth, so stale env keys are dangerous.

### 3.5 Expiry And Refresh
Treat `claude setup-token` output as an operator-rotated long-lived token. KAI should not invent an undocumented refresh flow.

Required behavior:

- Read from Vault through a TTL cache.
- Reject expired `expires_at`.
- Warn within 14 days of expiry.
- On SDK 401/auth failure, invalidate cache and re-read Vault once if no output was emitted.
- If retry fails, raise an operator-actionable reauth error.

Suggested error:

```text
Claude SDK endpoint requires subscription OAuth credentials.
Run `claude setup-token`, store the token in Vault at `claude/oauth-token`,
and ensure KAI can read localhost:8484. If ANTHROPIC_API_KEY is set,
unset it or configure the Claude SDK transport to ignore it.
```

### 3.6 Local Credential Files
Claude Code stores local credentials after interactive login, including `~/.claude/.credentials.json` on Linux unless `CLAUDE_CONFIG_DIR` is set. KAI daemon runtime must not depend on that file because daemon users and containers may differ from the operator shell, and this ticket requires Vault.

### 3.7 API-Key Fallback
API-key fallback is break-glass only. It must require `KAI_CLAUDE_ALLOW_API_KEY_FALLBACK=1`, present `ANTHROPIC_API_KEY`, startup warnings, telemetry tag `auth_source=api_key_fallback`, and visible operator logs. It must not silently override OAuth.

## 4. Endpoint Config Schema

### 4.1 Endpoint
Add a new endpoint:

```json
"claude-sdk": {
  "provider": "claude-sdk",
  "default_model": "claude-sonnet-4-6",
  "vault_path": "claude/oauth-token",
  "models": {
    "claude-opus-4-7": {
      "context_window": 1000000,
      "max_tokens": 32768,
      "reasoning_effort": "xhigh",
      "thinking_level": "xhigh",
      "role_profile": "orchestrator-dev-review",
      "allowed_for": ["orchestrator", "developer", "code-reviewer", "security-auditor", "qa-agent"]
    },
    "claude-sonnet-4-6": {
      "context_window": 1000000,
      "max_tokens": 32768,
      "reasoning_effort": "high",
      "thinking_level": "high",
      "role_profile": "default-claude-floor",
      "allowed_for": ["developer", "qa-agent", "analyst", "scanner", "mentor"]
    }
  }
}
```

No Haiku model or alias is allowed.

### 4.2 Fields
Endpoint-level fields:

- `provider`: required, exact `claude-sdk`.
- `default_model`: required, must exist in `models`.
- `vault_path`: optional, default `claude/oauth-token`.
- `allow_api_key_fallback`: optional, default false.
- `default_allowed_tools` and `default_disallowed_tools`: optional SDK tool filters.
- `permission_mode`: optional; KAI policy remains primary.

Model-level fields:

- `context_window`.
- `max_tokens`.
- `reasoning_effort`.
- `thinking_level`.
- `allowed_for`.
- `supports_1m_context`.
- `supports_adaptive_thinking`.
- `supports_interleaved_thinking`.

### 4.3 Validation
Config validation must enforce:

- `provider == "claude-sdk"` requires `default_model`.
- `default_model` must be in `models`.
- No model key may contain `haiku`.
- No key may equal `haiku`, `default`, or `best`.
- Sonnet model keys must be `claude-sonnet-4-6` or explicitly approved newer Sonnet.
- Opus model keys must be `claude-opus-4-7` or explicitly approved newer Opus.
- `reasoning_effort` and `thinking_level` must normalize through `normalize_reasoning_effort()` at `config.py:425-435`.
- If both are supplied, they must match, matching daemon override behavior at `daemon/server.py:233-255`.

### 4.4 Picker Rules
The KAI picker must include Sonnet 4.6, optionally include Opus 4.7 when accessible, exclude all Haiku strings, exclude `default`/`best` aliases, and fail instead of silently downgrading. If Opus access is missing, fall back only to Sonnet 4.6 with a visible downgrade note.

## 5. ChatClaudeSDK LangChain Wrapper

### 5.1 Goal
`ChatClaudeSDK` is the interim bridge for legacy LangChain `AgentExecutor` before #10428 LangGraph convergence. It should play the same architectural role for Claude that `ChatCodex` plays for Codex, but it must subclass `BaseChatModel`, not `ChatOpenAI`.

### 5.2 Shape
Target class:

```python
class ChatClaudeSDK(BaseChatModel):
    model: str
    vault_path: str
    reasoning_effort: str
    max_tokens: int
    streaming: bool = True

    @property
    def _llm_type(self) -> str:
        return "claude-sdk"

    def _generate(...): ...
    async def _agenerate(...): ...
    def _stream(...): ...
    async def _astream(...): ...
    def bind_tools(...): ...
```

### 5.3 Message Conversion
Rules:

- `SystemMessage` becomes `ClaudeAgentOptions.system_prompt`.
- Current `HumanMessage` becomes SDK prompt text.
- Prior turns are serialized into a bounded transcript for the first implementation.
- Tool messages should not be mapped into SDK state until #10428 defines graph-managed tool state.
- Use one SDK session per LangChain invocation initially to avoid mixing SDK state with current `AgentRunner` history.

### 5.4 SDK Options
The wrapper should set `model`, `system_prompt`, `cwd`, `env`, `max_turns`, and `include_partial_messages`. For normal chat completion, default to `max_turns=1` and hide SDK built-in mutating tools unless KAI intentionally exposes them. `claude_exec` gets a separate SDK runner because it is an agentic escalation boundary.

### 5.5 Streaming
`_astream()` should call SDK with `include_partial_messages=True`, convert text deltas into `ChatGenerationChunk`/`AIMessageChunk`, retain tool-call metadata, accumulate final content, and avoid duplicate final text. Sync `_stream()` can bridge to async only if safe in current LangChain runtime; otherwise prefer async daemon paths.

### 5.6 Tool Binding
`bind_tools()` should convert LangChain tools into an in-process SDK MCP server:

```python
server = create_sdk_mcp_server(name="kai_tools", tools=sdk_tools)
options.mcp_servers = {"kai_tools": server}
options.allowed_tools = ["mcp__kai_tools__file_read", "..."]
```

KAI policy decides which tools are exposed. SDK annotations are hints, not authority.

### 5.7 Reasoning Propagation
The wrapper should normalize `reasoning_effort` and `thinking_level` through `normalize_reasoning_effort()` at `config.py:425-435`, require equality when both are supplied, then set `CLAUDE_CODE_EFFORT_LEVEL` in SDK env. It may use SDK `extra_args` for effort only after Phase 1 confirms current package behavior.

### 5.8 Error Mapping
Map errors into typed KAI exceptions:

- Auth expired/unauthorized: invalidate Vault cache, re-read once, retry before output, then raise OAuth reauth error.
- Rate limit: raise capacity/rate-limit error with retry metadata if available.
- Model unavailable: raise, do not downgrade to Haiku or older Sonnet.
- Network/SDK process failure: raise transport error with redacted details.
- Tool schema failure: raise before SDK call with tool name.

### 5.9 Factory Hook
Add:

```python
def _create_claude_sdk_chat_model(endpoint_cfg: dict) -> ChatClaudeSDK:
    return ChatClaudeSDK(
        model=endpoint_cfg["model"],
        vault_path=endpoint_cfg.get("vault_path", "claude/oauth-token"),
        reasoning_effort=endpoint_cfg.get("reasoning_effort", "high"),
        max_tokens=endpoint_cfg.get("max_tokens", 32768),
        streaming=True,
    )
```

Then in `create_llm()`:

```python
if provider == "claude-sdk":
    return _create_claude_sdk_chat_model(endpoint_cfg)
```

Do not modify Codex behavior.

## 6. LangGraph Node Integration

### 6.1 Dependency On #10428
#10428 owns the LangGraph convergence. This ticket supplies the Claude transport and interim LangChain bridge. The same token provider, model policy, SDK runner, and tool converter should be reused when #10428 introduces graph nodes.

### 6.2 Node Contract
Target node: `claude_sdk_chat_node`.

Inputs:

- graph messages;
- active role;
- endpoint config;
- tool registry;
- runtime secrets provider;
- cancellation token;
- stream sink.

Outputs:

- appended assistant messages;
- tool-call intents or completed SDK-managed tool results;
- usage metadata;
- model/effort metadata;
- typed auth/capacity/model errors.

### 6.3 Behavior
The node must resolve the model, enforce the Claude model policy, load OAuth from Vault, bind allowed tools, stream deltas, return state updates only, record SDK session IDs when exposed, and never mutate graph state directly.

### 6.4 Tool Loop Choice
Initial recommendation:

- Use SDK-managed tool loops for `claude_exec`.
- Use graph-managed tool loops for normal chat after #10428 defines the canonical tool node.

Reason: `claude_exec` is already an agentic escalation boundary, while normal KAI chat needs provider-neutral audit, approval, cancellation, and tool accounting.

## 7. Tool Exposure

### 7.1 Principle
Reuse existing LangChain tool definitions. Do not fork tools. Do not create a second policy system. Do not expose Claude built-ins by default when equivalent KAI tools exist.

### 7.2 Current Tool Sources
Current KAI and sub-agent tool lists include core tools plus `claude_exec` at `agent/tools.py:1233-1242` and `agent/tools.py:1281-1290`; `claude_exec` itself is a `StructuredTool` at `agent/tools.py:334-344`; its policy is conservative at `agent/tool_policy.py:77-83`.

### 7.3 Conversion Algorithm
For each LangChain tool:

1. Read name and description.
2. Extract args schema from `args_schema` or `args`.
3. Convert Pydantic schema to JSON Schema.
4. Create async SDK handler that calls the original tool.
5. Return SDK MCP content blocks with text output.
6. Attach `ToolAnnotations` from KAI policy: read-only, destructive, and open-world hints.
7. Register through `create_sdk_mcp_server()`.

SDK tool names should be `mcp__kai_tools__<tool_name>`, while logs retain both SDK and KAI names.

### 7.4 Approval And Blocking
Before SDK execution, KAI filters tools by role, read-only mode, auto-mode approval, and session policy. During SDK execution, use `allowed_tools` and `disallowed_tools` to prevent Claude built-ins from bypassing KAI equivalents. Use SDK hooks for logging and emergency blocking, not as the sole security boundary.

### 7.5 `claude_exec` Recursion
Do not expose `claude_exec` to Claude-backed sessions by default. Recursive Claude-to-Claude escalation breaks budget, audit, and cancellation boundaries. Allow it only behind an explicit operator flag.

## 8. Migration Of Existing Claude Touchpoints

### 8.1 Auto-Loop Brain
Replace `ClaudeCLIToollessLLMClient` at `agent/auto_loop_brain.py:124-147` with `ClaudeSDKToollessLLMClient` implementing the same `complete_json()` protocol. The SDK critic sends no KAI tools, uses max turns one, requests strict JSON, passes model/effort, loads OAuth from Vault, and fails closed like the existing evaluator path at `agent/auto_loop_brain.py:486-489`.

Keep `client="claude-cli"` initially, add `client="claude-sdk"`, then flip Claude-specific configs after smoke tests. Default remains `codex-cli`.

### 8.2 Direct Anthropic Client
Keep `AnthropicToollessLLMClient` at `agent/auto_loop_brain.py:279-339` as explicit break-glass fallback. Add warnings when selected. Enforce Sonnet/Opus-only policy. Do not use it for subscription OAuth.

### 8.3 `claude_exec`
Keep tool name and args from `agent/tools.py:295-344`. Replace internals with a `ClaudeExecRunner` that uses Vault OAuth and Agent SDK permissions rather than `--dangerously-skip-permissions`. Preserve output truncation, worktree handling, timeout behavior, and tool policy.

### 8.4 Prompts
Keep prompt mentions at `agent/prompts.py:44`, `agent/prompts.py:78`, `agent/prompts.py:98`, and `agent/prompts.py:102` while the tool exists. After Phase 4, update wording from "Claude Code CLI" to "Claude Agent SDK transport".

### 8.5 Artifact Paths
Keep `claude/artifacts` output paths at `agent/prompt_renderer.py:221-228` and `agent/prompt_renderer.py:663-673`. They are output locations, not transport declarations.

### 8.6 Logical Claude Executor
Preserve logical `claude` in `agents.yaml` and `agent/taskboard_dispatcher.py`. Change physical provider from `claude-cli` to `claude-sdk`, pin full model names, prefer Opus 4.7 for review/security, and use Sonnet 4.6 as the floor.

## 9. Phased Delivery

### Phase 1 - SDK, OAuth Bootstrap, Vault, Standalone Smoke
Files: `requirements.txt`, new `agent/claude_oauth.py`, new smoke helper/script, `tests/test_claude_oauth.py`.

Work:

- Add `claude-agent-sdk>=0.1.81,<0.2`.
- Implement Vault-backed token read/write/status helpers.
- Store/read `claude/oauth-token`.
- Smoke `claude_agent_sdk.query()` using `claude-sonnet-4-6`.
- Prove `ANTHROPIC_API_KEY` is not required and cannot silently take precedence.

Tests: token extraction, path expansion, TTL cache, missing token, expired token, redaction.

Risk: low/medium; new dependency is large and bundles Claude Code runtime, but no routing changes.

Dependency: none.

Exit: standalone SDK smoke works from Vault OAuth.

### Phase 2 - `ChatClaudeSDK`, Endpoint, AgentRunner Smoke
Files: `agent/core.py`, `agent-config.json`, maybe `config.py`, `tests/test_claude_sdk_chat_model.py`, config tests.

Work:

- Add `ChatClaudeSDK`.
- Add `_create_claude_sdk_chat_model()`.
- Route `provider="claude-sdk"`.
- Add endpoint config.
- Keep default role routing unchanged unless explicitly selected.

Tests: factory routing, missing token error, reasoning normalization, Haiku rejection, streaming mapping, Codex regression.

Risk: medium; central LLM factory touched.

Dependency: Phase 1.

Exit: explicit AgentRunner Claude route works; Codex remains default.

### Phase 3 - Auto-Loop-Brain Claude SDK Path
Files: `agent/auto_loop_brain.py`, `tests/test_auto_loop_brain.py`.

Work:

- Add `ClaudeSDKToollessLLMClient`.
- Add `claude-sdk` valid client.
- Apply model policy.
- Keep `claude-cli` rollback and `codex-cli` default.

Tests: SDK critic construction, no tools, max-turn-one behavior, fail-closed SDK errors, Haiku rejection, existing backend regressions.

Risk: low/medium; critic is default-off for Claude and fail-closed.

Dependency: Phase 1, shared token provider from Phase 2 preferred.

Exit: Claude SDK critic works for bounded JSON classification.

### Phase 4 - SDK-Backed `claude_exec`
Files: `agent/tools.py`, tool tests, session env overlay tests.

Work:

- Replace subprocess internals.
- Keep public name and arguments.
- Use SDK runner, Vault OAuth, model policy, and KAI tool filters.
- Do not use `--dangerously-skip-permissions`.

Tests: missing token, model override rejection, working-directory mapping, timeout mapping, truncation, policy unchanged, no recursive `claude_exec` by default.

Risk: medium/high; changes a long-running side-effecting tool.

Dependency: Phase 1 plus SDK runner from Phase 2.

Exit: `claude_exec` no longer depends on `~/.local/bin/claude`.

### Phase 5 - Legacy Cleanup
Files: `agent/auto_loop_brain.py`, `agent/tools.py`, `agent/prompts.py`, `agents.yaml`, tests, docs.

Work:

- Remove `CLAUDE_PATH` and manual `claude -p` runtime path.
- Remove `ClaudeCLIToollessLLMClient` after rollback window.
- Update prompt wording.
- Change logical `claude` executor provider to `claude-sdk`.

Tests: full auto-loop-brain, tool, prompt, taskboard, and Codex regression suites.

Risk: medium; removes rollback.

Dependency: Phase 4 production burn-in and operator approval.

Exit: no production code path shells to `claude -p`.

## 10. Tests

### 10.1 OAuth/Vault
- Save/read token through fake Vault.
- Extract `oauth_token`, `CLAUDE_CODE_OAUTH_TOKEN`, `token`, `secret`, and `value`.
- Reject missing and expired tokens.
- Warn near expiry.
- Redact token-like values in errors.
- Verify TTL cache and explicit invalidation.

### 10.2 SDK Mocking
- Mock `claude_agent_sdk.query()`.
- Feed assistant, result, and stream-event messages.
- Simulate auth, rate-limit, process, and network failures.
- Verify typed KAI errors and cache invalidation behavior.

### 10.3 Model Routing
- Accept `claude-sonnet-4-6`.
- Accept `claude-opus-4-7`.
- Reject `haiku`, `claude-haiku-4-5`, `default`, `best`, and unknown Claude model strings.
- Reject or pin aliases before use.
- Verify Sonnet 4.6 floor in endpoint, auto-loop, `claude_exec`, and taskboard paths.

### 10.4 Tool Schema Mapping
- Convert `StructuredTool` schemas to SDK `input_schema`.
- Preserve required/optional fields.
- Return SDK MCP text content blocks.
- Map KAI policy to annotations.
- Confirm disallowed tools are not exposed.

### 10.5 Streaming
- Convert `content_block_delta` text deltas into LangChain chunks.
- Avoid duplicate final text.
- Preserve tool metadata.
- Follow `astream` contract.
- Close SDK session on cancellation.

### 10.6 Regressions
- Codex endpoint still builds `ChatCodex`.
- `gpt-5.5` still uses `xhigh`.
- Codex OAuth missing/refresh behavior unchanged.
- OpenAI-compatible endpoints still use `ChatOpenAI`.
- Auto-loop-brain default remains `codex-cli`.

## 11. Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| OAuth token theft | Subscription inference access compromise | Store only in Vault at `localhost:8484`, lock down Vault read access, redact logs, TTL cache, rotate before expiry. |
| Subscription rate limits | Claude path stalls or fails | Surface typed capacity errors, keep Codex fallback where configured, never downgrade to Haiku. |
| SDK version drift | Pre-1.0 API changes break transport | Pin `<0.2`, hide SDK behind adapter, mock SDK messages, require live smoke before upgrade. |
| Opus tier unavailable | High-stakes roles cannot use preferred model | Access check in Phase 1, visible fallback to Sonnet 4.6 only, no Haiku fallback. |
| API-key fallback | Subscription-auth goal undermined | Require explicit env flag, warn loudly, tag telemetry, keep fallback out of examples. |
| `langchain-anthropic` confusion | Wrong Claude route selected | Do not use it for `claude-sdk`; name any direct API-key route `anthropic-api`. |
| Legal/credential constraints | Misuse of Claude.ai credentials | Treat as operator-owned internal automation; do not offer third-party login or pool tokens; ask operator to confirm scope. |
| Bundled CLI failures | SDK errors still look process-like | Depend on SDK API, not manual subprocess; map SDK process errors; avoid hard-coded local CLI path. |
| Tool duplication | Policy bypass through SDK built-ins | Hide built-ins by default in normal chat; wrap KAI tools; use SDK built-ins only for isolated `claude_exec` if approved. |

## 12. Memory Hard-Rule Enforcement

### 12.1 Never Haiku
Reject any model equal to or containing `haiku`, including aliases and full names. Enforce in endpoint validation, model picker, `claude_exec`, auto-loop-brain, taskboard routing, fallback chains, and SDK child env construction.

### 12.2 Sonnet 4.6 Minimum
Default Claude model is `claude-sonnet-4-6`. Alias `sonnet` is rejected unless pinned to Sonnet 4.6 before SDK execution. Unknown Sonnet versions require explicit allowlist.

### 12.3 Opus 4.7 For High-Stakes Roles
Use `claude-opus-4-7` for orchestrator, complex developer work, code review, security audit, architecture review, and release-blocking QA when access exists. Fallback only to Sonnet 4.6 with visible downgrade. Never fallback to Haiku.

### 12.4 Codex Remains Default
Codex remains the default for non-Claude paths. Current state: `agent-config.json` routes `kai` to `codex-cli` at `agent-config.json:106-109`; `config.py` sets default executor `codex` at `config.py:27`; `agents.yaml` defines `codex` provider `codex-cli` at `agents.yaml:3-9`. Do not change those in Phase 1 or Phase 2.

## 13. Open Operator Questions
1. Is this Claude SDK usage strictly operator-owned internal automation, or will any other user authenticate with Claude.ai through KAI?
2. Which subscription tier should Phase 1 assume for Opus 4.7 access: Pro, Max 5x, Max 20x, Team, or Enterprise?
3. Should Opus 4.7 be hidden when access checks fail, or shown with an unavailable warning?
4. Should SDK-backed `claude_exec` expose Claude built-in file/bash tools, or only wrapped KAI tools from day one?
5. Should recursive Claude escalation be blocked globally?
6. What Vault write mechanism is preferred: direct Vault HTTP API, local `vault kv put`, or existing OpenClaw helper?
7. What token rotation interval should KAI enforce for `claude setup-token` tokens?
8. Should API-key fallback remain permanently available or only during bring-up?
9. Should `agents.yaml` flip logical `claude` to `claude-sdk` immediately after Phase 4 or after a burn-in period?
10. For #10428, should normal chat use SDK-managed or graph-managed tool loops first?
11. Should KAI pin `ANTHROPIC_DEFAULT_SONNET_MODEL` and `ANTHROPIC_DEFAULT_OPUS_MODEL` in the SDK child env even when full model names are passed?
12. Should the status UI expose Claude OAuth expiry and auth source alongside Codex OAuth health?
13. Should Claude `max` effort become a distinct future option, or should `xhigh` remain the top portable setting?

## Phase 1 Readiness
The first implementation ticket should be Phase 1 only. It should not touch `agent/core.py`, `agent/tools.py`, `agent-config.json`, or role routing. It should install the SDK, implement Vault-backed token helpers, add bootstrap/status/smoke commands, and prove a standalone `claude_agent_sdk.query()` call works from Vault OAuth.

That scope is intentionally narrow: prove package identity, subscription auth, Vault access, and a single Sonnet 4.6 smoke call before wiring Claude into shared agent execution.

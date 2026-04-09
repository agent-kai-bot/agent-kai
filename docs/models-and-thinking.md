# Models and thinking levels

How LLM endpoints, models, fallback chains, reasoning effort, and Codex OAuth work together. Covers `/model`, `/think`, `/login codex`, the endpoint registry in `agent-config.json`, and the runtime swap mechanics.

## TL;DR

```
/model                            show every agent's current endpoint+model
/model AGENT                      show that agent's setup + available choices
/model AGENT ENDPOINT/MODEL       swap an agent at runtime, in-memory only
/think                            show kai's reasoning level
/think LEVEL                      set kai's reasoning level
/think AGENT LEVEL                set any agent's reasoning level
/login codex                      OAuth into your ChatGPT subscription for codex-cli endpoint
```

In-memory swaps. Restart the TUI to revert. Edit `agent-config.json` for permanent changes.

## Endpoint registry

Endpoints are defined in `agent-config.json` under the `endpoints` key. Each endpoint has a `provider`, a `base_url`, an API key (or env var name), and a list of `models` it serves. Five endpoints ship by default:

### `kai-fast`

```json
"kai-fast": {
  "provider": "openai",
  "base_url": "https://agent-k.ai/v1",
  "api_key_env": "AGENT_KAI_API_KEY",
  "models": {
    "kai-fast": {
      "context_window": 32000,
      "max_tokens": 2048,
      "temperature": 0.3,
      "top_p": 0.95
    }
  }
}
```

The cloud agent-k.ai fast endpoint. 32k context, low max_tokens, low temperature. Used by the `scanner` sub-agent (high message volume, fast turnaround). Bearer-authenticated via `AGENT_KAI_API_KEY`.

### `kai-smart` (default for most agents)

```json
"kai-smart": {
  "provider": "openai",
  "base_url": "https://agent-k.ai/v1",
  "api_key_env": "AGENT_KAI_API_KEY",
  "models": {
    "kai-smart": {
      "context_window": 200000,
      "max_tokens": 4096,
      "temperature": 0.6,
      "top_p": 0.95
    }
  }
}
```

The cloud agent-k.ai smart endpoint. 200k context, higher max_tokens, balanced temperature. The default for almost every agent (`kai`, `analyst`, `trader`, `risk-manager`, `mentor`, all org-chart agents). The cloud routes this to the strongest model agent-k.ai is currently serving.

### `kai-local`

```json
"kai-local": {
  "provider": "openai",
  "base_url": "http://192.168.222.45:8000/v1",
  "api_key": "not-needed",
  "models": {
    "qwen35-gptq": {
      "context_window": 32000,
      "max_tokens": 4096,
      "temperature": 0.6,
      "top_p": 0.95
    }
  }
}
```

A local vLLM (or llama.cpp) server running Qwen 3.5 GPTQ. No auth. Configured for the user's home LAN — change the `base_url` to point at your own server. Used as a fallback for many sub-agents so the agent keeps working when the cloud is down or rate-limited.

### `codex-cli` (ChatGPT subscription)

```json
"codex-cli": {
  "provider": "codex-cli",
  "base_url": "https://chatgpt.com/backend-api/codex",
  "default_model": "gpt-5.4",
  "models": {
    "gpt-5.4": {
      "context_window": 200000,
      "max_tokens": 16384,
      "reasoning_effort": "medium",
      "text_verbosity": "medium"
    },
    "gpt-5.3": {
      "context_window": 200000,
      "max_tokens": 16384,
      "reasoning_effort": "high",
      "text_verbosity": "medium"
    },
    "gpt-5.1-codex-mini": {
      "context_window": 128000,
      "max_tokens": 8192,
      "reasoning_effort": "medium",
      "text_verbosity": "low"
    }
  }
}
```

The Codex Responses API — talks to `https://chatgpt.com/backend-api/codex/responses` using OAuth credentials from your ChatGPT subscription. **No API key needed** — the auth flow uses OAuth tokens stored at `~/.codex/auth.json`. See [Codex OAuth](#codex-oauth) below.

Three models exposed:

- **`gpt-5.4`** — current frontier reasoning model. Default `reasoning_effort: medium`. Best for complex multi-step analyses.
- **`gpt-5.3`** — previous generation. Default `reasoning_effort: high`. Slightly cheaper / more available.
- **`gpt-5.1-codex-mini`** — fast small model. Default `reasoning_effort: medium`. Good for high-volume tasks where you want some reasoning but not full GPT-5 cost.

These are the only endpoints that natively honor `reasoning_effort` (the others ignore it).

### `openai-direct`

```json
"openai-direct": {
  "provider": "openai",
  "base_url": "https://api.openai.com/v1",
  "api_key_env": "OPENAI_API_KEY",
  "default_model": "gpt-5.4",
  "models": {
    "gpt-5.4": {
      "context_window": 200000,
      "max_tokens": 16384
    },
    "gpt-4o": {
      "context_window": 128000,
      "max_tokens": 4096
    },
    "spark": {
      "context_window": 128000,
      "max_tokens": 4096
    }
  }
}
```

OpenAI direct API (with an API key — `OPENAI_API_KEY`). Useful if you want pay-as-you-go GPT-5.x without going through your ChatGPT subscription. Not used by any agent in the default config — opt in via `/model agent openai-direct/gpt-5.4`.

## Per-agent configuration

Each agent in `agent-config.json` references one or more endpoints:

```json
"analyst": {
  "description": "Technical analysis — indicators, patterns, chart reading, trading signals",
  "endpoint": "kai-smart",
  "fallback_endpoints": [
    {"endpoint": "kai-local", "model": "qwen35-gptq"},
    {"endpoint": "codex-cli", "model": "gpt-5.4"}
  ],
  "workspace": "analyst",
  "max_iterations": 200
}
```

- `endpoint`: the primary. Can be a bare string (`"kai-smart"`) or a slash-form (`"codex-cli/gpt-5.4"`) or a dict (`{"endpoint": "codex-cli", "model": "gpt-5.4"}`).
- `fallback_endpoints`: an ordered list. On any error from the primary, the agent walks the chain in order until something returns a non-error response.
- `max_iterations`: LangChain's max tool-call loop count for one task. 200 for most agents, 2000 for the long-running planner / coordinator agents.
- `reasoning_effort` (optional): per-agent override for the reasoning level. Set this to make a specific agent always think hard regardless of the model's default. See [thinking levels](#thinking-levels) below.

## /model — runtime endpoint switching

```
/model                            # Show every agent's current endpoint+model
/model AGENT                      # Show that agent's current setup + available choices
/model AGENT ENDPOINT/MODEL       # Switch the agent
```

Examples:

```
/model
# Output:
# Current models per agent:
#   kai: kai-smart/kai-smart
#   analyst: kai-smart/kai-smart
#   trader: kai-smart/kai-smart
#   ...

/model analyst
# Output:
# analyst: endpoint=kai-smart model=kai-smart
# Available: kai-fast/kai-fast, kai-smart/kai-smart, kai-local/qwen35-gptq, codex-cli/gpt-5.4, codex-cli/gpt-5.3, codex-cli/gpt-5.1-codex-mini, openai-direct/gpt-5.4, openai-direct/gpt-4o, openai-direct/spark

/model analyst codex-cli/gpt-5.4
# Output:
# analyst → codex-cli/gpt-5.4
# Respawning analyst with new model...
# analyst restarted on codex-cli/gpt-5.4
```

### Two rebuild paths

The TUI uses different strategies for the main agent vs sub-agents:

- **Main agent (`kai`):** `AgentRunner.reload_llm()` rebuilds the primary executor + fallback chain in place. Tools, memory, skills, prompt template, and chat_history are all preserved. The output: `kai now on codex-cli/gpt-5.4 (+1 fallback)`.
- **Sub-agents:** `SubAgentManager.stop(name)` + `spawn(name)`. Fast, but the sub-agent's chat history (which is per-task and short-lived anyway) is reset. The agent re-loads memory and skills on restart, so persistent state is preserved.

If a sub-agent isn't currently running when you `/model` it, the override is stored and takes effect on the next spawn.

### In-memory only

The override is in-memory only. Restart the TUI to revert to the on-disk default. To make a swap permanent, edit `agent-config.json`.

---

## Thinking levels

The Codex Responses API for GPT-5.x supports a `reasoning_effort` parameter that controls how much hidden chain-of-thought the model burns before producing its answer. Higher levels = better answers on hard problems, but slower and more expensive (the hidden tokens are billed).

The valid set comes from the openai SDK's `openai.types.shared_params.reasoning_effort.ReasoningEffort` literal:

| Canonical | Aliases | Effect |
|---|---|---|
| `none` | `off` | No reasoning. Lowest cost, lowest latency, weakest answers on hard problems. |
| `minimal` | `min` | Lowest reasoning. Useful for trivial queries where you want the speed boost. |
| `low` | | Light reasoning. Balanced for routine work. |
| `medium` | (default) | Standard reasoning. Default for `gpt-5.4`. |
| `high` | | More reasoning. Default for `gpt-5.3`. |
| `xhigh` | `x-high`, `extreme`, `max`, `extra` | Maximum reasoning. The model thinks for noticeably longer before answering. Save for genuinely hard problems. |

Note it's **`xhigh`** (one word), not `x-high` — but the command accepts the alias and normalizes to canonical.

## /think — runtime reasoning effort

```
/think                            # Show kai's current thinking level
/think LEVEL                      # Set kai's thinking level
/think AGENT                      # Show AGENT's current level
/think AGENT LEVEL                # Set AGENT's thinking level
```

Examples:

```
/think                            # "kai thinking level: (default: medium)"
/think high
/think analyst xhigh
/think mentor x-high              # alias resolves to xhigh
/think trader medium              # back to default
```

### How the override flows through

1. `set_agent_reasoning_effort("kai", "xhigh")` mutates `AGENTS["kai"]["reasoning_effort"]` in memory
2. `get_agent_config("kai")` injects that override into the resolved primary endpoint dict AND every fallback dict (so when the chain rolls over to the codex fallback, the effort still applies)
3. `_create_codex_chat_model(endpoint_cfg)` reads `reasoning_effort` off the dict and writes it to `extra_body.reasoning.effort` on the ChatCodex constructor
4. The Codex Responses API receives `{"reasoning": {"effort": "xhigh", "summary": "auto"}, ...}` in the request body and adjusts the internal reasoning budget accordingly

### Endpoint compatibility warning

Not every endpoint honors `reasoning_effort`:

| Endpoint | Honors reasoning_effort? |
|---|---|
| `codex-cli/gpt-5.x` | YES (Codex Responses API) |
| `openai-direct/gpt-5.x` | YES (when wired through `model_kwargs` or `extra_body`) |
| `kai-smart` | NO (cloud passes through to vLLM/qwen3 which doesn't expose this parameter) |
| `kai-fast` | NO |
| `kai-local/qwen35-gptq` | NO |

When you `/think` an agent whose CURRENT primary endpoint isn't in the thinking-capable set, the TUI sets the override anyway and emits a yellow warning:

```
warning: kai is on 'kai-smart' which may not honor reasoning_effort. Override
stored — will take effect after /model swap to a thinking-capable endpoint.
```

This is intentional. Two reasons:

1. It lets you pre-stage thinking levels before swapping (`/think kai xhigh`, then `/model kai codex-cli/gpt-5.4`)
2. The fallback chain often INCLUDES a codex endpoint, so the override DOES take effect when the chain rolls over to the thinking-capable fallback. Suppressing the override on the primary would silently drop it on the fallback too.

### Persistence

Like `/model`, `/think` is in-memory only. To make a thinking level permanent for an agent, add `reasoning_effort` to that agent's block in `agent-config.json`:

```json
"kai": {
  "description": "KAI — primary trading assistant, the user's main interface",
  "endpoint": "codex-cli/gpt-5.4",
  "reasoning_effort": "high",
  ...
}
```

This works at the agent level (previously only at the per-model level inside `endpoints.codex-cli.models.gpt-5.4.reasoning_effort`).

---

## Codex OAuth

`/login codex` lets you authenticate against your ChatGPT subscription so the agent can use the Codex Responses API without an API key.

### Why this exists

OpenAI gates GPT-5.x reasoning models behind two paths:

1. **API-key access** via `https://api.openai.com/v1` — pay-as-you-go, billed per token
2. **ChatGPT subscription** via `https://chatgpt.com/backend-api/codex` — OAuth, billed against your monthly ChatGPT Plus / Pro / Team quota

Path 2 is what the official `codex` CLI uses, and what we use here. If you already have a ChatGPT subscription, you've already paid for the model — there's no need to also pay per token.

### How it works

1. **First-time setup:** run `/login codex` in the TUI. This calls `agent.codex_auth.login()` which:
   - Generates a PKCE code verifier and challenge
   - Opens your default browser to `https://auth.openai.com/oauth/authorize?...`
   - Spins up a temporary localhost HTTP server on port 1455 to receive the redirect
   - Waits for the user to complete the OAuth flow in the browser
   - Exchanges the auth code for an access_token + refresh_token + account_id
   - Writes the tokens to `~/.codex/auth.json` in the same format the official codex CLI uses

2. **Reuse the codex CLI's existing login:** if you've already run `codex login` once on this machine, `~/.codex/auth.json` already exists and `/login codex` is unnecessary — the agent will read the tokens directly. Both tools share the same auth file.

3. **Token refresh:** access tokens expire (typically after ~30 minutes). The agent decodes the JWT's `exp` claim and refreshes when within 5 minutes of expiry. The refresh uses the long-lived `refresh_token` and updates `~/.codex/auth.json` in place.

### When you actually need this

You only need `/login codex` if BOTH:

- You want to use the `codex-cli` endpoint (i.e. you've configured an agent to use `codex-cli/gpt-5.x`)
- You don't already have `~/.codex/auth.json` from the official codex CLI

If neither is true, you can skip this entirely — the cloud `kai-smart` endpoint requires only `AGENT_KAI_API_KEY`.

### Sample flow

```
> /login codex
Starting Codex OAuth flow — a browser window will open...
[opens https://auth.openai.com/oauth/authorize?... in your default browser]
[you complete sign-in in the browser, OpenAI redirects to http://localhost:1455/auth/callback?code=...]
[the agent's local server receives the code, exchanges it for tokens, writes auth.json]
Logged in to ChatGPT (account_id=acc_abc123…, expires in 27h)
Codex endpoint is now usable. Restart agents that should pick it up.

> /model analyst codex-cli/gpt-5.4
analyst → codex-cli/gpt-5.4
Respawning analyst with new model...
analyst restarted on codex-cli/gpt-5.4

> /think analyst xhigh
analyst thinking → xhigh
analyst now thinking at xhigh (on codex-cli/gpt-5.4)

> /analyze BTC 1h
[runs on GPT-5.4 with maximum reasoning — the analyst spends meaningfully more
hidden tokens before producing the report]
```

### Security notes

- The `~/.codex/auth.json` file contains both an access token and a refresh token. Treat it like an SSH private key — it's full ChatGPT account access.
- Refresh tokens are long-lived (months). If your laptop is compromised, you should revoke the tokens via the ChatGPT account dashboard and re-run `/login codex`.
- The localhost callback server only listens during the auth flow and shuts down once it receives the code. It does NOT stay running.
- The OAuth client_id (`app_EMoamEEZ73f0CkXaXp7hrann`) is the same one the official codex CLI uses — sourced from `@mariozechner/pi-ai`.

---

## Common patterns

### Pattern: chat with kai on the cheap, escalate to GPT-5 for hard tasks

Default config: `kai` runs on `kai-smart` (free with your API key). For one-off hard problems:

```
/model kai codex-cli/gpt-5.4
/think kai xhigh
[ask the hard question]
/model kai kai-smart
/think kai medium
```

### Pattern: route the analyst to the strongest model permanently

Edit `agent-config.json`:

```json
"analyst": {
  "endpoint": "codex-cli/gpt-5.4",
  "reasoning_effort": "high",
  "fallback_endpoints": [
    {"endpoint": "kai-smart"},
    {"endpoint": "kai-local", "model": "qwen35-gptq"}
  ],
  ...
}
```

Now every `/analyze` runs on GPT-5.4 with high reasoning, falling back to kai-smart on errors and kai-local if both fail.

### Pattern: cheap-and-fast scanner, smart-and-slow analyst

The default config does this already:

```
scanner   → kai-fast    (32k context, low temperature, fast turnaround)
analyst   → kai-smart   (200k context, balanced)
mentor    → kai-smart   (200k context, codex-cli/gpt-5.4 fallback for hard reflections)
```

Tuned for the typical pump.fun scan being a "throw away results, do another" loop while the analyst needs to think.

### Pattern: full local mode (no cloud, no Codex)

Run everything against your own vLLM:

```bash
# In agent-config.json, change every agent's endpoint to "kai-local":
sed -i 's/"endpoint": "kai-smart"/"endpoint": "kai-local"/g' agent-config.json
sed -i 's/"endpoint": "kai-fast"/"endpoint": "kai-local"/g' agent-config.json
```

Then run with `AGENT_KAI_API_KEY=skip-this`. The agent will only hit `192.168.222.45:8000` (or wherever your vLLM is). No external API calls except for market data (which you can also disable by leaving `AGENT_KAI_API_KEY` blank — the chart panel will fail but the agent stays usable).

---

## What to read next

- [agents.md](agents.md) — sub-agent runtime, the 14 built-in agents, multi-agent orchestration
- [configuration.md](configuration.md) — full `agent-config.json` schema, env vars, secrets
- [learning-and-skills.md](learning-and-skills.md) — `/learn`, mentor reflection, skills (with a working example)
- [troubleshooting.md#codex-auth](troubleshooting.md#codex-auth-expired) — Codex auth issues

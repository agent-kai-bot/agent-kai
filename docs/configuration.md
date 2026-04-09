# Configuration

Everything that controls the agent's behavior outside of code: `agent-config.json`, environment variables, secrets, and the docker sandbox settings. This doc is the reference for editing any of them.

## Files and their purposes

| File | Purpose |
|---|---|
| `agent-config.json` | The single source of truth for endpoints, agents, fallback chains, memory limits, skills enable, and tool safety |
| `.env` (project root) | Optional. Auto-loaded by python-dotenv. Best place for `AGENT_KAI_API_KEY` in dev |
| `AGENT-KAI-API-KEY.txt` (project root) | Optional. One-line file with just the API key. Lowest-friction "drop the file in" path |
| `~/.codex/auth.json` | Codex OAuth credentials (if you use the `codex-cli` endpoint). Shared with the official codex CLI |
| `workspaces/terminal/state.json` | TUI state (chart symbol, timeframe, source, color scheme). Auto-managed |
| `workspaces/terminal/input_history.txt` | Bash-style up-arrow history. Auto-managed |
| `workspaces/{agent}/SOUL.md` | Per-agent role prompt — see [agents.md](agents.md) |
| `workspaces/{agent}/memories/MEMORY.md` | Per-agent persistent memory — see [learning-and-skills.md](learning-and-skills.md) |
| `workspaces/user.md` | Shared user profile across all agents |
| `workspaces/{agent}/skills/*.md` | Per-agent skill library |

`agent-config.json` and the SOUL files are version-controlled. Everything under `workspaces/` (except SOULs and skills you've manually committed) is gitignored — it's runtime state and per-user.

## agent-config.json schema

The full top-level structure:

```json
{
  "nats_url": "nats://localhost:4222",
  "default_agent": "kai",
  "workspaces_dir": "workspaces",
  "log_level": "DEBUG",
  "log_dir": "logs",

  "endpoints": { ... },
  "agents": { ... },
  "memory": { ... },
  "skills": { ... },
  "tool_safety": { ... }
}
```

Each section is documented below.

### Top-level keys

| Key | Type | Default | What |
|---|---|---|---|
| `nats_url` | string | `"nats://localhost:4222"` | NATS bus URL. Override with `--nats-url` CLI flag or `KAI_NATS_URL` env var |
| `default_agent` | string | `"kai"` | The agent name used when `main.py` is invoked without `--name`. Almost always `kai` |
| `workspaces_dir` | string | `"workspaces"` | Root directory for per-agent workspaces. Resolved relative to project root |
| `log_level` | string | `"DEBUG"` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. Override with `--log-level` CLI flag |
| `log_dir` | string | `"logs"` | Per-agent log files land here as `logs/{agent_name}_YYYY-MM-DD.log` |

### `endpoints`

The LLM endpoint registry. Each endpoint defines a `provider`, a `base_url`, an API key (or env var name), and a list of `models` it serves.

Standard schema:

```json
"endpoints": {
  "endpoint-name": {
    "provider": "openai",
    "base_url": "https://example.com/v1",
    "api_key_env": "MY_API_KEY_ENV_VAR",
    "api_key": "fallback-if-env-missing",
    "default_model": "model-id-here",
    "models": {
      "model-id-here": {
        "context_window": 200000,
        "max_tokens": 4096,
        "temperature": 0.6,
        "top_p": 0.95,
        "reasoning_effort": "medium",
        "text_verbosity": "medium"
      }
    }
  }
}
```

Per-endpoint keys:

- `provider` — `"openai"` for any OpenAI-compatible endpoint, `"codex-cli"` for the Codex Responses API
- `base_url` — full URL ending in `/v1` (or `/codex` for Codex)
- `api_key_env` — environment variable name to read the API key from. Takes precedence over `api_key`
- `api_key` — literal API key as fallback. Use `"not-needed"` for endpoints that don't require auth (local vLLM)
- `default_model` — which model to pick when an agent references the endpoint without specifying a model. Falls back to the first key in `models` if not set
- `models` — dict of model ID → model config

Per-model keys:

- `context_window` — total context window in tokens (informational, used for telemetry)
- `max_tokens` — max output tokens per request (sent to the API)
- `temperature` — sampling temperature
- `top_p` — nucleus sampling cutoff
- `reasoning_effort` — for Codex / GPT-5 reasoning models. One of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. See [models-and-thinking.md](models-and-thinking.md#thinking-levels)
- `text_verbosity` — for Codex. One of `low`, `medium`, `high`

The five default endpoints (`kai-fast`, `kai-smart`, `kai-local`, `codex-cli`, `openai-direct`) are documented in [models-and-thinking.md](models-and-thinking.md#endpoint-registry).

### `agents`

The agent registry. Each entry defines a sub-agent (or the main `kai` agent).

```json
"agents": {
  "kai": {
    "description": "KAI — primary trading assistant, the user's main interface",
    "endpoint": "kai-smart",
    "fallback_endpoints": [
      {"endpoint": "kai-local", "model": "qwen35-gptq"},
      {"endpoint": "codex-cli", "model": "gpt-5.4"}
    ],
    "max_iterations": 2000
  },
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
}
```

Per-agent keys:

- `description` — one-liner shown in `/model` listings and `list_agents` output
- `endpoint` — the primary LLM endpoint reference. Three forms accepted:
  - Bare string: `"kai-smart"` (uses the endpoint's default model)
  - Slash form: `"codex-cli/gpt-5.4"`
  - Dict: `{"endpoint": "codex-cli", "model": "gpt-5.4"}`
- `model` — optional override for the bare-string form (combined as `endpoint/model` internally)
- `fallback_endpoints` — list of references in any of the supported shapes. Walked in order on primary failure
- `fallback_endpoint` — legacy single-fallback alias. The plural takes priority if both are set
- `workspace` — workspace directory name under `workspaces_dir`. Defaults to the agent name
- `max_iterations` — LangChain's max tool-call loop count for one task. Typical: 200 for specialists, 2000 for long-running coordinator agents
- `system_prompt` — optional inline role prompt. If unset, the SOUL.md from the agent's workspace is loaded instead
- `reasoning_effort` — optional per-agent override for the reasoning level. Wins over the model-level default. See [models-and-thinking.md](models-and-thinking.md#thinking-levels)

### `memory`

```json
"memory": {
  "enabled": true,
  "user_profile_enabled": true,
  "memory_char_limit": 11000,
  "user_char_limit": 6875
}
```

- `enabled` — global memory on/off
- `user_profile_enabled` — whether the shared `workspaces/user.md` is loaded into agent prompts
- `memory_char_limit` — max chars in the per-agent `MEMORY.md` (rejects writes that would exceed this)
- `user_char_limit` — max chars in the shared `user.md`

When usage exceeds 80%, the agent should consolidate related entries before adding new ones. See [learning-and-skills.md#memory](learning-and-skills.md#memory).

### `skills`

```json
"skills": {
  "enabled": true
}
```

Single global toggle. When false, the four skill tools (`skills_list`, `skill_view`, `skill_manage`, plus the loader) all return "skills disabled" responses. The skill files on disk are unaffected.

### `tool_safety`

```json
"tool_safety": {
  "shell_timeout_seconds": 30,
  "max_file_read_chars": 10000,
  "max_output_chars": 5000,

  "docker_sandbox": {
    "default_image": "python:3.12-slim",
    "default_timeout_seconds": 60,
    "max_timeout_seconds": 600,
    "default_network": "none",
    "allowed_networks": ["none", "bridge"],
    "memory_limit": "512m",
    "cpu_limit": "1.0",
    "pids_limit": 512,
    "tmpfs_size": "64m",
    "run_as_user": "65534:65534",
    "mount_workspace_by_default": true
  }
}
```

Tool safety:

- `shell_timeout_seconds` — max wall time for `shell_exec` calls. Anything longer is killed with a timeout error
- `max_file_read_chars` — `file_read` truncates at this size and appends `[truncated at N chars]`
- `max_output_chars` — every tool's output is truncated at this size before being returned to the LLM. Prevents one tool call from blowing the context window

Docker sandbox (the `docker_sandbox` tool — see [agents.md](agents.md) and `agent/tools.py:create_docker_sandbox_tool`):

- `default_image` — the docker image to run by default. The agent can override per-call but the default applies if not specified
- `default_timeout_seconds` / `max_timeout_seconds` — wall time cap for sandboxed runs. The LLM's per-call timeout is clamped at `max_timeout_seconds` so it can't ask for an 8-hour run
- `default_network` / `allowed_networks` — `"none"` disables network entirely (the sandbox can't reach the internet); `"bridge"` allows outbound traffic. Anything not in `allowed_networks` is rejected
- `memory_limit` / `cpu_limit` / `pids_limit` — docker resource caps
- `tmpfs_size` — the size of the writable tmpfs at `/tmp` (the only writable area when no workspace is mounted)
- `run_as_user` — UID:GID for the container process. `65534:65534` is the `nobody` user on most Linux distros
- `mount_workspace_by_default` — when true, sub-agents get their workspace bind-mounted at `/work` automatically

The sandbox is hardened with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, and the `--tmpfs` mount. The host filesystem is not exposed except via the optional workspace mount.

---

## Environment variables

### Required

| Var | What |
|---|---|
| `AGENT_KAI_API_KEY` | Bearer token for the cloud `agent-k.ai` API (market data + LLM). Required unless you only use local endpoints |

### Optional

| Var | Default | What |
|---|---|---|
| `AGENT_KAI_BASE_URL` | `https://agent-k.ai` | Override the cloud REST base URL |
| `AGENT_KAI_WS_URL` | `wss://agent-k.ai/v1/ws` | Override the cloud WebSocket URL |
| `KAI_NATS_URL` | `nats://localhost:4222` | NATS bus URL (also settable via `--nats-url`) |
| `KAI_TRACKED_SYMBOLS` | `BTC,ETH,SOL` | Comma-separated default symbols for the watchlist + chart |
| `OPENAI_API_KEY` | (none) | Required only if any agent uses the `openai-direct` endpoint |
| `KAI_LOG_LEVEL` | from `agent-config.json` | Override log level (also settable via `--log-level`) |

### Set in your shell

```bash
export AGENT_KAI_API_KEY="kai-..."
export KAI_TRACKED_SYMBOLS="BTC,ETH,SOL,DOGE,XRP,ADA"
```

### Or in a .env file

```bash
cat > .env <<EOF
AGENT_KAI_API_KEY=kai-...
KAI_TRACKED_SYMBOLS=BTC,ETH,SOL
EOF
```

Auto-loaded by python-dotenv at startup.

---

## Secret loading

The agent looks for secrets in three places, in priority order:

1. **The current process environment** (best for production / Docker / systemd / CI)
2. **A `.env` file in the project root** (loaded via python-dotenv if present — convenient for dev, never commit)
3. **A bare token file at the project root**, like `AGENT-KAI-API-KEY.txt` (lowest friction — "download key, drop into project, restart agent")

For `AGENT_KAI_API_KEY` specifically:

```python
# config.py at import time
api_key = os.getenv("AGENT_KAI_API_KEY")
if not api_key:
    api_key = _load_secret_from_file("AGENT-KAI-API-KEY.txt")  # last resort
```

The token file path is in `.gitignore` so dropping the file in won't accidentally commit your key.

For other secrets (`OPENAI_API_KEY` etc), only the env var path is checked — there's no fallback file.

---

## How to add a new endpoint

1. Edit `agent-config.json` and add a new entry under `endpoints`:

```json
"my-custom-endpoint": {
  "provider": "openai",
  "base_url": "https://my-server.example.com/v1",
  "api_key_env": "MY_CUSTOM_API_KEY",
  "default_model": "my-model",
  "models": {
    "my-model": {
      "context_window": 32000,
      "max_tokens": 4096,
      "temperature": 0.6,
      "top_p": 0.95
    }
  }
}
```

2. Set the env var in your shell or `.env`:

```bash
export MY_CUSTOM_API_KEY="..."
```

3. Reference it from any agent in `agent-config.json`:

```json
"analyst": {
  "endpoint": "my-custom-endpoint",
  ...
}
```

4. Restart the TUI (or `/model analyst my-custom-endpoint/my-model` for an in-memory swap).

No code changes needed. The endpoint is picked up by `get_endpoint(name)` at runtime.

---

## How to add a new agent

1. Add a new entry to `agents` in `agent-config.json` (see the schema above).

2. Create the workspace directory and SOUL:

```bash
mkdir -p workspaces/myrole/{memories,skills}
touch workspaces/myrole/memories/MEMORY.md
cat > workspaces/myrole/SOUL.md <<'EOF'
# Myrole Agent

## Identity
You are the Myrole agent — [what you are].

## Responsibilities
- ...

## Tools You Use Most
- ...

## How you work
- ...
EOF
```

3. Optionally drop a few starter skills into `workspaces/myrole/skills/*.md` (use any of the existing skills as a template — see `workspaces/analyst/skills/macd-zero-cross.md`).

4. Restart the TUI. The agent appears in `list_agents()` and can be spawned via `spawn_agent("myrole")` or routed-to via `nats_request("myrole", task)`.

No code changes. The entire agent lives in `agent-config.json` + the workspace.

---

## Editing agent-config.json safely

- `agent-config.json` is loaded once at module import time (in `config.py`). Changes require a TUI restart.
- The exception: `/model` and `/think` mutate the in-memory `AGENTS` dict directly so you can iterate without restart. These changes do NOT persist to disk.
- The file is JSON — comments are not allowed. Don't add `//` lines.
- Keys are case-sensitive.
- If the file fails to parse (e.g. trailing comma, missing quote), `main.py` exits with a clear error pointing at the line.

### Validation

There's no formal schema validation today. The fields are read with `.get(key, default)` so an unknown key is silently ignored — including typos. If your agent isn't picking up a config change, double-check spelling.

---

## What to read next

- [models-and-thinking.md](models-and-thinking.md) — endpoint and reasoning details
- [agents.md](agents.md) — how agents use the config
- [troubleshooting.md](troubleshooting.md) — when config fails to load or apply

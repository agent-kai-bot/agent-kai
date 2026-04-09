"""Centralized configuration loaded from agent-config.json."""

import json
import os
import sys

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "agent-config.json")


def load_config(path=CONFIG_PATH):
    """Load and return the agent config dict."""
    with open(path) as f:
        return json.load(f)


_config = load_config()

# Project root (where agent-config.json lives)
PROJECT_ROOT = os.path.dirname(os.path.abspath(CONFIG_PATH))


# ── Secret loading ──────────────────────────────────────────
#
# Three places we accept secrets, in priority order:
#
# 1. The current process environment (preferred for production /
#    docker / systemd / CI).
# 2. A local ``.env`` file in the project root (loaded via
#    python-dotenv if present). Convenient for dev — never commit.
# 3. Bare token files at the project root, like
#    ``AGENT-KAI-API-KEY.txt``. Lowest friction for a one-off
#    test ("download key, drop into project, restart agent").
#
# Each of these is loaded ONCE here, not on every get_endpoint call,
# so the resolved env state is stable for the rest of the process.

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)
except ImportError:
    pass


def _load_secret_from_file(filename: str) -> str | None:
    """Read a single-line secret from a token file in PROJECT_ROOT.

    Returns the stripped contents if the file exists, else None.
    Used as a last-resort source for API keys so users can drop a
    downloaded credential file into the project and have things
    just work — without ever committing it (see .gitignore).
    """
    path = os.path.join(PROJECT_ROOT, filename)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


# Drop-in token files we know about. The mapping is
# {env_var_name: filename}. If the env var is not already set
# AND the file exists, we promote the file's contents into the env.
_TOKEN_FILE_MAPPINGS = {
    "AGENT_KAI_API_KEY": "AGENT-KAI-API-KEY.txt",
    "OPENAI_API_KEY": "OPENAI-API-KEY.txt",
    "ANTHROPIC_API_KEY": "ANTHROPIC-API-KEY.txt",
}

for env_var, filename in _TOKEN_FILE_MAPPINGS.items():
    if not os.environ.get(env_var):
        secret = _load_secret_from_file(filename)
        if secret:
            os.environ[env_var] = secret

# Top-level settings
NATS_URL = _config.get("nats_url", "nats://localhost:4222")
DEFAULT_AGENT = _config.get("default_agent", "nano")
WORKSPACES_DIR = os.path.join(PROJECT_ROOT, _config.get("workspaces_dir", "workspaces"))

# Endpoints registry: name -> {base_url, model, api_key, max_tokens, temperature, top_p}
ENDPOINTS = _config.get("endpoints", {})

# Agents registry: name -> {endpoint, fallback_endpoint, system_prompt, max_iterations, description}
AGENTS = _config.get("agents", {})

# Persistent memory — bounded curated memory (MEMORY.md per agent,
# USER.md shared across all agents). Ported from the Hermes design
# with larger char limits tuned for our LLM context budget. Disable
# either store by flipping its enabled flag in agent-config.json.
_memory_cfg = _config.get("memory", {})
MEMORY_ENABLED = bool(_memory_cfg.get("enabled", True))
USER_PROFILE_ENABLED = bool(_memory_cfg.get("user_profile_enabled", True))
MEMORY_CHAR_LIMIT = int(_memory_cfg.get("memory_char_limit", 11_000))
USER_CHAR_LIMIT = int(_memory_cfg.get("user_char_limit", 6_875))

# Skills — on-demand procedural memory. Per-agent library of
# reusable recipes the agent authored itself after hard-won
# trial-and-error sessions. Each agent has its own skills dir at
# workspaces/<role>/skills/. Disable globally by flipping the flag.
_skills_cfg = _config.get("skills", {})
SKILLS_ENABLED = bool(_skills_cfg.get("enabled", True))

# Tool safety
_tool_safety = _config.get("tool_safety", {})
SHELL_TIMEOUT_SECONDS = _tool_safety.get("shell_timeout_seconds", 30)
MAX_FILE_READ_CHARS = _tool_safety.get("max_file_read_chars", 10_000)
MAX_OUTPUT_CHARS = _tool_safety.get("max_output_chars", 5_000)

# Docker sandbox tool settings — defaults that apply to every sandboxed
# container run. These are deliberately strict so the LLM can opt into
# just the bits it needs (network, specific image) without having to
# remember every security flag each call.
_sandbox_cfg = _tool_safety.get("docker_sandbox", {})
DOCKER_SANDBOX_IMAGE = _sandbox_cfg.get("default_image", "python:3.12-slim")
DOCKER_SANDBOX_DEFAULT_TIMEOUT = int(_sandbox_cfg.get("default_timeout_seconds", 60))
DOCKER_SANDBOX_MAX_TIMEOUT = int(_sandbox_cfg.get("max_timeout_seconds", 600))
DOCKER_SANDBOX_DEFAULT_NETWORK = _sandbox_cfg.get("default_network", "none")
DOCKER_SANDBOX_ALLOWED_NETWORKS = tuple(
    _sandbox_cfg.get("allowed_networks", ["none", "bridge"])
)
DOCKER_SANDBOX_MEMORY = _sandbox_cfg.get("memory_limit", "512m")
DOCKER_SANDBOX_CPUS = str(_sandbox_cfg.get("cpu_limit", "1.0"))
DOCKER_SANDBOX_PIDS = int(_sandbox_cfg.get("pids_limit", 512))
DOCKER_SANDBOX_TMPFS_SIZE = _sandbox_cfg.get("tmpfs_size", "64m")
DOCKER_SANDBOX_USER = _sandbox_cfg.get("run_as_user", "65534:65534")
DOCKER_SANDBOX_MOUNT_WORKSPACE_DEFAULT = bool(
    _sandbox_cfg.get("mount_workspace_by_default", True)
)


def get_endpoint(name, model_name=None):
    """Resolve an (endpoint, model) pair into a flat config dict.

    Two endpoint formats are supported, both can coexist in agent-config.json:

    **New (multi-model)** — endpoint declares N models, each with its own
    context, max_tokens, and provider-specific settings::

        "openai-direct": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "models": {
                "gpt-5.4":   {"context_window": 200000, "max_tokens": 16384},
                "gpt-4o":    {"context_window": 128000, "max_tokens":  4096},
                "spark":     {"context_window": 128000, "max_tokens":  4096}
            }
        }

    **Legacy (single-model)** — top-level ``model`` field::

        "kai-local": {
            "base_url": "http://...",
            "model": "qwen35-gptq",
            "max_tokens": 4096
        }

    The legacy form is auto-wrapped as a one-model endpoint so existing
    agent configs keep working.

    Args:
        name: endpoint name in the registry
        model_name: optional model id within the endpoint. If omitted,
            uses the agent-supplied model OR the endpoint's first model.

    Returns the flat dict the LLM factory expects: ``base_url``, ``model``,
    ``api_key``, ``provider``, ``context_window``, ``max_tokens``,
    ``temperature``, ``top_p``, plus any provider-specific keys
    (``reasoning_effort``, ``text_verbosity``, …).
    """
    ep = ENDPOINTS.get(name)
    if not ep:
        print(f"Warning: endpoint '{name}' not found in config, available: {list(ENDPOINTS.keys())}")
        return None

    # Resolve API key (env var override takes precedence)
    api_key = ep.get("api_key", "not-needed")
    api_key_env = ep.get("api_key_env")
    if api_key_env:
        api_key = os.getenv(api_key_env, api_key)

    provider = ep.get("provider", "openai")
    base_url = ep.get("base_url", "")

    # Multi-model endpoint
    models = ep.get("models")
    if isinstance(models, dict) and models:
        # Pick the requested model, or the explicit endpoint default,
        # or the first key in the dict.
        chosen = model_name or ep.get("default_model") or next(iter(models))
        if chosen not in models:
            print(
                f"Warning: model '{chosen}' not found in endpoint '{name}'. "
                f"Available: {list(models)}. Falling back to {next(iter(models))}."
            )
            chosen = next(iter(models))
        mcfg = models[chosen] or {}
        return {
            "base_url": base_url,
            "provider": provider,
            "model": chosen,
            "api_key": api_key,
            "api_key_env": api_key_env,
            "context_window": mcfg.get("context_window") or ep.get("context_window") or 0,
            "max_tokens": mcfg.get("max_tokens") or ep.get("max_tokens", 4096),
            "temperature": mcfg.get("temperature", ep.get("temperature", 0.6)),
            "top_p": mcfg.get("top_p", ep.get("top_p", 0.95)),
            # Codex / responses-API specific knobs
            "reasoning_effort": mcfg.get("reasoning_effort", ep.get("reasoning_effort")),
            "text_verbosity": mcfg.get("text_verbosity", ep.get("text_verbosity")),
        }

    # Legacy single-model endpoint
    return {
        "base_url": base_url,
        "provider": provider,
        "model": ep.get("model"),
        "api_key": api_key,
        "api_key_env": api_key_env,
        "context_window": ep.get("context_window") or 0,
        "max_tokens": ep.get("max_tokens", 4096),
        "temperature": ep.get("temperature", 0.6),
        "top_p": ep.get("top_p", 0.95),
        "reasoning_effort": ep.get("reasoning_effort"),
        "text_verbosity": ep.get("text_verbosity"),
    }


def list_endpoint_models(name: str) -> list[str]:
    """Return the list of model ids exposed by a multi-model endpoint."""
    ep = ENDPOINTS.get(name) or {}
    models = ep.get("models")
    if isinstance(models, dict) and models:
        return list(models.keys())
    if ep.get("model"):
        return [ep["model"]]
    return []


def get_workspace_path(agent_name):
    """Get the workspace directory for an agent. Creates it if needed."""
    agent_cfg = AGENTS.get(agent_name, {})
    workspace_name = agent_cfg.get("workspace", agent_name)
    workspace_path = os.path.join(WORKSPACES_DIR, workspace_name)
    os.makedirs(workspace_path, exist_ok=True)
    return workspace_path


def get_memory_path(agent_name):
    """Get the path to the agent's MEMORY.md (per-agent notes)."""
    return os.path.join(get_workspace_path(agent_name), "memories", "MEMORY.md")


def get_skills_dir(agent_name):
    """Get the path to the agent's skills directory."""
    return os.path.join(get_workspace_path(agent_name), "skills")


def get_user_profile_path():
    """Get the path to the shared USER.md (global user profile).

    User preferences are intentionally shared across every agent so
    something the user tells the trader agent ("I prefer USD sizing")
    is also visible to the analyst, risk-manager, and nano agents on
    their next session without duplicating state.
    """
    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    return os.path.join(WORKSPACES_DIR, "user.md")


def load_soul(agent_name):
    """Load the SOUL.md for an agent. Returns content string or None."""
    workspace = get_workspace_path(agent_name)
    soul_path = os.path.join(workspace, "SOUL.md")
    if os.path.isfile(soul_path):
        with open(soul_path) as f:
            return f.read()
    return None


def _resolve_endpoint_ref(ref):
    """Normalize an agent's endpoint/fallback reference into a flat config.

    Accepts three shapes for maximum config-writing ergonomics:

    - **String**: ``"kai-local"`` — endpoint name; uses default model
    - **String w/ slash**: ``"openai-direct/gpt-5.4"`` — endpoint and model
    - **Dict**: ``{"endpoint": "openai-direct", "model": "gpt-5.4"}``

    Returns the flat dict from ``get_endpoint(name, model)`` or None
    if the reference is malformed / unknown.
    """
    if ref is None:
        return None
    if isinstance(ref, str):
        if "/" in ref:
            ep_name, model_name = ref.split("/", 1)
            return get_endpoint(ep_name, model_name=model_name)
        return get_endpoint(ref)
    if isinstance(ref, dict):
        ep_name = ref.get("endpoint") or ref.get("name")
        model_name = ref.get("model")
        if not ep_name:
            return None
        return get_endpoint(ep_name, model_name=model_name)
    return None


def get_agent_config(agent_name):
    """Get agent config by name. Returns dict with endpoint info + agent settings.

    Supports:
    - ``endpoint`` (string or dict) — primary endpoint
    - ``model`` — optional model override applied to a string ``endpoint``
    - ``fallback_endpoint`` (legacy single, string or dict)
    - ``fallback_endpoints`` (list of refs in any of the supported shapes)

    Returns:
        endpoint: flat dict for the primary
        fallback_endpoints: list of flat dicts in chain order (may be empty)
        fallback_endpoint: alias for the first item in fallback_endpoints
            (preserves backward compat with code that reads the singular)
    """
    agent_cfg = AGENTS.get(agent_name, {})
    endpoint_ref = agent_cfg.get("endpoint")
    explicit_model = agent_cfg.get("model")

    # If endpoint is a bare string and the agent specified a model field,
    # combine them into the slash form so _resolve handles both cases.
    if isinstance(endpoint_ref, str) and explicit_model and "/" not in endpoint_ref:
        endpoint_ref = f"{endpoint_ref}/{explicit_model}"

    # Default to the first available endpoint if nothing was specified
    if not endpoint_ref:
        first = next(iter(ENDPOINTS), None)
        endpoint_ref = first

    # Build the fallback chain. Accept either the legacy singular field
    # or the new plural list. The plural takes priority.
    chain: list = []
    plural = agent_cfg.get("fallback_endpoints")
    if isinstance(plural, list) and plural:
        chain = list(plural)
    elif agent_cfg.get("fallback_endpoint"):
        chain = [agent_cfg["fallback_endpoint"]]

    resolved_chain: list[dict] = []
    for ref in chain:
        flat = _resolve_endpoint_ref(ref)
        if flat:
            resolved_chain.append(flat)

    # Load SOUL.md as system prompt if no explicit prompt is set
    system_prompt = agent_cfg.get("system_prompt")
    if not system_prompt:
        soul = load_soul(agent_name)
        if soul:
            system_prompt = soul

    return {
        "endpoint": _resolve_endpoint_ref(endpoint_ref),
        "fallback_endpoint": resolved_chain[0] if resolved_chain else None,
        "fallback_endpoints": resolved_chain,
        "system_prompt": system_prompt,
        "max_iterations": agent_cfg.get("max_iterations", 200),
        "description": agent_cfg.get("description", ""),
        "workspace": get_workspace_path(agent_name),
    }

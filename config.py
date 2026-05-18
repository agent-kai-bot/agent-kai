"""Centralized configuration loaded from agent-config.json."""

import json
import os
import re
import sys

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

import yaml
from daemon.env_utils import _env_positive_int

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "agent-config.json")
AGENTS_YAML_PATH = os.path.join(os.path.dirname(__file__), "agents.yaml")

DEFAULT_HEALTH_PROBE_COMMAND = "echo healthy"
DEFAULT_HEALTH_PROBE_INTERVAL_SECONDS = 60
DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS = 10
DEFAULT_CAPACITY_FEEDBACK_CODES = [429, 503]
DEFAULT_COOLDOWN_SECONDS = 300
DEFAULT_EXECUTOR = "codex"
DEFAULT_OVERFLOW_EXECUTOR = "claude"
DEFAULT_SHELL_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_FILE_READ_CHARS = 250_000
DEFAULT_MAX_OUTPUT_CHARS = 200_000
VALID_EXECUTORS: tuple[str, ...] = ("codex", "claude", "local-llm")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


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
DEFAULT_AGENT = _config.get("default_agent", "kai")
WORKSPACES_DIR = os.path.join(PROJECT_ROOT, _config.get("workspaces_dir", "workspaces"))

# Endpoints registry: name -> {base_url, model, api_key, max_tokens, temperature, top_p}
ENDPOINTS = _config.get("endpoints", {})

# Agents registry: name -> {endpoint, fallback_endpoint, system_prompt, max_iterations, description}
AGENTS = _config.get("agents", {})


class HealthProbeConfig(BaseModel):
    """Health probe command metadata for an agent or executor."""

    model_config = ConfigDict(extra="forbid")

    command: str = DEFAULT_HEALTH_PROBE_COMMAND
    interval_seconds: int = Field(
        default=DEFAULT_HEALTH_PROBE_INTERVAL_SECONDS,
        ge=10,
        le=3600,
    )
    timeout_seconds: int = Field(default=DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS, ge=1)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        """Validate the command string shape without executing it."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("health_probe.command must be a non-empty string")
        if any(ch in value for ch in ("\x00", "\n", "\r")):
            raise ValueError("health_probe.command must be a single-line string")
        return value

    @model_validator(mode="after")
    def validate_timeout(self) -> "HealthProbeConfig":
        """Ensure probe timeouts cannot exceed the probe interval."""
        if self.timeout_seconds > self.interval_seconds:
            raise ValueError(
                "health_probe.timeout_seconds must be less than or equal to "
                "health_probe.interval_seconds"
            )
        return self


class AgentConfig(BaseModel):
    """Health and capacity metadata for a logical agent entry."""

    model_config = ConfigDict(extra="allow")

    health_probe: HealthProbeConfig = Field(default_factory=HealthProbeConfig)
    capacity_feedback_codes: list[StrictInt] = Field(
        default_factory=lambda: list(DEFAULT_CAPACITY_FEEDBACK_CODES)
    )
    cooldown_seconds: int = Field(default=DEFAULT_COOLDOWN_SECONDS, ge=60, le=86400)
    default_executor: str = DEFAULT_EXECUTOR
    overflow_executor: str | None = DEFAULT_OVERFLOW_EXECUTOR

    @field_validator("capacity_feedback_codes")
    @classmethod
    def validate_capacity_codes(cls, value: list[int]) -> list[int]:
        """Validate configured HTTP status codes that signal capacity exhaustion."""
        if not value:
            raise ValueError("capacity_feedback_codes must not be empty")
        invalid = [code for code in value if code < 400 or code > 599]
        if invalid:
            raise ValueError(
                "capacity_feedback_codes values must be integers in the 400-599 range"
            )
        return value

    @field_validator("default_executor", "overflow_executor")
    @classmethod
    def validate_executor_shape(cls, value: str | None) -> str | None:
        """Validate executor reference shape before root-level existence checks."""
        if value is None:
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("executor references must be non-empty strings")
        if not _SLUG_RE.fullmatch(value):
            raise ValueError("executor references must be slug-safe")
        return value


class ExecutorConfig(BaseModel):
    """Health and capacity metadata for a physical executor backend."""

    model_config = ConfigDict(extra="allow")

    provider: str | None = None
    endpoint: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    health_probe: HealthProbeConfig = Field(default_factory=HealthProbeConfig)
    capacity_feedback_codes: list[StrictInt] = Field(
        default_factory=lambda: list(DEFAULT_CAPACITY_FEEDBACK_CODES)
    )
    cooldown_seconds: int = Field(default=DEFAULT_COOLDOWN_SECONDS, ge=60, le=86400)
    overflow_executor: str | None = None

    @field_validator("capacity_feedback_codes")
    @classmethod
    def validate_capacity_codes(cls, value: list[int]) -> list[int]:
        """Validate configured HTTP status codes that signal capacity exhaustion."""
        return AgentConfig.validate_capacity_codes(value)

    @field_validator("overflow_executor")
    @classmethod
    def validate_overflow_executor_shape(cls, value: str | None) -> str | None:
        """Validate executor references before root-level existence checks."""
        return AgentConfig.validate_executor_shape(value)


class AgentsYamlConfig(BaseModel):
    """Parsed `agents.yaml` health and capacity registry."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    executors: dict[str, ExecutorConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        """Validate the supported agents.yaml schema version."""
        if value != 1:
            raise ValueError("agents.yaml version must be 1")
        return value

    @field_validator("executors", "agents")
    @classmethod
    def validate_registry_keys(cls, value: dict[str, object]) -> dict[str, object]:
        """Validate registry IDs are stable slug-safe strings."""
        for key in value:
            if not _SLUG_RE.fullmatch(key):
                raise ValueError(f"registry id '{key}' must be slug-safe")
        return value

    @model_validator(mode="after")
    def validate_executor_references(self) -> "AgentsYamlConfig":
        """Validate agent and executor references resolve to known executor IDs."""
        allowed = set(VALID_EXECUTORS) | set(self.executors)

        for executor_id, executor in self.executors.items():
            overflow = executor.overflow_executor
            if overflow is not None and overflow not in allowed:
                raise ValueError(
                    f"executors.{executor_id}.overflow_executor must be one of "
                    f"{', '.join(sorted(allowed))}"
                )

        for agent_id, agent in self.agents.items():
            if agent.default_executor not in allowed:
                raise ValueError(
                    f"agents.{agent_id}.default_executor must be one of "
                    f"{', '.join(sorted(allowed))}"
                )
            overflow = agent.overflow_executor
            if overflow is not None and overflow not in allowed:
                raise ValueError(
                    f"agents.{agent_id}.overflow_executor must be one of "
                    f"{', '.join(sorted(allowed))}"
                )
        return self


def _normalize_agents_yaml(raw: dict) -> dict:
    """Normalize supported agents.yaml layouts into the versioned schema shape."""
    known_top_level = {"version", "executors", "agents"}
    if "agents" in raw:
        return raw

    flat_agents = {
        key: value for key, value in raw.items() if key not in known_top_level
    }
    if not flat_agents:
        return raw

    normalized = {key: value for key, value in raw.items() if key in known_top_level}
    normalized.setdefault("version", 1)
    normalized["agents"] = flat_agents
    return normalized


def load_agents_yaml(
    path: str = AGENTS_YAML_PATH,
    base_agent_names: dict | list | tuple | set | None = None,
) -> AgentsYamlConfig:
    """Load and validate agents.yaml, applying default health metadata.

    Args:
        path: Filesystem path to the `agents.yaml` registry.
        base_agent_names: Existing logical agent names to merge into the health
            registry. Missing entries receive the schema defaults at load time.

    Returns:
        A validated `AgentsYamlConfig` with defaults applied.

    Raises:
        ValueError: If the YAML document is not a mapping or violates the schema.
        pydantic.ValidationError: If typed field validation fails.
    """
    raw: dict = {"version": 1}
    if path and os.path.exists(path):
        with open(path) as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError("agents.yaml must contain a mapping at the document root")
        raw = _normalize_agents_yaml(loaded)

    parsed = AgentsYamlConfig.model_validate(raw)

    names = tuple(base_agent_names or ())
    if not names:
        return parsed

    merged_agents = dict(parsed.agents)
    for name in names:
        merged_agents.setdefault(name, AgentConfig())
    if merged_agents == parsed.agents:
        return parsed

    return AgentsYamlConfig(
        version=parsed.version,
        executors=parsed.executors,
        agents=merged_agents,
    )


AGENT_HEALTH = load_agents_yaml(base_agent_names=AGENTS)


def get_agent_health_config(agent_name: str) -> AgentConfig:
    """Return health and capacity metadata for a logical agent."""
    return AGENT_HEALTH.agents.get(agent_name, AgentConfig())

# Signal handlers — declarative rules for what to do when a signal
# arrives via NATS (signals.{strategy}.{symbol}). Loaded as raw list
# of dicts here; agent.signal_handlers.load_handlers_from_config
# parses each entry into a SignalHandler dataclass with validation.
# See agent/signal_handlers.py for the full schema.
SIGNAL_HANDLERS = _config.get("signal_handlers", []) or []

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


def _configured_positive_int(value, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def get_shell_timeout_seconds() -> int:
    configured = _configured_positive_int(
        _tool_safety.get("shell_timeout_seconds", DEFAULT_SHELL_TIMEOUT_SECONDS),
        default=DEFAULT_SHELL_TIMEOUT_SECONDS,
    )
    return max(
        1,
        _env_positive_int("KAI_SHELL_TIMEOUT_SECONDS", default=configured),
    )


def get_max_file_read_chars() -> int:
    configured = _configured_positive_int(
        _tool_safety.get("max_file_read_chars", DEFAULT_MAX_FILE_READ_CHARS),
        default=DEFAULT_MAX_FILE_READ_CHARS,
    )
    return max(
        1,
        _env_positive_int("KAI_MAX_FILE_READ_CHARS", default=configured),
    )


def get_max_output_chars() -> int:
    configured = _configured_positive_int(
        _tool_safety.get("max_output_chars", DEFAULT_MAX_OUTPUT_CHARS),
        default=DEFAULT_MAX_OUTPUT_CHARS,
    )
    return max(
        1,
        _env_positive_int("KAI_MAX_OUTPUT_CHARS", default=configured),
    )


SHELL_TIMEOUT_SECONDS = get_shell_timeout_seconds()
MAX_FILE_READ_CHARS = get_max_file_read_chars()
MAX_OUTPUT_CHARS = get_max_output_chars()

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


# ── Reasoning effort (thinking levels) ─────────────────────────
#
# Reasoning-capable models — gpt-5.x via Codex Responses API,
# gpt-5.x via the OpenAI direct API, o1/o3, etc — accept a
# `reasoning_effort` field that controls how much hidden chain-of
# -thought the model burns before producing its answer. Higher
# levels = better answers on hard problems, but slower and more
# expensive (the hidden tokens are billed).
#
# The valid set comes from the openai SDK's
# ``openai.types.shared_params.reasoning_effort.ReasoningEffort``
# typed literal. Aliases like "x-high" / "extreme" are mapped to
# "xhigh" by ``normalize_reasoning_effort``.
VALID_REASONING_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)

# User-facing aliases — accepted on input, normalized to the
# canonical form before being stored or sent on the wire.
_REASONING_EFFORT_ALIASES: dict[str, str] = {
    "x-high": "xhigh",
    "extreme": "xhigh",
    "max": "xhigh",
    "extra": "xhigh",
    "off": "none",
    "min": "minimal",
}


def normalize_reasoning_effort(value: str) -> str | None:
    """Lowercase + alias-resolve a reasoning_effort value.

    Returns the canonical name if valid, else ``None`` (caller
    should reject and show the valid set).
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    v = _REASONING_EFFORT_ALIASES.get(v, v)
    return v if v in VALID_REASONING_EFFORTS else None


def set_agent_reasoning_effort(agent_name: str, effort: str) -> str:
    """Validate and apply a per-agent reasoning effort override.

    Mutates ``AGENTS[agent_name]["reasoning_effort"]`` in memory so
    the next call to ``get_agent_config`` (and the rebuild it
    drives) sees the new value. Does NOT persist to disk — runtime
    overrides are session-scoped, mirroring the existing /model
    behavior. To make a change permanent, edit agent-config.json.

    Returns the canonical effort name on success.
    Raises ``ValueError`` on unknown agent or invalid effort.
    """
    if agent_name not in AGENTS:
        raise ValueError(f"unknown agent '{agent_name}'")
    canonical = normalize_reasoning_effort(effort)
    if canonical is None:
        raise ValueError(
            f"invalid reasoning effort '{effort}' — "
            f"valid: {', '.join(VALID_REASONING_EFFORTS)}"
        )
    AGENTS[agent_name]["reasoning_effort"] = canonical
    return canonical


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
    is also visible to the analyst, risk-manager, and kai agents on
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
    health_cfg = get_agent_health_config(agent_name)
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

    primary_ep = _resolve_endpoint_ref(endpoint_ref)

    # Apply the per-agent reasoning_effort override (if set) to the
    # primary endpoint AND every fallback. This is the only sane
    # injection point: the resolved endpoint dicts come from
    # get_endpoint(), which is shared across agents — mutating the
    # endpoint registry directly would clobber every other agent
    # using the same model. Per-agent overrides need to live on the
    # agent's own copy of the dict, which is what we return here.
    #
    # Set via /think slash command at runtime, or by adding
    # ``"reasoning_effort": "high"`` to the agent block in
    # agent-config.json for a permanent override.
    agent_effort = agent_cfg.get("reasoning_effort")
    if agent_effort:
        canonical = normalize_reasoning_effort(agent_effort) or agent_effort
        if primary_ep is not None:
            primary_ep["reasoning_effort"] = canonical
        for fb in resolved_chain:
            fb["reasoning_effort"] = canonical

    # Load SOUL.md as system prompt if no explicit prompt is set
    system_prompt = agent_cfg.get("system_prompt")
    if not system_prompt:
        soul = load_soul(agent_name)
        if soul:
            system_prompt = soul

    return {
        "endpoint": primary_ep,
        "fallback_endpoint": resolved_chain[0] if resolved_chain else None,
        "fallback_endpoints": resolved_chain,
        "system_prompt": system_prompt,
        "max_iterations": agent_cfg.get("max_iterations", 200),
        "description": agent_cfg.get("description", ""),
        "workspace": get_workspace_path(agent_name),
        "reasoning_effort": agent_cfg.get("reasoning_effort"),
        "health_probe": health_cfg.health_probe.model_dump(),
        "capacity_feedback_codes": list(health_cfg.capacity_feedback_codes),
        "cooldown_seconds": health_cfg.cooldown_seconds,
        "default_executor": health_cfg.default_executor,
        "overflow_executor": health_cfg.overflow_executor,
        # Compatibility shims need the raw top-level routing config while the
        # daemon still consumes this normalized per-agent shape.
        "agents": _config.get("agents", {}),
        "signal_handlers": _config.get("signal_handlers", []),
        # Preserve daemon-scoped settings from agent-config.json for runtime
        # consumers that receive this normalized config dict.  In particular,
        # DaemonServer loads its HeartbeatService settings from the documented
        # top-level {"daemon": {"heartbeat": ...}} section.
        "daemon": _config.get("daemon", {}),
    }

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

# Top-level settings
NATS_URL = _config.get("nats_url", "nats://localhost:4222")
DEFAULT_AGENT = _config.get("default_agent", "nano")
WORKSPACES_DIR = os.path.join(PROJECT_ROOT, _config.get("workspaces_dir", "workspaces"))

# Endpoints registry: name -> {base_url, model, api_key, max_tokens, temperature, top_p}
ENDPOINTS = _config.get("endpoints", {})

# Agents registry: name -> {endpoint, fallback_endpoint, system_prompt, max_iterations, description}
AGENTS = _config.get("agents", {})

# Tool safety
_tool_safety = _config.get("tool_safety", {})
SHELL_TIMEOUT_SECONDS = _tool_safety.get("shell_timeout_seconds", 30)
MAX_FILE_READ_CHARS = _tool_safety.get("max_file_read_chars", 10_000)
MAX_OUTPUT_CHARS = _tool_safety.get("max_output_chars", 5_000)


def get_endpoint(name):
    """Get endpoint config by name. Returns dict with base_url, model, etc."""
    ep = ENDPOINTS.get(name)
    if not ep:
        print(f"Warning: endpoint '{name}' not found in config, available: {list(ENDPOINTS.keys())}")
        return None
    api_key = ep.get("api_key", "not-needed")
    api_key_env = ep.get("api_key_env")
    if api_key_env:
        api_key = os.getenv(api_key_env, api_key)
    return {
        "base_url": ep["base_url"],
        "model": ep["model"],
        "api_key": api_key,
        "api_key_env": api_key_env,
        "max_tokens": ep.get("max_tokens", 4096),
        "temperature": ep.get("temperature", 0.6),
        "top_p": ep.get("top_p", 0.95),
    }


def get_workspace_path(agent_name):
    """Get the workspace directory for an agent. Creates it if needed."""
    agent_cfg = AGENTS.get(agent_name, {})
    workspace_name = agent_cfg.get("workspace", agent_name)
    workspace_path = os.path.join(WORKSPACES_DIR, workspace_name)
    os.makedirs(workspace_path, exist_ok=True)
    return workspace_path


def load_soul(agent_name):
    """Load the SOUL.md for an agent. Returns content string or None."""
    workspace = get_workspace_path(agent_name)
    soul_path = os.path.join(workspace, "SOUL.md")
    if os.path.isfile(soul_path):
        with open(soul_path) as f:
            return f.read()
    return None


def get_agent_config(agent_name):
    """Get agent config by name. Returns dict with endpoint info + agent settings."""
    agent_cfg = AGENTS.get(agent_name, {})
    endpoint_name = agent_cfg.get("endpoint")
    fallback_name = agent_cfg.get("fallback_endpoint")

    # If agent not in config, use first available endpoint as default
    if not endpoint_name:
        endpoint_name = next(iter(ENDPOINTS), None)

    # Load SOUL.md as system prompt if no explicit prompt is set
    system_prompt = agent_cfg.get("system_prompt")
    if not system_prompt:
        soul = load_soul(agent_name)
        if soul:
            system_prompt = soul

    return {
        "endpoint": get_endpoint(endpoint_name) if endpoint_name else None,
        "fallback_endpoint": get_endpoint(fallback_name) if fallback_name else None,
        "system_prompt": system_prompt,
        "max_iterations": agent_cfg.get("max_iterations", 200),
        "description": agent_cfg.get("description", ""),
        "workspace": get_workspace_path(agent_name),
    }

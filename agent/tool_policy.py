"""Central tool policy registry for autonomous-mode safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace

from agent.memory_tool import create_memory_tool
from agent.skills_tool import create_skills_tools
from agent.tools import create_tools


@dataclass(frozen=True)
class ToolPolicy:
    """Execution-safety metadata for one tool."""

    name: str
    read_only: bool = True
    persistent: bool = False
    external_side_effects: bool = False
    long_running: bool = False
    requires_approval_in_auto: bool = False


_TOOL_ALIASES = {
    "get_ohlcv": "query_ohlcv",
    "get_portfolio": "get_positions",
}


def _policy(name: str, **overrides) -> ToolPolicy:
    return ToolPolicy(name=name, **overrides)


_POLICY_OVERRIDES: dict[str, ToolPolicy] = {
    "file_write": _policy(
        "file_write",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "file_edit": _policy(
        "file_edit",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "shell_exec": _policy(
        "shell_exec",
        read_only=False,
        persistent=True,
        external_side_effects=True,
        requires_approval_in_auto=True,
    ),
    "python_exec": _policy(
        "python_exec",
        read_only=False,
        persistent=True,
        external_side_effects=True,
        requires_approval_in_auto=True,
    ),
    "docker_sandbox": _policy(
        "docker_sandbox",
        read_only=False,
        persistent=True,
        long_running=True,
        requires_approval_in_auto=True,
    ),
    "codex_exec": _policy(
        "codex_exec",
        read_only=False,
        external_side_effects=True,
        long_running=True,
        requires_approval_in_auto=True,
    ),
    "claude_exec": _policy(
        "claude_exec",
        read_only=False,
        external_side_effects=True,
        long_running=True,
        requires_approval_in_auto=True,
    ),
    "place_order": _policy(
        "place_order",
        read_only=False,
        persistent=True,
        external_side_effects=True,
        requires_approval_in_auto=True,
    ),
    "spawn_agent": _policy(
        "spawn_agent",
        read_only=False,
        external_side_effects=True,
        requires_approval_in_auto=True,
    ),
    "nats_publish": _policy(
        "nats_publish",
        read_only=False,
        external_side_effects=True,
        requires_approval_in_auto=True,
    ),
    "nats_request": _policy(
        "nats_request",
        read_only=False,
        external_side_effects=True,
        long_running=True,
        requires_approval_in_auto=True,
    ),
    "schedule_at": _policy(
        "schedule_at",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "schedule_recurring": _policy(
        "schedule_recurring",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "schedule_when": _policy(
        "schedule_when",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "cancel_scheduled_job": _policy(
        "cancel_scheduled_job",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "pause_scheduled_job": _policy(
        "pause_scheduled_job",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "resume_scheduled_job": _policy(
        "resume_scheduled_job",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "optimizer_start": _policy(
        "optimizer_start",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "optimizer_pause": _policy(
        "optimizer_pause",
        read_only=False,
        persistent=True,
        requires_approval_in_auto=True,
    ),
    "propose_strategy": _policy(
        "propose_strategy",
        read_only=False,
        persistent=True,
    ),
    "memory": _policy(
        "memory",
        read_only=False,
        persistent=True,
    ),
    "skill_manage": _policy(
        "skill_manage",
        read_only=False,
        persistent=True,
    ),
}


class _StubSignalConsumer:
    count = 0

    def query(self, **_kwargs):
        return []


class _StubBus:
    agent_name = "policy-registry"


class _StubSubAgentManager:
    agents: dict[str, object] = {}


def _collect_tool_names() -> set[str]:
    session = SimpleNamespace(name="policy-session", current_source="user", current_job_id=None)
    scheduler = SimpleNamespace(timezone_name="UTC")
    tools = create_tools(
        bus=_StubBus(),
        sub_agent_manager=_StubSubAgentManager(),
        signal_consumer=_StubSignalConsumer(),
        scheduler=scheduler,
        session=session,
    )
    tools.append(create_memory_tool(None))
    tools.extend(create_skills_tools(None))
    return {tool.name for tool in tools}


@lru_cache(maxsize=1)
def _build_registry() -> dict[str, ToolPolicy]:
    registry = {
        name: ToolPolicy(name=name)
        for name in _collect_tool_names()
    }
    registry.update(_POLICY_OVERRIDES)
    for alias, canonical in _TOOL_ALIASES.items():
        registry[alias] = registry.get(canonical, ToolPolicy(name=canonical))
    return registry


def _canonical_tool_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        return ""
    return _TOOL_ALIASES.get(normalized, normalized)


def get_tool_policy(name: str) -> ToolPolicy:
    """Return the policy for one tool name, defaulting conservatively."""

    canonical = _canonical_tool_name(name)
    if not canonical:
        return ToolPolicy(name="")
    policy = _build_registry().get(canonical)
    if policy is not None:
        return policy
    return ToolPolicy(name=canonical)


def is_auto_safe(name: str) -> bool:
    """Return whether auto mode may use this tool without approval."""

    return not get_tool_policy(name).requires_approval_in_auto


def is_readonly(name: str) -> bool:
    """Return whether this tool is classified as read-only."""

    return get_tool_policy(name).read_only

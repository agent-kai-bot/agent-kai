"""Taskboard agent-id mapping for the local runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass

from config import AGENTS, DEFAULT_AGENT


@dataclass(frozen=True)
class AgentRoute:
    """Resolved taskboard agent route.

    Attributes:
        requested_agent_id: Agent id received from the taskboard.
        local_agent_name: Configured local agent name used by this runtime.
    """

    requested_agent_id: str
    local_agent_name: str


def _parse_aliases(raw: str) -> dict[str, str]:
    """Parse a comma-separated alias mapping.

    Args:
        raw: String in ``external=local,other=agent`` format.

    Returns:
        Mapping of external taskboard ids to local agent names.
    """

    aliases: dict[str, str] = {}
    for pair in raw.split(","):
        if not pair.strip() or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            aliases[key] = value
    return aliases


DEFAULT_ALIASES = {
    "main": DEFAULT_AGENT,
    "kai": DEFAULT_AGENT,
    "qa-agent": "qa-agent" if "qa-agent" in AGENTS else "qa",
}


def configured_aliases() -> dict[str, str]:
    """Return taskboard agent-id aliases from defaults plus environment.

    Returns:
        Alias map where keys are taskboard ids and values are local agents.
    """

    aliases = dict(DEFAULT_ALIASES)
    aliases.update(_parse_aliases(os.getenv("TASKBOARD_AGENT_ALIASES", "")))
    return aliases


def resolve_agent_id(agent_id: str) -> AgentRoute:
    """Resolve a taskboard ``agentId`` to a configured local agent name.

    Args:
        agent_id: Agent id supplied by the taskboard.

    Returns:
        Resolved route containing the requested and local ids.

    Raises:
        ValueError: If ``agent_id`` is empty or cannot be resolved.
    """

    requested = str(agent_id or "").strip()
    if not requested:
        raise ValueError("agentId is required")

    aliases = configured_aliases()
    local = aliases.get(requested, requested)
    if local not in AGENTS:
        known = sorted(set(AGENTS) | set(aliases))
        raise ValueError(
            f"unknown agentId '{requested}'; configured agents: {', '.join(known)}"
        )
    return AgentRoute(requested_agent_id=requested, local_agent_name=local)

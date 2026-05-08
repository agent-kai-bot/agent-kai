"""Agent-pack loading and in-memory role registration for signal-router spawns."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_logger import get_logger

DEFAULT_SPAWN_ENDPOINT = "codex-cli"
DEFAULT_SPAWN_MODEL_ID = "gpt-5.5"
DEFAULT_SPAWN_MAX_ITERATIONS = 30
KAI_ALERT_RESPONSE_SOURCE = "/home/atc/git/OPS/kai-alert-response"


class AgentPackError(RuntimeError):
    """Raised when an agent-pack cannot be resolved or read."""


@dataclass(frozen=True)
class AgentPack:
    """Loaded agent-pack prompt bundle."""

    name: str
    root_path: Path
    system_prompt: str
    decision_logic: str
    tools_reference: str
    schema_path: Path | None
    manifest: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackRoleRegistration:
    """Result of resolving one pack to one in-memory agent role."""

    pack_name: str
    role_name: str
    status: str
    warning: str | None = None


def default_agent_packs_dir() -> Path:
    """Return the daemon's default agent-packs directory."""

    return Path(os.getenv("AGENTKAI_HOME", Path.home() / ".agentkai")).expanduser() / "agent-packs"


def load_pack(pack_name: str, packs_dir: str | Path | None = None) -> AgentPack:
    """Load an agent-pack from ``$AGENTKAI_HOME/agent-packs/<name>/``."""

    name = str(pack_name or "").strip()
    if not name:
        raise AgentPackError("agent-pack name is required")
    root_path = _resolve_pack_root(name, packs_dir)
    system_prompt_path = root_path / "system_prompt.md"
    system_prompt = _read_required(system_prompt_path, name)
    decision_logic = _read_optional(root_path / "decision_logic.md")
    tools_reference = _read_optional(root_path / "tools_reference.md")
    schema_path = root_path / "nats_payload_schema.json"
    assembled_prompt = _assemble_prompt(
        system_prompt,
        decision_logic=decision_logic,
        tools_reference=tools_reference,
    )
    return AgentPack(
        name=name,
        root_path=root_path,
        system_prompt=assembled_prompt,
        decision_logic=decision_logic,
        tools_reference=tools_reference,
        schema_path=schema_path if schema_path.exists() else None,
        manifest={
            "system_prompt_path": str(system_prompt_path),
            "decision_logic_path": str(root_path / "decision_logic.md")
            if decision_logic
            else None,
            "tools_reference_path": str(root_path / "tools_reference.md")
            if tools_reference
            else None,
            "schema_path": str(schema_path) if schema_path.exists() else None,
            "readme_path": str(root_path / "README.md")
            if (root_path / "README.md").exists()
            else None,
            "example_alarms_path": str(root_path / "example_alarms")
            if (root_path / "example_alarms").exists()
            else None,
        },
    )


def register_pack_role(
    pack: AgentPack,
    *,
    agents: dict[str, Any] | None = None,
    logger: Any | None = None,
) -> PackRoleRegistration:
    """Resolve a loaded pack to an in-memory sub-agent role."""

    if agents is None:
        from config import AGENTS

        agents = AGENTS
    log = logger or get_logger("daemon.signal_router.agent_pack")
    role_name = pack.name
    existing = agents.get(role_name)
    if isinstance(existing, dict):
        warning = _existing_role_warning(existing, pack)
        if warning:
            log.warning("%s", warning)
            existing["system_prompt"] = pack.system_prompt
            return PackRoleRegistration(pack.name, role_name, "matched_existing_updated", warning)
        return PackRoleRegistration(pack.name, role_name, "matched_existing")

    agents[role_name] = {
        "description": f"Auto-registered from agent-pack {pack.name}",
        "endpoint": DEFAULT_SPAWN_ENDPOINT,
        "model": DEFAULT_SPAWN_MODEL_ID,
        "model_id": DEFAULT_SPAWN_MODEL_ID,
        "workspace": pack.name,
        "system_prompt": pack.system_prompt,
        "max_iterations": DEFAULT_SPAWN_MAX_ITERATIONS,
    }
    return PackRoleRegistration(pack.name, role_name, "auto_registered")


def load_and_register_pack_role(
    pack_name: str,
    *,
    packs_dir: str | Path | None = None,
    agents: dict[str, Any] | None = None,
    logger: Any | None = None,
) -> PackRoleRegistration:
    """Load an agent-pack and register its role in memory."""

    pack = load_pack(pack_name, packs_dir=packs_dir)
    return register_pack_role(pack, agents=agents, logger=logger)


def _resolve_pack_root(pack_name: str, packs_dir: str | Path | None) -> Path:
    base = Path(packs_dir).expanduser() if packs_dir is not None else default_agent_packs_dir()
    root_path = base / pack_name
    if root_path.exists() and root_path.is_dir():
        return root_path
    if root_path.is_symlink():
        raise AgentPackError(
            f"agent-pack {pack_name!r} symlink target is missing at {root_path}. "
            f"{_staging_instruction(pack_name)}"
        )
    raise AgentPackError(
        f"agent-pack {pack_name!r} not found at {root_path}. "
        f"{_staging_instruction(pack_name)}"
    )


def _read_required(path: Path, pack_name: str) -> str:
    if not path.is_file():
        raise AgentPackError(
            f"agent-pack {pack_name!r} is missing required system_prompt.md at {path}"
        )
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentPackError(
            f"agent-pack {pack_name!r} system_prompt.md is unreadable at {path}: {exc}"
        ) from exc


def _read_optional(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _assemble_prompt(
    system_prompt: str,
    *,
    decision_logic: str,
    tools_reference: str,
) -> str:
    parts = [system_prompt.strip()]
    if decision_logic:
        parts.append(f"## Decision logic\n\n{decision_logic.strip()}")
    if tools_reference:
        parts.append(f"## Tools reference\n\n{tools_reference.strip()}")
    return "\n\n".join(part for part in parts if part).strip()


def _existing_role_warning(existing: dict[str, Any], pack: AgentPack) -> str | None:
    prompt = existing.get("system_prompt")
    if prompt == pack.system_prompt:
        return None
    if isinstance(prompt, str) and _prompt_path_points_at_pack(prompt, pack):
        return None
    return (
        f"agent-pack {pack.name!r} matched existing role with divergent system_prompt; "
        "using pack content in memory"
    )


def _prompt_path_points_at_pack(prompt: str, pack: AgentPack) -> bool:
    raw = os.path.expandvars(prompt).strip()
    if not raw:
        return False
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return False
    try:
        return path.resolve() == (pack.root_path / "system_prompt.md").resolve()
    except OSError:
        return False


def _staging_instruction(pack_name: str) -> str:
    if pack_name == "kai-alert-response":
        return (
            "Stage it with: mkdir -p ~/.agentkai/agent-packs && "
            f"ln -s {KAI_ALERT_RESPONSE_SOURCE} "
            "~/.agentkai/agent-packs/kai-alert-response"
        )
    return "Stage the pack under $AGENTKAI_HOME/agent-packs/<name>/."

"""Daemon core primitives.

Phase 1 builds the in-process runtime here before any network transport
or multi-client support is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import AGENTS, WORKSPACES_DIR

DEFAULT_SESSION_NAME = "terminal"
DEFAULT_WATCHLIST_SYMBOLS = ("BTC", "ETH", "SOL")


def _normalize_session_name(name: str) -> str:
    """Validate and normalize a session name for filesystem use."""
    if not isinstance(name, str):
        raise TypeError("session name must be a string")
    normalized = name.strip()
    if not normalized:
        raise ValueError("session name cannot be empty")
    if normalized in {".", ".."} or "/" in normalized:
        raise ValueError("session name must not contain path separators")
    return normalized


@dataclass(frozen=True)
class SessionPaths:
    """Filesystem layout reserved for a session."""

    root_dir: Path
    state_path: Path
    sub_agents_dir: Path
    memory_dir: Path

    @classmethod
    def for_name(cls, name: str) -> "SessionPaths":
        root_dir = Path(WORKSPACES_DIR) / "sessions" / name
        return cls(
            root_dir=root_dir,
            state_path=root_dir.with_suffix(".json"),
            sub_agents_dir=root_dir / "sub_agents",
            memory_dir=root_dir / "memory",
        )


@dataclass
class SessionUIState:
    """Per-session UI state mirrored out of the terminal."""

    chart_symbol: str = "BTC"
    chart_timeframe: str = "1m"
    chart_source: str = "kai-api"
    chart_layout_mode: str = "dashboard"
    chart_color_scheme: str = "classic"
    watchlist_symbols: list[str] = field(
        default_factory=lambda: list(DEFAULT_WATCHLIST_SYMBOLS)
    )
    autotrade_enabled: bool = False
    activity_status: str = "idle"


@dataclass(frozen=True)
class SubAgentTemplate:
    """Read-only shared metadata for a configured sub-agent."""

    name: str
    description: str = ""
    workspace: str = ""
    system_prompt: str | None = None


@lru_cache(maxsize=1)
def load_sub_agent_templates() -> dict[str, SubAgentTemplate]:
    """Load shared sub-agent templates once per daemon process."""
    templates: dict[str, SubAgentTemplate] = {}
    for name, cfg in AGENTS.items():
        if name == "kai":
            continue
        templates[name] = SubAgentTemplate(
            name=name,
            description=cfg.get("description", ""),
            workspace=cfg.get("workspace", name),
            system_prompt=cfg.get("system_prompt"),
        )
    return templates


@dataclass
class SessionSubAgentState:
    """Mutable per-session state for one configured sub-agent."""

    template: SubAgentTemplate
    buffer_path: Path
    memory_dir: Path
    chat_history: list[Any] = field(default_factory=list)
    runtime: Any = None

    @property
    def name(self) -> str:
        return self.template.name


class SessionSubAgentPool:
    """Per-session sub-agent state built from shared templates."""

    def __init__(
        self,
        session_name: str,
        paths: SessionPaths,
        templates: dict[str, SubAgentTemplate] | None = None,
    ) -> None:
        self.session_name = session_name
        self.paths = paths
        self.templates = templates or load_sub_agent_templates()
        self._states = {
            name: SessionSubAgentState(
                template=template,
                buffer_path=self.paths.sub_agents_dir / f"{name}.json",
                memory_dir=self.paths.memory_dir,
            )
            for name, template in self.templates.items()
        }

    def get(self, name: str) -> SessionSubAgentState:
        return self._states[name]

    def items(self):
        return self._states.items()

    def names(self) -> list[str]:
        return list(self._states)

    def __contains__(self, name: str) -> bool:
        return name in self._states

    def __len__(self) -> int:
        return len(self._states)


class Session:
    """Named in-process session with isolated mutable state."""

    def __init__(self, name: str = DEFAULT_SESSION_NAME) -> None:
        self.name = _normalize_session_name(name)
        self.paths = SessionPaths.for_name(self.name)

        self.chat_history: list[Any] = []
        self.input_queue: list[str] = []
        self.ui_state = SessionUIState()
        self.agent_runner: Any = None
        self.signal_consumer: Any = None
        self.event_bus: Any = None

        self.sub_agent_pool = SessionSubAgentPool(
            session_name=self.name,
            paths=self.paths,
        )

    @property
    def activity_status(self) -> str:
        return self.ui_state.activity_status

    def set_activity_status(self, status: str) -> None:
        self.ui_state.activity_status = status or "idle"

    def __repr__(self) -> str:
        return (
            f"Session(name={self.name!r}, chat_history={len(self.chat_history)}, "
            f"queued_inputs={len(self.input_queue)}, sub_agents={len(self.sub_agent_pool)})"
        )

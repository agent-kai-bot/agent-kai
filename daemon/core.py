"""Daemon core primitives.

Phase 1 builds the in-process runtime here before any network transport
or multi-client support is introduced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent.core import AgentRunner
from agent.signal_consumer import SignalConsumer
from agent.sub_agents import SubAgentManager
from agent.tools import create_tools
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


class SessionSubAgentRegistry:
    """Session-scoped facade over the existing sub-agent manager."""

    def __init__(self, pool: SessionSubAgentPool) -> None:
        self.pool = pool
        self._manager: SubAgentManager | None = None

    @property
    def agents(self) -> dict[str, Any]:
        if self._manager is None:
            return {}
        return self._manager.agents

    def bind_bus(self, bus: Any) -> None:
        self._manager = SubAgentManager(bus) if bus is not None else None

    async def spawn(
        self,
        name: str,
        system_prompt: str | None = None,
        initial_task: str | None = None,
    ) -> str:
        if self._manager is None:
            return "Sub-agents require a connected bus."
        result = await self._manager.spawn(
            name,
            system_prompt=system_prompt,
            initial_task=initial_task,
        )
        if name in self.pool and name in self._manager.agents:
            self.pool.get(name).runtime = self._manager.agents[name]
        return result

    async def stop(self, name: str) -> str:
        if self._manager is None:
            return f"No agent named '{name}'."
        result = await self._manager.stop(name)
        if name in self.pool:
            self.pool.get(name).runtime = None
        return result

    def list_agents(self) -> list[str]:
        if self._manager is None:
            return []
        return self._manager.list_agents()

    async def stop_all(self) -> None:
        if self._manager is None:
            return
        await self._manager.stop_all()
        for _name, state in self.pool.items():
            state.runtime = None


@dataclass(frozen=True)
class SessionEvent:
    """One event emitted on a session-local bus."""

    session_name: str
    topic: str
    payload: dict[str, Any]


class SessionEventBus:
    """Minimal per-session pub/sub bus backed by asyncio queues."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self._subscriptions: dict[str, list[asyncio.Queue[SessionEvent]]] = {}

    def subscribe(self, topic: str = "*") -> asyncio.Queue[SessionEvent]:
        queue: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self._subscriptions.setdefault(topic, []).append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SessionEvent]) -> None:
        for queues in self._subscriptions.values():
            if queue in queues:
                queues.remove(queue)

    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> SessionEvent:
        event = SessionEvent(
            session_name=self.session_name,
            topic=topic,
            payload=dict(payload or {}),
        )
        recipients: list[asyncio.Queue[SessionEvent]] = []
        recipients.extend(self._subscriptions.get("*", []))
        recipients.extend(self._subscriptions.get(topic, []))
        seen: set[int] = set()
        for queue in recipients:
            qid = id(queue)
            if qid in seen:
                continue
            seen.add(qid)
            queue.put_nowait(event)
        return event


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
        self.event_bus = SessionEventBus(self.name)
        self.agent_name: str | None = None

        self.sub_agent_pool = SessionSubAgentPool(
            session_name=self.name,
            paths=self.paths,
        )
        self.sub_agent_registry = SessionSubAgentRegistry(self.sub_agent_pool)
        # Backwards-compat alias for the current terminal wiring.
        self.sub_agent_manager = self.sub_agent_registry

    @property
    def activity_status(self) -> str:
        return self.ui_state.activity_status

    def set_activity_status(self, status: str) -> None:
        self.ui_state.activity_status = status or "idle"
        self.publish_event("status.updated", {"status": self.ui_state.activity_status})

    def subscribe_events(self, topic: str = "*") -> asyncio.Queue[SessionEvent]:
        return self.event_bus.subscribe(topic)

    def publish_event(
        self,
        topic: str,
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent:
        return self.event_bus.publish(topic, payload)

    def queue_input(self, text: str) -> None:
        self.input_queue.append(text)
        self.publish_event(
            "input.queued",
            {"text": text, "depth": len(self.input_queue)},
        )

    def pop_input(self) -> str | None:
        if not self.input_queue:
            return None
        text = self.input_queue.pop(0)
        self.publish_event(
            "input.dequeued",
            {"text": text, "depth": len(self.input_queue)},
        )
        return text

    def attach_runtime(
        self,
        *,
        bus: Any = None,
        agent_name: str = "kai",
        signal_consumer: SignalConsumer | None = None,
    ) -> AgentRunner:
        """Attach the in-process agent runtime to this session."""
        self.agent_name = agent_name
        self.signal_consumer = signal_consumer or SignalConsumer()
        self.sub_agent_registry.bind_bus(bus)

        tools = create_tools(
            bus,
            self.sub_agent_registry if bus is not None else None,
            signal_consumer=self.signal_consumer,
        )
        self.agent_runner = AgentRunner(
            tools=tools,
            bus=bus,
            agent_name=agent_name,
        )
        self.agent_runner.chat_history = self.chat_history
        self.publish_event(
            "runtime.attached",
            {"agent_name": agent_name, "bus_enabled": bus is not None},
        )
        return self.agent_runner

    async def stream_agent_events(self, user_input: str):
        """Stream agent events through the session bus."""
        if self.agent_runner is None:
            raise RuntimeError("session runtime is not attached")

        self.publish_event("input.received", {"text": user_input})
        async for event in self.agent_runner.run(user_input):
            etype = event.get("type", "unknown")
            data = event.get("data")
            payload = data if isinstance(data, dict) else {"value": data}
            self.publish_event(f"agent.{etype}", payload)
            yield event

    def __repr__(self) -> str:
        return (
            f"Session(name={self.name!r}, chat_history={len(self.chat_history)}, "
            f"queued_inputs={len(self.input_queue)}, sub_agents={len(self.sub_agent_pool)})"
        )

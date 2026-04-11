"""Daemon core primitives.

Phase 1 builds the in-process runtime here before any network transport
or multi-client support is introduced.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent.auto_prompt import parse_auto_state
from agent.core import AgentRunner
from agent.signal_consumer import SignalConsumer
from agent.strategy_agent_tools import InProcessStrategyRuntime
from agent.sub_agents import SubAgentManager
from agent.tools import create_tools
from agent_logger import log_auto_event
from config import AGENTS, WORKSPACES_DIR
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

DEFAULT_SESSION_NAME = "terminal"
DEFAULT_WATCHLIST_SYMBOLS = ("BTC", "ETH", "SOL")
DEFAULT_AUTO_MAX_ITERATIONS = 20
MAX_AUTO_ITERATIONS = 100
DEFAULT_AUTO_MAX_DURATION_SECONDS = 180.0
SESSIONS_ROOT_DIR = Path(WORKSPACES_DIR) / "sessions"
SESSION_INDEX_PATH = SESSIONS_ROOT_DIR / "index.json"
RESERVED_SESSION_NAMES = frozenset({"index"})


def _utc_now_iso() -> str:
    """Return a stable UTC ISO-8601 timestamp for persistence metadata."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _normalize_session_name(name: str) -> str:
    """Validate and normalize a session name for filesystem use."""
    if not isinstance(name, str):
        raise TypeError("session name must be a string")
    normalized = name.strip()
    if not normalized:
        raise ValueError("session name cannot be empty")
    if normalized in {".", ".."} or "/" in normalized:
        raise ValueError("session name must not contain path separators")
    if normalized.casefold() in {reserved.casefold() for reserved in RESERVED_SESSION_NAMES}:
        raise ValueError(f"session name '{normalized}' is reserved")
    return normalized


def _flatten_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content) if content is not None else ""


def _role_for_message(message: Any) -> str:
    cls = type(message).__name__.lower()
    if "human" in cls:
        return "human"
    if "system" in cls:
        return "system"
    return "ai"


def _tool_name_from_auto_pause_reason(reason: str | None) -> str:
    text = str(reason or "").strip()
    if text.startswith("requires approval for "):
        return text.removeprefix("requires approval for ").strip() or "unknown"
    if text.startswith("auto readonly blocks non-read-only tool:"):
        return text.split(":", 1)[1].strip() or "unknown"
    return "unknown"


def serialize_messages(messages: list[Any]) -> list[dict[str, str]]:
    """Convert LangChain messages into the persisted JSON format."""
    serialized: list[dict[str, str]] = []
    for message in messages:
        content = _flatten_message_content(getattr(message, "content", ""))
        if not content:
            continue
        serialized.append(
            {
                "role": _role_for_message(message),
                "content": content,
            }
        )
    return serialized


def deserialize_messages(entries: list[dict[str, Any]]) -> list[Any]:
    """Rebuild LangChain messages from persisted JSON."""
    restored: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content", "")
        if not isinstance(content, str) or not content:
            continue
        role = entry.get("role", "")
        if role == "human":
            restored.append(HumanMessage(content=content))
        elif role == "system":
            restored.append(SystemMessage(content=content))
        else:
            restored.append(AIMessage(content=content))
    return restored


@contextmanager
def _json_file_lock(path: Path):
    """Lock a sidecar file so load-merge-write stays serialized."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_merge_write_json(path: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """Atomically merge a JSON patch into an on-disk dict."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _json_file_lock(path):
        merged = _read_json_dict(path)
        merged.update(patch)
        _write_json_dict_unlocked(path, merged)
    return merged


def _write_json_dict(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a JSON file with the supplied dict."""
    with _json_file_lock(path):
        _write_json_dict_unlocked(path, payload)


@dataclass(frozen=True)
class SessionIndexEntry:
    """Persisted metadata for one known session."""

    name: str
    state_path: str
    created_at: str
    last_activity: str


def _load_session_index_entries(
    index_path: Path | None = None,
) -> dict[str, SessionIndexEntry]:
    index_path = index_path or SESSION_INDEX_PATH
    payload = _read_json_dict(index_path)
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, dict):
        return {}

    entries: dict[str, SessionIndexEntry] = {}
    for name, raw_entry in raw_sessions.items():
        if not isinstance(name, str) or not isinstance(raw_entry, dict):
            continue
        state_path = raw_entry.get("state_path")
        created_at = raw_entry.get("created_at")
        last_activity = raw_entry.get("last_activity")
        if not isinstance(state_path, str) or not state_path:
            state_path = str(SessionPaths.for_name(name).state_path)
        if not isinstance(created_at, str) or not created_at:
            created_at = last_activity if isinstance(last_activity, str) and last_activity else _utc_now_iso()
        if not isinstance(last_activity, str) or not last_activity:
            last_activity = created_at
        entries[name] = SessionIndexEntry(
            name=name,
            state_path=state_path,
            created_at=created_at,
            last_activity=last_activity,
        )
    return entries


def list_indexed_sessions(index_path: Path | None = None) -> list[SessionIndexEntry]:
    """Return the persisted session index in stable alphabetical order."""
    return sorted(_load_session_index_entries(index_path).values(), key=lambda entry: entry.name)


def get_indexed_session(
    name: str,
    index_path: Path | None = None,
) -> SessionIndexEntry | None:
    """Look up one session in the persisted session index."""
    return _load_session_index_entries(index_path).get(_normalize_session_name(name))


def upsert_indexed_session(
    name: str,
    *,
    state_path: Path | None = None,
    last_activity: str | None = None,
    index_path: Path | None = None,
) -> SessionIndexEntry:
    """Create or refresh one session entry in the persisted index."""
    normalized = _normalize_session_name(name)
    activity_at = last_activity or _utc_now_iso()
    entry_path = str(state_path or SessionPaths.for_name(normalized).state_path)
    index_path = index_path or SESSION_INDEX_PATH

    index_path.parent.mkdir(parents=True, exist_ok=True)
    with _json_file_lock(index_path):
        payload = _read_json_dict(index_path)
        raw_sessions = payload.get("sessions")
        sessions = raw_sessions if isinstance(raw_sessions, dict) else {}
        existing = sessions.get(normalized) if isinstance(sessions.get(normalized), dict) else {}
        created_at = existing.get("created_at") if isinstance(existing.get("created_at"), str) else activity_at
        entry = SessionIndexEntry(
            name=normalized,
            state_path=entry_path,
            created_at=created_at,
            last_activity=activity_at,
        )
        sessions[normalized] = asdict(entry)
        payload["version"] = 1
        payload["sessions"] = sessions
        _write_json_dict_unlocked(index_path, payload)
    return entry


def remove_indexed_session(
    name: str,
    index_path: Path | None = None,
) -> bool:
    """Remove one session from the persisted session index."""
    normalized = _normalize_session_name(name)
    index_path = index_path or SESSION_INDEX_PATH
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with _json_file_lock(index_path):
        payload = _read_json_dict(index_path)
        raw_sessions = payload.get("sessions")
        sessions = raw_sessions if isinstance(raw_sessions, dict) else {}
        if normalized not in sessions:
            return False
        sessions.pop(normalized, None)
        payload["version"] = 1
        payload["sessions"] = sessions
        _write_json_dict_unlocked(index_path, payload)
    return True


def _write_json_dict_unlocked(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON dict assuming the caller already holds the file lock."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


@dataclass(frozen=True)
class SessionPaths:
    """Filesystem layout reserved for a session."""

    root_dir: Path
    state_path: Path
    sub_agents_dir: Path
    memory_dir: Path

    @classmethod
    def for_name(cls, name: str) -> "SessionPaths":
        root_dir = SESSIONS_ROOT_DIR / name
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
        self.strategy_runtime: Any = None
        self.event_bus = SessionEventBus(self.name)
        self.agent_name: str | None = None
        self.current_source: str = "user"
        self.current_job_id: str | None = None
        self.auto_mode: bool = False
        self.auto_readonly: bool = False
        self.auto_iterations_remaining: int = 0
        self.auto_iterations_total: int = 0
        self.auto_start_time: float = 0.0
        self.auto_max_duration: float = DEFAULT_AUTO_MAX_DURATION_SECONDS

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

    @staticmethod
    def _normalize_auto_iterations(max_iterations: int | None) -> int:
        if max_iterations is None:
            return DEFAULT_AUTO_MAX_ITERATIONS
        return max(1, min(MAX_AUTO_ITERATIONS, int(max_iterations)))

    @staticmethod
    def _tool_input_key(value: Any) -> str:
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except TypeError:
            return repr(value)

    def auto_elapsed_seconds(self) -> float:
        if self.auto_start_time <= 0:
            return 0.0
        return max(0.0, time.monotonic() - self.auto_start_time)

    def auto_status_payload(self, *, reason: str | None = None) -> dict[str, Any]:
        total = int(self.auto_iterations_total)
        remaining = int(max(0, self.auto_iterations_remaining))
        used = max(0, total - remaining)
        payload = {
            "enabled": bool(self.auto_mode),
            "readonly": bool(self.auto_readonly),
            "iterations_total": total,
            "iterations_remaining": remaining,
            "iterations_used": used,
            "elapsed_seconds": round(self.auto_elapsed_seconds(), 3),
        }
        if reason:
            payload["reason"] = reason
        return payload

    def start_auto_mode(
        self,
        *,
        max_iterations: int = DEFAULT_AUTO_MAX_ITERATIONS,
        readonly: bool = False,
    ) -> dict[str, Any]:
        """Enable autonomous multi-turn execution for the next task."""

        normalized_iterations = self._normalize_auto_iterations(max_iterations)
        self.auto_mode = True
        self.auto_readonly = bool(readonly)
        self.auto_iterations_total = normalized_iterations
        self.auto_iterations_remaining = normalized_iterations
        self.auto_start_time = time.monotonic()
        if self.agent_runner is not None:
            self.agent_runner._auto_readonly = self.auto_readonly
            self.agent_runner.set_auto_mode(True, max_iterations=normalized_iterations)
        log_auto_event(self, "AUTO_START", [f"budget={normalized_iterations}"])
        payload = self.auto_status_payload()
        self.publish_event("auto.started", payload)
        return payload

    def stop_auto_mode(self, reason: str) -> dict[str, Any]:
        """Disable autonomous mode and publish the stop reason."""

        payload = self.auto_status_payload(reason=reason)
        payload["enabled"] = False
        log_auto_event(
            self,
            "AUTO_STOP",
            [
                f"reason={reason}",
                f"iterations={payload['iterations_used']}",
                f"elapsed={payload['elapsed_seconds']:.1f}s",
            ],
        )
        self.auto_mode = False
        self.auto_readonly = False
        self.auto_iterations_remaining = 0
        self.auto_iterations_total = 0
        self.auto_start_time = 0.0
        if self.agent_runner is not None:
            self.agent_runner._auto_readonly = False
            self.agent_runner.set_auto_mode(False)
        self.publish_event("auto.stopped", payload)
        return payload

    def publish_auto_progress(self) -> dict[str, Any]:
        """Publish one auto-progress update to the session bus."""

        payload = self.auto_status_payload()
        self.publish_event("auto.progress", payload)
        return payload

    def attach_runtime(
        self,
        *,
        bus: Any = None,
        agent_name: str = "kai",
        signal_consumer: SignalConsumer | None = None,
        scheduler: Any = None,
    ) -> AgentRunner:
        """Attach the in-process agent runtime to this session."""
        self.agent_name = agent_name
        self.signal_consumer = signal_consumer or SignalConsumer()
        if self.strategy_runtime is None:
            self.strategy_runtime = InProcessStrategyRuntime(
                session_name=self.name,
                agent_name=agent_name,
                event_callback=self.publish_event,
            )
        self.sub_agent_registry.bind_bus(bus)

        tools = create_tools(
            bus,
            self.sub_agent_registry if bus is not None else None,
            signal_consumer=self.signal_consumer,
            scheduler=scheduler,
            session=self,
        )
        self.agent_runner = AgentRunner(
            tools=tools,
            bus=bus,
            agent_name=agent_name,
        )
        self.agent_runner.chat_history = self.chat_history
        if self.auto_mode:
            self.agent_runner._auto_readonly = self.auto_readonly
            self.agent_runner.set_auto_mode(
                True,
                max_iterations=max(1, self.auto_iterations_remaining or self.auto_iterations_total),
            )
        self.publish_event(
            "runtime.attached",
            {"agent_name": agent_name, "bus_enabled": bus is not None},
        )
        return self.agent_runner

    def ensure_layout(self) -> None:
        """Create the on-disk directories reserved for this session."""
        self.paths.root_dir.mkdir(parents=True, exist_ok=True)
        self.paths.sub_agents_dir.mkdir(parents=True, exist_ok=True)
        self.paths.memory_dir.mkdir(parents=True, exist_ok=True)

    def touch_index(self) -> SessionIndexEntry:
        """Ensure this session is discoverable in the persisted session index."""
        self.ensure_layout()
        return upsert_indexed_session(
            self.name,
            state_path=self.paths.state_path,
        )

    def save(self) -> None:
        """Persist the session state and every sub-agent buffer."""
        self.ensure_layout()
        state_patch = {
            "name": self.name,
            "agent_name": self.agent_name,
            "chat_history": serialize_messages(self.chat_history),
            "input_queue": list(self.input_queue),
            "ui_state": asdict(self.ui_state),
        }
        _load_merge_write_json(self.paths.state_path, state_patch)
        for name, state in self.sub_agent_pool.items():
            _load_merge_write_json(
                state.buffer_path,
                {
                    "name": name,
                    "chat_history": serialize_messages(state.chat_history),
                },
            )
        self.touch_index()
        self.publish_event(
            "session.persisted",
            {"path": str(self.paths.state_path)},
        )

    def load(self) -> None:
        """Load persisted state from disk if it exists."""
        self.ensure_layout()
        state = _read_json_dict(self.paths.state_path)
        ui_state = state.get("ui_state")
        if isinstance(ui_state, dict):
            for field_name in (
                "chart_symbol",
                "chart_timeframe",
                "chart_source",
                "chart_layout_mode",
                "chart_color_scheme",
                "watchlist_symbols",
                "autotrade_enabled",
                "activity_status",
            ):
                if field_name in ui_state:
                    setattr(self.ui_state, field_name, ui_state[field_name])

        messages = state.get("chat_history")
        if isinstance(messages, list):
            self.chat_history.clear()
            self.chat_history.extend(deserialize_messages(messages))

        queue_items = state.get("input_queue")
        if isinstance(queue_items, list):
            self.input_queue[:] = [item for item in queue_items if isinstance(item, str)]

        self.agent_name = state.get("agent_name") or self.agent_name

        for name, sub_agent_state in self.sub_agent_pool.items():
            payload = _read_json_dict(sub_agent_state.buffer_path)
            messages = payload.get("chat_history")
            if isinstance(messages, list):
                sub_agent_state.chat_history[:] = deserialize_messages(messages)

        if self.agent_runner is not None:
            self.agent_runner.chat_history = self.chat_history

        self.publish_event(
            "session.loaded",
            {"path": str(self.paths.state_path)},
        )

    async def stream_agent_events(
        self,
        user_input: str,
        *,
        source: str = "user",
        job_id: str | None = None,
        tool_budget: int | None = None,
    ):
        """Stream agent events through the session bus."""
        if self.agent_runner is None:
            raise RuntimeError("session runtime is not attached")

        if source == "scheduler" and job_id:
            self.chat_history.append(SystemMessage(content=f"[scheduled job: {job_id}]"))
            self.agent_runner.chat_history = self.chat_history

        self.publish_event(
            "input.received",
            {"text": user_input, "source": source, "job_id": job_id},
        )
        previous_source = self.current_source
        previous_job_id = self.current_job_id
        self.current_source = source
        self.current_job_id = job_id
        budget_override = getattr(self.agent_runner, "override_max_iterations", None)
        budget_context = (
            budget_override(tool_budget)
            if tool_budget is not None and callable(budget_override)
            else nullcontext()
        )
        try:
            with budget_context:
                current_input = user_input
                is_auto_continuation = False
                repeated_tool_calls: dict[tuple[str, str], int] = {}
                consecutive_no_tool_turns = 0
                last_final_text: str | None = None

                while True:
                    if self.auto_mode:
                        current_iteration = max(
                            1,
                            self.auto_iterations_total - self.auto_iterations_remaining + 1,
                        )
                        log_auto_event(
                            self,
                            "AUTO_CYCLE",
                            [f"iter={current_iteration}/{max(1, self.auto_iterations_total)}"],
                        )
                        self.agent_runner._auto_readonly = self.auto_readonly
                        self.agent_runner.set_auto_mode(
                            True,
                            max_iterations=max(
                                1,
                                self.auto_iterations_remaining or self.auto_iterations_total or 1,
                            ),
                        )

                    self.agent_runner._is_auto_continuation = is_auto_continuation
                    turn_final_text = ""
                    turn_tool_calls: list[tuple[str, str]] = []
                    try:
                        async for event in self.agent_runner.run(current_input):
                            etype = event.get("type", "unknown")
                            data = event.get("data")
                            if etype == "final":
                                turn_final_text = str(data or "")
                            elif etype == "tool_start" and isinstance(data, dict):
                                turn_tool_calls.append(
                                    (
                                        str(data.get("tool") or ""),
                                        self._tool_input_key(data.get("input")),
                                    )
                                )
                            payload = data if isinstance(data, dict) else {"value": data}
                            self.publish_event(f"agent.{etype}", payload)
                            yield event
                    finally:
                        self.agent_runner._is_auto_continuation = False

                    if not self.auto_mode:
                        break

                    runtime_pause_reason = self.agent_runner.consume_auto_pause_reason()
                    self.auto_iterations_remaining = max(0, self.auto_iterations_remaining - 1)
                    progress_payload = self.publish_auto_progress()
                    yield {"type": "auto_progress", "data": progress_payload}
                    if not self.auto_mode:
                        break

                    if runtime_pause_reason:
                        log_auto_event(
                            self,
                            "AUTO_TOOL_BLOCKED",
                            [
                                f"tool={_tool_name_from_auto_pause_reason(runtime_pause_reason)}",
                                f"reason={runtime_pause_reason}",
                            ],
                        )
                        stopped = self.stop_auto_mode(runtime_pause_reason)
                        yield {"type": "auto_stopped", "data": stopped}
                        break

                    auto_state, auto_reason = parse_auto_state(turn_final_text)
                    log_auto_event(
                        self,
                        "AUTO_STATE",
                        [
                            f"state={auto_state}",
                            f"reason={auto_reason or '<none>'}",
                        ],
                    )
                    if auto_state == "done":
                        stopped = self.stop_auto_mode(auto_reason or "task complete")
                        yield {"type": "auto_stopped", "data": stopped}
                        break
                    if auto_state == "pause":
                        stopped = self.stop_auto_mode(auto_reason or "paused")
                        yield {"type": "auto_stopped", "data": stopped}
                        break

                    if self.auto_iterations_remaining <= 0:
                        stopped = self.stop_auto_mode("iteration budget exhausted")
                        yield {"type": "auto_stopped", "data": stopped}
                        break

                    if self.auto_elapsed_seconds() > self.auto_max_duration:
                        stopped = self.stop_auto_mode("wall-clock budget exceeded")
                        yield {"type": "auto_stopped", "data": stopped}
                        break

                    normalized_final = turn_final_text.strip()
                    if normalized_final and normalized_final == last_final_text:
                        log_auto_event(
                            self,
                            "AUTO_LOOP_DETECTED",
                            ["type=repeated_final_response"],
                        )
                        stopped = self.stop_auto_mode("loop detected: repeated final response")
                        yield {"type": "auto_stopped", "data": stopped}
                        break
                    if normalized_final:
                        last_final_text = normalized_final

                    if turn_tool_calls:
                        consecutive_no_tool_turns = 0
                    else:
                        consecutive_no_tool_turns += 1
                        if consecutive_no_tool_turns >= 2:
                            log_auto_event(
                                self,
                                "AUTO_LOOP_DETECTED",
                                ["type=consecutive_no_tool_turns"],
                            )
                            stopped = self.stop_auto_mode("loop detected: consecutive no-tool turns")
                            yield {"type": "auto_stopped", "data": stopped}
                            break

                    loop_detected = False
                    for tool_key in turn_tool_calls:
                        repeated_tool_calls[tool_key] = repeated_tool_calls.get(tool_key, 0) + 1
                        if repeated_tool_calls[tool_key] >= 3:
                            loop_detected = True
                            break
                    if loop_detected:
                        log_auto_event(
                            self,
                            "AUTO_LOOP_DETECTED",
                            ["type=repeated_tool_call"],
                        )
                        stopped = self.stop_auto_mode("loop detected: repeated tool call")
                        yield {"type": "auto_stopped", "data": stopped}
                        break

                    if auto_state != "continue":
                        stopped = self.stop_auto_mode("missing or malformed AUTO_STATE footer")
                        yield {"type": "auto_stopped", "data": stopped}
                        break

                    log_auto_event(self, "AUTO_CONTINUE", ["injecting hidden turn"])
                    current_input = "Continue with the next step."
                    is_auto_continuation = True
        finally:
            self.current_source = previous_source
            self.current_job_id = previous_job_id

    def __repr__(self) -> str:
        return (
            f"Session(name={self.name!r}, chat_history={len(self.chat_history)}, "
            f"queued_inputs={len(self.input_queue)}, sub_agents={len(self.sub_agent_pool)})"
        )

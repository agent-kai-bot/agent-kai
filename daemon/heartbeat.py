"""Daemon-owned heartbeat tick service."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""

    return datetime.now(timezone.utc)


def _iso_z(value: datetime) -> str:
    """Serialize a datetime using the daemon's compact UTC ``Z`` form."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HeartbeatTick:
    """One daemon-owned periodic heartbeat tick."""

    seq: int
    emitted_at: str
    monotonic_seconds: float
    interval_seconds: float
    source: str = "daemon"
    reason: str = "periodic"
    type: str = "heartbeat.tick"

    def to_event_payload(self) -> dict[str, Any]:
        """Return the JSON-ready payload for daemon/session event buses."""

        return {
            "seq": self.seq,
            "emitted_at": self.emitted_at,
            "monotonic_seconds": self.monotonic_seconds,
            "interval_seconds": self.interval_seconds,
            "source": self.source,
            "reason": self.reason,
            "type": self.type,
        }


@dataclass(frozen=True)
class HeartbeatConfig:
    """Runtime configuration for the daemon heartbeat service."""

    enabled: bool = True
    interval_seconds: float = 1800.0
    publish_session_events: bool = True
    prompt_template_path: str = "prompts/heartbeat/main.md.tmpl"
    max_injected_turns_per_hour: int = 0


class SafeFormatDict(dict):
    """Format mapping that keeps unknown placeholders visible."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class HeartbeatPromptTemplate:
    """Git-tracked prompt template rendered for heartbeat turns."""

    name: str
    path: Path
    content: str

    @classmethod
    def load(cls, template_path: str | Path) -> "HeartbeatPromptTemplate":
        path = Path(template_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FileNotFoundError(
                f"heartbeat prompt template is not readable: {path}"
            ) from exc
        return cls(name=path.name, path=path, content=content)

    def render(
        self,
        tick: HeartbeatTick,
        *,
        session_name: str,
        agent_name: str | None,
    ) -> str:
        values = SafeFormatDict(
            seq=tick.seq,
            emitted_at=tick.emitted_at,
            interval_seconds=tick.interval_seconds,
            session_name=session_name,
            agent_name=agent_name or "",
            source=tick.source,
            reason=tick.reason,
        )
        return self.content.format_map(values)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def load_heartbeat_config(config: dict[str, Any] | None = None) -> HeartbeatConfig:
    """Load heartbeat config from agent config plus environment overrides."""

    heartbeat = ((config or {}).get("daemon") or {}).get("heartbeat") or {}
    enabled = bool(heartbeat.get("enabled", True))
    publish_session_events = bool(heartbeat.get("publish_session_events", True))
    try:
        interval_seconds = float(heartbeat.get("interval_seconds", 1800.0))
    except (TypeError, ValueError):
        interval_seconds = 1800.0
    prompt_template_path = str(
        heartbeat.get("prompt_template_path") or "prompts/heartbeat/main.md.tmpl"
    )
    try:
        max_injected_turns_per_hour = int(
            heartbeat.get("max_injected_turns_per_hour", 0)
        )
    except (TypeError, ValueError):
        max_injected_turns_per_hour = 0

    enabled = _env_bool("KAI_HEARTBEAT_ENABLED", enabled)
    publish_session_events = _env_bool(
        "KAI_HEARTBEAT_PUBLISH_SESSION_EVENTS",
        publish_session_events,
    )
    env_interval = os.getenv("KAI_HEARTBEAT_INTERVAL_SECONDS")
    if env_interval is not None:
        try:
            interval_seconds = float(env_interval)
        except ValueError:
            pass
    env_template = os.getenv("KAI_HEARTBEAT_PROMPT_TEMPLATE_PATH")
    if env_template:
        prompt_template_path = env_template
    env_max = os.getenv("KAI_HEARTBEAT_MAX_INJECTED_TURNS_PER_HOUR")
    if env_max is not None:
        try:
            max_injected_turns_per_hour = int(env_max)
        except ValueError:
            pass
    if _env_bool("KAI_HEARTBEAT_INJECTION_KILL_SWITCH", False):
        max_injected_turns_per_hour = 0

    interval_seconds = max(0.1, interval_seconds)
    max_injected_turns_per_hour = max(0, max_injected_turns_per_hour)
    return HeartbeatConfig(
        enabled=enabled,
        interval_seconds=interval_seconds,
        publish_session_events=publish_session_events,
        prompt_template_path=prompt_template_path,
        max_injected_turns_per_hour=max_injected_turns_per_hour,
    )


class HeartbeatService:
    """Own a periodic async task that emits daemon heartbeat ticks."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        tick_callback: Callable[[HeartbeatTick], Awaitable[None]],
        clock: Callable[[], datetime] = utc_now,
        enabled: bool = True,
    ) -> None:
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.tick_callback = tick_callback
        self.clock = clock
        self.enabled = enabled
        self._task: asyncio.Task[None] | None = None
        self._seq = 0
        self.last_tick: HeartbeatTick | None = None
        self.tick_count = 0
        self.failure_count = 0

    @property
    def running(self) -> bool:
        """Return True when the background heartbeat loop is active."""

        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the background loop if enabled and not already running."""

        if not self.enabled or self.running:
            return
        self._task = asyncio.create_task(self._run(), name="daemon-heartbeat")

    async def shutdown(self) -> None:
        """Cancel and await the background loop."""

        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def emit_once(self) -> HeartbeatTick:
        """Emit a single tick immediately; useful for tests and diagnostics."""

        self._seq += 1
        tick = HeartbeatTick(
            seq=self._seq,
            emitted_at=_iso_z(self.clock()),
            monotonic_seconds=time.monotonic(),
            interval_seconds=self.interval_seconds,
        )
        await self.tick_callback(tick)
        self.last_tick = tick
        self.tick_count += 1
        return tick

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.emit_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep service alive after callback failures
                self.failure_count += 1

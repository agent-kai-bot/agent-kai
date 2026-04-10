"""Phase 2 daemon WebSocket server."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shlex
import shutil
import time
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.signal_consumer import Signal, SignalConsumer
from config import DEFAULT_AGENT, NATS_URL
from daemon.auth import DAEMON_TOKEN_PATH, ensure_daemon_token, is_local_client_host, parse_bearer_token
from daemon.core import (
    Session,
    SessionEvent,
    get_indexed_session,
    list_indexed_sessions,
    remove_indexed_session,
    serialize_messages,
)
from daemon.scheduler import DaemonEventBus, Scheduler
from daemon.protocol import (
    AttachEnvelope,
    ChartBarEnvelope,
    ClientEnvelope,
    ErrorEnvelope,
    FinalEnvelope,
    HeartbeatEnvelope,
    InputEnvelope,
    InterruptEnvelope,
    NatsEventEnvelope,
    ScheduledJobCancelledEnvelope,
    ScheduledJobCompletedEnvelope,
    ScheduledJobCreatedEnvelope,
    ScheduledJobFailedEnvelope,
    ScheduledJobPausedEnvelope,
    ScheduledJobResumedEnvelope,
    ScheduledJobTriggeredEnvelope,
    SessionAttachedEnvelope,
    SessionStateSnapshot,
    SignalEnvelope,
    SlashEnvelope,
    StatusEnvelope,
    SubscribeEnvelope,
    TokenEnvelope,
    ToolEndEnvelope,
    ToolStartEnvelope,
    UnsubscribeEnvelope,
    decode_client_envelope,
    encode_envelope,
)
from nats_bus.bus import NatsBus

DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 8765
DEFAULT_DAEMON_WS_PATH = "/ws"
DEFAULT_DAEMON_WS_URL = (
    f"ws://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}{DEFAULT_DAEMON_WS_PATH}"
)
DEFAULT_WEB_BUILD_DIR = Path(__file__).resolve().parent.parent / "web" / "build"
_SCHEDULE_IN_PATTERN = re.compile(
    r"^in\s+(?P<count>\d+)\s+(?P<unit>minute|minutes|hour|hours|day|days)$",
    re.IGNORECASE,
)
_SCHEDULE_TOMORROW_PATTERN = re.compile(
    r"^tomorrow(?:\s+(?P<time>.+))?$",
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_schedule_at_value(raw: str, *, now: datetime | None = None) -> str:
    """Parse a minimal slash-command time expression into an ISO timestamp."""
    reference = now or _utc_now()
    text = raw.strip()
    if not text:
        raise ValueError("schedule time cannot be empty")

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.replace(microsecond=0).isoformat()

    in_match = _SCHEDULE_IN_PATTERN.match(text)
    if in_match:
        count = int(in_match.group("count"))
        unit = in_match.group("unit").lower()
        if "minute" in unit:
            target = reference + timedelta(minutes=count)
        elif "hour" in unit:
            target = reference + timedelta(hours=count)
        else:
            target = reference + timedelta(days=count)
        return target.replace(microsecond=0).isoformat()

    tomorrow_match = _SCHEDULE_TOMORROW_PATTERN.match(text)
    if tomorrow_match:
        tomorrow = (reference + timedelta(days=1)).astimezone(timezone.utc)
        clock = (tomorrow_match.group("time") or "09:00").strip().lower()
        for fmt in ("%H:%M", "%H", "%I%p", "%I:%M%p", "%I %p", "%I:%M %p"):
            try:
                parsed_time = datetime.strptime(clock, fmt)
            except ValueError:
                continue
            target = tomorrow.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=0,
                microsecond=0,
            )
            return target.isoformat()
        raise ValueError("unsupported tomorrow time format; use ISO, 'in N minutes', or 'tomorrow 7am'")

    raise ValueError("unsupported schedule time format; use ISO, 'in N minutes', or 'tomorrow 7am'")


def _looks_like_web_asset_request(asset_path: str) -> bool:
    normalized = asset_path.strip("/")
    if not normalized:
        return False
    tail = normalized.rsplit("/", 1)[-1]
    return normalized.startswith("_app/") or "." in tail


def _resolve_web_asset_path(build_dir: Path, asset_path: str) -> Path | None:
    normalized = asset_path.strip("/")
    candidate = (build_dir / normalized).resolve() if normalized else (build_dir / "index.html").resolve()
    try:
        candidate.relative_to(build_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _missing_web_build_response(build_dir: Path) -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<!doctype html><html><head><title>KAI Web UI unavailable</title></head>"
            "<body><h1>Web UI build not found</h1>"
            f"<p>Expected built assets under <code>{build_dir}</code>.</p></body></html>"
        ),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _fetch_watchlist_quote(symbol: str) -> dict[str, Any]:
    from agent.data_sources.coinbase import fetch_latest_price

    return fetch_latest_price(symbol)


def _load_portfolio_snapshot() -> dict[str, Any]:
    from data_api.paper_trading import portfolio

    return {
        "positions": portfolio.get_positions(),
        "pnl": portfolio.get_pnl(),
    }


def _load_chart_history(
    symbol: str,
    interval: str,
    source: str,
    limit: int = 300,
) -> list[dict[str, Any]]:
    normalized_source = source.strip().lower() or "kai-api"
    if normalized_source == "kai-api":
        from agent.data_sources.kai_api import fetch_candles

        return fetch_candles(symbol.upper(), interval, limit)
    if normalized_source == "coinbase":
        from agent.data_sources.coinbase import fetch_candles

        return fetch_candles(symbol.upper(), interval, min(limit, 300))
    raise ValueError(f"unsupported chart source '{source}'")


def _process_memory_bytes() -> int | None:
    """Return the current process RSS when the host exposes /proc."""
    statm_path = Path("/proc/self/statm")
    try:
        fields = statm_path.read_text(encoding="utf-8").split()
        resident_pages = int(fields[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (IndexError, OSError, ValueError):
        return None
    return resident_pages * page_size


@dataclass
class ManagedSession:
    """Server-owned session plus per-session coordination state."""

    session: Session
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class InputRunResult:
    """Outcome of running one input turn through a session."""

    final_text: str = ""
    error: str | None = None


class SessionCreateRequest(BaseModel):
    """Payload for REST session creation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


class DaemonServer:
    """FastAPI-facing daemon runtime that owns sessions and a shared bus."""

    def __init__(
        self,
        *,
        agent_name: str = DEFAULT_AGENT,
        nats_url: str = NATS_URL,
        bus_factory: Callable[[str, str], Any] | None = None,
        scheduler_factory: Callable[..., Scheduler] | None = None,
        token_path: str | Path | None = None,
        allow_unauthenticated_local: bool = True,
    ) -> None:
        self.agent_name = agent_name
        self.nats_url = nats_url
        self.bus_factory = bus_factory or self._default_bus_factory
        self.scheduler_factory = scheduler_factory or Scheduler
        self.token_path = Path(token_path) if token_path is not None else DAEMON_TOKEN_PATH
        self.allow_unauthenticated_local = allow_unauthenticated_local
        self.bus: Any | None = None
        self.sessions: dict[str, ManagedSession] = {}
        self.event_bus = DaemonEventBus()
        self.signal_consumer = SignalConsumer()
        self.scheduler: Scheduler | None = None
        self.daemon_token = ""
        self.started_at_monotonic: float | None = None
        self.log = logging.getLogger(__name__)

    @staticmethod
    def _default_bus_factory(url: str, agent_name: str) -> NatsBus:
        return NatsBus(url=url, agent_name=agent_name)

    async def startup(self) -> None:
        """Connect shared resources used by daemon-backed sessions."""
        self.daemon_token = ensure_daemon_token(self.token_path)
        self.started_at_monotonic = time.monotonic()
        self.signal_consumer.on_signal = self._handle_signal
        if self.bus_factory is None:
            self.bus = None
        else:
            try:
                bus = self.bus_factory(self.nats_url, self.agent_name)
                await bus.connect()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("daemon bus connect failed: %s", exc)
                self.bus = None
            else:
                self.bus = bus
                with suppress(Exception):
                    self.bus.on_message(self._handle_nats_message)
                with suppress(Exception):
                    await self.signal_consumer.subscribe(bus)

        self.scheduler = self.scheduler_factory(
            dispatch_callback=self._handle_scheduled_job_trigger,
            event_bus=self.event_bus,
            event_callback=self._handle_scheduler_event,
        )
        await self.scheduler.start()

    async def shutdown(self) -> None:
        """Stop all managed runtime resources."""
        for managed in self.sessions.values():
            with suppress(Exception):
                await managed.session.sub_agent_registry.stop_all()

        if self.scheduler is not None:
            with suppress(Exception):
                await self.scheduler.shutdown()
            self.scheduler = None

        if self.bus is not None:
            with suppress(Exception):
                await self.bus.disconnect()
            self.bus = None

    def uptime_seconds(self) -> float:
        """Return daemon uptime in seconds since the current process booted."""
        if self.started_at_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at_monotonic)

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return detailed daemon metrics for diagnostics and smoke tests."""
        queue_depth_by_session = {
            name: len(managed.session.input_queue)
            for name, managed in sorted(self.sessions.items())
        }
        scheduler_jobs = self.scheduler.list_jobs() if self.scheduler is not None else []
        scheduler_status_counts = Counter(job.status for job in scheduler_jobs)
        return {
            "agent_name": self.agent_name,
            "uptime_seconds": round(self.uptime_seconds(), 3),
            "bus_connected": self.bus is not None,
            "process": {
                "pid": os.getpid(),
                "memory_rss_bytes": _process_memory_bytes(),
            },
            "sessions": {
                "live_count": len(self.sessions),
                "indexed_count": len(list_indexed_sessions()),
                "queue_depth": {
                    "total": sum(queue_depth_by_session.values()),
                    "per_session": queue_depth_by_session,
                },
                "activity": {
                    name: managed.session.activity_status
                    for name, managed in sorted(self.sessions.items())
                },
            },
            "scheduler": {
                "job_count": len(scheduler_jobs),
                "status_counts": dict(sorted(scheduler_status_counts.items())),
            },
        }

    def health_snapshot(self) -> dict[str, Any]:
        """Return a compact health payload for readiness checks."""
        metrics = self.metrics_snapshot()
        return {
            "status": "ok",
            "agent_name": self.agent_name,
            "bus_connected": metrics["bus_connected"],
            "session_count": metrics["sessions"]["live_count"],
            "uptime_seconds": metrics["uptime_seconds"],
            "memory_rss_bytes": metrics["process"]["memory_rss_bytes"],
            "agent_queue_depth": metrics["sessions"]["queue_depth"]["total"],
            "scheduler_job_count": metrics["scheduler"]["job_count"],
        }

    def _is_authorized(self, *, token: str | None, client_host: str | None) -> bool:
        if token and self.daemon_token and secrets.compare_digest(token, self.daemon_token):
            return True
        return self.allow_unauthenticated_local and is_local_client_host(client_host)

    def require_http_auth(self, request: Request) -> None:
        """Reject unauthorized REST requests."""
        client_host = request.client.host if request.client is not None else None
        token = parse_bearer_token(request.headers.get("authorization"))
        if self._is_authorized(token=token, client_host=client_host):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="daemon bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def authorize_websocket(self, websocket: WebSocket) -> bool:
        """Validate websocket auth before the attach handshake."""
        client_host = websocket.client.host if websocket.client is not None else None
        token = parse_bearer_token(websocket.headers.get("authorization"))
        if token is None:
            token = websocket.query_params.get("token")
        return self._is_authorized(token=token, client_host=client_host)

    async def get_or_create_session(
        self,
        name: str,
        *,
        create_if_missing: bool,
    ) -> ManagedSession:
        """Return a live session, hydrating it from disk on first access."""
        if name in self.sessions:
            return self.sessions[name]

        session = Session(name)
        state_exists = session.paths.state_path.exists()
        indexed_entry = get_indexed_session(name)
        if not create_if_missing and not (state_exists or indexed_entry):
            raise KeyError(f"session '{name}' does not exist")

        session.load()
        session.attach_runtime(
            bus=self.bus,
            agent_name=self.agent_name,
            signal_consumer=self.signal_consumer,
            scheduler=self.scheduler,
        )
        session.touch_index()

        managed = ManagedSession(session=session)
        self.sessions[session.name] = managed
        return managed

    async def run_input(
        self,
        managed: ManagedSession,
        text: str,
        *,
        source: str = "user",
        job_id: str | None = None,
        tool_budget: int | None = None,
    ) -> InputRunResult:
        """Run one input turn through the target session."""
        result = InputRunResult()
        async with managed.input_lock:
            managed.session.set_activity_status("thinking...")
            try:
                async for event in managed.session.stream_agent_events(
                    text,
                    source=source,
                    job_id=job_id,
                    tool_budget=tool_budget,
                ):
                    if event.get("type") == "final":
                        result.final_text = str(event.get("data") or "")
                    elif event.get("type") == "error":
                        result.error = str(event.get("data") or "agent stream failed")
            except Exception as exc:  # noqa: BLE001
                result.error = str(exc)
                managed.session.publish_event("agent.error", {"value": str(exc)})
            finally:
                managed.session.set_activity_status("idle")
                with suppress(Exception):
                    managed.session.save()
        return result

    async def publish_daemon_event(self, channel: str, payload: dict[str, Any]) -> None:
        """Publish one daemon-scoped event to scheduler subscribers."""
        await self.event_bus.publish(channel, payload)

    async def _handle_scheduled_job_trigger(self, job, fired_at) -> None:
        """Dispatch one scheduled job into its owner session."""
        if self.scheduler is None:
            return
        self.scheduler.notify_triggered(job.id, fired_at=fired_at)

        try:
            managed = await self.get_or_create_session(
                job.owner_session,
                create_if_missing=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.scheduler.record_failure(job.id, fired_at=fired_at, error=str(exc))
            self.log.warning("scheduled job %s failed to attach session: %s", job.id, exc)
            return

        if job.concurrency == "skip" and managed.input_lock.locked():
            self.log.info(
                "scheduled job %s skipped because session %s is busy",
                job.id,
                job.owner_session,
            )
            return

        outcome = await self.run_input(
            managed,
            job.prompt,
            source="scheduler",
            job_id=job.id,
            tool_budget=job.tool_budget,
        )
        if outcome.error:
            self.scheduler.record_failure(job.id, fired_at=fired_at, error=outcome.error)
            return
        self.scheduler.record_completion(
            job.id,
            fired_at=fired_at,
            result_preview=outcome.final_text,
        )

    def _handle_scheduler_event(self, event_type: str, *, job, **payload: Any) -> None:
        """Publish scheduler lifecycle events onto the owner session bus."""
        managed = self.sessions.get(job.owner_session)
        if managed is None:
            return

        topic_map = {
            "created": "scheduled_job.created",
            "triggered": "scheduled_job.triggered",
            "completed": "scheduled_job.completed",
            "failed": "scheduled_job.failed",
            "cancelled": "scheduled_job.cancelled",
            "paused": "scheduled_job.paused",
            "resumed": "scheduled_job.resumed",
        }
        topic = topic_map.get(event_type)
        if not topic:
            return

        event_payload: dict[str, Any]
        if event_type == "created":
            event_payload = {"job": job.model_dump(mode="json")}
        elif event_type == "triggered":
            event_payload = {"job_id": job.id, "fired_at": payload.get("fired_at")}
        elif event_type == "completed":
            event_payload = {
                "job_id": job.id,
                "result_preview": payload.get("result_preview"),
            }
        elif event_type == "failed":
            event_payload = {"job_id": job.id, "error": payload.get("error") or "job failed"}
        else:
            event_payload = {"job_id": job.id}

        managed.session.publish_event(topic, event_payload)

    async def handle_schedule_command(self, managed: ManagedSession, command_text: str) -> str:
        """Execute one scheduler slash command for the attached session."""
        if self.scheduler is None:
            raise RuntimeError("scheduler is unavailable")

        try:
            parts = shlex.split(command_text)
        except ValueError as exc:
            raise ValueError(f"invalid schedule command: {exc}") from exc

        if not parts or parts[0] != "/schedule":
            raise ValueError("unsupported schedule command")

        session_name = managed.session.name
        sub = parts[1].lower() if len(parts) > 1 else "list"

        if sub == "list":
            show_all = len(parts) > 2 and parts[2].lower() == "all"
            jobs = (
                self.scheduler.list_jobs()
                if show_all
                else self.scheduler.list_jobs_for_session(session_name)
            )
            visible = [job for job in jobs if job.status in {"active", "paused"}]
            if not visible:
                scope = "all sessions" if show_all else f"session {session_name}"
                return f"No scheduled jobs for {scope}."
            return "\n".join(self._format_scheduled_job(job) for job in visible)

        if sub == "show":
            if len(parts) < 3:
                raise ValueError("usage: /schedule show JOB_ID")
            job = self._require_session_job(session_name, parts[2])
            return self._format_scheduled_job(job, include_prompt=True)

        if sub == "add":
            if len(parts) < 5:
                raise ValueError("usage: /schedule add at|cron ...")
            mode = parts[2].lower()
            if mode == "at":
                when = _parse_schedule_at_value(parts[3])
                job = self.scheduler.create_absolute_job(
                    when=when,
                    prompt=parts[4],
                    owner_session=session_name,
                    created_by="user",
                )
                return f"Scheduled {job.id} at {job.next_run} for session {job.owner_session}."
            if mode == "cron":
                job = self.scheduler.create_recurring_job(
                    cron=parts[3],
                    prompt=parts[4],
                    owner_session=session_name,
                    created_by="user",
                )
                return f"Scheduled recurring job {job.id} next={job.next_run}."
            raise ValueError("usage: /schedule add at|cron ...")

        if sub == "cancel":
            if len(parts) < 3:
                raise ValueError("usage: /schedule cancel JOB_ID")
            job = self._require_session_job(session_name, parts[2])
            self.scheduler.cancel_job(job.id)
            return f"Cancelled scheduled job {job.id}."

        if sub == "pause":
            if len(parts) < 3:
                raise ValueError("usage: /schedule pause JOB_ID|all [--global]")
            target = parts[2].lower()
            if target == "all":
                global_scope = "--global" in parts[3:]
                paused = self.scheduler.pause_all_jobs(None if global_scope else session_name)
                scope = "all sessions" if global_scope else f"session {session_name}"
                if not paused:
                    return f"No active scheduled jobs to pause for {scope}."
                return f"Paused {len(paused)} scheduled jobs for {scope}."
            job = self._require_session_job(session_name, parts[2])
            self.scheduler.pause_job(job.id)
            return f"Paused scheduled job {job.id}."

        if sub == "resume":
            if len(parts) < 3:
                raise ValueError("usage: /schedule resume JOB_ID")
            job = self._require_session_job(session_name, parts[2])
            self.scheduler.resume_job(job.id)
            return f"Resumed scheduled job {job.id}."

        raise ValueError("unsupported schedule command")

    def _require_session_job(self, session_name: str, job_id: str):
        if self.scheduler is None:
            raise RuntimeError("scheduler is unavailable")
        job = self.scheduler.get_job(job_id)
        if job is None:
            raise ValueError(f"scheduled job '{job_id}' was not found")
        if job.owner_session != session_name:
            raise ValueError(f"scheduled job '{job_id}' belongs to session '{job.owner_session}'")
        return job

    @staticmethod
    def _format_scheduled_job(job, *, include_prompt: bool = False) -> str:
        next_run = job.next_run or ("event-driven" if job.type == "event" else "n/a")
        text = (
            f"{job.id} [{job.status}] session={job.owner_session} "
            f"type={job.type} next={next_run}"
        )
        if include_prompt:
            text = f"{text} prompt={job.prompt}"
        return text

    def _handle_signal(self, signal: Signal) -> None:
        """Fan out shared signal events to live sessions and the daemon bus."""
        payload = signal.to_dict()
        for managed in self.sessions.values():
            managed.session.publish_event("signal.received", {"signal": payload})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish_daemon_event("signals", payload))

    def _handle_nats_message(self, direction: str, subject: str, payload: dict[str, Any]) -> None:
        """Mirror shared NATS traffic into each live session bus."""
        event_payload = {
            "direction": direction,
            "subject": subject,
            "payload": payload,
        }
        for managed in self.sessions.values():
            managed.session.publish_event("nats.message", event_payload)

    def describe_session(self, name: str) -> dict[str, Any]:
        """Return one session summary suitable for REST and slash listings."""
        entry = get_indexed_session(name)
        if entry is None:
            raise KeyError(f"session '{name}' does not exist")
        managed = self.sessions.get(entry.name)
        activity_status = managed.session.activity_status if managed else "idle"
        queued_inputs = len(managed.session.input_queue) if managed else 0
        return {
            "name": entry.name,
            "created_at": entry.created_at,
            "last_activity": entry.last_activity,
            "state_path": entry.state_path,
            "activity_status": activity_status,
            "queued_inputs": queued_inputs,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return the persisted session index merged with live runtime state."""
        return [self.describe_session(entry.name) for entry in list_indexed_sessions()]

    async def create_session(self, name: str) -> dict[str, Any]:
        """Create a new named session and persist its initial state."""
        normalized = Session(name).name
        if (
            normalized in self.sessions
            or get_indexed_session(normalized) is not None
            or Session(normalized).paths.state_path.exists()
        ):
            raise FileExistsError(f"session '{normalized}' already exists")

        managed = await self.get_or_create_session(
            normalized,
            create_if_missing=True,
        )
        managed.session.save()
        return self.describe_session(normalized)

    async def delete_session(self, name: str) -> dict[str, Any]:
        """Delete a named session from memory, disk, and the session index."""
        session = Session(name)
        normalized = session.name
        state_exists = session.paths.state_path.exists()
        indexed_entry = get_indexed_session(normalized)
        managed = self.sessions.pop(normalized, None)
        if managed is None and not state_exists and indexed_entry is None:
            raise KeyError(f"session '{normalized}' does not exist")

        if managed is not None:
            with suppress(Exception):
                await managed.session.sub_agent_registry.stop_all()

        for path in (
            session.paths.state_path,
            session.paths.state_path.with_suffix(session.paths.state_path.suffix + ".lock"),
        ):
            with suppress(FileNotFoundError):
                path.unlink()

        if session.paths.root_dir.exists():
            shutil.rmtree(session.paths.root_dir, ignore_errors=True)

        remove_indexed_session(normalized)
        return {"deleted": True, "name": normalized}

    async def forward_session_events(
        self,
        websocket: WebSocket,
        session: Session,
        event_queue: asyncio.Queue[SessionEvent],
        subscriptions: dict[str, Any],
    ) -> None:
        """Translate session-bus events into daemon wire messages."""
        tool_start_times: dict[str, float] = {}
        while True:
            event = await event_queue.get()
            message = self._event_to_message(
                session=session,
                event=event,
                subscriptions=subscriptions,
                tool_start_times=tool_start_times,
            )
            if message is None:
                continue
            await _send_server_envelope(websocket, message)

    def _event_to_message(
        self,
        *,
        session: Session,
        event: SessionEvent,
        subscriptions: dict[str, Any],
        tool_start_times: dict[str, float],
    ):
        """Map one internal session event to a WS envelope."""
        topic = event.topic
        payload = event.payload

        if topic == "agent.token":
            text = payload.get("value")
            return TokenEnvelope(type="token", text=text or "")

        if topic == "agent.tool_start":
            tool = str(payload.get("tool") or "")
            if tool:
                tool_start_times[tool] = time.monotonic()
            return ToolStartEnvelope(
                type="tool_start",
                tool=tool,
                args=payload.get("input"),
            )

        if topic == "agent.tool_end":
            tool = str(payload.get("tool") or "")
            started_at = tool_start_times.pop(tool, None)
            elapsed_ms = None
            if started_at is not None:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return ToolEndEnvelope(
                type="tool_end",
                tool=tool,
                elapsed_ms=elapsed_ms,
                ok=True,
            )

        if topic == "agent.final":
            return FinalEnvelope(type="final", text=payload.get("value") or "")

        if topic == "agent.status":
            return StatusEnvelope(
                type="status",
                activity=payload.get("value") or "idle",
                queue=len(session.input_queue),
            )

        if topic == "agent.error":
            return ErrorEnvelope(
                type="error",
                code="agent_error",
                message=payload.get("value") or "agent stream failed",
            )

        if topic == "status.updated":
            return StatusEnvelope(
                type="status",
                activity=payload.get("status") or "idle",
                queue=len(session.input_queue),
            )

        if topic == "input.queued" or topic == "input.dequeued":
            return StatusEnvelope(
                type="status",
                activity=session.activity_status,
                queue=payload.get("depth", len(session.input_queue)),
            )

        if topic == "signal.received":
            if not subscriptions.get("signals"):
                return None
            signal = payload.get("signal")
            return SignalEnvelope(type="signal", signal=signal or payload)

        if topic == "chart.bar":
            chart_subs = subscriptions.get("chart", set())
            symbol = payload.get("symbol")
            timeframe = payload.get("tf")
            if chart_subs and (symbol, timeframe) not in chart_subs:
                return None
            if not chart_subs:
                return None
            return ChartBarEnvelope(
                type="chart_bar",
                symbol=symbol,
                tf=timeframe,
                bar=payload.get("bar"),
            )

        if topic == "nats.message":
            if not subscriptions.get("nats"):
                return None
            direction = str(payload.get("direction") or "")
            subject = str(payload.get("subject") or "")
            if not direction or not subject:
                return None
            return NatsEventEnvelope(
                type="nats_event",
                direction=direction,
                subject=subject,
                payload=payload.get("payload"),
            )

        if topic == "scheduled_job.created":
            return ScheduledJobCreatedEnvelope(
                type="scheduled_job_created",
                job=payload.get("job"),
            )

        if topic == "scheduled_job.triggered":
            return ScheduledJobTriggeredEnvelope(
                type="scheduled_job_triggered",
                job_id=str(payload.get("job_id") or ""),
                fired_at=str(payload.get("fired_at") or ""),
            )

        if topic == "scheduled_job.completed":
            return ScheduledJobCompletedEnvelope(
                type="scheduled_job_completed",
                job_id=str(payload.get("job_id") or ""),
                result_preview=payload.get("result_preview"),
            )

        if topic == "scheduled_job.failed":
            return ScheduledJobFailedEnvelope(
                type="scheduled_job_failed",
                job_id=str(payload.get("job_id") or ""),
                error=str(payload.get("error") or "job failed"),
            )

        if topic == "scheduled_job.cancelled":
            return ScheduledJobCancelledEnvelope(
                type="scheduled_job_cancelled",
                job_id=str(payload.get("job_id") or ""),
            )

        if topic == "scheduled_job.paused":
            return ScheduledJobPausedEnvelope(
                type="scheduled_job_paused",
                job_id=str(payload.get("job_id") or ""),
            )

        if topic == "scheduled_job.resumed":
            return ScheduledJobResumedEnvelope(
                type="scheduled_job_resumed",
                job_id=str(payload.get("job_id") or ""),
            )

        return None

    @staticmethod
    def session_snapshot(session: Session) -> SessionStateSnapshot:
        """Serialize the attach-time state snapshot for one session."""
        return SessionStateSnapshot(
            chart_symbol=session.ui_state.chart_symbol,
            chart_timeframe=session.ui_state.chart_timeframe,
            chart_source=session.ui_state.chart_source,
            chart_layout_mode=session.ui_state.chart_layout_mode,
            chart_color_scheme=session.ui_state.chart_color_scheme,
            watchlist_symbols=list(session.ui_state.watchlist_symbols),
            autotrade_enabled=bool(session.ui_state.autotrade_enabled),
            activity_status=session.ui_state.activity_status,
            chat_history=serialize_messages(session.chat_history),
        )


async def _receive_client_envelope(websocket: WebSocket) -> ClientEnvelope:
    return decode_client_envelope(await websocket.receive_text())


async def _send_server_envelope(websocket: WebSocket, envelope) -> None:
    await websocket.send_json(encode_envelope(envelope))


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await _send_server_envelope(
        websocket,
        ErrorEnvelope(
            type="error",
            code=code,
            message=message,
        ),
    )


def create_app(
    *,
    agent_name: str = DEFAULT_AGENT,
    nats_url: str = NATS_URL,
    bus_factory: Callable[[str, str], Any] | None = None,
    scheduler_factory: Callable[..., Scheduler] | None = None,
    web_build_dir: str | Path | None = None,
    token_path: str | Path | None = None,
    allow_unauthenticated_local: bool = True,
) -> FastAPI:
    """Build the FastAPI app that exposes the daemon WebSocket server."""
    daemon_server = DaemonServer(
        agent_name=agent_name,
        nats_url=nats_url,
        bus_factory=bus_factory,
        scheduler_factory=scheduler_factory,
        token_path=token_path,
        allow_unauthenticated_local=allow_unauthenticated_local,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.daemon_server = daemon_server
        await daemon_server.startup()
        try:
            yield
        finally:
            await daemon_server.shutdown()

    app = FastAPI(lifespan=lifespan)
    build_dir = Path(web_build_dir) if web_build_dir is not None else DEFAULT_WEB_BUILD_DIR

    @app.get("/api/health")
    async def health_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        return daemon_server.health_snapshot()

    @app.get("/api/metrics")
    async def metrics_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        return daemon_server.metrics_snapshot()

    @app.get("/api/sessions")
    async def list_sessions_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        return {"sessions": daemon_server.list_sessions()}

    @app.get("/api/market/watchlist")
    async def watchlist_endpoint(request: Request, symbols: str = "") -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        requested = []
        seen: set[str] = set()
        for raw_symbol in symbols.split(","):
            symbol = raw_symbol.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            requested.append(symbol)

        quotes: list[dict[str, Any]] = []
        for symbol in requested:
            try:
                quote = await asyncio.to_thread(_fetch_watchlist_quote, symbol)
            except Exception as exc:  # noqa: BLE001
                quote = {"symbol": symbol, "error": str(exc)}
            quotes.append(quote)
        return {"quotes": quotes}

    @app.get("/api/market/ohlcv")
    async def market_ohlcv_endpoint(
        request: Request,
        symbol: str,
        interval: str = "1m",
        source: str = "kai-api",
        limit: int = 300,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            bars = await asyncio.to_thread(
                _load_chart_history,
                symbol,
                interval,
                source,
                limit,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return {"bars": bars}

    @app.get("/api/portfolio")
    async def portfolio_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            snapshot = await asyncio.to_thread(_load_portfolio_snapshot)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return snapshot

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session_endpoint(request: Request, payload: SessionCreateRequest) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            session_info = await daemon_server.create_session(payload.name)
        except FileExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return {"session": session_info}

    @app.delete("/api/sessions/{session_name}")
    async def delete_session_endpoint(request: Request, session_name: str) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return await daemon_server.delete_session(session_name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.websocket(DEFAULT_DAEMON_WS_PATH)
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()

        if not daemon_server.authorize_websocket(websocket):
            await _send_error(websocket, "unauthorized", "daemon bearer token required")
            await websocket.close(code=1008)
            return

        try:
            first_message = await _receive_client_envelope(websocket)
        except ValueError as exc:
            await _send_error(websocket, "bad_request", str(exc))
            await websocket.close(code=1003)
            return

        if not isinstance(first_message, AttachEnvelope):
            await _send_error(
                websocket,
                "bad_request",
                "first client message must be an attach envelope",
            )
            await websocket.close(code=1008)
            return

        try:
            managed = await daemon_server.get_or_create_session(
                first_message.session,
                create_if_missing=first_message.create_if_missing,
            )
        except (KeyError, TypeError, ValueError) as exc:
            await _send_error(websocket, "attach_failed", str(exc))
            await websocket.close(code=1008)
            return

        session = managed.session
        subscriptions: dict[str, Any] = {"signals": False, "chart": set(), "nats": False}
        event_queue = session.subscribe_events()
        forward_task = asyncio.create_task(
            daemon_server.forward_session_events(
                websocket,
                session,
                event_queue,
                subscriptions,
            )
        )

        await _send_server_envelope(
            websocket,
            SessionAttachedEnvelope(
                type="session_attached",
                session=session.name,
                state=daemon_server.session_snapshot(session),
            ),
        )
        await _send_server_envelope(
            websocket,
            StatusEnvelope(
                type="status",
                activity=session.activity_status,
                queue=len(session.input_queue),
            ),
        )

        try:
            while True:
                try:
                    payload = await _receive_client_envelope(websocket)
                except ValueError as exc:
                    await _send_error(websocket, "bad_request", str(exc))
                    continue

                if isinstance(payload, InputEnvelope):
                    if payload.text.strip().startswith("/schedule"):
                        try:
                            response_text = await daemon_server.handle_schedule_command(
                                managed,
                                payload.text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "schedule_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    await daemon_server.run_input(managed, payload.text)
                    continue

                if isinstance(payload, SlashEnvelope):
                    if payload.command.strip() == "/schedule":
                        command_text = payload.command.strip()
                        if payload.args.strip():
                            command_text = f"{command_text} {payload.args.strip()}"
                        try:
                            response_text = await daemon_server.handle_schedule_command(
                                managed,
                                command_text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "schedule_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    parts = [payload.command.strip()]
                    if payload.args.strip():
                        parts.append(payload.args.strip())
                    await daemon_server.run_input(managed, " ".join(parts))
                    continue

                if isinstance(payload, HeartbeatEnvelope):
                    continue

                if isinstance(payload, InterruptEnvelope):
                    await _send_error(
                        websocket,
                        "unsupported",
                        "interrupt is not implemented yet",
                    )
                    continue

                if isinstance(payload, SubscribeEnvelope):
                    if payload.channel == "signals":
                        subscriptions["signals"] = True
                    elif payload.channel == "chart":
                        subscriptions["chart"].add((payload.symbol, payload.tf))
                    elif payload.channel == "nats":
                        subscriptions["nats"] = True
                    continue

                if isinstance(payload, UnsubscribeEnvelope):
                    if payload.channel == "signals":
                        subscriptions["signals"] = False
                    elif payload.channel == "chart":
                        subscriptions["chart"].discard((payload.symbol, payload.tf))
                    elif payload.channel == "nats":
                        subscriptions["nats"] = False
                    continue

                await _send_error(
                    websocket,
                    "bad_request",
                    f"unsupported message type: {type(payload).__name__}",
                )
        except WebSocketDisconnect:
            pass
        finally:
            session.event_bus.unsubscribe(event_queue)
            forward_task.cancel()
            with suppress(asyncio.CancelledError):
                await forward_task

    @app.get("/", include_in_schema=False)
    async def web_index():
        index_path = _resolve_web_asset_path(build_dir, "")
        if index_path is None:
            return _missing_web_build_response(build_dir)
        return FileResponse(index_path)

    @app.get("/{asset_path:path}", include_in_schema=False)
    async def web_asset(asset_path: str):
        index_path = _resolve_web_asset_path(build_dir, "")
        if index_path is None:
            return _missing_web_build_response(build_dir)

        resolved = _resolve_web_asset_path(build_dir, asset_path)
        if resolved is not None:
            return FileResponse(resolved)

        if _looks_like_web_asset_request(asset_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="web asset not found",
            )
        return FileResponse(index_path)

    return app


app = create_app()

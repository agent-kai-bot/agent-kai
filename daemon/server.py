"""Phase 2 daemon WebSocket server."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field

from agent.signal_consumer import Signal, SignalConsumer
from config import DEFAULT_AGENT, NATS_URL
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
    ) -> None:
        self.agent_name = agent_name
        self.nats_url = nats_url
        self.bus_factory = bus_factory or self._default_bus_factory
        self.scheduler_factory = scheduler_factory or Scheduler
        self.bus: Any | None = None
        self.sessions: dict[str, ManagedSession] = {}
        self.event_bus = DaemonEventBus()
        self.signal_consumer = SignalConsumer()
        self.scheduler: Scheduler | None = None
        self.log = logging.getLogger(__name__)

    @staticmethod
    def _default_bus_factory(url: str, agent_name: str) -> NatsBus:
        return NatsBus(url=url, agent_name=agent_name)

    async def startup(self) -> None:
        """Connect shared resources used by daemon-backed sessions."""
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
) -> FastAPI:
    """Build the FastAPI app that exposes the daemon WebSocket server."""
    daemon_server = DaemonServer(
        agent_name=agent_name,
        nats_url=nats_url,
        bus_factory=bus_factory,
        scheduler_factory=scheduler_factory,
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

    @app.get("/api/health")
    async def health_endpoint() -> dict[str, Any]:
        return {
            "status": "ok",
            "agent_name": daemon_server.agent_name,
            "bus_connected": daemon_server.bus is not None,
            "session_count": len(daemon_server.sessions),
        }

    @app.get("/api/sessions")
    async def list_sessions_endpoint() -> dict[str, Any]:
        return {"sessions": daemon_server.list_sessions()}

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session_endpoint(payload: SessionCreateRequest) -> dict[str, Any]:
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
    async def delete_session_endpoint(session_name: str) -> dict[str, Any]:
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
        subscriptions: dict[str, Any] = {"signals": False, "chart": set()}
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
                    await daemon_server.run_input(managed, payload.text)
                    continue

                if isinstance(payload, SlashEnvelope):
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
                    continue

                if isinstance(payload, UnsubscribeEnvelope):
                    if payload.channel == "signals":
                        subscriptions["signals"] = False
                    elif payload.channel == "chart":
                        subscriptions["chart"].discard((payload.symbol, payload.tf))
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

    return app


app = create_app()

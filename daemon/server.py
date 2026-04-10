"""Phase 2 daemon WebSocket server."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from config import DEFAULT_AGENT, NATS_URL
from daemon.core import Session, SessionEvent, serialize_messages
from daemon.protocol import (
    AttachEnvelope,
    ChartBarEnvelope,
    ClientEnvelope,
    ErrorEnvelope,
    FinalEnvelope,
    HeartbeatEnvelope,
    InputEnvelope,
    InterruptEnvelope,
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


class DaemonServer:
    """FastAPI-facing daemon runtime that owns sessions and a shared bus."""

    def __init__(
        self,
        *,
        agent_name: str = DEFAULT_AGENT,
        nats_url: str = NATS_URL,
        bus_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.nats_url = nats_url
        self.bus_factory = bus_factory or self._default_bus_factory
        self.bus: Any | None = None
        self.sessions: dict[str, ManagedSession] = {}
        self.log = logging.getLogger(__name__)

    @staticmethod
    def _default_bus_factory(url: str, agent_name: str) -> NatsBus:
        return NatsBus(url=url, agent_name=agent_name)

    async def startup(self) -> None:
        """Connect shared resources used by daemon-backed sessions."""
        if self.bus_factory is None:
            self.bus = None
            return

        try:
            bus = self.bus_factory(self.nats_url, self.agent_name)
            await bus.connect()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("daemon bus connect failed: %s", exc)
            self.bus = None
            return

        self.bus = bus

    async def shutdown(self) -> None:
        """Stop all managed runtime resources."""
        for managed in self.sessions.values():
            with suppress(Exception):
                await managed.session.sub_agent_registry.stop_all()

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
        if not create_if_missing and not state_exists:
            raise KeyError(f"session '{name}' does not exist")

        session.load()
        session.attach_runtime(bus=self.bus, agent_name=self.agent_name)

        managed = ManagedSession(session=session)
        self.sessions[session.name] = managed
        return managed

    async def run_input(self, managed: ManagedSession, text: str) -> None:
        """Run one input turn through the target session."""
        async with managed.input_lock:
            managed.session.set_activity_status("thinking...")
            try:
                async for _event in managed.session.stream_agent_events(text):
                    pass
            except Exception as exc:  # noqa: BLE001
                managed.session.publish_event("agent.error", {"value": str(exc)})
            finally:
                managed.session.set_activity_status("idle")
                with suppress(Exception):
                    managed.session.save()

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
) -> FastAPI:
    """Build the FastAPI app that exposes the daemon WebSocket server."""
    daemon_server = DaemonServer(
        agent_name=agent_name,
        nats_url=nats_url,
        bus_factory=bus_factory,
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

"""Phase 2 daemon WebSocket server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from config import DEFAULT_AGENT, NATS_URL
from daemon.core import Session, SessionEvent, serialize_messages
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
            await websocket.send_json(message)

    def _event_to_message(
        self,
        *,
        session: Session,
        event: SessionEvent,
        subscriptions: dict[str, Any],
        tool_start_times: dict[str, float],
    ) -> dict[str, Any] | None:
        """Map one internal session event to a WS envelope."""
        topic = event.topic
        payload = event.payload

        if topic == "agent.token":
            text = payload.get("value")
            return {"type": "token", "text": text or ""}

        if topic == "agent.tool_start":
            tool = str(payload.get("tool") or "")
            if tool:
                tool_start_times[tool] = time.monotonic()
            return {
                "type": "tool_start",
                "tool": tool,
                "args": payload.get("input"),
            }

        if topic == "agent.tool_end":
            tool = str(payload.get("tool") or "")
            started_at = tool_start_times.pop(tool, None)
            elapsed_ms = None
            if started_at is not None:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return {
                "type": "tool_end",
                "tool": tool,
                "elapsed_ms": elapsed_ms,
                "ok": True,
            }

        if topic == "agent.final":
            return {"type": "final", "text": payload.get("value") or ""}

        if topic == "agent.status":
            return {
                "type": "status",
                "activity": payload.get("value") or "idle",
                "queue": len(session.input_queue),
            }

        if topic == "agent.error":
            return {
                "type": "error",
                "code": "agent_error",
                "message": payload.get("value") or "agent stream failed",
            }

        if topic == "status.updated":
            return {
                "type": "status",
                "activity": payload.get("status") or "idle",
                "queue": len(session.input_queue),
            }

        if topic == "input.queued" or topic == "input.dequeued":
            return {
                "type": "status",
                "activity": session.activity_status,
                "queue": payload.get("depth", len(session.input_queue)),
            }

        if topic == "signal.received":
            if not subscriptions.get("signals"):
                return None
            signal = payload.get("signal")
            return {"type": "signal", "signal": signal or payload}

        if topic == "chart.bar":
            chart_subs = subscriptions.get("chart", set())
            symbol = payload.get("symbol")
            timeframe = payload.get("tf")
            if chart_subs and (symbol, timeframe) not in chart_subs:
                return None
            if not chart_subs:
                return None
            return {
                "type": "chart_bar",
                "symbol": symbol,
                "tf": timeframe,
                "bar": payload.get("bar"),
            }

        return None

    @staticmethod
    def session_snapshot(session: Session) -> dict[str, Any]:
        """Serialize the attach-time state snapshot for one session."""
        return {
            "chart_symbol": session.ui_state.chart_symbol,
            "chart_timeframe": session.ui_state.chart_timeframe,
            "chart_source": session.ui_state.chart_source,
            "chart_layout_mode": session.ui_state.chart_layout_mode,
            "chart_color_scheme": session.ui_state.chart_color_scheme,
            "watchlist_symbols": list(session.ui_state.watchlist_symbols),
            "autotrade_enabled": bool(session.ui_state.autotrade_enabled),
            "activity_status": session.ui_state.activity_status,
            "chat_history": serialize_messages(session.chat_history),
        }


async def _receive_json(websocket: WebSocket) -> dict[str, Any]:
    raw = await websocket.receive_text()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("message payload must be a JSON object")
    return payload


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json(
        {
            "type": "error",
            "code": code,
            "message": message,
        }
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
            first_message = await _receive_json(websocket)
        except ValueError as exc:
            await _send_error(websocket, "bad_request", str(exc))
            await websocket.close(code=1003)
            return

        if first_message.get("type") != "attach":
            await _send_error(
                websocket,
                "bad_request",
                "first client message must be an attach envelope",
            )
            await websocket.close(code=1008)
            return

        session_name = str(first_message.get("session") or "").strip()
        create_if_missing = bool(first_message.get("create_if_missing"))
        try:
            managed = await daemon_server.get_or_create_session(
                session_name,
                create_if_missing=create_if_missing,
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

        await websocket.send_json(
            {
                "type": "session_attached",
                "session": session.name,
                "state": daemon_server.session_snapshot(session),
            }
        )
        await websocket.send_json(
            {
                "type": "status",
                "activity": session.activity_status,
                "queue": len(session.input_queue),
            }
        )

        try:
            while True:
                try:
                    payload = await _receive_json(websocket)
                except ValueError as exc:
                    await _send_error(websocket, "bad_request", str(exc))
                    continue

                message_type = payload.get("type")
                if message_type == "input":
                    text = payload.get("text")
                    if not isinstance(text, str) or not text.strip():
                        await _send_error(
                            websocket,
                            "bad_request",
                            "input envelope requires non-empty text",
                        )
                        continue
                    await daemon_server.run_input(managed, text)
                    continue

                if message_type == "slash":
                    command = payload.get("command")
                    args = payload.get("args")
                    if not isinstance(command, str) or not command.strip():
                        await _send_error(
                            websocket,
                            "bad_request",
                            "slash envelope requires a command string",
                        )
                        continue
                    parts = [command.strip()]
                    if isinstance(args, str) and args.strip():
                        parts.append(args.strip())
                    await daemon_server.run_input(managed, " ".join(parts))
                    continue

                if message_type == "heartbeat":
                    continue

                if message_type == "interrupt":
                    await _send_error(
                        websocket,
                        "unsupported",
                        "interrupt is not implemented yet",
                    )
                    continue

                if message_type == "subscribe":
                    channel = payload.get("channel")
                    if channel == "signals":
                        subscriptions["signals"] = True
                    elif channel == "chart":
                        symbol = payload.get("symbol")
                        timeframe = payload.get("tf")
                        if isinstance(symbol, str) and isinstance(timeframe, str):
                            subscriptions["chart"].add((symbol, timeframe))
                    continue

                if message_type == "unsubscribe":
                    channel = payload.get("channel")
                    if channel == "signals":
                        subscriptions["signals"] = False
                    elif channel == "chart":
                        symbol = payload.get("symbol")
                        timeframe = payload.get("tf")
                        subscriptions["chart"].discard((symbol, timeframe))
                    continue

                await _send_error(
                    websocket,
                    "bad_request",
                    f"unsupported message type: {message_type!r}",
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


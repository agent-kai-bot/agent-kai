"""Remote session adapter for the daemon WebSocket protocol."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse, urlunparse

from langchain_core.messages import AIMessage, HumanMessage

from daemon.core import DEFAULT_SESSION_NAME, SessionUIState, deserialize_messages
from daemon.protocol import (
    AttachEnvelope,
    ChartBarEnvelope,
    ErrorEnvelope,
    FinalEnvelope,
    SessionAttachedEnvelope,
    SignalEnvelope,
    StatusEnvelope,
    TokenEnvelope,
    ToolEndEnvelope,
    ToolStartEnvelope,
    decode_server_envelope,
    encode_envelope,
)
from daemon.server import DEFAULT_DAEMON_WS_PATH, DEFAULT_DAEMON_WS_URL

try:
    from websockets.asyncio.client import connect as websocket_connect
except ImportError:  # pragma: no cover - compatibility with older websockets
    from websockets import connect as websocket_connect


@dataclass(frozen=True)
class RemoteSessionPaths:
    """Placeholder path container so the terminal can type-check access."""

    state_path: Any | None = None


class RemoteSession:
    """WebSocket-backed session adapter that mimics the local Session surface."""

    is_remote = True

    def __init__(
        self,
        remote_url: str = DEFAULT_DAEMON_WS_URL,
        *,
        session_name: str = DEFAULT_SESSION_NAME,
        create_if_missing: bool = True,
        connection_factory=None,
    ) -> None:
        self.remote_url = self._normalize_remote_url(remote_url)
        self.name = session_name
        self.create_if_missing = create_if_missing
        self.ui_state = SessionUIState()
        self.chat_history: list[Any] = []
        self.input_queue: list[str] = []
        self.agent_runner = SimpleNamespace(chat_history=self.chat_history)
        self.signal_consumer = None
        self.sub_agent_manager = None
        self.paths = RemoteSessionPaths()
        self._connection_factory = connection_factory or websocket_connect
        self._websocket = None
        self._connected = False
        self._stream_lock = asyncio.Lock()

    @staticmethod
    def _normalize_remote_url(remote_url: str) -> str:
        parsed = urlparse(remote_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("remote websocket URL must include scheme and host")
        path = parsed.path
        if not path or path == "/":
            path = DEFAULT_DAEMON_WS_PATH
        return urlunparse(parsed._replace(path=path))

    async def connect(self) -> None:
        """Open the WS connection and attach to the target daemon session."""
        if self._connected:
            return

        self._websocket = await self._connection_factory(self.remote_url)
        await self._send_envelope(
            AttachEnvelope(
                type="attach",
                session=self.name,
                create_if_missing=self.create_if_missing,
            )
        )

        attached = await self._recv_envelope()
        if isinstance(attached, ErrorEnvelope):
            raise RuntimeError(attached.message)
        if not isinstance(attached, SessionAttachedEnvelope):
            raise RuntimeError("daemon did not acknowledge session attach")

        self._apply_snapshot(attached)

        status = await self._recv_envelope()
        if isinstance(status, ErrorEnvelope):
            raise RuntimeError(status.message)
        if isinstance(status, StatusEnvelope):
            self.set_activity_status(status.activity)

        self._connected = True

    async def close(self) -> None:
        """Close the underlying websocket if it is open."""
        if self._websocket is not None:
            await self._websocket.close()
        self._websocket = None
        self._connected = False

    def load(self) -> None:
        """Remote sessions are loaded during the attach handshake."""

    def save(self) -> None:
        """Remote state persistence is owned by the daemon."""

    def set_activity_status(self, status: str) -> None:
        self.ui_state.activity_status = status or "idle"

    def queue_input(self, text: str) -> None:
        self.input_queue.append(text)

    def pop_input(self) -> str | None:
        if not self.input_queue:
            return None
        return self.input_queue.pop(0)

    async def stream_agent_events(self, user_input: str):
        """Send one input turn to the daemon and translate the reply stream."""
        await self.connect()
        async with self._stream_lock:
            self.chat_history.append(HumanMessage(content=user_input))
            self.agent_runner.chat_history = self.chat_history
            await self._send_envelope(
                {"type": "input", "text": user_input}
            )

            saw_terminal_event = False
            while True:
                envelope = await self._recv_envelope()

                if isinstance(envelope, StatusEnvelope):
                    self.set_activity_status(envelope.activity)
                    yield {"type": "status", "data": envelope.activity}
                    if saw_terminal_event and envelope.activity == "idle":
                        break
                    continue

                if isinstance(envelope, TokenEnvelope):
                    yield {"type": "token", "data": envelope.text}
                    continue

                if isinstance(envelope, ToolStartEnvelope):
                    yield {
                        "type": "tool_start",
                        "data": {"tool": envelope.tool, "input": envelope.args},
                    }
                    continue

                if isinstance(envelope, ToolEndEnvelope):
                    yield {
                        "type": "tool_end",
                        "data": {"tool": envelope.tool, "output": ""},
                    }
                    continue

                if isinstance(envelope, FinalEnvelope):
                    saw_terminal_event = True
                    self.chat_history.append(AIMessage(content=envelope.text))
                    self.agent_runner.chat_history = self.chat_history
                    yield {"type": "final", "data": envelope.text}
                    continue

                if isinstance(envelope, ErrorEnvelope):
                    saw_terminal_event = True
                    yield {"type": "error", "data": envelope.message}
                    continue

                if isinstance(envelope, SessionAttachedEnvelope):
                    self._apply_snapshot(envelope)
                    continue

                if isinstance(envelope, SignalEnvelope | ChartBarEnvelope):
                    # Phase 2 keeps the terminal's market-data rendering local.
                    continue

    def _apply_snapshot(self, envelope: SessionAttachedEnvelope) -> None:
        state = envelope.state
        self.ui_state.chart_symbol = state.chart_symbol
        self.ui_state.chart_timeframe = state.chart_timeframe
        self.ui_state.chart_source = state.chart_source
        self.ui_state.chart_layout_mode = state.chart_layout_mode
        self.ui_state.chart_color_scheme = state.chart_color_scheme
        self.ui_state.watchlist_symbols = list(state.watchlist_symbols)
        self.ui_state.autotrade_enabled = state.autotrade_enabled
        self.ui_state.activity_status = state.activity_status
        self.chat_history[:] = deserialize_messages(
            [entry.model_dump(mode="json") for entry in state.chat_history]
        )
        self.agent_runner.chat_history = self.chat_history

    async def _send_envelope(self, envelope) -> None:
        if self._websocket is None:
            raise RuntimeError("remote session is not connected")
        payload = envelope
        if not isinstance(envelope, dict):
            payload = encode_envelope(envelope)
        await self._websocket.send(json.dumps(payload))

    async def _recv_envelope(self):
        if self._websocket is None:
            raise RuntimeError("remote session is not connected")
        raw = await self._websocket.recv()
        return decode_server_envelope(raw)

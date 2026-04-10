"""Remote session adapter for the daemon WebSocket protocol."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlunparse

import aiohttp

from langchain_core.messages import AIMessage, HumanMessage

from daemon.core import DEFAULT_SESSION_NAME, SessionUIState, deserialize_messages
from daemon.protocol import (
    AttachEnvelope,
    ChartBarEnvelope,
    ErrorEnvelope,
    FinalEnvelope,
    ScheduledJobCancelledEnvelope,
    ScheduledJobCompletedEnvelope,
    ScheduledJobCreatedEnvelope,
    ScheduledJobFailedEnvelope,
    ScheduledJobPausedEnvelope,
    ScheduledJobResumedEnvelope,
    ScheduledJobTriggeredEnvelope,
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
        self.api_base_url = self._derive_api_base_url(self.remote_url)
        self.auth_token = self._extract_auth_token(self.remote_url)
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
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
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

    @staticmethod
    def _derive_api_base_url(remote_url: str) -> str:
        """Translate a daemon websocket URL into the matching HTTP base URL."""
        parsed = urlparse(remote_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        path = parsed.path or ""
        if path.endswith(DEFAULT_DAEMON_WS_PATH):
            path = path[: -len(DEFAULT_DAEMON_WS_PATH)]
        return urlunparse(
            parsed._replace(
                scheme=scheme,
                path=path.rstrip("/"),
                params="",
                query="",
                fragment="",
            )
        )

    @staticmethod
    def _extract_auth_token(remote_url: str) -> str | None:
        """Reuse a websocket query token for daemon REST calls."""
        parsed = urlparse(remote_url)
        token = parse_qs(parsed.query).get("token", [""])[0].strip()
        return token or None

    def _build_auth_headers(self) -> dict[str, str] | None:
        """Return bearer auth headers when the remote URL carries a token."""
        if not self.auth_token:
            return None
        return {"Authorization": f"Bearer {self.auth_token}"}

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
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        """Close the underlying websocket if it is open."""
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader_task = None
        if self._websocket is not None:
            await self._websocket.close()
        self._websocket = None

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

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Fetch the daemon's known session list over REST."""
        payload = await self._request_json("GET", "/api/sessions")
        sessions = payload.get("sessions")
        return sessions if isinstance(sessions, list) else []

    async def delete_session(self, name: str) -> dict[str, Any]:
        """Delete a named session through the daemon REST API."""
        return await self._request_json(
            "DELETE",
            f"/api/sessions/{quote(name, safe='')}",
        )

    async def send_input(self, user_input: str) -> None:
        """Send one user input into the attached daemon session."""
        await self.connect()
        async with self._stream_lock:
            self.chat_history.append(HumanMessage(content=user_input))
            self.agent_runner.chat_history = self.chat_history
            await self._send_envelope({"type": "input", "text": user_input})

    async def next_event(self) -> dict[str, Any]:
        """Read the next translated daemon event from the shared inbox."""
        await self.connect()
        return await self._event_queue.get()

    async def stream_agent_events(self, user_input: str):
        """Send one input turn to the daemon and translate the reply stream."""
        await self.send_input(user_input)

        saw_terminal_event = False
        while True:
            event = await self.next_event()
            etype = event["type"]
            if etype == "status":
                yield event
                if saw_terminal_event and event["data"] == "idle":
                    break
                continue
            if etype in {"final", "error"}:
                saw_terminal_event = True
            yield event

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

    async def _reader_loop(self) -> None:
        """Continuously translate daemon envelopes into adapter events."""
        try:
            while True:
                envelope = await self._recv_envelope()
                event = self._translate_envelope(envelope)
                if event is not None:
                    await self._event_queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._connected:
                await self._event_queue.put({"type": "error", "data": str(exc)})

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one JSON REST request against the daemon HTTP API."""
        url = f"{self.api_base_url}{path}"
        async with aiohttp.ClientSession() as client:
            async with client.request(
                method,
                url,
                json=payload,
                headers=self._build_auth_headers(),
            ) as response:
                data = await response.json()
                if response.status >= 400:
                    detail = data.get("detail") if isinstance(data, dict) else None
                    raise RuntimeError(detail or f"{method} {path} failed")
                return data if isinstance(data, dict) else {}

    def _translate_envelope(self, envelope) -> dict[str, Any] | None:
        """Translate one validated server envelope into terminal-facing events."""
        if isinstance(envelope, StatusEnvelope):
            self.set_activity_status(envelope.activity)
            return {"type": "status", "data": envelope.activity}

        if isinstance(envelope, TokenEnvelope):
            return {"type": "token", "data": envelope.text}

        if isinstance(envelope, ToolStartEnvelope):
            return {
                "type": "tool_start",
                "data": {"tool": envelope.tool, "input": envelope.args},
            }

        if isinstance(envelope, ToolEndEnvelope):
            return {
                "type": "tool_end",
                "data": {"tool": envelope.tool, "output": ""},
            }

        if isinstance(envelope, FinalEnvelope):
            self.chat_history.append(AIMessage(content=envelope.text))
            self.agent_runner.chat_history = self.chat_history
            return {"type": "final", "data": envelope.text}

        if isinstance(
            envelope,
            ScheduledJobCreatedEnvelope
            | ScheduledJobTriggeredEnvelope
            | ScheduledJobCompletedEnvelope
            | ScheduledJobFailedEnvelope
            | ScheduledJobCancelledEnvelope
            | ScheduledJobPausedEnvelope
            | ScheduledJobResumedEnvelope,
        ):
            return {"type": envelope.type, "data": encode_envelope(envelope)}

        if isinstance(envelope, ErrorEnvelope):
            return {"type": "error", "data": envelope.message}

        if isinstance(envelope, SessionAttachedEnvelope):
            self._apply_snapshot(envelope)
            return None

        if isinstance(envelope, SignalEnvelope | ChartBarEnvelope):
            # The terminal still owns local chart/watchlist rendering.
            return None

        return None

    async def _recv_envelope(self):
        if self._websocket is None:
            raise RuntimeError("remote session is not connected")
        raw = await self._websocket.recv()
        return decode_server_envelope(raw)

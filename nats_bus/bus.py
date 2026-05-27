"""NATS message bus for inter-agent communication."""

import json
from datetime import datetime, timezone

import nats
from nats.aio.client import Client as NatsClient

from agent.error_surface import surface_exception
from agent_logger import get_logger, log_nats
from config import DEFAULT_AGENT, NATS_URL


LOGGER = get_logger("nats")


class NatsBus:
    """Async NATS client for agent pub/sub communication."""

    def __init__(self, url=NATS_URL, agent_name=DEFAULT_AGENT):
        self.url = url
        self.agent_name = agent_name
        self._nc: NatsClient | None = None
        self._subscriptions = []
        self._message_callbacks = []

    @property
    def is_connected(self):
        return self._nc is not None and self._nc.is_connected

    async def connect(self):
        """Connect to the NATS server."""
        async def _error_cb(exc):
            surfaced = surface_exception(exc, include_traceback=False)
            LOGGER.warning(
                "NATS_TYPED_ERROR agent=%s error_class=%s error_message=%s actionable_hint=%s",
                self.agent_name,
                surfaced.error_class,
                surfaced.error_message,
                surfaced.actionable_hint,
            )

        async def _disconnected_cb():
            LOGGER.warning("NATS_DISCONNECTED agent=%s url=%s", self.agent_name, self.url)

        async def _reconnected_cb():
            LOGGER.warning("NATS_RECONNECTED agent=%s url=%s", self.agent_name, self.url)

        async def _closed_cb():
            LOGGER.warning("NATS_CLOSED agent=%s url=%s", self.agent_name, self.url)

        self._nc = await nats.connect(
            self.url,
            reconnect_time_wait=2,
            max_reconnect_attempts=-1,
            error_cb=_error_cb,
            disconnected_cb=_disconnected_cb,
            reconnected_cb=_reconnected_cb,
            closed_cb=_closed_cb,
        )
        # Announce presence
        await self.publish("system.registry", {
            "agent": self.agent_name,
            "status": "online",
        })

    async def disconnect(self):
        """Gracefully drain and close the NATS connection."""
        if self._nc and self._nc.is_connected:
            await self.publish("system.registry", {
                "agent": self.agent_name,
                "status": "offline",
            })
            await self._nc.drain()

    async def publish(self, subject: str, payload: dict):
        """Publish a JSON message to a subject."""
        if not self.is_connected:
            return
        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        payload.setdefault("from", self.agent_name)
        data = json.dumps(payload).encode()
        await self._nc.publish(subject, data)
        self._notify("pub", subject, payload)

    async def subscribe(self, subject: str, handler):
        """Subscribe to a subject with an async handler.

        The handler receives (subject: str, payload: dict).
        """
        async def _wrapper(msg):
            try:
                payload = json.loads(msg.data.decode())
            except json.JSONDecodeError:
                payload = {"raw": msg.data.decode()}
            self._notify("sub", msg.subject, payload)
            await handler(msg.subject, payload)

        sub = await self._nc.subscribe(subject, cb=_wrapper)
        self._subscriptions.append(sub)
        return sub

    async def subscribe_with_reply(self, subject: str, handler):
        """Subscribe to a subject, supporting NATS request/reply.

        The handler receives (subject: str, payload: dict) and must return
        a dict that will be sent back as the reply.
        """
        async def _wrapper(msg):
            try:
                payload = json.loads(msg.data.decode())
            except json.JSONDecodeError:
                payload = {"raw": msg.data.decode()}
            self._notify("sub", msg.subject, payload)
            result = await handler(msg.subject, payload)
            if msg.reply and result is not None:
                reply_data = json.dumps(result).encode()
                await self._nc.publish(msg.reply, reply_data)
                self._notify("rep", msg.subject, result)

        sub = await self._nc.subscribe(subject, cb=_wrapper)
        self._subscriptions.append(sub)
        return sub

    async def request(self, subject: str, payload: dict, timeout=30.0):
        """Send a request and wait for a reply (NATS request/reply pattern)."""
        if not self.is_connected:
            try:
                await self.connect()
            except Exception as exc:
                surfaced = surface_exception(exc, include_traceback=False)
                LOGGER.warning(
                    "NATS_TYPED_ERROR agent=%s subject=%s error_class=%s error_message=%s actionable_hint=%s",
                    self.agent_name,
                    subject,
                    surfaced.error_class,
                    surfaced.error_message,
                    surfaced.actionable_hint,
                )
                raise RuntimeError(f"NATS bus is not connected: {self.url}") from exc
        payload.setdefault("from", self.agent_name)
        data = json.dumps(payload).encode()
        self._notify("req", subject, payload)
        response = await self._nc.request(subject, data, timeout=timeout)
        reply = json.loads(response.data.decode())
        self._notify("rep", subject, reply)
        return reply

    def on_message(self, callback):
        """Register a callback for all NATS activity (for TUI logging).

        Callback signature: callback(direction: str, subject: str, payload: dict)
        direction is one of: "pub", "sub", "req", "rep"
        """
        self._message_callbacks.append(callback)

    def _notify(self, direction, subject, payload):
        log_nats(direction, subject, payload)
        for cb in self._message_callbacks:
            try:
                cb(direction, subject, payload)
            except Exception:
                pass

"""Bridge agent-k.ai websocket updates into local NATS and WebSocket subjects."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote_plus

import nats
import websockets

from data_api.agent_kai_client import channel_symbol_and_interval, event_to_bar, rows_to_bars
from data_api.config import (
    AGENT_KAI_API_KEY,
    AGENT_KAI_MAX_BACKOFF_SECONDS,
    AGENT_KAI_WS_BACKOFF_SECONDS,
    AGENT_KAI_WS_URL,
    BRIDGE_INTERVALS,
    NATS_URL,
    TRACKED_SYMBOLS,
)
from data_api.websocket import ws_manager

log = logging.getLogger("kai.bridge")


class NatsBridge:
    """Publish market updates to local NATS and WebSocket consumers."""

    def __init__(self) -> None:
        self._nc = None
        self._running = False

    async def connect(self) -> None:
        """Connect to local NATS."""
        self._nc = await nats.connect(NATS_URL)
        log.info("NATS bridge connected to %s", NATS_URL)

    async def disconnect(self) -> None:
        """Drain the local NATS connection."""
        if self._nc:
            await self._nc.drain()

    async def start(self) -> None:
        """Start the websocket bridge loop."""
        self._running = True
        log.info(
            "Bridge started provider=agent-kai tracking=%s intervals=%s",
            TRACKED_SYMBOLS,
            BRIDGE_INTERVALS,
        )
        await self._stream_agent_kai()

    def stop(self) -> None:
        """Stop the bridge loop."""
        self._running = False

    async def _stream_agent_kai(self) -> None:
        """Stream remote websocket candles and republish them locally."""
        if not AGENT_KAI_API_KEY:
            raise RuntimeError("AGENT_KAI_API_KEY is required for market streaming")

        backoff = AGENT_KAI_WS_BACKOFF_SECONDS
        while self._running:
            try:
                await self._stream_agent_kai_once()
                if not self._running:
                    break
                backoff = AGENT_KAI_WS_BACKOFF_SECONDS
            except Exception as exc:
                log.error("Bridge websocket error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, AGENT_KAI_MAX_BACKOFF_SECONDS)

    async def _stream_agent_kai_once(self) -> None:
        """Run a single websocket session against agent-k.ai."""
        separator = "&" if "?" in AGENT_KAI_WS_URL else "?"
        url = f"{AGENT_KAI_WS_URL}{separator}api_key={quote_plus(AGENT_KAI_API_KEY)}"

        async with websockets.connect(url, ping_interval=None) as websocket:
            welcome_message = json.loads(await websocket.recv())
            await self._handle_agent_kai_message(websocket, welcome_message)
            await self._subscribe_agent_kai_channels(websocket)

            async for raw_message in websocket:
                message = json.loads(raw_message)
                await self._handle_agent_kai_message(websocket, message)

    async def _subscribe_agent_kai_channels(self, websocket) -> None:
        """Subscribe to the configured upstream channels."""
        channels = [
            f"market.{symbol}.{interval}"
            for symbol in TRACKED_SYMBOLS
            for interval in BRIDGE_INTERVALS
        ]
        await websocket.send(json.dumps({"op": "subscribe", "channels": channels}))
        log.info("Subscribed to agent-kai channels: %s", channels)

    async def _handle_agent_kai_message(self, websocket, message: dict) -> None:
        """Process a websocket message from agent-k.ai."""
        operation = message.get("op")

        if operation == "ping":
            await websocket.send(json.dumps({"op": "pong", "ts": message.get("ts")}))
            return

        if operation == "error":
            log.error("agent-kai websocket error: %s", message)
            return

        if operation == "snapshot":
            await self._handle_agent_kai_snapshot(message)
            return

        if operation == "event":
            await self._handle_agent_kai_event(message)
            return

        if operation not in {"welcome", "subscribed", "unsubscribed"}:
            log.debug("Ignoring websocket message: %s", message)

    async def _handle_agent_kai_snapshot(self, message: dict) -> None:
        """Publish the latest candle from an upstream snapshot."""
        channel = message.get("channel", "")
        rows = message.get("data", [])
        if not channel or not rows:
            return

        symbol, interval = channel_symbol_and_interval(channel)
        latest_bar = rows_to_bars(symbol, interval, [rows[-1]])[-1]
        await self._publish_bar(self._bar_to_payload(latest_bar))

    async def _handle_agent_kai_event(self, message: dict) -> None:
        """Publish a live upstream candle update."""
        channel = message.get("channel", "")
        data = message.get("data", {})
        if not channel or not data:
            return

        await self._publish_bar(self._bar_to_payload(event_to_bar(data, channel)))

    def _bar_to_payload(self, bar: dict) -> dict:
        """Convert a local bar dict into the transport payload shape."""
        ts = bar["ts"]
        ts_value = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        payload = {
            "symbol": bar["symbol"],
            "interval": bar["interval"],
            "ts": ts_value,
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar["volume"]),
        }
        if "is_closed" in bar:
            payload["is_closed"] = bool(bar["is_closed"])
        if "source" in bar and bar["source"] is not None:
            payload["source"] = bar["source"]
        return payload

    async def _publish_bar(self, bar_data: dict) -> None:
        """Publish a candle update to local consumers."""
        symbol = bar_data["symbol"]
        interval = bar_data["interval"]

        if self._nc and self._nc.is_connected:
            await self._nc.publish(
                f"market.{symbol}.{interval}",
                json.dumps(bar_data).encode(),
            )
        await ws_manager.broadcast(symbol, interval, bar_data)

        if interval == "1m":
            price_data = {
                "symbol": symbol,
                "price": bar_data["close"],
                "ts": bar_data["ts"],
                "volume": bar_data.get("volume"),
            }
            await self._publish_price(symbol, price_data)

    async def _publish_price(self, symbol: str, price_data: dict) -> None:
        """Publish a price tick to local consumers."""
        if self._nc and self._nc.is_connected:
            await self._nc.publish(
                f"market.{symbol}.price",
                json.dumps(price_data).encode(),
            )
        await ws_manager.broadcast_price(symbol, price_data)

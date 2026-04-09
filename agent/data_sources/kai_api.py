"""Cloud agent-k.ai market data client (REST + WebSocket).

Mirrors the shape of ``agent/data_sources/coinbase.py`` so the chart
panel can swap between sources without caring about the protocol.
The cloud is the default chart source for the open-source agent —
it ships out of the box and drives traffic to the hosted API, which
is how the project is monetized.

Two surfaces:

1. **REST historical bootstrap** — ``fetch_candles(symbol, interval, limit)``
   hits ``GET https://agent-k.ai/v1/market/ohlcv/{symbol}`` and returns
   a list of normalized bar dicts (oldest → newest). Used to seed
   the chart on source-switch.

2. **Live WebSocket stream** — ``KaiApiCandleStream(symbol, interval)``
   is an async iterator that connects to ``wss://agent-k.ai/v1/ws``,
   subscribes to ``market.{SYMBOL}.{INTERVAL}``, swallows the initial
   snapshot frame, and yields normalized candle dicts as live ``event``
   frames arrive. Auto-handles the server heartbeat (responds to
   ``ping`` with ``pong``) and reconnects with exponential backoff
   on disconnect.

Both surfaces require ``AGENT_KAI_API_KEY`` in the environment, which
``config.py`` auto-loads from ``AGENT-KAI-API-KEY.txt`` at the project
root if no env var is set.

The cloud channel name format is documented in
``vpn-stack-adjacent-repo:kai-new-v2/backend/app/routers/v1_ws.py``
as ``market.{SYMBOL}.{INTERVAL}`` (e.g. ``market.BTC.1m``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp
import requests

logger = logging.getLogger(__name__)

# ── Endpoints ───────────────────────────────────────────────

REST_BASE = "https://agent-k.ai/v1"
WS_URL = "wss://agent-k.ai/v1/ws"

# Per-channel snapshot cap (the cloud sends up to 50 historical
# candles on subscribe — same as the SNAPSHOT_CANDLE_LIMIT constant
# in the gateway's v1_ws.py). REST can pull more (up to 1000) so
# we use REST for the historical bootstrap and WS only for live.
WS_SNAPSHOT_CAP = 50
REST_DEFAULT_LIMIT = 200

# Auth — the cloud accepts the API key on the WS query string
# (custom Authorization headers don't survive the upgrade in all
# clients) and as a Bearer header on REST. Both come from the
# same env var, which config.py auto-loads at import time.
ENV_VAR = "AGENT_KAI_API_KEY"


def _get_api_key() -> Optional[str]:
    """Read the API key from the environment. Returns None if missing."""
    return os.environ.get(ENV_VAR) or None


# ── REST historical fetch ───────────────────────────────────

def fetch_candles(
    symbol: str,
    interval: str = "1h",
    limit: int = REST_DEFAULT_LIMIT,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    """Fetch historical candles via the cloud REST endpoint.

    Returns a list of normalized bars (oldest → newest), each shaped
    like::

        {"ts": iso_string, "open": float, "high": float,
         "low": float, "close": float, "volume": float}

    Raises ``RuntimeError`` if no API key is configured, ``ValueError``
    if the response has no bars, or ``requests.RequestException`` on
    network / HTTP errors.
    """
    key = _get_api_key()
    if not key:
        raise RuntimeError(
            f"{ENV_VAR} not set. Drop AGENT-KAI-API-KEY.txt at the project root "
            "or export the env var."
        )

    resp = requests.get(
        f"{REST_BASE}/market/ohlcv/{symbol.upper()}",
        params={"interval": interval, "limit": limit},
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    raw = payload.get("data") or payload.get("bars") or []
    if not raw:
        raise ValueError(f"No bars for {symbol} {interval} from cloud REST")

    bars = [b for b in (_normalize_snapshot_bar(arr) for arr in raw) if b]
    bars.sort(key=lambda b: b["ts"])
    return bars


# ── Bar normalization ───────────────────────────────────────
#
# Cloud uses TWO bar formats:
#
# - Snapshot frames + REST: positional arrays
#   ``[ts_ms, open, high, low, close, volume]``
# - Event frames: named dicts with extras
#   ``{"event": "kline_update", "source": "bingx", "symbol": "BTC",
#     "interval": "1m", "ts": ms, "open": .., ..., "is_closed": bool}``
#
# Both are normalized to the same chart-panel-friendly dict so the
# rest of the codebase doesn't have to know the difference. The
# ``is_closed`` flag from event frames is preserved as a hint to
# the chart so it knows whether to overwrite the current bar (live
# tick) or append a new one (closed).


def _ts_ms_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_snapshot_bar(arr: list) -> Optional[Dict[str, Any]]:
    """Convert a positional snapshot bar to the chart panel format."""
    if not isinstance(arr, list) or len(arr) < 6:
        return None
    try:
        return {
            "ts": _ts_ms_to_iso(int(arr[0])),
            "open": float(arr[1]),
            "high": float(arr[2]),
            "low": float(arr[3]),
            "close": float(arr[4]),
            "volume": float(arr[5]),
        }
    except (TypeError, ValueError):
        return None


def _normalize_event_bar(data: dict) -> Optional[Dict[str, Any]]:
    """Convert a kline_update event dict to the chart panel format.

    Preserves ``is_closed`` so consumers can decide whether the bar
    is the still-forming current candle (live update) or a finalized
    candle (append a new one).
    """
    if not isinstance(data, dict):
        return None
    try:
        return {
            "ts": _ts_ms_to_iso(int(data["ts"])),
            "open": float(data["open"]),
            "high": float(data["high"]),
            "low": float(data["low"]),
            "close": float(data["close"]),
            "volume": float(data["volume"]),
            "is_closed": bool(data.get("is_closed", False)),
            "symbol": data.get("symbol"),
            "interval": data.get("interval"),
        }
    except (KeyError, TypeError, ValueError):
        return None


# ── Live WebSocket stream ───────────────────────────────────

@dataclass
class KaiApiStreamSettings:
    symbol: str
    interval: str
    ws_url: str = WS_URL
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    receive_timeout_s: int = 60


class KaiApiCandleStream:
    """Live cloud agent-k.ai candle stream as an async iterator.

    Usage::

        stream = KaiApiCandleStream("BTC", "1m")
        async for bar in stream:
            # bar is a dict with ts/open/high/low/close/volume + is_closed
            ...

    The first ``snapshot`` frame from the server is consumed and
    discarded — callers should bootstrap historical bars via
    ``fetch_candles()`` first (REST gives a richer 200-bar window
    vs the WS snapshot's 50-bar cap). Subsequent ``event`` frames
    are normalized and yielded as they arrive.

    Auto-reconnects on disconnect with exponential backoff between
    ``reconnect_min_s`` and ``reconnect_max_s``. Call ``stop()`` from
    another coroutine to gracefully end iteration.
    """

    def __init__(
        self,
        symbol: str,
        interval: str = "1m",
        ws_url: str = WS_URL,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self.settings = KaiApiStreamSettings(
            symbol=symbol.upper(),
            interval=interval,
            ws_url=ws_url,
            reconnect_min_s=reconnect_min_s,
            reconnect_max_s=reconnect_max_s,
        )
        self._stop_event = asyncio.Event()
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def channel(self) -> str:
        return f"market.{self.settings.symbol}.{self.settings.interval}"

    def stop(self) -> None:
        """Signal the stream to stop on the next iteration."""
        self._stop_event.set()

    async def __aiter__(self) -> AsyncIterator[Dict[str, Any]]:
        key = _get_api_key()
        if not key:
            raise RuntimeError(
                f"{ENV_VAR} not set. Drop AGENT-KAI-API-KEY.txt at the project root "
                "or export the env var."
            )

        backoff = self.settings.reconnect_min_s
        self._session = aiohttp.ClientSession()
        try:
            while not self._stop_event.is_set():
                try:
                    async for bar in self._connect_and_stream(key):
                        yield bar
                        if self._stop_event.is_set():
                            return
                    backoff = self.settings.reconnect_min_s
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "kai-api WS error: %s — reconnecting in %.1fs", exc, backoff
                    )
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=backoff
                        )
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, self.settings.reconnect_max_s)
        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    async def _connect_and_stream(self, api_key: str) -> AsyncIterator[Dict[str, Any]]:
        """One connect-subscribe-receive cycle. Handles ping/pong inline."""
        assert self._session is not None
        url = f"{self.settings.ws_url}?api_key={api_key}"

        async with self._session.ws_connect(
            url,
            heartbeat=None,  # we handle ping/pong manually via the protocol op
            receive_timeout=self.settings.receive_timeout_s,
        ) as ws:
            logger.info("kai-api WS connected, subscribing to %s", self.channel)

            await ws.send_json({"op": "subscribe", "channels": [self.channel]})

            async for msg in ws:
                if self._stop_event.is_set():
                    await ws.close()
                    return
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        exc = ws.exception()
                        if exc:
                            raise exc
                    continue

                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                op = frame.get("op")

                if op == "event":
                    bar = _normalize_event_bar(frame.get("data") or {})
                    if bar is not None:
                        yield bar
                elif op == "ping":
                    # Respond to server heartbeat to keep the connection alive
                    try:
                        await ws.send_json({"op": "pong", "ts": frame.get("ts") or int(time.time() * 1000)})
                    except Exception:
                        pass
                elif op == "snapshot":
                    # The REST historical bootstrap covers more bars
                    # (200 vs 50), so we drop the WS snapshot to avoid
                    # double-painting the chart with stale data.
                    continue
                elif op == "subscribed":
                    logger.debug("kai-api WS subscribed: %s", frame.get("channels"))
                elif op == "error":
                    raise RuntimeError(
                        f"kai-api WS error frame: {frame.get('code')}: {frame.get('message')}"
                    )
                else:
                    # welcome / unsubscribed / unknown — log and ignore
                    logger.debug("kai-api WS frame %s ignored", op)

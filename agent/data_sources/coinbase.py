"""Coinbase Advanced Trade market data client.

Adapted from ``vpn-stack/workspace/coinbase-candles/coinbase_candles_service.py``
— the original service streams candles to NATS. This version exposes
the same WebSocket logic as a reusable async generator AND adds a
synchronous REST client for historical candles so the agent can
query Coinbase directly from tools and the backtest module.

No authentication is required for public market data. Rate limits
are generous for read-only endpoints (10 req/s per IP for REST,
8 concurrent connections for WebSocket).

Usage — REST (historical)::

    bars = fetch_candles("BTC-USD", interval="1h", limit=100)
    # → [{"ts": "...", "open": 70000.0, ...}, ...]

Usage — WebSocket (live)::

    async for candle in CoinbaseCandleStream(["BTC-USD"]):
        print(candle)  # dict with same shape as REST output
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp
import requests

logger = logging.getLogger(__name__)

# ── Endpoints ───────────────────────────────────────────────

REST_BASE = "https://api.coinbase.com/api/v3/brokerage/market"
WS_URL = "wss://advanced-trade-ws.coinbase.com"


# ── Interval mapping ────────────────────────────────────────
#
# Coinbase's API uses its own granularity strings. We map our
# project-standard interval labels onto them so callers can write
# ``interval="1h"`` and the client translates.

INTERVAL_TO_GRANULARITY: Dict[str, str] = {
    "1m": "ONE_MINUTE",
    "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "2h": "TWO_HOUR",
    "6h": "SIX_HOUR",
    "1d": "ONE_DAY",
}

INTERVAL_TO_SECONDS: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "6h": 21600,
    "1d": 86400,
}


# ── Helpers ─────────────────────────────────────────────────

def normalize_product_id(symbol: str) -> str:
    """Convert a bare symbol ("BTC") to a Coinbase product ID ("BTC-USD").

    Already-qualified IDs ("BTC-USD", "ETH-USDC") pass through untouched.
    """
    s = symbol.upper().strip()
    if "-" in s:
        return s
    return f"{s}-USD"


def _parse_candle(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a Coinbase candle dict to our project-standard format.

    Coinbase REST returns candles with string values and a unix timestamp
    as a string. This converts to floats and an ISO timestamp so the
    result matches what ``query_ohlcv`` and the backtest tool expect.
    """
    start = raw.get("start", "0")
    try:
        start_epoch = int(start)
        ts = datetime.fromtimestamp(start_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        ts = str(start)

    return {
        "ts": ts,
        "open": float(raw.get("open", 0)),
        "high": float(raw.get("high", 0)),
        "low": float(raw.get("low", 0)),
        "close": float(raw.get("close", 0)),
        "volume": float(raw.get("volume", 0)),
    }


# ── REST client (sync) ──────────────────────────────────────

def fetch_candles(
    symbol: str,
    interval: str = "1h",
    limit: int = 300,
    session: Optional[requests.Session] = None,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    """Fetch historical OHLCV candles from Coinbase REST.

    Args:
        symbol: "BTC", "BTC-USD", "ETH-USDC", etc. Bare symbols are
            normalized to "{symbol}-USD" automatically.
        interval: Candle interval. One of: 1m, 5m, 15m, 30m, 1h, 2h, 6h, 1d.
        limit: Number of bars to fetch (Coinbase caps at 350 per request).
        session: Optional ``requests.Session`` to reuse.
        timeout: Request timeout in seconds.

    Returns:
        List of candle dicts sorted oldest → newest, each with
        ``{ts, open, high, low, close, volume}``.

    Raises:
        ValueError: unknown interval or no candles returned.
        requests.RequestException: network or HTTP error.
    """
    product_id = normalize_product_id(symbol)
    granularity = INTERVAL_TO_GRANULARITY.get(interval)
    if granularity is None:
        raise ValueError(
            f"Unknown interval '{interval}'. "
            f"Supported: {', '.join(INTERVAL_TO_GRANULARITY)}"
        )

    if limit > 350:
        logger.warning("Coinbase caps at 350 candles per request; clamping %d → 350", limit)
        limit = 350

    seconds_per_bar = INTERVAL_TO_SECONDS[interval]
    end = int(time.time())
    start = end - (limit * seconds_per_bar)

    sess = session or requests
    resp = sess.get(
        f"{REST_BASE}/products/{product_id}/candles",
        params={"start": start, "end": end, "granularity": granularity},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    raw_candles = data.get("candles", [])
    if not raw_candles:
        raise ValueError(f"No candles returned for {product_id} {interval}")

    # Coinbase returns newest-first; we want oldest-first for consistency
    parsed = [_parse_candle(c) for c in raw_candles]
    parsed.sort(key=lambda c: c["ts"])
    return parsed


def fetch_latest_price(symbol: str, timeout: float = 10.0) -> Dict[str, Any]:
    """Fetch the most recent trade price for a product.

    Returns ``{symbol, product_id, price, timestamp}``.
    """
    product_id = normalize_product_id(symbol)
    resp = requests.get(
        f"{REST_BASE}/products/{product_id}",
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "symbol": symbol.upper(),
        "product_id": product_id,
        "price": float(data.get("price", 0)),
        "volume_24h": float(data.get("volume_24h", 0)),
        "price_change_24h_pct": float(data.get("price_percentage_change_24h", 0)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def list_products(
    quote: str = "USD",
    limit: int = 50,
    timeout: float = 10.0,
) -> List[Dict[str, Any]]:
    """List available Coinbase spot products, filtered by quote currency."""
    resp = requests.get(
        f"{REST_BASE}/products",
        params={"product_type": "SPOT", "limit": limit},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    products = data.get("products", [])
    filtered = [
        {
            "product_id": p.get("product_id"),
            "base": p.get("base_currency_id"),
            "quote": p.get("quote_currency_id"),
            "price": float(p.get("price", 0)) if p.get("price") else None,
            "volume_24h": float(p.get("volume_24h", 0)) if p.get("volume_24h") else None,
        }
        for p in products
        if p.get("quote_currency_id", "").upper() == quote.upper()
    ]
    return filtered


# ── WebSocket streaming (async) ─────────────────────────────
#
# Copied + adapted from the vpn-stack service. Instead of publishing to
# NATS, this class is an async iterator — each ``__anext__`` yields a
# normalized candle dict. Consumers integrate it wherever they need
# live data (chart panel, custom bots, live backtests).


@dataclass
class CoinbaseStreamSettings:
    product_ids: List[str]
    ws_url: str = WS_URL
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    heartbeat_s: int = 30
    receive_timeout_s: int = 60


class CoinbaseCandleStream:
    """Live Coinbase candle WebSocket stream as an async iterator.

    Usage::

        stream = CoinbaseCandleStream(["BTC-USD", "ETH-USD"])
        async for candle in stream:
            print(candle)

    Candles are yielded in the same normalized format as ``fetch_candles``:
    ``{ts, open, high, low, close, volume, product_id}``.

    The stream auto-reconnects with exponential backoff on failure.
    Call ``stop()`` from another coroutine to gracefully end iteration.
    """

    def __init__(
        self,
        product_ids: List[str],
        ws_url: str = WS_URL,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 30.0,
    ) -> None:
        normalized = [normalize_product_id(p) for p in product_ids]
        self.settings = CoinbaseStreamSettings(
            product_ids=normalized,
            ws_url=ws_url,
            reconnect_min_s=reconnect_min_s,
            reconnect_max_s=reconnect_max_s,
        )
        self._stop_event = asyncio.Event()
        self._session: Optional[aiohttp.ClientSession] = None

    def stop(self) -> None:
        """Signal the stream to stop on the next iteration."""
        self._stop_event.set()

    async def __aiter__(self) -> AsyncIterator[Dict[str, Any]]:
        backoff = self.settings.reconnect_min_s
        self._session = aiohttp.ClientSession()
        try:
            while not self._stop_event.is_set():
                try:
                    async for candle in self._connect_and_stream():
                        yield candle
                        if self._stop_event.is_set():
                            return
                    backoff = self.settings.reconnect_min_s
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Coinbase WS error: %s — reconnecting in %.1fs", exc, backoff)
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

    async def _connect_and_stream(self) -> AsyncIterator[Dict[str, Any]]:
        assert self._session is not None
        async with self._session.ws_connect(
            self.settings.ws_url,
            heartbeat=self.settings.heartbeat_s,
            receive_timeout=self.settings.receive_timeout_s,
        ) as ws:
            logger.info("Connected to Coinbase WS, subscribing to %s", self.settings.product_ids)
            await ws.send_json({
                "type": "subscribe",
                "product_ids": self.settings.product_ids,
                "channel": "candles",
            })

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
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "error":
                    logger.error("Coinbase WS error: %s", data.get("message"))
                    continue

                if data.get("channel") != "candles":
                    continue

                for event in data.get("events", []):
                    for raw_candle in event.get("candles", []):
                        normalized = _parse_candle(raw_candle)
                        normalized["product_id"] = raw_candle.get("product_id")
                        yield normalized

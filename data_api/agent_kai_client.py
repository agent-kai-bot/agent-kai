"""Async client helpers for the agent-k.ai market data provider."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from data_api.config import AGENT_KAI_BASE_URL, AGENT_KAI_HTTP_TIMEOUT_SECONDS, TRACKED_SYMBOLS

SUPPORTED_REMOTE_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "12h", "1d"}


def normalize_symbol(symbol: str) -> str:
    """Normalize market symbols to the local base-ticker format.

    Args:
        symbol: Raw symbol from user input or upstream API.

    Returns:
        A normalized uppercase base ticker such as ``BTC``.
    """
    cleaned = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
    for suffix in ("USDT", "USD"):
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            return cleaned[: -len(suffix)]
    return cleaned


def interval_to_milliseconds(interval: str) -> int:
    """Convert an interval string into milliseconds.

    Args:
        interval: Interval such as ``1m`` or ``6h``.

    Returns:
        Interval size in milliseconds.

    Raises:
        ValueError: If the interval format is unsupported.
    """
    unit = interval[-1]
    magnitude = int(interval[:-1])
    unit_map = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if unit not in unit_map:
        raise ValueError(f"unsupported interval: {interval}")
    return magnitude * unit_map[unit]


def iso_to_unix_milliseconds(value: str | None) -> int | None:
    """Convert an ISO timestamp string to unix milliseconds.

    Args:
        value: ISO-8601 timestamp string or ``None``.

    Returns:
        Unix milliseconds or ``None`` when no input is supplied.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def unix_milliseconds_to_datetime(value: int | float) -> datetime:
    """Convert unix milliseconds to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)


def channel_symbol_and_interval(channel: str) -> tuple[str, str]:
    """Parse a websocket channel name into symbol and interval.

    Args:
        channel: Upstream channel such as ``market.BTC.1m``.

    Returns:
        A ``(symbol, interval)`` tuple using local normalized symbol format.

    Raises:
        ValueError: If the channel shape is invalid.
    """
    parts = channel.split(".")
    if len(parts) != 3 or parts[0] != "market":
        raise ValueError(f"unexpected channel format: {channel}")
    return normalize_symbol(parts[1]), parts[2]


def rows_to_bars(symbol: str, interval: str, rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Convert upstream OHLCV rows to local bar dicts.

    Args:
        symbol: Asset symbol.
        interval: Candle interval.
        rows: Upstream list of ``[ts, open, high, low, close, volume]`` rows.

    Returns:
        A list of local OHLCV bar dictionaries sorted oldest-first.
    """
    normalized_symbol = normalize_symbol(symbol)
    ordered = sorted(rows, key=lambda row: row[0])
    bars = []
    for row in ordered:
        bars.append(
            {
                "symbol": normalized_symbol,
                "interval": interval,
                "ts": unix_milliseconds_to_datetime(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    return bars


def aggregate_bars(rows: list[dict[str, Any]], target_interval: str) -> list[dict[str, Any]]:
    """Aggregate lower-timeframe bars into a higher timeframe.

    Args:
        rows: Oldest-first list of local bar dictionaries.
        target_interval: Requested output interval.

    Returns:
        Aggregated OHLCV bars in oldest-first order.
    """
    bucket_ms = interval_to_milliseconds(target_interval)
    aggregated: dict[int, dict[str, Any]] = {}

    for row in sorted(rows, key=lambda item: item["ts"]):
        ts_ms = int(row["ts"].timestamp() * 1000)
        bucket_ts_ms = (ts_ms // bucket_ms) * bucket_ms
        bucket = aggregated.get(bucket_ts_ms)

        if bucket is None:
            aggregated[bucket_ts_ms] = {
                "symbol": row["symbol"],
                "interval": target_interval,
                "ts": unix_milliseconds_to_datetime(bucket_ts_ms),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            continue

        bucket["high"] = max(bucket["high"], row["high"])
        bucket["low"] = min(bucket["low"], row["low"])
        bucket["close"] = row["close"]
        bucket["volume"] += row["volume"]

    return [aggregated[key] for key in sorted(aggregated)]


def event_to_bar(data: dict[str, Any], channel: str | None = None) -> dict[str, Any]:
    """Translate an upstream websocket event payload into a local OHLCV bar.

    Args:
        data: Upstream event payload.
        channel: Optional upstream channel name.

    Returns:
        Local OHLCV bar dictionary.
    """
    symbol = data.get("symbol")
    interval = data.get("interval")
    if channel:
        channel_symbol, channel_interval = channel_symbol_and_interval(channel)
        symbol = symbol or channel_symbol
        interval = interval or channel_interval

    return {
        "symbol": normalize_symbol(symbol or ""),
        "interval": interval or "1m",
        "ts": unix_milliseconds_to_datetime(data["ts"]),
        "open": float(data["open"]),
        "high": float(data["high"]),
        "low": float(data["low"]),
        "close": float(data["close"]),
        "volume": float(data["volume"]),
        "is_closed": bool(data.get("is_closed", False)),
        "source": data.get("source"),
    }


@dataclass(slots=True)
class AgentKaiMarketClient:
    """Async HTTP client for agent-k.ai market data."""

    api_key: str
    base_url: str = AGENT_KAI_BASE_URL
    timeout_seconds: float = AGENT_KAI_HTTP_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    @property
    def headers(self) -> dict[str, str]:
        """Build auth headers for upstream requests."""
        return {"Authorization": f"Bearer {self.api_key}"}

    async def close(self) -> None:
        """Close the underlying HTTP session if one exists."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def warmup(self) -> None:
        """Create the underlying HTTP session eagerly."""
        await self._get_session()

    async def fetch_ohlcv(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 500,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV bars and translate them into the local schema.

        Args:
            symbol: Asset symbol such as ``BTC``.
            interval: Requested interval.
            limit: Maximum number of output bars.
            start: Optional ISO-8601 lower bound.
            end: Optional ISO-8601 upper bound.

        Returns:
            A list of local OHLCV bars sorted oldest-first.
        """
        normalized_symbol = normalize_symbol(symbol)
        start_ms = iso_to_unix_milliseconds(start)
        end_ms = iso_to_unix_milliseconds(end)

        if interval == "6h":
            source_limit = max(1, min(limit, 5000) * 6)
            source_rows = await self._fetch_remote_rows(
                normalized_symbol,
                interval="1h",
                limit=source_limit,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            aggregated = aggregate_bars(rows_to_bars(normalized_symbol, "1h", source_rows), "6h")
            return aggregated[-min(limit, 5000):]

        remote_rows = await self._fetch_remote_rows(
            normalized_symbol,
            interval=interval,
            limit=limit,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        return rows_to_bars(normalized_symbol, interval, remote_rows)

    async def fetch_latest_price(self, symbol: str) -> dict[str, Any] | None:
        """Fetch the latest price update for a symbol."""
        bars = await self.fetch_ohlcv(symbol=symbol, interval="1m", limit=1)
        if not bars:
            return None
        latest = bars[-1]
        return {
            "symbol": latest["symbol"],
            "price": latest["close"],
            "ts": latest["ts"],
            "volume": latest["volume"],
        }

    async def fetch_symbols(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch the latest known price for each tracked symbol."""
        selected_symbols = symbols or TRACKED_SYMBOLS
        tasks = [self.fetch_latest_price(symbol) for symbol in selected_symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        payload = []
        for symbol, result in zip(selected_symbols, results, strict=True):
            if isinstance(result, Exception) or result is None:
                payload.append({"symbol": normalize_symbol(symbol), "latest_price": None, "latest_ts": None})
                continue
            payload.append(
                {
                    "symbol": result["symbol"],
                    "latest_price": result["price"],
                    "latest_ts": result["ts"],
                }
            )
        return payload

    async def healthcheck(self) -> dict[str, Any]:
        """Probe the remote provider for health information."""
        sample_symbol = TRACKED_SYMBOLS[0] if TRACKED_SYMBOLS else "BTC"
        latest = await self.fetch_latest_price(sample_symbol)
        return {
            "status": "ok" if latest else "error",
            "provider": "agent-kai",
            "symbol": sample_symbol,
        }

    async def _fetch_remote_rows(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[list[Any]]:
        """Fetch raw upstream OHLCV rows, paging when needed.

        Args:
            symbol: Asset symbol.
            interval: Requested upstream interval.
            limit: Number of rows requested.
            start_ms: Optional start time in unix milliseconds.
            end_ms: Optional end time in unix milliseconds.

        Returns:
            Raw upstream OHLCV rows sorted oldest-first.
        """
        if interval not in SUPPORTED_REMOTE_INTERVALS:
            raise ValueError(f"unsupported upstream interval: {interval}")

        remaining = max(1, min(limit, 5000))
        rows: list[list[Any]] = []
        cursor_to = end_ms
        seen_timestamps: set[int] = set()

        while remaining > 0:
            batch_limit = min(remaining, 1000)
            params: dict[str, Any] = {"interval": interval, "limit": batch_limit}
            if start_ms is not None:
                params["from"] = start_ms
            if cursor_to is not None:
                params["to"] = cursor_to

            payload = await self._request_json("GET", f"/v1/market/ohlcv/{symbol}", params=params)
            batch = payload.get("data", [])
            if not batch:
                break

            normalized_batch = sorted(batch, key=lambda item: item[0])
            deduped_batch = [item for item in normalized_batch if int(item[0]) not in seen_timestamps]
            if not deduped_batch:
                break

            rows = deduped_batch + rows
            for item in deduped_batch:
                seen_timestamps.add(int(item[0]))

            remaining = max(0, limit - len(rows))
            earliest_ts = int(deduped_batch[0][0])
            if len(batch) < batch_limit or (start_ms is not None and earliest_ts <= start_ms):
                break
            cursor_to = earliest_ts - 1

        ordered_rows = sorted(rows, key=lambda item: item[0])
        if start_ms is not None:
            ordered_rows = [row for row in ordered_rows if int(row[0]) >= start_ms]
        if end_ms is not None:
            ordered_rows = [row for row in ordered_rows if int(row[0]) <= end_ms]
        return ordered_rows[-limit:]

    async def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an HTTP request and return decoded JSON."""
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        async with session.request(method, url, params=params, headers=self.headers) as response:
            response.raise_for_status()
            return await response.json()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return an existing HTTP session or create one lazily."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

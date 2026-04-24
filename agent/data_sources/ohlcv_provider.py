"""Unified OHLCV provider for TimeseriesDB and agent-k.ai.

This module gives the rest of the codebase a single place to request OHLCV
without caring whether candles come from:

1. a local Postgres / TimescaleDB 1m warehouse, or
2. the hosted agent-k.ai REST market-data API.

Primary design goal
-------------------
Callers should ask for a symbol set + lookback window + target timeframe and let
this module decide how to fetch and normalize the data.

Behavior
--------
- ``source='auto'``: prefer TimeseriesDB when ``db_url`` is configured,
  otherwise fall back to agent-k.ai.
- ``source='timeseries'``: require DB access.
- ``source='agent-kai'``: require API access.
- data is normalized to a pandas DataFrame with columns:
  ``ts, symbol, open, high, low, close, volume``
- higher timeframes are produced by pandas resampling from 1m data for
  consistent behavior across providers.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd
import requests
from sqlalchemy import create_engine, text

SAFE_IDENT = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
AGENT_KAI_ENV_VAR = "AGENT_KAI_API_KEY"
AGENT_KAI_BASE_URL = os.getenv("AGENT_KAI_BASE_URL", "https://agent-k.ai").rstrip("/")


class OHLCVProviderError(RuntimeError):
    """Raised when OHLCV cannot be loaded from the requested source."""


@dataclass(slots=True)
class TimeseriesConfig:
    db_url: str = os.getenv("KAI_TIMESERIES_DB_URL", os.getenv("DATABASE_URL", ""))
    table: str = os.getenv("KAI_OHLCV_TABLE", "ohlcv_1m")
    ts_col: str = os.getenv("KAI_OHLCV_TS_COL", "ts")
    symbol_col: str = os.getenv("KAI_OHLCV_SYMBOL_COL", "symbol")
    open_col: str = os.getenv("KAI_OHLCV_OPEN_COL", "open")
    high_col: str = os.getenv("KAI_OHLCV_HIGH_COL", "high")
    low_col: str = os.getenv("KAI_OHLCV_LOW_COL", "low")
    close_col: str = os.getenv("KAI_OHLCV_CLOSE_COL", "close")
    volume_col: str = os.getenv("KAI_OHLCV_VOLUME_COL", "volume")


@dataclass(slots=True)
class AgentKaiConfig:
    api_key: str = os.getenv(AGENT_KAI_ENV_VAR, "").strip()
    base_url: str = AGENT_KAI_BASE_URL
    timeout_seconds: float = 20.0


def _safe_ident(name: str) -> str:
    if not name or any(ch not in SAFE_IDENT for ch in name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def normalize_symbol(symbol: str) -> str:
    cleaned = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
    for suffix in ("USDT", "USD"):
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            return cleaned[: -len(suffix)]
    return cleaned


def _normalize_symbols(symbols: Iterable[str] | None) -> list[str]:
    if not symbols:
        return []
    deduped: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def interval_to_minutes(interval: str) -> int:
    value = interval.strip().lower()
    aliases = {"1min": "1m", "min": "1m", "1t": "1m"}
    value = aliases.get(value, value)
    if value.endswith("min"):
        value = f"{value[:-3]}m"
    unit = value[-1]
    magnitude = int(value[:-1])
    scale = {"m": 1, "h": 60, "d": 1440}
    if unit not in scale:
        raise ValueError(f"Unsupported interval: {interval}")
    return magnitude * scale[unit]


def interval_to_pandas_rule(interval: str) -> str:
    minutes = interval_to_minutes(interval)
    if minutes % 1440 == 0:
        return f"{minutes // 1440}D"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}min"


def resample_ohlcv(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if df_1m.empty:
        return df_1m.copy()
    if interval_to_minutes(timeframe) == 1:
        return df_1m.copy().sort_values(["symbol", "ts"]).reset_index(drop=True)

    rule = interval_to_pandas_rule(timeframe)
    out = (
        df_1m.groupby("symbol")
        .resample(rule, on="ts")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
        .sort_values(["symbol", "ts"])
        .reset_index(drop=True)
    )
    return out


def _finalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["ts", "symbol", "open", "high", "low", "close", "volume"])
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["symbol"] = out["symbol"].astype(str).map(normalize_symbol)
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["ts", "symbol", "open", "high", "low", "close", "volume"])
    out = out.sort_values(["symbol", "ts"]).drop_duplicates(["symbol", "ts"], keep="last")
    return out.reset_index(drop=True)


def load_ohlcv_from_timeseries(
    *,
    symbols: Iterable[str] | None = None,
    lookback_hours: int = 72,
    config: TimeseriesConfig | None = None,
) -> pd.DataFrame:
    config = config or TimeseriesConfig()
    if not config.db_url:
        raise OHLCVProviderError("TimeseriesDB requested but db_url is not configured")

    cols = {
        "ts": _safe_ident(config.ts_col),
        "symbol": _safe_ident(config.symbol_col),
        "open": _safe_ident(config.open_col),
        "high": _safe_ident(config.high_col),
        "low": _safe_ident(config.low_col),
        "close": _safe_ident(config.close_col),
        "volume": _safe_ident(config.volume_col),
        "table": _safe_ident(config.table),
    }
    symbol_list = _normalize_symbols(symbols)
    params: dict[str, Any] = {"lookback_hours": lookback_hours}
    symbols_filter = ""
    if symbol_list:
        params["symbols"] = symbol_list
        symbols_filter = f" AND UPPER({cols['symbol']}) = ANY(:symbols)"

    sql = text(
        f"""
        SELECT
            {cols['ts']} AS ts,
            UPPER({cols['symbol']}) AS symbol,
            {cols['open']} AS open,
            {cols['high']} AS high,
            {cols['low']} AS low,
            {cols['close']} AS close,
            {cols['volume']} AS volume
        FROM {cols['table']}
        WHERE {cols['ts']} >= NOW() - (:lookback_hours * INTERVAL '1 hour')
        {symbols_filter}
        ORDER BY symbol, ts
        """
    )

    engine = create_engine(config.db_url)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params=params)
    return _finalize_frame(df)


def _require_agent_kai_key(config: AgentKaiConfig) -> str:
    key = config.api_key.strip()
    if not key:
        raise OHLCVProviderError(
            f"agent-k.ai requested but {AGENT_KAI_ENV_VAR} is not configured"
        )
    return key


def _request_agent_kai_rows(
    *,
    symbol: str,
    limit: int,
    start_ms: int | None,
    end_ms: int | None,
    config: AgentKaiConfig,
) -> list[list[Any]]:
    key = _require_agent_kai_key(config)
    rows: list[list[Any]] = []
    remaining = max(1, min(int(limit), 5000))
    cursor_to = end_ms
    seen: set[int] = set()
    session = requests.Session()

    while remaining > 0:
        batch_limit = min(remaining, 1000)
        params: dict[str, Any] = {"interval": "1m", "limit": batch_limit}
        if start_ms is not None:
            params["from"] = start_ms
        if cursor_to is not None:
            params["to"] = cursor_to

        resp = session.get(
            f"{config.base_url.rstrip('/')}/v1/market/ohlcv/{normalize_symbol(symbol)}",
            params=params,
            headers={"Authorization": f"Bearer {key}"},
            timeout=config.timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("data") or payload.get("bars") or []
        if not batch:
            break

        normalized = sorted(batch, key=lambda item: int(item[0]))
        deduped = [item for item in normalized if int(item[0]) not in seen]
        if not deduped:
            break

        rows = deduped + rows
        for item in deduped:
            seen.add(int(item[0]))

        remaining = max(0, limit - len(rows))
        earliest_ts = int(deduped[0][0])
        if len(batch) < batch_limit or (start_ms is not None and earliest_ts <= start_ms):
            break
        cursor_to = earliest_ts - 1

    ordered = sorted(rows, key=lambda item: int(item[0]))
    if start_ms is not None:
        ordered = [row for row in ordered if int(row[0]) >= start_ms]
    if end_ms is not None:
        ordered = [row for row in ordered if int(row[0]) <= end_ms]
    return ordered[-limit:]


def load_ohlcv_from_agent_kai(
    *,
    symbols: Iterable[str] | None = None,
    lookback_hours: int = 72,
    config: AgentKaiConfig | None = None,
) -> pd.DataFrame:
    config = config or AgentKaiConfig()
    symbol_list = _normalize_symbols(symbols)
    if not symbol_list:
        raise OHLCVProviderError("agent-k.ai OHLCV load requires at least one symbol")

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=lookback_hours)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    bars_needed = min(max(int(math.ceil(lookback_hours * 60)) + 5, 1), 5000)

    records: list[dict[str, Any]] = []
    failures: list[str] = []
    for symbol in symbol_list:
        try:
            rows = _request_agent_kai_rows(
                symbol=symbol,
                limit=bars_needed,
                start_ms=start_ms,
                end_ms=end_ms,
                config=config,
            )
        except Exception:
            failures.append(symbol)
            continue
        for row in rows:
            records.append(
                {
                    "ts": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
                    "symbol": normalize_symbol(symbol),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
    if not records and failures:
        raise OHLCVProviderError(f"agent-k.ai returned no OHLCV rows; failed symbols: {', '.join(failures[:10])}")
    return _finalize_frame(pd.DataFrame.from_records(records))


def load_ohlcv(
    *,
    symbols: Iterable[str] | None = None,
    timeframe: str = "1m",
    lookback_hours: int = 72,
    source: str = "auto",
    timeseries: TimeseriesConfig | None = None,
    agent_kai: AgentKaiConfig | None = None,
) -> pd.DataFrame:
    """Load OHLCV from the selected provider and resample if needed."""
    normalized_source = (source or "auto").strip().lower()
    timeseries = timeseries or TimeseriesConfig()
    agent_kai = agent_kai or AgentKaiConfig()

    if normalized_source == "auto":
        normalized_source = "timeseries" if timeseries.db_url else "agent-kai"

    if normalized_source == "timeseries":
        df_1m = load_ohlcv_from_timeseries(symbols=symbols, lookback_hours=lookback_hours, config=timeseries)
    elif normalized_source in {"agent-kai", "kai-api", "cloud"}:
        df_1m = load_ohlcv_from_agent_kai(symbols=symbols, lookback_hours=lookback_hours, config=agent_kai)
    else:
        raise ValueError(f"Unknown OHLCV source: {source}")

    return resample_ohlcv(df_1m, timeframe)

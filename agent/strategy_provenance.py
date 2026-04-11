"""Provenance helpers for reproducible ASO strategy evaluations."""

from __future__ import annotations

import hashlib
import json

import pandas as pd

from agent.strategy_ir import StrategyIR

EXECUTOR_VERSION = "1.0.0"

V1_EXECUTION = {
    "order_type": "market",
    "entry_timing": "bar_close",
    "exit_timing": "bar_close",
    "fee_pct": 0.001,
    "slippage_pct": 0.001,
    "spread_pct": 0.0005,
    "partial_fills": False,
    "reduce_only_exits": True,
}

_OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def compute_dataset_hash(ohlcv: pd.DataFrame) -> str:
    """Return a deterministic SHA256 digest for normalized OHLCV data."""
    frame = _normalize_ohlcv(ohlcv)
    hasher = hashlib.sha256()
    for timestamp, row in frame.iterrows():
        encoded = "|".join(
            [
                _normalize_timestamp(timestamp),
                *(repr(float(row[column])) for column in _OHLCV_COLUMNS),
            ]
        ).encode("utf-8")
        hasher.update(encoded)
        hasher.update(b"\n")
    return hasher.hexdigest()


def get_fee_model_json(ir: StrategyIR) -> str:
    """Serialize the frozen v1 execution and cost inputs for a strategy."""
    payload = dict(V1_EXECUTION)
    payload.update(
        {
            "fee_pct": ir.costs.fee_pct,
            "slippage_pct": ir.costs.slippage_pct,
            "spread_pct": ir.costs.spread_pct,
        }
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _normalize_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    renamed = ohlcv.rename(columns={column: column.capitalize() for column in ohlcv.columns}).copy()
    missing = [column for column in _OHLCV_COLUMNS if column not in renamed.columns]
    if missing:
        raise ValueError(f"ohlcv is missing required columns: {', '.join(missing)}")

    frame = renamed[list(_OHLCV_COLUMNS)].astype(float)
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _normalize_timestamp(timestamp: pd.Timestamp) -> str:
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()

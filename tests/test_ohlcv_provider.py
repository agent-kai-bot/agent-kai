from __future__ import annotations

import pandas as pd

from agent.data_sources.ohlcv_provider import normalize_symbol, resample_ohlcv


def test_normalize_symbol_strips_common_quote_suffixes() -> None:
    assert normalize_symbol("BTC-USD") == "BTC"
    assert normalize_symbol("ethusdt") == "ETH"
    assert normalize_symbol("SOL") == "SOL"


def test_resample_ohlcv_preserves_symbol_and_aggregates_correctly() -> None:
    df = pd.DataFrame(
        {
            "ts": pd.to_datetime(
                [
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:01:00Z",
                    "2026-01-01T00:02:00Z",
                    "2026-01-01T00:03:00Z",
                ],
                utc=True,
            ),
            "symbol": ["BTC", "BTC", "BTC", "BTC"],
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [101.0, 102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0, 102.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "volume": [1.0, 2.0, 3.0, 4.0],
        }
    )

    out = resample_ohlcv(df, "2min")

    assert list(out.columns) == ["symbol", "ts", "open", "high", "low", "close", "volume"]
    assert len(out) == 2
    first = out.iloc[0]
    assert first["symbol"] == "BTC"
    assert first["open"] == 100.0
    assert first["high"] == 102.0
    assert first["low"] == 99.0
    assert first["close"] == 102.0
    assert first["volume"] == 3.0

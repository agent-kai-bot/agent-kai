---
name: unified-ohlcv-provider-auto-timeseries-or-agent-kai
description: Build and use a single OHLCV module that can load 1m candles from TimeseriesDB or agent-k.ai, then resample with pandas so callers don't care about the backend.
category: analysis
tags: [ohlcv, timeseries, agent-kai, pandas, resampling, abstraction]
---
# Unified OHLCV Provider

## When to use
Use when strategy code, scanners, or backtests should work against either:
1. local Timeseries/Postgres 1m candles, or
2. hosted agent-k.ai OHLCV

without duplicating fetch logic across scripts.

## Steps
1. Create a provider module under `agent/data_sources/ohlcv_provider.py`.
2. Normalize both backends to the same pandas DataFrame shape:
   - `ts, symbol, open, high, low, close, volume`
3. Support source selection:
   - `auto`: prefer TimeseriesDB if `KAI_TIMESERIES_DB_URL` / `DATABASE_URL` exists, else use agent-k.ai
   - `timeseries`: require DB config
   - `agent-kai`: require `AGENT_KAI_API_KEY`
4. Pull only 1m raw data from both sources, then resample with pandas for consistency.
5. For TimeseriesDB, make schema configurable via env/args:
   - `KAI_OHLCV_TABLE`, `KAI_OHLCV_TS_COL`, `KAI_OHLCV_SYMBOL_COL`, `KAI_OHLCV_OPEN_COL`, `KAI_OHLCV_HIGH_COL`, `KAI_OHLCV_LOW_COL`, `KAI_OHLCV_CLOSE_COL`, `KAI_OHLCV_VOLUME_COL`
6. For agent-k.ai, page `/v1/market/ohlcv/{symbol}` with `interval=1m`, `limit<=1000`, and `from`/`to` ms bounds until the requested lookback is covered.
7. Reuse the provider from scanners/backtests instead of embedding SQL in each script.
8. For standalone scripts under `scripts/`, prepend repo root to `sys.path` before importing `agent...` modules.

## Pitfalls
- Pandas `groupby(...).resample(...)` can drop the group key depending on how it's chained. Reliable form:
  - `df.groupby("symbol").resample(rule, on="ts").agg(...).reset_index()`
- `5min` is not the same as `5m` for custom interval parsing. Normalize `*min` suffixes to minutes.
- If tests cannot import `agent`, add `tests/conftest.py` to inject repo root into `sys.path`.
- Keep symbol normalization consistent (`BTC-USD` -> `BTC`, `ETHUSDT` -> `ETH`) so pooled analysis doesn't split symbols unexpectedly.

## Verification
1. `python -m py_compile agent/data_sources/ohlcv_provider.py scripts/post_dump_breakout_scan.py`
2. `python scripts/post_dump_breakout_scan.py --help`
3. Smoke test cloud path:
   - `python scripts/post_dump_breakout_scan.py --mode scan --source agent-kai --symbols BTC --lookback-hours 6 --timeframe 5min --json`
4. Run focused tests:
   - `pytest -q tests/test_ohlcv_provider.py -q`
5. Confirm script output is identical shape regardless of source selection.

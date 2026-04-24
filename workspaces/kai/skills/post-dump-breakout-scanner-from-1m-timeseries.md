---
name: post-dump-breakout-scanner-from-1m-timeseries
description: Build a pandas-based scanner that resamples 1m OHLCV from TimescaleDB and detects post-dump breakout setups with vectorized TA.
category: analysis
tags: [timeseriesdb, pandas, timescaledb, ohlcv, breakout, scanner, bollinger, rsi, macd, volume]
---
# Post-Dump Breakout Scanner From 1m Timeseries

## When to use
Use when you have 1-minute OHLCV stored in TimescaleDB/Postgres and want to scan many symbols quickly for a "dump then recovery breakout" pattern without looping symbol-by-symbol in Python.

## Steps
1. Load OHLCV for all target symbols in one SQL query from the 1m table.
2. Normalize to columns: `ts, symbol, open, high, low, close, volume`.
3. Resample with pandas using:
   - `df.set_index('ts').groupby('symbol').resample(rule).agg(...)`
4. Compute vectorized per-symbol features via `groupby('symbol').transform(...)`:
   - Bollinger Bands
   - RSI
   - EMA fast/slow
   - MACD / signal / histogram
   - Volume MA, volume ratio, volume z-score
   - Candle body / wick / close-location features
5. Model the setup in 3 phases:
   - Dump bar: sharp negative return, high volume ratio, close/low into lower Bollinger area
   - Selling fade: downside and volume both cool off after the dump
   - Buyers return: reclaim recent highs / BB mid / fast EMA with improving RSI and MACD histogram
6. Rank candidates with a simple additive score so downstream agents can sort by quality.
7. Print either a human table or JSON for further automation.

## Reference implementation
A reusable script exists at:
- `scripts/post_dump_breakout_scan.py`

It now supports four primary workflows:
- `--mode scan`: emit current candidates
- `--mode backtest`: simulate entries/exits and report stats + trades
- `--mode optimize`: run grid/random hyperparameter search and rank results
- `--mode sweep-dumps`: rank symbols/events by dump severity and forward recovery stats so you can find markets that actually fit the setup before backtesting

`sweep-dumps` accepts either `--symbols ...` or `--symbols-file path.txt` and reports per-symbol dump counts, worst dump size, max volume spike, BB/EMA reclaim rates within the recovery window, lower-low rate, and forward returns over configurable horizons like `--forward-bars 3 6 12`.

Example:
```bash
python scripts/post_dump_breakout_scan.py \
  --mode sweep-dumps \
  --source agent-kai \
  --symbols-file symbols.txt \
  --timeframe 5min \
  --lookback-hours 720 \
  --sweep-top-k 25 \
  --forward-bars 3 6 12 \
  --json
```

Use `sweep-dumps` before optimization when BTC underproduces signals; it helps identify better-fit symbols like ETH/SOL/ADA without guessing.

Note: when using the agent-k.ai backend across a broad symbol list, unsupported symbols can 404. The provider should skip per-symbol failures and continue with the rest of the universe.

Potential cleanup: the current sweep implementation uses `groupby.apply(...)` for a few recovery-window flags and may emit pandas FutureWarnings; functionally fine for now, but replace with vectorized/group-safe transforms later.

Key anti-leakage detail: dump detection is based on prior bars and entries only fire after fade/reclaim confirmation, so current-bar signals do not use future information.

Potential next step after optimization: split in-sample vs out-of-sample by time and only trust parameter sets that stay robust OOS.

```bash
# current script only uses one contiguous range; add manual OOS by running on two windows separately
```

Verification tip: compile with `python -m py_compile scripts/post_dump_breakout_scan.py` and run `--help` before wiring to production.

## Example:
```bash
python scripts/post_dump_breakout_scan.py \
  --db-url "$KAI_TIMESERIES_DB_URL" \
  --table ohlcv_1m \
  --symbols BTC ETH SOL \
  --timeframe 5min \
  --lookback-hours 72
```

## Important args
- `--mode`: `scan`, `backtest`, `optimize`, `diagnose`, or `sweep-dumps`
- `--symbols-file`: newline-delimited universe file for large sweeps
- `--sweep-top-k`: how many symbols/events to report in sweep mode
- `--forward-bars`: horizons used for post-dump forward-return stats
- `--db-url`: Postgres / Timescale connection string
- `--table`: 1m candle table/view name
- `--timeframe`: pandas resample rule (`5min`, `15min`, `1h`)
- `--min-volume-ratio`, `--min-vol-z`, `--min-dump-return`: define the dump
- `--fade-bars`, `--max-recovery-bars`: define the stabilization/recovery window
- `--breakout-lookback`, `--min-rsi-rebound`, `--min-macd-hist-improve`: define buyer return
- `--sl-pct`, `--tp-pct`, `--max-hold-bars`, `--exit-on-rsi`, `--fee-bps`, `--slippage-bps`: backtest controls
- `--search`, `--n-trials`, `--objective`, `--min-trades`, `--top-k`: optimization controls

## Pitfalls
- Avoid interpreting top in-sample optimization results as production-ready; rerun on a later window.
- If tuning feature windows (BB/RSI/EMA periods), the script recomputes features each trial; this is correct but slower.
- Large cross-products in grid search can explode; prefer `--search random` first.
- Backtest currently exits on intrabar stop/target with simple priority ordering (stop before target). For tighter realism, model bar path assumptions explicitly.
- No position sizing yet; results are trade-return based with simple compounded equity.

## Verification
1. Run `python -m py_compile scripts/post_dump_breakout_scan.py`.
2. Confirm `python scripts/post_dump_breakout_scan.py --help` works.
3. Run `--mode scan` on a short lookback.
4. Run `--mode backtest` on a longer window and inspect recent trades.
5. Run `--mode optimize` with `--search random --n-trials 25` first, then scale up.
6. Validate the best config on a separate out-of-sample window.

## Important args
- `--db-url`: Postgres / Timescale connection string
- `--table`: 1m candle table/view name
- `--ts-col`, `--symbol-col`, `--open-col`, `--high-col`, `--low-col`, `--close-col`, `--volume-col`: map schema differences
- `--timeframe`: pandas resample rule (`5min`, `15min`, `1h`)
- `--min-volume-ratio`: dump bar volume spike threshold
- `--min-dump-return`: minimum negative return for dump detection
- `--fade-bars`: bars allowed for selling to fade
- `--breakout-lookback`: bars used to define reclaim level
- `--min-rsi-rebound`: required RSI improvement from dump low to breakout
- `--min-score`: final candidate cutoff

## Pitfalls
- SQLAlchemy / pandas parameter handling with `ANY(:symbols)` can be driver-sensitive; test against the actual DB driver.
- Resample rule names should be pandas-friendly (`5min`, `15min`, `1h`), not exchange shorthand unless converted.
- The script assumes one row per symbol+minute; dedupe by `[symbol, ts]` first.
- A true Timescale schema may use different column names; pass the override flags rather than editing code.
- Feature leakage risk: breakout levels should use shifted rolling highs, not current-bar highs.

## Verification
1. Run `python -m py_compile scripts/post_dump_breakout_scan.py`.
2. Test on a few known dump/reversal examples with `--json`.
3. Manually inspect 3-5 hits on charts to tune thresholds.
4. If promising, validate with a follow-up backtest that measures returns after signal generation.

## Important args
- `--db-url`: Postgres / Timescale connection string
- `--table`: 1m candle table/view name
- `--ts-col`, `--symbol-col`, `--open-col`, `--high-col`, `--low-col`, `--close-col`, `--volume-col`: map schema differences
- `--timeframe`: pandas resample rule (`5min`, `15min`, `1h`)
- `--min-volume-ratio`: dump bar volume spike threshold
- `--min-dump-return`: minimum negative return for dump detection
- `--fade-bars`: bars allowed for selling to fade
- `--breakout-lookback`: bars used to define reclaim level
- `--min-rsi-rebound`: required RSI improvement from dump low to breakout
- `--min-score`: final candidate cutoff

## Pitfalls
- SQLAlchemy / pandas parameter handling with `ANY(:symbols)` can be driver-sensitive; test against the actual DB driver.
- Resample rule names should be pandas-friendly (`5min`, `15min`, `1h`), not exchange shorthand unless converted.
- The script assumes one row per symbol+minute; dedupe by `[symbol, ts]` first.
- A true Timescale schema may use different column names; pass the override flags rather than editing code.
- Feature leakage risk: breakout levels should use shifted rolling highs, not current-bar highs.

## Verification
1. Run `python -m py_compile scripts/post_dump_breakout_scan.py`.
2. Test on a few known dump/reversal examples with `--json`.
3. Manually inspect 3-5 hits on charts to tune thresholds.
4. If promising, validate with a follow-up backtest that measures returns after signal generation.

Examples:
```bash
python scripts/post_dump_breakout_scan.py \
  --mode scan \
  --db-url "$KAI_TIMESERIES_DB_URL" \
  --table ohlcv_1m \
  --symbols BTC ETH SOL \
  --timeframe 5min \
  --lookback-hours 72

python scripts/post_dump_breakout_scan.py \
  --mode backtest \
  --db-url "$KAI_TIMESERIES_DB_URL" \
  --symbols BTC ETH SOL \
  --timeframe 5min \
  --lookback-hours 720 \
  --sl-pct 1.2 --tp-pct 3.0

python scripts/post_dump_breakout_scan.py \
  --mode optimize \
  --db-url "$KAI_TIMESERIES_DB_URL" \
  --symbols BTC ETH SOL \
  --timeframe 5min \
  --lookback-hours 720 \
  --search random --n-trials 200 --top-k 20
```

The backtest uses next-bar-open entry by default, with configurable stop, target, max hold, RSI exit, cooldown, fees, and slippage.
The optimizer tunes signal thresholds plus exit/risk parameters and ranks candidates by sharpe / return / expectancy / profit factor.
```bash
# Save top optimization results to disk
python scripts/post_dump_breakout_scan.py --mode optimize ... --output results.json --json
```

Key anti-leakage detail: dump detection is based on prior bars and entries only fire after fade/reclaim confirmation, so current-bar signals do not use future information.

Potential next step after optimization: split in-sample vs out-of-sample by time and only trust parameter sets that stay robust OOS.

```bash
# current script only uses one contiguous range; add manual OOS by running on two windows separately
```

Verification tip: compile with `python -m py_compile scripts/post_dump_breakout_scan.py` and run `--help` before wiring to production.

## Example:
```bash
python scripts/post_dump_breakout_scan.py \
  --db-url "$KAI_TIMESERIES_DB_URL" \
  --table ohlcv_1m \
  --symbols BTC ETH SOL \
  --timeframe 5min \
  --lookback-hours 72
```

## Important args
- `--mode`: `scan`, `backtest`, or `optimize`
- `--db-url`: Postgres / Timescale connection string
- `--table`: 1m candle table/view name
- `--timeframe`: pandas resample rule (`5min`, `15min`, `1h`)
- `--min-volume-ratio`, `--min-vol-z`, `--min-dump-return`: define the dump
- `--fade-bars`, `--max-recovery-bars`: define the stabilization/recovery window
- `--breakout-lookback`, `--min-rsi-rebound`, `--min-macd-hist-improve`: define buyer return
- `--sl-pct`, `--tp-pct`, `--max-hold-bars`, `--exit-on-rsi`, `--fee-bps`, `--slippage-bps`: backtest controls
- `--search`, `--n-trials`, `--objective`, `--min-trades`, `--top-k`: optimization controls

## Pitfalls
- Avoid interpreting top in-sample optimization results as production-ready; rerun on a later window.
- If tuning feature windows (BB/RSI/EMA periods), the script recomputes features each trial; this is correct but slower.
- Large cross-products in grid search can explode; prefer `--search random` first.
- Backtest currently exits on intrabar stop/target with simple priority ordering (stop before target). For tighter realism, model bar path assumptions explicitly.
- No position sizing yet; results are trade-return based with simple compounded equity.

## Verification
1. Run `python -m py_compile scripts/post_dump_breakout_scan.py`.
2. Confirm `python scripts/post_dump_breakout_scan.py --help` works.
3. Run `--mode scan` on a short lookback.
4. Run `--mode backtest` on a longer window and inspect recent trades.
5. Run `--mode optimize` with `--search random --n-trials 25` first, then scale up.
6. Validate the best config on a separate out-of-sample window.

## Important args
- `--db-url`: Postgres / Timescale connection string
- `--table`: 1m candle table/view name
- `--ts-col`, `--symbol-col`, `--open-col`, `--high-col`, `--low-col`, `--close-col`, `--volume-col`: map schema differences
- `--timeframe`: pandas resample rule (`5min`, `15min`, `1h`)
- `--min-volume-ratio`: dump bar volume spike threshold
- `--min-dump-return`: minimum negative return for dump detection
- `--fade-bars`: bars allowed for selling to fade
- `--breakout-lookback`: bars used to define reclaim level
- `--min-rsi-rebound`: required RSI improvement from dump low to breakout
- `--min-score`: final candidate cutoff

## Pitfalls
- SQLAlchemy / pandas parameter handling with `ANY(:symbols)` can be driver-sensitive; test against the actual DB driver.
- Resample rule names should be pandas-friendly (`5min`, `15min`, `1h`), not exchange shorthand unless converted.
- The script assumes one row per symbol+minute; dedupe by `[symbol, ts]` first.
- A true Timescale schema may use different column names; pass the override flags rather than editing code.
- Feature leakage risk: breakout levels should use shifted rolling highs, not current-bar highs.

## Verification
1. Run `python -m py_compile scripts/post_dump_breakout_scan.py`.
2. Test on a few known dump/reversal examples with `--json`.
3. Manually inspect 3-5 hits on charts to tune thresholds.
4. If promising, validate with a follow-up backtest that measures returns after signal generation.

## Important args
- `--db-url`: Postgres / Timescale connection string
- `--table`: 1m candle table/view name
- `--ts-col`, `--symbol-col`, `--open-col`, `--high-col`, `--low-col`, `--close-col`, `--volume-col`: map schema differences
- `--timeframe`: pandas resample rule (`5min`, `15min`, `1h`)
- `--min-volume-ratio`: dump bar volume spike threshold
- `--min-dump-return`: minimum negative return for dump detection
- `--fade-bars`: bars allowed for selling to fade
- `--breakout-lookback`: bars used to define reclaim level
- `--min-rsi-rebound`: required RSI improvement from dump low to breakout
- `--min-score`: final candidate cutoff

## Pitfalls
- SQLAlchemy / pandas parameter handling with `ANY(:symbols)` can be driver-sensitive; test against the actual DB driver.
- Resample rule names should be pandas-friendly (`5min`, `15min`, `1h`), not exchange shorthand unless converted.
- The script assumes one row per symbol+minute; dedupe by `[symbol, ts]` first.
- A true Timescale schema may use different column names; pass the override flags rather than editing code.
- Feature leakage risk: breakout levels should use shifted rolling highs, not current-bar highs.

## Verification
1. Run `python -m py_compile scripts/post_dump_breakout_scan.py`.
2. Test on a few known dump/reversal examples with `--json`.
3. Manually inspect 3-5 hits on charts to tune thresholds.
4. If promising, validate with a follow-up backtest that measures returns after signal generation.

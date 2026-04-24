---
name: btc-mtf-analysis-with-validation
description: Multi-timeframe BTC analysis workflow that validates structure, momentum, and candidate trade logic with a quick backtest.
category: analysis
tags: [btc, technical-analysis, multi-timeframe, validation, coinbase]
---
# BTC Multi-Timeframe Analysis With Validation

## When to use
Use when asked to analyze BTC and extract a reusable workflow rather than provide pure commentary.

## Steps
1. Check for prior reusable knowledge with `skills_list`.
2. Check live scanner context first:
   - `get_signals(symbol="BTC", limit=10)`
   - `get_latest_price(symbol="BTC")`
   - `get_coinbase_price(symbol="BTC")`
3. Pull market structure on three timeframes:
   - Local: `query_ohlcv` for `1h` and `1d`
   - Preferred middle timeframe: `6h`
4. Compute indicators per timeframe:
   - RSI(14)
   - EMA(20)
   - EMA(50)
   - MACD
   - ATR on the execution timeframe (usually 1h)
5. If local `6h` data/errors fail, immediately fall back to Coinbase:
   - `get_coinbase_candles(symbol="BTC", interval="6h", limit=120)`
   - Also use `get_coinbase_candles(... interval="1h")` to cross-check local 1h trend if needed.
6. Read the regime top-down:
   - Daily = macro bias from price vs EMA20/EMA50 + MACD + RSI
   - 6h = swing bias / whether momentum confirms the daily bias
   - 1h = tactical trigger / whether near-term continuation or mean reversion is more likely
7. Mark simple levels from recent candles:
   - Recent swing high
   - Recent swing low
   - Current price vs 1h EMA20 and EMA50
   - ATR(14) on 1h for expected move and stop sizing
8. Form 1-2 candidate hypotheses only, for example:
   - Trend continuation: close > EMA50, MACD > signal, RSI > 50
   - Mean reversion: RSI < 35 and close < lower band, then exit on RSI recovery / EMA reclaim
9. Validate hypotheses with `run_backtest` on 1h over ~500 bars before recommending them.
10. Keep only strategies with roughly `win_rate > 55%` and `sharpe > 0.5`. If backtest is weak, say the workflow is useful but the tested trigger set is not production-worthy.

## Pitfalls
- Local `6h` BTC OHLCV can return a server error; use Coinbase 6h candles as the fallback without blocking the analysis.
- Daily price can be above EMAs while daily MACD stays bearish; treat this as a mixed regime, not a clean trend signal.
- A good Sharpe with a poor win rate can still be a weak workflow for discretionary trading if trade count is small; mention both.
- Do not promote candidate rules into a recommendation unless the backtest is acceptably positive.

## Verification
- You should end with three outputs:
  1. Current BTC regime by timeframe
  2. Key levels / volatility context
  3. A reusable checklist plus which tested hypotheses passed or failed validation
- Confirm at least one cross-venue check (local vs Coinbase) if any local timeframe errors occur.
- Confirm candidate rules were backtested, not just described.

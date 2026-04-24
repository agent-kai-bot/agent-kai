---
name: multi-timeframe-technical-analysis
description: Run a comprehensive technical analysis across multiple timeframes (1h/4h/1d) with proper interval specification and data limit handling
category: analysis
tags: [technical-analysis, multi-timeframe, indicator-calculation, data-validation]
---

# Multi-Timeframe Technical Analysis

## When to use
When the user requests a "full technical analysis" or "comprehensive analysis" on a crypto symbol without specifying a single timeframe, or when they explicitly ask for multiple timeframes (e.g., "weekly analysis," "swing analysis," "daily and 4h").

## Steps

### 1. Clarify the timeframe intent
- If user says "weekly" → use `interval='1d'` (daily candles represent weekly structure)
- If user says "swing" → use `interval='4h'` and `interval='1d'`
- If user says "intraday" → use `interval='1h'` and `interval='15m'`
- **Never assume 1m** unless explicitly requested

### 2. Check available data first
- Call `query_ohlcv` with `interval` and a conservative `limit` (e.g., 100) to see how many bars exist
- Note the actual bar count from the output (e.g., "BTC 1d — 85 bars")
- **Adjust all subsequent `calculate_indicator` calls** to use `limit <= actual_bar_count`

### 3. Calculate indicators with correct parameters
For each timeframe, calculate:
```
- RSI(14) with interval matching the timeframe
- MACD(12,26,9) with interval matching
- BBANDS(14) with interval matching
- EMA(50) with interval matching
- ATR(14) with interval matching
```

**Critical:** Always specify `interval` explicitly in every `calculate_indicator` call. The default is `1m` which is wrong for most analysis.

### 4. Handle "Not enough data" errors gracefully
- If you get "Not enough data (N bars) for indicator(M)" error:
  - Reduce the `limit` parameter to `min(limit, actual_bar_count)`
  - Or skip that indicator if it requires more data than available
  - **Do not retry with the same limit** — you already know it will fail

### 5. Synthesize findings across timeframes
- Compare RSI/MACD/BBANDS signals across 1h, 4h, and 1d
- Note if signals align (stronger conviction) or conflict (caution)
- Identify key support/resistance from the highest timeframe available
- Calculate ATR to understand volatility and set realistic stop distances

### 6. Deliver structured output
Present your analysis with:
- Current price and timeframe context
- Key indicators table (RSI, MACD, BBANDS position, EMA status)
- Trend assessment per timeframe
- Support/resistance levels (from highest timeframe)
- Overall conclusion with confidence level
- Watch items for confirmation

## Pitfalls

1. **Default interval trap**: The `calculate_indicator` tool defaults to `interval='1m'`. If you forget to specify `interval='1d'`, you'll get meaningless 1-minute data for a "weekly analysis."

2. **Over-requesting bars**: Daily charts often have only 85-100 bars of data. Requesting `limit=200` or `limit=300` will cause "Not enough data" errors for indicators like EMA(200) or SMA(200).

3. **Ignoring data limitations**: Don't keep retrying the same failed call. Read the error message, note the actual bar count, and adjust your next call accordingly.

4. **Single timeframe bias**: A "full analysis" should cover at least 2-3 timeframes. Don't just give me the 1d chart — show me how 1h, 4h, and 1d align or conflict.

## Verification

After completing your analysis, verify:
- [ ] All `calculate_indicator` calls had explicit `interval` parameters
- [ ] All `limit` parameters are <= the actual bar count reported by `query_ohlcv`
- [ ] You analyzed at least 2 timeframes (e.g., 4h and 1d for swing analysis)
- [ ] Your support/resistance levels come from the highest timeframe available
- [ ] You noted any "Not enough data" errors and adjusted accordingly

## Example workflow

```python
# User: "Run a full technical analysis on BTC weekly timeframe"

# Step 1: Check daily data availability
query_ohlcv(symbol='BTC', interval='1d', limit=100)
# Output: "BTC 1d — 85 bars"

# Step 2: Calculate indicators with adjusted limits
calculate_indicator(symbol='BTC', indicator='RSI', interval='1d', limit=85)
calculate_indicator(symbol='BTC', indicator='MACD', interval='1d', limit=85)
calculate_indicator(symbol='BTC', indicator='BBANDS', interval='1d', limit=85)
calculate_indicator(symbol='BTC', indicator='EMA', interval='1d', period=50, limit=85)
# Skip EMA(200) — not enough data

# Step 3: Also check 4h for shorter-term context
query_ohlcv(symbol='BTC', interval='4h', limit=100)
calculate_indicator(symbol='BTC', indicator='RSI', interval='4h', limit=100)
calculate_indicator(symbol='BTC', indicator='MACD', interval='4h', limit=100)

# Step 4: Get current price
get_latest_price(symbol='BTC')

# Step 5: Synthesize and present
```
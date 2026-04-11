---
name: btc-multi-timeframe-monitor
description: Monitor BTC across 15m, 1h, 4h for alignment signals
category: trading
tags: [btc, monitoring, alignment, multi-timeframe]
---

# BTC Multi-Timeframe Alignment Monitor

## When to use
Use this skill when you need to continuously monitor BTC across multiple timeframes (15m, 1h, 4h) for alignment signals that indicate strong bullish or bearish trends.

## Monitoring Criteria

### BULLISH ALIGNMENT (all 3 timeframes must pass):
- 15m: Price > EMA20 AND RSI(14) > 55
- 1h: Price > EMA20 AND RSI(14) > 55
- 4h: Price > EMA20 AND RSI(14) > 55

### BEARISH ALIGNMENT (all 3 timeframes must pass):
- 15m: Price < EMA20 AND RSI(14) < 45
- 1h: Price < EMA20 AND RSI(14) < 45
- 4h: Price < EMA20 AND RSI(14) < 45

## Steps

1. **Initialize monitoring loop**
   - Set check interval to 60 seconds
   - Set alert cooldown to 300 seconds (5 minutes) to prevent spam
   - Use agent's tool system to make tool calls (query_ohlcv, calculate_indicator, nats_publish)

2. **For each timeframe (15m, 1h, 4h):**
   - Query OHLCV data (limit=50 candles) using query_ohlcv tool
   - Calculate EMA20 using calculate_indicator tool
   - Calculate RSI14 using calculate_indicator tool
   - Store: price, ema20, rsi14

3. **Check alignment conditions:**
   - Track if ALL timeframes are bullish
   - Track if ALL timeframes are bearish

4. **When bullish alignment detected:**
   - Entry: Current 15m price
   - Stop Loss: Entry - (2 × ATR_15m)
   - First Target: Entry + (2 × ATR_15m)
   - Invalidation: Entry - (3 × ATR_15m)
   - R:R Ratio: 1:2

5. **When bearish alignment detected:**
   - Entry: Current 15m price
   - Stop Loss: Entry + (2 × ATR_15m)
   - First Target: Entry - (2 × ATR_15m)
   - Invalidation: Entry + (3 × ATR_15m)
   - R:R Ratio: 1:2

6. **Publish alert to NATS:**
   - Subject: `agent.alerts.btc`
   - Include: timestamp, signal_type, entry_price, stop_loss, first_target, invalidation_level, atr_15m, risk_reward_ratio, timeframes data
   - Use nats_publish tool

## Important Notes
- This skill must be run within the agent's context where tools are available
- For continuous monitoring, run the agent task repeatedly or use a scheduler
- The daemon script (monitor_daemon.sh) can be used to trigger agent tasks every 60 seconds

## Pitfalls
- Ensure all 3 timeframes align before triggering alert
- Respect cooldown period to avoid alert spam
- Handle missing data gracefully (skip incomplete checks)
- ATR_15m is used for all timeframe calculations

## Verification
- Check monitor_state.json for latest state
- Check monitor.log for full history
- Verify NATS alerts are being published

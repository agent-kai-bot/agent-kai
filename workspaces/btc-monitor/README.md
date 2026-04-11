# BTC Multi-Timeframe Alignment Monitor - Setup Guide

## Overview
This workspace contains tools for continuous BTC monitoring across 15m, 1h, and 4h timeframes.

## Files
- `btc_monitor.py` - Main monitoring script (requires kai_api module)
- `monitor_daemon.py` - API-based daemon version
- `monitor.log` - Runtime log file
- `monitor_state.json` - Latest state snapshot

## How to Run

### Option 1: Run as Agent Task (Recommended)
Since the monitoring requires access to the kai_api tools, run this as an agent task:

```bash
# Start monitoring session
python3 -c "
import sys
sys.path.insert(0, '/home/atc/git/claude-local-ai-agent')
from btc_monitor import main
main()
"
```

### Option 2: Run as Background Process
```bash
cd /home/atc/git/claude-local-ai-agent/workspaces/btc-monitor
nohup python3 monitor_daemon.py > monitor.log 2>&1 &
```

### Option 3: Run via Agent
Use the agent to run monitoring checks periodically.

## Monitoring Logic

### BULLISH ALIGNMENT (all 3 timeframes)
- 15m: Price > EMA20 AND RSI(14) > 55
- 1h: Price > EMA20 AND RSI(14) > 55
- 4h: Price > EMA20 AND RSI(14) > 55

### BEARISH ALIGNMENT (all 3 timeframes)
- 15m: Price < EMA20 AND RSI(14) < 45
- 1h: Price < EMA20 AND RSI(14) < 45
- 4h: Price < EMA20 AND RSI(14) < 45

## Alert Format (NATS: agent.alerts.btc)
```json
{
  "timestamp": "2026-04-10T03:38:00Z",
  "signal_type": "bullish" | "bearish",
  "entry_price": 71892.80,
  "stop_loss": 71692.80,
  "first_target": 72092.80,
  "invalidation_level": 71592.80,
  "atr_15m": 100.00,
  "risk_reward_ratio": 2.0,
  "timeframes": {
    "15m": {"price": 71892.80, "ema20": 71800.00, "rsi14": 58.5},
    "1h": {"price": 71890.00, "ema20": 71750.00, "rsi14": 60.2},
    "4h": {"price": 71885.00, "ema20": 71700.00, "rsi14": 62.1}
  }
}
```

## Check Current Status
```bash
cat monitor_state.json
```

## View Logs
```bash
tail -f monitor.log
```

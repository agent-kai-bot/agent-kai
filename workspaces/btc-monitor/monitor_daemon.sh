#!/bin/bash
# BTC Multi-Timeframe Alignment Monitor - Continuous Monitor
# Runs continuously, checking every 60 seconds

WORKSPACE="/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor"
LOG_FILE="$WORKSPACE/monitor_daemon.log"
PID_FILE="$WORKSPACE/monitor.pid"

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p $OLD_PID > /dev/null 2>&1; then
        echo "Monitor already running with PID $OLD_PID"
        exit 1
    fi
fi

# Start the monitoring loop
echo "Starting BTC Multi-Timeframe Alignment Monitor..."
echo "Log file: $LOG_FILE"
echo "Press Ctrl+C to stop"
echo ""

# Create log file if it doesn't exist
touch "$LOG_FILE"

# Write PID
echo $$ > "$PID_FILE"

# Main monitoring loop
check_count=0
last_alert_time=0
alert_cooldown=300  # 5 minutes

while true; do
    check_count=$((check_count + 1))
    current_time=$(date +%s)
    
    echo "========================================" >> "$LOG_FILE"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Check #$check_count" >> "$LOG_FILE"
    
    # Run the monitoring check
    python3 << 'PYTHON_SCRIPT'
import json
import time
from datetime import datetime
import sys

def log(message):
    timestamp = datetime.utcnow().isoformat() + "Z"
    print(f"[{timestamp}] {message}")
    with open('/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor/monitor_daemon.log', 'a') as f:
        f.write(f"[{timestamp}] {message}\n")

log("🔍 Starting alignment check")

timeframes_data = {}
all_bullish = True
all_bearish = True
atr_15m = None

# Check each timeframe
for interval in ["15m", "1h", "4h"]:
    try:
        # Get OHLCV
        ohlcv = query_ohlcv(symbol='BTC', interval=interval, limit=50)
        if ohlcv and len(ohlcv) > 0:
            price = ohlcv[-1]['close']
        else:
            log(f"⚠️ Failed to get OHLCV for {interval}")
            continue
        
        # Get EMA20
        ema_data = calculate_indicator(symbol='BTC', indicator='EMA', period=20, interval=interval, limit=50)
        ema20 = ema_data[-1]['value'] if ema_data and len(ema_data) > 0 else price
        
        # Get RSI14
        rsi_data = calculate_indicator(symbol='BTC', indicator='RSI', period=14, interval=interval, limit=50)
        rsi14 = rsi_data[-1]['value'] if rsi_data and len(rsi_data) > 0 else 50
        
        # Get ATR for 15m
        if interval == "15m":
            atr_data = calculate_indicator(symbol='BTC', indicator='ATR', period=14, interval='15m', limit=50)
            atr_15m = atr_data[-1]['value'] if atr_data and len(atr_data) > 0 else 100
        
        timeframes_data[interval] = {
            "price": price,
            "ema20": ema20,
            "rsi14": rsi14
        }
        
        # Check conditions
        is_bullish = price > ema20 and rsi14 > 55
        is_bearish = price < ema20 and rsi14 < 45
        
        if not is_bullish:
            all_bullish = False
        if not is_bearish:
            all_bearish = False
        
        log(f"  {interval}: Price=${price:.2f}, EMA20=${ema20:.2f}, RSI={rsi14:.1f}")
        log(f"           Bullish: {is_bullish}, Bearish: {is_bearish}")
        
    except Exception as e:
        log(f"❌ Error checking {interval}: {e}")

# Save state
state = {
    "timestamp": datetime.utcnow().isoformat() + "Z",
    "timeframes": timeframes_data,
    "all_bullish": all_bullish,
    "all_bearish": all_bearish,
    "atr_15m": atr_15m
}

with open('/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor/monitor_state.json', 'w') as f:
    json.dump(state, f, indent=2)

# Check for alignment
if len(timeframes_data) < 3:
    log("⏳ Incomplete data - skipping alert")
else:
    # Read last alert time from state file
    try:
        with open('/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor/monitor_state.json', 'r') as f:
            last_state = json.load(f)
            last_alert_time = last_state.get('last_alert_time', 0)
    except:
        last_alert_time = 0
    
    current_time = time.time()
    
    if all_bullish and (current_time - last_alert_time) > 300:
        entry_price = timeframes_data["15m"]["price"]
        atr = atr_15m or 100
        stop_loss = entry_price - (2 * atr)
        first_target = entry_price + (2 * atr)
        invalidation_level = entry_price - (3 * atr)
        rr_ratio = 2.0
        
        alert = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "signal_type": "bullish",
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "first_target": round(first_target, 2),
            "invalidation_level": round(invalidation_level, 2),
            "atr_15m": round(atr, 2),
            "risk_reward_ratio": rr_ratio,
            "timeframes": {
                k: {"price": round(v["price"], 2), "ema20": round(v["ema20"], 2), "rsi14": round(v["rsi14"], 2)}
                for k, v in timeframes_data.items()
            }
        }
        
        nats_publish(subject='agent.alerts.btc', message=json.dumps(alert))
        log(f"🚨 BULLISH ALIGNMENT DETECTED!")
        log(f"  Entry: ${entry_price:.2f}, SL: ${stop_loss:.2f}, TP: ${first_target:.2f}, R:R: {rr_ratio}")
        
        # Update state with last alert time
        state['last_alert_time'] = current_time
        with open('/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor/monitor_state.json', 'w') as f:
            json.dump(state, f, indent=2)
    
    elif all_bearish and (current_time - last_alert_time) > 300:
        entry_price = timeframes_data["15m"]["price"]
        atr = atr_15m or 100
        stop_loss = entry_price + (2 * atr)
        first_target = entry_price - (2 * atr)
        invalidation_level = entry_price + (3 * atr)
        rr_ratio = 2.0
        
        alert = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "signal_type": "bearish",
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "first_target": round(first_target, 2),
            "invalidation_level": round(invalidation_level, 2),
            "atr_15m": round(atr, 2),
            "risk_reward_ratio": rr_ratio,
            "timeframes": {
                k: {"price": round(v["price"], 2), "ema20": round(v["ema20"], 2), "rsi14": round(v["rsi14"], 2)}
                for k, v in timeframes_data.items()
            }
        }
        
        nats_publish(subject='agent.alerts.btc', message=json.dumps(alert))
        log(f"🚨 BEARISH ALIGNMENT DETECTED!")
        log(f"  Entry: ${entry_price:.2f}, SL: ${stop_loss:.2f}, TP: ${first_target:.2f}, R:R: {rr_ratio}")
        
        # Update state with last alert time
        state['last_alert_time'] = current_time
        with open('/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor/monitor_state.json', 'w') as f:
            json.dump(state, f, indent=2)
    
    else:
        status = "NO ALIGNMENT" if not (all_bullish or all_bearish) else "ALIGNMENT (cooldown)"
        log(f"⏳ {status}")

log("=" * 60)
PYTHON_SCRIPT

    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Check complete, waiting 60 seconds..." >> "$LOG_FILE"
    sleep 60
done

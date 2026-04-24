#!/usr/bin/env python3
"""
BTC Multi-Timeframe Alignment Monitor - Daemon Script
This script runs as a background daemon and monitors BTC alignment.
"""

import json
import time
from datetime import datetime
import subprocess
import os
import sys

# Configuration
WORKSPACE = "/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor"
LOG_FILE = f"{WORKSPACE}/monitor.log"
STATE_FILE = f"{WORKSPACE}/monitor_state.json"
PID_FILE = f"{WORKSPACE}/monitor.pid"

# NATS configuration
NATS_SUBJECT = "agent.alerts.btc"

def log(message):
    """Log to console and file"""
    timestamp = datetime.utcnow().isoformat() + "Z"
    log_entry = f"[{timestamp}] {message}"
    print(message)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(log_entry + "\n")
    except:
        pass

def get_current_price():
    """Get current BTC price"""
    from kai_api import KaiAPI
    api = KaiAPI()
    return api.get_latest_price(symbol='BTC')

def run_check():
    """Execute a single monitoring check"""

    log("🔍 Starting alignment check")

    timeframes_data = {}
    all_bullish = True
    all_bearish = True
    atr_15m = None

    # Check each timeframe
    for interval in ["15m", "1h", "4h"]:
        try:
            # Get OHLCV
            from kai_api import KaiAPI
            api = KaiAPI()
            ohlcv = api.query_ohlcv(symbol='BTC', interval=interval, limit=50)

            if ohlcv and len(ohlcv) > 0:
                latest = ohlcv[-1]
                price = latest['close']
            else:
                log(f"⚠️ Failed to get OHLCV for {interval}")
                continue

            # Get EMA20
            ema_data = api.calculate_indicator(symbol='BTC', indicator='EMA', period=20, interval=interval, limit=50)
            ema20 = ema_data[-1]['value'] if ema_data and len(ema_data) > 0 else price

            # Get RSI14
            rsi_data = api.calculate_indicator(symbol='BTC', indicator='RSI', period=14, interval=interval, limit=50)
            rsi14 = rsi_data[-1]['value'] if rsi_data and len(rsi_data) > 0 else 50

            # Get ATR for 15m
            if interval == "15m":
                atr_data = api.calculate_indicator(symbol='BTC', indicator='ATR', period=14, interval='15m', limit=50)
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

    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

    return timeframes_data, all_bullish, all_bearish, atr_15m

def publish_alert(signal_type, entry_price, stop_loss, first_target, invalidation_level, atr_15m, timeframes_data):
    """Publish alert to NATS"""

    risk = abs(entry_price - stop_loss)
    reward = abs(first_target - entry_price)
    rr_ratio = reward / risk if risk > 0 else 0

    alert = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "signal_type": signal_type,
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "first_target": round(first_target, 2),
        "invalidation_level": round(invalidation_level, 2),
        "atr_15m": round(atr_15m, 2) if atr_15m else None,
        "risk_reward_ratio": round(rr_ratio, 2),
        "timeframes": {
            interval: {
                "price": round(data["price"], 2),
                "ema20": round(data["ema20"], 2),
                "rsi14": round(data["rsi14"], 2)
            }
            for interval, data in timeframes_data.items()
        }
    }

    # Publish to NATS
    from kai_api import KaiAPI
    api = KaiAPI()
    api.nats_publish(subject=NATS_SUBJECT, message=json.dumps(alert))

    log(f"\n🚨 {signal_type.upper()} ALIGNMENT DETECTED!")
    log(f"Entry: ${alert['entry_price']:.2f}")
    log(f"Stop Loss: ${alert['stop_loss']:.2f}")
    log(f"First Target: ${alert['first_target']:.2f}")
    log(f"R:R Ratio: {alert['risk_reward_ratio']:.2f}")
    log(f"\nTimeframe Summary:")
    for interval, data in alert['timeframes'].items():
        log(f"  {interval}: Price=${data['price']:.2f}, EMA20=${data['ema20']:.2f}, RSI={data['rsi14']:.1f}")

    return alert

def main():
    """Main monitoring loop"""

    log("=" * 60)
    log("🔍 Starting BTC Multi-Timeframe Alignment Monitor")
    log(f"Workspace: {WORKSPACE}")
    log(f"Log file: {LOG_FILE}")
    log("=" * 60)

    check_count = 0
    last_alert_time = 0
    alert_cooldown = 300  # 5 minutes between alerts

    while True:
        check_count += 1
        current_time = time.time()

        try:
            # Run check
            timeframes_data, all_bullish, all_bearish, atr_15m = run_check()

            # Only proceed if we have data for all timeframes
            if len(timeframes_data) < 3:
                log(f"⏳ Check {check_count}: Incomplete data")
                time.sleep(60)
                continue

            # Check for bullish alignment
            if all_bullish and (current_time - last_alert_time) > alert_cooldown:
                entry_price = timeframes_data["15m"]["price"]
                atr = atr_15m if atr_15m else 100

                stop_loss = entry_price - (2 * atr)
                first_target = entry_price + (2 * atr)
                invalidation_level = entry_price - (3 * atr)

                publish_alert(
                    signal_type="bullish",
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    first_target=first_target,
                    invalidation_level=invalidation_level,
                    atr_15m=atr,
                    timeframes_data=timeframes_data
                )

                last_alert_time = current_time

            # Check for bearish alignment
            elif all_bearish and (current_time - last_alert_time) > alert_cooldown:
                entry_price = timeframes_data["15m"]["price"]
                atr = atr_15m if atr_15m else 100

                stop_loss = entry_price + (2 * atr)
                first_target = entry_price - (2 * atr)
                invalidation_level = entry_price + (3 * atr)

                publish_alert(
                    signal_type="bearish",
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    first_target=first_target,
                    invalidation_level=invalidation_level,
                    atr_15m=atr,
                    timeframes_data=timeframes_data
                )

                last_alert_time = current_time

            else:
                status = "NO ALIGNMENT"
                if all_bullish or all_bearish:
                    status = "ALIGNMENT DETECTED (cooldown active)"

                log(f"\n⏳ Check {check_count}: {status}")
                log(f"  Bullish: {all_bullish}, Bearish: {all_bearish}")

            log(f"\n⏰ Next check in 60 seconds...")
            time.sleep(60)

        except KeyboardInterrupt:
            log("\n\n👋 Monitor stopped by user")
            break
        except Exception as e:
            log(f"❌ Error in monitoring loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()

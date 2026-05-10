#!/usr/bin/env python3
"""BTC Multi-Timeframe Alignment Monitor.

Pulls OHLCV from the local kai daemon's /api/market/ohlcv endpoint
(coinbase source), computes EMA20 + RSI14 + ATR14 with pandas_ta,
checks 15m/1h/4h alignment, and publishes alerts to NATS subject
agent.alerts.btc. Cooldown 5 min between alerts.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import nats
import pandas as pd
import pandas_ta as ta
import requests

WORKSPACE = Path("/home/atc/git/claude-local-ai-agent/workspaces/btc-monitor")
LOG_FILE = WORKSPACE / "monitor.log"
STATE_FILE = WORKSPACE / "monitor_state.json"
TOKEN_FILE = Path("/home/atc/git/claude-local-ai-agent/workspaces/daemon-token.txt")

DAEMON_URL = "http://127.0.0.1:18789"
NATS_URL = "nats://127.0.0.1:4222"
NATS_SUBJECT = "agent.alerts.btc"
SYMBOL = "BTC"
INTERVAL_SOURCES = {"15m": "coinbase", "1h": "coinbase", "4h": "kai-api"}
CHECK_INTERVAL_S = 60
ALERT_COOLDOWN_S = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(message: str) -> None:
    entry = f"[{_now_iso()}] {message}"
    print(entry, flush=True)
    try:
        with LOG_FILE.open("a") as f:
            f.write(entry + "\n")
    except OSError:
        pass


def _bearer() -> str:
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""


def fetch_ohlcv(interval: str, limit: int = 100) -> pd.DataFrame:
    url = f"{DAEMON_URL}/api/market/ohlcv"
    params = {"symbol": SYMBOL, "interval": interval,
              "source": INTERVAL_SOURCES[interval], "limit": limit}
    headers = {}
    tok = _bearer()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    bars = r.json().get("bars", [])
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.dropna(subset=["close"]).reset_index(drop=True)


def compute_indicators(df: pd.DataFrame, want_atr: bool) -> dict:
    if len(df) < 20:
        raise ValueError(f"insufficient bars: {len(df)}")
    ema20 = ta.ema(df["close"], length=20).iloc[-1]
    rsi14 = ta.rsi(df["close"], length=14).iloc[-1]
    out = {
        "price": float(df["close"].iloc[-1]),
        "ema20": float(ema20),
        "rsi14": float(rsi14),
    }
    if want_atr:
        atr14 = ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1]
        out["atr14"] = float(atr14)
    return out


def run_check() -> tuple[dict, bool, bool, float | None]:
    log("Starting alignment check")
    timeframes_data: dict[str, dict] = {}
    atr_15m: float | None = None
    all_bullish = True
    all_bearish = True

    for interval in INTERVAL_SOURCES:
        try:
            df = fetch_ohlcv(interval, limit=100)
            if df.empty:
                log(f"  {interval}: no bars returned")
                all_bullish = all_bearish = False
                continue
            ind = compute_indicators(df, want_atr=(interval == "15m"))
            if interval == "15m":
                atr_15m = ind.get("atr14")
            timeframes_data[interval] = {
                "price": ind["price"],
                "ema20": ind["ema20"],
                "rsi14": ind["rsi14"],
            }
            is_bullish = ind["price"] > ind["ema20"] and ind["rsi14"] > 55
            is_bearish = ind["price"] < ind["ema20"] and ind["rsi14"] < 45
            if not is_bullish:
                all_bullish = False
            if not is_bearish:
                all_bearish = False
            log(f"  {interval}: price=${ind['price']:.2f} ema20=${ind['ema20']:.2f} "
                f"rsi14={ind['rsi14']:.1f} bull={is_bullish} bear={is_bearish}")
        except Exception as e:
            log(f"  {interval}: ERR {type(e).__name__}: {e}")
            all_bullish = all_bearish = False

    state = {
        "timestamp": _now_iso(),
        "timeframes": timeframes_data,
        "all_bullish": all_bullish,
        "all_bearish": all_bearish,
        "atr_15m": atr_15m,
    }
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f"state write failed: {e}")

    return timeframes_data, all_bullish, all_bearish, atr_15m


def build_alert(signal_type: str, tf_data: dict, atr_15m: float | None) -> dict:
    entry = tf_data["15m"]["price"]
    atr = atr_15m or 100.0
    if signal_type == "bullish":
        stop = entry - 2 * atr
        target = entry + 2 * atr
        invalidation = entry - 3 * atr
    else:
        stop = entry + 2 * atr
        target = entry - 2 * atr
        invalidation = entry + 3 * atr
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return {
        "timestamp": _now_iso(),
        "signal_type": signal_type,
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2),
        "first_target": round(target, 2),
        "invalidation_level": round(invalidation, 2),
        "atr_15m": round(atr, 2),
        "risk_reward_ratio": round(reward / risk, 2) if risk > 0 else 0,
        "timeframes": {
            tf: {"price": round(d["price"], 2), "ema20": round(d["ema20"], 2),
                 "rsi14": round(d["rsi14"], 2)}
            for tf, d in tf_data.items()
        },
    }


async def publish_alert(alert: dict) -> None:
    nc = await nats.connect(NATS_URL, connect_timeout=5)
    try:
        await nc.publish(NATS_SUBJECT, json.dumps(alert).encode())
        await nc.flush(timeout=3)
    finally:
        await nc.close()


def main() -> None:
    log("=" * 60)
    log("BTC Multi-Timeframe Alignment Monitor (15m/1h/4h, coinbase via daemon)")
    log("=" * 60)
    check_count = 0
    last_alert_time = 0.0
    while True:
        check_count += 1
        now = time.time()
        try:
            tf_data, all_bull, all_bear, atr_15m = run_check()
            if len(tf_data) < 3:
                log(f"check {check_count}: incomplete data ({len(tf_data)}/3 tfs)")
            elif (all_bull or all_bear) and (now - last_alert_time) > ALERT_COOLDOWN_S:
                signal = "bullish" if all_bull else "bearish"
                alert = build_alert(signal, tf_data, atr_15m)
                asyncio.run(publish_alert(alert))
                log(f"ALERT PUBLISHED: {signal.upper()} entry=${alert['entry_price']:.2f} "
                    f"sl=${alert['stop_loss']:.2f} t1=${alert['first_target']:.2f} "
                    f"rr={alert['risk_reward_ratio']:.2f}")
                last_alert_time = now
            elif all_bull or all_bear:
                log(f"check {check_count}: alignment ({'bull' if all_bull else 'bear'}) "
                    f"- in cooldown")
            else:
                log(f"check {check_count}: no alignment")
        except KeyboardInterrupt:
            log("stopped by user")
            break
        except Exception as e:
            log(f"loop error: {type(e).__name__}: {e}")
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()

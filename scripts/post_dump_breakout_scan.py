#!/usr/bin/env python3
"""Post-dump breakout scanner, backtester, and hyperparameter optimizer.

This script loads 1m OHLCV from Timescale/Postgres, optionally resamples it with
pandas, computes vectorized TA features, generates post-dump breakout signals,
and can:

1. scan:     show the latest signal candidates
2. backtest: evaluate a concrete parameter set over history
3. optimize: grid/random search parameter combinations and rank them

Pattern intent
--------------
A bullish post-dump breakout attempts to capture:
- a sharp selloff bar into / through the lower Bollinger area
- abnormally high sell volume on the dump
- fading downside pressure over the next few bars
- buyer return via reclaim of local structure / EMA / BB mid with improving RSI
  and MACD histogram

The implementation avoids lookahead by:
- marking the dump on a prior bar only
- requiring fade confirmation on subsequent bars
- entering only on the current bar when reclaim conditions are met
- evaluating exits strictly forward in time

Examples
--------
Scan latest candidates:
python scripts/post_dump_breakout_scan.py \
  --mode scan --db-url "$KAI_TIMESERIES_DB_URL" --symbols BTC ETH SOL --timeframe 5min

Backtest a single configuration:
python scripts/post_dump_breakout_scan.py \
  --mode backtest --db-url "$KAI_TIMESERIES_DB_URL" --symbols BTC ETH SOL \
  --timeframe 5min --lookback-hours 24*30 --tp-pct 3 --sl-pct 1.2

Optimize over a search space:
python scripts/post_dump_breakout_scan.py \
  --mode optimize --db-url "$KAI_TIMESERIES_DB_URL" --symbols BTC ETH SOL \
  --timeframe 5min --n-trials 200 --search random --top-k 20 --json
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from agent.data_sources.ohlcv_provider import AgentKaiConfig, TimeseriesConfig, load_ohlcv, resample_ohlcv as provider_resample_ohlcv

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["scan", "backtest", "optimize", "diagnose", "sweep-dumps"], default="scan")

    # Data source / schema
    p.add_argument("--source", choices=["auto", "timeseries", "agent-kai"], default="auto")
    p.add_argument("--db-url", default=os.getenv("KAI_TIMESERIES_DB_URL", os.getenv("DATABASE_URL", "")))
    p.add_argument("--table", default=os.getenv("KAI_OHLCV_TABLE", "ohlcv_1m"))
    p.add_argument("--ts-col", default=os.getenv("KAI_OHLCV_TS_COL", "ts"))
    p.add_argument("--symbol-col", default=os.getenv("KAI_OHLCV_SYMBOL_COL", "symbol"))
    p.add_argument("--open-col", default=os.getenv("KAI_OHLCV_OPEN_COL", "open"))
    p.add_argument("--high-col", default=os.getenv("KAI_OHLCV_HIGH_COL", "high"))
    p.add_argument("--low-col", default=os.getenv("KAI_OHLCV_LOW_COL", "low"))
    p.add_argument("--close-col", default=os.getenv("KAI_OHLCV_CLOSE_COL", "close"))
    p.add_argument("--volume-col", default=os.getenv("KAI_OHLCV_VOLUME_COL", "volume"))
    p.add_argument("--agent-kai-api-key", default=os.getenv("AGENT_KAI_API_KEY", ""))
    p.add_argument("--agent-kai-base-url", default=os.getenv("AGENT_KAI_BASE_URL", "https://agent-k.ai"))
    p.add_argument("--symbols", nargs="*", default=[])
    p.add_argument("--symbols-file", default="", help="Optional newline-delimited file of symbols for broad sweeps")
    p.add_argument("--lookback-hours", type=int, default=72)
    p.add_argument("--end-hours-ago", type=int, default=0, help="End the analysis window this many hours before now; useful for simple OOS splits")
    p.add_argument("--timeframe", default="5min", help="Pandas resample rule, e.g. 1min, 5min, 15min, 1h")
    p.add_argument("--sweep-top-k", type=int, default=50, help="Top symbols/events to return in sweep-dumps mode")
    p.add_argument("--forward-bars", nargs="*", type=int, default=[3, 6, 12], help="Forward return horizons for dump sweeps")

    # Feature windows
    p.add_argument("--bb-period", type=int, default=20)
    p.add_argument("--bb-std", type=float, default=2.0)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--ema-fast", type=int, default=9)
    p.add_argument("--ema-slow", type=int, default=21)
    p.add_argument("--vol-period", type=int, default=20)
    p.add_argument("--trend-period", type=int, default=50)
    p.add_argument("--breakout-lookback", type=int, default=5)

    # Signal thresholds
    p.add_argument("--fade-bars", type=int, default=3)
    p.add_argument("--max-recovery-bars", type=int, default=8)
    p.add_argument("--min-volume-ratio", type=float, default=2.0)
    p.add_argument("--min-vol-z", type=float, default=1.5)
    p.add_argument("--min-dump-return", type=float, default=0.012)
    p.add_argument("--max-dist-from-lower-band", type=float, default=0.003)
    p.add_argument("--min-lower-wick-frac", type=float, default=0.10)
    p.add_argument("--max-followthrough-return", type=float, default=0.004)
    p.add_argument("--min-rsi-rebound", type=float, default=8.0)
    p.add_argument("--min-macd-hist-improve", type=float, default=0.0)
    p.add_argument("--require-close-above-bb-mid", action="store_true")
    p.add_argument("--require-close-above-ema-fast", action="store_true")
    p.add_argument("--require-trend-filter", action="store_true")
    p.add_argument("--trend-filter-buffer", type=float, default=0.0)
    p.add_argument("--min-score", type=float, default=5.0)

    # Backtest exits
    p.add_argument("--entry-delay-bars", type=int, default=1, help="Enter on next bar open by default")
    p.add_argument("--max-hold-bars", type=int, default=24)
    p.add_argument("--sl-pct", type=float, default=1.2)
    p.add_argument("--tp-pct", type=float, default=3.0)
    p.add_argument("--trail-ema-fast", action="store_true")
    p.add_argument("--exit-on-rsi", type=float, default=68.0)
    p.add_argument("--cooldown-bars", type=int, default=5)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--allow-overlapping", action="store_true")

    # Search/optimization
    p.add_argument("--search", choices=["grid", "random"], default="random")
    p.add_argument("--n-trials", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--min-trades", type=int, default=5)
    p.add_argument("--objective", choices=["sharpe", "return", "expectancy", "profit_factor"], default="sharpe")

    # Output
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", default="", help="Optional path to save JSON/CSV depending on mode")
    return p.parse_args()


@dataclass(frozen=True)
class StrategyParams:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    vol_period: int = 20
    trend_period: int = 50
    breakout_lookback: int = 5
    fade_bars: int = 3
    max_recovery_bars: int = 8
    min_volume_ratio: float = 2.0
    min_vol_z: float = 1.5
    min_dump_return: float = 0.012
    max_dist_from_lower_band: float = 0.003
    min_lower_wick_frac: float = 0.10
    max_followthrough_return: float = 0.004
    min_rsi_rebound: float = 8.0
    min_macd_hist_improve: float = 0.0
    require_close_above_bb_mid: bool = False
    require_close_above_ema_fast: bool = False
    require_trend_filter: bool = False
    trend_filter_buffer: float = 0.0
    min_score: float = 5.0
    entry_delay_bars: int = 1
    max_hold_bars: int = 24
    sl_pct: float = 1.2
    tp_pct: float = 3.0
    trail_ema_fast: bool = False
    exit_on_rsi: float = 68.0
    cooldown_bars: int = 5
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    allow_overlapping: bool = False


def params_from_args(args: argparse.Namespace) -> StrategyParams:
    return StrategyParams(
        bb_period=args.bb_period,
        bb_std=args.bb_std,
        rsi_period=args.rsi_period,
        ema_fast=args.ema_fast,
        ema_slow=args.ema_slow,
        vol_period=args.vol_period,
        trend_period=args.trend_period,
        breakout_lookback=args.breakout_lookback,
        fade_bars=args.fade_bars,
        max_recovery_bars=args.max_recovery_bars,
        min_volume_ratio=args.min_volume_ratio,
        min_vol_z=args.min_vol_z,
        min_dump_return=args.min_dump_return,
        max_dist_from_lower_band=args.max_dist_from_lower_band,
        min_lower_wick_frac=args.min_lower_wick_frac,
        max_followthrough_return=args.max_followthrough_return,
        min_rsi_rebound=args.min_rsi_rebound,
        min_macd_hist_improve=args.min_macd_hist_improve,
        require_close_above_bb_mid=args.require_close_above_bb_mid,
        require_close_above_ema_fast=args.require_close_above_ema_fast,
        require_trend_filter=args.require_trend_filter,
        trend_filter_buffer=args.trend_filter_buffer,
        min_score=args.min_score,
        entry_delay_bars=args.entry_delay_bars,
        max_hold_bars=args.max_hold_bars,
        sl_pct=args.sl_pct,
        tp_pct=args.tp_pct,
        trail_ema_fast=args.trail_ema_fast,
        exit_on_rsi=args.exit_on_rsi,
        cooldown_bars=args.cooldown_bars,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        allow_overlapping=args.allow_overlapping,
    )


def _load_symbols_from_args(args: argparse.Namespace) -> list[str]:
    symbols = list(args.symbols or [])
    if args.symbols_file:
        path = Path(args.symbols_file)
        file_symbols = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]
        symbols.extend(file_symbols)
    # preserve order while deduping
    deduped: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        s = symbol.strip().upper()
        if s and s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def load_1m_ohlcv(args: argparse.Namespace) -> pd.DataFrame:
    symbols = _load_symbols_from_args(args)
    timeseries = TimeseriesConfig(
        db_url=args.db_url,
        table=args.table,
        ts_col=args.ts_col,
        symbol_col=args.symbol_col,
        open_col=args.open_col,
        high_col=args.high_col,
        low_col=args.low_col,
        close_col=args.close_col,
        volume_col=args.volume_col,
    )
    agent_kai = AgentKaiConfig(
        api_key=args.agent_kai_api_key,
        base_url=args.agent_kai_base_url,
    )
    df = load_ohlcv(
        symbols=symbols,
        timeframe="1m",
        lookback_hours=args.lookback_hours + max(int(args.end_hours_ago), 0),
        source=args.source,
        timeseries=timeseries,
        agent_kai=agent_kai,
    )
    if args.end_hours_ago > 0 and not df.empty:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=args.end_hours_ago)
        start = cutoff - pd.Timedelta(hours=args.lookback_hours)
        df = df[(df["ts"] >= start) & (df["ts"] < cutoff)].copy()
    return df



def resample_ohlcv(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe.lower() in {"1m", "1min", "min", "1t"}:
        return df_1m.copy().sort_values(["symbol", "ts"]).reset_index(drop=True)
    return provider_resample_ohlcv(df_1m, timeframe)


# ---------- indicators ----------


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))



def add_features(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    g = out.groupby("symbol", group_keys=False)

    out["ret_1"] = g["close"].pct_change()
    out["ret_fwd_1"] = g["close"].shift(-1) / out["close"] - 1
    out["range"] = (out["high"] - out["low"]).replace(0, np.nan)
    out["body"] = out["close"] - out["open"]
    out["body_pct"] = out["body"] / out["open"].replace(0, np.nan)
    out["lower_wick"] = np.minimum(out["open"], out["close"]) - out["low"]
    out["upper_wick"] = out["high"] - np.maximum(out["open"], out["close"])
    out["lower_wick_frac"] = out["lower_wick"] / out["range"]
    out["upper_wick_frac"] = out["upper_wick"] / out["range"]
    out["close_loc"] = (out["close"] - out["low"]) / out["range"]

    out["bb_mid"] = g["close"].transform(lambda s: s.rolling(p.bb_period).mean())
    bb_std = g["close"].transform(lambda s: s.rolling(p.bb_period).std())
    out["bb_upper"] = out["bb_mid"] + p.bb_std * bb_std
    out["bb_lower"] = out["bb_mid"] - p.bb_std * bb_std
    out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"].replace(0, np.nan)
    out["dist_from_bb_lower"] = (out["close"] - out["bb_lower"]) / out["close"].replace(0, np.nan)

    out["rsi"] = g["close"].transform(lambda s: rsi_wilder(s, p.rsi_period))
    out["rsi_delta"] = g["rsi"].diff()

    out["ema_fast"] = g["close"].transform(lambda s: s.ewm(span=p.ema_fast, adjust=False).mean())
    out["ema_slow"] = g["close"].transform(lambda s: s.ewm(span=p.ema_slow, adjust=False).mean())
    out["ema_trend"] = g["close"].transform(lambda s: s.ewm(span=p.trend_period, adjust=False).mean())

    ema12 = g["close"].transform(lambda s: s.ewm(span=12, adjust=False).mean())
    ema26 = g["close"].transform(lambda s: s.ewm(span=26, adjust=False).mean())
    out["macd"] = ema12 - ema26
    out["macd_signal"] = g["macd"].transform(lambda s: s.ewm(span=9, adjust=False).mean())
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    out["macd_hist_delta"] = g["macd_hist"].diff()

    out["vol_ma"] = g["volume"].transform(lambda s: s.rolling(p.vol_period).mean())
    out["vol_std"] = g["volume"].transform(lambda s: s.rolling(p.vol_period).std())
    out["vol_ratio"] = out["volume"] / out["vol_ma"].replace(0, np.nan)
    out["vol_z"] = (out["volume"] - out["vol_ma"]) / out["vol_std"].replace(0, np.nan)
    out["up_vol"] = np.where(out["body"] > 0, out["volume"], 0.0)
    out["down_vol"] = np.where(out["body"] < 0, out["volume"], 0.0)

    out["prev_breakout_high"] = g["high"].transform(lambda s: s.shift(1).rolling(p.breakout_lookback).max())
    out["prev_breakout_low"] = g["low"].transform(lambda s: s.shift(1).rolling(p.breakout_lookback).min())

    return out


# ---------- signal generation ----------


def generate_signals(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    g = out.groupby("symbol", group_keys=False)
    fade_bars = p.fade_bars

    dump_body_ok = out["body_pct"] <= -abs(p.min_dump_return)
    dump_vol_ratio_ok = out["vol_ratio"] >= p.min_volume_ratio
    dump_vol_z_ok = out["vol_z"] >= p.min_vol_z
    dump_bb_ok = out["dist_from_bb_lower"] <= p.max_dist_from_lower_band
    dump_wick_ok = out["lower_wick_frac"] >= p.min_lower_wick_frac

    dump_bar = dump_body_ok & dump_vol_ratio_ok & dump_vol_z_ok & dump_bb_ok & dump_wick_ok
    out["dump_body_ok"] = dump_body_ok.astype(int)
    out["dump_vol_ratio_ok"] = dump_vol_ratio_ok.astype(int)
    out["dump_vol_z_ok"] = dump_vol_z_ok.astype(int)
    out["dump_bb_ok"] = dump_bb_ok.astype(int)
    out["dump_wick_ok"] = dump_wick_ok.astype(int)
    out["dump_bar"] = dump_bar.astype(int)

    # Prior dump stats shifted forward so current bar can react to them without leakage.
    out["dump_recent"] = g["dump_bar"].transform(
        lambda s: s.shift(1).rolling(p.max_recovery_bars, min_periods=1).max()
    ).fillna(0).astype(int)
    out["dump_body_pct"] = np.where(dump_bar, out["body_pct"], np.nan)
    out["dump_rsi"] = np.where(dump_bar, out["rsi"], np.nan)
    out["dump_macd_hist"] = np.where(dump_bar, out["macd_hist"], np.nan)
    out["dump_low"] = np.where(dump_bar, out["low"], np.nan)
    out["dump_vol_ratio"] = np.where(dump_bar, out["vol_ratio"], np.nan)

    for col in ["dump_body_pct", "dump_rsi", "dump_macd_hist", "dump_low", "dump_vol_ratio"]:
        out[f"last_{col}"] = g[col].transform(lambda s: s.shift(1).ffill())

    out["bars_since_dump"] = g["dump_bar"].transform(
        lambda s: s.shift(1).rolling(p.max_recovery_bars, min_periods=1).apply(
            lambda x: len(x) - 1 - np.argmax(x[::-1]) if np.any(x == 1) else np.nan,
            raw=True,
        )
    )

    fade_return_ok = g["ret_1"].transform(
        lambda s: s.shift(1).rolling(fade_bars, min_periods=fade_bars).min()
    ) >= -abs(p.max_followthrough_return)
    fade_vol_ok = g["vol_ratio"].transform(
        lambda s: s.shift(1).rolling(fade_bars, min_periods=fade_bars).mean()
    ) <= np.maximum(p.min_volume_ratio * 0.9, 1.0)
    low_holds = out["low"] >= out["last_dump_low"].fillna(-np.inf)

    reclaim_structure = out["close"] > out["prev_breakout_high"]
    reclaim_ema = out["close"] > out["ema_fast"] if p.require_close_above_ema_fast else True
    reclaim_bb_mid = out["close"] > out["bb_mid"] if p.require_close_above_bb_mid else True
    trend_ok = (
        out["close"] >= out["ema_trend"] * (1 + p.trend_filter_buffer)
        if p.require_trend_filter
        else True
    )
    momentum_ok = (
        (out["rsi"] - out["last_dump_rsi"] >= p.min_rsi_rebound)
        & (out["macd_hist"] - out["last_dump_macd_hist"] >= p.min_macd_hist_improve)
        & (out["rsi_delta"] > 0)
        & (out["macd_hist_delta"] >= 0)
    )

    out["fade_return_ok"] = fade_return_ok.fillna(False).astype(int)
    out["fade_vol_ok"] = fade_vol_ok.fillna(False).astype(int)
    out["low_holds_ok"] = low_holds.fillna(False).astype(int)
    out["reclaim_structure_ok"] = reclaim_structure.fillna(False).astype(int)
    out["momentum_ok"] = momentum_ok.fillna(False).astype(int)

    setup_window = (out["dump_recent"] == 1) & out["bars_since_dump"].between(1, p.max_recovery_bars)
    setup = (
        setup_window
        & fade_return_ok.fillna(False)
        & fade_vol_ok.fillna(False)
        & low_holds.fillna(False)
        & reclaim_structure.fillna(False)
        & momentum_ok.fillna(False)
    )

    if isinstance(reclaim_ema, pd.Series):
        out["reclaim_ema_ok"] = reclaim_ema.fillna(False).astype(int)
        setup &= reclaim_ema.fillna(False)
    else:
        out["reclaim_ema_ok"] = int(bool(reclaim_ema))
    if isinstance(reclaim_bb_mid, pd.Series):
        out["reclaim_bb_mid_ok"] = reclaim_bb_mid.fillna(False).astype(int)
        setup &= reclaim_bb_mid.fillna(False)
    else:
        out["reclaim_bb_mid_ok"] = int(bool(reclaim_bb_mid))
    if isinstance(trend_ok, pd.Series):
        out["trend_ok"] = trend_ok.fillna(False).astype(int)
        setup &= trend_ok.fillna(False)
    else:
        out["trend_ok"] = int(bool(trend_ok))

    score = pd.Series(0.0, index=out.index)
    score += np.clip(out["last_dump_vol_ratio"].fillna(0) - 1.0, 0, 3)
    score += np.clip((-out["last_dump_body_pct"].fillna(0)) / max(p.min_dump_return, 1e-9), 0, 3)
    score += np.clip((out["rsi"] - out["last_dump_rsi"].fillna(out["rsi"])) / max(p.min_rsi_rebound, 1e-9), 0, 3)
    score += np.clip((out["macd_hist"] - out["last_dump_macd_hist"].fillna(out["macd_hist"])) * 100, 0, 3)
    score += np.where(out["close"] > out["bb_mid"], 1.0, 0.0)
    score += np.where(out["close"] > out["ema_fast"], 1.0, 0.0)
    out["signal_score"] = score
    out["setup_window"] = setup_window.astype(int)
    out["setup_all"] = setup.astype(int)
    out["signal_long"] = (setup & (out["signal_score"] >= p.min_score)).astype(int)

    return out


# ---------- backtest ----------


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())



def backtest_symbol(df: pd.DataFrame, p: StrategyParams) -> tuple[list[dict[str, Any]], pd.Series]:
    trades: list[dict[str, Any]] = []
    equity_curve = []
    equity = 1.0
    last_exit_idx = -10**9
    in_pos = False
    entry_idx = -1
    entry_price = 0.0
    stop_price = np.nan
    take_price = np.nan
    qty = 1.0

    idx = df.index.to_list()
    for i in range(len(df)):
        row = df.iloc[i]
        ts = row["ts"]

        if not in_pos:
            equity_curve.append({"ts": ts, "equity": equity})
            if row["signal_long"] != 1:
                continue
            if (not p.allow_overlapping) and (i - last_exit_idx <= p.cooldown_bars):
                continue
            entry_i = i + p.entry_delay_bars
            if entry_i >= len(df):
                continue
            entry_row = df.iloc[entry_i]
            slip = p.slippage_bps / 10000.0
            entry_price = float(entry_row["open"] * (1 + slip))
            stop_price = entry_price * (1 - p.sl_pct / 100.0)
            take_price = entry_price * (1 + p.tp_pct / 100.0)
            entry_idx = entry_i
            in_pos = True
            continue

        current = df.iloc[i]
        exit_reason = None
        exit_price = None

        if i <= entry_idx:
            equity_curve.append({"ts": ts, "equity": equity})
            continue

        if current["low"] <= stop_price:
            exit_reason = "stop"
            exit_price = stop_price * (1 - p.slippage_bps / 10000.0)
        elif current["high"] >= take_price:
            exit_reason = "target"
            exit_price = take_price * (1 - p.slippage_bps / 10000.0)
        elif p.trail_ema_fast and current["close"] < current["ema_fast"]:
            exit_reason = "trail_ema"
            exit_price = current["close"] * (1 - p.slippage_bps / 10000.0)
        elif current["rsi"] >= p.exit_on_rsi:
            exit_reason = "rsi_exit"
            exit_price = current["close"] * (1 - p.slippage_bps / 10000.0)
        elif i - entry_idx >= p.max_hold_bars:
            exit_reason = "time"
            exit_price = current["close"] * (1 - p.slippage_bps / 10000.0)

        if exit_reason is not None:
            cost = 2 * (p.fee_bps / 10000.0)
            gross = exit_price / entry_price - 1.0
            net = gross - cost
            equity *= 1 + net
            trades.append(
                {
                    "symbol": current["symbol"],
                    "entry_ts": df.iloc[entry_idx]["ts"],
                    "exit_ts": current["ts"],
                    "entry_price": round(entry_price, 8),
                    "exit_price": round(float(exit_price), 8),
                    "bars_held": int(i - entry_idx),
                    "return_pct": net * 100,
                    "gross_return_pct": gross * 100,
                    "exit_reason": exit_reason,
                    "signal_score": float(df.iloc[entry_idx - p.entry_delay_bars]["signal_score"]) if entry_idx - p.entry_delay_bars >= 0 else np.nan,
                }
            )
            in_pos = False
            last_exit_idx = i
            entry_idx = -1
            entry_price = 0.0
            stop_price = np.nan
            take_price = np.nan

        equity_curve.append({"ts": ts, "equity": equity})

    return trades, pd.DataFrame(equity_curve).drop_duplicates("ts").set_index("ts")["equity"]



def summarize_backtest(signals_df: pd.DataFrame, p: StrategyParams) -> dict[str, Any]:
    all_trades: list[dict[str, Any]] = []
    equities = []
    per_symbol = {}

    for symbol, sdf in signals_df.groupby("symbol", sort=True):
        trades, equity = backtest_symbol(sdf.reset_index(drop=True), p)
        all_trades.extend(trades)
        if not equity.empty:
            tmp = equity.rename(symbol)
            equities.append(tmp)
        per_symbol[symbol] = {
            "trades": len(trades),
            "avg_trade_pct": float(np.mean([t["return_pct"] for t in trades])) if trades else 0.0,
        }

    trades_df = pd.DataFrame(all_trades)
    if equities:
        eq = pd.concat(equities, axis=1).ffill().fillna(1.0)
        portfolio_equity = eq.mean(axis=1)
    else:
        portfolio_equity = pd.Series(dtype=float)

    if trades_df.empty:
        return {
            "params": strategy_params_to_dict(p),
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_trade_pct": 0.0,
            "median_trade_pct": 0.0,
            "profit_factor": 0.0,
            "expectancy_pct": 0.0,
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "per_symbol": per_symbol,
            "trades": [],
        }

    rets = trades_df["return_pct"] / 100.0
    wins = rets[rets > 0]
    losses = rets[rets < 0]
    profit_factor = float(wins.sum() / abs(losses.sum())) if not losses.empty and abs(losses.sum()) > 0 else float("inf")
    expectancy = float(rets.mean() * 100)
    sharpe = float((rets.mean() / rets.std()) * math.sqrt(len(rets))) if len(rets) > 1 and rets.std() > 0 else 0.0
    total_return = float((1 + rets).prod() - 1) * 100
    mdd = _max_drawdown(portfolio_equity) * 100 if not portfolio_equity.empty else 0.0

    return {
        "params": strategy_params_to_dict(p),
        "num_trades": int(len(trades_df)),
        "win_rate": float((rets > 0).mean() * 100),
        "avg_trade_pct": float(rets.mean() * 100),
        "median_trade_pct": float(rets.median() * 100),
        "profit_factor": profit_factor,
        "expectancy_pct": expectancy,
        "total_return_pct": total_return,
        "sharpe": sharpe,
        "max_drawdown_pct": float(mdd),
        "per_symbol": per_symbol,
        "trades": trades_df.to_dict(orient="records"),
    }


# ---------- optimization ----------


def strategy_params_to_dict(p: StrategyParams) -> dict[str, Any]:
    return asdict(p)



def params_replace(p: StrategyParams, updates: dict[str, Any]) -> StrategyParams:
    d = strategy_params_to_dict(p)
    d.update(updates)
    return StrategyParams(
        bb_period=d["bb_period"], bb_std=d["bb_std"], rsi_period=d["rsi_period"], ema_fast=d["ema_fast"],
        ema_slow=d["ema_slow"], vol_period=d["vol_period"], trend_period=d["trend_period"],
        breakout_lookback=d["breakout_lookback"], fade_bars=d["fade_bars"],
        max_recovery_bars=d["max_recovery_bars"], min_volume_ratio=d["min_volume_ratio"], min_vol_z=d["min_vol_z"],
        min_dump_return=d["min_dump_return"], max_dist_from_lower_band=d["max_dist_from_lower_band"],
        min_lower_wick_frac=d["min_lower_wick_frac"], max_followthrough_return=d["max_followthrough_return"],
        min_rsi_rebound=d["min_rsi_rebound"], min_macd_hist_improve=d["min_macd_hist_improve"],
        require_close_above_bb_mid=d["require_close_above_bb_mid"], require_close_above_ema_fast=d["require_close_above_ema_fast"],
        require_trend_filter=d["require_trend_filter"], trend_filter_buffer=d["trend_filter_buffer"], min_score=d["min_score"],
        entry_delay_bars=d["entry_delay_bars"], max_hold_bars=d["max_hold_bars"], sl_pct=d["sl_pct"], tp_pct=d["tp_pct"],
        trail_ema_fast=d["trail_ema_fast"], exit_on_rsi=d["exit_on_rsi"], cooldown_bars=d["cooldown_bars"],
        fee_bps=d["fee_bps"], slippage_bps=d["slippage_bps"], allow_overlapping=d["allow_overlapping"],
    )



def build_search_space(base: StrategyParams) -> dict[str, list[Any]]:
    return {
        "min_volume_ratio": [1.2, 1.5, 2.0, 2.5, 3.0],
        "min_vol_z": [0.5, 1.0, 1.5, 2.0],
        "min_dump_return": [0.0015, 0.0025, 0.004, 0.006, 0.008, 0.012],
        "max_dist_from_lower_band": [0.0015, 0.003, 0.005, 0.008],
        "min_lower_wick_frac": [0.02, 0.05, 0.10, 0.20],
        "max_followthrough_return": [0.002, 0.004, 0.006, 0.008],
        "fade_bars": [2, 3, 4],
        "max_recovery_bars": [4, 6, 8, 10],
        "breakout_lookback": [3, 5, 8],
        "min_rsi_rebound": [2.0, 4.0, 8.0, 12.0],
        "min_macd_hist_improve": [0.0, 0.0001, 0.0005, 0.001],
        "min_score": [3.0, 4.0, 5.0, 6.0, 7.0],
        "sl_pct": [0.5, 0.8, 1.2, 1.8],
        "tp_pct": [1.0, 1.5, 2.0, 3.0, 4.0],
        "max_hold_bars": [8, 12, 24, 36],
        "exit_on_rsi": [55.0, 60.0, 68.0, 75.0],
        "require_close_above_bb_mid": [False, True],
        "require_close_above_ema_fast": [False, True],
        "require_trend_filter": [False, True],
    }



def iter_param_candidates(base: StrategyParams, search: str, n_trials: int, seed: int):
    space = build_search_space(base)
    keys = list(space.keys())
    random.seed(seed)
    if search == "grid":
        combos = itertools.product(*(space[k] for k in keys))
        for values in combos:
            updates = dict(zip(keys, values))
            yield params_replace(base, updates)
    else:
        seen = set()
        for _ in range(n_trials):
            updates = {k: random.choice(space[k]) for k in keys}
            sig = tuple((k, updates[k]) for k in keys)
            if sig in seen:
                continue
            seen.add(sig)
            yield params_replace(base, updates)



def objective_value(summary: dict[str, Any], objective: str) -> float:
    if summary["num_trades"] == 0:
        return -1e9
    metric = {
        "sharpe": summary["sharpe"],
        "return": summary["total_return_pct"],
        "expectancy": summary["expectancy_pct"],
        "profit_factor": summary["profit_factor"] if np.isfinite(summary["profit_factor"]) else 999.0,
    }[objective]
    penalty = 0.0
    if summary["num_trades"] < 5:
        penalty -= 5.0
    penalty += summary["max_drawdown_pct"] * 0.05
    return float(metric + penalty)


# ---------- output ----------


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("No rows.")
        return
    widths = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(row.get(c, ""))))
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    print(header)
    print(sep)
    for row in rows:
        print(" | ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))



def emit(data: Any, args: argparse.Namespace) -> None:
    if args.output:
        if args.output.endswith(".json"):
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        elif isinstance(data, list):
            pd.DataFrame(data).to_csv(args.output, index=False)
        elif isinstance(data, dict) and "trades" in data:
            pd.DataFrame(data["trades"]).to_csv(args.output, index=False)
        else:
            pd.DataFrame(data).to_csv(args.output, index=False)

    if args.json:
        print(json.dumps(data, indent=2, default=str))


# ---------- main ----------


def run_scan(df: pd.DataFrame, args: argparse.Namespace, p: StrategyParams):
    latest = (
        df[df["signal_long"] == 1]
        .sort_values(["ts", "signal_score"], ascending=[False, False])
        .head(args.limit)
        [["ts", "symbol", "close", "signal_score", "rsi", "macd_hist", "vol_ratio", "last_dump_body_pct", "bars_since_dump"]]
        .copy()
    )
    rows = latest.to_dict(orient="records")
    emit(rows, args)
    if not args.json:
        print_table(rows, ["ts", "symbol", "close", "signal_score", "rsi", "macd_hist", "vol_ratio", "last_dump_body_pct", "bars_since_dump"])



def summarize_diagnostics(df: pd.DataFrame, p: StrategyParams, limit: int = 20) -> dict[str, Any]:
    out = df.copy()
    total_rows = int(len(out))
    setup_window = out["setup_window"] == 1
    setup_all = out["setup_all"] == 1
    final_signal = out["signal_long"] == 1

    dump_gates = {
        "dump_body_ok": int(out["dump_body_ok"].sum()),
        "dump_vol_ratio_ok": int(out["dump_vol_ratio_ok"].sum()),
        "dump_vol_z_ok": int(out["dump_vol_z_ok"].sum()),
        "dump_bb_ok": int(out["dump_bb_ok"].sum()),
        "dump_wick_ok": int(out["dump_wick_ok"].sum()),
        "dump_bar": int(out["dump_bar"].sum()),
    }
    setup_gates = {
        "setup_window": int(setup_window.sum()),
        "fade_return_ok": int((setup_window & (out["fade_return_ok"] == 1)).sum()),
        "fade_vol_ok": int((setup_window & (out["fade_vol_ok"] == 1)).sum()),
        "low_holds_ok": int((setup_window & (out["low_holds_ok"] == 1)).sum()),
        "reclaim_structure_ok": int((setup_window & (out["reclaim_structure_ok"] == 1)).sum()),
        "momentum_ok": int((setup_window & (out["momentum_ok"] == 1)).sum()),
        "reclaim_ema_ok": int((setup_window & (out["reclaim_ema_ok"] == 1)).sum()),
        "reclaim_bb_mid_ok": int((setup_window & (out["reclaim_bb_mid_ok"] == 1)).sum()),
        "trend_ok": int((setup_window & (out["trend_ok"] == 1)).sum()),
        "setup_all": int(setup_all.sum()),
        "score_ge_min": int((setup_all & (out["signal_score"] >= p.min_score)).sum()),
        "signal_long": int(final_signal.sum()),
    }

    quantiles = out[["body_pct", "vol_ratio", "vol_z", "dist_from_bb_lower", "lower_wick_frac", "signal_score"]].quantile([0.01, 0.05, 0.10, 0.50, 0.90, 0.95, 0.99]).round(6)

    near_misses = (
        out.loc[setup_window & (~final_signal), [
            "ts", "symbol", "close", "signal_score", "bars_since_dump", "rsi", "last_dump_rsi",
            "macd_hist", "last_dump_macd_hist", "body_pct", "vol_ratio", "vol_z", "dist_from_bb_lower",
            "lower_wick_frac", "fade_return_ok", "fade_vol_ok", "low_holds_ok", "reclaim_structure_ok",
            "momentum_ok", "reclaim_ema_ok", "reclaim_bb_mid_ok", "trend_ok"
        ]]
        .sort_values(["signal_score", "ts"], ascending=[False, False])
        .head(limit)
    )

    strongest_dump_candidates = (
        out[["ts", "symbol", "close", "body_pct", "vol_ratio", "vol_z", "dist_from_bb_lower", "lower_wick_frac"]]
        .sort_values(["body_pct", "dist_from_bb_lower"], ascending=[True, True])
        .head(limit)
    )

    return {
        "params": strategy_params_to_dict(p),
        "rows": total_rows,
        "dump_gates": dump_gates,
        "setup_gates": setup_gates,
        "feature_quantiles": quantiles.reset_index().rename(columns={"index": "quantile"}).to_dict(orient="records"),
        "near_misses": near_misses.to_dict(orient="records"),
        "strongest_dump_candidates": strongest_dump_candidates.to_dict(orient="records"),
    }



def run_backtest_mode(df: pd.DataFrame, args: argparse.Namespace, p: StrategyParams):
    summary = summarize_backtest(df, p)
    emit(summary, args)
    if not args.json:
        headline = [{
            "trades": summary["num_trades"],
            "win_rate_%": round(summary["win_rate"], 2),
            "avg_trade_%": round(summary["avg_trade_pct"], 3),
            "total_return_%": round(summary["total_return_pct"], 3),
            "sharpe": round(summary["sharpe"], 3),
            "max_dd_%": round(summary["max_drawdown_pct"], 3),
            "profit_factor": round(summary["profit_factor"], 3) if np.isfinite(summary["profit_factor"]) else "inf",
        }]
        print("Backtest summary")
        print_table(headline, list(headline[0].keys()))
        if summary["trades"]:
            print("\nRecent trades")
            print_table(summary["trades"][-10:], ["symbol", "entry_ts", "exit_ts", "return_pct", "bars_held", "exit_reason", "signal_score"])



def run_optimize_mode(resampled_df: pd.DataFrame, args: argparse.Namespace, base_p: StrategyParams):
    results = []
    candidates = iter_param_candidates(base_p, args.search, args.n_trials, args.seed)
    for i, p in enumerate(candidates, start=1):
        df = generate_signals(add_features(resampled_df, p), p)
        summary = summarize_backtest(df, p)
        if summary["num_trades"] < args.min_trades:
            if args.search == "random" and i >= args.n_trials:
                break
            continue
        score = objective_value(summary, args.objective)
        results.append({
            "rank_score": round(score, 6),
            "objective": args.objective,
            "num_trades": summary["num_trades"],
            "win_rate": round(summary["win_rate"], 3),
            "total_return_pct": round(summary["total_return_pct"], 3),
            "sharpe": round(summary["sharpe"], 3),
            "max_drawdown_pct": round(summary["max_drawdown_pct"], 3),
            "profit_factor": round(summary["profit_factor"], 3) if np.isfinite(summary["profit_factor"]) else 999.0,
            **summary["params"],
        })
        if args.search == "random" and i >= args.n_trials:
            break

    results = sorted(results, key=lambda x: x["rank_score"], reverse=True)[: args.top_k]
    emit(results, args)
    if not args.json:
        cols = [
            "rank_score", "num_trades", "win_rate", "total_return_pct", "sharpe", "max_drawdown_pct",
            "profit_factor", "min_volume_ratio", "min_dump_return", "fade_bars", "max_recovery_bars",
            "breakout_lookback", "min_rsi_rebound", "min_score", "sl_pct", "tp_pct", "max_hold_bars",
        ]
        print_table(results, cols)



def run_diagnose_mode(df: pd.DataFrame, args: argparse.Namespace, p: StrategyParams):
    summary = summarize_diagnostics(df, p, limit=args.limit)
    emit(summary, args)
    if not args.json:
        print("Diagnostic summary")
        print_table([{
            "rows": summary["rows"],
            **summary["dump_gates"],
            **summary["setup_gates"],
        }], ["rows", "dump_body_ok", "dump_vol_ratio_ok", "dump_vol_z_ok", "dump_bb_ok", "dump_wick_ok", "dump_bar", "setup_window", "fade_return_ok", "fade_vol_ok", "low_holds_ok", "reclaim_structure_ok", "momentum_ok", "setup_all", "score_ge_min", "signal_long"])
        if summary["near_misses"]:
            print("\nTop near misses")
            print_table(summary["near_misses"], ["ts", "symbol", "close", "signal_score", "bars_since_dump", "fade_return_ok", "fade_vol_ok", "low_holds_ok", "reclaim_structure_ok", "momentum_ok"])
        else:
            print("\nNo near misses in current setup window.")



def _future_window_any(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 0:
        return np.zeros(len(values), dtype=int)
    n = len(values)
    out = np.zeros(n, dtype=int)
    for i in range(n):
        start = i + 1
        end = min(n, i + 1 + window)
        if start < end:
            out[i] = int(np.any(values[start:end]))
    return out



def summarize_dump_sweep(df: pd.DataFrame, args: argparse.Namespace, p: StrategyParams) -> dict[str, Any]:
    out = df.copy().sort_values(["symbol", "ts"]).reset_index(drop=True)
    g = out.groupby("symbol", group_keys=False)
    horizons = sorted({int(x) for x in (args.forward_bars or [3, 6, 12]) if int(x) > 0})

    out["dump_severity"] = 0.0
    out["dump_severity"] += np.clip((-out["body_pct"].fillna(0.0)) / max(p.min_dump_return, 1e-9), 0, 5)
    out["dump_severity"] += np.clip(out["vol_ratio"].fillna(0.0) - 1.0, 0, 5)
    out["dump_severity"] += np.clip(out["vol_z"].fillna(0.0), 0, 5)
    out["dump_severity"] += np.clip((p.max_dist_from_lower_band - out["dist_from_bb_lower"].fillna(p.max_dist_from_lower_band)) / max(p.max_dist_from_lower_band, 1e-9), 0, 3)
    out["dump_severity"] += np.clip(out["lower_wick_frac"].fillna(0.0) / max(p.min_lower_wick_frac, 1e-9), 0, 3)

    bb_reclaim_parts = []
    ema_reclaim_parts = []
    lower_low_parts = []
    for _, sdf in out.groupby("symbol", sort=False):
        bb_reclaim_parts.append(pd.Series(
            _future_window_any((sdf["close"] > sdf["bb_mid"]).fillna(False).to_numpy(), p.max_recovery_bars),
            index=sdf.index,
        ))
        ema_reclaim_parts.append(pd.Series(
            _future_window_any((sdf["close"] > sdf["ema_fast"]).fillna(False).to_numpy(), p.max_recovery_bars),
            index=sdf.index,
        ))
        lows = sdf["low"].to_numpy(dtype=float)
        lower_low = np.zeros(len(sdf), dtype=int)
        for i in range(len(sdf)):
            start = i + 1
            end = min(len(sdf), i + 1 + p.max_recovery_bars)
            if start < end:
                lower_low[i] = int(np.any(lows[start:end] < lows[i]))
        lower_low_parts.append(pd.Series(lower_low, index=sdf.index))

    out["reclaims_bb_mid_within_window"] = pd.concat(bb_reclaim_parts).sort_index().astype(int)
    out["reclaims_ema_fast_within_window"] = pd.concat(ema_reclaim_parts).sort_index().astype(int)
    out["makes_lower_low_within_window"] = pd.concat(lower_low_parts).sort_index().astype(int)

    for h in horizons:
        out[f"fwd_ret_{h}"] = g["close"].shift(-h) / out["close"] - 1.0

    dump_events = out[out["dump_bar"] == 1].copy()
    if dump_events.empty:
        return {
            "rows": int(len(out)),
            "num_symbols": int(out["symbol"].nunique()),
            "num_dump_events": 0,
            "symbol_summary": [],
            "top_dump_events": [],
            "top_recovery_symbols": [],
            "horizons": horizons,
        }

    agg_map: dict[str, Any] = {
        "ts": "count",
        "body_pct": "min",
        "vol_ratio": "max",
        "vol_z": "max",
        "dist_from_bb_lower": "min",
        "dump_severity": ["mean", "max"],
        "reclaims_bb_mid_within_window": "mean",
        "reclaims_ema_fast_within_window": "mean",
        "makes_lower_low_within_window": "mean",
    }
    for h in horizons:
        agg_map[f"fwd_ret_{h}"] = ["mean", "median"]

    grouped = dump_events.groupby("symbol").agg(agg_map)
    grouped.columns = ["_".join(str(x) for x in col if x) for col in grouped.columns.to_flat_index()]
    grouped = grouped.rename(columns={
        "ts_count": "dump_event_count",
        "body_pct_min": "worst_dump_body_pct",
        "vol_ratio_max": "max_vol_ratio",
        "vol_z_max": "max_vol_z",
        "dist_from_bb_lower_min": "closest_lower_bb_dist",
        "dump_severity_mean": "avg_dump_severity",
        "dump_severity_max": "max_dump_severity",
        "reclaims_bb_mid_within_window_mean": "bb_mid_reclaim_rate",
        "reclaims_ema_fast_within_window_mean": "ema_fast_reclaim_rate",
        "makes_lower_low_within_window_mean": "lower_low_rate",
    }).reset_index()

    for h in horizons:
        mean_col = f"fwd_ret_{h}_mean"
        median_col = f"fwd_ret_{h}_median"
        if mean_col in grouped.columns:
            grouped[f"fwd_ret_{h}_mean_pct"] = grouped[mean_col] * 100.0
            grouped.drop(columns=[mean_col], inplace=True)
        if median_col in grouped.columns:
            grouped[f"fwd_ret_{h}_median_pct"] = grouped[median_col] * 100.0
            grouped.drop(columns=[median_col], inplace=True)

    grouped["bb_mid_reclaim_rate"] *= 100.0
    grouped["ema_fast_reclaim_rate"] *= 100.0
    grouped["lower_low_rate"] *= 100.0

    recovery_score = grouped["avg_dump_severity"].fillna(0.0)
    recovery_score += grouped["bb_mid_reclaim_rate"].fillna(0.0) / 20.0
    recovery_score += grouped["ema_fast_reclaim_rate"].fillna(0.0) / 20.0
    recovery_score -= grouped["lower_low_rate"].fillna(0.0) / 25.0
    for h in horizons:
        col = f"fwd_ret_{h}_mean_pct"
        if col in grouped.columns:
            recovery_score += grouped[col].fillna(0.0)
    grouped["recovery_score"] = recovery_score
    grouped = grouped.sort_values(["recovery_score", "dump_event_count"], ascending=[False, False]).reset_index(drop=True)

    top_events = dump_events.sort_values(["dump_severity", "ts"], ascending=[False, False]).head(args.sweep_top_k).copy()
    event_cols = ["ts", "symbol", "close", "body_pct", "vol_ratio", "vol_z", "dist_from_bb_lower", "lower_wick_frac", "dump_severity"]
    for h in horizons:
        event_cols.append(f"fwd_ret_{h}")
    top_events = top_events[event_cols]
    for h in horizons:
        top_events[f"fwd_ret_{h}"] = top_events[f"fwd_ret_{h}"] * 100.0

    return {
        "rows": int(len(out)),
        "num_symbols": int(out["symbol"].nunique()),
        "num_dump_events": int(len(dump_events)),
        "horizons": horizons,
        "symbol_summary": grouped.head(args.sweep_top_k).round(4).to_dict(orient="records"),
        "top_recovery_symbols": grouped.head(args.sweep_top_k).round(4).to_dict(orient="records"),
        "top_dump_events": top_events.round(4).to_dict(orient="records"),
    }



def run_sweep_dumps_mode(df: pd.DataFrame, args: argparse.Namespace, p: StrategyParams):
    summary = summarize_dump_sweep(df, args, p)
    emit(summary, args)
    if not args.json:
        print("Dump sweep summary")
        print_table([{
            "rows": summary["rows"],
            "num_symbols": summary["num_symbols"],
            "num_dump_events": summary["num_dump_events"],
            "horizons": ",".join(str(x) for x in summary["horizons"]),
        }], ["rows", "num_symbols", "num_dump_events", "horizons"])
        if summary["top_recovery_symbols"]:
            print("\nTop recovery symbols")
            cols = [c for c in [
                "symbol", "dump_event_count", "worst_dump_body_pct", "max_vol_ratio", "avg_dump_severity",
                "bb_mid_reclaim_rate", "ema_fast_reclaim_rate", "lower_low_rate", "recovery_score"
            ] if c in summary["top_recovery_symbols"][0]]
            print_table(summary["top_recovery_symbols"], cols)
        if summary["top_dump_events"]:
            print("\nTop dump events")
            cols = ["ts", "symbol", "close", "body_pct", "vol_ratio", "vol_z", "dump_severity"]
            for h in summary["horizons"]:
                col = f"fwd_ret_{h}"
                if col in summary["top_dump_events"][0]:
                    cols.append(col)
            print_table(summary["top_dump_events"], cols)



def main() -> None:
    args = parse_args()
    base_p = params_from_args(args)
    raw = load_1m_ohlcv(args)
    if raw.empty:
        raise SystemExit("No OHLCV rows returned.")
    resampled = resample_ohlcv(raw, args.timeframe)

    if args.mode == "optimize":
        # Feature windows themselves are tunable, so optimization recomputes features internally.
        run_optimize_mode(resampled, args, base_p)
        return

    featured = add_features(resampled, base_p)
    signaled = generate_signals(featured, base_p)

    if args.mode == "scan":
        run_scan(signaled, args, base_p)
    elif args.mode == "backtest":
        run_backtest_mode(signaled, args, base_p)
    elif args.mode == "diagnose":
        run_diagnose_mode(signaled, args, base_p)
    elif args.mode == "sweep-dumps":
        run_sweep_dumps_mode(signaled, args, base_p)


if __name__ == "__main__":
    main()

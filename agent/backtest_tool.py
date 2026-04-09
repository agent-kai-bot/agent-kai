"""Backtesting tool — lets agents design, test, and validate strategies
against historical data using the backtesting.py library.

Two modes:

1. **Declarative** (default, safe): the agent provides a JSON strategy
   spec with indicator-based entry/exit conditions. The tool generates
   a ``backtesting.Strategy`` subclass internally, runs it over OHLCV
   from the data API, and returns structured results. No LLM-generated
   code touches the runtime.

2. **Code** (advanced): the agent provides raw Python for a Strategy
   subclass. This should be routed through ``docker_sandbox`` by the
   agent, NOT through this tool. The declarative mode covers the
   vast majority of TA strategies the agent will want to test.

Declarative spec shape::

    {
      "symbol": "BTC",
      "interval": "1h",
      "bars": 500,
      "cash": 100000,
      "commission_pct": 0.1,
      "buy_when": [
        {"indicator": "RSI_14", "op": "<", "value": 30},
        {"indicator": "close", "op": ">", "ref": "EMA_50"}
      ],
      "sell_when": [
        {"indicator": "RSI_14", "op": ">", "value": 70}
      ],
      "stop_loss_pct": null,
      "take_profit_pct": null
    }

Supported indicators (auto-computed from OHLCV):
  RSI_{period}, EMA_{period}, SMA_{period}, ATR_{period},
  BBANDS_upper_{period}, BBANDS_middle_{period}, BBANDS_lower_{period},
  MACD, MACD_signal, MACD_hist, VWAP,
  close, open, high, low, volume
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests
from langchain_core.tools import StructuredTool

from data_api.config import API_PORT

logger = logging.getLogger(__name__)

API_BASE = f"http://localhost:{API_PORT}/api/v1"

# ── Indicator computation ───────────────────────────────────
#
# We pre-compute all requested indicators and add them as columns to
# the DataFrame BEFORE handing it to backtesting.py. This avoids
# doing TA inside the Strategy.next() hot loop and keeps the strategy
# class simple (just column comparisons).


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0.0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift(1)).abs()
    lc = (df["Low"] - df["Close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _bbands(series: pd.Series, period: int = 20, std: float = 2.0):
    mid = series.rolling(period).mean()
    band = series.rolling(period).std() * std
    return mid + band, mid, mid - band


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    cum_tp_vol = (tp * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum()
    return cum_tp_vol / cum_vol


# Pattern: RSI_14, EMA_50, SMA_200, ATR_14, BBANDS_upper_20, etc.
_INDICATOR_RE = re.compile(
    r"^(RSI|EMA|SMA|ATR|BBANDS_upper|BBANDS_middle|BBANDS_lower)_(\d+)$",
    re.IGNORECASE,
)


def _add_indicators(df: pd.DataFrame, indicators: set[str]) -> pd.DataFrame:
    """Compute requested indicators and add as columns."""
    for name in indicators:
        low = name.lower()
        if low in ("close", "open", "high", "low", "volume"):
            continue  # already a column

        m = _INDICATOR_RE.match(name)
        if m:
            kind, period = m.group(1).upper(), int(m.group(2))
            if kind == "RSI":
                df[name] = _rsi(df["Close"], period)
            elif kind == "EMA":
                df[name] = _ema(df["Close"], period)
            elif kind == "SMA":
                df[name] = _sma(df["Close"], period)
            elif kind == "ATR":
                df[name] = _atr(df, period)
            elif kind == "BBANDS_UPPER":
                upper, mid, lower = _bbands(df["Close"], period)
                df[name] = upper
                df[f"BBANDS_middle_{period}"] = mid
                df[f"BBANDS_lower_{period}"] = lower
            elif kind == "BBANDS_MIDDLE":
                _, mid, _ = _bbands(df["Close"], period)
                df[name] = mid
            elif kind == "BBANDS_LOWER":
                _, _, lower = _bbands(df["Close"], period)
                df[name] = lower
            continue

        if low in ("macd", "macd_signal", "macd_hist"):
            if "MACD" not in df.columns:
                macd_l, sig_l, hist_l = _macd(df["Close"])
                df["MACD"] = macd_l
                df["MACD_signal"] = sig_l
                df["MACD_hist"] = hist_l
            continue

        if low == "vwap":
            df["VWAP"] = _vwap(df)
            continue

        logger.warning("Unknown indicator: %s (skipping)", name)

    return df


# ── Condition evaluation ────────────────────────────────────

OPERATORS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "crosses_above": None,  # handled specially
    "crosses_below": None,
}


def _extract_indicators_from_conditions(conditions: list[dict]) -> set[str]:
    """Pull every indicator name referenced in a condition list."""
    names: set[str] = set()
    for cond in conditions:
        ind = cond.get("indicator", "")
        if ind:
            names.add(ind)
        ref = cond.get("ref", "")
        if ref:
            names.add(ref)
    return names


def _column_name(name: str) -> str:
    """Map a condition field name to a DataFrame column name."""
    low = name.lower()
    return {"close": "Close", "open": "Open", "high": "High", "low": "Low", "volume": "Volume"}.get(low, name)


# ── Dynamic Strategy generation ─────────────────────────────

def _build_strategy_class(
    buy_conditions: list[dict],
    sell_conditions: list[dict],
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
):
    """Generate a backtesting.Strategy subclass from a declarative spec."""
    from backtesting import Strategy

    class DeclaredStrategy(Strategy):
        _buy_conds = buy_conditions
        _sell_conds = sell_conditions
        _sl_pct = stop_loss_pct
        _tp_pct = take_profit_pct

        def init(self):
            pass  # All indicators are pre-computed columns

        def next(self):
            # ── Buy logic (all conditions must be true) ──
            if not self.position:
                all_met = True
                for cond in self._buy_conds:
                    col = _column_name(cond["indicator"])
                    op = cond.get("op", ">")
                    if "value" in cond:
                        target = cond["value"]
                    elif "ref" in cond:
                        ref_col = _column_name(cond["ref"])
                        target = self.data[ref_col][-1]
                    else:
                        all_met = False
                        break

                    current = self.data[col][-1]
                    if op == "crosses_above":
                        prev = self.data[col][-2] if len(self.data[col]) > 1 else current
                        if not (prev <= target and current > target):
                            all_met = False
                            break
                    elif op == "crosses_below":
                        prev = self.data[col][-2] if len(self.data[col]) > 1 else current
                        if not (prev >= target and current < target):
                            all_met = False
                            break
                    else:
                        fn = OPERATORS.get(op)
                        if fn and not fn(current, target):
                            all_met = False
                            break

                if all_met:
                    sl = self.data.Close[-1] * (1 - self._sl_pct / 100) if self._sl_pct else None
                    tp = self.data.Close[-1] * (1 + self._tp_pct / 100) if self._tp_pct else None
                    self.buy(sl=sl, tp=tp)

            # ── Sell logic (any condition triggers exit) ──
            elif self.position:
                for cond in self._sell_conds:
                    col = _column_name(cond["indicator"])
                    op = cond.get("op", ">")
                    if "value" in cond:
                        target = cond["value"]
                    elif "ref" in cond:
                        ref_col = _column_name(cond["ref"])
                        target = self.data[ref_col][-1]
                    else:
                        continue

                    current = self.data[col][-1]
                    if op in ("crosses_above", "crosses_below"):
                        prev = self.data[col][-2] if len(self.data[col]) > 1 else current
                        if op == "crosses_above" and prev <= target and current > target:
                            self.position.close()
                            return
                        if op == "crosses_below" and prev >= target and current < target:
                            self.position.close()
                            return
                    else:
                        fn = OPERATORS.get(op)
                        if fn and fn(current, target):
                            self.position.close()
                            return

    return DeclaredStrategy


# ── Data fetching ───────────────────────────────────────────

def _fetch_ohlcv(symbol: str, interval: str, limit: int, source: str = "local") -> pd.DataFrame:
    """Fetch OHLCV and return a backtesting-ready DataFrame.

    Args:
        symbol: Trading symbol.
        interval: Candle interval.
        limit: Number of bars.
        source: "local" (default — hits the project data_api) or
            "coinbase" (direct Coinbase REST — useful for pairs not
            covered by the local source).
    """
    if source == "coinbase":
        from agent.data_sources.coinbase import fetch_candles
        bars = fetch_candles(symbol, interval=interval, limit=limit)
    else:
        resp = requests.get(
            f"{API_BASE}/ohlcv/{symbol}",
            params={"interval": interval, "limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        bars = resp.json()

    if not bars:
        raise ValueError(f"No OHLCV data for {symbol} {interval} (source={source})")

    df = pd.DataFrame(bars)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts")
    df = df.rename(columns={c: c.capitalize() for c in df.columns})
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df = df.sort_index()
    return df


# ── Run backtest ────────────────────────────────────────────

def run_declarative_backtest(spec: dict) -> dict[str, Any]:
    """Run a backtest from a declarative spec dict. Returns a results dict."""
    from backtesting import Backtest

    symbol = spec.get("symbol", "BTC")
    interval = spec.get("interval", "1h")
    limit = spec.get("bars", 500)
    cash = spec.get("cash", 100000)
    commission = spec.get("commission_pct", 0.1) / 100  # backtesting.py uses fraction
    source = spec.get("source", "local")

    buy_conds = spec.get("buy_when", [])
    sell_conds = spec.get("sell_when", [])

    if not buy_conds:
        return {"success": False, "error": "buy_when must have at least one condition"}

    # Fetch data
    df = _fetch_ohlcv(symbol, interval, limit, source=source)

    # Discover and compute all referenced indicators
    all_indicators = (
        _extract_indicators_from_conditions(buy_conds)
        | _extract_indicators_from_conditions(sell_conds)
    )
    df = _add_indicators(df, all_indicators)
    df = df.dropna()

    if len(df) < 50:
        return {"success": False, "error": f"Not enough data after indicator warm-up ({len(df)} bars)"}

    # Build and run
    strategy_cls = _build_strategy_class(
        buy_conditions=buy_conds,
        sell_conditions=sell_conds,
        stop_loss_pct=spec.get("stop_loss_pct"),
        take_profit_pct=spec.get("take_profit_pct"),
    )

    bt = Backtest(df, strategy_cls, cash=cash, commission=commission, finalize_trades=True)
    stats = bt.run()

    # Extract the stats we care about
    num_trades = int(stats.get("# Trades", 0))
    result = {
        "success": True,
        "symbol": symbol,
        "interval": interval,
        "bars_used": len(df),
        "period": f"{df.index[0].isoformat()} to {df.index[-1].isoformat()}",
        "total_return_pct": round(float(stats.get("Return [%]", 0)), 2),
        "sharpe_ratio": round(float(stats.get("Sharpe Ratio", 0)), 4) if pd.notna(stats.get("Sharpe Ratio")) else None,
        "max_drawdown_pct": round(float(stats.get("Max. Drawdown [%]", 0)), 2),
        "num_trades": num_trades,
        "win_rate_pct": round(float(stats.get("Win Rate [%]", 0)), 1) if num_trades > 0 else None,
        "avg_trade_pct": round(float(stats.get("Avg. Trade [%]", 0)), 2) if num_trades > 0 else None,
        "best_trade_pct": round(float(stats.get("Best Trade [%]", 0)), 2) if num_trades > 0 else None,
        "worst_trade_pct": round(float(stats.get("Worst Trade [%]", 0)), 2) if num_trades > 0 else None,
        "exposure_pct": round(float(stats.get("Exposure Time [%]", 0)), 1),
        "buy_and_hold_return_pct": round(float(stats.get("Buy & Hold Return [%]", 0)), 2),
    }

    # Interpretation helper
    if num_trades == 0:
        result["interpretation"] = "No trades were triggered. The conditions may be too strict or the market didn't match."
    elif result["win_rate_pct"] and result["win_rate_pct"] >= 55 and result.get("sharpe_ratio") and result["sharpe_ratio"] > 0.5:
        result["interpretation"] = "Promising edge. Consider saving this as a validated skill."
    elif num_trades < 5:
        result["interpretation"] = "Too few trades to draw conclusions. Try a longer period or looser conditions."
    else:
        result["interpretation"] = "Weak or negative edge. Review the conditions or try a different approach."

    return result


# ── LangChain tool ──────────────────────────────────────────

def _run_backtest_tool(
    symbol: str = "BTC",
    interval: str = "1h",
    bars: int = 500,
    buy_when: str = "",
    sell_when: str = "",
    stop_loss_pct: float = 0,
    take_profit_pct: float = 0,
    source: str = "local",
) -> str:
    """Run a backtest with a declarative strategy spec.

    Args:
        symbol: Trading symbol (e.g. BTC, ETH, SOL)
        interval: Candle interval (1m, 5m, 15m, 1h, 4h, 1d)
        bars: Number of historical bars to test over (default 500)
        buy_when: JSON array of entry conditions. Each condition is an object with:
            - indicator: name (RSI_14, EMA_50, close, MACD, BBANDS_upper_20, etc.)
            - op: comparison operator (<, >, <=, >=, ==, crosses_above, crosses_below)
            - value: numeric threshold, OR
            - ref: another indicator name to compare against
            Example: [{"indicator": "RSI_14", "op": "<", "value": 30}]
        sell_when: JSON array of exit conditions (same format, any one triggers exit)
        stop_loss_pct: Stop loss as percent below entry (0 = no stop)
        take_profit_pct: Take profit as percent above entry (0 = no TP)
        source: Data source — "local" (default, uses project data_api)
            or "coinbase" (Coinbase Advanced Trade public API).

    Returns:
        JSON with backtest results including win_rate, sharpe, drawdown, num_trades.
    """
    try:
        buy_conds = json.loads(buy_when) if buy_when else []
        sell_conds = json.loads(sell_when) if sell_when else []
    except json.JSONDecodeError as e:
        return json.dumps({"success": False, "error": f"Invalid JSON in conditions: {e}"})

    if not buy_conds:
        return json.dumps({"success": False, "error": "buy_when must have at least one condition"})

    spec = {
        "symbol": symbol,
        "interval": interval,
        "bars": bars,
        "buy_when": buy_conds,
        "sell_when": sell_conds,
        "stop_loss_pct": stop_loss_pct or None,
        "take_profit_pct": take_profit_pct or None,
        "source": source,
    }

    try:
        result = run_declarative_backtest(spec)
    except Exception as e:
        logger.error("Backtest failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(result, default=str)


run_backtest = StructuredTool.from_function(
    func=_run_backtest_tool,
    name="run_backtest",
    description=(
        "Backtest a trading strategy over historical data. Provide entry "
        "conditions (buy_when) and exit conditions (sell_when) as JSON "
        "arrays of indicator-based rules. Each rule specifies an indicator "
        "(RSI_14, EMA_50, MACD, BBANDS_upper_20, close, etc.), a comparison "
        "operator (<, >, crosses_above, crosses_below), and a threshold "
        "value or reference indicator.\n\n"
        "Example buy_when: "
        '[{"indicator": "RSI_14", "op": "<", "value": 30}, '
        '{"indicator": "close", "op": ">", "ref": "EMA_50"}]\n\n'
        "Returns: win_rate, sharpe_ratio, max_drawdown, num_trades, "
        "avg_trade_pct, and an interpretation. Use this to VALIDATE a "
        "hypothesis before recommending a trade or saving it as a skill. "
        "A strategy with win_rate > 55% and sharpe > 0.5 is worth keeping."
    ),
)

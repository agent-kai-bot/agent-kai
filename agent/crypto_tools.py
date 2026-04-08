"""Crypto-specific tools for KAI agents."""

import requests
import pandas as pd
from langchain_core.tools import StructuredTool

from data_api.config import API_PORT

API_BASE = f"http://localhost:{API_PORT}/api/v1"
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


def _last_valid_value(series: pd.Series) -> float:
    """Return the latest non-null numeric value from a series.

    Args:
        series: Indicator output series.

    Returns:
        The most recent non-null value.

    Raises:
        ValueError: If the series has no non-null values.
    """
    valid = series.dropna()
    if valid.empty:
        raise ValueError("indicator result contained no valid values")
    return float(valid.iloc[-1])


def _extract_bbands_levels(bb: pd.DataFrame) -> tuple[float, float, float]:
    """Extract lower, mid, and upper Bollinger Band levels.

    Args:
        bb: DataFrame returned by ``pandas_ta.bbands``.

    Returns:
        A tuple of ``(lower, mid, upper)``.

    Raises:
        ValueError: If the expected band columns are missing or invalid.
    """
    if bb is None or bb.empty:
        raise ValueError("bollinger band result was empty")

    lower_col = next((col for col in bb.columns if str(col).startswith("BBL_")), None)
    mid_col = next((col for col in bb.columns if str(col).startswith("BBM_")), None)
    upper_col = next((col for col in bb.columns if str(col).startswith("BBU_")), None)

    if not all([lower_col, mid_col, upper_col]):
        raise ValueError(f"unexpected bollinger band columns: {list(bb.columns)}")

    lower = _last_valid_value(bb[lower_col])
    mid = _last_valid_value(bb[mid_col])
    upper = _last_valid_value(bb[upper_col])
    return lower, mid, upper


def _classify_bbands_position(price: float, lower: float, mid: float, upper: float) -> str:
    """Classify the current price relative to Bollinger Bands.

    Args:
        price: Current closing price.
        lower: Lower Bollinger Band.
        mid: Middle Bollinger Band.
        upper: Upper Bollinger Band.

    Returns:
        A short position label such as ``near upper`` or ``mid range``.
    """
    upper_half = upper - mid
    lower_half = mid - lower

    if upper_half <= 0 or lower_half <= 0:
        return "mid range"
    if price >= upper - (upper_half * 0.2):
        return "near upper"
    if price <= lower + (lower_half * 0.2):
        return "near lower"
    return "mid range"


# ── Query OHLCV ─────────────────────────────────────────────

def _query_ohlcv(symbol: str, interval: str = "1m", limit: int = 100,
                 start: str = "", end: str = "") -> str:
    """Query historical OHLCV candle data."""
    params = {"interval": interval, "limit": limit}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    try:
        resp = requests.get(f"{API_BASE}/ohlcv/{symbol.upper()}", params=params, timeout=10)
        resp.raise_for_status()
        bars = resp.json()
        if not bars:
            return f"No data for {symbol.upper()} ({interval})"
        lines = [f"{symbol.upper()} {interval} — {len(bars)} bars:"]
        lines.append(f"{'Time':<20} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>12}")
        for b in bars[-20:]:  # Show last 20
            ts = b["ts"][:16] if len(b["ts"]) > 16 else b["ts"]
            lines.append(f"{ts:<20} {b['open']:>10.2f} {b['high']:>10.2f} {b['low']:>10.2f} {b['close']:>10.2f} {b['volume']:>12.4f}")
        if len(bars) > 20:
            lines.insert(2, f"  ... showing last 20 of {len(bars)} bars")
        return "\n".join(lines)
    except Exception as e:
        return f"Error querying OHLCV: {e}"


query_ohlcv = StructuredTool.from_function(
    func=_query_ohlcv,
    name="query_ohlcv",
    description=(
        "Query historical OHLCV candle data for a crypto symbol. "
        "Inputs: symbol (str, e.g. 'BTC'), interval (str: '1m','5m','15m','1h','6h','1d', default '1m'), "
        "limit (int, default 100, max 5000), start (str, optional ISO datetime), end (str, optional ISO datetime)."
    ),
)


# ── Get Latest Price ─────────────────────────────────────────

def _get_latest_price(symbol: str) -> str:
    """Get the latest price for a crypto symbol."""
    try:
        resp = requests.get(f"{API_BASE}/price/{symbol.upper()}", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return f"{data['symbol']}: ${data['price']:,.2f} (as of {data['ts'][:19]})"
    except Exception as e:
        return f"Error getting price: {e}"


get_latest_price = StructuredTool.from_function(
    func=_get_latest_price,
    name="get_latest_price",
    description="Get the latest price for a crypto symbol. Input: symbol (str, e.g. 'BTC').",
)


# ── List Symbols ─────────────────────────────────────────────

def _list_symbols() -> str:
    """List all available crypto symbols with latest prices."""
    try:
        resp = requests.get(f"{API_BASE}/symbols", timeout=10)
        resp.raise_for_status()
        symbols = resp.json()
        lines = [f"{len(symbols)} symbols available:"]
        lines.append(f"{'Symbol':<10} {'Price':>12}")
        for s in sorted(symbols, key=lambda x: x.get("latest_price") or 0, reverse=True):
            price = s.get("latest_price")
            if price is not None:
                lines.append(f"{s['symbol']:<10} ${price:>11,.2f}")
            else:
                lines.append(f"{s['symbol']:<10} {'N/A':>12}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing symbols: {e}"


list_symbols = StructuredTool.from_function(
    func=_list_symbols,
    name="list_symbols",
    description="List all available crypto symbols with their latest prices.",
)


# ── Calculate Indicator ──────────────────────────────────────

def _calculate_indicator(
    symbol: str,
    indicator: str = "RSI",
    period: int = 14,
    interval: str = "1m",
    limit: int = 200,
) -> str:
    """Compute a technical analysis indicator on OHLCV data.

    Args:
        symbol: Asset symbol such as ``BTC``.
        indicator: Indicator name.
        period: Lookback period for period-based indicators.
        interval: Candle timeframe.
        limit: Number of bars to fetch.

    Returns:
        A formatted technical-indicator summary.
    """
    try:
        resp = requests.get(
            f"{API_BASE}/ohlcv/{symbol.upper()}",
            params={"interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        bars = resp.json()
        if len(bars) < period + 1:
            return f"Not enough data ({len(bars)} bars) for {indicator}({period})"

        df = pd.DataFrame(bars)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        import pandas_ta as ta
        indicator = indicator.upper()
        result_lines = [f"{symbol.upper()} {indicator}({period}) on {interval}:"]

        if indicator == "RSI":
            series = ta.rsi(df["close"], length=period)
            val = _last_valid_value(series)
            zone = "overbought (>70)" if val > 70 else "oversold (<30)" if val < 30 else "neutral"
            result_lines.append(f"  RSI({period}) = {val:.1f} [{zone}]")
            recent = ", ".join(f"{v:.1f}" for v in series.dropna().tail(5))
            result_lines.append(f"  Last 5: {recent}")

        elif indicator == "SMA":
            series = ta.sma(df["close"], length=period)
            val = _last_valid_value(series)
            price = df["close"].iloc[-1]
            above = "price ABOVE SMA" if price > val else "price BELOW SMA"
            result_lines.append(f"  SMA({period}) = ${val:,.2f} [{above}, price=${price:,.2f}]")

        elif indicator == "EMA":
            series = ta.ema(df["close"], length=period)
            val = _last_valid_value(series)
            price = df["close"].iloc[-1]
            above = "price ABOVE EMA" if price > val else "price BELOW EMA"
            result_lines.append(f"  EMA({period}) = ${val:,.2f} [{above}, price=${price:,.2f}]")

        elif indicator == "MACD":
            macd_df = ta.macd(
                df["close"],
                fast=MACD_FAST,
                slow=MACD_SLOW,
                signal=MACD_SIGNAL,
            )
            if macd_df is not None and not macd_df.empty:
                macd_val = _last_valid_value(macd_df.iloc[:, 0])
                signal_val = _last_valid_value(macd_df.iloc[:, 1])
                hist_val = _last_valid_value(macd_df.iloc[:, 2])
                trend = "bullish" if hist_val > 0 else "bearish"
                result_lines[0] = (
                    f"{symbol.upper()} MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) on {interval}:"
                )
                result_lines.append(
                    f"  MACD = {macd_val:.2f}, Signal = {signal_val:.2f}, "
                    f"Histogram = {hist_val:.2f} [{trend}]"
                )

        elif indicator == "BBANDS":
            bb = ta.bbands(df["close"], length=period)
            if bb is not None and not bb.empty:
                lower, mid, upper = _extract_bbands_levels(bb)
                price = df["close"].iloc[-1]
                pos = _classify_bbands_position(price, lower, mid, upper)
                result_lines.append(f"  Upper=${upper:,.2f}, Mid=${mid:,.2f}, Lower=${lower:,.2f} [price {pos}]")

        elif indicator == "ATR":
            series = ta.atr(df["high"], df["low"], df["close"], length=period)
            val = _last_valid_value(series)
            price = df["close"].iloc[-1]
            pct = (val / price) * 100
            result_lines.append(f"  ATR({period}) = ${val:,.2f} ({pct:.2f}% of price)")

        elif indicator == "VWAP":
            series = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
            if series is not None:
                val = _last_valid_value(series)
                price = df["close"].iloc[-1]
                above = "price ABOVE VWAP" if price > val else "price BELOW VWAP"
                result_lines.append(f"  VWAP = ${val:,.2f} [{above}, price=${price:,.2f}]")

        else:
            return f"Unknown indicator: {indicator}. Available: RSI, SMA, EMA, MACD, BBANDS, ATR, VWAP"

        return "\n".join(result_lines)
    except Exception as e:
        return f"Error calculating indicator: {e}"


calculate_indicator = StructuredTool.from_function(
    func=_calculate_indicator,
    name="calculate_indicator",
    description=(
        "Compute a technical analysis indicator on crypto OHLCV data. "
        "Inputs: symbol (str), indicator (str: 'RSI','SMA','EMA','MACD','BBANDS','ATR','VWAP'), "
        "period (int, default 14), interval (str, default '1m'), limit (int, default 200). "
        "MACD uses standard 12/26/9 settings."
    ),
)


# ── Place Order ──────────────────────────────────────────────

def _place_order(symbol: str, side: str, quantity: float,
                 order_type: str = "market", price: float = 0.0,
                 stop_loss: float = 0.0, take_profit: float = 0.0) -> str:
    """Place a paper trade."""
    from data_api.paper_trading import portfolio
    result = portfolio.place_order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price if price > 0 else None,
        stop_loss=stop_loss if stop_loss > 0 else None,
        take_profit=take_profit if take_profit > 0 else None,
    )
    if result.success:
        return result.message
    return f"Order failed: {result.message}"


place_order = StructuredTool.from_function(
    func=_place_order,
    name="place_order",
    description=(
        "Place a paper trade (simulated). "
        "Inputs: symbol (str), side (str: 'buy' or 'sell'), quantity (float), "
        "order_type (str: 'market' or 'limit', default 'market'), "
        "price (float, required for limit orders), "
        "stop_loss (float, optional), take_profit (float, optional)."
    ),
)


# ── Get Positions ────────────────────────────────────────────

def _get_positions(symbol: str = "") -> str:
    """Get current paper trading positions and P&L."""
    from data_api.paper_trading import portfolio
    positions = portfolio.get_positions(symbol if symbol else None)
    pnl = portfolio.get_pnl()

    lines = [f"Portfolio: ${pnl['total_value']:,.2f} (P&L: ${pnl['total_pnl']:+,.2f} / {pnl['total_pnl_pct']:+.2f}%)"]
    lines.append(f"Cash: ${pnl['cash']:,.2f} | Unrealized: ${pnl['unrealized_pnl']:+,.2f} | Realized: ${pnl['realized_pnl']:+,.2f}")

    if positions:
        lines.append(f"\n{'Symbol':<8} {'Side':<6} {'Qty':>8} {'Entry':>10} {'Current':>10} {'P&L':>10} {'P&L%':>7}")
        for p in positions:
            lines.append(
                f"{p['symbol']:<8} {p['side']:<6} {p['quantity']:>8.4f} "
                f"${p['entry_price']:>9,.2f} ${p['current_price']:>9,.2f} "
                f"${p['unrealized_pnl']:>+9,.2f} {p['pnl_pct']:>+6.1f}%"
            )
    else:
        lines.append("\nNo open positions.")

    return "\n".join(lines)


get_positions = StructuredTool.from_function(
    func=_get_positions,
    name="get_positions",
    description="Get current paper trading positions and portfolio P&L. Optional input: symbol (str) to filter.",
)


# ── Scan Tokens ──────────────────────────────────────────────

def _scan_tokens(filter_type: str = "new", limit: int = 20) -> str:
    """Scan for new/trending tokens on pump.fun."""
    try:
        if filter_type == "trending":
            url = "https://frontend-api-v3.pump.fun/coins/trending"
        elif filter_type == "graduated":
            url = "https://frontend-api-v3.pump.fun/coins/graduated"
        else:
            url = "https://frontend-api-v3.pump.fun/coins/latest"

        resp = requests.get(url, timeout=10, headers={"User-Agent": "KAI/1.0"})
        resp.raise_for_status()
        tokens = resp.json()

        if isinstance(tokens, dict):
            tokens = tokens.get("coins", tokens.get("data", []))
        if not isinstance(tokens, list):
            return f"Unexpected response format from pump.fun"

        tokens = tokens[:limit]
        if not tokens:
            return f"No {filter_type} tokens found."

        lines = [f"pump.fun {filter_type} tokens ({len(tokens)}):"]
        lines.append(f"{'Name':<20} {'Symbol':<10} {'Market Cap':>12}")
        for t in tokens:
            name = t.get("name", "?")[:19]
            sym = t.get("symbol", "?")[:9]
            mcap = t.get("market_cap") or t.get("usd_market_cap") or 0
            if isinstance(mcap, (int, float)) and mcap > 0:
                lines.append(f"{name:<20} {sym:<10} ${mcap:>11,.0f}")
            else:
                lines.append(f"{name:<20} {sym:<10} {'N/A':>12}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error scanning tokens: {e}"


scan_tokens = StructuredTool.from_function(
    func=_scan_tokens,
    name="scan_tokens",
    description=(
        "Scan pump.fun for new or trending tokens. "
        "Inputs: filter_type (str: 'new', 'trending', or 'graduated', default 'new'), "
        "limit (int, default 20)."
    ),
)


# ── Registry ─────────────────────────────────────────────────

ALL_CRYPTO_TOOLS = [
    query_ohlcv, get_latest_price, list_symbols,
    calculate_indicator, place_order, get_positions, scan_tokens,
]

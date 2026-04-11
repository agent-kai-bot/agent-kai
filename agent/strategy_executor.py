"""Typed strategy executor for ASO v1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pandas as pd

from agent.backtest_tool import _atr, _bbands, _ema, _rsi, _sma
from agent.strategy_ir import (
    AtrExitSpec,
    Condition,
    ConditionOperator,
    ConstantValue,
    IndicatorBand,
    IndicatorSpec,
    IndicatorType,
    IndicatorValue,
    PercentExitSpec,
    RangeValue,
    StrategyIR,
    TrailingStopSpec,
)

INITIAL_CASH = 100_000.0


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_EXIT = "time_exit"
    DATA_BOUNDARY = "data_boundary"


@dataclass(frozen=True)
class TradeRecord:
    """One fully closed trade."""

    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_bar: int
    exit_bar: int
    entry_price: float
    exit_price: float
    gross_entry_price: float
    gross_exit_price: float
    quantity: float
    entry_notional: float
    exit_notional: float
    gross_pnl: float
    net_pnl: float
    fees_paid: float
    bars_held: int
    return_pct: float
    exit_reason: ExitReason
    entry_equity_before: float


@dataclass(frozen=True)
class BacktestResult:
    """Full typed executor output."""

    trades: list[TradeRecord]
    equity_curve: pd.Series
    gross_equity_curve: pd.Series
    benchmark_prices: pd.Series
    warmup_bars: int
    initial_cash: float


def execute_strategy(ir: StrategyIR, ohlcv: pd.DataFrame) -> BacktestResult:
    """Run a compiled v1 strategy over OHLCV data."""
    frame = _normalize_ohlcv(ohlcv)
    frame = _add_ir_indicators(frame, ir.indicators)

    if len(frame) <= ir.warmup_bars:
        raise ValueError("not enough data after warmup")

    cash = INITIAL_CASH
    gross_cash = INITIAL_CASH
    trades: list[TradeRecord] = []
    equity_points: list[float] = []
    gross_equity_points: list[float] = []
    position: dict[str, Any] | None = None
    last_index = len(frame) - 1

    for bar_index, (timestamp, row) in enumerate(frame.iterrows()):
        if position is not None:
            exit_payload = _check_exit(ir, frame, row, bar_index, timestamp, position)
            if exit_payload is not None:
                cash, gross_cash, trade = _close_position(cash, gross_cash, position, exit_payload)
                trades.append(trade)
                position = None

        if position is None and bar_index >= ir.warmup_bars and bar_index < last_index:
            if _conditions_met(frame, bar_index, ir.entry.conditions):
                position, cash, gross_cash = _open_position(ir, frame, row, timestamp, bar_index, cash, gross_cash)

        if position is not None and bar_index == last_index:
            boundary_exit = {
                "exit_time": timestamp,
                "exit_bar": bar_index,
                "exit_price": _time_exit_price(float(row["Close"]), ir.costs.slippage_pct),
                "gross_exit_price": float(row["Close"]),
                "exit_reason": ExitReason.DATA_BOUNDARY,
            }
            cash, gross_cash, trade = _close_position(cash, gross_cash, position, boundary_exit)
            trades.append(trade)
            position = None

        equity_points.append(_mark_to_market(cash, position, float(row["Close"])))
        gross_equity_points.append(_mark_to_market(gross_cash, position, float(row["Close"])))

    equity_curve = pd.Series(equity_points, index=frame.index, name="equity").iloc[ir.warmup_bars :]
    gross_equity_curve = pd.Series(gross_equity_points, index=frame.index, name="gross_equity").iloc[ir.warmup_bars :]
    benchmark_prices = frame["Close"].astype(float).iloc[ir.warmup_bars :]
    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        gross_equity_curve=gross_equity_curve,
        benchmark_prices=benchmark_prices,
        warmup_bars=ir.warmup_bars,
        initial_cash=INITIAL_CASH,
    )


def _normalize_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    renamed = ohlcv.rename(columns={column: column.capitalize() for column in ohlcv.columns}).copy()
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [column for column in required if column not in renamed.columns]
    if missing:
        raise ValueError(f"ohlcv is missing required columns: {', '.join(missing)}")
    renamed = renamed[required].astype(float)
    if not isinstance(renamed.index, pd.DatetimeIndex):
        renamed.index = pd.to_datetime(renamed.index)
    return renamed.sort_index()


def _add_ir_indicators(frame: pd.DataFrame, indicators: list[IndicatorSpec]) -> pd.DataFrame:
    for indicator in indicators:
        if indicator.type == IndicatorType.RSI:
            frame[indicator.alias] = _rsi(frame["Close"], indicator.period)
        elif indicator.type == IndicatorType.EMA:
            frame[indicator.alias] = _ema(frame["Close"], indicator.period)
        elif indicator.type == IndicatorType.SMA:
            frame[indicator.alias] = _sma(frame["Close"], indicator.period)
        elif indicator.type == IndicatorType.ATR:
            frame[indicator.alias] = _atr(frame, indicator.period)
        elif indicator.type == IndicatorType.BBANDS:
            upper, middle, lower = _bbands(frame["Close"], indicator.period, indicator.stddev)
            band_map = {
                IndicatorBand.UPPER: upper,
                IndicatorBand.MIDDLE: middle,
                IndicatorBand.LOWER: lower,
            }
            assert indicator.band is not None
            frame[indicator.alias] = band_map[indicator.band]
    return frame


def _conditions_met(frame: pd.DataFrame, bar_index: int, conditions: list[Condition]) -> bool:
    for condition in conditions:
        left_current = _series_value(frame, condition.indicator, bar_index)
        if left_current is None:
            return False

        if condition.operator == ConditionOperator.ABOVE:
            right_value = _target_value(frame, condition.target, bar_index)
            if right_value is None or left_current <= right_value:
                return False
        elif condition.operator == ConditionOperator.BELOW:
            right_value = _target_value(frame, condition.target, bar_index)
            if right_value is None or left_current >= right_value:
                return False
        elif condition.operator == ConditionOperator.CROSSES_ABOVE:
            left_previous = _series_value(frame, condition.indicator, bar_index - 1)
            right_current = _target_value(frame, condition.target, bar_index)
            right_previous = _target_value(frame, condition.target, bar_index - 1)
            if None in (left_previous, right_current, right_previous):
                return False
            if not (left_previous <= right_previous and left_current > right_current):
                return False
        elif condition.operator == ConditionOperator.CROSSES_BELOW:
            left_previous = _series_value(frame, condition.indicator, bar_index - 1)
            right_current = _target_value(frame, condition.target, bar_index)
            right_previous = _target_value(frame, condition.target, bar_index - 1)
            if None in (left_previous, right_current, right_previous):
                return False
            if not (left_previous >= right_previous and left_current < right_current):
                return False
        elif condition.operator == ConditionOperator.BETWEEN:
            assert isinstance(condition.target, RangeValue)
            lower_value = _target_value(frame, condition.target.lower, bar_index)
            upper_value = _target_value(frame, condition.target.upper, bar_index)
            if None in (lower_value, upper_value):
                return False
            if not (lower_value <= left_current <= upper_value):
                return False
    return True


def _target_value(frame: pd.DataFrame, target, bar_index: int) -> float | None:
    if isinstance(target, ConstantValue):
        return float(target.value)
    if isinstance(target, IndicatorValue):
        return _series_value(frame, target.value, bar_index)
    raise TypeError(f"unsupported target type: {type(target)!r}")


def _series_value(frame: pd.DataFrame, name: str, bar_index: int) -> float | None:
    if bar_index < 0 or bar_index >= len(frame):
        return None

    column_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    value = frame.iloc[bar_index][column_map.get(name, name)]
    return None if pd.isna(value) else float(value)


def _open_position(
    ir: StrategyIR,
    frame: pd.DataFrame,
    row: pd.Series,
    timestamp: pd.Timestamp,
    bar_index: int,
    cash: float,
    gross_cash: float,
) -> tuple[dict[str, Any], float, float]:
    reference_entry_price = float(row["Close"])
    entry_price = reference_entry_price * (1.0 + ir.costs.slippage_pct + ir.costs.spread_pct)
    position_notional = cash * ir.risk.max_position_pct
    quantity = position_notional / entry_price
    entry_fee = position_notional * ir.costs.fee_pct

    cash_after_entry = cash - position_notional - entry_fee
    gross_cash_after_entry = gross_cash - (quantity * reference_entry_price)
    atr_at_entry = {
        indicator.alias: float(frame.iloc[bar_index][indicator.alias])
        for indicator in ir.indicators
        if indicator.type == IndicatorType.ATR
    }
    position = {
        "entry_time": timestamp,
        "entry_bar": bar_index,
        "entry_price": entry_price,
        "gross_entry_price": reference_entry_price,
        "entry_fee": entry_fee,
        "fee_pct": ir.costs.fee_pct,
        "entry_notional": position_notional,
        "quantity": quantity,
        "entry_equity_before": cash,
        "stop_price": _offset_level(reference_entry_price, ir.exit.stop_loss, atr_at_entry),
        "take_profit_price": _offset_level(reference_entry_price, ir.exit.take_profit, atr_at_entry, is_take_profit=True),
        "highest_high": float(row["High"]),
        "trail_price": None,
        "trail_active": False,
        "atr_at_entry": atr_at_entry,
    }
    return position, cash_after_entry, gross_cash_after_entry


def _check_exit(
    ir: StrategyIR,
    frame: pd.DataFrame,
    row: pd.Series,
    bar_index: int,
    timestamp: pd.Timestamp,
    position: dict[str, Any],
) -> dict[str, Any] | None:
    low = float(row["Low"])
    high = float(row["High"])
    close = float(row["Close"])

    trailing_stop = ir.exit.trailing_stop
    if trailing_stop is not None:
        position["highest_high"] = max(position["highest_high"], high)
        if _trailing_activation_met(trailing_stop, frame, bar_index, position):
            position["trail_active"] = True
            candidate = _trailing_price(trailing_stop, frame, bar_index, position)
            if candidate is not None:
                position["trail_price"] = candidate if position["trail_price"] is None else max(position["trail_price"], candidate)

    if position["stop_price"] is not None and low <= position["stop_price"]:
        stop_price = position["stop_price"]
        return {
            "exit_time": timestamp,
            "exit_bar": bar_index,
            "exit_price": stop_price * (1.0 + ir.costs.slippage_pct),
            "gross_exit_price": stop_price,
            "exit_reason": ExitReason.STOP_LOSS,
        }

    if position["take_profit_price"] is not None and high >= position["take_profit_price"]:
        take_profit_price = position["take_profit_price"]
        return {
            "exit_time": timestamp,
            "exit_bar": bar_index,
            "exit_price": take_profit_price * (1.0 - ir.costs.slippage_pct),
            "gross_exit_price": take_profit_price,
            "exit_reason": ExitReason.TAKE_PROFIT,
        }

    if position["trail_active"] and position["trail_price"] is not None and low <= position["trail_price"]:
        trail_price = position["trail_price"]
        return {
            "exit_time": timestamp,
            "exit_bar": bar_index,
            "exit_price": trail_price,
            "gross_exit_price": trail_price,
            "exit_reason": ExitReason.TRAILING_STOP,
        }

    if ir.exit.time_exit is not None:
        bars_held = bar_index - position["entry_bar"]
        if bars_held >= ir.exit.time_exit.max_bars:
            return {
                "exit_time": timestamp,
                "exit_bar": bar_index,
                "exit_price": _time_exit_price(close, ir.costs.slippage_pct),
                "gross_exit_price": close,
                "exit_reason": ExitReason.TIME_EXIT,
            }
    return None


def _time_exit_price(close_price: float, slippage_pct: float) -> float:
    return close_price * (1.0 + slippage_pct)


def _close_position(
    cash: float,
    gross_cash: float,
    position: dict[str, Any],
    exit_payload: dict[str, Any],
) -> tuple[float, float, TradeRecord]:
    quantity = position["quantity"]
    exit_notional = quantity * exit_payload["exit_price"]
    gross_exit_notional = quantity * exit_payload["gross_exit_price"]

    fee_pct = position["fee_pct"]
    entry_fee = position["entry_fee"]
    exit_fee = exit_notional * fee_pct
    gross_pnl = gross_exit_notional - (quantity * position["gross_entry_price"])
    cash_after_exit = cash + exit_notional - exit_fee
    gross_cash_after_exit = gross_cash + gross_exit_notional
    fees_paid = entry_fee + exit_fee

    net_pnl = cash_after_exit - position["entry_equity_before"]
    trade = TradeRecord(
        entry_time=position["entry_time"],
        exit_time=exit_payload["exit_time"],
        entry_bar=position["entry_bar"],
        exit_bar=exit_payload["exit_bar"],
        entry_price=position["entry_price"],
        exit_price=exit_payload["exit_price"],
        gross_entry_price=position["gross_entry_price"],
        gross_exit_price=exit_payload["gross_exit_price"],
        quantity=quantity,
        entry_notional=position["entry_notional"],
        exit_notional=exit_notional,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        fees_paid=fees_paid,
        bars_held=exit_payload["exit_bar"] - position["entry_bar"],
        return_pct=(cash_after_exit - position["entry_equity_before"]) / position["entry_notional"],
        exit_reason=exit_payload["exit_reason"],
        entry_equity_before=position["entry_equity_before"],
    )
    return cash_after_exit, gross_cash_after_exit, trade


def _offset_level(
    reference_entry_price: float,
    offset_spec,
    atr_at_entry: dict[str, float],
    *,
    is_take_profit: bool = False,
) -> float | None:
    if offset_spec is None:
        return None
    if isinstance(offset_spec, PercentExitSpec):
        direction = 1.0 if is_take_profit else -1.0
        return reference_entry_price * (1.0 + direction * offset_spec.percent)
    if isinstance(offset_spec, AtrExitSpec):
        atr_value = atr_at_entry[offset_spec.atr_indicator]
        direction = 1.0 if is_take_profit else -1.0
        return reference_entry_price + direction * (atr_value * offset_spec.multiple)
    raise TypeError(f"unsupported exit offset: {type(offset_spec)!r}")


def _trailing_activation_met(
    trailing_stop: TrailingStopSpec,
    frame: pd.DataFrame,
    bar_index: int,
    position: dict[str, Any],
) -> bool:
    if trailing_stop.activation <= 0:
        return True
    if trailing_stop.type.value == "percent":
        activation_price = position["gross_entry_price"] * (1.0 + trailing_stop.activation)
        return position["highest_high"] >= activation_price

    assert trailing_stop.atr_indicator is not None
    activation_price = position["gross_entry_price"] + (
        position["atr_at_entry"][trailing_stop.atr_indicator] * trailing_stop.activation
    )
    return position["highest_high"] >= activation_price


def _trailing_price(
    trailing_stop: TrailingStopSpec,
    frame: pd.DataFrame,
    bar_index: int,
    position: dict[str, Any],
) -> float | None:
    if trailing_stop.type.value == "percent":
        return position["highest_high"] * (1.0 - trailing_stop.distance)

    assert trailing_stop.atr_indicator is not None
    atr_value = _series_value(frame, trailing_stop.atr_indicator, bar_index)
    if atr_value is None:
        return None
    return position["highest_high"] - (atr_value * trailing_stop.distance)


def _mark_to_market(cash: float, position: dict[str, Any] | None, close_price: float) -> float:
    if position is None:
        return cash
    return cash + (position["quantity"] * close_price)

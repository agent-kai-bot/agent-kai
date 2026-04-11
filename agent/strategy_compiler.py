"""YAML-to-IR compiler for ASO v1 strategies."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from agent.strategy_ir import (
    AtrExitSpec,
    ComparisonValue,
    Condition,
    ConditionOperator,
    ConstantValue,
    CostsSpec,
    EntrySpec,
    ExitSpec,
    ExitValueType,
    IndicatorBand,
    IndicatorSpec,
    IndicatorType,
    IndicatorValue,
    PercentExitSpec,
    RangeValue,
    RiskSpec,
    StrategyIR,
    TimeExitSpec,
    TrailingStopSpec,
)

_ALLOWED_TOP_LEVEL_FIELDS = {
    "name",
    "version",
    "parent",
    "symbol",
    "universe",
    "timeframe",
    "timeframes",
    "indicators",
    "entry",
    "exit",
    "risk",
    "costs",
}
_BOOLEAN_LOGIC_KEYS = {"or", "not"}
_EXECUTION_KEYS = {
    "execution",
    "order_type",
    "entry_timing",
    "exit_timing",
    "maker_taker",
    "partial_fills",
    "reduce_only_exits",
    "limit_price",
    "stop_price",
}


def compile_strategy(yaml_str: str) -> StrategyIR:
    """Compile a YAML strategy document into the typed v1 IR."""
    raw_document = yaml.safe_load(yaml_str)
    if not isinstance(raw_document, Mapping):
        raise ValueError("strategy YAML must define a mapping")

    raw_strategy = raw_document.get("strategy", raw_document)
    if not isinstance(raw_strategy, Mapping):
        raise ValueError("strategy YAML must define a strategy mapping")

    strategy = {str(key): value for key, value in raw_strategy.items()}
    _reject_unsupported_features(strategy)

    indicators = _parse_indicators(strategy.get("indicators"))
    max_warmup = max(indicator.period for indicator in indicators)
    entry = _parse_entry(strategy.get("entry"))
    exit_spec = _parse_exit(strategy.get("exit", {}), indicators)
    risk = _parse_risk(strategy.get("risk"))
    costs = _parse_costs(strategy.get("costs", {}))

    symbol = _extract_symbol(strategy)
    timeframe = _extract_timeframe(strategy)
    return StrategyIR(
        name=str(strategy.get("name", "unnamed_strategy")),
        symbol=symbol,
        timeframe=timeframe,
        indicators=indicators,
        entry=entry,
        exit=exit_spec,
        risk=risk,
        costs=costs,
        max_warmup=max_warmup,
        warmup_bars=StrategyIR.compute_warmup_bars(max_warmup),
    )


def _reject_unsupported_features(strategy: Mapping[str, Any]) -> None:
    if _contains_any_key(strategy, _EXECUTION_KEYS):
        raise ValueError("v1 uses fixed market-on-close execution")
    if _contains_key(strategy, "short"):
        raise ValueError("v1 is long-only")
    if _contains_any_key(strategy.get("entry", {}), _BOOLEAN_LOGIC_KEYS):
        raise ValueError("v1 supports AND-only conditions")

    extra_fields = set(strategy) - _ALLOWED_TOP_LEVEL_FIELDS
    if extra_fields:
        unknown = sorted(extra_fields)[0]
        raise ValueError(f"unsupported v1 field: {unknown}")

    universe = strategy.get("universe")
    if isinstance(universe, Sequence) and not isinstance(universe, (str, bytes)) and len(universe) > 1:
        raise ValueError("v1 is single-symbol")
    symbol = strategy.get("symbol")
    if isinstance(symbol, Sequence) and not isinstance(symbol, (str, bytes)) and len(symbol) > 1:
        raise ValueError("v1 is single-symbol")

    if "timeframes" in strategy:
        raise ValueError("v1 is single-timeframe")
    timeframe = strategy.get("timeframe")
    if isinstance(timeframe, Sequence) and not isinstance(timeframe, (str, bytes)) and len(timeframe) > 1:
        raise ValueError("v1 is single-timeframe")


def _extract_symbol(strategy: Mapping[str, Any]) -> str:
    if "symbol" in strategy:
        return _single_text_value(strategy["symbol"], "v1 is single-symbol")

    if "universe" in strategy:
        value = strategy["universe"]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if not value:
                raise ValueError("strategy must declare a symbol")
            return str(value[0]).strip()
        return str(value).strip()

    raise ValueError("strategy must declare a symbol")


def _extract_timeframe(strategy: Mapping[str, Any]) -> str:
    if "timeframe" not in strategy:
        raise ValueError("strategy must declare a timeframe")
    return _single_text_value(strategy["timeframe"], "v1 is single-timeframe")


def _parse_indicators(raw_indicators: Any) -> list[IndicatorSpec]:
    if not isinstance(raw_indicators, Sequence) or isinstance(raw_indicators, (str, bytes)):
        raise ValueError("indicators must be a list")
    if not raw_indicators:
        raise ValueError("indicators must define at least one indicator")

    normalized_rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for raw_indicator in raw_indicators:
        if not isinstance(raw_indicator, Mapping):
            raise ValueError("indicator definitions must be mappings")

        unexpected = set(raw_indicator) - {"name", "type", "period", "source", "alias", "band", "stddev"}
        if unexpected:
            unknown = sorted(unexpected)[0]
            raise ValueError(f"unsupported indicator field: {unknown}")

        raw_name = raw_indicator.get("type", raw_indicator.get("name"))
        if not isinstance(raw_name, str):
            raise ValueError("indicator definitions require a name")

        try:
            indicator_type = IndicatorType(raw_name.strip().lower())
        except ValueError as exc:
            raise ValueError(f"unsupported indicator: {raw_name}") from exc

        band = raw_indicator.get("band")
        if band is None and indicator_type == IndicatorType.BBANDS:
            band = "middle"

        base_name = indicator_type.value
        if indicator_type == IndicatorType.BBANDS:
            base_name = f"{base_name}_{str(band).strip().lower()}"
        counts[base_name] += 1
        normalized_rows.append(
            {
                "type": indicator_type,
                "period": raw_indicator.get("period"),
                "source": raw_indicator.get("source", "close"),
                "alias": raw_indicator.get("alias"),
                "band": band,
                "stddev": raw_indicator.get("stddev", 2.0),
                "base_name": base_name,
            }
        )

    indicators: list[IndicatorSpec] = []
    for row in normalized_rows:
        alias = row["alias"]
        if not alias:
            alias = row["base_name"] if counts[row["base_name"]] == 1 else f"{row['base_name']}_{row['period']}"

        band = row["band"]
        indicator = IndicatorSpec(
            type=row["type"],
            period=row["period"],
            alias=alias,
            source=row["source"],
            band=IndicatorBand(str(band).strip().lower()) if band is not None else None,
            stddev=row["stddev"],
        )
        indicators.append(indicator)
    return indicators


def _parse_entry(raw_entry: Any) -> EntrySpec:
    if not isinstance(raw_entry, Mapping):
        raise ValueError("entry must be a mapping")

    long_block = raw_entry.get("long", raw_entry)
    if not isinstance(long_block, Mapping):
        raise ValueError("entry.long must be a mapping")
    if _contains_any_key(long_block, _BOOLEAN_LOGIC_KEYS):
        raise ValueError("v1 supports AND-only conditions")

    conditions = long_block.get("conditions")
    if not isinstance(conditions, Sequence) or isinstance(conditions, (str, bytes)):
        raise ValueError("entry.conditions must be a list")

    return EntrySpec(conditions=[_parse_condition(condition) for condition in conditions])


def _parse_condition(raw_condition: Any) -> Condition:
    if not isinstance(raw_condition, Mapping):
        raise ValueError("conditions must be mappings")

    unexpected = set(raw_condition) - {"indicator", "operator", "value", "ref", "range"}
    if unexpected:
        unknown = sorted(unexpected)[0]
        raise ValueError(f"unsupported condition field: {unknown}")

    indicator = raw_condition.get("indicator")
    operator = raw_condition.get("operator")
    if not isinstance(indicator, str) or not isinstance(operator, str):
        raise ValueError("conditions require indicator and operator")

    operator_enum = ConditionOperator(operator.strip().lower())
    target = _parse_condition_target(operator_enum, raw_condition)
    return Condition(indicator=indicator, operator=operator_enum, target=target)


def _parse_condition_target(operator: ConditionOperator, raw_condition: Mapping[str, Any]) -> ComparisonValue | RangeValue:
    if operator == ConditionOperator.BETWEEN:
        if "range" in raw_condition:
            raw_range = raw_condition["range"]
        else:
            raw_range = raw_condition.get("value")
        return _parse_range_value(raw_range)

    if "ref" in raw_condition:
        ref = raw_condition["ref"]
        if not isinstance(ref, str):
            raise ValueError("condition ref values must be strings")
        return IndicatorValue(value=ref)
    if "value" not in raw_condition:
        raise ValueError("conditions must declare either value or ref")
    return ConstantValue(value=float(raw_condition["value"]))


def _parse_range_value(raw_range: Any) -> RangeValue:
    if isinstance(raw_range, Mapping):
        lower = raw_range.get("lower")
        upper = raw_range.get("upper")
    elif isinstance(raw_range, Sequence) and not isinstance(raw_range, (str, bytes)) and len(raw_range) == 2:
        lower, upper = raw_range
    else:
        raise ValueError("between requires a two-sided range")

    return RangeValue(lower=_parse_range_operand(lower), upper=_parse_range_operand(upper))


def _parse_range_operand(value: Any) -> ComparisonValue:
    if isinstance(value, str):
        return IndicatorValue(value=value)
    return ConstantValue(value=float(value))


def _parse_exit(raw_exit: Any, indicators: list[IndicatorSpec]) -> ExitSpec:
    if not isinstance(raw_exit, Mapping):
        raise ValueError("exit must be a mapping")

    unexpected = set(raw_exit) - {"stop_loss", "take_profit", "trailing_stop", "time_exit"}
    if unexpected:
        unknown = sorted(unexpected)[0]
        raise ValueError(f"unsupported exit field: {unknown}")

    atr_alias = _default_atr_alias(indicators)
    return ExitSpec(
        stop_loss=_parse_offset_exit(raw_exit.get("stop_loss"), atr_alias),
        take_profit=_parse_offset_exit(raw_exit.get("take_profit"), atr_alias),
        trailing_stop=_parse_trailing_stop(raw_exit.get("trailing_stop"), atr_alias),
        time_exit=_parse_time_exit(raw_exit.get("time_exit")),
    )


def _parse_offset_exit(raw_offset: Any, default_atr_alias: str | None):
    if raw_offset is None:
        return None
    if not isinstance(raw_offset, Mapping):
        raise ValueError("exit offsets must be mappings")

    raw_type = raw_offset.get("type")
    if not isinstance(raw_type, str):
        raise ValueError("exit offsets require a type")
    normalized_type = raw_type.strip().lower()
    if normalized_type in {"atr_multiple", "atr"}:
        atr_indicator = raw_offset.get("atr_indicator", default_atr_alias)
        if not isinstance(atr_indicator, str):
            raise ValueError("ATR exits require atr_indicator when multiple ATR indicators exist")
        multiple = raw_offset.get("multiple", raw_offset.get("multiplier"))
        return AtrExitSpec(atr_indicator=atr_indicator, multiple=float(multiple))
    if normalized_type == "percent":
        percent = raw_offset.get("percent", raw_offset.get("value"))
        return PercentExitSpec(percent=float(percent))
    raise ValueError(f"unsupported exit type: {raw_type}")


def _parse_trailing_stop(raw_trailing_stop: Any, default_atr_alias: str | None) -> TrailingStopSpec | None:
    if raw_trailing_stop is None:
        return None
    if not isinstance(raw_trailing_stop, Mapping):
        raise ValueError("trailing_stop must be a mapping")
    if raw_trailing_stop.get("enabled") is False:
        return None

    if "distance_atr" in raw_trailing_stop or "activation_atr" in raw_trailing_stop:
        atr_indicator = raw_trailing_stop.get("atr_indicator", default_atr_alias)
        if not isinstance(atr_indicator, str):
            raise ValueError("ATR trailing stops require atr_indicator when multiple ATR indicators exist")
        return TrailingStopSpec(
            type=ExitValueType.ATR,
            atr_indicator=atr_indicator,
            distance=float(raw_trailing_stop.get("distance_atr")),
            activation=float(raw_trailing_stop.get("activation_atr", 0.0)),
        )
    if "distance_pct" in raw_trailing_stop or "activation_pct" in raw_trailing_stop:
        return TrailingStopSpec(
            type=ExitValueType.PERCENT,
            distance=float(raw_trailing_stop.get("distance_pct")),
            activation=float(raw_trailing_stop.get("activation_pct", 0.0)),
        )

    raw_type = raw_trailing_stop.get("type")
    if not isinstance(raw_type, str):
        raise ValueError("trailing_stop requires a type")
    trailing_type = ExitValueType(raw_type.strip().lower())
    return TrailingStopSpec(
        type=trailing_type,
        atr_indicator=raw_trailing_stop.get("atr_indicator", default_atr_alias),
        distance=float(raw_trailing_stop.get("distance")),
        activation=float(raw_trailing_stop.get("activation", 0.0)),
    )


def _parse_time_exit(raw_time_exit: Any) -> TimeExitSpec | None:
    if raw_time_exit is None:
        return None
    if not isinstance(raw_time_exit, Mapping):
        raise ValueError("time_exit must be a mapping")
    return TimeExitSpec(max_bars=int(raw_time_exit.get("max_bars")))


def _parse_risk(raw_risk: Any) -> RiskSpec:
    if not isinstance(raw_risk, Mapping):
        raise ValueError("risk must be a mapping")

    unexpected = set(raw_risk) - {"max_position_pct", "max_drawdown_pct"}
    if unexpected:
        unknown = sorted(unexpected)[0]
        raise ValueError(f"unsupported risk field: {unknown}")

    return RiskSpec(
        max_position_pct=float(raw_risk.get("max_position_pct")),
        max_drawdown_pct=float(raw_risk.get("max_drawdown_pct")),
    )


def _parse_costs(raw_costs: Any) -> CostsSpec:
    if not isinstance(raw_costs, Mapping):
        raise ValueError("costs must be a mapping")

    unexpected = set(raw_costs) - {"fee_pct", "slippage_pct", "spread_pct"}
    if unexpected:
        unknown = sorted(unexpected)[0]
        raise ValueError(f"unsupported costs field: {unknown}")

    return CostsSpec(
        fee_pct=float(raw_costs.get("fee_pct", 0.0)),
        slippage_pct=float(raw_costs.get("slippage_pct", 0.0)),
        spread_pct=float(raw_costs.get("spread_pct", 0.0)),
    )


def _default_atr_alias(indicators: list[IndicatorSpec]) -> str | None:
    atr_aliases = [indicator.alias for indicator in indicators if indicator.type == IndicatorType.ATR]
    if not atr_aliases:
        return None
    if len(atr_aliases) == 1:
        return atr_aliases[0]
    return None


def _single_text_value(value: Any, error_message: str) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 1:
            raise ValueError(error_message)
        value = value[0]
    if not isinstance(value, str):
        raise ValueError(error_message)
    return value.strip()


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, Mapping):
        for current_key, current_value in value.items():
            if str(current_key).strip().lower() == key:
                return True
            if _contains_key(current_value, key):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_key(item, key) for item in value)
    return False


def _contains_any_key(value: Any, keys: set[str]) -> bool:
    return any(_contains_key(value, key) for key in keys)

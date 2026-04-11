"""Typed strategy intermediate representation for ASO v1."""

from __future__ import annotations

from enum import StrEnum
from math import ceil
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BUILTIN_SERIES = frozenset({"open", "high", "low", "close", "volume"})

PositiveFraction = Annotated[float, Field(gt=0.0, le=1.0)]
NonNegativeFraction = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
NonNegativeFloat = Annotated[float, Field(ge=0.0)]


class IndicatorType(StrEnum):
    RSI = "rsi"
    EMA = "ema"
    SMA = "sma"
    ATR = "atr"
    BBANDS = "bbands"


class IndicatorBand(StrEnum):
    UPPER = "upper"
    MIDDLE = "middle"
    LOWER = "lower"


class ConditionOperator(StrEnum):
    ABOVE = "above"
    BELOW = "below"
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"
    BETWEEN = "between"


class ExitValueType(StrEnum):
    PERCENT = "percent"
    ATR = "atr"


class ConstantValue(BaseModel):
    """Numeric comparison operand."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["constant"] = "constant"
    value: float


class IndicatorValue(BaseModel):
    """Indicator or series comparison operand."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["indicator"] = "indicator"
    value: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip().lower()


ComparisonValue = Annotated[ConstantValue | IndicatorValue, Field(discriminator="kind")]


class RangeValue(BaseModel):
    """Target range for the ``between`` operator."""

    model_config = ConfigDict(extra="forbid")

    lower: ComparisonValue
    upper: ComparisonValue


class IndicatorSpec(BaseModel):
    """One computed indicator series exposed to entry/exit rules."""

    model_config = ConfigDict(extra="forbid")

    type: IndicatorType
    period: int = Field(gt=0)
    alias: str = Field(min_length=1)
    source: Literal["close"] = "close"
    band: IndicatorBand | None = None
    stddev: PositiveFloat = 2.0

    @field_validator("alias")
    @classmethod
    def normalize_alias(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_shape(self) -> IndicatorSpec:
        if self.type == IndicatorType.BBANDS and self.band is None:
            raise ValueError("BBANDS indicators must declare a band")
        if self.type != IndicatorType.BBANDS and self.band is not None:
            raise ValueError("Only BBANDS indicators may declare a band")
        return self


class Condition(BaseModel):
    """Single AND-ed entry condition."""

    model_config = ConfigDict(extra="forbid")

    indicator: str = Field(min_length=1)
    operator: ConditionOperator
    target: ComparisonValue | RangeValue

    @field_validator("indicator")
    @classmethod
    def normalize_indicator(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_target(self) -> Condition:
        is_range = isinstance(self.target, RangeValue)
        if self.operator == ConditionOperator.BETWEEN and not is_range:
            raise ValueError("between requires a lower and upper target")
        if self.operator != ConditionOperator.BETWEEN and is_range:
            raise ValueError(f"{self.operator.value} does not accept a range target")
        return self


class EntrySpec(BaseModel):
    """v1 entry block: implicit AND over all conditions."""

    model_config = ConfigDict(extra="forbid")

    conditions: list[Condition] = Field(min_length=1)


class PercentExitSpec(BaseModel):
    """Percent-based stop or target."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["percent"] = "percent"
    percent: PositiveFraction


class AtrExitSpec(BaseModel):
    """ATR-multiple stop or target."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["atr"] = "atr"
    atr_indicator: str = Field(min_length=1)
    multiple: PositiveFloat

    @field_validator("atr_indicator")
    @classmethod
    def normalize_indicator(cls, value: str) -> str:
        return value.strip().lower()


ExitOffsetSpec = Annotated[PercentExitSpec | AtrExitSpec, Field(discriminator="type")]


class TrailingStopSpec(BaseModel):
    """Trailing-stop configuration, active only after optional activation."""

    model_config = ConfigDict(extra="forbid")

    type: ExitValueType
    distance: PositiveFloat
    activation: NonNegativeFloat = 0.0
    atr_indicator: str | None = None

    @field_validator("atr_indicator")
    @classmethod
    def normalize_atr_indicator(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else value

    @model_validator(mode="after")
    def validate_shape(self) -> TrailingStopSpec:
        if self.type == ExitValueType.PERCENT:
            if self.atr_indicator is not None:
                raise ValueError("percent trailing stops do not accept atr_indicator")
            if self.distance > 1 or self.activation > 1:
                raise ValueError("percent trailing stop fields must be between 0 and 1")
        else:
            if not self.atr_indicator:
                raise ValueError("ATR trailing stops require atr_indicator")
        return self


class TimeExitSpec(BaseModel):
    """Close the position after a fixed number of bars."""

    model_config = ConfigDict(extra="forbid")

    max_bars: int = Field(gt=0)


class ExitSpec(BaseModel):
    """Supported v1 exits."""

    model_config = ConfigDict(extra="forbid")

    stop_loss: ExitOffsetSpec | None = None
    take_profit: ExitOffsetSpec | None = None
    trailing_stop: TrailingStopSpec | None = None
    time_exit: TimeExitSpec | None = None


class RiskSpec(BaseModel):
    """Portfolio-level risk controls carried in the IR."""

    model_config = ConfigDict(extra="forbid")

    max_position_pct: PositiveFraction
    max_drawdown_pct: PositiveFraction


class CostsSpec(BaseModel):
    """Frozen v1 cost inputs."""

    model_config = ConfigDict(extra="forbid")

    fee_pct: NonNegativeFraction = 0.0
    slippage_pct: NonNegativeFraction = 0.0
    spread_pct: NonNegativeFraction = 0.0


class StrategyIR(BaseModel):
    """Typed executable subset of the ASO YAML strategy schema."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    indicators: list[IndicatorSpec] = Field(min_length=1)
    entry: EntrySpec
    exit: ExitSpec
    risk: RiskSpec
    costs: CostsSpec = Field(default_factory=CostsSpec)
    max_warmup: int = Field(ge=0)
    warmup_bars: int = Field(ge=0)

    @field_validator("symbol", "timeframe", "name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_references(self) -> StrategyIR:
        aliases = {indicator.alias: indicator for indicator in self.indicators}
        if len(aliases) != len(self.indicators):
            raise ValueError("indicator aliases must be unique")

        valid_series = set(aliases) | set(BUILTIN_SERIES)
        for condition in self.entry.conditions:
            if condition.indicator not in valid_series:
                raise ValueError(f"unknown indicator reference: {condition.indicator}")
            self._validate_target(condition.target, valid_series)

        atr_aliases = {name for name, indicator in aliases.items() if indicator.type == IndicatorType.ATR}
        self._validate_exit_offset(self.exit.stop_loss, atr_aliases)
        self._validate_exit_offset(self.exit.take_profit, atr_aliases)
        if self.exit.trailing_stop and self.exit.trailing_stop.type == ExitValueType.ATR:
            assert self.exit.trailing_stop.atr_indicator is not None
            if self.exit.trailing_stop.atr_indicator not in atr_aliases:
                raise ValueError(
                    f"ATR trailing stop references unknown ATR indicator: {self.exit.trailing_stop.atr_indicator}"
                )

        expected_warmup = self.compute_warmup_bars(self.max_warmup)
        if self.warmup_bars != expected_warmup:
            raise ValueError(
                f"warmup_bars must equal ceil(max_warmup * 1.5); expected {expected_warmup}, got {self.warmup_bars}"
            )
        return self

    @staticmethod
    def compute_warmup_bars(max_warmup: int) -> int:
        return ceil(max_warmup * 1.5)

    @staticmethod
    def _validate_target(target: ComparisonValue | RangeValue, valid_series: set[str]) -> None:
        if isinstance(target, RangeValue):
            StrategyIR._validate_target(target.lower, valid_series)
            StrategyIR._validate_target(target.upper, valid_series)
            return
        if isinstance(target, IndicatorValue) and target.value not in valid_series:
            raise ValueError(f"unknown indicator reference: {target.value}")

    @staticmethod
    def _validate_exit_offset(offset: ExitOffsetSpec | None, atr_aliases: set[str]) -> None:
        if isinstance(offset, AtrExitSpec) and offset.atr_indicator not in atr_aliases:
            raise ValueError(f"ATR exit references unknown ATR indicator: {offset.atr_indicator}")

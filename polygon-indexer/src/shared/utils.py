from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Iterator, Sequence, TypeVar

from .events import INTERVAL_SECONDS

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_iso8601(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_period(value: str | None, default: timedelta) -> timedelta:
    if not value:
        return default
    suffix = value[-1].lower()
    amount = int(value[:-1])
    if suffix == "m":
        return timedelta(minutes=amount)
    if suffix == "h":
        return timedelta(hours=amount)
    if suffix == "d":
        return timedelta(days=amount)
    raise ValueError(f"unsupported period: {value}")


def bucket_start(timestamp: datetime, interval: str) -> datetime:
    seconds = INTERVAL_SECONDS[interval]
    unix = int(timestamp.timestamp())
    aligned = unix - (unix % seconds)
    return datetime.fromtimestamp(aligned, tz=UTC)


def chunked(values: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def chunk_range(start: int, end: int, size: int) -> Iterator[tuple[int, int]]:
    current = start
    while current <= end:
        upper = min(current + size - 1, end)
        yield current, upper
        current = upper + 1


def decimal_to_json(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def compute_gini(values: Iterable[Decimal]) -> Decimal | None:
    ordered = sorted((value for value in values if value > 0), reverse=False)
    count = len(ordered)
    if count == 0:
        return None
    total = sum(ordered, Decimal(0))
    if total == 0:
        return Decimal(0)
    weighted = Decimal(0)
    for index, value in enumerate(ordered, start=1):
        weighted += Decimal(index) * value
    gini = (Decimal(2) * weighted) / (Decimal(count) * total) - (Decimal(count + 1) / Decimal(count))
    return max(gini, Decimal(0))


def now_date() -> date:
    return utcnow().date()


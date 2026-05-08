"""Shadow-mode diff metrics for the daemon signal router."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class DivergenceKind(str, Enum):
    """Supported legacy-vs-router comparison outcomes."""

    AGREED_FIRE = "agreed_fire"
    AGREED_SUPPRESS = "agreed_suppress"
    LEGACY_FIRED_ROUTER_SUPPRESSED = "legacy_fired_router_suppressed"
    LEGACY_SUPPRESSED_ROUTER_FIRED = "legacy_suppressed_router_fired"
    MATCH_DIVERGED = "match_diverged"
    COOLDOWN_SKEW = "cooldown_skew"
    CAP_SKEW = "cap_skew"


DIVERGENCE_KINDS: tuple[DivergenceKind, ...] = tuple(DivergenceKind)


@dataclass(frozen=True)
class DiffMetric:
    """One shadow comparison between the legacy and router decisions."""

    route_name: str
    legacy_decision: str
    router_decision: str
    divergence_kind: DivergenceKind
    details: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready payload for health and telemetry."""

        return {
            "ts": self.ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "route_name": self.route_name,
            "legacy_decision": self.legacy_decision,
            "router_decision": self.router_decision,
            "divergence_kind": self.divergence_kind.value,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DiffMetric":
        """Rehydrate a metric from its serialized form."""

        raw_ts = str(payload.get("ts") or "")
        if raw_ts.endswith("Z"):
            raw_ts = raw_ts[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(raw_ts)
        except ValueError:
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return cls(
            route_name=str(payload.get("route_name") or ""),
            legacy_decision=str(payload.get("legacy_decision") or ""),
            router_decision=str(payload.get("router_decision") or ""),
            divergence_kind=DivergenceKind(str(payload.get("divergence_kind") or "")),
            details=dict(payload.get("details") or {}),
            ts=ts,
        )


class DiffMetricStore:
    """In-memory rolling counters for shadow-mode diff decisions."""

    def __init__(self, *, max_samples: int = 10000) -> None:
        self.max_samples = max(1, int(max_samples))
        self._samples: deque[DiffMetric] = deque(maxlen=self.max_samples)

    def record(self, metric: DiffMetric) -> None:
        """Record one diff metric."""

        self._samples.append(metric)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return the health payload expected by `/api/health.signal_router`."""

        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        rolling_1h = self._counts_since(reference.timestamp() - 3600)
        rolling_24h = self._counts_since(reference.timestamp() - 86400)
        by_route: dict[str, Counter[str]] = {}
        for metric in self._samples:
            if metric.ts.timestamp() < reference.timestamp() - 86400:
                continue
            route_counts = by_route.setdefault(metric.route_name, Counter())
            route_counts[metric.divergence_kind.value] += 1
        divergent = [
            metric
            for metric in reversed(self._samples)
            if metric.divergence_kind
            not in {DivergenceKind.AGREED_FIRE, DivergenceKind.AGREED_SUPPRESS}
        ][:5]
        return {
            "rolling_1h": _complete_counts(rolling_1h),
            "rolling_24h": _complete_counts(rolling_24h),
            "by_route": {
                route: _complete_counts(counts)
                for route, counts in sorted(by_route.items())
            },
            "last_diff_sample": [metric.to_dict() for metric in divergent],
        }

    def _counts_since(self, cutoff_epoch: float) -> Counter[str]:
        counts: Counter[str] = Counter()
        for metric in self._samples:
            if metric.ts.timestamp() >= cutoff_epoch:
                counts[metric.divergence_kind.value] += 1
        return counts


def _complete_counts(counts: Counter[str]) -> dict[str, int]:
    return {kind.value: int(counts.get(kind.value, 0)) for kind in DIVERGENCE_KINDS}

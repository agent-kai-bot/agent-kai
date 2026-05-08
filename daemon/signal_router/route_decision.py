"""Dry-run route decision types for the daemon signal router."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .domain_model import ActionDescriptor

DEDUP_STATUS_FIRED = "fired"
DEDUP_STATUS_SUPPRESSED_COOLDOWN = "suppressed_cooldown"
DEDUP_STATUS_SUPPRESSED_DAILY_CAP = "suppressed_daily_cap"
DEDUP_STATUS_SUPPRESSED_HOURLY_CAP = "suppressed_hourly_cap"
DEDUP_STATUS_WOULD_HAVE_FIRED_IN_SHADOW = "would_have_fired_in_shadow"

DEDUP_STATUS_VALUES: tuple[str, ...] = (
    DEDUP_STATUS_FIRED,
    DEDUP_STATUS_SUPPRESSED_COOLDOWN,
    DEDUP_STATUS_SUPPRESSED_DAILY_CAP,
    DEDUP_STATUS_SUPPRESSED_HOURLY_CAP,
    DEDUP_STATUS_WOULD_HAVE_FIRED_IN_SHADOW,
)


@dataclass(frozen=True)
class MatchResult:
    """Matcher output and its human-readable reason."""

    matched: bool
    reason: str
    details: dict[str, Any] | None = None

    def audit_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "matched": self.matched,
            "reason": self.reason,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class RouteDecision:
    """Immutable decision record produced by route evaluation."""

    route_name: str
    channel: str
    match_result: MatchResult
    actions: list[ActionDescriptor]
    decided_at: datetime
    dedup_key: str | None
    dedup_status: str | None

    def audit_payload(self) -> dict[str, Any]:
        """Return the Phase 5 JSONL audit-log payload shape."""

        return {
            "route_name": self.route_name,
            "channel": self.channel,
            "match_result": self.match_result.audit_payload(),
            "actions": [
                {
                    "kind": action.kind,
                    "target": action.target,
                    "params": action.params,
                }
                for action in self.actions
            ],
            "decided_at": self.decided_at.isoformat(),
            "dedup_key": self.dedup_key,
            "dedup_status": self.dedup_status,
        }

"""Daemon-owned unified signal router skeleton."""

from .dedup_table import RouterDedupTable
from .domain_model import ActionDescriptor, Channel, Route
from .feature_flags import SignalRouterMode, kill_switch_active, resolve_mode
from .route_decision import MatchResult, RouteDecision
from .router import SignalRouter, legacy_cooldown_key, route_matches
from .actions import EXECUTORS, ActionResult, ExecutionContext, ValidationError

__all__ = [
    "ActionDescriptor",
    "ActionResult",
    "Channel",
    "EXECUTORS",
    "ExecutionContext",
    "MatchResult",
    "Route",
    "RouteDecision",
    "RouterDedupTable",
    "SignalRouter",
    "SignalRouterMode",
    "ValidationError",
    "kill_switch_active",
    "legacy_cooldown_key",
    "route_matches",
    "resolve_mode",
]

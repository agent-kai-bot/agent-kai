"""Daemon-owned unified signal router skeleton."""

from .dedup_table import RouterDedupTable
from .domain_model import ActionDescriptor, Channel, Route
from .feature_flags import SignalRouterMode, kill_switch_active, resolve_mode
from .route_decision import MatchResult, RouteDecision
from .router import SignalRouter

__all__ = [
    "ActionDescriptor",
    "Channel",
    "MatchResult",
    "Route",
    "RouteDecision",
    "RouterDedupTable",
    "SignalRouter",
    "SignalRouterMode",
    "kill_switch_active",
    "resolve_mode",
]

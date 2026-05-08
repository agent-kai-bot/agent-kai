"""Dormant Phase 1 coordinator for the daemon signal router."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_logger import get_logger

from .dedup_table import RouterDedupTable
from .domain_model import ActionDescriptor, Channel, Route
from .feature_flags import SignalRouterMode, kill_switch_active, resolve_mode
from .route_decision import RouteDecision


class SignalRouter:
    """Main signal router coordinator.

    Phase 1 only loads config and exposes lookup/health surfaces. Subscription,
    matching, and action execution land in later phases.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        dedup_table: RouterDedupTable | None = None,
        log_info: Callable[[str], None] | None = None,
        log_debug: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config or {}
        self.mode: SignalRouterMode = resolve_mode(self.config)
        self.dedup_table = dedup_table or RouterDedupTable(
            self.config.get("dedup_table_path")
        )
        self.log = get_logger("daemon.signal_router")
        self.log_info = log_info or (lambda message: self.log.info("%s", message))
        self.log_debug = log_debug or (lambda message: self.log.debug("%s", message))
        self.channels: dict[str, Channel] = self._load_channels(self.config)
        self.routes: dict[str, Route] = self._load_routes(self.config)
        self.log_info(
            "signal_router loaded "
            f"mode={self.mode.value} routes={len(self.routes)} "
            f"channels={len(self.channels)}"
        )

    def route(self, envelope: dict[str, Any]) -> RouteDecision | None:
        """Phase 1 stub. Phase 2 adds shim-backed routing."""

        self.log_debug(f"signal_router route stub ignored envelope={bool(envelope)}")
        return None

    def find_channel_for_subject(self, subject: str) -> Channel | None:
        """Return the first configured channel whose NATS pattern matches subject."""

        for channel in self.channels.values():
            if any(_subject_matches(pattern, subject) for pattern in channel.subjects):
                return channel
        return None

    def health_payload(self) -> dict[str, Any]:
        """Return the Phase 1 health shape."""

        return {
            "mode": self.mode.value,
            "routes_loaded": len(self.routes),
            "channels_loaded": len(self.channels),
            "dedup_keys_count": self.dedup_table.count_keys(),
            "kill_switch_active": kill_switch_active(),
        }

    def _load_channels(self, config: dict[str, Any]) -> dict[str, Channel]:
        raw_channels = config.get("channels", {})
        channels: dict[str, Channel] = {}
        if not isinstance(raw_channels, dict):
            return channels
        for name, raw_channel in raw_channels.items():
            if not isinstance(raw_channel, dict):
                continue
            raw_subjects = raw_channel.get("subjects", [])
            subjects = [str(subject) for subject in raw_subjects if isinstance(subject, str)]
            channels[str(name)] = Channel(
                name=str(name),
                subjects=subjects,
                schema=(
                    str(raw_channel["schema"])
                    if raw_channel.get("schema") is not None
                    else None
                ),
            )
        return channels

    def _load_routes(self, config: dict[str, Any]) -> dict[str, Route]:
        raw_routes = config.get("routes", [])
        routes: dict[str, Route] = {}
        if not isinstance(raw_routes, list):
            return routes
        for raw_route in raw_routes:
            if not isinstance(raw_route, dict):
                continue
            name = str(raw_route.get("name", "")).strip()
            if not name:
                continue
            raw_actions = raw_route.get("actions", [])
            actions = [
                _action_from_config(raw_action)
                for raw_action in raw_actions
                if isinstance(raw_action, dict)
            ]
            routes[name] = Route(
                name=name,
                channel=str(raw_route.get("channel", "")),
                match=dict(raw_route.get("match") or {}),
                actions=actions,
                pre_action=(
                    dict(raw_route["pre_action"])
                    if isinstance(raw_route.get("pre_action"), dict)
                    else None
                ),
                enabled=bool(raw_route.get("enabled", True)),
            )
        return routes


def _action_from_config(raw_action: dict[str, Any]) -> ActionDescriptor:
    reserved = {"kind", "target"}
    params = {
        key: value
        for key, value in raw_action.items()
        if key not in reserved
    }
    raw_params = raw_action.get("params")
    if isinstance(raw_params, dict):
        params.update(raw_params)
    return ActionDescriptor(
        kind=str(raw_action.get("kind", "")),
        target=(
            str(raw_action["target"])
            if raw_action.get("target") is not None
            else None
        ),
        params=params,
    )


def _subject_matches(pattern: str, subject: str) -> bool:
    pattern_tokens = pattern.split(".")
    subject_tokens = subject.split(".")
    return _tokens_match(pattern_tokens, subject_tokens)


def _tokens_match(pattern_tokens: list[str], subject_tokens: list[str]) -> bool:
    for index, token in enumerate(pattern_tokens):
        if token == ">":
            return index == len(pattern_tokens) - 1
        if index >= len(subject_tokens):
            return False
        if token != "*" and token != subject_tokens[index]:
            return False
    return len(subject_tokens) == len(pattern_tokens)

"""Dormant Phase 1 coordinator for the daemon signal router."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from agent_logger import get_logger

from .agent_pack import load_and_register_pack_role
from .actions import EXECUTORS, ActionResult, ExecutionContext
from .actions.base import ACTION_STATUS_FAILED
from .dedup_table import RouterDedupTable
from .domain_model import ActionDescriptor, Channel, Route
from .feature_flags import SignalRouterMode, kill_switch_active, resolve_mode
from .route_decision import MatchResult, RouteDecision

try:
    from agent.signal_handlers import SignalHandler, matches as legacy_signal_matches
except Exception:  # pragma: no cover - import should be available in daemon runtime
    SignalHandler = None  # type: ignore[assignment]
    legacy_signal_matches = None  # type: ignore[assignment]


class SignalRouter:
    """Main signal router coordinator.

    Phase 1 only loads config and exposes lookup/health surfaces. Subscription,
    matching, and action execution land in later phases.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        routes: list[Route] | None = None,
        channels: list[Channel] | None = None,
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
        if channels:
            self.channels.update({channel.name: channel for channel in channels})
        self.routes: dict[str, Route] = self._load_routes(self.config)
        if routes:
            self.routes.update({route.name: route for route in routes})
        self.log_info(
            "signal_router loaded "
            f"mode={self.mode.value} routes={len(self.routes)} "
            f"channels={len(self.channels)}"
        )
        self.action_executors = dict(EXECUTORS)

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

    def match_route(self, route: Route, payload: Any) -> MatchResult:
        """Evaluate a route match expression with legacy signal-handler parity."""

        try:
            matched = route_matches(route, payload)
        except Exception as exc:  # noqa: BLE001
            return MatchResult(False, "match_error", {"error": str(exc)})
        return MatchResult(
            matched,
            "matched" if matched else "match_failed",
            {"route_name": route.name},
        )

    def execute_actions(
        self,
        decision: RouteDecision,
        envelope: dict[str, Any],
        context: ExecutionContext | None = None,
    ) -> list[ActionResult]:
        """Execute route actions according to router cutover mode."""

        if self.mode == SignalRouterMode.LEGACY:
            return []

        execution_context = context or ExecutionContext()
        execution_context = replace(
            execution_context,
            dry_run=self.mode == SignalRouterMode.SHADOW,
            channel=execution_context.channel or decision.channel,
            route_name=execution_context.route_name or decision.route_name,
            subject=execution_context.subject or envelope.get("subject"),
            dedup_table=execution_context.dedup_table or self.dedup_table,
        )
        results: list[ActionResult] = []
        for action in decision.actions:
            executor = self.action_executors.get(action.kind)
            if executor is None:
                result = ActionResult(
                    kind=action.kind,
                    target=action.target,
                    status=ACTION_STATUS_FAILED,
                    detail=f"unknown signal_router action kind: {action.kind}",
                    metrics={},
                )
                results.append(result)
                continue
            try:
                result = executor.execute(action, envelope, execution_context)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "signal_router action failed route=%s kind=%s target=%s error=%s",
                    decision.route_name,
                    action.kind,
                    action.target,
                    exc,
                )
                result = ActionResult(
                    kind=action.kind,
                    target=action.target,
                    status=ACTION_STATUS_FAILED,
                    detail=str(exc),
                    metrics={},
                )
            results.append(result)
        return results

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
            self._register_spawn_agent_packs(actions)
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
                cooldown_seconds=int(raw_route.get("cooldown_seconds", 0) or 0),
                requires_autotrade=bool(raw_route.get("requires_autotrade", False)),
                config={
                    key: value
                    for key, value in raw_route.items()
                    if key
                    not in {
                        "name",
                        "channel",
                        "match",
                        "actions",
                        "pre_action",
                        "enabled",
                        "cooldown_seconds",
                        "requires_autotrade",
                    }
                },
            )
        return routes

    def _register_spawn_agent_packs(self, actions: list[ActionDescriptor]) -> None:
        packs_dir = self.config.get("agent_packs_dir")
        for action in actions:
            if action.kind != "spawn_agent":
                continue
            pack_name = action.params.get("pack") or action.target
            if not pack_name:
                continue
            outcome = load_and_register_pack_role(
                str(pack_name),
                packs_dir=packs_dir,
                logger=self.log,
            )
            self.log_info(
                "signal_router agent-pack role "
                f"pack={outcome.pack_name} role={outcome.role_name} status={outcome.status}"
            )


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


def route_matches(route: Route, payload: Any) -> bool:
    """Return whether payload matches route.match using the legacy matcher."""

    if legacy_signal_matches is None or SignalHandler is None:
        raise RuntimeError("legacy signal matcher is unavailable")
    handler = SignalHandler(name=route.name, match=route.match)
    return bool(legacy_signal_matches(handler, payload))


def legacy_cooldown_key(route_name: str, symbol: str | None) -> tuple[str, str]:
    """Return the legacy per-handler cooldown key shape."""

    return (route_name, (symbol or "").upper())


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

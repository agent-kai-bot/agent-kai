"""Backward-compatible translator for top-level ``signal_handlers[]``."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent.signal_handlers import (
    ACTION_CHAT_MESSAGE,
    ACTION_DISPATCH_AGENT,
    ACTION_DISPATCH_KAI,
    ACTION_PUBLISH,
    ACTION_WEBHOOK,
    SignalHandler,
    matches as legacy_matches,
)
from config import AGENTS, PROJECT_ROOT

from .domain_model import ActionDescriptor, Route
from .feature_flags import SignalRouterMode, resolve_mode
from .router import legacy_cooldown_key, route_matches

log = logging.getLogger(__name__)

SHIM_SOURCE = "signal_handlers"
LEGACY_ROUTE_PREFIX = "legacy:"


@dataclass(frozen=True)
class ShimError:
    """Validation issue found while translating compatibility routes."""

    mode: str
    index: int | None
    name: str | None
    message: str
    severity: str = "error"
    enabled: bool = True


class ShimValidationError(RuntimeError):
    """Raised when shim validation must fail daemon startup."""

    def __init__(self, errors: list[ShimError]) -> None:
        self.errors = errors
        joined = "; ".join(error.message for error in errors)
        super().__init__(f"signal_router shim validation failed: {joined}")


@dataclass(frozen=True)
class LegacyActionSpec:
    required_fields: tuple[str, ...]
    build: Callable[[dict[str, Any]], ActionDescriptor]


def _dispatch_agent_action(raw: dict[str, Any]) -> ActionDescriptor:
    return ActionDescriptor(
        kind="inject_session",
        target=str(raw.get("agent") or ""),
        params={
            "template_inline": raw.get("task_template", ""),
            "legacy_action": ACTION_DISPATCH_AGENT,
        },
    )


def _dispatch_kai_action(raw: dict[str, Any]) -> ActionDescriptor:
    return ActionDescriptor(
        kind="inject_session",
        target="kai",
        params={
            "template_inline": raw.get("task_template") or raw.get("template", ""),
            "legacy_action": ACTION_DISPATCH_KAI,
        },
    )


def _chat_message_action(raw: dict[str, Any]) -> ActionDescriptor:
    return ActionDescriptor(
        kind="notify",
        target="chat",
        params={
            "template_inline": raw.get("template", ""),
            "legacy_action": ACTION_CHAT_MESSAGE,
        },
    )


def _publish_action(raw: dict[str, Any]) -> ActionDescriptor:
    params: dict[str, Any] = {
        "subject": raw.get("subject", ""),
        "legacy_action": ACTION_PUBLISH,
    }
    if raw.get("template"):
        params["template_inline"] = raw.get("template")
    else:
        params["raw_event"] = True
    return ActionDescriptor(kind="notify", target="nats", params=params)


def _webhook_action(raw: dict[str, Any]) -> ActionDescriptor:
    return ActionDescriptor(
        kind="notify",
        target="webhook",
        params={
            "url": raw.get("url", ""),
            "template_inline": raw.get("template", ""),
            "legacy_action": ACTION_WEBHOOK,
        },
    )


LEGACY_ACTION_SPECS: dict[str, LegacyActionSpec] = {
    ACTION_DISPATCH_AGENT: LegacyActionSpec(("agent",), _dispatch_agent_action),
    ACTION_DISPATCH_KAI: LegacyActionSpec((), _dispatch_kai_action),
    ACTION_CHAT_MESSAGE: LegacyActionSpec((), _chat_message_action),
    ACTION_PUBLISH: LegacyActionSpec(("subject",), _publish_action),
    ACTION_WEBHOOK: LegacyActionSpec(("url",), _webhook_action),
}


def translate_signal_handlers_config(
    config: dict[str, Any],
    *,
    mode: str | SignalRouterMode | None = None,
    project_root: str | Path | None = None,
) -> tuple[list[Route], list[ShimError]]:
    """Translate top-level legacy signal handlers into router routes."""

    mode_value = _mode_value(mode, config)
    root = Path(project_root or PROJECT_ROOT)
    raw_handlers = config.get("signal_handlers") or []
    if not isinstance(raw_handlers, list):
        return [], [
            _error(
                mode_value,
                None,
                None,
                f"signal_handlers must be a list, got {type(raw_handlers).__name__}",
                enabled=True,
            )
        ]

    routes: list[Route] = []
    errors: list[ShimError] = []
    for index, raw in enumerate(raw_handlers):
        route, handler_errors = _translate_one(raw, index, config, mode_value, root)
        errors.extend(handler_errors)
        if route is not None:
            routes.append(route)
    return routes, errors


translate_legacy_signal_handlers = translate_signal_handlers_config


def raise_for_startup_errors(errors: list[ShimError], mode: str | SignalRouterMode) -> None:
    """Fail startup when the current router mode requires strict shim validity."""

    mode_value = _mode_value(mode)
    if mode_value == SignalRouterMode.LEGACY.value:
        return
    fatal = [
        error
        for error in errors
        if error.severity == "error" and error.enabled
    ]
    if fatal:
        raise ShimValidationError(fatal)


def log_shim_errors(errors: list[ShimError], logger: logging.Logger | None = None) -> None:
    """Log shim validation issues with warning/error severity."""

    target = logger or log
    for error in errors:
        message = (
            f"signal_router {SHIM_SOURCE} shim {error.severity}: "
            f"index={error.index} name={error.name!r} {error.message}"
        )
        if error.severity == "error":
            target.error("%s", message)
        else:
            target.warning("%s", message)


def effective_autotrade_gate(raw: dict[str, Any]) -> bool:
    """Return the legacy effective autotrade gate for a handler."""

    if bool(raw.get("requires_autotrade", False)):
        return True
    return (
        raw.get("action") == ACTION_DISPATCH_AGENT
        and str(raw.get("agent") or "").lower() == "trader"
    )


def route_cooldown_key(route: Route, symbol: str | None) -> tuple[str, str]:
    """Expose the preserved legacy cooldown key format for tests/callers."""

    return legacy_cooldown_key(route.name, symbol)


def generate_parity_fixtures(raw: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Generate representative matching and non-matching events for a handler."""

    match = raw.get("match") or {}
    positive = {
        "source": "signal-scanner",
        "strategy": "shim_strategy",
        "symbol": "BTC",
        "signal_type": "BUY",
        "price": 100.0,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    if isinstance(match, dict):
        for key, expected in match.items():
            chosen = expected[0] if isinstance(expected, list) and expected else expected
            _assign_fixture_value(positive, str(key), chosen)

    fixtures = [("positive", positive)]
    if isinstance(match, dict) and match:
        negative = dict(positive)
        first_key = next(iter(match))
        _assign_fixture_value(negative, str(first_key), _non_matching_value(match[first_key]))
        fixtures.append(("negative", negative))
    return fixtures


def _translate_one(
    raw: Any,
    index: int,
    config: dict[str, Any],
    mode: str,
    project_root: Path,
) -> tuple[Route | None, list[ShimError]]:
    if not isinstance(raw, dict):
        return None, [
            _error(
                mode,
                index,
                None,
                f"handler entry must be a dict, got {type(raw).__name__}",
                enabled=True,
            )
        ]

    name = str(raw.get("name") or "unnamed")
    enabled = bool(raw.get("enabled", True))
    errors: list[ShimError] = []
    action = raw.get("action") or ACTION_CHAT_MESSAGE
    spec = LEGACY_ACTION_SPECS.get(action)
    if spec is None:
        errors.append(
            _error(mode, index, name, f"unknown action {action!r}", enabled=enabled)
        )
        return None, errors

    errors.extend(_validate_required_fields(raw, spec, index, name, mode, enabled))
    errors.extend(_validate_target(raw, config, index, name, mode, enabled))
    errors.extend(_validate_templates(raw, index, name, mode, enabled, project_root))
    errors.extend(_validate_match(raw, index, name, mode, enabled))
    if errors:
        return None, errors

    action_descriptor = spec.build(raw)
    route = Route(
        name=f"{LEGACY_ROUTE_PREFIX}{name}",
        channel=_infer_channel(raw.get("match") or {}),
        match=dict(raw.get("match") or {}),
        actions=[action_descriptor],
        pre_action=None,
        enabled=enabled,
        cooldown_seconds=int(raw.get("cooldown_seconds", 0) or 0),
        requires_autotrade=effective_autotrade_gate(raw),
        config={
            "source_compat": SHIM_SOURCE,
            "legacy_action": action,
        },
    )
    errors.extend(_validate_safety(route, index, name, mode, enabled))
    errors.extend(_validate_parity(raw, route, index, name, mode, enabled))
    return route, errors


def _validate_required_fields(
    raw: dict[str, Any],
    spec: LegacyActionSpec,
    index: int,
    name: str,
    mode: str,
    enabled: bool,
) -> list[ShimError]:
    errors: list[ShimError] = []
    for field_name in spec.required_fields:
        if not str(raw.get(field_name) or "").strip():
            errors.append(
                _error(
                    mode,
                    index,
                    name,
                    f"missing required field {field_name!r}",
                    enabled=enabled,
                )
            )
    return errors


def _validate_target(
    raw: dict[str, Any],
    config: dict[str, Any],
    index: int,
    name: str,
    mode: str,
    enabled: bool,
) -> list[ShimError]:
    if raw.get("action") != ACTION_DISPATCH_AGENT or not raw.get("agent"):
        return []
    agent_name = str(raw.get("agent"))
    if agent_name.casefold() in _accepted_agents(config):
        return []
    return [
        _error(
            mode,
            index,
            name,
            f"dispatch_agent.agent {agent_name!r} is not a known agent",
            enabled=enabled,
        )
    ]


def _validate_templates(
    raw: dict[str, Any],
    index: int,
    name: str,
    mode: str,
    enabled: bool,
    project_root: Path,
) -> list[ShimError]:
    errors: list[ShimError] = []
    for field_name in ("template", "template_inline", "task_template"):
        if field_name in raw and not isinstance(raw.get(field_name), str):
            errors.append(
                _error(
                    mode,
                    index,
                    name,
                    f"{field_name} must be a string",
                    enabled=enabled,
                )
            )
    for field_name in ("template_path", "task_template_path"):
        if field_name not in raw:
            continue
        value = raw.get(field_name)
        if not isinstance(value, str):
            errors.append(
                _error(mode, index, name, f"{field_name} must be a string", enabled=enabled)
            )
            continue
        path = Path(value)
        if not path.is_absolute():
            path = project_root / path
        if not path.is_file():
            errors.append(
                _error(
                    mode,
                    index,
                    name,
                    f"{field_name} is not readable: {value}",
                    enabled=enabled,
                )
            )
    return errors


def _validate_match(
    raw: dict[str, Any],
    index: int,
    name: str,
    mode: str,
    enabled: bool,
) -> list[ShimError]:
    match = raw.get("match") or {}
    if not isinstance(match, dict):
        return [_error(mode, index, name, "match must be a dict", enabled=enabled)]
    errors: list[ShimError] = []
    for key, value in match.items():
        if not isinstance(key, str):
            errors.append(_error(mode, index, name, "match keys must be strings", enabled=enabled))
        if isinstance(value, list):
            if not all(_is_scalar(item) for item in value):
                errors.append(
                    _error(
                        mode,
                        index,
                        name,
                        f"match value for {key!r} must be scalar or list of scalars",
                        enabled=enabled,
                    )
                )
        elif not _is_scalar(value):
            errors.append(
                _error(
                    mode,
                    index,
                    name,
                    f"match value for {key!r} must be scalar or list",
                    enabled=enabled,
                )
            )
    return errors


def _validate_safety(
    route: Route,
    index: int,
    name: str,
    mode: str,
    enabled: bool,
) -> list[ShimError]:
    targets_trader = any(
        action.target and action.target.lower() == "trader"
        for action in route.actions
    )
    uses_trade = any(action.kind == "trade" for action in route.actions)
    if (targets_trader or uses_trade) and not route.requires_autotrade:
        return [
            _error(
                mode,
                index,
                name,
                "trader/trade route lacks an effective autotrade gate",
                enabled=enabled,
            )
        ]
    return []


def _validate_parity(
    raw: dict[str, Any],
    route: Route,
    index: int,
    name: str,
    mode: str,
    enabled: bool,
) -> list[ShimError]:
    try:
        handler = SignalHandler.from_dict(raw)
    except Exception as exc:  # noqa: BLE001
        return [_error(mode, index, name, f"legacy parser rejected handler: {exc}", enabled=enabled)]

    errors: list[ShimError] = []
    for label, event in generate_parity_fixtures(raw):
        legacy_result = legacy_matches(handler, event)
        router_result = route_matches(route, event)
        if legacy_result != router_result:
            errors.append(
                _error(
                    mode,
                    index,
                    name,
                    (
                        f"parity divergence on {label} fixture: "
                        f"legacy={legacy_result} router={router_result}"
                    ),
                    enabled=enabled,
                )
            )
    return errors


def _infer_channel(match: dict[str, Any]) -> str:
    source = match.get("source")
    signal_type = match.get("signal_type")
    if _match_value_contains(source, "ai-token-analyzer") or _match_value_contains(
        signal_type, "ANALYSIS"
    ):
        return "ai_analyses"
    return "trade_signals"


def _accepted_agents(config: dict[str, Any]) -> set[str]:
    accepted = {str(name).casefold() for name in AGENTS.keys()}
    raw_agents = config.get("agents")
    if isinstance(raw_agents, dict):
        accepted.update(str(name).casefold() for name in raw_agents)
    accepted.add("kai")
    return accepted


def _match_value_contains(value: Any, candidate: str) -> bool:
    if isinstance(value, list):
        return any(_match_value_contains(item, candidate) for item in value)
    return isinstance(value, str) and value.casefold() == candidate.casefold()


def _assign_fixture_value(event: dict[str, Any], path: str, value: Any) -> None:
    if "." not in path:
        event[path] = value
        return
    current = event
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _non_matching_value(expected: Any) -> Any:
    if isinstance(expected, list):
        return "__shim_miss__"
    if isinstance(expected, str):
        return "__shim_miss__"
    if isinstance(expected, bool):
        return not expected
    if isinstance(expected, (int, float)):
        return expected + 1
    return "__shim_miss__"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _mode_value(
    mode: str | SignalRouterMode | None,
    config: dict[str, Any] | None = None,
) -> str:
    if isinstance(mode, SignalRouterMode):
        return mode.value
    if mode is not None:
        return str(mode)
    return resolve_mode(config or {}).value


def _error(
    mode: str,
    index: int | None,
    name: str | None,
    message: str,
    *,
    enabled: bool,
) -> ShimError:
    severity = "warning" if mode == SignalRouterMode.LEGACY.value or not enabled else "error"
    return ShimError(
        mode=mode,
        index=index,
        name=name,
        message=message,
        severity=severity,
        enabled=enabled,
    )

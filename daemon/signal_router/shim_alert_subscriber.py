"""Compatibility translator for ``daemon.alert_subscriber`` config."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain_model import ActionDescriptor, Route
from .shim_signal_handlers import ShimError

SHIM_SOURCE = "alert_subscriber"
DEFAULT_POLYMARKET_SUBSCRIPTION_NAME = "polymarket-default"
DEFAULT_POLYMARKET_TEMPLATE = "prompts/alerts/polymarket.md.tmpl"


@dataclass(frozen=True)
class AlertSubscriptionConfig:
    name: str = DEFAULT_POLYMARKET_SUBSCRIPTION_NAME
    enabled: bool = False
    subject_pattern: str = "polymarket.alpha.alarm.>"
    prompt_template_path: str = DEFAULT_POLYMARKET_TEMPLATE
    target_session: str = "kai"
    target_agent: str | None = None
    max_injected_turns_per_hour: int = 10


@dataclass(frozen=True)
class AlertSubscriberConfig:
    enabled: bool = False
    kill_switch: bool = False
    alerts_yaml_path: str | None = "alerts.yaml"
    subscriptions: tuple[AlertSubscriptionConfig, ...] = ()


def translate_alert_subscriber_config(
    config: AlertSubscriberConfig | dict[str, Any],
    *,
    mode: str = "legacy",
) -> tuple[list[Route], list[ShimError]]:
    """Translate AlertSubscriber subscriptions into dormant router routes."""

    subscriber_config = _coerce_config(config)
    routes = [
        _subscription_to_route(sub, subscriber_config)
        for sub in subscriber_config.subscriptions
    ]
    return routes, []


def _coerce_config(config: AlertSubscriberConfig | dict[str, Any]) -> AlertSubscriberConfig:
    if isinstance(config, AlertSubscriberConfig):
        return config
    return load_alert_subscriber_config(config)


def load_alert_subscriber_config(config: dict[str, Any] | None = None) -> AlertSubscriberConfig:
    """Load legacy alert subscriber config for router compatibility translation."""

    block = ((config or {}).get("daemon") or {}).get("alert_subscriber") or {}
    default_sub = AlertSubscriptionConfig()
    raw_subscriptions = block.get("subscriptions") or []
    subscriptions = [
        _coerce_subscription(item)
        for item in raw_subscriptions
        if isinstance(item, dict)
    ]
    if not any(sub.name == default_sub.name for sub in subscriptions):
        subscriptions.append(default_sub)

    enabled = bool(block.get("enabled", False))
    kill_switch = bool(block.get("kill_switch", False))
    alerts_yaml_path = block.get("alerts_yaml_path", "alerts.yaml")
    alerts_yaml_path = str(alerts_yaml_path) if alerts_yaml_path is not None else None

    enabled = _env_bool("KAI_ALERT_SUBSCRIBER_ENABLED", enabled)
    kill_switch = _env_bool("KAI_ALERT_SUBSCRIBER_KILL_SWITCH", kill_switch)
    if os.getenv("KAI_ALERT_SUBSCRIBER_ALERTS_YAML_PATH") is not None:
        alerts_yaml_path = os.getenv("KAI_ALERT_SUBSCRIBER_ALERTS_YAML_PATH") or None

    try:
        overlay_subscriptions = _load_alerts_yaml(alerts_yaml_path)
    except Exception:
        if enabled:
            raise
        overlay_subscriptions = []
    by_name = {sub.name: sub for sub in subscriptions}
    by_name.update({sub.name: sub for sub in overlay_subscriptions})
    subscriptions = list(by_name.values())

    polymarket_enabled_override = os.getenv("KAI_ALERT_SUBSCRIBER_ENABLE_POLYMARKET")
    default_max = _env_int(
        "KAI_ALERT_SUBSCRIBER_DEFAULT_MAX_INJECTED_TURNS_PER_HOUR",
        default_sub.max_injected_turns_per_hour,
    )
    updated: list[AlertSubscriptionConfig] = []
    for sub in subscriptions:
        if sub.name == DEFAULT_POLYMARKET_SUBSCRIPTION_NAME:
            sub = AlertSubscriptionConfig(
                name=sub.name,
                enabled=_env_bool("KAI_ALERT_SUBSCRIBER_ENABLE_POLYMARKET", sub.enabled),
                subject_pattern=os.getenv(
                    "KAI_ALERT_SUBSCRIBER_POLYMARKET_SUBJECT",
                    sub.subject_pattern,
                ),
                prompt_template_path=os.getenv(
                    "KAI_ALERT_SUBSCRIBER_POLYMARKET_TEMPLATE",
                    sub.prompt_template_path,
                ),
                target_session=os.getenv(
                    "KAI_ALERT_SUBSCRIBER_POLYMARKET_TARGET_SESSION",
                    sub.target_session,
                ),
                target_agent=sub.target_agent,
                max_injected_turns_per_hour=_env_int(
                    "KAI_ALERT_SUBSCRIBER_DEFAULT_MAX_INJECTED_TURNS_PER_HOUR",
                    sub.max_injected_turns_per_hour,
                ),
            )
        elif (
            polymarket_enabled_override is None
            and sub.max_injected_turns_per_hour == default_sub.max_injected_turns_per_hour
        ):
            sub = AlertSubscriptionConfig(
                **{**sub.__dict__, "max_injected_turns_per_hour": default_max}
            )
        updated.append(sub)
    return AlertSubscriberConfig(
        enabled=enabled,
        kill_switch=kill_switch,
        alerts_yaml_path=alerts_yaml_path,
        subscriptions=tuple(updated),
    )


def _coerce_subscription(raw: dict[str, Any]) -> AlertSubscriptionConfig:
    default = AlertSubscriptionConfig()
    return AlertSubscriptionConfig(
        name=str(raw.get("name") or default.name),
        enabled=bool(raw.get("enabled", default.enabled)),
        subject_pattern=str(raw.get("subject_pattern") or default.subject_pattern),
        prompt_template_path=str(raw.get("prompt_template_path") or default.prompt_template_path),
        target_session=str(raw.get("target_session") or default.target_session),
        target_agent=(str(raw["target_agent"]) if raw.get("target_agent") else None),
        max_injected_turns_per_hour=_safe_int(
            raw.get("max_injected_turns_per_hour"),
            default.max_injected_turns_per_hour,
        ),
    )


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_alerts_yaml(path_value: str | None) -> list[AlertSubscriptionConfig]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore[import-untyped]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("alerts.yaml exists but PyYAML is not available") from exc
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ValueError("alerts.yaml must contain a mapping")
    subscriptions = loaded.get("subscriptions") or []
    if not isinstance(subscriptions, list):
        raise ValueError("alerts.yaml subscriptions must be a list")
    return [_coerce_subscription(item) for item in subscriptions if isinstance(item, dict)]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _subscription_to_route(
    subscription: AlertSubscriptionConfig,
    subscriber_config: AlertSubscriberConfig,
) -> Route:
    enabled = (
        bool(subscriber_config.enabled)
        and not bool(subscriber_config.kill_switch)
        and bool(subscription.enabled)
    )
    return Route(
        name=f"alert-subscription:{subscription.name}",
        channel=channel_name_from_subject_pattern(subscription.subject_pattern),
        match={},
        actions=[
            ActionDescriptor(
                kind="inject_session",
                target=subscription.target_session,
                params={
                    "template": subscription.prompt_template_path,
                    "prompt_template_path": subscription.prompt_template_path,
                    "target_agent": subscription.target_agent,
                    "rate_limit": {
                        "max_per_hour": subscription.max_injected_turns_per_hour,
                    },
                },
            )
        ],
        pre_action=None,
        enabled=enabled,
        cooldown_seconds=0,
        requires_autotrade=False,
        config={
            "source_compat": SHIM_SOURCE,
            "subject_pattern": subscription.subject_pattern,
            "target_agent": subscription.target_agent,
        },
    )


def channel_name_from_subject_pattern(subject_pattern: str) -> str:
    """Derive a stable router channel name from a NATS subject pattern."""

    if subject_pattern == "polymarket.alpha.alarm.>":
        return "polymarket_alarms"
    base = re.sub(r"[.*>]+", "", subject_pattern).strip(".")
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    return base or "alert_subscriber"

"""Compatibility translator for ``daemon.alert_subscriber`` config."""

from __future__ import annotations

import re
from typing import Any

from daemon.alert_subscriber import (
    AlertSubscriberConfig,
    AlertSubscriptionConfig,
    load_alert_subscriber_config,
)

from .domain_model import ActionDescriptor, Route
from .shim_signal_handlers import ShimError

SHIM_SOURCE = "alert_subscriber"


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

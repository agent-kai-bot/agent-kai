"""Alert subscriber configuration and prompt rendering scaffolding."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daemon.event_injector import EventInjectionTemplate, stable_json

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
    enabled: bool = True
    kill_switch: bool = False
    alerts_yaml_path: str | None = "alerts.yaml"
    subscriptions: tuple[AlertSubscriptionConfig, ...] = ()


@dataclass(frozen=True)
class AlertEvent:
    type: str
    subscription_name: str
    subject: str
    received_at: str
    monotonic_seconds: float
    seq: int
    payload: dict[str, Any]
    dedup_key: str
    source: str
    alert_type: str
    severity: str
    target_session: str
    prompt_template_path: str

    def to_template_values(self) -> dict[str, Any]:
        data = self.payload.get("data") if isinstance(self.payload.get("data"), dict) else {}
        return {
            "subscription_name": self.subscription_name,
            "subject": self.subject,
            "seq": self.seq,
            "received_at": self.received_at,
            "source": self.source,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "event_id": str(self.payload.get("event_id") or ""),
            "market": str(self.payload.get("market") or ""),
            "symbol": str(self.payload.get("symbol") or ""),
            "title": str(self.payload.get("title") or ""),
            "summary": str(self.payload.get("summary") or ""),
            "body": str(self.payload.get("body") or self.payload.get("summary") or ""),
            "url": str(self.payload.get("url") or ""),
            "timestamp": str(self.payload.get("timestamp") or self.received_at),
            "dedup_key": self.dedup_key,
            "target_session": self.target_session,
            "payload_json": stable_json(self.payload),
            "data_json": stable_json(data),
        }


class MalformedAlertPayload(ValueError):
    """Raised when an alert payload cannot be safely rendered."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        path = Path(__file__).resolve().parent.parent / path
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


def load_alert_subscriber_config(config: dict[str, Any] | None = None) -> AlertSubscriberConfig:
    """Load alert subscriber config from agent config, optional overlay, and env."""

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

    enabled = bool(block.get("enabled", True))
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
    default_max = _env_int("KAI_ALERT_SUBSCRIBER_DEFAULT_MAX_INJECTED_TURNS_PER_HOUR", default_sub.max_injected_turns_per_hour)
    updated: list[AlertSubscriptionConfig] = []
    for sub in subscriptions:
        if sub.name == DEFAULT_POLYMARKET_SUBSCRIPTION_NAME:
            sub = AlertSubscriptionConfig(
                name=sub.name,
                enabled=_env_bool("KAI_ALERT_SUBSCRIBER_ENABLE_POLYMARKET", sub.enabled),
                subject_pattern=os.getenv("KAI_ALERT_SUBSCRIBER_POLYMARKET_SUBJECT", sub.subject_pattern),
                prompt_template_path=os.getenv("KAI_ALERT_SUBSCRIBER_POLYMARKET_TEMPLATE", sub.prompt_template_path),
                target_session=os.getenv("KAI_ALERT_SUBSCRIBER_POLYMARKET_TARGET_SESSION", sub.target_session),
                target_agent=sub.target_agent,
                max_injected_turns_per_hour=_env_int(
                    "KAI_ALERT_SUBSCRIBER_DEFAULT_MAX_INJECTED_TURNS_PER_HOUR",
                    sub.max_injected_turns_per_hour,
                ),
            )
        elif polymarket_enabled_override is None and sub.max_injected_turns_per_hour == default_sub.max_injected_turns_per_hour:
            sub = AlertSubscriptionConfig(**{**sub.__dict__, "max_injected_turns_per_hour": default_max})
        updated.append(sub)
    result = AlertSubscriberConfig(
        enabled=enabled,
        kill_switch=kill_switch,
        alerts_yaml_path=alerts_yaml_path,
        subscriptions=tuple(updated),
    )
    validate_alert_subscriber_config(result)
    return result


def validate_alert_subscriber_config(config: AlertSubscriberConfig) -> None:
    """Fail fast when enabled subscriptions reference unreadable templates."""

    if not config.enabled:
        return
    for sub in config.subscriptions:
        if not sub.enabled:
            continue
        EventInjectionTemplate.load(sub.prompt_template_path)


def normalize_alert_event(
    subscription: AlertSubscriptionConfig,
    subject: str,
    payload: Any,
    *,
    seq: int = 1,
    received_at: str | None = None,
    monotonic_seconds: float | None = None,
) -> AlertEvent:
    """Validate and normalize one alert payload for template rendering."""

    if not isinstance(payload, dict) or set(payload.keys()) == {"raw"}:
        raise MalformedAlertPayload("non_json")
    data = payload.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise MalformedAlertPayload("invalid_data")
    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or payload.get("body") or "").strip()
    if not title and not summary:
        raise MalformedAlertPayload("missing_text")
    received = received_at or _utc_now_iso()
    source = str(payload.get("source") or subject.split(".", 1)[0] or "unknown").strip()
    alert_type = str(payload.get("alert_type") or "unknown").strip() or "unknown"
    severity = str(payload.get("severity") or "info").strip().lower()
    if severity not in {"info", "warning", "critical"}:
        severity = "info"
    normalized = dict(payload)
    normalized["source"] = source
    normalized["alert_type"] = alert_type
    normalized["severity"] = severity
    normalized["title"] = title
    normalized["summary"] = summary
    normalized["timestamp"] = str(payload.get("timestamp") or received)
    normalized["data"] = data
    dedup_key = str(payload.get("event_id") or f"{source}:{alert_type}:{title}:{summary}")
    return AlertEvent(
        type="alert_subscriber.alert",
        subscription_name=subscription.name,
        subject=subject,
        received_at=received,
        monotonic_seconds=float(monotonic_seconds if monotonic_seconds is not None else time.monotonic()),
        seq=seq,
        payload=normalized,
        dedup_key=dedup_key,
        source=source,
        alert_type=alert_type,
        severity=severity,
        target_session=subscription.target_session,
        prompt_template_path=subscription.prompt_template_path,
    )


def render_alert_prompt(subscription: AlertSubscriptionConfig, subject: str, payload: Any) -> str | None:
    """Render a configured alert prompt, returning None for malformed payloads."""

    try:
        event = normalize_alert_event(subscription, subject, payload)
    except MalformedAlertPayload:
        return None
    template = EventInjectionTemplate.load(subscription.prompt_template_path)
    return template.render(event)

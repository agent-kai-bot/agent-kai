"""Session injection action executor."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daemon.signal_router.domain_model import ActionDescriptor

from .base import (
    ACTION_STATUS_FAILED,
    ACTION_STATUS_FIRED,
    ACTION_STATUS_SKIPPED,
    ActionResult,
    ExecutionContext,
    ValidationError,
    dry_run_result,
    emit_telemetry,
    event_payload,
    render_template,
)

ACTIVE_ATTR = "_signal_router_event_turn_active"
TIMESTAMPS_ATTR = "signal_router_event_injection_timestamps"


@dataclass(frozen=True)
class InlineEventInjectionTemplate:
    """Inline prompt template using EventInjector's render behavior."""

    name: str
    path: Path
    content: str

    @classmethod
    def from_content(cls, content: str) -> "InlineEventInjectionTemplate":
        return cls(name="inline", path=Path("<inline>"), content=content)

    def render_map(self, values: dict[str, Any]) -> str:
        from .base import SafeFormatDict

        return self.content.format_map(SafeFormatDict(values))


class InjectSessionExecutor:
    """Convert routed events into EventInjector requests."""

    kind = "inject_session"

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if not action.target:
            errors.append(ValidationError("target", "inject_session requires target"))
        if bool(action.params.get("template")) == bool(action.params.get("template_inline")):
            errors.append(
                ValidationError(
                    "template",
                    "inject_session requires exactly one of template or template_inline",
                )
            )
        rate_limit = action.params.get("rate_limit")
        if rate_limit is not None and not isinstance(rate_limit, dict):
            errors.append(ValidationError("rate_limit", "rate_limit must be an object"))
        elif isinstance(rate_limit, dict) and int(rate_limit.get("max_per_hour", 1) or 0) < 0:
            errors.append(
                ValidationError("rate_limit.max_per_hour", "max_per_hour must be non-negative")
            )
        return errors

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        if action.target and action.target.lower() == "trader" and not context.autotrade_enabled():
            emit_telemetry(
                context,
                "auto.signal_router.inject_session.gated",
                {"target": action.target, "route_name": context.route_name},
            )
            return ActionResult(
                kind=self.kind,
                target=action.target,
                status=ACTION_STATUS_SKIPPED,
                detail="autotrade_disabled",
                metrics={"autotrade_gate": "blocked"},
            )
        dedup_result = self._check_dedup(action, envelope, context)
        if dedup_result is not None:
            return dedup_result
        if context.dry_run:
            emit_telemetry(
                context,
                "auto.signal_router.shadow.inject_session.would_fire",
                {"target": action.target, "channel": context.channel},
            )
            return dry_run_result(action, metrics={"target": action.target})
        if context.event_injector is None:
            return ActionResult(
                kind=self.kind,
                target=action.target,
                status=ACTION_STATUS_FAILED,
                detail="event_injector_unavailable",
                metrics={},
            )
        managed = context.sessions.get(action.target or "")
        if managed is None:
            return ActionResult(
                kind=self.kind,
                target=action.target,
                status=ACTION_STATUS_FAILED,
                detail="target_session_unavailable",
                metrics={},
            )
        session = getattr(managed, "session", managed)
        self._ensure_injection_attrs(session)
        request = self._request(action, envelope, context)
        decision = context.event_injector.handle(managed, request)
        if hasattr(decision, "__await__"):
            emit_telemetry(
                context,
                "auto.signal_router.inject_session.scheduled",
                {"target": action.target, "channel": context.channel},
            )
            from .base import _dispatch_maybe_async

            _dispatch_maybe_async(decision)
            return ActionResult(
                kind=self.kind,
                target=action.target,
                status=ACTION_STATUS_FIRED,
                detail="scheduled",
                metrics={"async": True},
            )
        return ActionResult(
            kind=self.kind,
            target=action.target,
            status=ACTION_STATUS_FIRED if decision.ok else ACTION_STATUS_SKIPPED,
            detail=decision.reason,
            metrics={"decision_ok": bool(decision.ok)},
        )

    def _request(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> Any:
        from daemon.event_injector import EventInjectionPolicy, EventInjectionRequest

        payload = event_payload(envelope)
        channel = context.channel or envelope.get("channel") or "unknown"
        return EventInjectionRequest(
            event=payload,
            template=self._template(action),
            policy=EventInjectionPolicy(
                source=f"signal_router:{channel}",
                drop_topic="auto.signal_router.inject_session.dropped",
                injected_topic="auto.signal_router.inject_session.injected",
                active_attr=ACTIVE_ATTR,
                timestamp_attr=TIMESTAMPS_ATTR,
                max_injected_turns_per_hour=self._max_per_hour(action),
                require_auto_mode=bool(action.params.get("require_auto_mode", True)),
                active_reason="signal_router_turn_active",
                single_auto_iteration=bool(action.params.get("single_auto_iteration", True)),
                prefetch_polymarket_bbo=bool(action.params.get("prefetch_polymarket_bbo", False)),
                prefetch_polymarket_token_info=bool(action.params.get("prefetch_polymarket_token_info", False)),
            ),
            render_values={**payload, "channel": channel, "subject": context.subject or envelope.get("subject", "")},
            seq=payload.get("id") or payload.get("seq") or context.route_name or "signal-router",
            monotonic_seconds=context.monotonic_seconds(),
            job_id=f"signal_router:{channel}:{context.route_name or action.target or 'route'}",
            task_name=f"signal-router-{channel}-{action.target or 'session'}",
            injected_payload={
                "source": f"signal_router:{channel}",
                "target": action.target,
                "route_name": context.route_name,
                "channel": channel,
            },
        )

    def _template(self, action: ActionDescriptor) -> Any:
        if action.params.get("template_inline"):
            return InlineEventInjectionTemplate.from_content(str(action.params["template_inline"]))
        from daemon.event_injector import EventInjectionTemplate

        return EventInjectionTemplate.load(str(action.params["template"]))

    @staticmethod
    def _max_per_hour(action: ActionDescriptor) -> int:
        rate_limit = action.params.get("rate_limit")
        if isinstance(rate_limit, dict) and "max_per_hour" in rate_limit:
            return int(rate_limit.get("max_per_hour") or 0)
        return 3600

    @staticmethod
    def _ensure_injection_attrs(session: Any) -> None:
        if not hasattr(session, ACTIVE_ATTR):
            setattr(session, ACTIVE_ATTR, False)
        if not hasattr(session, TIMESTAMPS_ATTR):
            setattr(session, TIMESTAMPS_ATTR, deque())

    def _check_dedup(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult | None:
        dedup = action.params.get("dedup")
        if not isinstance(dedup, dict) or context.dedup_table is None:
            return None
        ttl_seconds = int(dedup.get("ttl_seconds") or 0)
        if ttl_seconds <= 0:
            return None
        fields = dedup.get("key_fields") or []
        if not isinstance(fields, list):
            fields = []
        payload = {**event_payload(envelope), "channel": context.channel or envelope.get("channel")}
        key_parts = [context.route_name or action.kind]
        for field_name in fields:
            key_parts.append(str(payload.get(str(field_name), "")))
        key = ":".join(key_parts)
        if context.dry_run:
            return None
        if context.dedup_table.check_and_record_cooldown(key, ttl_seconds):
            return None
        return ActionResult(
            kind=self.kind,
            target=action.target,
            status=ACTION_STATUS_SKIPPED,
            detail="dedup_suppressed",
            metrics={"dedup_key": key},
        )

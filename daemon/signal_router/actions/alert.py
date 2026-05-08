"""Operator alert action executor."""

from __future__ import annotations

from typing import Any

from daemon.signal_router.domain_model import ActionDescriptor

from .base import (
    ACTION_STATUS_FIRED,
    ActionResult,
    ExecutionContext,
    ValidationError,
    dry_run_result,
    emit_telemetry,
    event_payload,
    render_template,
)


class AlertExecutor:
    """Emit high-salience operator alert telemetry and UI events."""

    kind = "alert"

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        return []

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        payload = {
            "category": "alerts",
            "channel": context.channel or envelope.get("channel"),
            "subject": context.subject or envelope.get("subject"),
            "severity": action.params.get("severity", "high"),
            "message": (
                render_template(str(action.params["template_inline"]), envelope, context)
                if action.params.get("template_inline")
                else None
            ),
            "event": event_payload(envelope),
            "route_name": context.route_name,
        }
        if context.dry_run:
            emit_telemetry(context, "auto.signal_router.shadow.alert.would_fire", payload)
            return dry_run_result(action, metrics={"severity": payload["severity"]})
        emit_telemetry(context, "auto.signal_router.alert", payload)
        for entry in context.sessions.values():
            session = getattr(entry, "session", entry)
            session.publish_event("signal.received", {"signal": payload})
        return ActionResult(
            self.kind,
            action.target,
            ACTION_STATUS_FIRED,
            "alert_emitted",
            {"severity": payload["severity"]},
        )

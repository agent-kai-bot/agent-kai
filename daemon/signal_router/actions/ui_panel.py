"""UI panel action executor."""

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
)


class UIPanelExecutor:
    """Publish routed events through the legacy signal UI envelope."""

    kind = "ui_panel"

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        if action.target is None:
            return [ValidationError("target", "ui_panel requires a target category")]
        return []

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        payload = self._signal_payload(action, envelope, context)
        if context.dry_run:
            emit_telemetry(
                context,
                "auto.signal_router.shadow.ui_panel.would_fire",
                {"target": action.target, "channel": payload.get("channel")},
            )
            return dry_run_result(action, metrics={"sessions": len(context.sessions)})

        for entry in context.sessions.values():
            session = getattr(entry, "session", entry)
            session.publish_event("signal.received", {"signal": dict(payload)})
        if context.daemon_event_publisher is not None:
            emit_telemetry(context, "signals", payload)
        return ActionResult(
            kind=self.kind,
            target=action.target,
            status=ACTION_STATUS_FIRED,
            detail="published signal.received",
            metrics={"sessions": len(context.sessions), "channel": payload.get("channel")},
        )

    @staticmethod
    def _signal_payload(
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]:
        payload = event_payload(envelope)
        payload["category"] = action.params.get("category") or action.target or "signals"
        payload["channel"] = context.channel or envelope.get("channel") or payload.get("channel")
        payload.setdefault("subject", context.subject or envelope.get("subject"))
        return payload

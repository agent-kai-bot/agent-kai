"""Ignore action executor."""

from __future__ import annotations

from typing import Any

from daemon.signal_router.domain_model import ActionDescriptor

from .base import (
    ACTION_STATUS_SKIPPED,
    ActionResult,
    ExecutionContext,
    ValidationError,
    dry_run_result,
    emit_telemetry,
    event_payload,
    write_audit,
)


class IgnoreExecutor:
    """Record a route decision without causing a terminal side effect."""

    kind = "ignore"

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        return []

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        reason = str(action.params.get("reason") or "ignored")
        record = {
            "kind": "signal_router.ignore",
            "reason": reason,
            "route_name": context.route_name,
            "channel": context.channel or envelope.get("channel"),
            "event": event_payload(envelope),
        }
        if context.dry_run:
            emit_telemetry(context, "auto.signal_router.shadow.ignore.would_fire", record)
            return dry_run_result(action, detail=reason)
        write_audit(context, record)
        return ActionResult(
            self.kind,
            action.target,
            ACTION_STATUS_SKIPPED,
            reason,
            {"ignored": True},
        )

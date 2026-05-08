"""Structured log/audit action executor."""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
    write_audit,
)


class LogExecutor:
    """Write structured log/audit records."""

    kind = "log"

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        return []

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "signal_router.action",
            "action_kind": self.kind,
            "target": action.target or "daemon",
            "route_name": context.route_name,
            "channel": context.channel or envelope.get("channel"),
            "subject": context.subject or envelope.get("subject"),
            "message": (
                render_template(str(action.params["template_inline"]), envelope, context)
                if action.params.get("template_inline")
                else None
            ),
            "event": event_payload(envelope),
        }
        if context.dry_run:
            emit_telemetry(context, "auto.signal_router.shadow.log.would_fire", record)
            return dry_run_result(action)
        write_audit(context, record)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
        if context.chat_logger is not None and (action.target or "daemon") == "tui":
            context.chat_logger(line)
        return ActionResult(
            self.kind,
            action.target,
            ACTION_STATUS_FIRED,
            line,
            {"bytes": len(line.encode("utf-8"))},
        )

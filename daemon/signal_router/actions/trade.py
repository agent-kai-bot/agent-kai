"""Direct trade action executor."""

from __future__ import annotations

from typing import Any

from daemon.signal_router.domain_model import ActionDescriptor

from .base import (
    ACTION_STATUS_FIRED,
    ACTION_STATUS_SKIPPED,
    ActionResult,
    ExecutionContext,
    ValidationError,
    dry_run_result,
    emit_telemetry,
    event_payload,
    write_audit,
)


class TradeExecutor:
    """Gate direct trade automation behind explicit action config."""

    kind = "trade"

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if not bool(action.params.get("explicit", True)):
            errors.append(ValidationError("explicit", "trade action must be explicitly configured"))
        return errors

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        payload = event_payload(envelope)
        diff_metric = {
            "route_name": context.route_name,
            "channel": context.channel or envelope.get("channel"),
            "target": action.target or "autotrade",
            "symbol": payload.get("symbol"),
            "signal_type": payload.get("signal_type"),
        }
        emit_telemetry(context, "auto.signal_router.trade.diff_metric_stub", diff_metric)
        if not context.autotrade_enabled():
            write_audit(
                context,
                {
                    "kind": "signal_router.trade",
                    "status": "gated",
                    "reason": "autotrade_disabled",
                    **diff_metric,
                },
            )
            return ActionResult(
                self.kind,
                action.target,
                ACTION_STATUS_SKIPPED,
                "autotrade_disabled",
                {"autotrade_gate": "blocked", "diff_metric_stub": True},
            )
        if context.dry_run:
            emit_telemetry(context, "auto.signal_router.shadow.trade.would_fire", diff_metric)
            return dry_run_result(action, metrics={"diff_metric_stub": True})
        write_audit(
            context,
            {
                "kind": "signal_router.trade",
                "status": "would_dispatch_direct_trade_adapter",
                "event": payload,
                **diff_metric,
            },
        )
        return ActionResult(
            self.kind,
            action.target,
            ACTION_STATUS_FIRED,
            "direct_trade_adapter_stub",
            {"diff_metric_stub": True},
        )

"""Direct trade action executor."""

from __future__ import annotations

from collections.abc import Callable
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

    def __init__(
        self,
        runtime_config_store: Any | None = None,
        execution_adapter: Callable[[ActionDescriptor, dict[str, Any], ExecutionContext], Any] | None = None,
    ) -> None:
        self.runtime_config_store = runtime_config_store
        self.execution_adapter = execution_adapter

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
        if not self._live_trades_enabled(context):
            intended_action = {
                "kind": action.kind,
                "target": action.target,
                "params": dict(action.params),
            }
            emit_telemetry(
                context,
                "auto.signal_router.trade.dry_run",
                {
                    "route": context.route_name,
                    "intended_action": intended_action,
                    "reason": "live_trades_disabled",
                },
            )
            write_audit(
                context,
                {
                    "kind": "signal_router.trade",
                    "status": "suppressed_dry_run",
                    "reason": "live_trades_disabled",
                    "route_name": context.route_name,
                    "channel": context.channel or envelope.get("channel"),
                    "target": action.target or "autotrade",
                    "event": payload,
                },
            )
            return ActionResult(
                self.kind,
                action.target,
                "suppressed_dry_run",
                "live_trades_disabled",
                {"live_trades_enabled": False},
            )
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
        if self.execution_adapter is not None:
            adapter_result = self.execution_adapter(action, envelope, context)
            if isinstance(adapter_result, ActionResult):
                return adapter_result
        return ActionResult(
            self.kind,
            action.target,
            ACTION_STATUS_FIRED,
            "direct_trade_adapter_stub",
            {"diff_metric_stub": True},
        )

    def _live_trades_enabled(self, context: ExecutionContext) -> bool:
        store = context.runtime_config_store or self.runtime_config_store
        if store is None:
            return True
        getter = getattr(store, "get_signal_router_live_trades_enabled", None)
        if getter is None:
            return True
        return bool(getter())

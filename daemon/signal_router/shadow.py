"""Shadow-mode runner for comparing legacy side effects with router dry-runs."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.signal_consumer import Signal

from .actions import ActionResult, ExecutionContext
from .actions.base import (
    ACTION_STATUS_FIRED,
    ACTION_STATUS_SKIPPED,
    ACTION_STATUS_SUPPRESSED_DRY_RUN,
)
from .audit_writer import RouterAuditWriter
from .diff_metrics import DiffMetric, DiffMetricStore, DivergenceKind
from .feature_flags import SignalRouterMode
from .route_decision import RouteDecision
from .router import SignalRouter

TelemetryEmitter = Callable[[str, dict[str, Any]], Any]
LegacyDispatch = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class LegacyDecision:
    """Normalized outcome returned by a legacy dispatch tap."""

    fired: bool
    route_name: str = "legacy"
    matched: bool = True
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def decision(self) -> str:
        return "fired" if self.fired else "suppressed"


@dataclass(frozen=True)
class RouterEvaluation:
    """One router route decision plus dry-run action results."""

    decision: RouteDecision
    results: list[ActionResult]

    @property
    def fired(self) -> bool:
        return any(_result_would_fire(result) for result in self.results)

    @property
    def reason(self) -> str:
        if self.fired:
            return "would_fire"
        for result in self.results:
            if result.detail:
                return str(result.detail)
            if result.status != ACTION_STATUS_FIRED:
                return result.status
        return "no_actions"

    @property
    def decision_text(self) -> str:
        return "would_fire" if self.fired else "would_suppress"


@dataclass(frozen=True)
class ShadowRunResult:
    """Complete shadow processing outcome for one envelope."""

    ran_shadow: bool
    legacy_decisions: list[LegacyDecision]
    router_evaluations: list[RouterEvaluation]
    diff_metrics: list[DiffMetric]
    added_latency_ms: float


class _NoopAuditWriter:
    """Suppress action-fire audit writes while the shadow summary writer owns audit."""

    def __call__(self, _decision: dict[str, Any]) -> None:
        return None

    def write(
        self,
        _decision: dict[str, Any],
        path_template: str | None = None,
    ) -> Path | None:
        del path_template
        return None


def default_shadow_audit_path_template() -> str:
    """Return the separate JSONL path for shadow decision audit rows."""

    return "${AGENTKAI_HOME}/audit/router_shadow_{date}.jsonl"


class ShadowRunner:
    """Run legacy dispatch and router dry-run in parallel in shadow mode only."""

    def __init__(
        self,
        router: SignalRouter,
        *,
        mode: str | SignalRouterMode | None = None,
        diff_store: DiffMetricStore | None = None,
        audit_writer: RouterAuditWriter | None = None,
        audit_path_template: str | None = None,
        telemetry_emitter: TelemetryEmitter | None = None,
        context_factory: Callable[[dict[str, Any]], ExecutionContext] | None = None,
    ) -> None:
        self.router = router
        self.mode = SignalRouterMode(mode or router.mode.value)
        self.diff_store = diff_store or DiffMetricStore()
        self.audit_writer = audit_writer or RouterAuditWriter(
            audit_path_template or default_shadow_audit_path_template()
        )
        self.audit_path_template = audit_path_template or default_shadow_audit_path_template()
        self.telemetry_emitter = telemetry_emitter
        self.context_factory = context_factory or (lambda _envelope: ExecutionContext())
        self.running = self.mode == SignalRouterMode.SHADOW

    async def process_signal(
        self,
        signal: Signal,
        legacy_dispatch: Callable[[Signal], Any],
    ) -> ShadowRunResult:
        """Process a legacy `SignalConsumer` callback in shadow mode."""

        envelope = signal_to_envelope(signal)

        async def legacy_from_envelope(_envelope: dict[str, Any]) -> Any:
            result = legacy_dispatch(signal)
            if inspect.isawaitable(result):
                return await result
            return result

        return await self.process_envelope(envelope, legacy_from_envelope)

    async def process_envelope(
        self,
        envelope: dict[str, Any],
        legacy_dispatch: LegacyDispatch,
    ) -> ShadowRunResult:
        """Run the legacy side-effect path and the router dry-run path."""

        start = time.perf_counter()
        if not self.running:
            legacy = await self._run_legacy(envelope, legacy_dispatch)
            return ShadowRunResult(
                ran_shadow=False,
                legacy_decisions=legacy,
                router_evaluations=[],
                diff_metrics=[],
                added_latency_ms=(time.perf_counter() - start) * 1000,
            )

        legacy_task = asyncio.create_task(self._run_legacy(envelope, legacy_dispatch))
        router_task = asyncio.create_task(self._run_router(envelope))
        legacy_decisions, router_evaluations = await asyncio.gather(
            legacy_task,
            router_task,
        )
        for evaluation in router_evaluations:
            self._emit_action_telemetry(evaluation, envelope)
        diff_metrics = self._compare(envelope, legacy_decisions, router_evaluations)
        for metric in diff_metrics:
            self.diff_store.record(metric)
            if metric.divergence_kind not in {
                DivergenceKind.AGREED_FIRE,
                DivergenceKind.AGREED_SUPPRESS,
            }:
                self._emit("auto.signal_router.shadow.diff", metric.to_dict())
            self._write_shadow_audit(envelope, metric, router_evaluations)
        return ShadowRunResult(
            ran_shadow=True,
            legacy_decisions=legacy_decisions,
            router_evaluations=router_evaluations,
            diff_metrics=diff_metrics,
            added_latency_ms=(time.perf_counter() - start) * 1000,
        )

    async def _run_legacy(
        self,
        envelope: dict[str, Any],
        legacy_dispatch: LegacyDispatch,
    ) -> list[LegacyDecision]:
        outcome = await _call_maybe_async(legacy_dispatch, envelope)
        return _coerce_legacy_decisions(outcome)

    async def _run_router(self, envelope: dict[str, Any]) -> list[RouterEvaluation]:
        return await asyncio.to_thread(self._run_router_sync, envelope)

    def _run_router_sync(self, envelope: dict[str, Any]) -> list[RouterEvaluation]:
        evaluations: list[RouterEvaluation] = []
        for decision in self.router.decide(envelope):
            base_context = self.context_factory(envelope)
            context = ExecutionContext(
                dry_run=True,
                channel=base_context.channel or decision.channel,
                subject=base_context.subject or envelope.get("subject"),
                route_name=base_context.route_name or decision.route_name,
                sessions=base_context.sessions,
                dedup_table=base_context.dedup_table,
                event_injector=base_context.event_injector,
                daemon_event_publisher=None,
                nats_publisher=base_context.nats_publisher,
                webhook_poster=base_context.webhook_poster,
                http_poster=base_context.http_poster,
                chat_logger=base_context.chat_logger,
                audit_writer=_NoopAuditWriter(),
                telemetry_emitter=None,
                sub_agent_manager=base_context.sub_agent_manager,
                nats_request=base_context.nats_request,
                autotrade_enabled=base_context.autotrade_enabled,
                monotonic_seconds=base_context.monotonic_seconds,
            )
            results = self.router.execute_actions(decision, envelope, context)
            evaluations.append(RouterEvaluation(decision=decision, results=results))
        return evaluations

    def _compare(
        self,
        envelope: dict[str, Any],
        legacy_decisions: list[LegacyDecision],
        router_evaluations: list[RouterEvaluation],
    ) -> list[DiffMetric]:
        if not router_evaluations:
            legacy_fired = any(decision.fired for decision in legacy_decisions)
            legacy_matched = any(decision.matched for decision in legacy_decisions)
            kind = (
                DivergenceKind.MATCH_DIVERGED
                if legacy_matched or legacy_fired
                else DivergenceKind.AGREED_SUPPRESS
            )
            return [
                DiffMetric(
                    route_name="unmatched",
                    legacy_decision="fired" if legacy_fired else "suppressed",
                    router_decision="would_suppress",
                    divergence_kind=kind,
                    details={"subject": envelope.get("subject")},
                )
            ]

        metrics: list[DiffMetric] = []
        legacy_fired = any(decision.fired for decision in legacy_decisions)
        legacy_decision = "fired" if legacy_fired else "suppressed"
        for evaluation in router_evaluations:
            kind = _classify(legacy_fired, evaluation)
            metrics.append(
                DiffMetric(
                    route_name=evaluation.decision.route_name,
                    legacy_decision=legacy_decision,
                    router_decision=evaluation.decision_text,
                    divergence_kind=kind,
                    details={
                        "subject": envelope.get("subject"),
                        "channel": evaluation.decision.channel,
                        "router_reason": evaluation.reason,
                        "legacy": [decision.details for decision in legacy_decisions],
                        "actions": [
                            {
                                "kind": result.kind,
                                "target": result.target,
                                "status": result.status,
                                "detail": result.detail,
                            }
                            for result in evaluation.results
                        ],
                    },
                )
            )
        return metrics

    def _emit_action_telemetry(
        self,
        evaluation: RouterEvaluation,
        envelope: dict[str, Any],
    ) -> None:
        for result in evaluation.results:
            if _result_would_fire(result):
                suffix = "would_fire"
            else:
                suffix = f"suppressed_{_suppression_reason(result)}"
            self._emit(
                f"auto.signal_router.shadow.{result.kind}.{suffix}",
                {
                    "route_name": evaluation.decision.route_name,
                    "channel": evaluation.decision.channel,
                    "subject": envelope.get("subject"),
                    "target": result.target,
                    "status": result.status,
                    "detail": result.detail,
                },
            )

    def _write_shadow_audit(
        self,
        envelope: dict[str, Any],
        metric: DiffMetric,
        evaluations: list[RouterEvaluation],
    ) -> None:
        evaluation = next(
            (
                item
                for item in evaluations
                if item.decision.route_name == metric.route_name
            ),
            None,
        )
        match_result = (
            evaluation.decision.match_result.audit_payload()
            if evaluation is not None
            else {"matched": False, "reason": "router_unmatched"}
        )
        actions = []
        if evaluation is not None:
            actions = [
                {
                    "kind": action.kind,
                    "target": action.target,
                    "params": action.params,
                }
                for action in evaluation.decision.actions
            ]
        row = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": "shadow",
            "route": metric.route_name,
            "channel": envelope.get("channel") or (evaluation.decision.channel if evaluation else None),
            "subject": envelope.get("subject"),
            "match_result": match_result,
            "decision": metric.router_decision,
            "reason": metric.details.get("router_reason"),
            "actions": actions,
            "diff": metric.to_dict(),
        }
        try:
            self.audit_writer.write(row, path_template=self.audit_path_template)
        except Exception:  # noqa: BLE001
            self._emit("auto.signal_router.shadow.audit.failed", {"route": metric.route_name})

    def _emit(self, topic: str, payload: dict[str, Any]) -> None:
        if self.telemetry_emitter is None:
            return
        try:
            result = self.telemetry_emitter(topic, dict(payload))
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(result)
                else:
                    loop.create_task(result)
        except Exception:  # noqa: BLE001
            return


def signal_to_envelope(signal: Signal) -> dict[str, Any]:
    """Convert a legacy signal object into the router envelope shape."""

    payload = signal.to_dict()
    strategy = str(payload.get("strategy") or "unknown")
    symbol = str(payload.get("symbol") or "?")
    return {
        "subject": f"signals.{strategy}.{symbol}",
        "channel": "trade_signals",
        "payload": payload,
        "received_at": payload.get("received_at"),
    }


async def _call_maybe_async(callback: Callable[..., Any], *args: Any) -> Any:
    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    result = await asyncio.to_thread(callback, *args)
    if inspect.isawaitable(result):
        return await result
    return result


def _coerce_legacy_decisions(outcome: Any) -> list[LegacyDecision]:
    if outcome is None:
        return [LegacyDecision(fired=True, reason="legacy_dispatched")]
    if isinstance(outcome, LegacyDecision):
        return [outcome]
    if isinstance(outcome, bool):
        return [LegacyDecision(fired=outcome, reason="legacy_bool")]
    if isinstance(outcome, int):
        return [LegacyDecision(fired=outcome > 0, reason="legacy_count", details={"count": outcome})]
    if isinstance(outcome, Mapping):
        return [
            LegacyDecision(
                fired=bool(outcome.get("fired")),
                route_name=str(outcome.get("route_name") or "legacy"),
                matched=bool(outcome.get("matched", True)),
                reason=(str(outcome["reason"]) if outcome.get("reason") is not None else None),
                details=dict(outcome),
            )
        ]
    if isinstance(outcome, list):
        decisions: list[LegacyDecision] = []
        for item in outcome:
            decisions.extend(_coerce_legacy_decisions(item))
        return decisions or [LegacyDecision(fired=False, matched=False, reason="legacy_empty")]
    return [LegacyDecision(fired=bool(outcome), reason="legacy_truthy")]


def _result_would_fire(result: ActionResult) -> bool:
    if result.status == ACTION_STATUS_FIRED:
        return True
    if result.status == ACTION_STATUS_SUPPRESSED_DRY_RUN:
        return result.detail in {None, "would_fire"} or bool(result.metrics.get("would_fire"))
    return False


def _suppression_reason(result: ActionResult) -> str:
    detail = str(result.detail or result.status or "unknown")
    return detail.replace("suppressed_", "").replace(" ", "_")


def _classify(
    legacy_fired: bool,
    evaluation: RouterEvaluation,
) -> DivergenceKind:
    router_fired = evaluation.fired
    if legacy_fired and router_fired:
        return DivergenceKind.AGREED_FIRE
    if not legacy_fired and not router_fired:
        return DivergenceKind.AGREED_SUPPRESS
    reason = evaluation.reason
    if legacy_fired and not router_fired:
        if "cooldown" in reason:
            return DivergenceKind.COOLDOWN_SKEW
        if "cap" in reason:
            return DivergenceKind.CAP_SKEW
        return DivergenceKind.LEGACY_FIRED_ROUTER_SUPPRESSED
    return DivergenceKind.LEGACY_SUPPRESSED_ROUTER_FIRED

"""Deterministic pre-LLM diagnostics for ASO optimization cycles."""

from __future__ import annotations

from dataclasses import dataclass

from agent.strategy_metrics import MetricsReport


@dataclass(frozen=True)
class DiagnosticResult:
    proceed: bool
    reason: str
    failure_mode: str | None
    bundled_hypothesis: bool


def diagnose_strategy(metrics: MetricsReport, iteration_history: list) -> DiagnosticResult:
    """Classify the strategy's weakest observable weakness."""
    del iteration_history  # Reserved for future convergence/stagnation heuristics.

    total_trades = metrics.trades.total
    sharpe = metrics.risk_adjusted.sharpe_ratio
    sortino = metrics.risk_adjusted.sortino_ratio
    max_drawdown = abs(metrics.drawdown.max_drawdown_pct)
    win_rate = metrics.trades.win_rate_pct
    profit_factor = metrics.trades.profit_factor

    if total_trades < 30:
        return DiagnosticResult(
            proceed=False,
            reason="INSUFFICIENT_EVIDENCE",
            failure_mode=None,
            bundled_hypothesis=False,
        )

    if (
        sharpe is not None
        and sharpe > 2.0
        and sortino is not None
        and sortino > 2.5
        and max_drawdown < 10.0
    ):
        return DiagnosticResult(
            proceed=False,
            reason="CONVERGED",
            failure_mode=None,
            bundled_hypothesis=False,
        )

    bundled_hypothesis = total_trades < 50 and (win_rate or 0.0) >= 60.0

    for predicate, failure_mode in (
        (max_drawdown > 20.0, "drawdown"),
        (sharpe is None or sharpe < 0.5, "poor_risk_adjusted"),
        (win_rate is None or win_rate < 40.0, "noisy_entries"),
        (profit_factor is None or profit_factor < 1.3, "poor_win_loss"),
        (sortino is None or sortino < 1.0, "downside_volatility"),
        (total_trades < 50, "low_signal"),
        (total_trades > 500, "over_trading"),
    ):
        if predicate:
            return DiagnosticResult(
                proceed=True,
                reason=f"PROCEED:{failure_mode}",
                failure_mode=failure_mode,
                bundled_hypothesis=bundled_hypothesis,
            )

    return DiagnosticResult(
        proceed=True,
        reason="PROCEED:general_improvement",
        failure_mode="general_improvement",
        bundled_hypothesis=bundled_hypothesis,
    )

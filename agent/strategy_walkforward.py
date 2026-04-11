"""Walk-forward validation for ASO strategies."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

import pandas as pd

from agent.strategy_executor import execute_strategy
from agent.strategy_ir import StrategyIR
from agent.strategy_metrics import MetricsReport, compute_metrics
from agent.strategy_sample_size import check_sample_size

MAX_DRAWDOWN_PCT = 20.0


@dataclass(frozen=True)
class FoldResult:
    fold_index: int
    metrics: MetricsReport
    sample_size_pass: bool
    sample_size_detail: str
    hard_constraint_pass: bool
    hard_constraint_detail: str


@dataclass(frozen=True)
class WalkForwardResult:
    folds: list[FoldResult]
    median_sharpe: float | None
    median_sortino: float | None
    median_calmar: float | None
    worst_max_drawdown_pct: float
    total_trades: int
    all_folds_pass: bool
    rejection_reason: str | None


def walk_forward_evaluate(ir: StrategyIR, ohlcv: pd.DataFrame, n_folds: int = 5) -> WalkForwardResult:
    """Evaluate a strategy across expanding-window walk-forward folds."""
    if n_folds <= 0:
        raise ValueError("n_folds must be positive")

    frame = _normalize_ohlcv(ohlcv)
    fold_ranges = _build_fold_ranges(len(frame), n_folds, ir.warmup_bars)

    folds: list[FoldResult] = []
    for fold_index, (train_end, test_end, test_window) in enumerate(fold_ranges):
        test_slice = frame.iloc[train_end:test_end]
        exec_slice = frame.iloc[train_end - ir.warmup_bars : test_end]

        backtest = execute_strategy(ir, exec_slice)
        metrics = compute_metrics(backtest.equity_curve, backtest.trades, backtest.benchmark_prices)
        avg_bars_held = metrics.trades.avg_duration_bars or 0.0
        sample_pass, sample_detail = check_sample_size(
            metrics.trades.total,
            avg_bars_held,
            len(test_slice),
            "walk_forward",
        )
        hard_pass, hard_detail = _hard_constraints_pass(metrics, sample_pass, sample_detail)
        folds.append(
            FoldResult(
                fold_index=fold_index,
                metrics=metrics,
                sample_size_pass=sample_pass,
                sample_size_detail=sample_detail,
                hard_constraint_pass=hard_pass,
                hard_constraint_detail=hard_detail,
            )
        )

    all_folds_pass = all(fold.hard_constraint_pass for fold in folds)
    rejection_reason = None if all_folds_pass else next(
        f"fold {fold.fold_index} failed: {fold.hard_constraint_detail}" for fold in folds if not fold.hard_constraint_pass
    )
    return WalkForwardResult(
        folds=folds,
        median_sharpe=_median_or_none(fold.metrics.risk_adjusted.sharpe_ratio for fold in folds),
        median_sortino=_median_or_none(fold.metrics.risk_adjusted.sortino_ratio for fold in folds),
        median_calmar=_median_or_none(fold.metrics.risk_adjusted.calmar_ratio for fold in folds),
        worst_max_drawdown_pct=min(fold.metrics.drawdown.max_drawdown_pct for fold in folds),
        total_trades=sum(fold.metrics.trades.total for fold in folds),
        all_folds_pass=all_folds_pass,
        rejection_reason=rejection_reason,
    )


def _normalize_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    frame = ohlcv.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _build_fold_ranges(total_bars: int, n_folds: int, warmup_bars: int) -> list[tuple[int, int, int]]:
    test_window = total_bars // (n_folds + 1)
    if test_window <= 0:
        raise ValueError("not enough bars for walk-forward folds")

    train_size = total_bars - (n_folds * test_window)
    if train_size <= warmup_bars:
        raise ValueError("not enough training bars to satisfy warmup")

    fold_ranges: list[tuple[int, int, int]] = []
    for fold_index in range(n_folds):
        train_end = train_size + (fold_index * test_window)
        test_end = train_end + test_window
        fold_ranges.append((train_end, test_end, test_window))
    return fold_ranges


def _hard_constraints_pass(metrics: MetricsReport, sample_pass: bool, sample_detail: str) -> tuple[bool, str]:
    failures: list[str] = []
    if not sample_pass:
        failures.append(sample_detail)

    if abs(metrics.drawdown.max_drawdown_pct) > MAX_DRAWDOWN_PCT:
        failures.append(f"max drawdown {abs(metrics.drawdown.max_drawdown_pct):.2f}% exceeds {MAX_DRAWDOWN_PCT:.2f}%")

    if failures:
        return False, "; ".join(failures)
    return True, "ok"


def _median_or_none(values) -> float | None:
    filtered = [value for value in values if value is not None]
    return None if not filtered else float(median(filtered))

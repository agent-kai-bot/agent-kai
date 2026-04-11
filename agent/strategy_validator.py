"""Validation ladder orchestration for ASO strategies."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agent.strategy_executor import execute_strategy
from agent.strategy_ir import StrategyIR
from agent.strategy_metrics import MetricsReport, compute_metrics
from agent.strategy_sample_size import check_sample_size
from agent.strategy_walkforward import MAX_DRAWDOWN_PCT, WalkForwardResult, walk_forward_evaluate


@dataclass(frozen=True)
class StageValidationResult:
    metrics: MetricsReport
    sample_size_pass: bool
    sample_size_detail: str
    hard_constraint_pass: bool
    hard_constraint_detail: str
    passed: bool
    rejection_reason: str | None


@dataclass(frozen=True)
class ValidationResult:
    in_sample: StageValidationResult
    walk_forward: WalkForwardResult
    lockbox: StageValidationResult | None
    overall_pass: bool
    rejection_reason: str | None


def validate_strategy(ir: StrategyIR, ohlcv_full: pd.DataFrame, lockbox_ohlcv: pd.DataFrame) -> ValidationResult:
    """Run the in-sample, walk-forward, and lockbox stages of the v1 ladder."""
    full_frame = _normalize_ohlcv(ohlcv_full)
    lockbox_frame = _normalize_ohlcv(lockbox_ohlcv)
    split_index = _in_sample_end(len(full_frame))
    in_sample_frame = full_frame.iloc[:split_index]

    in_sample_result = _evaluate_in_sample(ir, in_sample_frame)
    walk_forward_result = walk_forward_evaluate(ir, in_sample_frame)
    if not walk_forward_result.all_folds_pass:
        return ValidationResult(
            in_sample=in_sample_result,
            walk_forward=walk_forward_result,
            lockbox=None,
            overall_pass=False,
            rejection_reason=walk_forward_result.rejection_reason,
        )

    if len(in_sample_frame) <= ir.warmup_bars:
        raise ValueError("not enough in-sample bars to prepend lockbox warmup")

    lockbox_exec = pd.concat([in_sample_frame.iloc[-ir.warmup_bars :], lockbox_frame])
    lockbox_result = _evaluate_gated_stage(ir, lockbox_exec, len(lockbox_frame), "lockbox")
    return ValidationResult(
        in_sample=in_sample_result,
        walk_forward=walk_forward_result,
        lockbox=lockbox_result,
        overall_pass=lockbox_result.passed,
        rejection_reason=lockbox_result.rejection_reason,
    )


def _normalize_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    frame = ohlcv.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _in_sample_end(total_bars: int) -> int:
    split_index = int(total_bars * 0.6)
    if split_index <= 0:
        raise ValueError("not enough bars for in-sample validation")
    return split_index


def _evaluate_in_sample(ir: StrategyIR, in_sample_frame: pd.DataFrame) -> StageValidationResult:
    backtest = execute_strategy(ir, in_sample_frame)
    metrics = compute_metrics(backtest.equity_curve, backtest.trades, backtest.benchmark_prices)
    return StageValidationResult(
        metrics=metrics,
        sample_size_pass=True,
        sample_size_detail="not gated for in_sample",
        hard_constraint_pass=True,
        hard_constraint_detail="not gated for in_sample",
        passed=True,
        rejection_reason=None,
    )


def _evaluate_gated_stage(
    ir: StrategyIR,
    ohlcv: pd.DataFrame,
    scored_bars: int,
    stage: str,
) -> StageValidationResult:
    backtest = execute_strategy(ir, ohlcv)
    metrics = compute_metrics(backtest.equity_curve, backtest.trades, backtest.benchmark_prices)
    avg_bars_held = metrics.trades.avg_duration_bars or 0.0
    sample_pass, sample_detail = check_sample_size(metrics.trades.total, avg_bars_held, scored_bars, stage)
    hard_pass, hard_detail = _hard_constraints(metrics, sample_pass, sample_detail)
    return StageValidationResult(
        metrics=metrics,
        sample_size_pass=sample_pass,
        sample_size_detail=sample_detail,
        hard_constraint_pass=hard_pass,
        hard_constraint_detail=hard_detail,
        passed=hard_pass,
        rejection_reason=None if hard_pass else hard_detail,
    )


def _hard_constraints(metrics: MetricsReport, sample_pass: bool, sample_detail: str) -> tuple[bool, str]:
    failures: list[str] = []
    if not sample_pass:
        failures.append(sample_detail)
    if abs(metrics.drawdown.max_drawdown_pct) > MAX_DRAWDOWN_PCT:
        failures.append(f"max drawdown {abs(metrics.drawdown.max_drawdown_pct):.2f}% exceeds {MAX_DRAWDOWN_PCT:.2f}%")
    if failures:
        return False, "; ".join(failures)
    return True, "ok"

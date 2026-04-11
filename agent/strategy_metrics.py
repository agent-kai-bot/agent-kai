"""Metrics pipeline for ASO strategy backtests."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd

from agent.strategy_executor import TradeRecord


@dataclass(frozen=True)
class ReturnMetrics:
    total_pct: float
    annualized_pct: float | None
    monthly_returns: dict[str, float]


@dataclass(frozen=True)
class RiskAdjustedMetrics:
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None


@dataclass(frozen=True)
class DrawdownMetrics:
    max_drawdown_pct: float
    max_drawdown_duration_days: float
    avg_drawdown_pct: float
    recovery_factor: float | None
    underwater_curve: list[float]


@dataclass(frozen=True)
class TradeMetrics:
    total: int
    winners: int
    losers: int
    win_rate_pct: float | None
    profit_factor: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    largest_win_pct: float | None
    largest_loss_pct: float | None
    avg_duration_bars: float | None


@dataclass(frozen=True)
class BenchmarkMetrics:
    cash_return_pct: float
    buy_and_hold_return_pct: float | None
    alpha_pct: float | None
    beta: float | None
    correlation: float | None


@dataclass(frozen=True)
class TailRiskMetrics:
    cvar_95_pct: float | None
    cvar_99_pct: float | None
    time_under_water_bars: int
    time_under_water_pct: float


@dataclass(frozen=True)
class StabilityMetrics:
    monthly_return_stddev: float | None
    positive_months_pct: float | None
    longest_losing_streak: int


@dataclass(frozen=True)
class CostAnalysisMetrics:
    gross_return_pct: float
    net_return_pct: float
    fee_burden_pct_of_gross: float | None


@dataclass(frozen=True)
class MetricsReport:
    returns: ReturnMetrics
    risk_adjusted: RiskAdjustedMetrics
    drawdown: DrawdownMetrics
    trades: TradeMetrics
    benchmark: BenchmarkMetrics
    tail_risk: TailRiskMetrics
    stability: StabilityMetrics
    cost_analysis: CostAnalysisMetrics


def compute_metrics(
    equity_curve: pd.Series,
    trades: list[TradeRecord],
    benchmark_prices: pd.Series | None,
) -> MetricsReport:
    """Compute the standardized ASO v1 metrics suite."""
    equity = _coerce_series(equity_curve, "equity_curve")
    if equity.empty:
        raise ValueError("equity_curve must not be empty")

    returns = equity.pct_change().dropna()
    periods_per_year = _periods_per_year(equity.index)
    monthly_returns = equity.resample("ME").last().pct_change().dropna()
    underwater, drawdown_durations = _drawdown_profile(equity)
    trade_returns = np.array([trade.return_pct for trade in trades], dtype=float) if trades else np.array([])
    trade_pnls = np.array([trade.net_pnl for trade in trades], dtype=float) if trades else np.array([])

    total_return = (float(equity.iloc[-1]) / float(equity.iloc[0])) - 1.0
    annualized_return = _annualized_return(equity, periods_per_year)
    max_drawdown = float(underwater.min()) if not underwater.empty else 0.0
    total_fees = float(sum(trade.fees_paid for trade in trades))
    initial_capital = float(trades[0].entry_equity_before) if trades else float(equity.iloc[0])
    gross_total_pnl = float(sum(trade.gross_pnl for trade in trades))
    gross_return_pct = ((initial_capital + gross_total_pnl) / initial_capital - 1.0) * 100.0

    benchmark = _benchmark_metrics(equity, benchmark_prices, periods_per_year)
    return MetricsReport(
        returns=ReturnMetrics(
            total_pct=total_return * 100.0,
            annualized_pct=None if annualized_return is None else annualized_return * 100.0,
            monthly_returns={index.strftime("%Y-%m"): value * 100.0 for index, value in monthly_returns.items()},
        ),
        risk_adjusted=RiskAdjustedMetrics(
            sharpe_ratio=_annualized_ratio(returns, periods_per_year),
            sortino_ratio=_sortino_ratio(returns, periods_per_year),
            calmar_ratio=None if max_drawdown == 0 else (annualized_return / abs(max_drawdown) if annualized_return is not None else None),
        ),
        drawdown=DrawdownMetrics(
            max_drawdown_pct=max_drawdown * 100.0,
            max_drawdown_duration_days=max(drawdown_durations, default=0.0),
            avg_drawdown_pct=_average_drawdown(underwater) * 100.0,
            recovery_factor=None if max_drawdown == 0 else total_return / abs(max_drawdown),
            underwater_curve=[value * 100.0 for value in underwater.tolist()],
        ),
        trades=TradeMetrics(
            total=len(trades),
            winners=int(np.sum(trade_pnls > 0)) if len(trades) else 0,
            losers=int(np.sum(trade_pnls < 0)) if len(trades) else 0,
            win_rate_pct=(float(np.mean(trade_pnls > 0)) * 100.0) if len(trades) else None,
            profit_factor=_profit_factor(trade_pnls),
            avg_win_pct=(float(np.mean(trade_returns[trade_returns > 0])) * 100.0) if np.any(trade_returns > 0) else None,
            avg_loss_pct=(float(np.mean(trade_returns[trade_returns < 0])) * 100.0) if np.any(trade_returns < 0) else None,
            largest_win_pct=(float(np.max(trade_returns)) * 100.0) if len(trade_returns) else None,
            largest_loss_pct=(float(np.min(trade_returns)) * 100.0) if len(trade_returns) else None,
            avg_duration_bars=float(np.mean([trade.bars_held for trade in trades])) if trades else None,
        ),
        benchmark=benchmark,
        tail_risk=TailRiskMetrics(
            cvar_95_pct=_cvar(returns, 0.95),
            cvar_99_pct=_cvar(returns, 0.99),
            time_under_water_bars=int((underwater < 0).sum()),
            time_under_water_pct=float((underwater < 0).mean() * 100.0) if not underwater.empty else 0.0,
        ),
        stability=StabilityMetrics(
            monthly_return_stddev=(float(monthly_returns.std(ddof=0)) * 100.0) if not monthly_returns.empty else None,
            positive_months_pct=(float((monthly_returns > 0).mean()) * 100.0) if not monthly_returns.empty else None,
            longest_losing_streak=_longest_losing_streak(trade_pnls),
        ),
        cost_analysis=CostAnalysisMetrics(
            gross_return_pct=gross_return_pct,
            net_return_pct=total_return * 100.0,
            fee_burden_pct_of_gross=(total_fees / gross_total_pnl * 100.0) if gross_total_pnl > 0 else None,
        ),
    )


def _coerce_series(series: pd.Series, name: str) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise TypeError(f"{name} must be a pandas Series")
    coerced = series.astype(float).sort_index()
    if not isinstance(coerced.index, pd.DatetimeIndex):
        coerced.index = pd.to_datetime(coerced.index)
    return coerced


def _periods_per_year(index: pd.DatetimeIndex) -> float | None:
    if len(index) < 2:
        return None
    deltas = index.to_series().diff().dropna().dt.total_seconds()
    if deltas.empty or deltas.median() <= 0:
        return None
    seconds_per_year = 365.25 * 24 * 60 * 60
    return float(seconds_per_year / deltas.median())


def _annualized_return(equity: pd.Series, periods_per_year: float | None) -> float | None:
    if periods_per_year is None or len(equity) < 2:
        return None
    total_return = float(equity.iloc[-1]) / float(equity.iloc[0])
    periods = len(equity) - 1
    if total_return <= 0 or periods <= 0:
        return None
    return total_return ** (periods_per_year / periods) - 1.0


def _annualized_ratio(returns: pd.Series, periods_per_year: float | None) -> float | None:
    if periods_per_year is None or returns.empty:
        return None
    std = float(returns.std(ddof=0))
    if std == 0:
        return None
    return float(returns.mean()) / std * sqrt(periods_per_year)


def _sortino_ratio(returns: pd.Series, periods_per_year: float | None) -> float | None:
    if periods_per_year is None or returns.empty:
        return None
    downside = returns[returns < 0]
    if downside.empty:
        return None
    downside_std = float(downside.std(ddof=0))
    if downside_std == 0:
        return None
    return float(returns.mean()) / downside_std * sqrt(periods_per_year)


def _drawdown_profile(equity: pd.Series) -> tuple[pd.Series, list[float]]:
    running_max = equity.cummax()
    underwater = (equity / running_max) - 1.0
    durations: list[float] = []
    start: pd.Timestamp | None = None
    for timestamp, value in underwater.items():
        if value < 0 and start is None:
            start = timestamp
        if value >= 0 and start is not None:
            durations.append((timestamp - start).total_seconds() / 86400.0)
            start = None
    if start is not None:
        durations.append((underwater.index[-1] - start).total_seconds() / 86400.0)
    return underwater, durations


def _average_drawdown(underwater: pd.Series) -> float:
    drawdowns = underwater[underwater < 0]
    return float(drawdowns.mean()) if not drawdowns.empty else 0.0


def _profit_factor(trade_pnls: np.ndarray) -> float | None:
    if trade_pnls.size == 0:
        return None
    gross_profit = float(trade_pnls[trade_pnls > 0].sum())
    gross_loss = float(abs(trade_pnls[trade_pnls < 0].sum()))
    if gross_loss == 0:
        return None if gross_profit == 0 else float("inf")
    return gross_profit / gross_loss


def _cvar(returns: pd.Series, confidence: float) -> float | None:
    if returns.empty:
        return None
    threshold = returns.quantile(1.0 - confidence)
    tail = returns[returns <= threshold]
    if tail.empty:
        return None
    return float(tail.mean() * 100.0)


def _longest_losing_streak(trade_pnls: np.ndarray) -> int:
    longest = 0
    current = 0
    for pnl in trade_pnls:
        if pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _benchmark_metrics(
    equity: pd.Series,
    benchmark_prices: pd.Series | None,
    periods_per_year: float | None,
) -> BenchmarkMetrics:
    if benchmark_prices is None:
        return BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=None,
            alpha_pct=None,
            beta=None,
            correlation=None,
        )

    benchmark = _coerce_series(benchmark_prices, "benchmark_prices")
    aligned = pd.concat([equity, benchmark], axis=1, join="inner").dropna()
    if aligned.empty:
        return BenchmarkMetrics(
            cash_return_pct=0.0,
            buy_and_hold_return_pct=None,
            alpha_pct=None,
            beta=None,
            correlation=None,
        )

    strategy_returns = aligned.iloc[:, 0].pct_change().dropna()
    benchmark_returns = aligned.iloc[:, 1].pct_change().dropna()
    joined_returns = pd.concat([strategy_returns, benchmark_returns], axis=1, join="inner").dropna()

    beta = None
    alpha_pct = None
    correlation = None
    if not joined_returns.empty:
        strategy_values = joined_returns.iloc[:, 0]
        benchmark_values = joined_returns.iloc[:, 1]
        benchmark_variance = float(benchmark_values.var(ddof=0))
        correlation = float(strategy_values.corr(benchmark_values))
        if benchmark_variance > 0:
            covariance = float(np.cov(strategy_values, benchmark_values, ddof=0)[0, 1])
            beta = covariance / benchmark_variance
            if periods_per_year is not None:
                alpha_pct = (float(strategy_values.mean()) - (beta * float(benchmark_values.mean()))) * periods_per_year * 100.0

    buy_and_hold = (float(aligned.iloc[-1, 1]) / float(aligned.iloc[0, 1]) - 1.0) * 100.0
    return BenchmarkMetrics(
        cash_return_pct=0.0,
        buy_and_hold_return_pct=buy_and_hold,
        alpha_pct=alpha_pct,
        beta=beta,
        correlation=correlation,
    )

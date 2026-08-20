"""Risk metrics and performance analytics — all bar-by-bar, not trade-by-trade.

ADR-003: bar-by-bar Sharpe is directly comparable to prime broker reporting
and penalizes idle capital. Trade-by-trade Sharpe has too few data points.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

from src.models.portfolio import EquityPoint
from src.models.trade import Trade

if TYPE_CHECKING:
    from src.engine.backtest import BacktestResult


class MetricName(StrEnum):
    """Metrics that may be used as permutation-test statistics."""

    SHARPE_RATIO = "sharpe_ratio"
    SORTINO_RATIO = "sortino_ratio"
    CAGR = "cagr"
    MAX_DRAWDOWN = "max_drawdown"
    MAX_DRAWDOWN_DURATION = "max_drawdown_duration"
    WIN_RATE = "win_rate"
    PROFIT_FACTOR = "profit_factor"
    CALMAR_RATIO = "calmar_ratio"
    TOTAL_TRADES = "total_trades"
    TOTAL_RETURN = "total_return"


def parse_metric_name(metric: MetricName | str) -> MetricName:
    """Return a supported metric name or fail closed on unknown input."""
    try:
        return MetricName(metric)
    except ValueError as error:
        raise ValueError(f"Unsupported permutation metric: {metric!r}") from error


def require_metric(metrics: Mapping[str, float], metric: MetricName) -> float:
    """Read a declared metric without converting contract drift into zero."""
    try:
        return metrics[metric.value]
    except KeyError as error:
        message = f"Declared permutation metric {metric.value!r} is missing"
        raise RuntimeError(message) from error


def equity_curve_to_returns(equity_curve: list[EquityPoint]) -> list[float]:
    """Convert equity curve to bar-by-bar percentage returns."""
    if len(equity_curve) < 2:
        return []
    equities = [pt.equity for pt in equity_curve]
    return [
        (equities[i] - equities[i - 1]) / equities[i - 1]
        for i in range(1, len(equities))
        if equities[i - 1] != 0
    ]


def sharpe_ratio(
    returns: list[float],
    risk_free_rate: float = 0.04,
    periods_per_year: int = 252,
) -> float:
    """Annualized Sharpe ratio from bar-by-bar returns."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    std = float(np.std(arr, ddof=1))
    if std < 1e-10:
        return 0.0
    daily_rf = risk_free_rate / periods_per_year
    excess_mean = float(np.mean(arr)) - daily_rf
    return float(excess_mean / std * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: list[float],
    risk_free_rate: float = 0.04,
    periods_per_year: int = 252,
) -> float:
    """Sharpe variant using only downside deviation in the denominator.

    Downside deviation is target semideviation relative to the daily risk-free
    rate, averaged across all observations. Returns 0.0 for fewer than two
    observations or negligible downside deviation.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    daily_rf = risk_free_rate / periods_per_year
    downside_gaps = np.minimum(arr - daily_rf, 0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside_gaps))))
    if downside_deviation < 1e-10:
        return 0.0
    excess_mean = float(np.mean(arr)) - daily_rf
    return float(excess_mean / downside_deviation * math.sqrt(periods_per_year))


def cagr(equity_curve: list[EquityPoint], periods_per_year: int = 252) -> float:
    """Compound Annual Growth Rate."""
    if len(equity_curve) < 2:
        return 0.0
    initial = equity_curve[0].equity
    final = equity_curve[-1].equity
    if initial <= 0:
        return 0.0
    n_bars = len(equity_curve) - 1
    return float((final / initial) ** (periods_per_year / n_bars) - 1)


def max_drawdown(equity_curve: list[EquityPoint]) -> float:
    """Peak-to-trough drawdown as a negative fraction.

    Example: equities [100, 110, 90, 95, 80, 100] → peak=110, trough=80
    drawdown = (80 - 110) / 110 = -0.27272...
    """
    if not equity_curve:
        return 0.0
    equities = np.array([pt.equity for pt in equity_curve], dtype=float)
    peak = equities[0]
    min_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak if peak > 0 else 0.0
        if dd < min_dd:
            min_dd = dd
    return float(min_dd)


def max_drawdown_duration(equity_curve: list[EquityPoint]) -> int:
    """Longest drawdown period in bars."""
    if not equity_curve:
        return 0
    equities = [pt.equity for pt in equity_curve]
    peak = equities[0]
    peak_idx = 0
    max_duration = 0
    for i, eq in enumerate(equities):
        if eq >= peak:
            peak = eq
            peak_idx = i
        else:
            duration = i - peak_idx
            if duration > max_duration:
                max_duration = duration
    return max_duration


def win_rate(trades: list[Trade]) -> float:
    """Fraction of closed winning trades."""
    closed = [t for t in trades if t.is_closed]
    if not closed:
        return 0.0
    wins = sum(1 for t in closed if t.pnl > 0)
    return wins / len(closed)


def profit_factor(trades: list[Trade]) -> float:
    """Gross profit / gross loss for closed trades."""
    closed = [t for t in trades if t.is_closed]
    gross_profit = sum(t.pnl for t in closed if t.pnl > 0)
    gross_loss = sum(abs(t.pnl) for t in closed if t.pnl < 0)
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def calmar_ratio(equity_curve: list[EquityPoint], periods_per_year: int = 252) -> float:
    """CAGR / |max_drawdown|."""
    mdd = max_drawdown(equity_curve)
    if mdd == 0:
        return 0.0
    return cagr(equity_curve, periods_per_year) / abs(mdd)


def compute_all_metrics(result: BacktestResult) -> dict[str, float]:
    """Compute all standard metrics for a BacktestResult."""
    returns = equity_curve_to_returns(result.equity_curve)
    closed_trades = [t for t in result.trades if t.is_closed]
    total_return = (
        (result.final_equity - result.initial_capital) / result.initial_capital
        if result.initial_capital > 0
        else 0.0
    )

    return {
        MetricName.SHARPE_RATIO.value: sharpe_ratio(returns),
        MetricName.SORTINO_RATIO.value: sortino_ratio(returns),
        MetricName.CAGR.value: cagr(result.equity_curve),
        MetricName.MAX_DRAWDOWN.value: max_drawdown(result.equity_curve),
        MetricName.MAX_DRAWDOWN_DURATION.value: float(
            max_drawdown_duration(result.equity_curve)
        ),
        MetricName.WIN_RATE.value: win_rate(result.trades),
        MetricName.PROFIT_FACTOR.value: profit_factor(result.trades),
        MetricName.CALMAR_RATIO.value: calmar_ratio(result.equity_curve),
        MetricName.TOTAL_TRADES.value: float(len(closed_trades)),
        MetricName.TOTAL_RETURN.value: total_return,
    }

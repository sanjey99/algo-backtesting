"""Risk metrics and performance analytics — all bar-by-bar, not trade-by-trade.

ADR-003: bar-by-bar Sharpe is directly comparable to prime broker reporting
and penalizes idle capital. Trade-by-trade Sharpe has too few data points.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from src.models.portfolio import EquityPoint
from src.models.trade import Trade

if TYPE_CHECKING:
    from src.engine.backtest import BacktestResult


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

    Downside deviation uses only negative (below zero) returns, per spec.
    Returns 0.0 if no negative returns or fewer than 2 returns.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    daily_rf = risk_free_rate / periods_per_year
    # Downside: only bars with negative returns (not excess)
    negative = arr[arr < 0]
    if len(negative) == 0:
        return 0.0
    if len(negative) == 1:
        downside_std = float(np.abs(negative[0]))
    else:
        downside_std = float(np.std(negative, ddof=1))
    if downside_std == 0:
        return 0.0
    excess_mean = float(np.mean(arr)) - daily_rf
    return float(excess_mean / downside_std * math.sqrt(periods_per_year))


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
        "sharpe_ratio": sharpe_ratio(returns),
        "sortino_ratio": sortino_ratio(returns),
        "cagr": cagr(result.equity_curve),
        "max_drawdown": max_drawdown(result.equity_curve),
        "max_drawdown_duration": float(max_drawdown_duration(result.equity_curve)),
        "win_rate": win_rate(result.trades),
        "profit_factor": profit_factor(result.trades),
        "calmar_ratio": calmar_ratio(result.equity_curve),
        "total_trades": float(len(closed_trades)),
        "total_return": total_return,
    }

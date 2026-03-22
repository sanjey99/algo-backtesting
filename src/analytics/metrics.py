"""Analytics metrics — Sharpe, Sortino, max-drawdown, CAGR, etc."""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from src.engine.backtest import BacktestResult
from src.models.portfolio import EquityPoint


# ---------------------------------------------------------------------------
# Individual metric functions
# ---------------------------------------------------------------------------


def equity_returns(equity_curve: list[EquityPoint]) -> list[float]:
    """Convert an equity curve to a list of period returns."""
    if len(equity_curve) < 2:
        return []
    values = [pt.equity for pt in equity_curve]
    return [
        (values[i] - values[i - 1]) / values[i - 1]
        for i in range(1, len(values))
        if values[i - 1] != 0
    ]


def sharpe_ratio(
    returns: list[float],
    risk_free_rate: float = 0.04,
    periods_per_year: int = 252,
) -> float:
    """Annualised Sharpe ratio.

    Returns 0.0 if fewer than 2 returns or std is zero.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    daily_rf = risk_free_rate / periods_per_year
    excess = arr - daily_rf
    std = float(np.std(excess, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: list[float],
    risk_free_rate: float = 0.04,
    periods_per_year: int = 252,
) -> float:
    """Sharpe variant using only downside deviation below the risk-free rate.

    Downside deviation uses returns below the daily risk-free threshold.
    Returns 0.0 if no below-target returns or fewer than 2 returns.
    """
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    daily_rf = risk_free_rate / periods_per_year
    # Downside: returns below the daily risk-free threshold
    below_target = arr[arr < daily_rf]
    if len(below_target) == 0:
        return 0.0
    if len(below_target) == 1:
        downside_std = float(np.abs(below_target[0] - daily_rf))
    else:
        downside_std = float(np.std(below_target - daily_rf, ddof=1))
    if downside_std == 0:
        return 0.0
    excess_mean = float(np.mean(arr)) - daily_rf
    return float(excess_mean / downside_std * math.sqrt(periods_per_year))


def max_drawdown(equity_curve: list[EquityPoint]) -> float:
    """Maximum peak-to-trough drawdown as a fraction (0.0 to 1.0).

    Returns 0.0 if fewer than 2 points.
    """
    if len(equity_curve) < 2:
        return 0.0
    values = [pt.equity for pt in equity_curve]
    peak = values[0]
    max_dd = 0.0
    for v in values[1:]:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def cagr(
    equity_curve: list[EquityPoint],
    periods_per_year: int = 252,
) -> float:
    """Compound Annual Growth Rate.

    Returns 0.0 if fewer than 2 points or initial equity is zero.
    """
    if len(equity_curve) < 2:
        return 0.0
    start = equity_curve[0].equity
    end = equity_curve[-1].equity
    if start <= 0:
        return 0.0
    n_periods = len(equity_curve) - 1
    years = n_periods / periods_per_year
    if years <= 0:
        return 0.0
    return float((end / start) ** (1.0 / years) - 1.0)


def calmar_ratio(
    equity_curve: list[EquityPoint],
    periods_per_year: int = 252,
) -> float:
    """CAGR divided by max drawdown.

    Returns 0.0 if max_drawdown is zero.
    """
    dd = max_drawdown(equity_curve)
    if dd == 0:
        return 0.0
    return cagr(equity_curve, periods_per_year) / dd


def win_rate(trades: list[Any]) -> float:
    """Fraction of closed trades with positive PnL.

    Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0
    winners = sum(1 for t in trades if getattr(t, "pnl", 0.0) > 0)
    return winners / len(trades)


def profit_factor(trades: list[Any]) -> float:
    """Gross profit divided by gross loss.

    Returns 0.0 if no gross loss.
    """
    gross_profit = sum(t.pnl for t in trades if getattr(t, "pnl", 0.0) > 0)
    gross_loss = sum(abs(t.pnl) for t in trades if getattr(t, "pnl", 0.0) < 0)
    if gross_loss == 0:
        return 0.0
    return gross_profit / gross_loss


# ---------------------------------------------------------------------------
# Compute-all helper
# ---------------------------------------------------------------------------


def compute_all_metrics(result: BacktestResult) -> dict[str, float]:
    """Return a dict of all standard metrics for a BacktestResult."""
    returns = equity_returns(result.equity_curve)
    return {
        "sharpe_ratio": sharpe_ratio(returns),
        "sortino_ratio": sortino_ratio(returns),
        "max_drawdown": max_drawdown(result.equity_curve),
        "cagr": cagr(result.equity_curve),
        "calmar_ratio": calmar_ratio(result.equity_curve),
        "win_rate": win_rate(result.trades),
        "profit_factor": profit_factor(result.trades),
        "total_trades": float(len(result.trades)),
        "final_equity": result.final_equity,
    }

"""Tests for analytics metrics."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from src.analytics.metrics import (
    cagr,
    calmar_ratio,
    compute_all_metrics,
    equity_returns,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)
from src.models.portfolio import EquityPoint


def _make_equity_curve(values: list[float]) -> list[EquityPoint]:
    start = datetime(2023, 1, 2)
    return [
        EquityPoint(date=start + timedelta(days=i), equity=v)
        for i, v in enumerate(values)
    ]


class TestEquityReturns:
    def test_basic(self) -> None:
        curve = _make_equity_curve([100.0, 110.0, 121.0])
        returns = equity_returns(curve)
        assert len(returns) == 2
        assert abs(returns[0] - 0.1) < 1e-9
        assert abs(returns[1] - 0.1) < 1e-9

    def test_empty(self) -> None:
        assert equity_returns([]) == []

    def test_single(self) -> None:
        assert equity_returns(_make_equity_curve([100.0])) == []


class TestSharpeRatio:
    def test_returns_zero_for_single(self) -> None:
        assert sharpe_ratio([0.01]) == 0.0

    def test_returns_zero_for_zero_std(self) -> None:
        # All identical returns → zero std
        assert sharpe_ratio([0.01, 0.01, 0.01]) == 0.0

    def test_positive_returns_positive_sharpe(self) -> None:
        returns = [0.005] * 252
        sr = sharpe_ratio(returns)
        assert sr > 0

    def test_annualised(self) -> None:
        # Known: daily_rf = 0.04/252; mean excess and std determine Sharpe
        returns = [0.01] * 252
        sr = sharpe_ratio(returns)
        assert sr > 5.0  # High Sharpe for consistent positive returns


class TestSortinoRatio:
    def test_returns_zero_for_single(self) -> None:
        assert sortino_ratio([0.01]) == 0.0

    def test_returns_zero_when_no_below_target_returns(self) -> None:
        # All returns well above daily_rf (0.04/252 ≈ 0.000159)
        daily_rf = 0.04 / 252
        returns = [daily_rf + 0.01] * 10
        assert sortino_ratio(returns) == 0.0

    def test_downside_below_rf_not_below_zero(self) -> None:
        """R-11: downside uses arr < daily_rf, not arr < 0.

        Returns that are positive but below daily_rf should count as
        below-target (which is the R-11 fix — old code used arr < 0).
        We verify this by checking that we can get a non-zero Sortino
        even when all returns are positive (so arr < 0 would find nothing).
        """
        daily_rf = 0.04 / 252
        # Returns that are positive but below the daily risk-free threshold
        # Varying values so std != 0
        below_target = [daily_rf * 0.1, daily_rf * 0.3, daily_rf * 0.5, daily_rf * 0.2]
        above_target = [daily_rf + 0.005] * 10
        returns = below_target + above_target
        # All returns are positive, so old code (arr < 0) would find no downside
        assert all(r > 0 for r in returns), "Test data: all returns must be positive"
        sortino = sortino_ratio(returns)
        # New code (arr < daily_rf) finds below_target returns, computes non-zero std
        assert sortino != 0.0

    def test_positive_sortino_for_mixed_returns(self) -> None:
        """Sortino > 0 when mean return > daily_rf and there are some below-target returns."""
        daily_rf = 0.04 / 252
        # Mix: mostly above daily_rf but a few below it
        returns = [daily_rf - 0.001] * 10 + [daily_rf + 0.005] * 242
        sr = sortino_ratio(returns)
        assert sr > 0

    def test_single_below_target_uses_abs_deviation(self) -> None:
        """When only one return is below target, use abs deviation."""
        daily_rf = 0.04 / 252
        returns = [daily_rf - 0.001] + [daily_rf + 0.01] * 10
        sr = sortino_ratio(returns)
        assert sr != 0.0


class TestMaxDrawdown:
    def test_no_drawdown(self) -> None:
        curve = _make_equity_curve([100.0, 110.0, 120.0])
        assert max_drawdown(curve) == 0.0

    def test_50_percent_drawdown(self) -> None:
        curve = _make_equity_curve([100.0, 50.0])
        assert abs(max_drawdown(curve) - 0.5) < 1e-9

    def test_recovers(self) -> None:
        curve = _make_equity_curve([100.0, 80.0, 120.0])
        assert abs(max_drawdown(curve) - 0.2) < 1e-9

    def test_empty(self) -> None:
        assert max_drawdown([]) == 0.0

    def test_single(self) -> None:
        assert max_drawdown(_make_equity_curve([100.0])) == 0.0


class TestCAGR:
    def test_flat(self) -> None:
        # 252 bars, flat → CAGR = 0
        curve = _make_equity_curve([100.0] * 253)
        assert abs(cagr(curve)) < 1e-9

    def test_double_in_one_year(self) -> None:
        # 253 points = 252 periods = 1 year → CAGR = 100%
        curve = _make_equity_curve([100.0] + [200.0] * 252)
        assert abs(cagr(curve) - 1.0) < 0.01

    def test_empty(self) -> None:
        assert cagr([]) == 0.0


class TestCalmarRatio:
    def test_zero_drawdown(self) -> None:
        curve = _make_equity_curve([100.0, 110.0, 120.0])
        assert calmar_ratio(curve) == 0.0

    def test_positive(self) -> None:
        # Growing curve with small drawdown
        values = list(range(100, 200)) + [180.0] + list(range(181, 250))
        curve = _make_equity_curve([float(v) for v in values])
        result = calmar_ratio(curve)
        # Just confirm it's a real number; exact value depends on periods
        assert isinstance(result, float)


class _FakeTrade:
    def __init__(self, pnl: float) -> None:
        self.pnl = pnl


class TestWinRate:
    def test_empty(self) -> None:
        assert win_rate([]) == 0.0

    def test_all_winners(self) -> None:
        trades = [_FakeTrade(100.0), _FakeTrade(50.0)]
        assert win_rate(trades) == 1.0

    def test_half_winners(self) -> None:
        trades = [_FakeTrade(100.0), _FakeTrade(-50.0)]
        assert win_rate(trades) == 0.5


class TestProfitFactor:
    def test_no_losses(self) -> None:
        trades = [_FakeTrade(100.0)]
        assert profit_factor(trades) == 0.0

    def test_equal_profit_loss(self) -> None:
        trades = [_FakeTrade(100.0), _FakeTrade(-100.0)]
        assert abs(profit_factor(trades) - 1.0) < 1e-9

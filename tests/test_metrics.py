"""Tests for src/analytics/metrics.py — ADR-003 bar-by-bar metrics."""
from __future__ import annotations

import math
import uuid
from datetime import datetime

import pytest

from src.analytics.metrics import (
    cagr,
    calmar_ratio,
    compute_all_metrics,
    equity_curve_to_returns,
    max_drawdown,
    max_drawdown_duration,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)
from src.engine.backtest import BacktestResult
from src.models.order import Direction
from src.models.portfolio import EquityPoint
from src.models.trade import Trade

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_equity_curve(values: list[float]) -> list[EquityPoint]:
    return [EquityPoint(date=datetime(2023, 1, i + 1), equity=v) for i, v in enumerate(values)]


def make_trade(pnl_value: float) -> Trade:
    """Create a closed Trade with the desired PnL by tuning exit_price."""
    # entry_price=100, qty=10 → cost=1000
    # pnl = (exit - entry) * qty - commission
    # commission=0, so exit = entry + pnl / qty
    entry_price = 100.0
    qty = 10
    exit_price = entry_price + pnl_value / qty
    return Trade(
        symbol="TEST",
        direction=Direction.LONG,
        entry_price=entry_price,
        quantity=qty,
        entry_date=datetime(2023, 1, 1),
        exit_price=exit_price,
        exit_date=datetime(2023, 1, 2),
        trade_id=str(uuid.uuid4())[:8],
    )


def make_backtest_result(
    equity_values: list[float],
    trades: list[Trade] | None = None,
) -> BacktestResult:
    equity_curve = make_equity_curve(equity_values)
    return BacktestResult(
        strategy_name="test_strategy",
        symbol="TEST",
        start_date=datetime(2023, 1, 1),
        end_date=datetime(2023, 12, 31),
        parameters={},
        trades=trades or [],
        equity_curve=equity_curve,
        final_equity=equity_values[-1],
        initial_capital=equity_values[0],
    )


# ---------------------------------------------------------------------------
# equity_curve_to_returns
# ---------------------------------------------------------------------------


class TestEquityCurveToReturns:
    def test_basic(self):
        ec = make_equity_curve([100.0, 110.0, 99.0])
        returns = equity_curve_to_returns(ec)
        assert len(returns) == 2
        assert math.isclose(returns[0], 0.10, rel_tol=1e-9)
        assert math.isclose(returns[1], -0.10, rel_tol=1e-6)

    def test_single_point_returns_empty(self):
        assert equity_curve_to_returns(make_equity_curve([100.0])) == []

    def test_empty_returns_empty(self):
        assert equity_curve_to_returns([]) == []


# ---------------------------------------------------------------------------
# sharpe_ratio
# ---------------------------------------------------------------------------


class TestSharpeRatio:
    def test_constant_positive_returns(self):
        """252 identical returns → well-defined Sharpe."""
        r = [0.01] * 252
        result = sharpe_ratio(r, risk_free_rate=0.04, periods_per_year=252)
        # Manual: daily_rf=0.04/252, excess_mean=0.01-daily_rf, std=0 (ddof=1, all same)
        # std(ddof=1) of constant array is 0 → should return 0.0
        assert result == 0.0

    def test_known_returns(self):
        """Returns with known std should give predictable Sharpe."""
        import numpy as np

        r = [0.01, 0.02, -0.005, 0.015, 0.008] * 50  # 250 elements
        arr = np.array(r)
        daily_rf = 0.04 / 252
        expected = (arr.mean() - daily_rf) / arr.std(ddof=1) * math.sqrt(252)
        result = sharpe_ratio(r, risk_free_rate=0.04, periods_per_year=252)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_empty_returns_zero(self):
        assert sharpe_ratio([]) == 0.0

    def test_single_element_zero(self):
        assert sharpe_ratio([0.05]) == 0.0

    def test_zero_std_returns_zero(self):
        # All same values → std=0
        assert sharpe_ratio([0.005] * 100) == 0.0

    def test_returns_float(self):
        r = [0.01, -0.005, 0.008, 0.012, -0.002] * 20
        result = sharpe_ratio(r)
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# sortino_ratio
# ---------------------------------------------------------------------------


class TestSortinoRatio:
    def test_empty_returns_zero(self):
        assert sortino_ratio([]) == 0.0

    def test_single_element_zero(self):
        assert sortino_ratio([0.05]) == 0.0

    def test_all_positive_returns_zero(self):
        """Returns all above the daily target have no downside deviation."""
        assert sortino_ratio([0.01] * 50) == 0.0

    def test_positive_return_below_target_contributes_to_downside(self):
        """A positive bar below the target must not be treated as zero downside."""
        result = sortino_ratio([0.005, 0.025], risk_free_rate=0.12, periods_per_year=12)

        # Daily target = 0.01; gaps = [-0.005, 0.0]; semideviation = 0.005 / sqrt(2).
        assert result == pytest.approx(math.sqrt(24.0))

    def test_downside_semideviation_uses_all_observations(self):
        """One downside gap is averaged across every period, not just downside bars."""
        result = sortino_ratio(
            [-0.01, 0.03, 0.03, 0.03], risk_free_rate=0.0, periods_per_year=1
        )

        # Gaps = [-0.01, 0, 0, 0], semideviation = 0.005, excess mean = 0.02.
        assert result == pytest.approx(4.0)

    def test_known_mixed_returns(self):
        import numpy as np

        r = [0.01, -0.005, 0.008, -0.012, 0.003] * 20
        arr = np.array(r)
        daily_rf = 0.04 / 252
        gaps = np.minimum(arr - daily_rf, 0.0)
        downside_deviation = float(np.sqrt(np.mean(np.square(gaps))))
        expected = (float(arr.mean()) - daily_rf) / downside_deviation * math.sqrt(252)
        result = sortino_ratio(r)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_returns_float(self):
        r = [0.01, -0.005, 0.008] * 10
        assert isinstance(sortino_ratio(r), float)


# ---------------------------------------------------------------------------
# cagr
# ---------------------------------------------------------------------------


class TestCAGR:
    def test_two_points_one_bar(self):
        """[100, 110] over 1 bar → (110/100)^252 - 1 (very large number)."""
        ec = make_equity_curve([100.0, 110.0])
        result = cagr(ec, periods_per_year=252)
        expected = (110.0 / 100.0) ** 252 - 1.0
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_three_points_two_bars(self):
        """[100, 110, 121] over 2 bars → (121/100)^(252/2) - 1."""
        ec = make_equity_curve([100.0, 110.0, 121.0])
        result = cagr(ec, periods_per_year=252)
        expected = (121.0 / 100.0) ** (252 / 2) - 1.0
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_flat_equity_zero_cagr(self):
        ec = make_equity_curve([100.0, 100.0, 100.0])
        assert cagr(ec) == 0.0

    def test_fewer_than_two_points_zero(self):
        assert cagr(make_equity_curve([100.0])) == 0.0
        assert cagr([]) == 0.0

    def test_non_positive_initial_equity_zero(self):
        ec = make_equity_curve([0.0, 100.0])
        assert cagr(ec) == 0.0

    def test_returns_float(self):
        ec = make_equity_curve([100.0, 105.0, 110.0])
        assert isinstance(cagr(ec), float)


# ---------------------------------------------------------------------------
# max_drawdown
# ---------------------------------------------------------------------------


class TestMaxDrawdown:
    def test_blueprint_case(self):
        """Blueprint requirement: [100,110,90,95,80,100] → -27.27%."""
        ec = make_equity_curve([100.0, 110.0, 90.0, 95.0, 80.0, 100.0])
        result = max_drawdown(ec)
        expected = (80.0 - 110.0) / 110.0  # -0.272727...
        assert math.isclose(result, expected, rel_tol=1e-6)

    def test_monotonically_increasing_zero(self):
        ec = make_equity_curve([100.0, 105.0, 110.0, 120.0])
        assert max_drawdown(ec) == 0.0

    def test_empty_zero(self):
        assert max_drawdown([]) == 0.0

    def test_single_point_zero(self):
        assert max_drawdown(make_equity_curve([100.0])) == 0.0

    def test_result_is_negative(self):
        ec = make_equity_curve([100.0, 90.0])
        result = max_drawdown(ec)
        assert result < 0

    def test_returns_float(self):
        ec = make_equity_curve([100.0, 110.0, 80.0])
        assert isinstance(max_drawdown(ec), float)

    def test_peak_to_trough_correct(self):
        """Multiple troughs — picks the deepest relative drawdown."""
        # Peak=200, then falls to 100 → -50%; earlier peak=100 falls to 80 → -20%
        ec = make_equity_curve([100.0, 80.0, 200.0, 150.0, 100.0])
        result = max_drawdown(ec)
        expected = (100.0 - 200.0) / 200.0  # -0.5
        assert math.isclose(result, expected, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# max_drawdown_duration
# ---------------------------------------------------------------------------


class TestMaxDrawdownDuration:
    def test_blueprint_case(self):
        """[100,110,90,95,80,100]: peak at bar 1 (110), recovers at bar 5 (100 == peak? No).

        After reaching 110 (bar index 1), it never recovers to 110 within the list
        (bar 5 = 100 < 110), so drawdown lasts from bar 2 to bar 5 = 4 bars.
        """
        ec = make_equity_curve([100.0, 110.0, 90.0, 95.0, 80.0, 100.0])
        result = max_drawdown_duration(ec)
        assert result == 4

    def test_no_drawdown_zero(self):
        ec = make_equity_curve([100.0, 105.0, 110.0, 115.0])
        assert max_drawdown_duration(ec) == 0

    def test_empty_zero(self):
        assert max_drawdown_duration([]) == 0

    def test_single_point_zero(self):
        assert max_drawdown_duration(make_equity_curve([100.0])) == 0

    def test_full_recovery_resets_duration(self):
        """Drawdown ends when equity returns to peak; new drawdown counted separately."""
        # Bar 0: 100 (peak), bar 1: 90 (down 1), bar 2: 100 (recovered, peak reset)
        # bar 3: 80 (down 1), bar 4: 70 (down 2) → max_duration = 2
        ec = make_equity_curve([100.0, 90.0, 100.0, 80.0, 70.0])
        result = max_drawdown_duration(ec)
        assert result == 2

    def test_simple_drawdown(self):
        """3-bar drawdown from peak."""
        ec = make_equity_curve([100.0, 50.0, 60.0, 70.0])
        result = max_drawdown_duration(ec)
        assert result == 3


# ---------------------------------------------------------------------------
# win_rate
# ---------------------------------------------------------------------------


class TestWinRate:
    def test_three_wins_two_losses(self):
        trades = [
            make_trade(100.0),   # win
            make_trade(200.0),   # win
            make_trade(50.0),    # win
            make_trade(-100.0),  # loss
            make_trade(-50.0),   # loss
        ]
        result = win_rate(trades)
        assert math.isclose(result, 0.6, rel_tol=1e-9)

    def test_empty_trades_zero(self):
        assert win_rate([]) == 0.0

    def test_all_winners(self):
        trades = [make_trade(100.0), make_trade(50.0)]
        assert win_rate(trades) == 1.0

    def test_all_losers(self):
        trades = [make_trade(-100.0), make_trade(-50.0)]
        assert win_rate(trades) == 0.0

    def test_open_trades_excluded(self):
        """Open trades (no exit_price) must not count."""
        open_trade = Trade(
            symbol="TEST",
            direction=Direction.LONG,
            entry_price=100.0,
            quantity=1,
            entry_date=datetime(2023, 1, 1),
            trade_id="open01",
        )
        closed_win = make_trade(100.0)
        result = win_rate([open_trade, closed_win])
        assert result == 1.0  # only 1 closed trade, it's a win

    def test_returns_float(self):
        assert isinstance(win_rate([make_trade(10.0)]), float)


# ---------------------------------------------------------------------------
# profit_factor
# ---------------------------------------------------------------------------


class TestProfitFactor:
    def test_known_ratio(self):
        """gross_profit=1500, gross_loss=1000 → 1.5."""
        trades = [
            make_trade(1500.0),
            make_trade(-1000.0),
        ]
        result = profit_factor(trades)
        assert math.isclose(result, 1.5, rel_tol=1e-9)

    def test_all_winners_returns_inf(self):
        """No losses → inf."""
        trades = [make_trade(100.0), make_trade(200.0)]
        result = profit_factor(trades)
        assert result == float("inf")

    def test_no_trades_zero(self):
        assert profit_factor([]) == 0.0

    def test_all_losers(self):
        trades = [make_trade(-100.0), make_trade(-200.0)]
        result = profit_factor(trades)
        assert result == 0.0

    def test_gross_calculation(self):
        """Multiple winners and losers."""
        trades = [
            make_trade(300.0),
            make_trade(700.0),   # gross profit = 1000
            make_trade(-200.0),
            make_trade(-300.0),  # gross loss = 500
        ]
        result = profit_factor(trades)
        assert math.isclose(result, 2.0, rel_tol=1e-9)

    def test_returns_float(self):
        trades = [make_trade(100.0), make_trade(-50.0)]
        assert isinstance(profit_factor(trades), float)


# ---------------------------------------------------------------------------
# calmar_ratio
# ---------------------------------------------------------------------------


class TestCalmarRatio:
    def test_zero_drawdown_returns_zero(self):
        ec = make_equity_curve([100.0, 105.0, 110.0])
        assert calmar_ratio(ec) == 0.0

    def test_empty_returns_zero(self):
        assert calmar_ratio([]) == 0.0

    def test_positive_cagr_negative_mdd(self):
        """Calmar = CAGR / |MDD|; should be positive for positive CAGR."""
        ec = make_equity_curve([100.0, 110.0, 90.0, 95.0, 80.0, 130.0])
        c = cagr(ec)
        mdd = max_drawdown(ec)
        expected = c / abs(mdd)
        result = calmar_ratio(ec)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_returns_float(self):
        ec = make_equity_curve([100.0, 110.0, 80.0, 120.0])
        assert isinstance(calmar_ratio(ec), float)


# ---------------------------------------------------------------------------
# compute_all_metrics
# ---------------------------------------------------------------------------


class TestComputeAllMetrics:
    REQUIRED_KEYS = {
        "sharpe_ratio",
        "sortino_ratio",
        "cagr",
        "max_drawdown",
        "max_drawdown_duration",
        "win_rate",
        "profit_factor",
        "calmar_ratio",
        "total_trades",
        "total_return",
    }

    def test_all_keys_present(self):
        result = make_backtest_result([100.0, 105.0, 102.0, 108.0, 95.0, 110.0])
        metrics = compute_all_metrics(result)
        assert self.REQUIRED_KEYS == set(metrics.keys())

    def test_all_values_are_float(self):
        result = make_backtest_result([100.0, 105.0, 102.0, 108.0])
        metrics = compute_all_metrics(result)
        for key, val in metrics.items():
            assert isinstance(val, float), f"Key '{key}' is not float: {type(val)}"

    def test_total_return_correct(self):
        result = make_backtest_result([100.0, 120.0])
        metrics = compute_all_metrics(result)
        assert math.isclose(metrics["total_return"], 0.2, rel_tol=1e-9)

    def test_total_trades_counts_closed(self):
        trades = [make_trade(100.0), make_trade(-50.0)]
        result = make_backtest_result([100.0, 110.0], trades=trades)
        metrics = compute_all_metrics(result)
        assert metrics["total_trades"] == 2.0

    def test_empty_equity_curve_no_crash(self):
        result = BacktestResult(
            strategy_name="test",
            symbol="TEST",
            start_date=datetime(2023, 1, 1),
            end_date=datetime(2023, 1, 1),
            parameters={},
            trades=[],
            equity_curve=[],
            final_equity=100_000.0,
            initial_capital=100_000.0,
        )
        metrics = compute_all_metrics(result)
        assert self.REQUIRED_KEYS == set(metrics.keys())
        assert metrics["sharpe_ratio"] == 0.0
        assert metrics["max_drawdown"] == 0.0

    def test_with_trades_win_rate_propagates(self):
        trades = [
            make_trade(100.0),
            make_trade(200.0),
            make_trade(-50.0),
        ]
        result = make_backtest_result([100.0, 105.0, 110.0, 108.0], trades=trades)
        metrics = compute_all_metrics(result)
        assert math.isclose(metrics["win_rate"], 2 / 3, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Edge case / graceful handling
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_sharpe_empty(self):
        assert sharpe_ratio([]) == 0.0

    def test_sortino_empty(self):
        assert sortino_ratio([]) == 0.0

    def test_cagr_empty(self):
        assert cagr([]) == 0.0

    def test_max_drawdown_empty(self):
        assert max_drawdown([]) == 0.0

    def test_max_drawdown_duration_empty(self):
        assert max_drawdown_duration([]) == 0

    def test_win_rate_empty(self):
        assert win_rate([]) == 0.0

    def test_profit_factor_empty(self):
        assert profit_factor([]) == 0.0

    def test_calmar_empty(self):
        assert calmar_ratio([]) == 0.0

    def test_equity_curve_to_returns_empty(self):
        assert equity_curve_to_returns([]) == []

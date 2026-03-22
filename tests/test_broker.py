"""Tests for position sizers (FixedQuantitySizer, FixedFractionSizer, KellyCriterionSizer).

SimulatedBroker tests already exist in test_engine.py and are not duplicated here.
"""
from __future__ import annotations

import math
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.engine.event import SignalEvent
from src.engine.position_sizer import (
    FixedFractionSizer,
    FixedQuantitySizer,
    KellyCriterionSizer,
)
from src.models.order import Direction
from src.models.portfolio import Portfolio
from src.models.trade import Trade

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_portfolio(equity: float = 100_000.0, trades: list[Trade] | None = None) -> MagicMock:
    """Return a mock Portfolio with configurable equity and trades list."""
    mock = MagicMock(spec=Portfolio)
    mock.equity = equity
    mock.cash = equity
    mock.trades = trades if trades is not None else []
    return mock


def _make_signal(direction: Direction = Direction.LONG) -> SignalEvent:
    return SignalEvent(symbol="TEST", direction=direction)


def _make_closed_trade(pnl_value: float, entry_price: float = 100.0) -> Trade:
    """Create a closed Trade with a deterministic PnL."""
    # quantity and prices chosen so that pnl (no commission) == pnl_value
    # pnl = (exit - entry) * qty  ->  exit = entry + pnl_value (for qty=1)
    exit_price = entry_price + pnl_value
    return Trade(
        symbol="TEST",
        direction=Direction.LONG,
        entry_price=entry_price,
        quantity=1,
        entry_date=datetime(2024, 1, 1),
        exit_price=exit_price,
        exit_date=datetime(2024, 1, 2),
        commission=0.0,
    )


# ---------------------------------------------------------------------------
# FixedQuantitySizer
# ---------------------------------------------------------------------------


class TestFixedQuantitySizer:
    def test_default_quantity(self) -> None:
        sizer = FixedQuantitySizer()
        assert sizer.calculate(_make_signal(), _make_portfolio(), 100.0) == 100

    def test_custom_quantity_is_returned_exactly(self) -> None:
        sizer = FixedQuantitySizer(quantity=250)
        assert sizer.calculate(_make_signal(), _make_portfolio(), 100.0) == 250

    def test_quantity_independent_of_price(self) -> None:
        sizer = FixedQuantitySizer(quantity=50)
        result_low = sizer.calculate(_make_signal(), _make_portfolio(), 1.0)
        result_high = sizer.calculate(_make_signal(), _make_portfolio(), 100_000.0)
        assert result_low == 50
        assert result_high == 50

    def test_quantity_independent_of_portfolio_equity(self) -> None:
        sizer = FixedQuantitySizer(quantity=10)
        small = sizer.calculate(_make_signal(), _make_portfolio(equity=1.0), 50.0)
        large = sizer.calculate(_make_signal(), _make_portfolio(equity=10_000_000.0), 50.0)
        assert small == 10
        assert large == 10

    def test_invalid_quantity_raises(self) -> None:
        with pytest.raises(ValueError):
            FixedQuantitySizer(quantity=0)


# ---------------------------------------------------------------------------
# FixedFractionSizer
# ---------------------------------------------------------------------------


class TestFixedFractionSizer:
    def test_known_values(self) -> None:
        """equity=100000, fraction=0.02, price=100.0 -> floor(2000/100) = 20"""
        sizer = FixedFractionSizer(fraction=0.02)
        result = sizer.calculate(_make_signal(), _make_portfolio(equity=100_000.0), 100.0)
        assert result == 20

    def test_floor_behaviour(self) -> None:
        """equity=100000, fraction=0.02, price=150.0 -> floor(2000/150) = floor(13.33) = 13"""
        sizer = FixedFractionSizer(fraction=0.02)
        result = sizer.calculate(_make_signal(), _make_portfolio(equity=100_000.0), 150.0)
        assert result == math.floor((100_000.0 * 0.02) / 150.0)

    def test_minimum_one_share_when_equity_very_small(self) -> None:
        """Even with tiny equity the result must be at least 1."""
        sizer = FixedFractionSizer(fraction=0.02)
        # equity=10, fraction=0.02, price=100 -> floor(0.2/100)=0 -> clamp to 1
        result = sizer.calculate(_make_signal(), _make_portfolio(equity=10.0), 100.0)
        assert result == 1

    def test_result_formula(self) -> None:
        """Generic formula check: floor((equity * fraction) / price)."""
        equity, fraction, price = 75_000.0, 0.05, 37.5
        sizer = FixedFractionSizer(fraction=fraction)
        expected = max(1, math.floor((equity * fraction) / price))
        result = sizer.calculate(_make_signal(), _make_portfolio(equity=equity), price)
        assert result == expected

    def test_scales_with_fraction(self) -> None:
        portfolio = _make_portfolio(equity=100_000.0)
        price = 100.0
        sizer_small = FixedFractionSizer(fraction=0.01)
        sizer_large = FixedFractionSizer(fraction=0.10)
        assert sizer_small.calculate(_make_signal(), portfolio, price) < sizer_large.calculate(
            _make_signal(), portfolio, price
        )

    def test_invalid_fraction_raises(self) -> None:
        with pytest.raises(ValueError):
            FixedFractionSizer(fraction=0.0)
        with pytest.raises(ValueError):
            FixedFractionSizer(fraction=1.5)


# ---------------------------------------------------------------------------
# KellyCriterionSizer
# ---------------------------------------------------------------------------


class TestKellyCriterionSizer:
    # --- fallback behaviour -------------------------------------------------

    def test_fallback_when_no_trades(self) -> None:
        """No closed trades -> fallback to FixedFractionSizer(0.02)."""
        sizer = KellyCriterionSizer(lookback=20)
        portfolio = _make_portfolio(equity=100_000.0, trades=[])
        result = sizer.calculate(_make_signal(), portfolio, 100.0)
        expected_fallback = FixedFractionSizer(0.02).calculate(_make_signal(), portfolio, 100.0)
        assert result == expected_fallback

    def test_fallback_when_fewer_than_lookback_trades(self) -> None:
        """5 closed trades with lookback=20 -> fallback."""
        trades = [_make_closed_trade(pnl_value=10.0) for _ in range(5)]
        sizer = KellyCriterionSizer(lookback=20)
        portfolio = _make_portfolio(equity=100_000.0, trades=trades)
        result = sizer.calculate(_make_signal(), portfolio, 100.0)
        expected_fallback = FixedFractionSizer(0.02).calculate(_make_signal(), portfolio, 100.0)
        assert result == expected_fallback

    def test_fallback_result_is_at_least_one(self) -> None:
        """Fallback path still enforces minimum 1 share."""
        sizer = KellyCriterionSizer(lookback=20)
        portfolio = _make_portfolio(equity=1.0, trades=[])
        result = sizer.calculate(_make_signal(), portfolio, 100.0)
        assert result >= 1

    # --- sufficient trades: winning streak vs losing streak -----------------

    def test_winning_trades_produce_larger_size_than_losing_trades(self) -> None:
        """With mostly winning trades kelly fraction is larger -> more shares."""
        win_trades = [_make_closed_trade(pnl_value=50.0) for _ in range(16)] + [
            _make_closed_trade(pnl_value=-10.0) for _ in range(4)
        ]
        lose_trades = [_make_closed_trade(pnl_value=-50.0) for _ in range(16)] + [
            _make_closed_trade(pnl_value=10.0) for _ in range(4)
        ]

        sizer = KellyCriterionSizer(lookback=20)
        equity = 100_000.0
        price = 100.0

        win_portfolio = _make_portfolio(equity=equity, trades=win_trades)
        lose_portfolio = _make_portfolio(equity=equity, trades=lose_trades)

        win_result = sizer.calculate(_make_signal(), win_portfolio, price)
        lose_result = sizer.calculate(_make_signal(), lose_portfolio, price)

        assert win_result > lose_result

    # --- safety: never exceed portfolio equity ------------------------------

    def test_result_never_exceeds_equity(self) -> None:
        """shares * price should never exceed portfolio.equity."""
        trades = [_make_closed_trade(pnl_value=200.0) for _ in range(20)]
        sizer = KellyCriterionSizer(lookback=20, max_pct=0.25)
        equity = 100_000.0
        price = 10.0
        portfolio = _make_portfolio(equity=equity, trades=trades)
        result = sizer.calculate(_make_signal(), portfolio, price)
        assert result * price <= equity

    def test_max_pct_cap_respected(self) -> None:
        """Even with a very large kelly fraction, max_pct is never exceeded."""
        # All winning trades -> kelly fraction would be large
        trades = [_make_closed_trade(pnl_value=500.0) for _ in range(19)] + [
            _make_closed_trade(pnl_value=-1.0)
        ]
        equity = 100_000.0
        price = 100.0
        max_pct = 0.10
        sizer = KellyCriterionSizer(lookback=20, fraction=0.5, max_pct=max_pct)
        portfolio = _make_portfolio(equity=equity, trades=trades)
        result = sizer.calculate(_make_signal(), portfolio, price)
        max_allowed_shares = math.floor((equity * max_pct) / price)
        assert result <= max_allowed_shares

    # --- minimum 1 share ----------------------------------------------------

    def test_result_always_at_least_one(self) -> None:
        """Even with a negative or zero kelly fraction, return >= 1."""
        # All losing trades -> kelly fraction will be negative -> clamp to 1
        trades = [_make_closed_trade(pnl_value=-10.0) for _ in range(18)] + [
            _make_closed_trade(pnl_value=1.0) for _ in range(2)
        ]
        sizer = KellyCriterionSizer(lookback=20)
        portfolio = _make_portfolio(equity=100_000.0, trades=trades)
        result = sizer.calculate(_make_signal(), portfolio, 100.0)
        assert result >= 1

    def test_result_at_least_one_with_tiny_equity(self) -> None:
        """Minimum 1 share is enforced even when equity is very small."""
        trades = [_make_closed_trade(pnl_value=10.0) for _ in range(16)] + [
            _make_closed_trade(pnl_value=-5.0) for _ in range(4)
        ]
        sizer = KellyCriterionSizer(lookback=20)
        portfolio = _make_portfolio(equity=0.50, trades=trades)
        result = sizer.calculate(_make_signal(), portfolio, 100.0)
        assert result >= 1

    # --- uses only last `lookback` trades -----------------------------------

    def test_uses_only_last_lookback_trades(self) -> None:
        """Old trades outside the lookback window must not affect sizing."""
        old_losing = [_make_closed_trade(pnl_value=-50.0) for _ in range(50)]
        recent_winning = [_make_closed_trade(pnl_value=50.0) for _ in range(16)] + [
            _make_closed_trade(pnl_value=-5.0) for _ in range(4)
        ]
        all_trades = old_losing + recent_winning

        sizer_with_history = KellyCriterionSizer(lookback=20)
        sizer_only_recent = KellyCriterionSizer(lookback=20)

        portfolio_all = _make_portfolio(equity=100_000.0, trades=all_trades)
        portfolio_recent = _make_portfolio(equity=100_000.0, trades=recent_winning)

        result_all = sizer_with_history.calculate(_make_signal(), portfolio_all, 100.0)
        result_recent = sizer_only_recent.calculate(_make_signal(), portfolio_recent, 100.0)

        # Both should produce the same result because the old trades are outside lookback
        assert result_all == result_recent

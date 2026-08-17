"""Tests for domain models — Step 1 exit criteria."""
from datetime import datetime

import pytest

from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType
from src.models.portfolio import FillOutcome, FillRejectionReason, Portfolio
from src.models.trade import Trade
from tests.conftest import make_candle

# ── Candle ──────────────────────────────────────────────────────────────────


class TestCandle:
    def test_valid_candle(self) -> None:
        c = make_candle()
        assert c.close == 100.0

    def test_frozen(self) -> None:
        c = make_candle()
        with pytest.raises((AttributeError, TypeError)):
            c.close = 999.0  # type: ignore[misc]

    def test_ohlc_invariant_open(self) -> None:
        with pytest.raises(ValueError, match="OHLC invariant"):
            Candle(
                timestamp=datetime(2023, 1, 2),
                open=50.0,  # below low
                high=101.0,
                low=98.0,
                close=100.0,
                volume=1_000_000,
                adj_close=100.0,
            )

    def test_ohlc_invariant_close(self) -> None:
        with pytest.raises(ValueError, match="OHLC invariant"):
            Candle(
                timestamp=datetime(2023, 1, 2),
                open=99.0,
                high=101.0,
                low=98.0,
                close=110.0,  # above high
                volume=1_000_000,
                adj_close=110.0,
            )

    def test_negative_volume(self) -> None:
        with pytest.raises(ValueError, match="Volume"):
            Candle(
                timestamp=datetime(2023, 1, 2),
                open=99.0,
                high=101.0,
                low=98.0,
                close=100.0,
                volume=-1,
                adj_close=100.0,
            )

    def test_typical_price(self) -> None:
        c = Candle(
            timestamp=datetime(2023, 1, 2),
            open=99.0,
            high=110.0,
            low=90.0,
            close=100.0,
            volume=1_000_000,
            adj_close=100.0,
        )
        assert c.typical_price == pytest.approx((110 + 90 + 100) / 3)


# ── Order ────────────────────────────────────────────────────────────────────


class TestOrder:
    def test_market_order(self) -> None:
        o = Order(symbol="AAPL", direction=Direction.LONG, quantity=100)
        assert o.order_type == OrderType.MARKET

    def test_limit_order_requires_price(self) -> None:
        with pytest.raises(ValueError, match="limit_price"):
            Order(symbol="AAPL", direction=Direction.LONG, quantity=100, order_type=OrderType.LIMIT)

    def test_limit_order_valid(self) -> None:
        o = Order(
            symbol="AAPL",
            direction=Direction.LONG,
            quantity=100,
            order_type=OrderType.LIMIT,
            limit_price=150.0,
        )
        assert o.limit_price == 150.0

    def test_stop_order_requires_price(self) -> None:
        with pytest.raises(ValueError, match="stop_price"):
            Order(symbol="AAPL", direction=Direction.LONG, quantity=100, order_type=OrderType.STOP)

    def test_zero_quantity(self) -> None:
        with pytest.raises(ValueError, match="Quantity"):
            Order(symbol="AAPL", direction=Direction.LONG, quantity=0)

    def test_direction_opposite(self) -> None:
        assert Direction.LONG.opposite() == Direction.SHORT
        assert Direction.SHORT.opposite() == Direction.LONG


# ── Trade ────────────────────────────────────────────────────────────────────


class TestTrade:
    def test_open_trade_pnl_zero(self) -> None:
        t = Trade(
            symbol="SPY",
            direction=Direction.LONG,
            entry_price=100.0,
            quantity=100,
            entry_date=datetime(2023, 1, 2),
        )
        assert not t.is_closed
        assert t.pnl == 0.0

    def test_long_pnl(self) -> None:
        t = Trade(
            symbol="SPY",
            direction=Direction.LONG,
            entry_price=100.0,
            quantity=100,
            entry_date=datetime(2023, 1, 2),
            commission=10.0,
            exit_price=110.0,
            exit_date=datetime(2023, 2, 1),
        )
        assert t.is_closed
        assert t.pnl == pytest.approx(100 * (110 - 100) - 10)

    def test_short_pnl(self) -> None:
        t = Trade(
            symbol="SPY",
            direction=Direction.SHORT,
            entry_price=100.0,
            quantity=100,
            entry_date=datetime(2023, 1, 2),
            commission=10.0,
            exit_price=90.0,
            exit_date=datetime(2023, 2, 1),
        )
        assert t.pnl == pytest.approx(100 * (100 - 90) - 10)

    def test_pnl_pct(self) -> None:
        t = Trade(
            symbol="SPY",
            direction=Direction.LONG,
            entry_price=100.0,
            quantity=100,
            entry_date=datetime(2023, 1, 2),
            exit_price=110.0,
            exit_date=datetime(2023, 2, 1),
        )
        # pnl=1000, cost=10000 → 10%
        assert t.pnl_pct == pytest.approx(0.10)

    def test_duration_days(self) -> None:
        t = Trade(
            symbol="SPY",
            direction=Direction.LONG,
            entry_price=100.0,
            quantity=100,
            entry_date=datetime(2023, 1, 2),
            exit_price=105.0,
            exit_date=datetime(2023, 1, 12),
        )
        assert t.duration_days == pytest.approx(10.0)


# ── Portfolio ────────────────────────────────────────────────────────────────


class TestPortfolio:
    def test_initial_state(self, portfolio: Portfolio) -> None:
        assert portfolio.cash == 100_000.0
        assert portfolio.equity == 100_000.0
        assert len(portfolio.trades) == 0

    def test_invalid_capital(self) -> None:
        with pytest.raises(ValueError):
            Portfolio(initial_capital=0)

    def test_open_long_position(self, portfolio: Portfolio) -> None:
        dt = datetime(2023, 1, 2)
        portfolio.record_fill("SPY", Direction.LONG, 100, 100.0, dt, commission=10.0)
        assert portfolio.cash == pytest.approx(100_000 - 100 * 100 - 10)
        assert portfolio.has_position("SPY")

    def test_close_long_position_profit(self, portfolio: Portfolio) -> None:
        dt1 = datetime(2023, 1, 2)
        dt2 = datetime(2023, 2, 1)
        portfolio.record_fill("SPY", Direction.LONG, 100, 100.0, dt1, commission=10.0)
        outcome = portfolio.record_fill("SPY", Direction.SHORT, 100, 110.0, dt2, commission=10.0)
        assert outcome.accepted is True
        assert outcome.trade is not None
        assert outcome.trade.is_closed
        assert outcome.trade.pnl == pytest.approx(100 * 10 - 20)  # 1000 - 20 comm
        assert not portfolio.has_position("SPY")

    def test_equity_curve_recorded(self, portfolio: Portfolio) -> None:
        dt = datetime(2023, 1, 2)
        portfolio.update({"SPY": 100.0}, dt)
        assert len(portfolio.equity_curve) == 1
        assert portfolio.equity_curve[0].equity == pytest.approx(100_000.0)

    def test_drawdown_calculation(self, portfolio: Portfolio) -> None:
        # Equity goes 100k → 80k → drawdown = -20%
        portfolio.update({"X": 100.0}, datetime(2023, 1, 2))
        # Force equity down by manually setting cash
        portfolio._cash = 80_000.0
        portfolio.update({}, datetime(2023, 1, 3))
        assert portfolio.equity_curve[-1].drawdown_pct == pytest.approx(-0.20)

    def test_reset(self, portfolio: Portfolio) -> None:
        portfolio.record_fill("SPY", Direction.LONG, 100, 100.0, datetime(2023, 1, 2))
        portfolio.reset()
        assert portfolio.cash == 100_000.0
        assert len(portfolio.trades) == 0
        assert not portfolio.has_position("SPY")

    def test_mark_to_market(self, portfolio: Portfolio) -> None:
        dt = datetime(2023, 1, 2)
        portfolio.record_fill("SPY", Direction.LONG, 100, 100.0, dt)
        # Price moves to 110 — equity should reflect gain
        portfolio.update({"SPY": 110.0}, dt)
        assert portfolio.equity == pytest.approx(portfolio.cash + 100 * 110)

    def test_open_long_returns_accepted_outcome(self) -> None:
        portfolio = Portfolio(1_000.0)
        outcome = portfolio.record_fill("X", Direction.LONG, 1, 100.0, datetime(2023, 1, 2))
        assert outcome == FillOutcome(True, None, None, 900.0, 0.0)

    def test_insufficient_cash_returns_rejection_without_mutation(self) -> None:
        portfolio = Portfolio(1_000.0)
        outcome = portfolio.record_fill("X", Direction.LONG, 100, 100.0, datetime(2023, 1, 2))
        assert outcome.accepted is False
        assert outcome.rejection_reason is FillRejectionReason.INSUFFICIENT_BUYING_POWER
        assert portfolio.cash == 1_000.0
        assert portfolio.open_positions == {}

    def test_same_direction_fill_against_open_position_is_invalid(self) -> None:
        portfolio = Portfolio(1_000.0)
        portfolio.record_fill("X", Direction.LONG, 1, 100.0, datetime(2023, 1, 2))
        with pytest.raises(ValueError, match="opposite"):
            portfolio.record_fill("X", Direction.LONG, 1, 101.0, datetime(2023, 1, 3))

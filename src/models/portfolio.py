"""Portfolio — cash + open positions, equity curve recorder."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

from src.models.order import Direction
from src.models.trade import Trade


@dataclass
class EquityPoint:
    date: datetime
    equity: float
    drawdown_pct: float = 0.0


class FillRejectionReason(StrEnum):
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"


@dataclass(frozen=True, slots=True)
class FillOutcome:
    accepted: bool
    trade: Trade | None
    rejection_reason: FillRejectionReason | None
    available_cash: float
    restricted_collateral: float


class Portfolio:
    """Stateful portfolio: tracks cash, open positions, and equity curve.

    Designed to be reset between backtests via reset().
    record_fill() opens or closes positions.
    update() marks-to-market at the close of each bar.
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        short_initial_margin: float = 1.50,
        short_maintenance_margin: float = 0.30,
        annual_short_borrow_rate: float = 0.03,
        borrow_day_count: float = 365.0,
    ) -> None:
        if not isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("Initial capital must be positive")
        self._validate_short_settings(
            short_initial_margin,
            short_maintenance_margin,
            annual_short_borrow_rate,
            borrow_day_count,
        )
        self.initial_capital = initial_capital
        self.short_initial_margin = short_initial_margin
        self.short_maintenance_margin = short_maintenance_margin
        self.annual_short_borrow_rate = annual_short_borrow_rate
        self.borrow_day_count = borrow_day_count
        self._cash: float = initial_capital
        self._restricted_collateral: float = 0.0
        self._short_collateral: dict[str, float] = {}
        self._last_short_borrow_timestamp: dict[str, datetime] = {}
        # symbol -> (direction, quantity, entry_price, entry_date, trade_id, commission)
        self._open_positions: dict[str, tuple[Direction, int, float, datetime, str, float]] = {}
        self._trades: list[Trade] = []
        self._equity_curve: list[EquityPoint] = []
        self._peak_equity: float = initial_capital
        self._current_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Core state
    # ------------------------------------------------------------------

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def restricted_collateral(self) -> float:
        return self._restricted_collateral

    @property
    def equity(self) -> float:
        """Cash + mark-to-market value of open positions."""
        pos_value = 0.0
        for symbol, (direction, qty, entry_price, _, _, _) in self._open_positions.items():
            price = self._current_prices.get(symbol, entry_price)
            if direction == Direction.LONG:
                pos_value += qty * price
            else:
                pos_value -= qty * price
        return self._cash + self._restricted_collateral + pos_value

    @property
    def equity_curve(self) -> list[EquityPoint]:
        return list(self._equity_curve)

    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    @property
    def open_positions(self) -> dict[str, tuple[Direction, int, float, datetime, str, float]]:
        return dict(self._open_positions)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._open_positions

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def record_fill(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        fill_price: float,
        fill_date: datetime,
        commission: float = 0.0,
    ) -> FillOutcome:
        """Open or close a position and report whether the fill was accepted."""
        if symbol in self._open_positions:
            existing_dir, qty, entry_price, entry_date, trade_id, entry_comm = (
                self._open_positions[symbol]
            )
            if direction != existing_dir.opposite():
                raise ValueError("Closing fill direction must be opposite the open position")
            # Closing trade (direction is opposite of position)
            trade = Trade(
                symbol=symbol,
                direction=existing_dir,
                entry_price=entry_price,
                quantity=qty,
                entry_date=entry_date,
                commission=entry_comm + commission,
                exit_price=fill_price,
                exit_date=fill_date,
                trade_id=trade_id,
            )
            self._trades.append(trade)
            # Adjust cash
            if existing_dir == Direction.LONG:
                self._cash += qty * fill_price - commission
            else:
                released_collateral = self._short_collateral.pop(symbol)
                self._restricted_collateral -= released_collateral
                self._cash += released_collateral - qty * fill_price - commission
                del self._last_short_borrow_timestamp[symbol]
            del self._open_positions[symbol]
            return FillOutcome(True, trade, None, self._cash, self._restricted_collateral)
        else:
            # Opening new position
            trade_id = str(uuid.uuid4())[:8]
            if direction == Direction.LONG:
                cost = quantity * fill_price + commission
                if cost > self._cash:
                    return FillOutcome(
                        False,
                        None,
                        FillRejectionReason.INSUFFICIENT_BUYING_POWER,
                        self._cash,
                        self._restricted_collateral,
                    )
                self._cash -= cost
            else:
                notional = quantity * fill_price
                additional_margin = notional * (self.short_initial_margin - 1.0)
                required_cash = additional_margin + commission
                if required_cash > self._cash:
                    return FillOutcome(
                        False,
                        None,
                        FillRejectionReason.INSUFFICIENT_BUYING_POWER,
                        self._cash,
                        self._restricted_collateral,
                    )
                collateral = notional * self.short_initial_margin
                self._cash -= required_cash
                self._restricted_collateral += collateral
                self._short_collateral[symbol] = collateral
                self._last_short_borrow_timestamp[symbol] = fill_date
            self._open_positions[symbol] = (
                direction,
                quantity,
                fill_price,
                fill_date,
                trade_id,
                commission,
            )
            return FillOutcome(True, None, None, self._cash, self._restricted_collateral)

    def accrue_short_borrow(self, timestamp: datetime) -> float:
        """Deduct borrow fees for open shorts using their previously marked prices."""
        short_positions = [
            (symbol, position)
            for symbol, position in self._open_positions.items()
            if position[0] == Direction.SHORT
        ]
        if not short_positions:
            return 0.0

        for symbol, _ in short_positions:
            if timestamp <= self._last_short_borrow_timestamp[symbol]:
                raise ValueError("Short borrow accrual timestamp must be strictly later")

        charge = 0.0
        for symbol, (_, quantity, entry_price, _, _, _) in short_positions:
            elapsed_days = (
                timestamp - self._last_short_borrow_timestamp[symbol]
            ).total_seconds() / 86_400.0
            previous_marked_short_value = quantity * self._current_prices.get(symbol, entry_price)
            charge += (
                previous_marked_short_value
                * self.annual_short_borrow_rate
                * elapsed_days
                / self.borrow_day_count
            )

        self._cash -= charge
        for symbol, _ in short_positions:
            self._last_short_borrow_timestamp[symbol] = timestamp
        return charge

    def maintenance_ratio(self, symbol: str) -> float | None:
        """Return equity over marked short value, or None when *symbol* is not short."""
        position = self._open_positions.get(symbol)
        if position is None or position[0] != Direction.SHORT:
            return None
        _, quantity, entry_price, _, _, _ = position
        current_price = self._current_prices.get(symbol, entry_price)
        return self.equity / (quantity * current_price)

    # ------------------------------------------------------------------
    # Mark-to-market
    # ------------------------------------------------------------------

    def update(self, prices: dict[str, float], timestamp: datetime) -> None:
        """Update current prices and append equity curve point."""
        self._current_prices.update(prices)
        current_equity = self.equity
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
        drawdown = (
            (current_equity - self._peak_equity) / self._peak_equity
            if self._peak_equity > 0
            else 0.0
        )
        self._equity_curve.append(
            EquityPoint(date=timestamp, equity=current_equity, drawdown_pct=drawdown)
        )

    def reset(self) -> None:
        """Reset to initial state for a new backtest run."""
        self._cash = self.initial_capital
        self._restricted_collateral = 0.0
        self._short_collateral.clear()
        self._last_short_borrow_timestamp.clear()
        self._open_positions.clear()
        self._trades.clear()
        self._equity_curve.clear()
        self._peak_equity = self.initial_capital
        self._current_prices.clear()

    @staticmethod
    def _validate_short_settings(
        short_initial_margin: float,
        short_maintenance_margin: float,
        annual_short_borrow_rate: float,
        borrow_day_count: float,
    ) -> None:
        settings = (
            short_initial_margin,
            short_maintenance_margin,
            annual_short_borrow_rate,
            borrow_day_count,
        )
        if not all(isfinite(value) for value in settings):
            raise ValueError("Short margin settings must be finite")
        if short_initial_margin < 1.0:
            raise ValueError("Short initial margin must be at least 1.0")
        if short_maintenance_margin < 0.0:
            raise ValueError("Short maintenance margin must be nonnegative")
        if annual_short_borrow_rate < 0.0:
            raise ValueError("Annual short borrow rate must be nonnegative")
        if borrow_day_count <= 0.0:
            raise ValueError("Borrow day count must be positive")

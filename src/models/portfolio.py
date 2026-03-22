"""Portfolio — cash + open positions, equity curve recorder."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.models.order import Direction
from src.models.trade import Trade


@dataclass
class EquityPoint:
    date: datetime
    equity: float
    drawdown_pct: float = 0.0


class Portfolio:
    """Stateful portfolio: tracks cash, open positions, and equity curve.

    Designed to be reset between backtests via reset().
    record_fill() opens or closes positions.
    update() marks-to-market at the close of each bar.
    """

    def __init__(self, initial_capital: float = 100_000.0) -> None:
        if initial_capital <= 0:
            raise ValueError("Initial capital must be positive")
        self.initial_capital = initial_capital
        self._cash: float = initial_capital
        # symbol -> (direction, quantity, entry_price, entry_date, trade_id, commission)
        self._open_positions: Dict[str, Tuple[Direction, int, float, datetime, str, float]] = {}
        self._trades: List[Trade] = []
        self._equity_curve: List[EquityPoint] = []
        self._peak_equity: float = initial_capital
        self._current_prices: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Core state
    # ------------------------------------------------------------------

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def equity(self) -> float:
        """Cash + mark-to-market value of open positions."""
        pos_value = 0.0
        for symbol, (direction, qty, entry_price, _, _, _) in self._open_positions.items():
            price = self._current_prices.get(symbol, entry_price)
            if direction == Direction.LONG:
                pos_value += qty * price
            else:
                # Short: profit when price falls
                pos_value += qty * (2 * entry_price - price)
        return self._cash + pos_value

    @property
    def equity_curve(self) -> List[EquityPoint]:
        return list(self._equity_curve)

    @property
    def trades(self) -> List[Trade]:
        return list(self._trades)

    @property
    def open_positions(self) -> Dict[str, Tuple[Direction, int, float, datetime, str, float]]:
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
    ) -> Optional[Trade]:
        """Open or close a position; returns closed Trade or None for open."""
        if symbol in self._open_positions:
            existing_dir, qty, entry_price, entry_date, trade_id, entry_comm = (
                self._open_positions[symbol]
            )
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
                self._cash += qty * (2 * entry_price - fill_price) - commission
            del self._open_positions[symbol]
            return trade
        else:
            # Opening new position
            trade_id = str(uuid.uuid4())[:8]
            if direction == Direction.LONG:
                cost = quantity * fill_price + commission
                if cost > self._cash:
                    return None  # Insufficient funds — reject fill
                self._cash -= cost
            else:
                # Short: receive proceeds
                self._cash += quantity * fill_price - commission
            self._open_positions[symbol] = (
                direction,
                quantity,
                fill_price,
                fill_date,
                trade_id,
                commission,
            )
            return None

    # ------------------------------------------------------------------
    # Mark-to-market
    # ------------------------------------------------------------------

    def update(self, prices: Dict[str, float], timestamp: datetime) -> None:
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
        self._open_positions.clear()
        self._trades.clear()
        self._equity_curve.clear()
        self._peak_equity = self.initial_capital
        self._current_prices.clear()

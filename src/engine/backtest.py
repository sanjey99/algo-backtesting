"""BacktestEngine — bar-by-bar event-driven backtest runner."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from math import isfinite
from typing import Any, Protocol

from src.engine.broker import SimulatedBroker
from src.engine.context import StrategyContext
from src.engine.event import FillEvent, SignalEvent
from src.models.candle import Candle
from src.models.order import Order, OrderType
from src.models.portfolio import EquityPoint, FillOutcome, Portfolio
from src.models.trade import Trade

# ---------------------------------------------------------------------------
# Strategy protocol — any object with these attributes is accepted
# ---------------------------------------------------------------------------

class Strategy(Protocol):
    """Structural type that BacktestEngine accepts as a strategy."""

    name: str
    parameters: dict[str, Any]

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        ...


# ---------------------------------------------------------------------------
# Config / Result
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_pct: float = 0.001
    slippage_pct: float = 0.0005
    short_initial_margin: float = 1.50
    short_maintenance_margin: float = 0.30
    annual_short_borrow_rate: float = 0.03
    borrow_day_count: float = 365.0

    def __post_init__(self) -> None:
        margin_settings = (
            self.short_initial_margin,
            self.short_maintenance_margin,
            self.annual_short_borrow_rate,
            self.borrow_day_count,
        )
        if not all(isfinite(value) for value in margin_settings):
            raise ValueError("Short margin settings must be finite")
        if self.short_initial_margin < 1.0:
            raise ValueError("Short initial margin must be at least 1.0")
        if self.short_maintenance_margin < 0.0:
            raise ValueError("Short maintenance margin must be nonnegative")
        if self.annual_short_borrow_rate < 0.0:
            raise ValueError("Annual short borrow rate must be nonnegative")
        if self.borrow_day_count <= 0.0:
            raise ValueError("Borrow day count must be positive")


@dataclass
class BacktestResult:
    strategy_name: str
    symbol: str
    start_date: datetime
    end_date: datetime
    parameters: dict[str, Any]
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    final_equity: float
    initial_capital: float
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True, slots=True)
class _PendingOrder:
    """Run-local order awaiting execution on a later candle."""

    order: Order
    forced_cover: bool = False


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Bar-by-bar event-driven backtest engine.

    Wiring per bar
    --------------
    1. Execute an order queued by an earlier candle, retaining an untriggered
       conditional order.
    2. Mark the filled portfolio to the current close.
    3. Build immutable strategy context from filled and pending state.
    4. Queue a valid strategy order for execution on a later candle, replacing
       any stale strategy order.

    The engine is stateless between runs: a fresh Portfolio and
    SimulatedBroker are constructed from *config* on every call to run().
    """

    def run(
        self,
        strategy: Strategy,
        candles: list[Candle],
        config: BacktestConfig,
    ) -> BacktestResult:
        if not candles:
            raise ValueError("candles list must not be empty")
        if any(
            current.timestamp <= previous.timestamp
            for previous, current in pairwise(candles)
        ):
            raise ValueError("candles must have strictly increasing timestamps")

        portfolio = Portfolio(
            initial_capital=config.initial_capital,
            short_initial_margin=config.short_initial_margin,
            short_maintenance_margin=config.short_maintenance_margin,
            annual_short_borrow_rate=config.annual_short_borrow_rate,
            borrow_day_count=config.borrow_day_count,
        )
        broker = SimulatedBroker(
            slippage_pct=config.slippage_pct,
            commission_pct=config.commission_pct,
        )

        # The engine is single-symbol per run. Capture the symbol from the
        # first actionable order, defaulting to "UNKNOWN" if none is queued.
        symbol: str = "UNKNOWN"
        pending: _PendingOrder | None = None

        for candle_index, candle in enumerate(candles):
            if candle_index > 0:
                portfolio.accrue_short_borrow(candle.timestamp)
            if pending is not None:
                fill: FillEvent | None = broker.execute(pending.order, candle)
                if fill is None:
                    if pending.order.order_type not in (
                        OrderType.LIMIT,
                        OrderType.STOP,
                    ):
                        pending = None
                else:
                    fill_outcome: FillOutcome = portfolio.record_fill(
                        symbol=fill.symbol,
                        direction=fill.direction,
                        quantity=fill.quantity,
                        fill_price=fill.fill_price,
                        fill_date=fill.fill_date,
                        commission=fill.commission,
                    )
                    if not fill_outcome.accepted:
                        # A typed rejection terminates the attempted order.
                        pending = None
                    else:
                        symbol = fill.symbol
                        pending = None

            portfolio.update({symbol: candle.close}, candle.timestamp)

            context = self._build_context(portfolio, pending)
            signal: SignalEvent | None = strategy.on_candle(candle, context)

            if signal is not None:
                order: Order | None = self._build_order(
                    signal, portfolio, candle
                )
                if order is not None:
                    symbol = order.symbol
                    pending = _PendingOrder(order=order)

        return BacktestResult(
            strategy_name=strategy.name,
            symbol=symbol,
            start_date=candles[0].timestamp,
            end_date=candles[-1].timestamp,
            parameters=dict(strategy.parameters),
            trades=portfolio.trades,
            equity_curve=portfolio.equity_curve,
            final_equity=portfolio.equity,
            initial_capital=config.initial_capital,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_order(
        signal: SignalEvent,
        portfolio: Portfolio,
        candle: Candle,
    ) -> Order | None:
        """Translate a signal into an opening or exact-position closing order."""
        del candle
        execution_symbol = signal.symbol
        quantity = 1
        open_positions = portfolio.open_positions

        if open_positions:
            execution_symbol, position = next(iter(open_positions.items()))
            existing_direction, quantity, *_ = position
            if signal.direction is not existing_direction.opposite():
                return None

        return Order(
            symbol=execution_symbol,
            direction=signal.direction,
            quantity=quantity,
            order_type=signal.order_type,
            limit_price=signal.limit_price,
            stop_price=signal.stop_price,
            created_at=signal.timestamp,
        )

    @staticmethod
    def _build_context(
        portfolio: Portfolio,
        pending: _PendingOrder | None,
    ) -> StrategyContext:
        """Expose the sole filled position and pending strategy order."""
        open_positions = portfolio.open_positions
        if open_positions:
            position_direction, position_quantity, *_ = next(
                iter(open_positions.values())
            )
        else:
            position_direction = None
            position_quantity = 0

        return StrategyContext(
            position_direction=position_direction,
            position_quantity=position_quantity,
            pending_direction=pending.order.direction if pending is not None else None,
            pending_order_type=pending.order.order_type if pending is not None else None,
            forced_cover_pending=pending.forced_cover if pending is not None else False,
        )

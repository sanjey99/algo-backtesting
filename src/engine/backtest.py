"""BacktestEngine — bar-by-bar event-driven backtest runner."""
from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from src.engine.broker import SimulatedBroker
from src.engine.event import Event, FillEvent, MarketEvent, OrderEvent, SignalEvent
from src.engine.position_sizer import FixedQuantitySizer, PositionSizer
from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType
from src.models.portfolio import EquityPoint, Portfolio
from src.models.trade import Trade


# ---------------------------------------------------------------------------
# Strategy protocol — any object with these attributes is accepted
# ---------------------------------------------------------------------------

class Strategy(Protocol):
    """Structural type that BacktestEngine accepts as a strategy."""

    name: str
    parameters: dict[str, Any]
    symbol: str

    def on_candle(self, candle: Candle) -> SignalEvent | None:
        ...


# ---------------------------------------------------------------------------
# Config / Result
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_pct: float = 0.001
    slippage_pct: float = 0.0005
    position_sizer: PositionSizer | None = None


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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Bar-by-bar event-driven backtest engine.

    Wiring per bar
    --------------
    1. Execute pending orders from the *previous* bar at THIS bar's open.
    2. Enqueue MarketEvent for this bar.
    3. Drain the event queue: MarketEvent -> strategy.on_candle -> SignalEvent
       -> _build_order -> OrderEvent -> deferred to _pending_orders.
    4. Mark portfolio to close (equity curve point) after all events settled.

    This deferred-fill approach eliminates look-ahead bias (R-23): the
    strategy sees the completed bar first, then orders fill at the *next*
    bar's open price.

    The engine is stateless between runs: a fresh Portfolio and
    SimulatedBroker are constructed from *config* on every call to run().
    """

    def __init__(self) -> None:
        self._pending_orders: list[Order] = []
        self._pending_order_ages: dict[str, int] = {}   # order_id -> bars pending
        self._event_queue: deque[Event] = deque()

    def run(
        self,
        strategy: Strategy,
        candles: list[Candle],
        config: BacktestConfig,
        symbol: str = "UNKNOWN",
    ) -> BacktestResult:
        if not candles:
            raise ValueError("candles list must not be empty")

        portfolio = Portfolio(initial_capital=config.initial_capital)
        broker = SimulatedBroker(
            slippage_pct=config.slippage_pct,
            commission_pct=config.commission_pct,
        )
        sizer: PositionSizer = config.position_sizer or FixedQuantitySizer(1)

        # Set symbol on strategy so signals carry the correct ticker (R-03)
        strategy.symbol = symbol

        self._pending_orders.clear()
        self._pending_order_ages.clear()
        self._event_queue.clear()

        MAX_BARS_PENDING = 50  # GTC up to 50 bars, then cancel

        for candle in candles:
            # Execute pending orders from previous bar at THIS bar's open → enqueue FillEvents
            filled_ids: set[str] = set()
            for order in self._pending_orders:
                fill = broker.execute(order, candle)
                if fill is not None:
                    self._event_queue.append(fill)  # FillEvent directly
                    filled_ids.add(order.order_id)

            # Keep unfilled LIMIT/STOP orders; discard filled orders and MARKET orders
            self._pending_orders = [
                o for o in self._pending_orders
                if o.order_id not in filled_ids
                and o.order_type != OrderType.MARKET
            ]

            # Increment age of persisting orders and expire old ones
            self._pending_order_ages = {
                o.order_id: self._pending_order_ages.get(o.order_id, 0) + 1
                for o in self._pending_orders
            }
            self._pending_orders = [
                o for o in self._pending_orders
                if self._pending_order_ages.get(o.order_id, 0) < MAX_BARS_PENDING
            ]

            # Enqueue market event for this bar
            self._event_queue.append(MarketEvent(symbol=symbol, candle=candle))

            # Drain the event queue
            self._process_events(self._event_queue, strategy, portfolio, candle, sizer)

            # Mark portfolio to close (equity curve point) after all events settled
            portfolio.update({symbol: candle.close}, candle.timestamp)

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

    def _process_events(
        self,
        queue: deque[Event],
        strategy: Strategy,
        portfolio: Portfolio,
        candle: Candle,
        sizer: PositionSizer,
    ) -> None:
        """Drain the event queue for the current bar."""
        while queue:
            event = queue.popleft()
            if isinstance(event, FillEvent):
                portfolio.record_fill(
                    symbol=event.symbol,
                    direction=event.direction,
                    quantity=event.quantity,
                    fill_price=event.fill_price,
                    fill_date=event.fill_date,
                    commission=event.commission,
                )
            elif isinstance(event, MarketEvent):
                signal = strategy.on_candle(event.candle)
                if signal is not None:
                    queue.append(signal)
            elif isinstance(event, SignalEvent):
                order = self._build_order(event, portfolio, candle, sizer)
                if order is not None:
                    queue.append(OrderEvent(order=order))
            elif isinstance(event, OrderEvent):
                # Deferred to next bar's open (R-23 look-ahead bias fix)
                self._pending_orders.append(event.order)

    @staticmethod
    def _build_order(
        signal: SignalEvent,
        portfolio: Portfolio,
        candle: Candle,
        sizer: PositionSizer,
    ) -> Order | None:
        """Translate a SignalEvent into a MARKET Order, or None if nothing to do."""
        symbol = signal.symbol
        has_position = portfolio.has_position(symbol)

        if not has_position:
            # No position open — enter in the signal direction
            quantity = sizer.calculate(signal, portfolio, candle.close)
            order = Order(
                symbol=symbol,
                direction=signal.direction,
                quantity=quantity,
                order_type=signal.order_type,
                limit_price=signal.limit_price,
                stop_price=signal.stop_price,
            )
            order.order_id = str(uuid.uuid4())[:8]
            return order

        # Position already open — only act if signal is a reversal / close
        existing_dir = portfolio.open_positions[symbol][0]
        if signal.direction == existing_dir.opposite():
            # Close (and potentially reverse) by submitting a closing order
            # in the *opposite* direction of the signal so record_fill sees it
            # as closing the existing leg.  We use the existing position's
            # quantity so we close exactly what we have.
            existing_qty = portfolio.open_positions[symbol][1]
            order = Order(
                symbol=symbol,
                direction=signal.direction,
                quantity=existing_qty,
                order_type=OrderType.MARKET,
            )
            order.order_id = str(uuid.uuid4())[:8]
            return order

        # Signal is in the same direction as existing position — ignore
        return None

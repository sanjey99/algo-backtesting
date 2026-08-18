"""BacktestEngine — bar-by-bar event-driven backtest runner."""
from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from math import isfinite
from typing import Any, Protocol

from src.engine.broker import SimulatedBroker
from src.engine.context import StrategyContext
from src.engine.event import Event, FillEvent, MarketEvent, OrderEvent, SignalEvent
from src.engine.position_sizer import FixedQuantitySizer, PositionSizer
from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType
from src.models.portfolio import EquityPoint, FillOutcome, Portfolio
from src.models.trade import Trade
from src.observability import log_event

logger = logging.getLogger(__name__)

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


@dataclass
class _RunState:
    """Mutable state owned exclusively by one backtest run and its event queue."""

    run_id: str
    portfolio: Portfolio
    broker: SimulatedBroker
    strategy: Strategy
    requested_symbol: str | None
    position_sizer: PositionSizer
    symbol: str
    pending: _PendingOrder | None = None
    forced_order_ids: set[int] = field(default_factory=set)


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
    3. Queue a forced market cover after a short maintenance-margin breach.
    4. Build immutable strategy context from filled and pending state.
    5. Queue a valid strategy order for execution on a later candle, replacing
       any stale strategy order unless a forced cover is pending.

    The engine is stateless between runs: a fresh Portfolio and
    SimulatedBroker are constructed from *config* on every call to run().
    """

    def run(
        self,
        strategy: Strategy,
        candles: list[Candle],
        config: BacktestConfig,
        symbol: str | None = None,
        position_sizer: PositionSizer | None = None,
    ) -> BacktestResult:
        run_id = str(uuid.uuid4())
        try:
            log_event(
                logger,
                logging.INFO,
                "backtest.started",
                run_id=run_id,
                strategy=strategy.name,
                bar_count=len(candles),
            )
            return self._run(
                strategy,
                candles,
                config,
                run_id,
                symbol,
                position_sizer if position_sizer is not None else FixedQuantitySizer(1),
            )
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "backtest.failed",
                run_id=run_id,
                error_type=type(error).__name__,
            )
            raise

    def _run(
        self,
        strategy: Strategy,
        candles: list[Candle],
        config: BacktestConfig,
        run_id: str,
        requested_symbol: str | None,
        position_sizer: PositionSizer,
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

        # A request-supplied symbol defines the instrument for the entire run.
        # Older direct callers retain their signal-derived behavior when omitted.
        state = _RunState(
            run_id=run_id,
            portfolio=portfolio,
            broker=broker,
            strategy=strategy,
            requested_symbol=requested_symbol,
            position_sizer=position_sizer,
            symbol=requested_symbol or "UNKNOWN",
        )
        events: deque[Event] = deque()

        for candle_index, candle in enumerate(candles):
            if candle_index > 0:
                state.portfolio.accrue_short_borrow(candle.timestamp)
            if state.pending is not None:
                fill: FillEvent | None = state.broker.execute(state.pending.order, candle)
                if fill is None:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "order.untriggered",
                        **self._order_fields(run_id, state.pending.order),
                    )
                    if state.pending.order.order_type not in (
                        OrderType.LIMIT,
                        OrderType.STOP,
                    ):
                        state.pending = None
                else:
                    events.append(fill)
                    self._drain_events(events, state, candle)

            state.portfolio.update({state.symbol: candle.close}, candle.timestamp)

            if state.pending is None or not state.pending.forced_cover:
                open_positions = state.portfolio.open_positions
                if len(open_positions) == 1:
                    position_symbol, position = next(iter(open_positions.items()))
                    position_direction, position_quantity, *_ = position
                    maintenance_ratio = state.portfolio.maintenance_ratio(position_symbol)
                    if (
                        position_direction is Direction.SHORT
                        and maintenance_ratio is not None
                        and maintenance_ratio < config.short_maintenance_margin
                    ):
                        forced_order = Order(
                            symbol=position_symbol,
                            direction=Direction.LONG,
                            quantity=position_quantity,
                            order_type=OrderType.MARKET,
                            created_at=candle.timestamp,
                        )
                        state.forced_order_ids.add(id(forced_order))
                        events.append(OrderEvent(forced_order))
                        self._drain_events(events, state, candle)
                        log_event(
                            logger,
                            logging.WARNING,
                            "margin.call_queued",
                            run_id=run_id,
                            symbol=position_symbol,
                            quantity=position_quantity,
                            maintenance_ratio=maintenance_ratio,
                            maintenance_requirement=config.short_maintenance_margin,
                        )

            events.append(MarketEvent(state.symbol, candle))
            self._drain_events(events, state, candle)

        if state.pending is not None:
            log_event(
                logger,
                logging.DEBUG,
                "order.cancelled_end_of_data",
                **self._order_fields(run_id, state.pending.order),
            )
            if state.pending.forced_cover:
                log_event(
                    logger,
                    logging.WARNING,
                    "margin.call_unresolved",
                    run_id=run_id,
                    symbol=state.pending.order.symbol,
                    quantity=state.pending.order.quantity,
                )

        result = BacktestResult(
            strategy_name=strategy.name,
            symbol=state.symbol,
            start_date=candles[0].timestamp,
            end_date=candles[-1].timestamp,
            parameters=dict(strategy.parameters),
            trades=state.portfolio.trades,
            equity_curve=state.portfolio.equity_curve,
            final_equity=state.portfolio.equity,
            initial_capital=config.initial_capital,
            run_id=run_id,
        )
        log_event(
            logger,
            logging.INFO,
            "backtest.completed",
            run_id=run_id,
            strategy=strategy.name,
            symbol=state.symbol,
            trade_count=len(result.trades),
            final_equity=result.final_equity,
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _drain_events(
        self,
        events: deque[Event],
        state: _RunState,
        candle: Candle,
    ) -> None:
        """Apply queued events in FIFO order at the current execution phase."""
        while events:
            self._dispatch_event(events.popleft(), events, state, candle)

    def _dispatch_event(
        self,
        event: Event,
        events: deque[Event],
        state: _RunState,
        candle: Candle,
    ) -> None:
        """Apply one real engine event, possibly queueing its downstream event."""
        if isinstance(event, FillEvent):
            self._handle_fill_event(event, state)
            return

        if isinstance(event, MarketEvent):
            self._handle_market_event(event, events, state)
            return

        if isinstance(event, SignalEvent):
            self._handle_signal_event(event, events, state, candle)
            return

        if isinstance(event, OrderEvent):
            self._handle_order_event(event, state)
            return

        raise TypeError(f"Unsupported event type: {type(event).__name__}")

    def _handle_fill_event(self, event: FillEvent, state: _RunState) -> None:
        pending = state.pending
        if pending is None:
            raise RuntimeError("FillEvent dispatched without a pending order")
        fill_outcome: FillOutcome = state.portfolio.record_fill(
            symbol=event.symbol,
            direction=event.direction,
            quantity=event.quantity,
            fill_price=event.fill_price,
            fill_date=event.fill_date,
            commission=event.commission,
        )
        if not fill_outcome.accepted:
            log_event(
                logger,
                logging.WARNING,
                "order.rejected",
                **self._order_fields(state.run_id, pending.order),
                reason=fill_outcome.rejection_reason,
            )
        else:
            log_event(
                logger,
                logging.DEBUG,
                "order.filled",
                **self._order_fields(state.run_id, pending.order),
                fill_price=event.fill_price,
                commission=event.commission,
            )
            state.symbol = event.symbol
        state.pending = None

    def _handle_market_event(
        self,
        event: MarketEvent,
        events: deque[Event],
        state: _RunState,
    ) -> None:
        context = self._build_context(state.portfolio, state.pending)
        signal = state.strategy.on_candle(event.candle, context)
        if signal is not None:
            events.append(signal)

    def _handle_signal_event(
        self,
        event: SignalEvent,
        events: deque[Event],
        state: _RunState,
        candle: Candle,
    ) -> None:
        if state.pending is not None and state.pending.forced_cover:
            return
        order = self._build_order(
            event,
            state.portfolio,
            candle,
            state.requested_symbol,
            state.position_sizer,
        )
        if order is not None:
            events.append(OrderEvent(order))

    def _handle_order_event(self, event: OrderEvent, state: _RunState) -> None:
        forced_cover = id(event.order) in state.forced_order_ids
        state.forced_order_ids.discard(id(event.order))
        if state.pending is not None:
            log_event(
                logger,
                logging.DEBUG,
                "order.replaced",
                **self._order_fields(state.run_id, state.pending.order),
                replacement_order_type=event.order.order_type,
                replacement_direction=event.order.direction,
                replacement_quantity=event.order.quantity,
            )
        state.symbol = event.order.symbol
        state.pending = _PendingOrder(order=event.order, forced_cover=forced_cover)
        log_event(
            logger,
            logging.DEBUG,
            "order.queued",
            **self._order_fields(state.run_id, event.order),
        )

    @staticmethod
    def _order_fields(run_id: str, order: Order) -> dict[str, object]:
        """Return the safe, stable order identity fields for lifecycle logs."""
        fields: dict[str, object] = {
            "run_id": run_id,
            "symbol": order.symbol,
            "order_type": order.order_type,
            "direction": order.direction,
            "quantity": order.quantity,
        }
        if order.order_id:
            fields["order_id"] = order.order_id
        return fields

    @staticmethod
    def _build_order(
        signal: SignalEvent,
        portfolio: Portfolio,
        candle: Candle,
        requested_symbol: str | None,
        position_sizer: PositionSizer,
    ) -> Order | None:
        """Translate a signal into an opening or exact-position closing order."""
        execution_symbol = requested_symbol or signal.symbol
        open_positions = portfolio.open_positions

        if open_positions:
            execution_symbol, position = next(iter(open_positions.items()))
            existing_direction, quantity, *_ = position
            if signal.direction is not existing_direction.opposite():
                return None
        else:
            quantity = position_sizer.calculate(signal, portfolio, candle.close)

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

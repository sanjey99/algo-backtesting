"""BacktestEngine — bar-by-bar event-driven backtest runner."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from src.engine.broker import SimulatedBroker
from src.engine.context import StrategyContext
from src.engine.event import FillEvent, OrderEvent, SignalEvent
from src.models.candle import Candle
from src.models.order import Order, OrderType
from src.models.portfolio import EquityPoint, Portfolio
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
    1. strategy.on_candle(candle, context) -> Optional[SignalEvent]
    2. If signal and no open position    -> create MARKET OrderEvent (open)
       If signal is opposite open position -> create MARKET OrderEvent (close)
    3. broker.execute(order, candle) -> Optional[FillEvent]
    4. portfolio.record_fill(...)
    5. portfolio.update({symbol: close}, timestamp)

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

        portfolio = Portfolio(initial_capital=config.initial_capital)
        broker = SimulatedBroker(
            slippage_pct=config.slippage_pct,
            commission_pct=config.commission_pct,
        )

        # Infer symbol from candles (engine is single-symbol per run)
        # Strategies emit signals that carry their own symbol; we capture it
        # from the first signal seen, defaulting to "UNKNOWN" if none fires.
        symbol: str = "UNKNOWN"

        for candle in candles:
            signal: SignalEvent | None = strategy.on_candle(candle, StrategyContext())

            if signal is not None:
                symbol = signal.symbol
                order: Order | None = self._build_order(
                    signal, portfolio, candle
                )
                if order is not None:
                    order_event = OrderEvent(order=order)
                    fill: FillEvent | None = broker.execute(
                        order_event.order, candle
                    )
                    if fill is not None:
                        portfolio.record_fill(
                            symbol=fill.symbol,
                            direction=fill.direction,
                            quantity=fill.quantity,
                            fill_price=fill.fill_price,
                            fill_date=fill.fill_date,
                            commission=fill.commission,
                        )

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

    @staticmethod
    def _build_order(
        signal: SignalEvent,
        portfolio: Portfolio,
        candle: Candle,
    ) -> Order | None:
        """Translate a SignalEvent into a MARKET Order, or None if nothing to do."""
        symbol = signal.symbol
        has_position = portfolio.has_position(symbol)

        if not has_position:
            # No position open — enter in the signal direction
            return Order(
                symbol=symbol,
                direction=signal.direction,
                quantity=1,
                order_type=OrderType.MARKET,
            )

        # Position already open — only act if signal is a reversal / close
        existing_dir = portfolio.open_positions[symbol][0]
        if signal.direction == existing_dir.opposite():
            # Close (and potentially reverse) by submitting a closing order
            # in the *opposite* direction of the signal so record_fill sees it
            # as closing the existing leg.  We use the existing position's
            # quantity so we close exactly what we have.
            existing_qty = portfolio.open_positions[symbol][1]
            return Order(
                symbol=symbol,
                direction=signal.direction,
                quantity=existing_qty,
                order_type=OrderType.MARKET,
            )

        # Signal is in the same direction as existing position — ignore
        return None

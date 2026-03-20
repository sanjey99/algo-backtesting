"""Shared pytest fixtures."""
from datetime import datetime, timedelta

import pytest

from src.models.candle import Candle
from src.models.order import Direction
from src.models.portfolio import Portfolio


def make_candle(
    close: float = 100.0,
    open_: float = 99.0,
    high: float = 101.0,
    low: float = 98.0,
    volume: float = 1_000_000.0,
    adj_close: float | None = None,
    dt: datetime | None = None,
) -> Candle:
    return Candle(
        timestamp=dt or datetime(2023, 1, 2),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        adj_close=adj_close if adj_close is not None else close,
    )


def make_candle_series(n: int = 20, base: float = 100.0) -> list[Candle]:
    """Generate n daily candles with a gentle uptrend."""
    result = []
    start = datetime(2023, 1, 2)
    for i in range(n):
        c = base + i * 0.5
        result.append(
            make_candle(
                close=c,
                open_=c - 0.3,
                high=c + 0.5,
                low=c - 0.5,
                dt=start + timedelta(days=i),
            )
        )
    return result


@pytest.fixture
def candle() -> Candle:
    return make_candle()


@pytest.fixture
def candle_series() -> list[Candle]:
    return make_candle_series(50)


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(initial_capital=100_000.0)

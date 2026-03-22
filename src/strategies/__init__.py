from src.strategies.base import BaseStrategy
from src.strategies.breakout import BreakoutStrategy
from src.strategies.ma_crossover import MACrossoverStrategy
from src.strategies.rsi_mean_reversion import RSIMeanReversionStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "ma_crossover": MACrossoverStrategy,
    "rsi_mean_reversion": RSIMeanReversionStrategy,
    "breakout": BreakoutStrategy,
}

__all__ = [
    "BaseStrategy",
    "MACrossoverStrategy",
    "RSIMeanReversionStrategy",
    "BreakoutStrategy",
    "STRATEGY_REGISTRY",
]

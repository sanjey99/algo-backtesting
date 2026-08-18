"""Direct contract tests for strategy execution context."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.engine.context import StrategyContext
from src.models.order import Direction


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"position_quantity": -1}, "nonnegative"),
        ({"position_quantity": 1}, "must be zero"),
        ({"position_direction": Direction.LONG}, "must be positive"),
    ],
)
def test_strategy_context_rejects_inconsistent_position_state(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        StrategyContext(**kwargs)  # type: ignore[arg-type]


def test_strategy_context_is_frozen() -> None:
    context = StrategyContext(position_direction=Direction.LONG, position_quantity=1)

    with pytest.raises(FrozenInstanceError):
        context.position_quantity = 2  # type: ignore[misc]

"""Strategy routes — list available strategies and their parameter spaces."""
from __future__ import annotations

from fastapi import APIRouter

from src.api.schemas import StrategyInfo
from src.strategies import STRATEGY_REGISTRY

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.get("", response_model=list[StrategyInfo])
def list_strategies():
    result = []
    for key, cls in STRATEGY_REGISTRY.items():
        instance = cls()
        result.append(
            StrategyInfo(
                key=key,
                name=cls.name,
                parameter_space=instance.parameter_space,
            )
        )
    return result

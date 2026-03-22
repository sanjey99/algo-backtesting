"""Data routes — fetch and cache market data."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api.schemas import DataFetchOut, DataFetchRequest
from src.data import df_to_candles

router = APIRouter(prefix="/api/data", tags=["data"])


@router.post("/fetch", response_model=DataFetchOut)
def fetch_data(req: DataFetchRequest) -> DataFetchOut:
    from src.data.fetcher import YFinanceFetcher

    fetcher = YFinanceFetcher()
    df = fetcher.fetch(req.symbol, req.start, req.end)
    candles = df_to_candles(df)

    if not candles:
        raise HTTPException(status_code=422, detail="No data returned for given symbol/range")

    return DataFetchOut(
        symbol=req.symbol,
        n_candles=len(candles),
        start=candles[0].timestamp.date().isoformat(),
        end=candles[-1].timestamp.date().isoformat(),
        from_cache=False,
    )

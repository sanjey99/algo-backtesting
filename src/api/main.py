"""FastAPI application entry point."""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import backtest, data, strategies
from src.db.database import init_db
from src.observability import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    init_db()
    yield


app = FastAPI(
    title="Algo Backtester API",
    description=(
        "Event-driven backtesting engine with Walk-Forward Analysis and Permutation Testing."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

_cors_origins_raw = os.environ.get("CORS_ORIGINS", "*")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(backtest.router)
app.include_router(strategies.router)
app.include_router(data.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

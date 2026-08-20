"""FastAPI application entry point."""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from math import isfinite
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.routes import backtest, data, strategies
from src.db import database
from src.db.database import init_db
from src.observability import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    try:
        init_db()
        yield
    finally:
        database.get_engine().dispose()


app = FastAPI(
    title="Algo Backtester API",
    description=(
        "Event-driven backtesting engine with Walk-Forward Analysis and Permutation Testing."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _json_safe_validation_value(value: Any) -> Any:
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe_validation_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe_validation_value(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    _request: Request, error: RequestValidationError
) -> JSONResponse:
    """Return JSON-safe validation details for every syntactically valid request."""
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {"detail": _json_safe_validation_value(error.errors())}
        ),
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

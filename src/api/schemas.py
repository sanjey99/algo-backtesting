"""Pydantic request/response schemas for the FastAPI layer."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final

from pydantic import BaseModel, Field, model_validator

from src.analytics.metrics import MetricName

MAX_OPTIMIZATION_TRIALS: Final = 200
MAX_PERMUTATIONS: Final = 1_000

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class BacktestRequest(BaseModel):
    strategy: str = Field(..., description="Strategy key, e.g. 'ma_crossover'")
    symbol: str = Field(..., description="Ticker symbol, e.g. 'SPY'")
    start: str = Field(..., description="ISO date, e.g. '2020-01-01'")
    end: str = Field(..., description="ISO date, e.g. '2023-12-31'")
    params: dict[str, Any] = Field(default_factory=dict, description="Strategy parameters")
    initial_capital: float = Field(100_000.0, gt=0)
    commission_pct: float = Field(0.001, ge=0)
    slippage_pct: float = Field(0.0005, ge=0)

    @model_validator(mode="after")
    def _validate_dates(self) -> BacktestRequest:
        try:
            s = date.fromisoformat(self.start)
            e = date.fromisoformat(self.end)
        except ValueError as exc:
            raise ValueError(f"start/end must be ISO date strings (YYYY-MM-DD): {exc}") from exc
        if s >= e:
            raise ValueError(f"start ({self.start}) must be before end ({self.end})")
        return self


class WalkForwardRequest(BaseModel):
    strategy: str
    symbol: str
    start: str
    end: str
    in_sample_days: int = Field(252, gt=0)
    out_of_sample_days: int = Field(63, gt=0)
    step_days: int = Field(63, gt=0)
    n_optimization_trials: int = Field(50, gt=0, le=MAX_OPTIMIZATION_TRIALS)
    initial_capital: float = Field(100_000.0, gt=0)

    @model_validator(mode="after")
    def _validate_dates(self) -> WalkForwardRequest:
        try:
            s = date.fromisoformat(self.start)
            e = date.fromisoformat(self.end)
        except ValueError as exc:
            raise ValueError(f"start/end must be ISO date strings (YYYY-MM-DD): {exc}") from exc
        if s >= e:
            raise ValueError(f"start ({self.start}) must be before end ({self.end})")
        return self


class PermutationRequest(BaseModel):
    strategy: str
    symbol: str
    start: str
    end: str
    params: dict[str, Any] = Field(default_factory=dict)
    n_permutations: int = Field(200, gt=0, le=MAX_PERMUTATIONS)
    metric: MetricName = Field(MetricName.SHARPE_RATIO)
    initial_capital: float = Field(100_000.0, gt=0)

    @model_validator(mode="after")
    def _validate_dates(self) -> PermutationRequest:
        try:
            s = date.fromisoformat(self.start)
            e = date.fromisoformat(self.end)
        except ValueError as exc:
            raise ValueError(f"start/end must be ISO date strings (YYYY-MM-DD): {exc}") from exc
        if s >= e:
            raise ValueError(f"start ({self.start}) must be before end ({self.end})")
        return self


class DataFetchRequest(BaseModel):
    symbol: str
    start: str
    end: str
    use_cache: bool = True
    source: str = "auto"
    calendar: str = "XNYS"
    refresh: bool = False


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class TradeOut(BaseModel):
    entry_date: datetime
    exit_date: datetime | None
    direction: str
    entry_price: float
    exit_price: float | None
    quantity: float
    pnl: float | None
    pnl_pct: float | None
    commission: float


class EquityPointOut(BaseModel):
    date: datetime
    equity: float
    drawdown_pct: float


class BacktestSummary(BaseModel):
    run_id: str
    strategy_name: str
    symbol: str
    start_date: datetime
    end_date: datetime
    final_equity: float
    initial_capital: float
    metrics: dict[str, float]


class BacktestDetail(BacktestSummary):
    parameters: dict[str, Any]
    trades: list[TradeOut]
    equity_curve: list[EquityPointOut]


class WindowOut(BaseModel):
    window_index: int
    in_sample_start: datetime
    in_sample_end: datetime
    oos_start: datetime
    oos_end: datetime
    best_params: dict[str, Any]
    oos_sharpe: float


class WalkForwardOut(BaseModel):
    windows: list[WindowOut]
    combined_sharpe: float
    optimization_stability: float
    combined_metrics: dict[str, float]


class PermutationOut(BaseModel):
    actual_metric: float
    permuted_metrics: list[float]
    p_value: float
    is_significant: bool
    percentile: float


class AsyncJobOut(BaseModel):
    job_id: str
    status: str  # "pending" | "running" | "done" | "error"
    result: PermutationOut | None = None
    error: str | None = None


class StrategyInfo(BaseModel):
    key: str
    name: str
    parameter_space: dict[str, Any]


class DataAcquisitionSummary(BaseModel):
    acquisition_id: str
    status: str
    sources_used: list[str]
    selected_source: str | None = None
    cache_status: str
    requested_sessions: int
    accepted_rows: int
    rejected_rows: int
    missing_sessions: int
    duplicates_removed: int
    coverage: float | None
    warnings: int


class DataFetchOut(BaseModel):
    symbol: str
    n_candles: int
    start: str
    end: str
    from_cache: bool
    summary: DataAcquisitionSummary

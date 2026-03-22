"""Backtest routes — run, retrieve, walk-forward, permutation."""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from src.analytics.metrics import compute_all_metrics
from src.analytics.permutation_test import PermutationTester
from src.analytics.walk_forward import WalkForwardAnalyzer
from src.api.deps import get_db, get_job, set_job
from src.api.schemas import (
    AsyncJobOut,
    BacktestDetail,
    BacktestRequest,
    BacktestSummary,
    EquityPointOut,
    PermutationOut,
    PermutationRequest,
    TradeOut,
    WalkForwardOut,
    WalkForwardRequest,
    WindowOut,
)
from src.db.crud import (
    get_backtest_run,
    get_equity_curve,
    get_metrics,
    get_trades,
    list_backtest_runs,
    save_backtest_run,
)
from src.engine.backtest import BacktestConfig, BacktestEngine
from src.strategies import STRATEGY_REGISTRY

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

DBDep = Annotated[Session, Depends(get_db)]


def _fetch_candles(symbol: str, start: str, end: str) -> list[Any]:
    """Fetch candles via YFinanceFetcher, with parquet caching via DataStore."""
    from datetime import datetime
    from src.data import df_to_candles
    from src.data.fetcher import YFinanceFetcher
    from src.data.store import DataStore

    fetcher = YFinanceFetcher()
    store = DataStore()
    df = store.fetch_or_cache(
        symbol,
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
        fetcher,
    )
    return df_to_candles(df)


def _strategy_from_request(strategy_key: str, params: dict[str, Any]):
    cls = STRATEGY_REGISTRY.get(strategy_key)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_key!r}")
    try:
        return cls(**params) if params else cls()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid strategy params: {exc}") from exc


# ---------------------------------------------------------------------------
# POST /api/backtest — run a backtest, persist and return summary
# ---------------------------------------------------------------------------


@router.post("", response_model=BacktestSummary, status_code=201)
def run_backtest(req: BacktestRequest, db: DBDep):
    strategy = _strategy_from_request(req.strategy, req.params)
    candles = _fetch_candles(req.symbol, req.start, req.end)
    if not candles:
        raise HTTPException(status_code=422, detail="No candle data returned for given symbol/range")

    config = BacktestConfig(
        initial_capital=req.initial_capital,
        commission_pct=req.commission_pct,
        slippage_pct=req.slippage_pct,
    )
    result = BacktestEngine().run(strategy, candles, config)
    metrics = compute_all_metrics(result)

    trades_dicts = []
    for t in result.trades:
        trades_dicts.append({
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "direction": t.direction.value,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "pnl": t.pnl if t.is_closed else None,
            "pnl_pct": t.pnl_pct if t.is_closed else None,
            "commission": t.commission,
        })

    equity_dicts = [
        {"date": pt.date, "equity": pt.equity, "drawdown_pct": pt.drawdown_pct}
        for pt in result.equity_curve
    ]

    save_backtest_run(
        db,
        run_id=result.run_id,
        strategy_name=result.strategy_name,
        symbol=result.symbol,
        start_date=result.start_date,
        end_date=result.end_date,
        params=result.parameters,
        initial_capital=result.initial_capital,
        commission_pct=config.commission_pct,
        slippage_pct=config.slippage_pct,
        trades=trades_dicts,
        equity_curve=equity_dicts,
        metrics=metrics,
    )

    return BacktestSummary(
        run_id=result.run_id,
        strategy_name=result.strategy_name,
        symbol=result.symbol,
        start_date=result.start_date,
        end_date=result.end_date,
        final_equity=result.final_equity,
        initial_capital=result.initial_capital,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# GET /api/backtest — list recent runs
# ---------------------------------------------------------------------------


@router.get("", response_model=list[BacktestSummary])
def list_runs(db: DBDep, limit: int = 50):
    runs = list_backtest_runs(db, limit=limit)
    result = []
    for run in runs:
        m = get_metrics(db, run.id)
        equity_pts = get_equity_curve(db, run.id)
        result.append(BacktestSummary(
            run_id=run.id,
            strategy_name=run.strategy_name,
            symbol=run.symbol,
            start_date=run.start_date,
            end_date=run.end_date,
            final_equity=equity_pts[-1].equity if equity_pts else run.initial_capital,
            initial_capital=run.initial_capital,
            metrics=m,
        ))
    return result


# ---------------------------------------------------------------------------
# GET /api/backtest/{run_id}
# ---------------------------------------------------------------------------


@router.get("/{run_id}", response_model=BacktestDetail)
def get_run(run_id: str, db: DBDep):
    import json

    run = get_backtest_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    metrics = get_metrics(db, run_id)
    trades = [
        TradeOut(
            entry_date=t.entry_date,
            exit_date=t.exit_date,
            direction=t.direction,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            quantity=t.quantity,
            pnl=t.pnl,
            pnl_pct=t.pnl_pct,
            commission=t.commission,
        )
        for t in get_trades(db, run_id)
    ]
    equity = [
        EquityPointOut(date=pt.date, equity=pt.equity, drawdown_pct=pt.drawdown_pct)
        for pt in get_equity_curve(db, run_id)
    ]

    return BacktestDetail(
        run_id=run.id,
        strategy_name=run.strategy_name,
        symbol=run.symbol,
        start_date=run.start_date,
        end_date=run.end_date,
        final_equity=equity[-1].equity if equity else run.initial_capital,
        initial_capital=run.initial_capital,
        metrics=metrics,
        parameters=json.loads(run.params_json or "{}"),
        trades=trades,
        equity_curve=equity,
    )


# ---------------------------------------------------------------------------
# GET /api/backtest/{run_id}/trades
# ---------------------------------------------------------------------------


@router.get("/{run_id}/trades", response_model=list[TradeOut])
def get_run_trades(run_id: str, db: DBDep):
    if get_backtest_run(db, run_id) is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return [
        TradeOut(
            entry_date=t.entry_date,
            exit_date=t.exit_date,
            direction=t.direction,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            quantity=t.quantity,
            pnl=t.pnl,
            pnl_pct=t.pnl_pct,
            commission=t.commission,
        )
        for t in get_trades(db, run_id)
    ]


# ---------------------------------------------------------------------------
# GET /api/backtest/{run_id}/equity-curve
# ---------------------------------------------------------------------------


@router.get("/{run_id}/equity-curve", response_model=list[EquityPointOut])
def get_run_equity_curve(run_id: str, db: DBDep):
    if get_backtest_run(db, run_id) is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return [
        EquityPointOut(date=pt.date, equity=pt.equity, drawdown_pct=pt.drawdown_pct)
        for pt in get_equity_curve(db, run_id)
    ]


# ---------------------------------------------------------------------------
# POST /api/backtest/walk-forward
# ---------------------------------------------------------------------------


@router.post("/walk-forward", response_model=WalkForwardOut)
def run_walk_forward(req: WalkForwardRequest):
    cls = STRATEGY_REGISTRY.get(req.strategy)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy!r}")

    candles = _fetch_candles(req.symbol, req.start, req.end)
    if not candles:
        raise HTTPException(status_code=422, detail="No candle data returned")

    config = BacktestConfig(initial_capital=req.initial_capital)
    analyzer = WalkForwardAnalyzer(
        strategy_cls=cls,
        in_sample_days=req.in_sample_days,
        out_of_sample_days=req.out_of_sample_days,
        step_days=req.step_days,
        n_optimization_trials=req.n_optimization_trials,
        config=config,
    )
    wf = analyzer.run(candles)

    return WalkForwardOut(
        windows=[
            WindowOut(
                window_index=w.window_index,
                in_sample_start=w.in_sample_start,
                in_sample_end=w.in_sample_end,
                oos_start=w.oos_start,
                oos_end=w.oos_end,
                best_params=w.best_params,
                oos_sharpe=w.oos_sharpe,
            )
            for w in wf.windows
        ],
        combined_sharpe=wf.combined_sharpe,
        optimization_stability=wf.optimization_stability,
        combined_metrics=wf.combined_metrics,
    )


# ---------------------------------------------------------------------------
# POST /api/backtest/permutation-test — 202 Accepted + background task
# ---------------------------------------------------------------------------


def _run_permutation_bg(job_id: str, req: PermutationRequest) -> None:
    """Background worker — updates job store on completion."""
    try:
        set_job(job_id, AsyncJobOut(job_id=job_id, status="running"))
        strategy = _strategy_from_request(req.strategy, req.params)
        candles = _fetch_candles(req.symbol, req.start, req.end)
        config = BacktestConfig(initial_capital=req.initial_capital)
        tester = PermutationTester(
            strategy=strategy,
            candles=candles,
            n_permutations=req.n_permutations,
            metric=req.metric,
            config=config,
        )
        perm_result = tester.run()
        set_job(
            job_id,
            AsyncJobOut(
                job_id=job_id,
                status="done",
                result=PermutationOut(
                    actual_metric=perm_result.actual_metric,
                    permuted_metrics=perm_result.permuted_metrics,
                    p_value=perm_result.p_value,
                    is_significant=perm_result.is_significant,
                    percentile=perm_result.percentile,
                ),
            ),
        )
    except Exception as exc:
        set_job(job_id, AsyncJobOut(job_id=job_id, status="error", error=str(exc)))


@router.post("/permutation-test", response_model=AsyncJobOut, status_code=202)
def start_permutation_test(req: PermutationRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    set_job(job_id, AsyncJobOut(job_id=job_id, status="pending"))
    background_tasks.add_task(_run_permutation_bg, job_id, req)
    return get_job(job_id)


@router.get("/permutation-test/{job_id}", response_model=AsyncJobOut)
def poll_permutation_test(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

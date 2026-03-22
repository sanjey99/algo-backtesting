"""CRUD operations — all persistence in single transactions."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.db.tables import BacktestRun, EquityCurvePoint, MetricRecord, TradeRecord


def save_backtest_run(
    session: Session,
    *,
    run_id: str | None = None,
    strategy_name: str,
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    params: dict[str, Any],
    initial_capital: float,
    commission_pct: float,
    slippage_pct: float,
    trades: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    metrics: dict[str, float],
) -> str:
    """Persist a complete backtest run in one transaction. Returns run_id."""
    rid = run_id or str(uuid.uuid4())

    run = BacktestRun(
        id=rid,
        strategy_name=strategy_name,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        params_json=json.dumps(params),
        initial_capital=initial_capital,
        commission_pct=commission_pct,
        slippage_pct=slippage_pct,
    )
    session.add(run)

    for t in trades:
        session.add(
            TradeRecord(
                backtest_id=rid,
                entry_date=t["entry_date"],
                exit_date=t.get("exit_date"),
                direction=t["direction"],
                entry_price=t["entry_price"],
                exit_price=t.get("exit_price"),
                quantity=t["quantity"],
                pnl=t.get("pnl"),
                pnl_pct=t.get("pnl_pct"),
                commission=t.get("commission", 0.0),
            )
        )

    for pt in equity_curve:
        session.add(
            EquityCurvePoint(
                backtest_id=rid,
                date=pt["date"],
                equity=pt["equity"],
                drawdown_pct=pt.get("drawdown_pct", 0.0),
            )
        )

    for name, value in metrics.items():
        session.add(MetricRecord(backtest_id=rid, metric_name=name, metric_value=value))

    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return rid


def get_backtest_run(session: Session, run_id: str) -> BacktestRun | None:
    return session.get(BacktestRun, run_id)


def list_backtest_runs(session: Session, limit: int = 50) -> list[BacktestRun]:
    from sqlalchemy import select

    stmt = select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def get_trades(session: Session, run_id: str) -> list[TradeRecord]:
    from sqlalchemy import select

    stmt = select(TradeRecord).where(TradeRecord.backtest_id == run_id)
    return list(session.scalars(stmt))


def get_equity_curve(session: Session, run_id: str) -> list[EquityCurvePoint]:
    from sqlalchemy import select

    stmt = select(EquityCurvePoint).where(EquityCurvePoint.backtest_id == run_id).order_by(
        EquityCurvePoint.date
    )
    return list(session.scalars(stmt))


def get_metrics(session: Session, run_id: str) -> dict[str, float]:
    from sqlalchemy import select

    stmt = select(MetricRecord).where(MetricRecord.backtest_id == run_id)
    rows = session.scalars(stmt)
    return {r.metric_name: r.metric_value for r in rows}

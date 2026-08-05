"""Integration tests for the ordered realized-trade analytics query."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.analytics.sql_contracts import TRADE_SEQUENCE_CONTRACT
from src.analytics.sql_service import AnalyticsService, RunNotFoundError
from src.db.crud import save_backtest_run

SEQUENCE_RUN_ID = "sequence-run"
OPEN_ONLY_RUN_ID = "open-only-run"


def _seed_trade_sequence_runs(engine: Engine) -> list[int]:
    """Add hand-calculable trades without changing the shared fixture."""
    with Session(engine) as session:
        save_backtest_run(
            session,
            run_id=SEQUENCE_RUN_ID,
            strategy_name="sequence_strategy",
            symbol="IWM",
            start_date=datetime(2024, 2, 1),
            end_date=datetime(2024, 2, 29),
            params={},
            initial_capital=10_000.0,
            commission_pct=0.001,
            slippage_pct=0.0005,
            trades=[
                _trade(1, 10.0),
                _trade(2, -5.0),
                _trade(3, 20.0),
                _trade(3, -10.0),
                _trade(4, 5.0),
                _trade(5, 30.0),
            ],
            equity_curve=[],
            metrics={},
        )
        save_backtest_run(
            session,
            run_id=OPEN_ONLY_RUN_ID,
            strategy_name="open_only",
            symbol="IWM",
            start_date=datetime(2024, 2, 1),
            end_date=datetime(2024, 2, 29),
            params={},
            initial_capital=10_000.0,
            commission_pct=0.001,
            slippage_pct=0.0005,
            trades=[
                {
                    "entry_date": datetime(2024, 2, 1),
                    "direction": "LONG",
                    "entry_price": 100.0,
                    "quantity": 1,
                }
            ],
            equity_curve=[],
            metrics={},
        )
        trade_ids = list(
            session.execute(
                text(
                    "SELECT id FROM trades WHERE backtest_id = :run_id ORDER BY id",
                ),
                {"run_id": SEQUENCE_RUN_ID},
            ).scalars()
        )
    return trade_ids


def _trade(exit_day: int, pnl: float) -> dict[str, object]:
    return {
        "entry_date": datetime(2024, 2, 1),
        "exit_date": datetime(2024, 2, exit_day),
        "direction": "LONG",
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl,
        "quantity": 1,
        "pnl": pnl,
    }


def test_trade_sequence_calculates_ordered_windows_and_tie_breaks(analytics_db: Engine) -> None:
    """Each closed trade receives deterministic running and rolling realized-P&L facts."""
    trade_ids = _seed_trade_sequence_runs(analytics_db)

    frame = AnalyticsService(analytics_db).trade_sequence(SEQUENCE_RUN_ID)

    assert tuple(frame.columns) == TRADE_SEQUENCE_CONTRACT.names
    assert frame["trade_id"].tolist() == trade_ids
    assert frame["trade_sequence"].tolist() == [1, 2, 3, 4, 5, 6]
    assert frame["cumulative_pnl"].tolist() == pytest.approx([10.0, 5.0, 25.0, 15.0, 20.0, 50.0])
    assert frame["cumulative_wins"].tolist() == [1, 1, 2, 2, 3, 4]
    assert frame["cumulative_win_rate"].tolist() == pytest.approx(
        [1.0, 0.5, 2 / 3, 0.5, 0.6, 2 / 3]
    )
    assert frame["rolling_5_trade_avg_pnl"].tolist() == pytest.approx(
        [10.0, 2.5, 25 / 3, 3.75, 4.0, 8.0]
    )


def test_trade_sequence_normalizes_a_known_run_without_closed_trades(analytics_db: Engine) -> None:
    """Open positions are excluded without treating the known run as missing."""
    _seed_trade_sequence_runs(analytics_db)

    frame = AnalyticsService(analytics_db).trade_sequence(OPEN_ONLY_RUN_ID)

    assert tuple(frame.columns) == TRADE_SEQUENCE_CONTRACT.names
    assert frame.empty
    assert {name: str(dtype) for name, dtype in frame.dtypes.items()} == {
        "trade_id": "Int64",
        "exit_date": "datetime64[ns]",
        "pnl": "Float64",
        "trade_sequence": "Int64",
        "cumulative_pnl": "Float64",
        "cumulative_wins": "Int64",
        "cumulative_win_rate": "Float64",
        "rolling_5_trade_avg_pnl": "Float64",
    }


def test_trade_sequence_rejects_an_unknown_run(analytics_db: Engine) -> None:
    """An absent parent id is distinct from a known run with no closed trades."""
    with pytest.raises(RunNotFoundError, match="does not exist"):
        AnalyticsService(analytics_db).trade_sequence("missing-run")

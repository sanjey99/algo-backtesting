"""Deterministic persisted runs used by SQL analytics integration tests."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from src.db.crud import save_backtest_run

RUN_MA_ID = "run-ma"
RUN_RSI_ID = "run-rsi"
RUN_OTHER_ID = "run-other"

COMPARISON_START = datetime(2024, 1, 1)
COMPARISON_END = datetime(2024, 1, 31)


def _metrics(total_return: float, sharpe_ratio: float) -> dict[str, float]:
    return {
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sharpe_ratio + 0.1,
        "cagr": total_return,
        "max_drawdown": -0.05,
        "max_drawdown_duration": 3.0,
        "win_rate": 0.5,
        "profit_factor": 1.5,
        "calmar_ratio": 0.4,
        "total_trades": 2.0,
        "total_return": total_return,
    }


def seed_comparison_runs(session: Session) -> None:
    """Persist fixed runs whose comparisons can be calculated by hand."""
    save_backtest_run(
        session,
        run_id=RUN_MA_ID,
        strategy_name="moving_average",
        symbol="SPY",
        start_date=COMPARISON_START,
        end_date=COMPARISON_END,
        params={"fast": 10, "slow": 30},
        initial_capital=10_000.0,
        commission_pct=0.001,
        slippage_pct=0.0005,
        trades=[
            {
                "entry_date": datetime(2024, 1, 3),
                "exit_date": datetime(2024, 1, 8),
                "direction": "LONG",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "quantity": 10,
                "pnl": 100.0,
                "pnl_pct": 0.1,
                "commission": 5.0,
            },
            {
                "entry_date": datetime(2024, 1, 10),
                "exit_date": datetime(2024, 1, 15),
                "direction": "SHORT",
                "entry_price": 120.0,
                "exit_price": 115.0,
                "quantity": 10,
                "pnl": 50.0,
                "pnl_pct": 0.041667,
                "commission": 5.0,
            },
            {
                "entry_date": datetime(2024, 1, 20),
                "direction": "LONG",
                "entry_price": 115.0,
                "quantity": 5,
                "commission": 2.0,
            },
        ],
        equity_curve=[
            {"date": datetime(2024, 1, 1), "equity": 10_000.0},
            {"date": datetime(2024, 1, 15), "equity": 10_100.0, "drawdown_pct": -0.01},
            {"date": datetime(2024, 1, 31), "equity": 10_200.0},
        ],
        metrics=_metrics(0.02, 1.5),
    )
    save_backtest_run(
        session,
        run_id=RUN_RSI_ID,
        strategy_name="rsi_reversion",
        symbol="SPY",
        start_date=COMPARISON_START,
        end_date=COMPARISON_END,
        params={"period": 14},
        initial_capital=10_000.0,
        commission_pct=0.001,
        slippage_pct=0.0005,
        trades=[
            {
                "entry_date": datetime(2024, 1, 5),
                "exit_date": datetime(2024, 1, 12),
                "direction": "LONG",
                "entry_price": 100.0,
                "exit_price": 107.0,
                "quantity": 10,
                "pnl": 70.0,
                "pnl_pct": 0.07,
                "commission": 4.0,
            }
        ],
        equity_curve=[
            {"date": datetime(2024, 1, 1), "equity": 10_000.0},
            {"date": datetime(2024, 1, 31), "equity": 10_100.0},
        ],
        metrics=_metrics(0.01, 1.0),
    )
    save_backtest_run(
        session,
        run_id=RUN_OTHER_ID,
        strategy_name="breakout",
        symbol="QQQ",
        start_date=COMPARISON_START,
        end_date=COMPARISON_END,
        params={"lookback": 20},
        initial_capital=20_000.0,
        commission_pct=0.001,
        slippage_pct=0.0005,
        trades=[],
        equity_curve=[
            {"date": datetime(2024, 1, 1), "equity": 20_000.0},
            {"date": datetime(2024, 1, 31), "equity": 20_200.0},
        ],
        metrics=_metrics(0.01, 0.5),
    )

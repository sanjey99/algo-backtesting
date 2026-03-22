"""Report generation — summarise BacktestResult as a dict or text."""
from __future__ import annotations

from src.analytics.metrics import compute_all_metrics
from src.engine.backtest import BacktestResult


def generate_report(result: BacktestResult) -> dict:
    """Return a summary dict of all standard metrics for a BacktestResult."""
    metrics = compute_all_metrics(result)
    return {
        "strategy_name": result.strategy_name,
        "symbol": result.symbol,
        "start_date": result.start_date.isoformat(),
        "end_date": result.end_date.isoformat(),
        "parameters": result.parameters,
        "initial_capital": result.initial_capital,
        "final_equity": result.final_equity,
        "total_trades": int(metrics["total_trades"]),
        "sharpe_ratio": metrics["sharpe_ratio"],
        "sortino_ratio": metrics["sortino_ratio"],
        "max_drawdown": metrics["max_drawdown"],
        "cagr": metrics["cagr"],
        "calmar_ratio": metrics["calmar_ratio"],
        "win_rate": metrics["win_rate"],
        "profit_factor": metrics["profit_factor"],
    }

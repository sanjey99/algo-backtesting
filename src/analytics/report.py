"""Report generation — summarise BacktestResult as a dict or standalone HTML."""
from __future__ import annotations

from html import escape
from typing import Any

from src.analytics.metrics import compute_all_metrics
from src.engine.backtest import BacktestResult

try:
    import plotly.graph_objects as go
    import plotly.io as pio
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False


def _build_equity_chart_html(equity_curve: list[Any]) -> str:
    """Return a standalone Plotly chart HTML snippet."""
    if not PLOTLY_AVAILABLE:
        return "<p>Plotly not available — install plotly to see the equity chart.</p>"
    if not equity_curve:
        return "<p>No equity data.</p>"

    date_values = [
        pt["date"] if isinstance(pt, dict) else pt.date for pt in equity_curve
    ]
    dates = [
        value.isoformat() if hasattr(value, "isoformat") else str(value)
        for value in date_values
    ]
    equities = [pt["equity"] if isinstance(pt, dict) else pt.equity for pt in equity_curve]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates,
        y=equities,
        mode="lines",
        name="Equity",
        line=dict(color="#00b4d8", width=2),
    ))
    fig.update_layout(
        title="Portfolio Equity",
        plot_bgcolor="#0e1117",
        paper_bgcolor="#1a1f2e",
        font=dict(color="#e2e8f0"),
        height=400,
        margin=dict(l=20, r=20, t=40, b=20),
    )
    return str(pio.to_html(fig, full_html=False, include_plotlyjs=True))


def generate_html_report(result: BacktestResult, metrics: dict[str, Any] | None = None) -> str:
    """Return a fully standalone HTML report with embedded Plotly equity chart."""
    if metrics is None:
        metrics = compute_all_metrics(result)

    chart_html = _build_equity_chart_html(result.equity_curve)
    strategy_name = escape(str(result.strategy_name), quote=True)
    symbol = escape(str(result.symbol), quote=True)
    start_date = escape(str(result.start_date), quote=True)
    end_date = escape(str(result.end_date), quote=True)

    def fmt_pct(v: float) -> str:
        return f"{v * 100:.2f}%"

    def fmt_f(v: float) -> str:
        return f"{v:.4f}"

    percentage_metrics = ("rate", "drawdown", "cagr", "return")
    rows_html = "".join(
        f"<tr><td>{escape(k.replace('_', ' ').title(), quote=True)}</td><td>"
        f"{fmt_pct(v) if any(term in k for term in percentage_metrics) else fmt_f(v)}</td></tr>"
        for k, v in metrics.items()
        if isinstance(v, (int, float))
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Backtest Report — {strategy_name} / {symbol}</title>
<style>
  body {{ font-family: sans-serif; background: #0e1117; color: #e2e8f0; margin: 0; padding: 24px; }}
  h1 {{ color: #00b4d8; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 600px; margin-bottom: 32px; }}
  th, td {{ border: 1px solid #2d3748; padding: 8px 12px; text-align: left; }}
  th {{ background: #1a1f2e; }}
</style>
</head>
<body>
<h1>Backtest Report</h1>
<p><strong>Strategy:</strong> {strategy_name} &nbsp;|&nbsp;
   <strong>Symbol:</strong> {symbol} &nbsp;|&nbsp;
   <strong>Period:</strong> {start_date} – {end_date}</p>
<table>
  <tr><th>Metric</th><th>Value</th></tr>
  {rows_html}
</table>
{chart_html}
</body>
</html>"""


def generate_report(result: BacktestResult) -> dict[str, Any]:
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

"""Streamlit dashboard — algo backtester UI.

Sections:
  1. Sidebar — strategy/symbol/date/param inputs
  2. KPI cards — Sharpe, CAGR, Max Drawdown, Win Rate, Total Trades
  3. Equity curve + drawdown overlay
  4. Trade table
  5. Monthly returns heatmap
  6. Walk-Forward Analysis tab
  7. Permutation Test tab
  8. Strategy comparison tab

Run:
    streamlit run src/dashboard/app.py
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import TypedDict, cast

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Inline imports — avoid heavy imports at module level for faster startup
# ---------------------------------------------------------------------------
from src.analytics.metrics import compute_all_metrics
from src.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from src.models.candle import Candle
from src.models.portfolio import EquityPoint
from src.models.trade import Trade
from src.strategies import STRATEGY_REGISTRY

type Metrics = dict[str, float]


class DashboardConfig(TypedDict):
    strategy_key: str
    params: dict[str, int | float]
    symbol: str
    start: str
    end: str
    initial_capital: float
    run: bool

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Algo Backtester",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Helper — run backtest inline (no API dependency for dashboard)
# ---------------------------------------------------------------------------

def _fetch_candles(symbol: str, start: str, end: str) -> list[Candle]:
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


def _run_backtest(
    strategy_key: str,
    params: Mapping[str, int | float],
    symbol: str,
    start: str,
    end: str,
    initial_capital: float,
) -> tuple[BacktestResult | None, Metrics | None, list[Candle] | None]:
    cls = STRATEGY_REGISTRY[strategy_key]
    strategy = cls(**params) if params else cls()
    candles = _fetch_candles(symbol, start, end)
    if not candles:
        return None, None, None
    config = BacktestConfig(initial_capital=initial_capital)
    result = BacktestEngine().run(strategy, candles, config)
    metrics = compute_all_metrics(result)
    return result, metrics, candles


# ---------------------------------------------------------------------------
# Section 1 — Sidebar inputs
# ---------------------------------------------------------------------------

def render_sidebar() -> DashboardConfig:
    st.sidebar.header("⚙️ Backtest Config")

    strategy_key = st.sidebar.selectbox(
        "Strategy",
        options=list(STRATEGY_REGISTRY.keys()),
        format_func=lambda k: k.replace("_", " ").title(),
    )

    symbol = st.sidebar.text_input("Symbol", value="SPY")
    col1, col2 = st.sidebar.columns(2)
    start = col1.date_input("Start", value=date(2018, 1, 1))
    end = col2.date_input("End", value=date(2023, 12, 31))
    initial_capital = st.sidebar.number_input(
        "Initial Capital ($)", min_value=1_000.0, value=100_000.0, step=5_000.0
    )

    st.sidebar.subheader("Strategy Parameters")
    cls = STRATEGY_REGISTRY[strategy_key]
    instance = cls()
    params: dict[str, int | float] = {}
    for name, (low, high, step) in instance.parameter_space.items():
        default = getattr(instance, name, low)
        params[name] = st.sidebar.slider(
            name.replace("_", " ").title(),
            min_value=int(low),
            max_value=int(high),
            value=int(default),
            step=int(step),
        )

    run = st.sidebar.button("▶ Run Backtest", type="primary", use_container_width=True)

    return {
        "strategy_key": strategy_key,
        "params": params,
        "symbol": symbol.upper().strip(),
        "start": str(start),
        "end": str(end),
        "initial_capital": float(initial_capital),
        "run": run,
    }


# ---------------------------------------------------------------------------
# Section 2 — KPI cards
# ---------------------------------------------------------------------------

def render_kpis(metrics: Metrics, initial_capital: float, final_equity: float) -> None:
    st.subheader("📊 Key Performance Indicators")
    kpis = [
        ("Sharpe Ratio", f"{metrics.get('sharpe_ratio', 0):.2f}", ""),
        ("Sortino Ratio", f"{metrics.get('sortino_ratio', 0):.2f}", ""),
        ("CAGR", f"{metrics.get('cagr', 0)*100:.1f}%", ""),
        ("Max Drawdown", f"{metrics.get('max_drawdown', 0)*100:.1f}%", ""),
        ("Win Rate", f"{metrics.get('win_rate', 0)*100:.1f}%", ""),
        ("Profit Factor", f"{metrics.get('profit_factor', 0):.2f}", ""),
        ("Total Trades", f"{int(metrics.get('total_trades', 0))}", ""),
        ("Total Return", f"{metrics.get('total_return', 0)*100:.1f}%", ""),
    ]
    cols = st.columns(len(kpis))
    for col, (label, value, _) in zip(cols, kpis):
        col.metric(label, value)


# ---------------------------------------------------------------------------
# Section 3 — Equity curve + drawdown overlay
# ---------------------------------------------------------------------------

def render_equity_curve(equity_curve: list[EquityPoint]) -> None:
    if not equity_curve:
        st.info("No equity data.")
        return

    dates = [pt.date for pt in equity_curve]
    equities = [pt.equity for pt in equity_curve]
    drawdowns = [pt.drawdown_pct * 100 for pt in equity_curve]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.03,
        subplot_titles=("Portfolio Equity", "Drawdown (%)"),
    )

    fig.add_trace(
        go.Scatter(x=dates, y=equities, name="Equity", line=dict(color="#00b4d8", width=2)),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=dates, y=drawdowns,
            name="Drawdown",
            fill="tozeroy",
            line=dict(color="#e63946", width=1),
            fillcolor="rgba(230, 57, 70, 0.2)",
        ),
        row=2, col=1,
    )

    fig.update_layout(
        height=500,
        showlegend=True,
        plot_bgcolor="#0e1117",
        paper_bgcolor="#0e1117",
        font=dict(color="#fafafa"),
        xaxis=dict(gridcolor="#1f2937"),
        yaxis=dict(gridcolor="#1f2937"),
        xaxis2=dict(gridcolor="#1f2937"),
        yaxis2=dict(gridcolor="#1f2937"),
        margin=dict(l=0, r=0, t=30, b=0),
    )

    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Section 4 — Trade table
# ---------------------------------------------------------------------------

def render_trade_table(trades: list[Trade]) -> None:
    st.subheader("📋 Trade Log")
    if not trades:
        st.info("No closed trades.")
        return

    rows = []
    for t in trades:
        if not t.is_closed:
            continue
        rows.append({
            "Entry Date": t.entry_date.date() if t.entry_date else None,
            "Exit Date": t.exit_date.date() if t.exit_date else None,
            "Direction": t.direction.value if hasattr(t.direction, "value") else t.direction,
            "Entry Price": f"${t.entry_price:.2f}",
            "Exit Price": f"${t.exit_price:.2f}" if t.exit_price else "—",
            "P&L": f"${t.pnl:.2f}" if t.pnl is not None else "—",
            "P&L %": f"{t.pnl_pct*100:.2f}%" if t.pnl_pct is not None else "—",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, height=300)


# ---------------------------------------------------------------------------
# Section 5 — Monthly returns heatmap
# ---------------------------------------------------------------------------

def render_monthly_heatmap(equity_curve: list[EquityPoint]) -> None:
    st.subheader("🗓️ Monthly Returns Heatmap")
    if len(equity_curve) < 5:
        st.info("Not enough data for monthly heatmap.")
        return

    df = pd.DataFrame([{"date": pt.date, "equity": pt.equity} for pt in equity_curve])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    monthly = df["equity"].resample("ME").last().pct_change().dropna()
    if monthly.empty:
        st.info("Not enough data for monthly heatmap.")
        return

    monthly_df = monthly.reset_index()
    monthly_df.columns = ["date", "return"]
    monthly_df["year"] = monthly_df["date"].dt.year
    monthly_df["month"] = monthly_df["date"].dt.strftime("%b")

    pivot = monthly_df.pivot(index="year", columns="month", values="return")
    month_order = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    pivot = pivot.reindex(columns=[m for m in month_order if m in pivot.columns])

    fig = go.Figure(data=go.Heatmap(
        z=pivot.values * 100,
        x=pivot.columns.tolist(),
        y=[str(y) for y in pivot.index.tolist()],
        colorscale="RdYlGn",
        zmid=0,
        text=[[f"{v:.1f}%" if pd.notna(v) else "" for v in row] for row in pivot.values * 100],
        texttemplate="%{text}",
        showscale=True,
        colorbar=dict(title="Return %"),
    ))
    fig.update_layout(
        height=300,
        plot_bgcolor="#0e1117",
        paper_bgcolor="#0e1117",
        font=dict(color="#fafafa"),
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Section 6 — Walk-Forward Analysis tab
# ---------------------------------------------------------------------------

def render_walk_forward_tab(cfg: DashboardConfig) -> None:
    st.subheader("🔄 Walk-Forward Analysis")

    col1, col2, col3 = st.columns(3)
    is_days = col1.number_input("In-Sample Days", min_value=50, value=252, step=21)
    oos_days = col2.number_input("OOS Days", min_value=10, value=63, step=21)
    step_days = col3.number_input("Step Days", min_value=10, value=63, step=21)
    n_trials = st.number_input("Optimization Trials", min_value=3, max_value=200, value=20, step=5)

    wf = st.session_state.get("wfa_result")

    if st.button("▶ Run Walk-Forward", key="wfa_run"):
        with st.spinner("Running walk-forward analysis…"):
            from src.analytics.walk_forward import WalkForwardAnalyzer
            from src.engine.backtest import BacktestConfig

            cls = STRATEGY_REGISTRY[cfg["strategy_key"]]
            candles = _fetch_candles(cfg["symbol"], cfg["start"], cfg["end"])
            if not candles:
                st.error("No data returned.")
                return

            analyzer = WalkForwardAnalyzer(
                strategy_cls=cls,
                in_sample_days=int(is_days),
                out_of_sample_days=int(oos_days),
                step_days=int(step_days),
                n_optimization_trials=int(n_trials),
                config=BacktestConfig(initial_capital=cfg["initial_capital"]),
            )
            wf = analyzer.run(candles)
            st.session_state["wfa_result"] = wf

    if wf is not None:
        st.success(f"Completed {len(wf.windows)} windows")
        kpi_cols = st.columns(3)
        kpi_cols[0].metric("Combined Sharpe", f"{wf.combined_sharpe:.2f}")
        kpi_cols[1].metric("Optimization Stability (CV)", f"{wf.optimization_stability:.2f}")
        kpi_cols[2].metric("Windows", len(wf.windows))

        # Window results table
        rows = [
            {
                "Window": w.window_index + 1,
                "IS Start": w.in_sample_start,
                "IS End": w.in_sample_end,
                "OOS Sharpe": f"{w.oos_sharpe:.2f}",
                "Best Params": str(w.best_params),
            }
            for w in wf.windows
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

        # OOS equity curve
        if wf.combined_equity_curve:
            render_equity_curve(wf.combined_equity_curve)


# ---------------------------------------------------------------------------
# Section 7 — Permutation Test tab
# ---------------------------------------------------------------------------

def render_permutation_tab(
    cfg: DashboardConfig, current_metrics: Metrics | None
) -> None:
    st.subheader("🎲 Permutation Test (Statistical Significance)")

    n_perms = st.number_input(
        "Number of Permutations", min_value=10, max_value=1000, value=100, step=10
    )

    perm = st.session_state.get("perm_result")

    if st.button("▶ Run Permutation Test", key="perm_run"):
        with st.spinner(f"Running {n_perms} permutations…"):
            from src.analytics.permutation_test import PermutationTester

            cls = STRATEGY_REGISTRY[cfg["strategy_key"]]
            strategy = cls(**cfg["params"]) if cfg["params"] else cls()
            candles = _fetch_candles(cfg["symbol"], cfg["start"], cfg["end"])
            if not candles:
                st.error("No data returned.")
                return

            tester = PermutationTester(
                strategy=strategy,
                candles=candles,
                n_permutations=int(n_perms),
            )
            perm = tester.run()
            st.session_state["perm_result"] = perm

    if perm is not None:
        status_color = "green" if perm.is_significant else "red"
        sig_label = "✅ Significant" if perm.is_significant else "❌ Not Significant"

        c1, c2, c3 = st.columns(3)
        c1.metric("Actual Sharpe", f"{perm.actual_metric:.3f}")
        c2.metric("p-value", f"{perm.p_value:.3f}")
        c3.metric("Percentile", f"{perm.percentile:.1f}%")
        st.markdown(f"**Result:** :{status_color}[{sig_label}] (p < 0.05 threshold)")

        # Distribution plot
        fig = go.Figure()
        fig.add_trace(go.Histogram(
            x=perm.permuted_metrics,
            name="Permuted",
            nbinsx=30,
            marker_color="#6b7280",
            opacity=0.7,
        ))
        fig.add_vline(
            x=perm.actual_metric,
            line_dash="dash",
            line_color="#00b4d8",
            annotation_text=f"Actual: {perm.actual_metric:.2f}",
        )
        fig.update_layout(
            title="Null Distribution vs Actual Strategy",
            xaxis_title="Sharpe Ratio",
            yaxis_title="Count",
            height=350,
            plot_bgcolor="#0e1117",
            paper_bgcolor="#0e1117",
            font=dict(color="#fafafa"),
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Section 8 — Strategy comparison tab
# ---------------------------------------------------------------------------

def render_comparison_tab(cfg: DashboardConfig) -> None:
    st.subheader("🆚 Strategy Comparison")

    st.info(
        "ℹ️ Strategies are compared using their **default parameters**. Run Walk-Forward "
        "Analysis first to find optimal parameters per strategy."
    )

    candles: list[Candle] | None = None
    results: dict[str, Metrics] = st.session_state.get("comparison_results", {})

    if st.button("▶ Compare All Strategies", key="compare_run"):
        with st.spinner("Running all strategies…"):
            for key, cls in STRATEGY_REGISTRY.items():
                strategy = cls()
                if candles is None:
                    candles = _fetch_candles(cfg["symbol"], cfg["start"], cfg["end"])
                    if not candles:
                        st.error("No data.")
                        return
                config = BacktestConfig(initial_capital=cfg["initial_capital"])
                result = BacktestEngine().run(strategy, candles, config)
                metrics = compute_all_metrics(result)
                results[key] = metrics
        st.session_state["comparison_results"] = results

    if results:
        rows = []
        for key, m in results.items():
            rows.append({
                "Strategy": key.replace("_", " ").title(),
                "Sharpe": f"{m.get('sharpe_ratio', 0):.2f}",
                "CAGR": f"{m.get('cagr', 0)*100:.1f}%",
                "Max DD": f"{m.get('max_drawdown', 0)*100:.1f}%",
                "Win Rate": f"{m.get('win_rate', 0)*100:.1f}%",
                "Trades": int(m.get("total_trades", 0)),
                "Total Return": f"{m.get('total_return', 0)*100:.1f}%",
            })

        st.dataframe(pd.DataFrame(rows), use_container_width=True)

        # Bar chart: Sharpe comparison
        names = [r["Strategy"] for r in rows]
        sharpes = [float(m.get("sharpe_ratio", 0)) for m in results.values()]
        fig = go.Figure(go.Bar(x=names, y=sharpes, marker_color="#00b4d8"))
        fig.update_layout(
            title="Sharpe Ratio by Strategy",
            height=300,
            plot_bgcolor="#0e1117",
            paper_bgcolor="#0e1117",
            font=dict(color="#fafafa"),
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:
    st.title("📈 Algorithmic Backtester")
    st.caption("Event-driven • Walk-Forward Analysis • Permutation Testing")

    cfg = render_sidebar()

    # Run backtest on button press — store result in session state
    if cfg["run"]:
        with st.spinner(f"Running {cfg['strategy_key']} on {cfg['symbol']}…"):
            result, metrics, candles = _run_backtest(
                cfg["strategy_key"],
                cfg["params"],
                cfg["symbol"],
                cfg["start"],
                cfg["end"],
                cfg["initial_capital"],
            )
        if result is None:
            st.error(f"No data returned for {cfg['symbol']} in the selected date range.")
            return
        st.session_state["result"] = result
        st.session_state["metrics"] = metrics
        st.session_state["cfg"] = cfg

    result = cast(BacktestResult | None, st.session_state.get("result"))
    metrics = cast(Metrics | None, st.session_state.get("metrics"))
    active_cfg = cast(DashboardConfig, st.session_state.get("cfg", cfg))

    if result is None or metrics is None:
        st.info("👈 Configure and click **▶ Run Backtest** to get started.")
        return

    # Tabs
    tab_main, tab_wfa, tab_perm, tab_compare = st.tabs([
        "📈 Results", "🔄 Walk-Forward", "🎲 Permutation", "🆚 Compare"
    ])

    with tab_main:
        render_kpis(metrics, result.initial_capital, result.final_equity)
        st.divider()
        render_equity_curve(result.equity_curve)
        st.divider()
        render_monthly_heatmap(result.equity_curve)
        st.divider()
        render_trade_table(result.trades)

    with tab_wfa:
        render_walk_forward_tab(active_cfg)

    with tab_perm:
        render_permutation_tab(active_cfg, metrics)

    with tab_compare:
        render_comparison_tab(active_cfg)


if __name__ == "__main__":
    main()

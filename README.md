# Algo Backtester

An event-driven algorithmic trading backtester with Walk-Forward Analysis and statistical significance testing.

## Architecture

```
Data Layer          Engine Layer         Analytics Layer      API / Dashboard
───────────         ────────────         ───────────────      ───────────────
DataFetcher ──→  BacktestEngine       WalkForwardAnalyzer   FastAPI REST API
  YFinance        event queue:          parameter sweep       Streamlit UI
  AlphaVantage    MarketEvent     ──→  PermutationTester     HTML Report
DataStore         SignalEvent          compute_all_metrics
  parquet cache   OrderEvent
                  FillEvent
                  SimulatedBroker
                  PositionSizer
```

## Features

- **Event-driven engine** — MarketEvent → SignalEvent → OrderEvent → FillEvent pipeline eliminates look-ahead bias; orders execute at next bar's open
- **3 strategies** — MA Crossover, RSI Mean Reversion (Wilder's incremental RSI), Breakout with STOP entry orders
- **3 position sizers** — Fixed Quantity, Fixed Fraction, Kelly Criterion
- **Walk-Forward Analysis** — rolling in-sample optimization + out-of-sample evaluation with datetime-indexed window results
- **Permutation Test** — Monte Carlo statistical significance (p = (count_gte + 1) / (n + 1))
- **Parquet caching** — `DataStore` caches OHLCV data locally; no duplicate network calls
- **SQLite persistence** — SQLAlchemy 2.0 ORM; Alembic migrations for schema evolution
- **Reviewed direct-SQL analytics** — packaged, named-bind queries for run comparison, trade and equity audits, cohort summaries, integrity validation, and reproducible SQLite evidence; see [SQL analytics verification](docs/sql-analytics.md)
- **FastAPI REST API** — async background jobs for permutation tests, Pydantic v2 validation
- **Streamlit dashboard** — interactive KPI cards, equity curves, comparison tab
- **Standalone HTML reports** — embedded Plotly charts, no CDN dependency

## Quickstart

```bash
# 1. Clone and install
git clone <repo-url> && cd algo-backtester
pip install -e ".[dev]"

# 2. Run tests
pytest --tb=short

# 3. Run the API server
uvicorn src.api.main:app --reload

# 4. Run the dashboard
streamlit run src/dashboard/app.py

# 5. Apply DB migrations
alembic upgrade head
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| API | FastAPI + Uvicorn |
| Dashboard | Streamlit + Plotly |
| Database | SQLite via SQLAlchemy 2.0 |
| Migrations | Alembic |
| Data | yfinance, Alpha Vantage, pandas |
| Testing | pytest |
| Type checking | mypy --strict |

## Project Structure

```
algo-backtester/
├── src/
│   ├── analytics/          # metrics, walk-forward, permutation, reports
│   ├── api/                # FastAPI app, routes, schemas
│   ├── dashboard/          # Streamlit UI
│   ├── data/               # fetchers, DataStore cache
│   ├── db/                 # SQLAlchemy tables, CRUD, Alembic migrations
│   ├── engine/             # BacktestEngine, SimulatedBroker, events, position sizers
│   ├── models/             # Candle, Order, Portfolio, Trade
│   └── strategies/         # BaseStrategy, MA Crossover, RSI, Breakout
├── tests/                  # pytest test suite
├── alembic/                # DB migration scripts
├── notebooks/              # Research notebooks
└── docs/                   # Architecture docs
```

## Design Decisions

### ADR-001: Event-Driven Architecture
Chose event-driven over vectorized backtesting for realistic order lifecycle modeling (MARKET/LIMIT/STOP). The `deque`-based event queue enables GTC order persistence across bars with look-ahead bias prevention (signals at bar N fill at bar N+1 open).

### ADR-002: Wilder's Incremental RSI
RSI uses O(1) incremental Wilder smoothing (`avg = (prev*(period-1) + change) / period`) rather than rolling window recalculation, matching how RSI is computed in live trading systems.

### ADR-003: Statistically Correct p-values
Permutation test uses `p = (count_gte + 1) / (n + 1)` (Monte Carlo standard) to avoid p=0 for finite samples — consistent with Phipson & Smyth (2010).

### ADR-004: Deferred Fill Execution
All order types (MARKET, LIMIT, STOP) are added to a pending queue and executed at the next bar's open. This eliminates look-ahead bias and correctly models real execution latency.

### ADR-005: Parquet Caching
`DataStore` uses exact (symbol, start, end) matching for cache keys. Cache is stored in `data/raw/` (gitignored). Trade-off: no partial-range cache hits; full re-fetch if date range changes.

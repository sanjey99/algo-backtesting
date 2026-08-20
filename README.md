# Algo Backtester

An event-driven algorithmic trading backtester with Walk-Forward Analysis and statistical significance testing.

## Architecture

```
Data Layer             Engine Layer         Analytics Layer      Entry Points
──────────             ────────────         ───────────────      ────────────
AcquisitionService ──→ BacktestEngine       WalkForwardAnalyzer   FastAPI REST API
  YFinanceProvider      event queue:          parameter sweep       Streamlit UI
  AlphaVantageProvider  MarketEvent     ──→  PermutationTester     HTML Report
DataStore generations   SignalEvent          compute_all_metrics
  canonical parquet     OrderEvent
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

# 3. Migrate the default SQLite database before application startup
python -m src.db.migrate --database data/backtester.db

# 4. Run the API server
uvicorn src.api.main:app --reload

# 5. Run the dashboard
streamlit run src/dashboard/app.py
```

The migration command above classifies existing SQLite schemas and safely handles fresh,
unversioned-baseline, and versioned databases. Use plain `alembic upgrade head` only when Alembic
is already configured to target the intended fresh or correctly versioned database; it does not
provide the classifier's safeguards for an unversioned legacy schema.

## Verification

```bash
make test
make lint
make verify-warnings
```

`make verify-warnings` is a compatibility release gate: it treats Starlette transport deprecations
and naive-UTC persistence deprecations as errors in their affected test scopes.

## Structured operational logs

The API lifespan, data CLI, and Streamlit dashboard each configure one process-wide JSON logging
handler at their runtime boundary. Library imports do not configure logging. The default level is
`INFO`; use `LOG_LEVEL=DEBUG` to include diagnostic events:

```bash
LOG_LEVEL=DEBUG uvicorn src.api.main:app --reload
LOG_LEVEL=DEBUG python -m src.data.cli benchmark --output artifacts/data-quality-benchmark.json
LOG_LEVEL=DEBUG streamlit run src/dashboard/app.py
```

Each log line is a JSON object written by Python's default `StreamHandler` to stderr. CLI command
results remain machine-readable JSON on stdout, so callers can process output independently from
operational logs. Records include a UTC timestamp with a `Z` suffix, severity, logger, and a
stable dot-separated lowercase event name (for example, `backtest.started` or
`acquisition.cache_result`). Event names and fields are intended for operational queries rather
than free-form messages.

Sensitive fields are always emitted as `[REDACTED]` when their keys are `api_key`, `authorization`,
`token`, `password`, `secret`, or `cookie` (case-insensitive). Credential-bearing URLs are also
redacted, including URLs with user-info credentials or any of those keys in a query string.

## Execution semantics

Strategy decisions are made from a completed daily bar and execute no earlier than the next
bar's open. LIMIT and STOP orders are good-til-cancelled (GTC): unfilled conditional orders remain
pending until they fill, are replaced, or the data ends. LIMIT fills are price-protected and never
execute worse than their stated limit. STOP fills carry gap risk, because a gap through the stop
uses the next open (with adverse slippage).

Short positions use the Global Constraints defaults: initial margin `1.50`, maintenance margin
`0.30`, annual borrow rate `0.03`, and borrow day count `365.0`.

## Market data acquisition quality

The daily acquisition boundary normalizes provider data, applies XNYS-session-aware quality rules,
uses immutable cache generations, and produces redacted manifests for both successes and admitted
failures. The API, CLI, and dashboard share that service.

```bash
# Write canonical Parquet plus a redacted request report.
python -m src.data.cli acquire --symbol SPY --start 2020-01-01 --end 2024-12-31 \
  --canonical artifacts/spy.parquet --report artifacts/spy-report.json

# Inspect an archived report or run the fully offline verification benchmark.
python -m src.data.cli inspect --acquisition-id <acquisition-id>
python -m src.data.cli benchmark --output artifacts/data-quality-benchmark.json
```

The benchmark uses generated provider-shaped payloads, fresh fixtures per sample, and no network
requests. Treat its output as reproducible local evidence only; do not commit generated artifacts
or use deterministic timings as claims about live providers. See
[the data acquisition quality guide](docs/data-acquisition-quality.md) for provider limitations,
contracts, manifests, benchmark methodology, and claim discipline.

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
`DataStore` publishes immutable canonical generations under contract/calendar/interval/symbol namespaces. Requests can reuse valid partial ranges and fetch only missing or stale exchange sessions; cache artifacts remain under `data/raw/` (gitignored).

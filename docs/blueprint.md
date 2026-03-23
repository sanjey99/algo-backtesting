# Blueprint: Algorithmic Trading Backtesting Platform

## Objective

Build a production-grade, event-driven backtesting engine in Python that lets a user define trading strategies, run them against historical market data, and evaluate performance with institutional-quality risk metrics. The platform differentiates itself from tutorial-level projects by including **permutation testing for overfitting detection**, **walk-forward analysis with periodic re-optimization**, and **Kelly Criterion position sizing** -- features that directly map to quant workflows at Goldman Sachs, JP Morgan, and Morgan Stanley. A FastAPI backend exposes all functionality as a REST API, and a Streamlit dashboard provides interactive visualization.

**Target audience for the resume line:** Quant dev / fintech analyst recruiters scanning for evidence of statistical rigor, OOP design discipline, and real understanding of market microstructure pitfalls (survivorship bias, slippage, corporate actions).

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | Python 3.11+ | Industry standard for quant finance |
| Core Libraries | pandas 2.x, numpy, scipy | Vectorized data manipulation, statistical tests |
| Backtesting Engine | Custom (event-driven OOP) | Demonstrates design patterns; no black-box libraries |
| API | FastAPI 0.110+ | Async, auto-generated OpenAPI docs, type-safe |
| Database | SQLite (dev), PostgreSQL (prod) | SQLAlchemy ORM abstracts both; SQLite for zero-setup |
| Data Source | yfinance (primary), Alpha Vantage (fallback) | Free, covers equities, ETFs |
| Visualization | Plotly (interactive), matplotlib (static exports) | Plotly for dashboard, matplotlib for PDF reports |
| Frontend | Streamlit 1.30+ | Rapid prototyping, no JS required |
| Testing | pytest, pytest-cov | Unit + integration coverage |
| Type Checking | mypy (strict mode) | Signals production-grade discipline |
| Linting | ruff | Fast, replaces flake8+isort+black |

## Repository Structure

```
algo-backtester/
├── README.md
├── pyproject.toml
├── .env.example
├── alembic/
│   └── versions/
├── data/
│   ├── raw/
│   └── adjusted/
├── src/
│   ├── models/
│   │   ├── candle.py
│   │   ├── trade.py
│   │   ├── order.py
│   │   └── portfolio.py
│   ├── data/
│   │   ├── fetcher.py
│   │   ├── adjustments.py
│   │   └── store.py
│   ├── engine/
│   │   ├── backtest.py
│   │   ├── event.py
│   │   ├── broker.py
│   │   └── position_sizer.py
│   ├── strategies/
│   │   ├── base.py
│   │   ├── ma_crossover.py
│   │   ├── rsi_mean_reversion.py
│   │   └── breakout.py
│   ├── analytics/
│   │   ├── metrics.py
│   │   ├── walk_forward.py
│   │   ├── permutation_test.py
│   │   └── report.py
│   ├── api/
│   │   ├── main.py
│   │   ├── schemas.py
│   │   ├── deps.py
│   │   └── routes/
│   │       ├── backtest.py
│   │       ├── strategies.py
│   │       └── data.py
│   ├── db/
│   │   ├── database.py
│   │   ├── tables.py
│   │   └── crud.py
│   └── dashboard/
│       └── app.py
├── tests/
└── notebooks/
```

## Database Schema

```sql
CREATE TABLE backtest_runs (
    id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL, symbol TEXT NOT NULL,
    start_date DATE NOT NULL, end_date DATE NOT NULL, params_json TEXT NOT NULL,
    initial_capital REAL NOT NULL DEFAULT 100000.0, commission_pct REAL NOT NULL DEFAULT 0.001,
    slippage_pct REAL NOT NULL DEFAULT 0.0005, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, backtest_id TEXT NOT NULL REFERENCES backtest_runs(id),
    entry_date TIMESTAMP NOT NULL, exit_date TIMESTAMP, direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    entry_price REAL NOT NULL, exit_price REAL, quantity INTEGER NOT NULL,
    pnl REAL, pnl_pct REAL, commission REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT, backtest_id TEXT NOT NULL REFERENCES backtest_runs(id),
    date TIMESTAMP NOT NULL, equity REAL NOT NULL, drawdown_pct REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, backtest_id TEXT NOT NULL REFERENCES backtest_runs(id),
    metric_name TEXT NOT NULL, metric_value REAL NOT NULL
);
```

## Implementation Steps

### Step 1: Project Skeleton and Domain Models
**Context:** Nothing exists yet. Start with core data classes every other module depends on.
**Files:** `pyproject.toml`, `src/models/candle.py`, `src/models/trade.py`, `src/models/order.py`, `src/models/portfolio.py`, `tests/conftest.py`, `tests/test_models.py`

Key classes:
- `Candle` — frozen dataclass with `timestamp, open, high, low, close, volume, adj_close`
- `OrderType` enum (MARKET, LIMIT, STOP), `Direction` enum (LONG, SHORT), `Order` dataclass
- `Trade` dataclass with `pnl`, `pnl_pct`, `is_closed` properties
- `Portfolio` class with `update()`, `record_fill()`, `equity`, `equity_curve`, `open_positions`

**Verification:** `pytest tests/test_models.py -v && mypy src/models/ --strict`
**Exit criteria:** All model tests pass. Zero mypy errors.

---

### Step 2: Data Ingestion Layer
**Context:** Domain models exist. Need to fetch, cache, adjust, and serve historical price data.
**Files:** `src/data/fetcher.py`, `src/data/adjustments.py`, `src/data/store.py`, `.env.example`

Key components:
- `DataFetcher` ABC with `fetch(symbol, start, end, interval)` and `available_symbols()`
- `YFinanceFetcher(DataFetcher)` — primary source, returns DataFrame with `[timestamp, open, high, low, close, volume, adj_close]`
- `AlphaVantageFetcher(DataFetcher)` — fallback, reads `ALPHA_VANTAGE_API_KEY` from env, rate-limited to 5 calls/min
- `DataStore` — caches to parquet at `data/raw/{symbol}_{start}_{end}.parquet`
- `adjust_for_splits()`, `adjust_for_dividends()` in `adjustments.py`
- `validate_adjustment()` — flags suspicious >20% single-day gaps

**Verification:** `python -c "from src.data.fetcher import YFinanceFetcher; ..."`
**Exit criteria:** AAPL data fetches. Parquet cache appears. Adjustment validation works.

---

### Step 3: Event System and Backtest Engine
**Context:** Models and data layer exist. Core event-driven engine.
**Files:** `src/engine/event.py`, `src/engine/broker.py`, `src/engine/backtest.py`, `tests/test_engine.py`

Key components:
- Event hierarchy: `Event(ABC)` → `MarketEvent`, `SignalEvent`, `OrderEvent`, `FillEvent`
- `SimulatedBroker`: slippage = `price * (1 ± slippage_pct)`, commission = `fill_price * qty * commission_pct`
- LIMIT orders fill only if price crosses limit; STOP orders fill if price crosses stop level
- `BacktestEngine.run()` — iterates bar-by-bar, emits events through `strategy → broker → portfolio`
- `BacktestResult` dataclass with `trades`, `equity_curve`, `parameters`, `final_equity`

**Verification:** `pytest tests/test_engine.py -v`
**Exit criteria:** Engine runs trivial "buy day 1, sell day 50" strategy. PnL matches expected value with slippage + commission.

---

### Step 4: Strategy Framework and Three Concrete Strategies
**Context:** Engine runs but has no real strategies.
**Files:** `src/strategies/base.py`, `src/strategies/ma_crossover.py`, `src/strategies/rsi_mean_reversion.py`, `src/strategies/breakout.py`, `tests/test_strategies.py`

Key design:
- `BaseStrategy(ABC)` with `name`, `parameters`, `parameter_space` (ranges for walk-forward optimizer), `on_candle()`, `generate_orders()`, `reset()`
- `MACrossoverStrategy` — fast/slow SMA or EMA crossover, handles warmup period
- `RSIMeanReversionStrategy` — RSI from scratch (no TA-Lib), LONG when RSI < oversold, exit when > 50
- `BreakoutStrategy` — Donchian channel, LONG when close > highest high of last N bars
- `STRATEGY_REGISTRY` dict in `__init__.py`

**Verification:** `pytest tests/test_strategies.py -v` + SPY smoke test
**Exit criteria:** All 3 strategies produce plausible signals. MA crossover on SPY 2020-2023 generates 10-100 trades.

---

### Step 5: Risk Metrics and Performance Analytics
**Context:** Engine runs strategies and produces trades + equity curve.
**Files:** `src/analytics/metrics.py`, `tests/test_metrics.py`

Metrics (all using bar-by-bar returns, not trade-by-trade):
- `sharpe_ratio(returns, risk_free_rate=0.04, periods_per_year=252)`
- `cagr(equity_curve, periods_per_year=252)`
- `max_drawdown(equity_curve)` — peak-to-trough as negative %
- `max_drawdown_duration(equity_curve)` — longest drawdown in trading days
- `win_rate(trades)`, `profit_factor(trades)`, `calmar_ratio()`, `sortino_ratio()`
- `compute_all_metrics(result: BacktestResult) -> dict[str, float]`

**Verification:** `pytest tests/test_metrics.py -v`
**Exit criteria:** `[100,110,90,95,80,100]` equity gives max_drawdown = -27.27%. All metrics handle empty input gracefully.

---

### Step 6: Position Sizing Module
**Context:** Strategies generate signals but position size is hardcoded.
**Files:** `src/engine/position_sizer.py`, updates to `src/engine/backtest.py`

- `PositionSizer(ABC)` with `calculate(signal, portfolio, current_price) -> int`
- `FixedFractionSizer(fraction=0.02)` — risk % of equity per trade
- `KellyCriterionSizer(lookback=20, fraction=0.5, max_pct=0.25)` — falls back to FixedFraction until enough trade history; capped at `max_pct` of equity
- `FixedQuantitySizer(quantity=100)` — always trades fixed shares

**Verification:** `pytest tests/test_broker.py -v`
**Exit criteria:** Kelly produces larger sizes after win streaks. Never exceeds portfolio equity.

---

### Step 7: Walk-Forward Analysis ⭐ KEY DIFFERENTIATOR
**Context:** Single backtest works. WFA splits data into rolling train/test windows.
**Files:** `src/analytics/walk_forward.py`, `tests/test_walk_forward.py`

- `WalkForwardAnalyzer(strategy_cls, data, in_sample_days=252, out_of_sample_days=63, step_days=63, optimization_metric="sharpe_ratio", n_optimization_trials=50)`
- `_optimize_window(train_data)` — random search over `strategy.parameter_space`, returns best params
- `WalkForwardResult` — list of `WindowResult` + combined equity curve (out-of-sample only) + `optimization_stability` (std/mean of metric across windows)
- Combined equity curve chains only out-of-sample segments — realistic live performance estimate

**Verification:** `pytest tests/test_walk_forward.py -v` + SPY 2015-2024 integration test
**Exit criteria:** At least 3 windows on 10yr SPY. Combined out-of-sample Sharpe reported. Parameter stability metric computed.

---

### Step 8: Permutation Testing ⭐ STRONGEST DIFFERENTIATOR
**Context:** WFA gives realistic OOS estimates. Permutation testing answers "could random data achieve this?"
**Files:** `src/analytics/permutation_test.py`, `tests/test_permutation.py`

- `PermutationTester(strategy, data, n_permutations=1000, metric="sharpe_ratio", seed=42)`
- `_permute_returns(df, rng)` — shuffles daily log-returns, reconstructs price series (preserves distribution, destroys temporal structure)
- `PermutationResult` — `actual_metric`, `permuted_metrics`, `p_value` (fraction permuted >= actual), `is_significant` (p < 0.05), `percentile`
- Parallelized with `ProcessPoolExecutor(max_workers=cpu_count()-1)`

**Verification:** `pytest tests/test_permutation.py -v --timeout=120`
**Exit criteria:** Random strategy correctly not significant (p > 0.05). Cheating strategy (future data) correctly significant. Parallelization gives 2x+ speedup.

---

### Step 9: Database Layer
**Context:** All computation works in memory. Add persistence.
**Files:** `src/db/database.py`, `src/db/tables.py`, `src/db/crud.py`

- SQLAlchemy 2.0 mapped_column style
- `init_db()`, `get_session()` using `sqlite:///data/backtester.db`
- CRUD: `save_backtest_run()` (UUID, saves run + trades + equity curve + metrics in single transaction), `get_backtest_run()`, `list_backtest_runs()`, `get_trades()`, `get_equity_curve()`, `get_metrics()`
- Add optional `persist=True` to `BacktestEngine.run()`

**Verification:** `python -c "from src.db.database import init_db; init_db()"` + round-trip test
**Exit criteria:** DB file created. Save + load produces identical metrics.

---

### Step 10: FastAPI Backend
**Context:** All business logic works. Expose as REST API.
**Files:** `src/api/main.py`, `src/api/schemas.py`, `src/api/routes/backtest.py`, `src/api/routes/strategies.py`, `src/api/routes/data.py`, `tests/test_api.py`

Endpoints:
- `POST /api/backtest` → run + persist → `BacktestResponse`
- `GET /api/backtest/{run_id}` → retrieve saved run
- `GET /api/backtest/{run_id}/trades`
- `GET /api/backtest/{run_id}/equity-curve`
- `POST /api/backtest/walk-forward`
- `POST /api/backtest/permutation-test` → `202 Accepted` + background task (long-running)
- `GET /api/strategies` → list with `parameter_space`
- `POST /api/data/fetch`

**Verification:** `pytest tests/test_api.py -v` + `curl POST /api/backtest`
**Exit criteria:** All API tests pass. OpenAPI docs at `/docs`. Full round-trip via API.

---

### Step 11: Streamlit Dashboard
**Context:** API complete. Build visual dashboard.
**Files:** `src/dashboard/app.py`

Sections:
1. Sidebar: strategy/symbol/date/params/capital/commission/slippage configurator
2. Metrics: KPI cards (Sharpe, CAGR, MaxDrawdown, WinRate, ProfitFactor, TotalReturn)
3. Equity curve: Plotly line + drawdown overlay (shaded)
4. Trade table: entry/exit/direction/prices/PnL
5. Monthly returns heatmap
6. Walk-Forward tab: window results + combined OOS equity + parameter stability chart
7. Permutation Testing tab: histogram of permuted metrics + vertical line (actual) + p-value (green < 0.05, red >= 0.05)
8. Strategy comparison: overlaid equity curves + metrics table

**Verification:** `streamlit run src/dashboard/app.py` — manual check all 5 sections render
**Exit criteria:** All visualization sections render with real SPY data.

---

### Step 12: Polish and Documentation
**Files:** `src/analytics/report.py`, `README.md`, `notebooks/01_data_exploration.ipynb`, `notebooks/02_strategy_research.ipynb`

- `ReportGenerator` — standalone HTML with embedded Plotly charts (for interview demos)
- README: architecture diagram, feature list, 5-command quickstart, design decisions section
- Notebook 1: SPY candlestick chart, volume analysis, return distribution
- Notebook 2: Full strategy development journey (hypothesis → backtest → WFA → permutation test)
- `Makefile` with `test`, `lint`, `serve`, `dashboard`, `report` targets

**Final checks:**
```bash
make lint    # zero errors (mypy strict + ruff)
make test    # all pass, >80% coverage
make serve   # API starts
make dashboard  # dashboard starts
```

---

## Dependency Graph

```
Step 1 (Models)
  ├── Step 2 (Data) ──────────────┐
  └── Step 3 (Engine) ────────┐  │
                               ▼  ▼
                          Step 4 (Strategies)
                    ┌──────────┼──────────┐
                    ▼          ▼          ▼
             Step 5 (Metrics) Step 6 (Sizers) Step 9 (DB)
                    │          │               │
                    ▼          │               │
           Step 7 (WFA) ◄──────┘               │
           Step 8 (Permutation)                │
                    └──────────────────────────┘
                                   ▼
                           Step 10 (API)
                                   ▼
                          Step 11 (Dashboard)
                                   ▼
                           Step 12 (Polish)
```

**Parallelizable:** Steps 2+3, Steps 5+6+9, Steps 7+8+9

## Rollback Strategy

Before each step: copy `src/` to `src_backup_stepN/`. After every step: run `pytest tests/ -v`. If a previously-passing test fails, the step introduced a regression — fix before proceeding. Alternatively: `git init && git commit` after each step for free rollback.

## Exit Criteria (Project Complete)

- [ ] All 12 steps pass verification
- [ ] `pytest tests/ --cov=src` > 80% line coverage
- [ ] `mypy src/ --strict` zero errors
- [ ] `ruff check src/ tests/` zero violations
- [ ] FastAPI `/docs` renders all endpoints
- [ ] Streamlit dashboard renders all 5 sections with real SPY data
- [ ] Walk-forward on SPY 2015-2024 reports combined OOS metrics
- [ ] Permutation test rejects random strategy (p > 0.05) on 100 permutations
- [ ] HTML report generates self-contained file
- [ ] README has architecture diagram, feature list, quickstart, design decisions
- [ ] 5-minute interview walkthrough: problem → architecture → key insight (permutation testing) → results

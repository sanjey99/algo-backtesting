# System Design: Algorithmic Trading Backtesting Platform

> **Author:** Sanje | **Date:** 2026-03-21 | **Status:** Draft
> **Target:** Quant Dev / Tech Analyst roles at Goldman Sachs, JP Morgan, Morgan Stanley

---

## 1. Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           STREAMLIT DASHBOARD                          │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌────────┐ ┌──────────────┐  │
│  │ KPI Cards│ │ Equity   │ │ Trade     │ │ Heatmap│ │ Permutation  │  │
│  │ (Sharpe, │ │ Curve +  │ │ Table     │ │ Monthly│ │ Histogram +  │  │
│  │ CAGR,    │ │ Drawdown │ │ (entries, │ │ Returns│ │ WFA Results  │  │
│  │ MaxDD)   │ │ Overlay  │ │  exits)   │ │        │ │              │  │
│  └──────────┘ └──────────┘ └───────────┘ └────────┘ └──────────────┘  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ HTTP (localhost:8000)
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         FASTAPI REST API                                │
│  POST /api/backtest          GET /api/strategies                        │
│  GET  /api/backtest/{id}     POST /api/data/fetch                       │
│  GET  /api/backtest/{id}/trades                                         │
│  GET  /api/backtest/{id}/equity-curve                                   │
│  POST /api/backtest/walk-forward                                        │
│  POST /api/backtest/permutation-test  (202 Accepted, background task)   │
└───────────┬──────────────────────────────────────────────────────────────┘
            │ function calls (in-process)
            ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                        BACKTEST ENGINE (Orchestrator)                     │
│                                                                           │
│  BacktestEngine.run(strategy, data, config)                               │
│    │                                                                      │
│    │  ┌─────────────┐    ┌─────────────┐    ┌──────────────┐              │
│    ├──► DataFetcher  │    │  Strategy    │    │ PositionSizer│              │
│    │  │ (YFinance /  │    │ (MA Cross /  │    │ (Kelly /     │              │
│    │  │  AlphaVant.) │    │  RSI / Brkot)│    │  FixedFrac)  │              │
│    │  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘              │
│    │         │                   │                    │                     │
│    │         ▼                   ▼                    ▼                     │
│    │  ┌──────────────────────────────────────────────────────┐              │
│    │  │              EVENT QUEUE (in-memory deque)           │              │
│    │  │  MarketEvent → SignalEvent → OrderEvent → FillEvent  │              │
│    │  └──────────────────────────┬───────────────────────────┘              │
│    │                             │                                          │
│    │                             ▼                                          │
│    │                    ┌────────────────┐                                  │
│    │                    │ SimulatedBroker │                                  │
│    │                    │ (slippage,      │                                  │
│    │                    │  commission,    │                                  │
│    │                    │  order types)   │                                  │
│    │                    └───────┬────────┘                                  │
│    │                            │ FillEvent                                 │
│    │                            ▼                                           │
│    │                    ┌──────────────┐                                    │
│    │                    │  Portfolio    │                                    │
│    │                    │ (positions,   │                                    │
│    │                    │  equity curve)│                                    │
│    │                    └──────┬───────┘                                    │
│    │                           │                                            │
│    ▼                           ▼                                            │
│  BacktestResult { trades, equity_curve, params, final_equity }             │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                         ANALYTICS LAYER                                   │
│                                                                           │
│  ┌────────────┐   ┌────────────────────┐   ┌────────────────────┐        │
│  │  Metrics   │   │  WalkForward       │   │ PermutationTester  │        │
│  │  (Sharpe,  │   │  Analyzer          │   │ (1000 shuffles,    │        │
│  │   CAGR,    │   │  (rolling train/   │   │  p-value,          │        │
│  │   MaxDD,   │   │   test windows,    │   │  ProcessPool       │        │
│  │   Sortino, │   │   random search    │   │  parallelism)      │        │
│  │   Calmar,  │   │   optimization)    │   │                    │        │
│  │   PF, WR)  │   │                    │   │                    │        │
│  └─────┬──────┘   └────────┬───────────┘   └─────────┬──────────┘        │
└────────┼───────────────────┼─────────────────────────┼────────────────────┘
         └───────────────────┼─────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                    DATABASE (SQLAlchemy 2.0 ORM)                          │
│                                                                           │
│  ┌──────────────┐  ┌────────┐  ┌──────────────┐  ┌─────────┐            │
│  │ backtest_runs│  │ trades │  │ equity_curve │  │ metrics │            │
│  │ (UUID, strat,│  │ (entry,│  │ (date,       │  │ (name,  │            │
│  │  symbol,     │  │  exit, │  │  equity,     │  │  value) │            │
│  │  params_json)│  │  PnL)  │  │  drawdown)   │  │         │            │
│  └──────────────┘  └────────┘  └──────────────┘  └─────────┘            │
│                                                                           │
│  DEV:  sqlite:///data/backtester.db                                       │
│  PROD: postgresql://user:pass@host:5432/backtester                        │
└───────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Key Abstraction |
|-----------|---------------|-----------------|
| **DataFetcher** | Fetch, cache (parquet), and adjust historical OHLCV data | `DataFetcher` ABC with `YFinanceFetcher` and `AlphaVantageFetcher` |
| **BacktestEngine** | Orchestrate bar-by-bar event loop; wire strategy, broker, portfolio | `BacktestEngine.run()` returns `BacktestResult` |
| **Strategy** | Consume a candle plus immutable execution context and emit `SignalEvent` based on indicator logic | `BaseStrategy` ABC with `on_candle(candle, context)` and `parameter_space` |
| **SimulatedBroker** | Convert `OrderEvent` to `FillEvent` with realistic slippage and commission | Models MARKET, LIMIT, STOP order types |
| **Portfolio** | Track positions, cash, equity; record fills | `Portfolio.update()` and `record_fill()` |
| **PositionSizer** | Determine trade size from signal, portfolio state, and price | `PositionSizer` ABC with Kelly, FixedFraction, FixedQuantity |
| **Analytics** | Compute risk metrics, walk-forward analysis, permutation testing | Pure functions on `BacktestResult` |
| **API** | REST interface to all engine and analytics functionality | FastAPI with Pydantic schemas |
| **DB** | Persist and retrieve backtest runs, trades, equity curves, metrics | SQLAlchemy 2.0 mapped_column, CRUD layer |
| **Dashboard** | Interactive visualization of results | Streamlit + Plotly |

---

## 2. Data Flow

### Single Backtest Request: Step-by-Step

```
Client (Dashboard / curl)
  │
  │  POST /api/backtest
  │  { strategy: "ma_crossover", symbol: "SPY", ... }
  │
  ▼
Step 1: API Layer — validate BacktestRequest schema, resolve strategy class
Step 2: DataFetcher — check parquet cache; if miss, fetch + adjust + validate + cache
Step 3: BacktestEngine.run() — initialize Portfolio, Strategy, Broker, PositionSizer
Step 4: Bar-by-bar Event Loop
  ├── 4a. Accrue short borrow from the previous mark to this candle
  ├── 4b. Evaluate orders pending before this candle against current OHLC
  │        MARKET: next eligible open with adverse slippage
  │        LIMIT/STOP: apply gap rules; retain untriggered orders as GTC
  ├── 4c. Portfolio.record_fill(fill) for accepted fills; remove rejected orders
  ├── 4d. Portfolio.update(close) — mark-to-market, append equity curve row
  ├── 4e. Evaluate maintenance margin; queue a forced next-open cover on breach
  ├── 4f. Strategy.on_candle(candle, context) → SignalEvent (or nothing)
  └── 4g. Size and enqueue the signal's order for execution on a later bar
Step 5: Engine returns BacktestResult { trades, equity_curve, final_equity }
Step 6: Analytics — compute_all_metrics(result) → dict[str, float]
Step 7: DB — save_backtest_run() in single transaction (runs + trades + curve + metrics)
Step 8: API Response — BacktestResponse with run_id, metrics, sub-resource links
```

A signal generated on bar N cannot execute on bar N. Its first eligible execution is a later bar;
a MARKET order uses that bar's open. LIMIT and STOP orders persist until filled, replaced, cancelled
by a conflicting signal, superseded by a forced cover, or cancelled at end of data. A forced margin
cover has priority over strategy orders for the same symbol. Custom strategies must migrate to the
`on_candle(candle, context)` signature and use the supplied immutable `StrategyContext` rather than
maintaining fill assumptions from signal time.

### Walk-Forward Flow (abbreviated)

```
WalkForwardAnalyzer splits data into rolling windows:
  Window 1: train [2015-2016] → random search 50 combos → test [2016-Q1]
  Window 2: train [2015.25-2016.25] → optimize → test [2016.25-Q1]
  ...
  Window N: train [2022-2023] → optimize → test [2023-Q1..Q2]

Chain out-of-sample equity curves → combined OOS performance
optimization_stability = std(metric) / mean(metric) across windows
```

### Permutation Test Flow (abbreviated)

```
1. Run strategy on real data → actual_sharpe = 1.24
2. For i in 1..1000 (parallelized across CPU cores via ProcessPoolExecutor):
     - Shuffle daily log-returns, reconstruct synthetic price series
     - Run same strategy on synthetic data → permuted_sharpe[i]
3. p_value = count(permuted_sharpe >= actual_sharpe) / 1000
4. p_value < 0.05 → result is statistically significant
```

---

## 3. Key Design Decisions

### ADR-001: Event-Driven Architecture over Vectorized Backtesting

**Decision:** Event-driven as primary architecture.

**Rationale:**
- **Execution realism.** Vectorized backtesting applies signals retroactively across entire columns — impossible to model path-dependent behavior: stop-losses that trigger mid-bar, position sizing that depends on current equity, or limit orders that only fill when price crosses a threshold.
- **Live-backtest parity.** The only difference between backtest and live is swapping `SimulatedBroker` for a `LiveBroker`. Strategy, PositionSizer, Portfolio remain identical. This eliminates an entire class of bugs where strategies pass backtest but fail live.
- **Extensibility.** New order types, new event types, or new portfolio constraints can be added by extending the event hierarchy without modifying existing code.

**Trade-off:** Event-driven is 10-100x slower than vectorized. Mitigation: add a `vectorized_signal()` fast-screening path on `BaseStrategy` for initial parameter sweeps.

---

### ADR-002: Random Search over Grid Search for Walk-Forward Optimization

**Decision:** Random search with 50 trials per window.

**Rationale:**
- Bergstra & Bengio (2012) proved random search finds near-optimal hyperparameters in fewer evaluations when only a few parameters dominate performance — which is typical.
- Grid search with a 3-parameter strategy (20×20×10 = 4,000 combos) across 10 windows = 40,000 backtests. Random search: always 50 trials × 10 windows = 500.
- Random search's incomplete coverage acts as implicit regularization, reducing chance of overfitting to the in-sample window.
- Adding a third strategy parameter does not change compute cost of random search.

---

### ADR-003: Bar-by-Bar Returns over Trade-by-Trade for Sharpe

**Decision:** Bar-by-bar daily returns from equity curve.

**Rationale:**
- A strategy trading 12 times/year gives only 12 trade-by-trade data points. Standard error of Sharpe is `sqrt((1 + 0.5S²)/N)` — 12 observations yield a confidence interval too wide to be meaningful.
- Bar-by-bar Sharpe naturally penalizes time out-of-market (idle capital has opportunity cost). Trade-by-trade ignores this.
- Directly comparable to how prime brokers and benchmark indices report Sharpe ratios.
- Annualization `sharpe_daily * sqrt(252)` is well-defined; trade-by-trade annualization requires assumptions about future trade frequency.

---

### ADR-004: Permutation Testing as Primary Overfitting Detection

**Decision:** 1000-permutation test as primary statistical validity check, supplementing WFA.

**Rationale:**
- Permutation testing answers precisely: "What is P(strategy-with-no-edge achieves Sharpe ≥ actual | same return distribution)?"
- Shuffling daily log-returns preserves mean, variance, skewness, kurtosis but destroys all temporal structure (momentum, mean-reversion).
- Non-parametric — makes no distributional assumptions. T-tests on Sharpe ratios assume normality, which equity returns violate (fat tails).
- Train/test split is vulnerable to: the specific split point, regime differences, unconscious look-ahead in strategy development. Permutation testing is immune to all three.
- Walk-forward tests OOS robustness; permutation tests statistical significance. Together they address both overfitting and significance — a combination almost never seen in tutorial-level projects.

---

### ADR-005: SQLite for Development, PostgreSQL for Production

**Decision:** SQLAlchemy 2.0 ORM abstracting both backends via `DATABASE_URL` env var.

**Rationale:**
- SQLite: zero-setup dev, single file, no server. Removes the most common barrier to running/demoing the project.
- PostgreSQL: concurrent writes, proper transaction isolation, production-grade. Switch = one env var change.
- Four-table schema is fully portable — no database-specific features used.
- Same pattern used at banks — demonstrates production engineering familiarity.

---

## 4. API Contract

### Base URL: `http://localhost:8000/api`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/backtest` | Run a backtest, persist, return metrics |
| `GET` | `/backtest/{run_id}` | Retrieve a saved run |
| `GET` | `/backtest/{run_id}/trades` | Paginated trade list |
| `GET` | `/backtest/{run_id}/equity-curve` | Full equity curve as JSON |
| `POST` | `/backtest/walk-forward` | Run walk-forward analysis |
| `POST` | `/backtest/permutation-test` | Start permutation test (202 Accepted) |
| `GET` | `/backtest/permutation-test/{task_id}` | Poll permutation test result |
| `GET` | `/strategies` | List strategies with `parameter_space` |
| `POST` | `/data/fetch` | Fetch and cache market data |

**Key response schemas:**

```json
// POST /backtest → 200
{
  "run_id": "a1b2c3d4-...",
  "metrics": {
    "sharpe_ratio": 1.24, "cagr": 0.087, "max_drawdown": -0.1532,
    "max_drawdown_duration": 47, "win_rate": 0.58, "profit_factor": 1.73,
    "sortino_ratio": 1.61, "calmar_ratio": 0.567, "total_trades": 42
  },
  "final_equity": 118742.30,
  "links": { "trades": "/api/backtest/{id}/trades", "equity_curve": "..." }
}

// POST /backtest/permutation-test → 202
{ "task_id": "perm-xyz-...", "status": "running", "estimated_seconds": 15 }

// GET /backtest/permutation-test/{task_id} → 200 (complete)
{
  "actual_metric": 1.24,
  "permuted_metrics_summary": { "mean": 0.03, "std": 0.41, "percentile_95": 0.72 },
  "p_value": 0.012, "is_significant": true, "percentile": 98.8
}
```

---

## 5. Scalability Considerations

### Single-User → Multi-User

| Concern | Change |
|---------|--------|
| Database | SQLite → PostgreSQL with `pool_size=20` |
| Auth | JWT tokens via FastAPI `Depends()`; `user_id` FK on `backtest_runs` |
| Concurrent backtests | Celery task queue with Redis broker; `POST /backtest` returns 202 |
| Data storage | Local parquet → S3/GCS with symbol+date-range keys |
| Dashboard | Per-user Streamlit session state → React SPA for true multi-tenancy |

### Daily Bars → Tick Data

| Concern | Change |
|---------|--------|
| Volume | ~252 rows/yr (~5KB parquet) → ~50K ticks/day (~1.2GB/yr) |
| Storage | Partitioned parquet by date: `data/ticks/{symbol}/{YYYY-MM-DD}.parquet` |
| Engine | Streaming iterator — never loads full series; bar aggregation layer |
| Slippage model | Bid-ask spread model (fill longs at ask, shorts at bid) |
| Metrics | `periods_per_year` = 252×390 for 1-min data |

### One Strategy → Hundreds

| Concern | Change |
|---------|--------|
| Registry | Plugin auto-discovery via `importlib`/entry_points |
| Optimization | Bayesian search (Optuna) instead of random search |
| Comparison | `POST /backtest/batch`; Bonferroni p-value correction for multiple comparisons |
| Compute | Celery distributed across workers; one strategy per task |

### Architecture Evolution Path

```
Phase 1 (Now):      Monolith — single process, SQLite, local parquet
Phase 2 (10 users): PostgreSQL, Celery+Redis, S3 data
Phase 3 (100 users): Kubernetes, React SPA, WebSocket live updates
Phase 4 (Firm-wide): Kafka real-time data, GPU permutation testing (CuPy), RBAC
```

---

## 6. Interview Talking Points

### 6.1 Survivorship Bias

> "Yahoo Finance only provides data for companies that still exist today. If I backtest a stock-picking strategy on current S&P 500 constituents, I am implicitly excluding every company that went bankrupt, was delisted, or acquired at a loss. This creates systematic upward bias — potentially 1-2 percentage points annually, which is the difference between investable alpha and noise. My DataFetcher logs a warning when the symbol set matches a current index composition rather than a point-in-time composition. For production, I would use CRSP or Compustat survivorship-bias-free datasets."

### 6.2 Permutation Testing

> "A 1.5 Sharpe looks impressive, but the question is: could a strategy with zero predictive power achieve that Sharpe on data with the same statistical properties? My permutation tester shuffles daily log-returns 1,000 times. Shuffling preserves mean, variance, skewness, kurtosis — everything except temporal structure. If my strategy relies on genuine temporal patterns, it should rank in the top 5% of permuted results, giving p < 0.05. This is non-parametric — no normality assumption — which matters because equity returns have fat tails that parametric Sharpe tests systematically underestimate. I parallelized across CPU cores: 1,000 permutations takes about 12 seconds on a 4-core machine."

### 6.3 Execution Realism

> "Most academic backtests assume frictionless execution. My engine models three friction layers. Slippage: 5 basis points adverse price impact per trade, applied directionally. Commission: 10 basis points of notional. Execution timing: signals are queued, MARKET orders first execute at a later bar's open, and persistent LIMIT/STOP orders use explicit intrabar and gap rules. These compound significantly — a strategy showing 8% annual return frictionless might show 3% after costs, which fails to beat a 4% risk-free rate. The SimulatedBroker implements the same interface as a LiveBroker, so I can swap in a real exchange connection without changing any other code."

### 6.4 Live-Backtest Parity

> "The architecture decision I am most deliberate about is live-backtest parity. Event-driven processing means the backtest control flow is structurally identical to a live system: MarketEvent → Strategy → SignalEvent → PositionSizer → OrderEvent → Broker → FillEvent → Portfolio. In production, only `SimulatedBroker` changes to `LiveBroker`. Strategy, PositionSizer, and Portfolio run identical code. This eliminates an entire class of bugs — look-ahead bias, event ordering bugs, state management differences — that appear when backtest and live systems have fundamentally different architectures."

### 6.5 Why These Metrics Matter to Banks

> "The risk metrics I compute map directly to regulatory requirements. Maximum drawdown and drawdown duration are central to VaR reporting under Basel III/IV. Sharpe and Sortino are how portfolio managers justify capital allocation to risk committees. Calmar ratio (CAGR ÷ max drawdown) captures return-per-unit-of-tail-risk that compliance teams use to assess strategy acceptability. Profit factor is what desk heads check to evaluate whether a strategy has edge or is lucky. By computing these from bar-by-bar returns, my numbers are directly comparable to how prime brokers and risk systems report — they could be dropped into an actual risk report without recalculation."

---

## Appendix: Domain Model

```
Candle (frozen) → consumed by → Strategy → emits → SignalEvent
                                                         │
PositionSizer ←────────────────────────────────────────┘
     │ calculates quantity
     ▼
Order (MARKET/LIMIT/STOP) → executed by → SimulatedBroker → FillEvent
                                                                  │
                                                      Portfolio.record_fill()
                                                                  │
                                              Trade { entry/exit, pnl, commission }
                                                                  │
                                              equity_curve [ (date, equity, drawdown%) ]
```

---

*Derived from [[plans/blueprint|blueprint.md]] · 2026-03-21*

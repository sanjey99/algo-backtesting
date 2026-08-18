# Revisions: Gaps Between Design Docs and Implementation

> **Status (2026-08-18): Historical gap inventory, not an active sequential task list.**
> The descriptions below capture the pre-remediation implementation. In particular, the run-local
> event queue, integrated position sizing, and target-relative Sortino calculation are implemented.
> Use current code, tests, and `BLOCKERS.md` for readiness decisions; preserve this document as the
> provenance of earlier review findings.

---

## CRITICAL — Architecture Deviations

### R-01: Engine Does Not Use the Event Queue

**Spec (system-design §2):** The engine uses an in-memory deque event queue: `MarketEvent → SignalEvent → OrderEvent → FillEvent`. The event hierarchy exists in `src/engine/event.py`.

**Actual:** `BacktestEngine.run()` in `src/engine/backtest.py` calls `strategy.on_candle()` directly and never instantiates `MarketEvent`. The event queue (deque) does not exist. `OrderEvent` is created but only to immediately extract `.order` — it is never enqueued.

**Fix:**
1. Add an `_event_queue: deque[Event]` to `BacktestEngine`.
2. In the bar loop:
   - Create and enqueue `MarketEvent(symbol, candle)`.
   - Process `MarketEvent` → call `strategy.on_candle()` → if signal, enqueue `SignalEvent`.
   - Process `SignalEvent` → build order → enqueue `OrderEvent`.
   - Process `OrderEvent` → `broker.execute()` → if fill, enqueue `FillEvent`.
   - Process `FillEvent` → `portfolio.record_fill()`.
3. Add a `_process_events()` method that drains the queue per bar.
4. Update `tests/test_engine.py` to assert event flow (e.g., verify a MarketEvent is created per candle).

**Why it matters:** The event queue is the #1 talking point for "live-backtest parity" (ADR-001). Without it, the architecture is just sequential function calls — interviewers will ask "where's the queue?" and there isn't one.

---

### R-02: Engine Does Not Use PositionSizer

**Spec (blueprint §Step 6, system-design §2 step 4c):** `PositionSizer.calculate(signal, portfolio, price) → quantity`. The engine should pass signals through the sizer before creating orders.

**Actual:** `BacktestEngine._build_order()` hardcodes `quantity=1` on line 153 of `src/engine/backtest.py`. `PositionSizer` exists in `src/engine/position_sizer.py` but is never imported or used by the engine.

**Fix:**
1. Add `position_sizer: PositionSizer | None = None` parameter to `BacktestEngine.run()` (or to `BacktestConfig`).
2. Default to `FixedQuantitySizer(100)` when None (matching blueprint: "Fixed Quantity" is the simplest default).
3. In `_build_order()`, call `sizer.calculate(signal, portfolio, candle.close)` to get quantity instead of hardcoding 1.
4. Update tests: the existing "buy day 0, sell day 49" test manually computes PnL with qty=1 — update to work with qty from sizer.
5. Add a test that uses `FixedFractionSizer` and verifies the engine respects it.

**Why it matters:** Without this, Kelly Criterion sizing is dead code. "Kelly position sizing" is listed as a key differentiator — it must actually run.

---

### R-03: Strategies Emit Wrong `symbol` in SignalEvent

**Spec:** `SignalEvent.symbol` should be the ticker being traded (e.g., "SPY").

**Actual:**
- `MACrossoverStrategy.on_candle()` (line 68 of `src/strategies/ma_crossover.py`) sets `symbol=candle.timestamp.strftime("%Y-%m-%d")` — it puts a **date string** as the symbol.
- `RSIMeanReversionStrategy` and `BreakoutStrategy` set `symbol=""` (empty string).

**Consequence:** The engine at line 99 of `src/engine/backtest.py` does `symbol = signal.symbol`, so the portfolio tracks positions under a date string or empty string rather than a real ticker. This breaks multi-symbol support and makes the trade log confusing.

**Fix:**
1. Add a `symbol: str` attribute to `BaseStrategy`. Set it in the constructor or via a `configure(symbol)` method.
2. In each concrete strategy's `on_candle()`, use `self.symbol` for `SignalEvent.symbol`.
3. Have `BacktestEngine.run()` pass the symbol to the strategy before the loop: `strategy.symbol = inferred_symbol` (or require it as a `run()` parameter — the blueprint specifies `BacktestEngine.run(strategy, data, config)` where data already implies symbol).
4. Alternative simpler fix: add `symbol: str` parameter to `BacktestEngine.run()` and pass it when constructing signals or inject it after signal creation.

---

## HIGH — Missing Blueprint Features

### R-04: DataStore Not Used in API or Dashboard

**Spec (blueprint §Step 2):** `DataStore` caches to parquet. The API and dashboard should use `DataStore.fetch_or_cache()`.

**Actual:** `_fetch_candles()` in both `src/api/routes/backtest.py` and `src/dashboard/app.py` calls `YFinanceFetcher().fetch()` directly — no caching. Every API call or dashboard run re-downloads data.

**Fix:**
1. In `_fetch_candles()` in both files, wrap the fetcher with `DataStore`:
   ```python
   store = DataStore()
   fetcher = YFinanceFetcher()
   df = store.fetch_or_cache(symbol, datetime.fromisoformat(start), datetime.fromisoformat(end), fetcher)
   ```
2. Respect the `use_cache` flag in `DataFetchRequest` for the `/api/data/fetch` endpoint.

---

### R-05: No `alembic/` Migration Setup

**Spec (blueprint repo structure):** `alembic/versions/` exists for database migrations.

**Actual:** No `alembic/` directory. The DB is created via `Base.metadata.create_all()` in `src/db/database.py`.

**Fix:**
1. `pip install alembic` (add to pyproject.toml dependencies).
2. Run `alembic init alembic` in project root.
3. Configure `alembic/env.py` to import `src.db.tables.Base` and use the same `DATABASE_URL`.
4. Generate initial migration: `alembic revision --autogenerate -m "initial schema"`.
5. Keep `init_db()` for dev convenience but document that production uses `alembic upgrade head`.

---

### R-06: CRUD `save_backtest_run` Does Not Commit

**Actual:** `src/db/crud.py:save_backtest_run()` calls `session.add()` but never `session.commit()`. This relies on the caller or a middleware to commit.

**Fix:**
1. Add `session.commit()` at the end of `save_backtest_run()`.
2. Wrap the entire function body in a try/except with `session.rollback()` on failure to match the "single transaction" guarantee from the spec.
3. Alternatively, ensure the FastAPI dependency (`get_db`) commits on successful response — check `src/api/deps.py` and add auto-commit in the `finally` or use a middleware.

---

### R-07: Missing `notebooks/` Content

**Spec (blueprint §Step 12):**
- `notebooks/01_data_exploration.ipynb` — SPY candlestick chart, volume analysis, return distribution.
- `notebooks/02_strategy_research.ipynb` — Full strategy development journey.

**Actual:** The `notebooks/` directory exists (per git status `?? notebooks/`) but contents are unknown/empty.

**Fix:**
1. Create `notebooks/01_data_exploration.ipynb` with:
   - Fetch SPY 2015-2024 via `YFinanceFetcher`.
   - Candlestick chart (plotly).
   - Volume bar chart.
   - Return distribution histogram + Q-Q plot.
   - Basic statistics (mean, std, skew, kurtosis of daily returns).
2. Create `notebooks/02_strategy_research.ipynb` with:
   - Hypothesis statement for MA crossover.
   - Single backtest run + metrics display.
   - Walk-forward analysis on SPY 2015-2024.
   - Permutation test on the best WFA result.
   - Conclusion: is the strategy statistically significant?

---

### R-08: Missing README.md

**Spec (blueprint §Step 12):** README with architecture diagram, feature list, 5-command quickstart, design decisions section.

**Actual:** No `README.md` exists.

**Fix:** Create `README.md` with:
1. Title + one-line description.
2. Architecture diagram (ASCII or Mermaid — copy from system-design §1).
3. Feature list (event-driven engine, WFA, permutation testing, Kelly sizing, 3 strategies).
4. Quickstart (5 commands: clone, install, test, serve, dashboard).
5. Design decisions section (summarise ADR-001 through ADR-005).
6. Tech stack table.
7. Project structure tree.

---

## MEDIUM — Correctness Issues

### R-09: `AlphaVantageFetcher.fetch()` Compares String to Datetime

**File:** `src/data/fetcher.py`, line 210.

**Bug:** `start` and `end` parameters are typed `DateLike = str | datetime`. On line 210: `if start <= dt <= end:` — this compares a string or datetime against a datetime object. If the caller passes strings (which the API does), this silently produces wrong results or raises TypeError in Python 3.11+.

**Fix:**
1. At the top of `fetch()`, coerce `start` and `end`:
   ```python
   start_dt = datetime.fromisoformat(str(start)) if isinstance(start, str) else start
   end_dt = datetime.fromisoformat(str(end)) if isinstance(end, str) else end
   ```
2. Use `start_dt` and `end_dt` in the comparison.

---

### R-10: `datetime.utcnow()` Is Deprecated

**Files:** `src/models/order.py:37`, `src/engine/event.py:31`, `src/db/tables.py:26`, and several others.

**Issue:** `datetime.utcnow()` is deprecated since Python 3.12 and will be removed. The spec says Python 3.11+ but the code should be forward-compatible.

**Fix:** Replace all `datetime.utcnow()` with `datetime.now(datetime.timezone.utc)` (or `from datetime import timezone; datetime.now(timezone.utc)`). Use find-and-replace across the codebase.

---

### R-11: Sortino Ratio Uses Incorrect Downside Deviation

**File:** `src/analytics/metrics.py`, `sortino_ratio()`.

**Issue:** Sortino ratio should use returns below the *target return* (typically risk-free rate), not returns below zero. The current implementation filters on `arr < 0` (line 64). This means in a high-rate environment (risk_free=0.04), a return of +0.01% daily is treated as "not downside" even though it underperforms the risk-free rate.

**Resolution:** Implemented as target semideviation. Set above-target gaps to zero and compute the
root mean square across *all* observations, so the denominator reflects both downside magnitude and
frequency:
```python
downside_gaps = np.minimum(arr - daily_rf, 0.0)
downside_deviation = float(np.sqrt(np.mean(np.square(downside_gaps))))
```

---

### R-12: Walk-Forward Window Uses Bar Indices, Not Dates

**File:** `src/analytics/walk_forward.py`

**Issue:** `WindowResult` stores `in_sample_start`, `in_sample_end`, `oos_start`, `oos_end` as **integer bar indices** (lines 26-29). The API exposes these raw integers in `WindowOut`. This is meaningless to users — they need dates.

**Fix:**
1. Change `WindowResult` fields to store actual `datetime` values: `in_sample_start_date`, `in_sample_end_date`, `oos_start_date`, `oos_end_date`.
2. Populate from `candles[start].timestamp`, `candles[is_end-1].timestamp`, etc.
3. Keep the integer indices as optional fields for internal use if needed.
4. Update `WindowOut` schema and API route accordingly.

---

### R-13: `get_run()` Returns Wrong `final_equity`

**File:** `src/api/routes/backtest.py`, line 193.

**Bug:** `final_equity=run.initial_capital` — this always returns the initial capital as the final equity. It should read the last equity curve point or store final_equity in the DB.

**Fix:** Either:
1. Add `final_equity` column to `BacktestRun` table and populate it in `save_backtest_run()`.
2. Or compute from equity curve: `equity_pts[-1].equity if equity_pts else run.initial_capital`.

---

## MEDIUM — Quality and Polish

### R-14: BaseStrategy Class Attributes Are Mutable Defaults

**File:** `src/strategies/base.py`, lines 25-26.

**Issue:** `parameters: dict[str, Any] = {}` and `parameter_space: dict[str, tuple[...]] = {}` are **class-level mutable defaults**. All instances share the same dict object unless overridden in `__init__`. This is a classic Python gotcha and would be flagged in a code review.

**Fix:** Use `None` as default and initialize in `__init__`:
```python
class BaseStrategy(ABC):
    name: str = "base_strategy"

    def __init__(self) -> None:
        self.parameters: dict[str, Any] = {}
        self.parameter_space: dict[str, tuple[float, float, float]] = {}
```
Or make `BaseStrategy` a dataclass, or use `field(default_factory=dict)`.

Note: each concrete strategy already overrides these in `__init__`, so this is safe to change. But `WalkForwardAnalyzer._sample_params()` does `dummy = self.strategy_cls()` and reads `dummy.parameter_space` — verify this still works.

---

### R-15: No `mypy --strict` Compliance

**Spec (blueprint §Exit Criteria):** `mypy src/ --strict` zero errors.

**Actual:** Not verified. Several patterns will fail strict mypy:
- `dict` without type parameters in test strategies.
- `Any` return types on several functions.
- Missing return type on `health()` in `src/api/main.py`.
- Bare `assert` in broker (mypy strict forbids these as runtime checks).

**Fix:**
1. Run `mypy src/ --strict` and fix all errors.
2. Replace `assert` in `src/engine/broker.py` lines 80, 93 with proper `if ... raise ValueError`.
3. Add return type annotations to all public functions.
4. Add `py.typed` marker file to `src/`.

---

### R-16: CORS Allows All Origins

**File:** `src/api/main.py`, line 29.

**Issue:** `allow_origins=["*"]` is fine for dev but the system-design §5 mentions production deployment. This should be configurable.

**Fix:**
1. Read origins from an environment variable: `CORS_ORIGINS` (comma-separated).
2. Default to `["*"]` for dev.
3. Example: `origins = os.environ.get("CORS_ORIGINS", "*").split(",")`.

---

### R-17: Tests Missing `session.commit()` Coverage for DB

**Spec (blueprint §Step 9 exit criteria):** "Save + load produces identical metrics."

**Check:** Verify `tests/test_db.py` actually does a round-trip: save → commit → load → assert equality. If the CRUD doesn't commit (see R-06), the test may be passing by reading uncommitted data from the same session.

**Fix:** If the test uses a single session, add an explicit `session.commit()` after save, then use a *new* session to read back and verify.

---

## LOW — Nice-to-Have Improvements

### R-18: Add Survivorship Bias Warning to DataFetcher

**Spec (system-design §6.1):** "My DataFetcher logs a warning when the symbol set matches a current index composition."

**Actual:** No such warning exists.

**Fix:** Add a `logger.warning()` in `YFinanceFetcher.fetch()` noting that yfinance data is subject to survivorship bias. This is a one-liner but directly supports the interview talking point.

---

### R-19: Add `vectorized_signal()` Fast Path to BaseStrategy

**Spec (system-design ADR-001 trade-off):** "Add a `vectorized_signal()` fast-screening path on BaseStrategy for initial parameter sweeps."

**Actual:** Not implemented.

**Fix:** Add an optional `vectorized_signal(candles: list[Candle]) -> list[SignalEvent]` method to `BaseStrategy` with a default implementation that loops `on_candle()`. `WalkForwardAnalyzer` could use this for faster optimization sweeps. This is a nice-to-have for performance.

---

### R-20: Add `Profit Factor` and `Sortino` to Dashboard KPI Cards

**Actual:** Dashboard KPI cards show 6 metrics but omit Sortino and Profit Factor, which are computed by `compute_all_metrics()`.

**Fix:** Add them to the KPI grid in `render_kpis()` — extend to 8 columns or use two rows.

---

### R-21: Walk-Forward and Permutation Tabs Don't Persist Results in Session State

**Actual:** In `src/dashboard/app.py`, clicking "Run Walk-Forward" or "Run Permutation Test" renders results inline. But Streamlit reruns the entire script on every interaction — results disappear if you switch tabs.

**Fix:** Store WFA and permutation results in `st.session_state` (same pattern used for the main backtest result on line 462-464).

---

### R-22: HTML Report Uses uPlot CDN — Should Embed

**Spec (blueprint §Step 12):** "Standalone HTML with embedded Plotly charts."

**Actual:** `src/analytics/report.py` uses uPlot (not Plotly) loaded from a CDN. The report is not standalone — it requires internet access.

**Fix:** Either:
1. Switch to Plotly and embed the JS inline (`plotly.io.to_html(full_html=False)`), or
2. Inline the uPlot JS as a `<script>` tag with the minified source.

---

## CRITICAL — Quant Correctness

### R-23: Look-Ahead Bias in MARKET Order Fills

**This is the single most damaging bug for a quant portfolio project.**

**Issue:** The strategy's `on_candle()` receives a `Candle` that includes the **close price**. If it generates a LONG signal, the broker fills the MARKET order at `close * (1 + slippage)` — the close of **the same bar** that triggered the signal.

In reality, a strategy that decides to trade based on today's close can only execute at **tomorrow's open** (or later). Filling at the same bar's close is classic look-ahead bias: the strategy "sees" the price, decides to buy, and gets filled at that price, which is physically impossible in live trading.

**File:** `src/engine/backtest.py` lines 96-118, `src/engine/broker.py` `_market_fill()`.

**Fix — Option A (next-bar execution, recommended):**
1. When a signal is generated, do **not** execute the order on the current bar.
2. Store pending orders: `self._pending_orders: list[Order] = []`.
3. At the **start** of the next bar's processing (before calling `strategy.on_candle()`), execute any pending orders against the new bar's **open** price (with slippage applied to open).
4. Change `_market_fill()` to: `candle.open * (1 + slippage)` for LONG, `candle.open * (1 - slippage)` for SHORT.
5. This matches how all institutional-grade backtesters work (Zipline, backtrader, QuantConnect).

**Fix — Option B (simpler, less realistic):**
Keep same-bar execution but fill at the **next bar's open**. This requires buffering one bar.

**Impact on existing tests:** `test_buy_day0_sell_day49_pnl_matches_expected` manually computes PnL using `candles[0].close` as entry. After this fix, entry would be `candles[1].open` — update the test accordingly.

**Why this matters for interviews:** Look-ahead bias is THE question quant interviewers ask. The system-design doc talks about "execution realism" as a core differentiator. A strategy that fills at the signal bar's close is the textbook example of what NOT to do. Fixing this elevates the project from "tutorial-grade" to "actually understands market microstructure."

---

### R-24: LIMIT and STOP Orders Never Used — Dead Feature

**Issue:** The broker supports LIMIT and STOP orders with careful fill logic (15 test cases). But:
- All 3 strategies only emit signals (not orders with types).
- `BacktestEngine._build_order()` hardcodes `order_type=OrderType.MARKET` on lines 148 and 163.
- There is no mechanism for a strategy to request a LIMIT or STOP order.

The LIMIT/STOP fill logic is thoroughly tested dead code.

**Fix:**
1. Extend `SignalEvent` with an optional `order_type: OrderType = OrderType.MARKET` and optional `limit_price` / `stop_price` fields.
2. In `_build_order()`, propagate the signal's order type and prices to the `Order`.
3. Add stop-loss support to at least one strategy (e.g., `BreakoutStrategy` with a trailing stop — natural fit for Donchian channels, and a strong interview talking point).
4. Add a test that verifies a LIMIT order placed on bar N only fills when the price condition is met on a later bar (ties into R-23 pending order queue).

---

### R-25: Strategies Duplicate Portfolio Position State

**Issue:** All 3 strategies maintain `self._in_position: bool` internally. The `Portfolio` also tracks open positions via `self._open_positions`. These two state machines can **drift** — if a fill fails (broker returns None), the strategy still flips `_in_position` but the portfolio doesn't change.

Look at `MACrossoverStrategy.on_candle()` lines 73, 82: `self._in_position` is set to True/False at signal time, before the order even reaches the broker. If the broker rejects the order (e.g., LIMIT not filled), the strategy thinks it's in a position but the portfolio disagrees.

**Fix:**
1. Remove `_in_position` from all strategies.
2. Instead, have the engine pass position state to the strategy, either:
   - Add `has_position: bool` parameter to `on_candle()`, or
   - Add a `context: dict` parameter the engine populates with portfolio state, or
   - Let the engine handle "already in position" logic (it partially does this in `_build_order()` already).
3. The cleanest approach: strategies should be pure signal generators — they emit directional opinions, and the engine decides whether to act based on portfolio state. This matches the spec's "strategy → signal → order" separation.

---

### R-26: RSI Recomputed from Scratch Every Bar

**File:** `src/strategies/rsi_mean_reversion.py`, `on_candle()` line 87.

**Issue:** Every bar calls `self._compute_rsi(list(self._prices))` which iterates the full window and recomputes Wilder's smoothing from the seed. This is O(N) per bar where N = period. Wilder's smoothing is designed to be **incremental** — each new bar updates in O(1):

```python
avg_gain = (prev_avg_gain * (period - 1) + current_gain) / period
```

The class already stores `self._avg_gain` and `self._avg_loss` but never uses them (they stay `None`).

**Fix:**
1. Use the initial `period` bars to seed `_avg_gain` and `_avg_loss` with simple averages.
2. On subsequent bars, update incrementally.
3. This also makes the RSI values more accurate — the current approach re-seeds every bar from the sliding window, which diverges from true Wilder RSI after the window fills.

**Why it matters:** "Computing RSI from scratch" is listed as an interview talking point. An interviewer who knows Wilder smoothing will notice the O(N)-per-bar recomputation and ask why you aren't using the incremental formula. Fixing this demonstrates genuine understanding.

---

## HIGH — Robustness

### R-27: Bare `except Exception` Silently Swallows Errors

**Files:**
- `src/analytics/walk_forward.py` line 91: `_run_backtest()` catches ALL exceptions and returns a dummy result. Invalid parameter combos silently produce empty results counted toward optimization.
- `src/analytics/permutation_test.py` line 217: permutation failures silently append `0.0` to metrics, biasing the p-value downward (making strategies look more significant than they are).
- `src/analytics/permutation_test.py` line 126: strategy instantiation failure silently falls back to defaults.

**Fix:**
1. In `_run_backtest()`: catch only `ValueError` (invalid params like fast >= slow). Log a debug message. Re-raise anything else.
2. In permutation test: catch only `ValueError` and `RuntimeError` from backtest runs. Log failures. Exclude failed permutations from the denominator instead of counting them as 0.0.
3. In `_run_single_permutation()`: remove the bare `except Exception` on strategy creation.

**Why it matters:** Silent error swallowing is a reliability anti-pattern. More critically, counting failed permutations as Sharpe=0.0 inflates the number of permutations "worse than actual," making bad strategies look statistically significant.

---

### R-28: No Logging Anywhere

**Issue:** The entire codebase has zero `logging` usage. No module-level loggers, no log statements. For a "production-grade" system targeting quant roles, this is conspicuous.

**Fix:**
1. Add `logger = logging.getLogger(__name__)` to key modules: `backtest.py`, `broker.py`, `walk_forward.py`, `permutation_test.py`, `fetcher.py`.
2. Log at appropriate levels:
   - `logger.info()` for backtest start/end, strategy name, date range.
   - `logger.debug()` for per-bar events, order fills.
   - `logger.warning()` for data gaps, survivorship bias (R-18), failed permutations.
3. Configure a default handler in `src/__init__.py` or let the API/dashboard configure it.

---

### R-29: No Cash Check Before Opening Positions

**File:** `src/models/portfolio.py`, `record_fill()` line 124-125.

**Issue:** When opening a LONG position, `self._cash -= quantity * fill_price + commission`. There is no check that `self._cash >= quantity * fill_price + commission`. The portfolio can go negative cash, which is unrealistic — you can't buy shares you can't afford.

With R-02 (PositionSizer integration), `FixedFractionSizer` computes shares based on equity, but between signal generation and fill, equity can change. More importantly, `FixedQuantitySizer(100)` will happily try to buy 100 shares of a $500 stock ($50k) on a $10k account.

**Fix:**
1. In `record_fill()`, before opening a LONG position, check:
   ```python
   cost = quantity * fill_price + commission
   if cost > self._cash:
       return None  # insufficient funds, order rejected
   ```
2. For SHORT positions, check margin requirements (if modeled) or at minimum prevent shorting when equity is insufficient.
3. Have the engine check the return value — if `record_fill()` returns None for an open, the order was rejected.

---

### R-30: Pending LIMIT/STOP Orders Don't Persist Across Bars

**Issue:** Currently, the broker processes an order against a single candle and returns fill or None. If a LIMIT order is placed on bar 5 but the price condition isn't met until bar 12, the order is lost — it's checked once and discarded.

**Spec (blueprint §Step 3):** "LIMIT orders fill only if price crosses limit; STOP orders fill if price crosses stop level." — implies they should persist until filled or cancelled.

**Fix:**
1. Add `_pending_orders: list[Order]` to the broker (or engine).
2. Each bar, iterate pending orders and attempt to fill them.
3. Add order cancellation (e.g., time-in-force: GTC, DAY, or N-bar expiry).
4. This naturally integrates with R-23 (next-bar execution) — MARKET orders become "pending until next bar" with immediate fill on next bar's open.

---

### R-31: Short P&L Model Doesn't Account for Margin

**File:** `src/models/portfolio.py`, lines 54-57 and 108-110.

**Issue:** The short P&L formula `qty * (2 * entry_price - price)` is a simplification. It correctly computes directional PnL, but:
- No margin is reserved when opening a short (cash actually increases: `self._cash += quantity * fill_price`)
- No borrowing cost is modeled
- No margin call trigger if the position moves against you

For a project targeting quant roles, this should at least be documented as a known simplification, or better, margin requirements should be modeled.

**Fix (minimal — document):** Add a comment in Portfolio explaining the short-selling model and its limitations.

**Fix (better — model margin):**
1. When opening a short, reserve `margin = quantity * fill_price * margin_requirement` (typically 150% of position value).
2. Track margin separately from available cash.
3. Add a `margin_requirement: float = 1.5` parameter to Portfolio or BacktestConfig.

---

## MEDIUM — Additional Quality

### R-32: Walk-Forward Optimization Creates Strategy Instance Per Trial Without Reset

**File:** `src/analytics/walk_forward.py`, `_run_backtest()` line 87.

**Issue:** Each optimization trial creates a new strategy instance via `self.strategy_cls(**params)`. This is actually correct (fresh state). However, `_sample_params()` on line 77 creates a `dummy = self.strategy_cls()` every call just to read `parameter_space` — this is wasteful (50 dummy instances per window). Cache the parameter space once.

**Fix:**
```python
def __init__(self, ...):
    ...
    self._parameter_space = self.strategy_cls().parameter_space
```
Then use `self._parameter_space` in `_sample_params()`.

---

### R-33: Permutation Test p-value Denominator Should Include Actual

**File:** `src/analytics/permutation_test.py`, line 241.

**Issue:** The p-value is `sum(permuted >= actual) / n` where `n` = number of permutations. The statistically correct permutation p-value includes the actual observation:

```
p = (count(permuted >= actual) + 1) / (n + 1)
```

This is the standard Monte Carlo p-value that prevents p=0.0 (which is technically impossible from a finite permutation test).

**Fix:** Change line 241 to:
```python
p_value = (sum(1 for m in permuted_metrics if m >= actual_metric) + 1) / (n + 1)
```

---

### R-34: Dashboard Comparison Tab Re-fetches Data for Each Strategy

**File:** `src/dashboard/app.py`, `render_comparison_tab()` lines 396-407.

**Issue:** The comparison loop fetches candles on the first iteration and reuses them, which is correct. However, each strategy creates a new `BacktestEngine()` instance. More importantly, the strategies run with **default parameters** (`cls()`) rather than allowing the user to configure each one. This makes the comparison misleading — it compares default MA(10,50) against default RSI(14,30) against default Breakout(20).

**Fix:**
1. Allow users to configure parameters per strategy in the comparison (or run each with its WFA-optimized parameters).
2. At minimum, note in the UI that comparisons use default parameters.

---

### R-35: No Input Validation on Date Ranges

**Issue:** The API accepts `start` and `end` as plain strings. There's no validation that:
- `start` < `end`
- Dates are valid ISO format
- Range isn't absurdly large (e.g., 100 years)
- Range isn't in the future

**Files:** `src/api/schemas.py` `BacktestRequest`, `WalkForwardRequest`, `PermutationRequest`.

**Fix:** Add Pydantic validators:
```python
from pydantic import field_validator
from datetime import date

@field_validator("start", "end")
@classmethod
def validate_date(cls, v: str) -> str:
    date.fromisoformat(v)  # raises ValueError if invalid
    return v
```
Add a model validator that checks `start < end`.

---

## Execution Order

Priority order for maximum impact:

1. **R-23** (look-ahead bias) — **the most critical quant correctness issue**
2. **R-03** (fix symbol in signals) — prerequisite for everything else working correctly
3. **R-02** (integrate PositionSizer) — unlocks Kelly Criterion, a key differentiator
4. **R-25** (remove duplicate position state) — prevents state drift bugs
5. **R-01** (event queue) — architectural correctness, interview talking point
6. **R-29** (cash check) — prevents impossible trades
7. **R-24** (LIMIT/STOP actually usable) — unlocks stop-loss, dead code becomes live
8. **R-06** (CRUD commit) — data persistence actually works
9. **R-13** (final_equity bug) — API returns wrong data
10. **R-27** (stop swallowing errors) — silent failures bias results
11. **R-33** (p-value formula) — statistical correctness
12. **R-26** (incremental RSI) — correctness + performance + interview talking point
13. **R-09** (AlphaVantage type bug) — runtime error waiting to happen
14. **R-10** (deprecated utcnow) — forward compatibility
15. **R-11** (Sortino fix) — metric correctness
16. **R-04** (DataStore integration) — caching works
17. **R-12** (WFA dates) — API usability
18. **R-28** (logging) — production readiness
19. **R-14** (mutable defaults) — code quality
20. **R-15** (mypy strict) — matches exit criteria
21. **R-35** (input validation) — API robustness
22. **R-30** (persistent orders) — order lifecycle correctness
23. **R-08** (README) — project completeness
24. **R-07** (notebooks) — project completeness
25. **R-05** (alembic) — production readiness
26. **R-31** (short margin) — realism
27. **R-32** (cache parameter_space) — minor perf
28. **R-34** (comparison params) — UX
29. **R-16 through R-22** — polish

---

## Verification After All Revisions

```bash
# Must all pass
pytest tests/ -v --tb=short
mypy src/ --strict
ruff check src/ tests/

# Manual verification
make serve        # API starts, /docs renders all endpoints
make dashboard    # All 8 sections render with real data
python -m src.analytics.report --symbol SPY  # Standalone HTML opens without internet
```

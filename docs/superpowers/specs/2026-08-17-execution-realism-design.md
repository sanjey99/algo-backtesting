# Execution Realism, Margin, and Observability

**Status:** Approved
**Date:** 2026-08-17
**Repository:** `algo-backtesting`
**Branch:** `master`

## 1. Problem and Scope

The backtest engine currently asks a strategy to inspect a complete candle and then fills the
resulting market order at that same candle's close. That is look-ahead bias: the decision and fill
use information that was not simultaneously tradable. Conditional orders are supported by the
broker but are neither emitted by strategies nor retained across bars. Short positions calculate
directional P&L but do not reserve margin, accrue borrow cost, or trigger margin calls. Important
operational decisions are also invisible because the application has no structured logging.

This change makes daily, single-symbol backtests materially more realistic while preserving the
existing API, dashboard, walk-forward, permutation, and SQL analytics entry points.

In scope:

- Execute market orders on the next bar's open.
- Retain GTC LIMIT and STOP orders until fill, replacement, cancellation at end of data, or a
  conflicting signal.
- Apply explicit gap-price rules for conditional orders.
- Represent accepted and rejected portfolio fills with a typed outcome.
- Reserve short collateral, accrue borrow cost, and enforce maintenance margin.
- Emit structured, redacted operational logs.
- Remove the known Starlette TestClient and `datetime.utcnow()` deprecation warnings.

Deferred:

- Intraday partial fills, volume participation, exchange queues, and order-book simulation.
- Multi-asset portfolios, portfolio-level netting, and cross-margin.
- Locate/borrow availability, hard-to-borrow schedules, dividends owed by shorts, and variable
  broker rates.
- User-defined cancellation events, bracket/OCO orders, and partial position reversals.
- Persisting the internal pending-order queue across process restarts.

## 2. Approaches Considered

### 2.1 Replace the engine with a third-party backtester

This would provide mature order lifecycle behavior, but it would replace the project's core
event-driven implementation and make existing analytics contracts adapters around another system.
Rejected because it obscures the project's own execution model and creates a migration much larger
than the correctness gap.

### 2.2 Put all lifecycle state in the broker

The broker could own pending orders, fills, expiry, and margin. That resembles a live brokerage,
but it would couple pricing to portfolio policy and leave the engine unable to explain why a fill
was rejected. Rejected because the existing broker is a small, useful price-execution boundary.

### 2.3 Evolve the existing boundaries — selected

`BacktestEngine` owns order lifecycle and timing. `SimulatedBroker` owns deterministic fill-price
calculation against one bar. `Portfolio` owns buying power, collateral, borrow charges, and margin
state. Strategies remain signal generators. This is the smallest architecture that makes timing,
pricing, and account policy independently testable.

## 3. Execution Lifecycle

For each candle, the engine performs these steps in order:

1. Accrue short borrow cost from the previous mark timestamp to the current candle using the
   previous marked short value. No current-bar price is used during accrual.
2. Submit every order that was pending before this candle to the broker for evaluation against the
   current OHLC values.
3. Apply accepted fills to the portfolio. Log and remove orders rejected for insufficient buying
   power. Retain untriggered LIMIT and STOP orders.
4. Mark the portfolio to the candle close.
5. Evaluate maintenance margin at the close. A breach queues a forced market cover for the next
   bar's open; it never fills retroactively at the closing price that caused the breach.
6. Pass the complete candle and a frozen `StrategyContext` derived from filled portfolio and
   pending-order state to `strategy.on_candle()`.
7. Translate any signal into an order and enqueue it. The new order cannot execute until a later
   candle.

This ordering means a signal generated on bar N can first fill on bar N+1. The final candle may
produce an order, but that order remains unfilled and is cancelled with a structured end-of-data
event. Open positions are not automatically liquidated at the final close because that would
reintroduce a privileged same-bar fill.

The queue is local to one `BacktestEngine.run()` invocation. It is created fresh for every run so
reusing an engine instance cannot leak orders between backtests.

## 4. Orders and Conditional Signals

`SignalEvent` gains optional order instructions:

- `order_type`, defaulting to `MARKET`.
- `limit_price`, required only for LIMIT.
- `stop_price`, required only for STOP.

The existing `Order` model remains the validation boundary for price requirements. `_build_order()`
copies these instructions rather than hardcoding MARKET. Existing strategies require no changes and
continue to emit market signals.

V1 time in force is GTC for LIMIT and STOP orders and next-bar-only for MARKET orders. An unfilled
market order is removed after its eligible bar because a market order always has an executable
open when the candle contract is valid. A conditional order remains pending until:

- it fills;
- a newer signal for the same symbol replaces it;
- a forced margin-cover order supersedes it; or
- the candle series ends.

There is at most one pending strategy order per symbol. A newer same-direction signal replaces the
older order so stale price instructions cannot accumulate. An opposite signal cancels the old
pending order before the engine decides whether the portfolio needs an exit or a new entry.

## 5. Fill Pricing and Gap Rules

All slippage remains adverse to the trader. Commission remains a percentage of filled notional.

### 5.1 MARKET

- Buy: `open * (1 + slippage_pct)`.
- Sell: `open * (1 - slippage_pct)`.

### 5.2 LIMIT

A limit order cannot fill worse than its limit:

- Buy limit: if `open <= limit`, use `min(limit, open * (1 + slippage_pct))`; otherwise, if
  `low <= limit`, fill at limit.
- Sell limit: if `open >= limit`, use `max(limit, open * (1 - slippage_pct))`; otherwise, if
  `high >= limit`, fill at limit.

Thus a favorable overnight gap receives price improvement at the obtainable open rather than an
artificial fill at the limit.

### 5.3 STOP

A stop becomes marketable when triggered and may gap adversely:

- Buy stop: if `open >= stop`, fill at open; otherwise, if `high >= stop`, fill at stop.
- Sell stop: if `open <= stop`, fill at open; otherwise, if `low <= stop`, fill at stop.

Slippage is then applied adversely to the selected raw stop fill price. This prevents a gap through
a stop from receiving an impossible threshold-price fill. LIMIT slippage is capped at the limit as
specified above because a limit order cannot execute at a worse price.

## 6. Portfolio Fill Outcomes

`Portfolio.record_fill()` returns a frozen `FillOutcome` instead of overloading `None` to mean both
"position opened" and "fill rejected." The outcome contains:

- `accepted: bool`;
- optional closed `Trade`;
- a closed rejection reason enum;
- resulting available cash and restricted collateral.

Expected buying-power rejection is data, not an exception. Invalid quantities, prices, directions,
or impossible internal position transitions remain programming errors and fail loudly. The engine
removes rejected orders and emits a warning event; it does not retry an unaffordable order against
later bars.

## 7. Short Margin and Borrow Cost

`BacktestConfig` gains validated defaults:

- `short_initial_margin: float = 1.50`;
- `short_maintenance_margin: float = 0.30`;
- `annual_short_borrow_rate: float = 0.03`;
- `borrow_day_count: float = 365.0`.

All rates must be finite. Initial margin must be at least `1.0`; maintenance margin and borrow rate
must be nonnegative; day count must be positive.

When opening a short with notional `N`:

- Restricted collateral becomes `N * short_initial_margin`.
- The short-sale proceeds are part of that collateral.
- Available cash decreases by `N * (short_initial_margin - 1) + commission`.
- The order is rejected if available cash cannot supply that amount.

Equity is:

```text
available_cash + restricted_collateral - current_short_market_value
```

At entry this equals pre-trade equity less commission. On cover, collateral is released, shares are
repurchased, exit commission is deducted, and realized P&L remains `quantity * (entry - exit)` less
both commissions.

Borrow cost uses actual elapsed calendar time:

```text
previous_marked_short_value * annual_short_borrow_rate * elapsed_days / borrow_day_count
```

It is deducted from available cash and therefore from equity. The first short bar has no elapsed
borrow period. Non-increasing timestamps are contract violations.

After each close mark, maintenance ratio is `equity / current_short_market_value`. If it falls below
the configured threshold, the engine cancels the symbol's pending strategy order and queues a
forced market cover for the next bar. If no next bar exists, the position remains open and an
unresolved-margin event is logged; the engine does not invent a fill.

The current engine supports one open position per symbol, so the first implementation tracks one
collateral record per open short. Multi-position allocation and cross-margin remain deferred.

## 8. Strategy and Position State

Strategies continue to emit directional opinions. The engine remains authoritative for deciding
whether a signal opens, closes, replaces, or is ignored based on actual portfolio and pending-order
state.

A frozen `StrategyContext` exposes only the execution state needed for signal intent:

- the filled position direction and quantity, if any;
- the pending strategy-order direction and type, if any; and
- whether a forced margin cover is pending.

The strategy protocol becomes `on_candle(candle, context)`. Built-in strategies and test strategies
are migrated together. The context does not expose mutable `Portfolio` or `Order` objects, so a
strategy cannot mutate execution state.

Existing strategy `_in_position` flags are removed because they change at signal time rather than
fill time and can drift when an order remains pending or is rejected. Strategies retain only the
price history needed to calculate signals and use `StrategyContext` to distinguish an entry opinion
from a close opinion. This preserves the built-in strategies' current long-only behavior: for
example, a moving-average death cross closes an actual long but does not open a new short after an
earlier entry was rejected. The engine independently suppresses redundant or conflicting orders as
the final authority.

## 9. Structured Logging

The project uses the Python standard library `logging` package with module-level loggers and a
small JSON formatter. Library modules never install global handlers. Runtime entry points configure
the handler and level from settings, defaulting to `INFO`.

Each record contains stable fields:

- UTC timestamp, level, logger, and event name;
- run ID, strategy, and symbol when available;
- order ID/type/direction/quantity without serializing the whole object;
- acquisition ID/provider/status for data events;
- numeric counts or reason codes.

Event names include:

- `backtest.started`, `backtest.completed`, `backtest.failed`;
- `order.queued`, `order.filled`, `order.untriggered`, `order.replaced`, `order.rejected`,
  `order.cancelled_end_of_data`;
- `margin.borrow_accrued`, `margin.call_queued`, `margin.call_unresolved`;
- `acquisition.cache_result`, `acquisition.provider_attempt`, `acquisition.fallback`,
  `acquisition.quality_warning`, `acquisition.failed`;
- `walk_forward.invalid_parameters`, `permutation.failed`.

Per-bar marks are not logged at INFO. Order lifecycle details are DEBUG; starts/completions are INFO;
rejections, fallback, quality degradation, and margin events are WARNING; unexpected failures are
logged with exception context and re-raised.

Logs must never contain API keys, authorization headers, credential-bearing URLs, full provider
payloads, raw data frames, or unrestricted exception representations from external services. Tests
assert representative records are valid JSON and redact known secret-shaped values.

## 10. Compatibility Warning Remediation

The installed baseline is FastAPI 0.141.1, Starlette 1.3.1, httpx 0.28.1, and SQLAlchemy 2.0.51.

Starlette 1.3 documents `httpx2` as the TestClient transport and deprecates plain `httpx`. Add
`httpx2` to development dependencies, remove the unused direct `httpx` dependency if dependency
inspection confirms no production import, regenerate `uv.lock`, and retain the public
`fastapi.testclient.TestClient` imports. A warning-as-error API test proves the migration.

The SQLAlchemy warning is triggered by the repository's `BacktestRun.created_at` default using
`datetime.utcnow`. Replace it with an application helper returning timezone-naive UTC via
`datetime.now(UTC).replace(tzinfo=None)`. SQLite persistence remains intentionally naive UTC, so no
schema migration is required. A warning-as-error insert test covers the default.

Dependency changes are limited to versions resolved by `uv` under the existing Python constraint.
No broad framework upgrade is required unless lock resolution demonstrates a compatibility need.

## 11. Public Compatibility and Persistence

Existing API request and response schemas remain unchanged. `BacktestResult`, persisted trades,
equity points, and metrics retain their current serialized fields. Results will change numerically
because fills and borrow costs become realistic; this is an intentional correctness change.

The internal Python strategy protocol changes to accept `StrategyContext`. All built-in strategies,
analytics helpers, and test strategies are migrated atomically. This protocol is not exposed through
the HTTP API, but direct Python users implementing custom strategies must update their method
signature.

New margin configuration first exists in `BacktestConfig` with defaults, so current callers remain
source-compatible. Exposing these settings through API/dashboard controls is deferred unless an
existing generic configuration mapping already carries them without schema expansion.

No database schema change is needed. Borrow cost and margin events are reflected in cash/equity and
logs but are not added as new analytics tables in this iteration.

## 12. Error Handling

- Empty candle lists and invalid configuration fail before the run starts.
- Candles must be strictly increasing by timestamp; duplicates or reversals fail closed.
- Unexpected broker, engine, strategy, or portfolio failures are logged and propagated.
- Expected invalid optimization parameters retain their existing typed fallback behavior.
- Untriggered conditional orders and final-bar market orders are normal cancellations, not errors.
- Insufficient buying power rejects the order without mutating portfolio state.
- A margin call queues a forced cover; it cannot be overridden by a strategy signal.

## 13. Test Strategy

Implementation follows TDD, with each behavioral slice demonstrated red before production code.

Unit coverage:

- MARKET fills use next open and adverse slippage.
- LIMIT and STOP gap rules cover buy/sell, favorable/adverse gaps, intrabar triggers, and misses.
- GTC orders persist across several bars and do not fill on their creation bar.
- Replacement, end-of-data cancellation, and forced-cover priority are deterministic.
- `FillOutcome` distinguishes open, close, and buying-power rejection without partial mutation.
- Short entry accounting, profitable/loss covers, collateral release, borrow accrual, and rejected
  shorts reconcile exactly.
- Maintenance breach queues a next-open cover and handles a missing next bar.
- Strategy state cannot drift after an unfilled/rejected order.
- Built-in long-only strategies do not accidentally open shorts when an entry was never filled.
- JSON logs contain required fields and omit secrets.
- UTC defaults and TestClient run with targeted deprecations promoted to errors.

Integration coverage:

- A deterministic multi-bar strategy proves signal-on-N/fill-on-N+1 timing through the full engine.
- Walk-forward and permutation analyses propagate the new engine behavior without interface changes.
- API backtest creation persists trades and equity generated by next-open execution.
- Data acquisition emits structured cache, fallback, warning, and terminal-failure records.

Regression verification:

- Entire pytest suite passes with at least 80% source coverage.
- `ruff check src tests` passes.
- `mypy src --strict` passes.
- Targeted warning-as-error checks pass for Starlette TestClient and SQLAlchemy datetime defaults.
- Deterministic expected metrics and benchmark hashes are updated only where the execution model
  legitimately changes their values.

## 14. Delivery Sequence

1. Introduce next-bar MARKET execution and exact pricing rules.
2. Add conditional signal fields and persistent GTC order lifecycle.
3. Add typed fill outcomes and remove strategy position-state duplication.
4. Add short collateral and borrow accrual.
5. Add maintenance-margin forced covers.
6. Add structured logging and redaction tests.
7. Remediate dependency/default deprecations and regenerate the lockfile.
8. Run full verification and update operator-facing documentation.

Each slice is independently tested and committed only after its focused tests pass. The final branch
is not pushed without explicit user authorization.

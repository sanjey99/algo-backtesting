# Execution and Margin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove same-bar look-ahead fills, persist conditional orders, eliminate strategy/portfolio state drift, and add deterministic short-margin accounting.

**Architecture:** `BacktestEngine` owns timing and the pending-order lifecycle, `SimulatedBroker` owns fill-price selection, and `Portfolio` owns fill acceptance, collateral, borrow charges, and margin state. Strategies receive immutable execution context rather than maintaining their own position flags.

**Tech Stack:** Python 3.12+, dataclasses, enum, pytest, pytest-cov, Ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-08-17-execution-realism-design.md`

## Global Constraints

- MARKET orders created on bar N first execute at bar N+1 open.
- LIMIT/STOP orders are GTC and obey the exact gap formulas in spec section 5.
- Default short settings are initial margin `1.50`, maintenance margin `0.30`, annual borrow rate `0.03`, and day count `365.0`.
- API response schemas and persisted table schemas do not change.
- The final branch is not pushed without explicit user authorization.
- Preserve the unrelated untracked `.codegraph/` directory.

## File Structure

- Create `src/engine/context.py`: immutable strategy-facing execution state.
- Modify `src/engine/broker.py`: deterministic OHLC/gap fill pricing only.
- Modify `src/engine/backtest.py`: configuration validation, pending orders, next-bar flow, and forced covers.
- Modify `src/engine/event.py`: validate existing conditional signal fields and remove temporary comments.
- Modify `src/models/portfolio.py`: typed outcomes, collateral, borrow, and maintenance calculations.
- Modify `src/strategies/base.py`: context-aware strategy protocol and helpers.
- Modify all three files under `src/strategies/`: remove private position flags and consume context.
- Modify `src/analytics/permutation_test.py`: carry the complete expanded config into workers.
- Modify engine, model, strategy, analytics, API, and walk-forward tests that construct strategies or assert exact fills.
- Modify `README.md`: document next-open timing and margin defaults.

---

### Task 1: Deterministic next-open and gap-aware broker pricing

**Files:**
- Modify: `src/engine/broker.py:9-103`
- Test: `tests/test_engine.py:64-253`

**Interfaces:**
- Consumes: existing `SimulatedBroker.execute(order: Order, candle: Candle) -> FillEvent | None`.
- Produces: unchanged public signature with the spec section 5 pricing rules.

- [ ] **Step 1: Write failing market and gap tests**

Update the market assertions to use an open distinct from close and add all four gap directions:

```python
def test_market_long_fills_at_open(self) -> None:
    candle = make_candle(open_=90.0, close=100.0, high=101.0, low=89.0)
    order = Order(symbol="X", direction=Direction.LONG, quantity=1)
    fill = self.broker.execute(order, candle)
    assert fill is not None
    assert fill.fill_price == pytest.approx(90.0 * (1 + self.SLIPPAGE))

def test_buy_limit_gap_gets_protected_slipped_open(self) -> None:
    broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
    candle = make_candle(open_=90.0, high=96.0, low=89.0, close=95.0)
    order = Order("X", Direction.LONG, 1, OrderType.LIMIT, limit_price=95.0)
    fill = broker.execute(order, candle)
    assert fill is not None
    assert fill.fill_price == pytest.approx(90.9)

def test_sell_limit_gap_gets_protected_slipped_open(self) -> None:
    broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
    candle = make_candle(open_=110.0, high=111.0, low=104.0, close=105.0)
    order = Order("X", Direction.SHORT, 1, OrderType.LIMIT, limit_price=105.0)
    fill = broker.execute(order, candle)
    assert fill is not None
    assert fill.fill_price == pytest.approx(108.9)

def test_buy_stop_gap_pays_open_plus_slippage(self) -> None:
    broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
    candle = make_candle(open_=110.0, high=112.0, low=109.0, close=111.0)
    order = Order("X", Direction.LONG, 1, OrderType.STOP, stop_price=105.0)
    fill = broker.execute(order, candle)
    assert fill is not None
    assert fill.fill_price == pytest.approx(111.1)

def test_sell_stop_gap_pays_open_minus_slippage(self) -> None:
    broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
    candle = make_candle(open_=90.0, high=91.0, low=88.0, close=89.0)
    order = Order("X", Direction.SHORT, 1, OrderType.STOP, stop_price=95.0)
    fill = broker.execute(order, candle)
    assert fill is not None
    assert fill.fill_price == pytest.approx(89.1)
```

- [ ] **Step 2: Run the broker tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_engine.py -k 'market or limit or stop' -q`

Expected: current MARKET tests use close, and new gap tests return threshold prices.

- [ ] **Step 3: Implement exact raw-price and slippage rules**

Use these helpers in `SimulatedBroker`:

```python
def _market_fill(self, order: Order, candle: Candle) -> float:
    return self._adverse_price(candle.open, order.direction)

def _limit_fill(self, order: Order, candle: Candle) -> float | None:
    assert order.limit_price is not None
    limit = order.limit_price
    if order.direction is Direction.LONG:
        if candle.open <= limit:
            return min(limit, self._adverse_price(candle.open, order.direction))
        return limit if candle.low <= limit else None
    if candle.open >= limit:
        return max(limit, self._adverse_price(candle.open, order.direction))
    return limit if candle.high >= limit else None

def _stop_fill(self, order: Order, candle: Candle) -> float | None:
    assert order.stop_price is not None
    stop = order.stop_price
    if order.direction is Direction.LONG:
        raw = candle.open if candle.open >= stop else stop if candle.high >= stop else None
    else:
        raw = candle.open if candle.open <= stop else stop if candle.low <= stop else None
    return None if raw is None else self._adverse_price(raw, order.direction)

def _adverse_price(self, price: float, direction: Direction) -> float:
    multiplier = 1.0 + self.slippage_pct if direction is Direction.LONG else 1.0 - self.slippage_pct
    return price * multiplier
```

- [ ] **Step 4: Run focused tests and lint**

Run: `uv run --extra dev pytest tests/test_engine.py -k 'SimulatedBroker' -q`

Run: `uv run --extra dev ruff check src/engine/broker.py tests/test_engine.py`

Expected: PASS.

- [ ] **Step 5: Commit the broker slice**

```bash
git add src/engine/broker.py tests/test_engine.py
git commit -m "fix: model next-open and gap fill prices"
```

### Task 2: Typed portfolio fill outcomes

**Files:**
- Modify: `src/models/portfolio.py:1-163`
- Modify: `src/models/__init__.py`
- Test: `tests/test_models.py:180-249`

**Interfaces:**
- Produces: `FillRejectionReason`, `FillOutcome`, and `Portfolio.record_fill(...) -> FillOutcome`.
- Produces: `Portfolio.restricted_collateral -> float`, initially always zero until Task 5.
- Consumed by: Task 4 engine order processing and Task 5 margin accounting.

- [ ] **Step 1: Write failing outcome tests**

```python
def test_open_long_returns_accepted_outcome(self) -> None:
    portfolio = Portfolio(1_000.0)
    outcome = portfolio.record_fill("X", Direction.LONG, 1, 100.0, datetime(2023, 1, 2))
    assert outcome == FillOutcome(True, None, None, 900.0, 0.0)

def test_insufficient_cash_returns_rejection_without_mutation(self) -> None:
    portfolio = Portfolio(1_000.0)
    outcome = portfolio.record_fill("X", Direction.LONG, 100, 100.0, datetime(2023, 1, 2))
    assert outcome.accepted is False
    assert outcome.rejection_reason is FillRejectionReason.INSUFFICIENT_BUYING_POWER
    assert portfolio.cash == 1_000.0
    assert portfolio.open_positions == {}

def test_same_direction_fill_against_open_position_is_invalid(self) -> None:
    portfolio = Portfolio(1_000.0)
    portfolio.record_fill("X", Direction.LONG, 1, 100.0, datetime(2023, 1, 2))
    with pytest.raises(ValueError, match="opposite"):
        portfolio.record_fill("X", Direction.LONG, 1, 101.0, datetime(2023, 1, 3))
```

- [ ] **Step 2: Run the outcome tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_models.py -k 'outcome or insufficient_cash' -q`

Expected: `FillOutcome` and `FillRejectionReason` do not exist.

- [ ] **Step 3: Add immutable outcomes and update all return paths**

```python
class FillRejectionReason(StrEnum):
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"

@dataclass(frozen=True, slots=True)
class FillOutcome:
    accepted: bool
    trade: Trade | None
    rejection_reason: FillRejectionReason | None
    available_cash: float
    restricted_collateral: float
```

Add `restricted_collateral` as a read-only property. Return `FillOutcome(False, None,
FillRejectionReason.INSUFFICIENT_BUYING_POWER, self._cash, self._restricted_collateral)` before any
mutation, and return accepted outcomes for both open and close paths.

- [ ] **Step 4: Update existing model assertions and run tests**

Replace assertions that treat the close result as a `Trade` with `outcome.trade`, and assertions
that treat `None` as rejection with `outcome.accepted is False`.

Run: `uv run --extra dev pytest tests/test_models.py tests/test_broker.py -q`

Expected: PASS.

- [ ] **Step 5: Commit typed outcomes**

```bash
git add src/models/portfolio.py src/models/__init__.py tests/test_models.py tests/test_broker.py
git commit -m "refactor: return typed portfolio fill outcomes"
```

### Task 3: Immutable strategy execution context

**Files:**
- Create: `src/engine/context.py`
- Modify: `src/engine/backtest.py:20-27`
- Modify: `src/strategies/base.py`
- Modify: `src/strategies/ma_crossover.py`
- Modify: `src/strategies/rsi_mean_reversion.py`
- Modify: `src/strategies/breakout.py`
- Test: `tests/test_strategies.py`

**Interfaces:**
- Produces: `StrategyContext` with filled position, pending order, and forced-cover state.
- Produces: `Strategy.on_candle(candle: Candle, context: StrategyContext) -> SignalEvent | None`.
- Consumed by: Task 4 pending-order engine.

- [ ] **Step 1: Add failing long-only state tests**

Add a deterministic strategy test using a primed indicator state:

```python
def _ma_crossing_candles() -> list[Candle]:
    prices = (1.0, 2.0, 3.0, 4.0, 1.0)
    return [
        make_candle_with_price(price, datetime(2023, 1, index + 1))
        for index, price in enumerate(prices)
    ]

def test_ma_death_cross_without_filled_long_does_not_open_short() -> None:
    strategy = MACrossoverStrategy(fast_period=2, slow_period=3)
    context = StrategyContext()
    signals = [strategy.on_candle(candle, context) for candle in _ma_crossing_candles()]
    assert all(signal is None or signal.direction is not Direction.SHORT for signal in signals)

def test_ma_death_cross_with_filled_long_emits_close() -> None:
    strategy = MACrossoverStrategy(fast_period=2, slow_period=3)
    long_context = StrategyContext(position_direction=Direction.LONG, position_quantity=1)
    signals = [strategy.on_candle(candle, long_context) for candle in _ma_crossing_candles()]
    assert any(signal is not None and signal.direction is Direction.SHORT for signal in signals)
```

- [ ] **Step 2: Run the strategy tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_strategies.py -q`

Expected: missing `StrategyContext` and incompatible `on_candle` signatures.

- [ ] **Step 3: Create the frozen context contract**

```python
@dataclass(frozen=True, slots=True)
class StrategyContext:
    position_direction: Direction | None = None
    position_quantity: int = 0
    pending_direction: Direction | None = None
    pending_order_type: OrderType | None = None
    forced_cover_pending: bool = False
```

Validate that quantity is nonnegative, zero when no direction exists, and positive when a direction
exists.

- [ ] **Step 4: Migrate strategy signatures and remove `_in_position`**

Use this signature everywhere:

```python
def on_candle(self, candle: Candle, context: StrategyContext) -> SignalEvent | None:
```

For long-only entry conditions require both `context.position_direction is None` and
`context.pending_direction is None`. For exits require
`context.position_direction is Direction.LONG` and `not context.forced_cover_pending`. Remove every
assignment/reset of `_in_position`. Update `BaseStrategy.vectorized_signal` and `generate_orders`
to require a context argument rather than inventing fill state.

- [ ] **Step 5: Run all strategy tests and strict typing**

Run: `uv run --extra dev pytest tests/test_strategies.py -q`

Run: `uv run --extra dev mypy src/engine/context.py src/strategies --strict`

Expected: PASS.

- [ ] **Step 6: Commit strategy context**

```bash
git add src/engine/context.py src/engine/backtest.py src/strategies tests/test_strategies.py
git commit -m "refactor: derive strategy state from execution context"
```

### Task 4: Pending orders and next-bar engine flow

**Files:**
- Modify: `src/engine/event.py:17-34`
- Modify: `src/engine/backtest.py:1-171`
- Test: `tests/test_engine.py:256-380`

**Interfaces:**
- Consumes: `StrategyContext` and typed `FillOutcome`.
- Produces: run-local `_PendingOrder(order: Order, forced_cover: bool = False)` lifecycle.
- Preserves: `BacktestEngine.run(strategy, candles, config) -> BacktestResult`.

- [ ] **Step 1: Write failing next-bar and persistence tests**

```python
def _bar(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        timestamp=datetime(2023, 1, index + 1),
        open=open_, high=high, low=low, close=close,
        volume=1_000.0, adj_close=close,
    )

class _BuyOnZeroSellOnOne:
    name = "timing"
    parameters: dict[str, object] = {}

    def __init__(self) -> None:
        self.index = 0

    def on_candle(self, candle: Candle, context: StrategyContext) -> SignalEvent | None:
        index = self.index
        self.index += 1
        if index == 0:
            return SignalEvent("X", Direction.LONG, timestamp=candle.timestamp)
        if index == 1 and context.position_direction is Direction.LONG:
            return SignalEvent("X", Direction.SHORT, timestamp=candle.timestamp)
        return None

class _BuyLimitThenCloseAfterFill:
    name = "limit-timing"
    parameters: dict[str, object] = {}

    def __init__(self, limit: float) -> None:
        self.limit = limit
        self.submitted = False

    def on_candle(self, candle: Candle, context: StrategyContext) -> SignalEvent | None:
        if not self.submitted:
            self.submitted = True
            return SignalEvent("X", Direction.LONG, timestamp=candle.timestamp, order_type=OrderType.LIMIT, limit_price=self.limit)
        if context.position_direction is Direction.LONG:
            return SignalEvent("X", Direction.SHORT, timestamp=candle.timestamp)
        return None

class _SignalOnlyOnLastBar:
    name = "last-bar"
    parameters: dict[str, object] = {}

    def __init__(self, last_index: int = 2) -> None:
        self.index = 0
        self.last_index = last_index

    def on_candle(self, candle: Candle, context: StrategyContext) -> SignalEvent | None:
        index = self.index
        self.index += 1
        return SignalEvent("X", Direction.LONG, timestamp=candle.timestamp) if index == self.last_index else None

class _ScheduledSignals:
    name = "scheduled"
    parameters: dict[str, object] = {}

    def __init__(self, schedule: dict[int, tuple[Direction, OrderType, float | None]]) -> None:
        self.index = 0
        self.schedule = schedule

    def on_candle(self, candle: Candle, context: StrategyContext) -> SignalEvent | None:
        index = self.index
        self.index += 1
        instruction = self.schedule.get(index)
        if instruction is None:
            return None
        direction, order_type, price = instruction
        return SignalEvent(
            "X",
            direction,
            timestamp=candle.timestamp,
            order_type=order_type,
            limit_price=price if order_type is OrderType.LIMIT else None,
            stop_price=price if order_type is OrderType.STOP else None,
        )

def test_signal_on_bar_n_fills_at_next_bar_open(self) -> None:
    candles = [_bar(0, 100.0, 101.0, 99.0, 100.0), _bar(1, 110.0, 111.0, 109.0, 110.0), _bar(2, 120.0, 121.0, 119.0, 120.0)]
    result = BacktestEngine().run(_BuyOnZeroSellOnOne(), candles, BacktestConfig())
    trade = result.trades[0]
    assert trade.entry_date == candles[1].timestamp
    assert trade.entry_price == pytest.approx(candles[1].open * 1.0005)
    assert trade.exit_date == candles[2].timestamp

def test_limit_order_persists_until_later_bar_crosses(self) -> None:
    candles = [_bar(0, 100, 105, 99, 103), _bar(1, 103, 104, 101, 102), _bar(2, 98, 100, 95, 99), _bar(3, 100, 102, 99, 101)]
    result = BacktestEngine().run(_BuyLimitThenCloseAfterFill(limit=99.0), candles, BacktestConfig())
    assert result.trades[0].entry_date == candles[2].timestamp
    assert result.trades[0].exit_date == candles[3].timestamp

def test_final_bar_signal_is_not_filled(self) -> None:
    result = BacktestEngine().run(_SignalOnlyOnLastBar(), make_candle_series(3), BacktestConfig())
    assert result.trades == []
    assert result.final_equity == result.initial_capital

def test_newer_same_direction_limit_replaces_stale_pending_order(self) -> None:
    strategy = _ScheduledSignals({
        0: (Direction.LONG, OrderType.LIMIT, 90.0),
        1: (Direction.LONG, OrderType.LIMIT, 95.0),
        2: (Direction.SHORT, OrderType.MARKET, None),
    })
    candles = [_bar(0, 100, 101, 99, 100), _bar(1, 100, 101, 98, 100), _bar(2, 94, 96, 93, 95), _bar(3, 96, 97, 95, 96)]
    result = BacktestEngine().run(strategy, candles, BacktestConfig())
    assert result.trades[0].entry_date == candles[2].timestamp

def test_opposite_signal_replaces_unfilled_entry(self) -> None:
    strategy = _ScheduledSignals({
        0: (Direction.LONG, OrderType.LIMIT, 90.0),
        1: (Direction.SHORT, OrderType.MARKET, None),
        2: (Direction.LONG, OrderType.MARKET, None),
    })
    candles = [_bar(0, 100, 101, 99, 100), _bar(1, 100, 101, 98, 100), _bar(2, 99, 100, 98, 99), _bar(3, 98, 99, 97, 98)]
    result = BacktestEngine().run(strategy, candles, BacktestConfig())
    assert result.trades[0].direction is Direction.SHORT
    assert result.trades[0].entry_date == candles[2].timestamp
```

The helpers deliberately close positions on a later bar so tests can assert through existing
`Trade` fields. Do not add open positions to `BacktestResult`.

- [ ] **Step 2: Run engine tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_engine.py -k 'next_bar or persists or final_bar' -q`

Expected: same-bar fills or no persistent order.

- [ ] **Step 3: Validate conditional signals through `Order`**

Remove `# NEW` comments from `SignalEvent`. In `_build_order`, pass through:

```python
return Order(
    symbol=execution_symbol,
    direction=signal.direction,
    quantity=quantity,
    order_type=signal.order_type,
    limit_price=signal.limit_price,
    stop_price=signal.stop_price,
    created_at=signal.timestamp,
)
```

Let `Order.__post_init__` reject missing conditional prices.

- [ ] **Step 4: Implement the run-local pending lifecycle**

At the start of each bar, execute the pending order before marking and before calling the strategy.
Retain only an untriggered LIMIT/STOP. Remove a rejected order. Build `StrategyContext` from the sole
filled position and pending order, call the strategy, then replace the pending strategy order with a
new valid order. Validate candle timestamps with:

```python
if any(current.timestamp <= previous.timestamp for previous, current in pairwise(candles)):
    raise ValueError("candles must have strictly increasing timestamps")
```

When closing, use the actual open position's symbol and quantity rather than the signal's symbol.

- [ ] **Step 5: Update integration fixtures for next-open timing**

Change `_BuyDay0SellDay49Strategy` to sell at index 48 so it closes at bar 49. Compute expected
prices from `candles[1].open` and `candles[49].open`. Update every test strategy to accept
`StrategyContext`.

- [ ] **Step 6: Run engine, walk-forward, and permutation tests**

Run: `uv run --extra dev pytest tests/test_engine.py tests/test_walk_forward.py tests/test_permutation.py -q`

Expected: PASS.

- [ ] **Step 7: Commit next-bar execution**

```bash
git add src/engine/event.py src/engine/backtest.py tests/test_engine.py tests/test_walk_forward.py tests/test_permutation.py
git commit -m "fix: execute queued orders on later bars"
```

### Task 5: Short collateral and borrow accrual

**Files:**
- Modify: `src/engine/backtest.py:34-39`
- Modify: `src/models/portfolio.py`
- Test: `tests/test_models.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces validated margin fields on `BacktestConfig`.
- Produces `Portfolio.accrue_short_borrow(timestamp: datetime) -> float`.
- Produces `Portfolio.maintenance_ratio(symbol: str) -> float | None`.

- [ ] **Step 1: Write failing configuration and short-accounting tests**

```python
@pytest.mark.parametrize("field,value", [("short_initial_margin", 0.99), ("short_maintenance_margin", -0.01), ("annual_short_borrow_rate", -0.01), ("borrow_day_count", 0.0)])
def test_backtest_config_rejects_invalid_margin_values(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        BacktestConfig(**{field: value})

def test_short_entry_reserves_collateral_without_creating_equity(self) -> None:
    portfolio = Portfolio(10_000.0, short_initial_margin=1.5)
    outcome = portfolio.record_fill("X", Direction.SHORT, 10, 100.0, datetime(2023, 1, 2), 1.0)
    assert outcome.accepted
    assert portfolio.cash == pytest.approx(9_499.0)
    assert portfolio.restricted_collateral == pytest.approx(1_500.0)
    assert portfolio.equity == pytest.approx(9_999.0)

def test_short_borrow_uses_previous_mark_and_elapsed_calendar_days(self) -> None:
    portfolio = Portfolio(10_000.0, annual_short_borrow_rate=0.365, borrow_day_count=365.0)
    portfolio.record_fill("X", Direction.SHORT, 10, 100.0, datetime(2023, 1, 2))
    portfolio.update({"X": 100.0}, datetime(2023, 1, 2))
    charged = portfolio.accrue_short_borrow(datetime(2023, 1, 5))
    assert charged == pytest.approx(3.0)
```

- [ ] **Step 2: Run margin model tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_models.py tests/test_engine.py -k 'margin or short or borrow' -q`

Expected: missing configuration and collateral methods.

- [ ] **Step 3: Add finite config validation**

Add the four defaults from Global Constraints and a `BacktestConfig.__post_init__` using
`math.isfinite`. Reject initial margin below one, negative maintenance/borrow, and nonpositive day
count.

- [ ] **Step 4: Implement short collateral accounting**

Track collateral and the last borrow timestamp per short symbol. Entry requires:

```python
notional = quantity * fill_price
additional_margin = notional * (self.short_initial_margin - 1.0)
required_cash = additional_margin + commission
```

Reject without mutation if `required_cash > self._cash`; otherwise deduct required cash and add
`notional * self.short_initial_margin` to restricted collateral. On cover, release that symbol's
collateral and apply `released - quantity * fill_price - commission` to available cash.

- [ ] **Step 5: Implement borrow and maintenance calculations**

Use the exact spec formulas and require a strictly later accrual timestamp. Return zero when there
is no short. `maintenance_ratio(symbol)` returns `None` for no short and otherwise
`self.equity / (quantity * current_price)`. Pass all four margin settings when the engine constructs
`Portfolio`. At the start of each later bar, call `portfolio.accrue_short_borrow(candle.timestamp)`
before executing pending orders so the calculation can use only the previous mark.

- [ ] **Step 6: Run portfolio tests and strict typing**

Run: `uv run --extra dev pytest tests/test_models.py -q`

Run: `uv run --extra dev mypy src/models/portfolio.py src/engine/backtest.py --strict`

Expected: PASS.

- [ ] **Step 7: Commit short accounting**

```bash
git add src/models/portfolio.py src/engine/backtest.py tests/test_models.py tests/test_engine.py
git commit -m "feat: model short collateral and borrow cost"
```

### Task 6: Maintenance-margin forced covers

**Files:**
- Modify: `src/engine/backtest.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `Portfolio.maintenance_ratio()` and pending-order flow.
- Produces: forced MARKET cover queued after a close breach and executed at the next open.

- [ ] **Step 1: Write failing forced-cover tests**

```python
class _OpenShortOnce:
    name = "short-once"
    parameters: dict[str, object] = {}

    def __init__(self) -> None:
        self.submitted = False

    def on_candle(self, candle: Candle, context: StrategyContext) -> SignalEvent | None:
        if self.submitted:
            return None
        self.submitted = True
        return SignalEvent("X", Direction.SHORT, timestamp=candle.timestamp)

def _margin_config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=1_000.0,
        commission_pct=0.0,
        slippage_pct=0.0,
        short_maintenance_margin=0.30,
    )

def _margin_breach_candles(include_cover_bar: bool = True) -> list[Candle]:
    candles = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 100, 1_000, 100, 1_000),
    ]
    if include_cover_bar:
        candles.append(_bar(2, 1_100, 1_110, 1_090, 1_100))
    return candles

def test_margin_breach_forces_cover_at_following_open(self) -> None:
    candles = _margin_breach_candles()
    result = BacktestEngine().run(_OpenShortOnce(), candles, _margin_config())
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == candles[2].timestamp
    assert result.trades[0].exit_price == 1_100.0

def test_final_bar_margin_breach_does_not_invent_cover(self) -> None:
    result = BacktestEngine().run(_OpenShortOnce(), _margin_breach_candles(False), _margin_config())
    assert result.trades == []
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_engine.py -k 'margin_breach' -q`

Expected: no forced cover.

- [ ] **Step 3: Queue forced covers after close marks**

After `portfolio.update`, if the sole short's ratio is below maintenance, replace any strategy
pending order with a `_PendingOrder` containing a MARKET order in `Direction.LONG`, the open
quantity, and `forced_cover=True`. A forced cover remains authoritative until it executes; strategy
signals cannot replace it.

- [ ] **Step 4: Run focused engine tests**

Run: `uv run --extra dev pytest tests/test_engine.py -k 'margin or pending or next_bar' -q`

Expected: PASS.

- [ ] **Step 5: Commit forced covers**

```bash
git add src/engine/backtest.py tests/test_engine.py
git commit -m "feat: enforce maintenance margin on next open"
```

### Task 7: Integrate callers and complete execution regression coverage

**Files:**
- Modify: `src/analytics/permutation_test.py`
- Modify: all tests containing local `on_candle` strategies
- Modify: `tests/test_api.py`
- Modify: `tests/test_walk_forward.py`
- Modify: `tests/test_permutation.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: expanded `BacktestConfig`, context-aware strategy protocol, and next-bar engine.
- Produces: unchanged HTTP and persistence contracts with new numerical semantics.

- [ ] **Step 1: Add failing config propagation and API persistence tests**

```python
def test_permutation_worker_receives_margin_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = BacktestConfig(short_initial_margin=1.6, short_maintenance_margin=0.35, annual_short_borrow_rate=0.05)
    captured: dict[str, object] = {}
    original_config = BacktestConfig

    def recording_config(**kwargs: object) -> BacktestConfig:
        captured.update(kwargs)
        return original_config(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("src.engine.backtest.BacktestConfig", recording_config)
    _run_single_permutation(
        "src.strategies.ma_crossover.MACrossoverStrategy",
        {"fast_period": 2, "slow_period": 3},
        [0.01, -0.01, 0.02],
        [100.0, 101.0, 100.0, 102.0],
        [datetime(2023, 1, day) for day in range(1, 5)],
        "sharpe_ratio",
        asdict(config),
        42,
    )
    assert captured["short_initial_margin"] == 1.6
    assert captured["short_maintenance_margin"] == 0.35
    assert captured["annual_short_borrow_rate"] == 0.05
```

Add an API test that patches deterministic candles, posts a backtest, loads persisted trades, and
asserts the first entry timestamp equals the bar after the signal rather than the signal bar.

- [ ] **Step 2: Run affected tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_permutation.py tests/test_api.py -q`

Expected: worker config omits margin fields or local strategy signatures are incompatible.

- [ ] **Step 3: Carry all config fields and migrate local strategies**

Add the four margin fields to `config_dict` and reconstruct them in `_run_single_permutation`.
Update every test/local strategy method to:

```python
def on_candle(self, candle: Candle, context: StrategyContext) -> SignalEvent | None:
```

Do not change API response or database schemas.

- [ ] **Step 4: Document execution semantics**

Add a README section stating that decisions on a completed daily bar execute no earlier than the
next open, conditional orders are GTC, LIMITs are price-protected, STOPs carry gap risk, and the four
short defaults match Global Constraints.

- [ ] **Step 5: Run the complete execution test surface**

Run: `uv run --extra dev pytest tests/test_engine.py tests/test_models.py tests/test_strategies.py tests/test_walk_forward.py tests/test_permutation.py tests/test_api.py -q`

Run: `uv run --extra dev ruff check src tests`

Run: `uv run --extra dev mypy src --strict`

Expected: PASS.

- [ ] **Step 6: Commit integration**

```bash
git add src/analytics/permutation_test.py tests README.md
git commit -m "test: integrate realistic execution across callers"
```

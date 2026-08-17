# Structured Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add redacted JSON operational logs for backtests, order lifecycle, margin events, data acquisition, walk-forward validation, and permutation failures.

**Architecture:** A small `src/observability.py` module owns JSON formatting, field sanitization, and idempotent runtime configuration. Library modules emit closed event names and safe scalar fields without installing handlers; API, CLI, and dashboard entry points configure output.

**Tech Stack:** Python standard-library `logging` and `json`, pytest `caplog`, FastAPI, argparse, Streamlit.

**Spec:** `docs/superpowers/specs/2026-08-17-execution-realism-design.md`

## Global Constraints

- Logs never contain keys, authorization headers, credential-bearing URLs, frames, provider payloads, or unrestricted external exception text.
- INFO contains lifecycle summaries, DEBUG contains order details, WARNING contains degradation/rejections/margin, and unexpected failures use exception context then re-raise.
- Library modules never configure the root logger.
- `LOG_LEVEL` defaults to `INFO`; unsupported values fall back to `INFO`.
- Preserve `.codegraph/` and do not push.

## File Structure

- Create `src/observability.py`: formatter, sanitizer, `log_event`, and idempotent configuration.
- Create `tests/test_observability.py`: JSON contract, redaction, exception, and configuration tests.
- Modify engine/portfolio analytics modules: emit execution and analysis events.
- Modify `src/data/acquisition.py`: emit cache/provider/fallback/quality/terminal events.
- Modify API, CLI, and dashboard entry points: configure logging once.
- Modify README: document log format and `LOG_LEVEL`.

---

### Task 1: JSON logging and redaction boundary

**Files:**
- Create: `src/observability.py`
- Create: `tests/test_observability.py`

**Interfaces:**
- Produces: `log_event(logger, level, event, **fields) -> None`.
- Produces: `JsonFormatter` and `configure_logging(level: str | None = None) -> None`.

- [ ] **Step 1: Write failing formatter and redaction tests**

```python
def _format_event(event: str, **fields: object) -> dict[str, object]:
    stream = io.StringIO()
    logger = logging.getLogger(f"test.observability.{event}")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    log_event(logger, logging.INFO, event, **fields)
    return json.loads(stream.getvalue())

def test_json_formatter_emits_stable_fields() -> None:
    output = _format_event("backtest.started", run_id="run-1", strategy="ma")
    assert output["event"] == "backtest.started"
    assert output["run_id"] == "run-1"
    assert output["timestamp"].endswith("Z")

def test_secret_shaped_fields_are_redacted() -> None:
    sensitive_fields = {
        "api_key": "fixture-sensitive-value",
        "authorization": "fixture-auth-value",
    }
    output = _format_event("acquisition.failed", symbol="SPY", **sensitive_fields)
    encoded = json.dumps(output)
    assert "fixture-sensitive-value" not in encoded
    assert "fixture-auth-value" not in encoded
    assert output["api_key"] == "[REDACTED]"

def test_external_exception_message_is_not_serialized() -> None:
    output = _format_event("backtest.failed", error=ValueError("fixture-sensitive-value"))
    assert output["error"] == "ValueError"
    assert "fixture-sensitive-value" not in json.dumps(output)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `uv run --extra dev pytest tests/test_observability.py -q`

Expected: module does not exist.

- [ ] **Step 3: Implement the closed JSON formatter**

Use a frozen set of secret-shaped keys (`api_key`, `authorization`, `token`, `password`, `secret`,
`cookie`) and accept only `None`, booleans, strings, finite numbers, and enum values. Convert other
objects to their type name, never `repr`. `log_event` stores fields under a single private
`event_fields` LogRecord attribute. `JsonFormatter.format()` emits UTC ISO 8601 with `Z`, level,
logger, event, sanitized fields, and exception type only.

- [ ] **Step 4: Implement idempotent runtime configuration**

```python
def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    if any(getattr(handler, "_algo_json_handler", False) for handler in root.handlers):
        return
    selected = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    numeric = getattr(logging, selected, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._algo_json_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(numeric)
```

- [ ] **Step 5: Run tests, Ruff, and mypy**

Run: `uv run --extra dev pytest tests/test_observability.py -q`

Run: `uv run --extra dev ruff check src/observability.py tests/test_observability.py`

Run: `uv run --extra dev mypy src/observability.py --strict`

Expected: PASS.

- [ ] **Step 6: Commit the observability boundary**

```bash
git add src/observability.py tests/test_observability.py
git commit -m "feat: add redacted structured logging"
```

### Task 2: Backtest, order, and margin events

**Files:**
- Modify: `src/engine/backtest.py`
- Modify: `src/models/portfolio.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_observability.py`

**Interfaces:**
- Consumes: `log_event` plus the execution/margin plan's pending and fill outcome types.
- Produces: stable event names from spec section 9.

- [ ] **Step 1: Write failing lifecycle log tests**

```python
def test_backtest_emits_start_fill_and_completion(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        BacktestEngine().run(_BuyOnZeroSellOnOne(), [_bar(0, 100, 101, 99, 100), _bar(1, 101, 102, 100, 101), _bar(2, 102, 103, 101, 102)], BacktestConfig())
    events = [getattr(record, "event", None) for record in caplog.records]
    assert events[0] == "backtest.started"
    assert "order.queued" in events
    assert "order.filled" in events
    assert events[-1] == "backtest.completed"

def test_rejected_order_logs_reason_not_payload(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        BacktestEngine().run(_BuyOnZeroSellOnOne(), [_bar(0, 100, 101, 99, 100), _bar(1, 101, 102, 100, 101)], BacktestConfig(initial_capital=1.0))
    record = next(record for record in caplog.records if record.event == "order.rejected")
    assert record.event_fields["reason"] == "insufficient_buying_power"
```

- [ ] **Step 2: Run and confirm RED**

Run: `uv run --extra dev pytest tests/test_engine.py tests/test_observability.py -k 'emits or logs_reason' -q`

Expected: no records with event fields.

- [ ] **Step 3: Emit engine events at their state transitions**

Add module-level `logger = logging.getLogger(__name__)`. Emit `backtest.started/completed/failed`,
`order.queued/filled/untriggered/replaced/rejected/cancelled_end_of_data`, and
`margin.call_queued/call_unresolved`. Wrap only the run body for `backtest.failed`; log
`error_type=type(error).__name__` and re-raise.

- [ ] **Step 4: Emit borrow events without per-bar INFO noise**

When `Portfolio.accrue_short_borrow()` returns a positive value, emit
`margin.borrow_accrued` at DEBUG with symbol, charge, and elapsed days. Do not log on zero charge.

- [ ] **Step 5: Run focused tests and commit**

Run: `uv run --extra dev pytest tests/test_engine.py tests/test_models.py tests/test_observability.py -q`

```bash
git add src/engine/backtest.py src/models/portfolio.py tests/test_engine.py tests/test_observability.py
git commit -m "feat: log execution and margin lifecycle"
```

### Task 3: Acquisition and analytics events

**Files:**
- Modify: `src/data/acquisition.py`
- Modify: `src/analytics/walk_forward.py`
- Modify: `src/analytics/permutation_test.py`
- Test: `tests/test_data_acquisition.py`
- Test: `tests/test_walk_forward.py`
- Test: `tests/test_permutation.py`

**Interfaces:**
- Consumes: `log_event`.
- Produces: acquisition, walk-forward, and permutation event names from spec section 9.

- [ ] **Step 1: Write failing acquisition event tests**

```python
def test_provider_failure_and_fallback_are_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    yfinance = FakeProvider(
        Provider.YFINANCE,
        lambda _: (_ for _ in ()).throw(TransientProviderError("down")),
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE,
        lambda request: native_batch(Provider.ALPHA_VANTAGE, request),
    )
    acquisition, _, _ = service(
        tmp_path,
        {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha},
    )
    with caplog.at_level(logging.WARNING):
        acquisition.acquire(AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 10)))
    events = [record.event for record in caplog.records if hasattr(record, "event")]
    assert "acquisition.fallback" in events
    assert all("secret" not in str(getattr(record, "event_fields", {})).lower() for record in caplog.records)
```

Add focused tests for a cache hit, warning-level quality result, terminal acquisition failure,
invalid walk-forward parameters, and a failed permutation future.

- [ ] **Step 2: Run and confirm RED**

Run: `uv run --extra dev pytest tests/test_data_acquisition.py tests/test_walk_forward.py tests/test_permutation.py -k 'logged or log' -q`

Expected: event records absent.

- [ ] **Step 3: Add safe acquisition events**

Emit `acquisition.cache_result` after range planning, `provider_attempt` before an eligible fetch,
`fallback` when a nonterminal provider/quality failure advances to another provider,
`quality_warning` when an accepted result has warning severity, and `failed` immediately before an
archived terminal raise. Fields are IDs/enums/counts only.

- [ ] **Step 4: Add analysis events**

In walk-forward, log `walk_forward.invalid_parameters` at DEBUG only for the caught `ValueError`.
In permutation execution, log `permutation.failed` at WARNING for failed worker and local runs using
seed and exception type; retain existing statistical behavior in this observability-only plan.

- [ ] **Step 5: Run focused tests and commit**

Run: `uv run --extra dev pytest tests/test_data_acquisition.py tests/test_walk_forward.py tests/test_permutation.py -q`

```bash
git add src/data/acquisition.py src/analytics/walk_forward.py src/analytics/permutation_test.py tests/test_data_acquisition.py tests/test_walk_forward.py tests/test_permutation.py
git commit -m "feat: log acquisition and analysis outcomes"
```

### Task 4: Configure runtime entry points and document operations

**Files:**
- Modify: `src/api/main.py`
- Modify: `src/data/cli.py`
- Modify: `src/dashboard/app.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_data_cli.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `configure_logging`.
- Produces: one idempotent JSON handler per process.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_configure_logging_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    configure_logging("DEBUG")
    configure_logging("DEBUG")
    added = [handler for handler in root.handlers if getattr(handler, "_algo_json_handler", False)]
    assert len(added) == 1
    root.handlers[:] = before
```

Add API lifespan and CLI tests that monkeypatch `configure_logging` and assert one call.

- [ ] **Step 2: Run and confirm RED**

Run: `uv run --extra dev pytest tests/test_observability.py tests/test_api.py tests/test_data_cli.py -k 'logging' -q`

Expected: entry points do not configure logging.

- [ ] **Step 3: Configure only at runtime boundaries**

Call `configure_logging()` at the start of API lifespan and CLI `main()`. Call it once during
dashboard module startup before work is executed. Do not call it from package `__init__.py`.

- [ ] **Step 4: Document JSON output and redaction**

Add README examples for `LOG_LEVEL=DEBUG`, stable event names, UTC timestamps, stdout/stderr
behavior, and the fields that are always redacted.

- [ ] **Step 5: Run integration checks and commit**

Run: `uv run --extra dev pytest tests/test_observability.py tests/test_api.py tests/test_data_cli.py -q`

Run: `uv run --extra dev ruff check src tests`

Run: `uv run --extra dev mypy src --strict`

```bash
git add src/api/main.py src/data/cli.py src/dashboard/app.py tests/test_api.py tests/test_data_cli.py README.md
git commit -m "feat: configure structured runtime logs"
```

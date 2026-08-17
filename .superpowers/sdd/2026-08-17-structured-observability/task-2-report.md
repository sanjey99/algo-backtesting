# Task 2 — execution and margin lifecycle observability

## RED evidence

Command:

```text
uv run --extra dev pytest tests/test_engine.py tests/test_observability.py -k 'emits or logs_reason' -q
```

Result: `2 failed, 2 passed, 69 deselected`. The lifecycle test had no captured
records and the rejection test found no `order.rejected` event.

## GREEN evidence

The engine now emits structured lifecycle records at authoritative pending-order,
fill, rejection, margin-call, end-of-data, and run-boundary transitions. Borrow
accrual logs at the portfolio mutation boundary only when its positive charge is
applied.

## Events, levels, and fields

| Event | Level | Stable fields |
| --- | --- | --- |
| `backtest.started` | INFO | `run_id`, `strategy`, `bar_count` |
| `backtest.completed` | INFO | `run_id`, `strategy`, `symbol`, `trade_count`, `final_equity` |
| `backtest.failed` | ERROR | `run_id`, `error_type` |
| `order.queued`, `order.filled`, `order.untriggered`, `order.replaced`, `order.cancelled_end_of_data` | DEBUG | `run_id`, `symbol`, `order_type`, `direction`, `quantity`, and `order_id` when present; fills add `fill_price` and `commission`; replacements add replacement identity fields |
| `order.rejected` | WARNING | order identity fields plus the typed `reason` |
| `margin.borrow_accrued` | DEBUG | `symbol`, `charge`, `elapsed_days` |
| `margin.call_queued` | WARNING | `run_id`, `symbol`, `quantity`, `maintenance_ratio`, `maintenance_requirement` |
| `margin.call_unresolved` | WARNING | `run_id`, `symbol`, `quantity` |

No per-bar mark is logged at INFO. Failure logging carries only the exception
type field and re-raises the original exception without serializing its message.

## Verification

```text
uv run --extra dev pytest tests/test_engine.py tests/test_models.py tests/test_observability.py -q
111 passed in 0.08s

uv run --extra dev ruff check src/engine/backtest.py src/models/portfolio.py tests/test_engine.py tests/test_observability.py
All checks passed!

uv run --extra dev mypy --strict src
Success: no issues found in 64 source files

uv run --extra dev pytest -q
Passed (641 tests collected)
```

## Self-review

- `backtest.started` is first and `backtest.completed` is last on a successful run.
- A stale pending strategy order logs untriggered/replaced before its replacement
  is queued; a margin forced cover follows the same ordering before its warning.
- A final pending order always logs `order.cancelled_end_of_data`; a final forced
  cover additionally logs `margin.call_unresolved`.
- Each state transition has one emission path, while retained conditional orders
  intentionally emit one DEBUG untriggered event per execution attempt.
- Typed rejection uses only `FillOutcome.rejection_reason`; no outcome or domain
  object is serialized.
- Safe failure coverage proves the original exception propagates and its message
  is absent from the structured log record.

## Concerns

None. `order_id` is included only when an order supplies one; this preserves the
existing engine behavior, which does not assign IDs during order construction.

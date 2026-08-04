# Task 2 — Provider Adapters and Retry Classification

## Implementation

- Added `src.data.providers` with a small provider protocol, no-I/O capability
  planning, and concrete yfinance and Alpha Vantage adapters.  Each adapter
  performs one transport operation and provider-shape parse, returning a
  provider-native `ProviderBatch`; normalization and retries remain outside
  this boundary.
- The yfinance adapter requests `actions=True`, `auto_adjust=False`, and
  translates the public inclusive end date to yfinance's exclusive end date.
  It accepts flat and single-symbol MultiIndex provider frames without
  normalizing, sorting, or deduplicating them.
- The Alpha adapter has explicit API-key and adjusted-daily entitlement
  eligibility, output-size coverage planning, redacted metadata, provider
  envelope mapping, parameter copying, configurable local daily budget, and
  configurable process-local pacing.  Its controls are explicitly not a
  distributed/global rate limiter.
- Added `RetryExecutor`, which owns injected clock/sleeper/randomness and
  classifies failures through exception types: transient failures retry,
  quota/entitlement failures remain fallbackable, and all other errors are
  terminal.
- Retained `DataFetcher`, `YFinanceFetcher`, and `AlphaVantageFetcher` import
  and dataframe-returning compatibility.  The legacy Alpha wrapper enriches a
  private copy of historical six-field mocked payloads with its old action
  defaults; the new adapter itself remains fail-closed on missing action
  fields.
- Added redacted, hand-derived provider fixtures plus deterministic behavior
  tests.  No test contacts a live provider.

## Contract compatibility correction

`ProviderCapabilities.output_size` was added because the approved Task 1
capability contract had no field capable of expressing Alpha Vantage compact
versus full coverage.  `DataAcquisitionError` now redacts its own message,
which is required to ensure provider/retry exceptions cannot retain API keys
outside Task 1's existing manifest/evidence redaction boundary.

## Files

- `src/data/contracts.py`
- `src/data/fetcher.py`
- `src/data/providers/__init__.py`
- `src/data/providers/base.py`
- `src/data/providers/yfinance.py`
- `src/data/providers/alpha_vantage.py`
- `src/data/retry.py`
- `tests/test_data_providers.py`
- `tests/test_data_retry.py`
- `tests/fixtures/market_data/*`

## TDD evidence

### RED

Command:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q tests/test_data_providers.py tests/test_data_retry.py
```

Output before implementation:

```text
ModuleNotFoundError: No module named 'src.data.providers'
ModuleNotFoundError: No module named 'src.data.retry'
2 errors in 0.46s
```

The first sandboxed invocation could not open the pre-existing uv cache;
the rerun used the approved `uv run` cache access and reached the expected
missing-module RED state.

### GREEN / focused regression

Command:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q tests/test_data.py tests/test_api.py tests/test_data_providers.py tests/test_data_retry.py
```

Output:

```text
73 passed, 6 warnings in 1.30s
```

## Verification

### Lint

```bash
uv run --extra dev ruff check src/data/contracts.py src/data/fetcher.py src/data/providers src/data/retry.py tests/test_data_providers.py tests/test_data_retry.py
```

Output: `All checks passed!`

### Types

```bash
uv run --extra dev mypy --python-version 3.12 src/data/contracts.py src/data/fetcher.py src/data/providers src/data/retry.py
```

Output: `Success: no issues found in 7 source files`

### Focused coverage

Command (with the Task 1 documented pandas preload for the NumPy loader
environment):

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c 'import pandas; import coverage; import pytest; cov = coverage.Coverage(source=["src.data.providers", "src.data.retry"]); cov.start(); status = pytest.main(["-q", "tests/test_data_providers.py", "tests/test_data_retry.py"]); cov.stop(); cov.save(); cov.report(show_missing=True); raise SystemExit(status)'
```

Output: `17 passed in 0.20s`; provider/retry total coverage was `83%` (224 of
270 statements), exceeding the 80% requirement.

### Full suite

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q
```

Output: `280 passed, 18 warnings in 2.95s`.

## Self-review

- Provider metadata contains endpoint/capability facts only; API keys are
  omitted and exception text is redacted at the acquisition-error boundary.
- Adapter request construction copies caller mappings, and raw dataframes are
  neither normalized nor mutated at the provider boundary.
- Response envelopes and daily rows are validated fail-closed before creating
  a batch.  Local quota/pacing state is instance/process-local and configurable.
- Retry decisions are entirely type-based; no downstream error-message
  matching is used.
- The behavior suite covers actions, flat/MultiIndex frames, empty/malformed
  data, response envelopes, entitlement/coverage skips, retries, fallbackable
  failures, bounded Retry-After, and credential redaction.

## Concerns

The existing suite retains its 18 pre-existing warnings.  The new focused
coverage measurement required the documented pandas preload because this
environment's coverage tracer can otherwise hit the known NumPy loader issue.

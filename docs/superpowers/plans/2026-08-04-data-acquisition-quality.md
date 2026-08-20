# Market Data Acquisition and Quality Layer Implementation Plan

> Implement with `superpowers:test-driven-development` task by task. Keep provider network access
> mocked in tests and commit after each green task.

**Goal:** Deliver a daily US-equity acquisition subsystem with capability-aware fallback,
canonical validation, exchange-session gap detection, partial Parquet caching, lineage reports,
and reproducible measurements.

**Baseline:** Python 3.12; 245 tests pass. Preserve current API defaults and public fetcher imports.

## File Map

Create:

- `src/data/contracts.py` — enums, dataclasses, policies, typed errors, JSON-safe primitives
- `src/data/providers/base.py` — provider protocol and capability contract
- `src/data/providers/yfinance.py` and `alpha_vantage.py` — concrete adapters
- `src/data/normalization.py` — source-to-canonical conversion and duplicate resolution
- `src/data/quality.py` — pure row/dataset checks, coverage, corporate-action findings
- `src/data/calendars.py` — exchange-calendar protocol and pandas implementation
- `src/data/retry.py` — injected retry executor and failure classification
- `src/data/acquisition.py` — cache-first orchestration, fallback, merge, final validation
- `src/data/manifest.py` — versioned serialization, redaction, report repository
- `src/data/cli.py` and `src/data/benchmark.py` — verification interfaces
- `tests/fixtures/market_data/` — redacted golden and deterministic benchmark inputs
- Focused `tests/test_data_*` unit and integration modules
- `docs/data-acquisition-quality.md` — operator and interview guide

Modify:

- `src/data/fetcher.py` — compatibility re-exports; remove policy from adapters
- `src/data/store.py` — immutable generation cache with optimistic rebasing
- `src/data/__init__.py` — strict canonical-to-Candle conversion and exports
- `src/api/schemas.py`, `src/api/routes/data.py`, `src/api/routes/backtest.py` — shared service
- `pyproject.toml`, `README.md`, and `Makefile` — calendar dependency and demo commands

## Dependency Order

```text
Task 1 contracts/calendar
  -> Task 2 adapters/retry
  -> Task 3 normalization/quality
  -> Task 4 cache/manifests
  -> Task 5 acquisition/fallback
  -> Task 6 Candle/API/CLI
  -> Task 7 benchmark/docs
  -> Task 8 full verification
```

## Task 1: Contracts and Calendar Boundary

### Tests first

Create `tests/test_data_contracts.py` and `tests/test_data_calendars.py`.

Assert:

- `AcquisitionRequest` normalizes a safe symbol and rejects traversal, empty symbols, inverted
  ranges, unsupported intervals, calendars, and source names.
- All public statuses and providers serialize to stable string enums.
- `QualityPolicy` defaults are 0.98 coverage, two consecutive missing sessions, and recorded
  corporate-action tolerances.
- `RetryPolicy` defaults are three attempts, 0.5-second base, and 8-second cap.
- XNYS expected sessions exclude weekends and known holidays and use normalized timezone-naive
  daily labels.
- Contiguous missing sessions are grouped by adjacency in the exchange schedule, not calendar days.
- Calendar and dependency versions are exposed for manifest evidence.

Run the focused tests and confirm they fail because contracts do not exist.

### Implement

Define immutable dataclasses and closed enums for requests, capabilities, batches, findings,
rejected rows, attempts, cache evidence, manifests, and results. Define the typed exception
hierarchy from the specification. Avoid pandas frames in JSON serializers; keep them only in
in-process batch/result types.

Add `pandas_market_calendars` and `filelock` to runtime dependencies. Wrap the calendar behind
`MarketCalendar` so tests can
use a deterministic fake. V1 accepts only `XNYS` and `1d`; adding a calendar later requires a
registry entry rather than arbitrary import or string dispatch.

Run focused tests, then the full suite. Commit:

```text
feat(data): define acquisition contracts and calendars
```

## Task 2: Provider Adapters and Retry Classification

### Fixtures and failing tests

Add small redacted fixtures for:

- yfinance flat and MultiIndex columns, empty output, actions, unsorted dates, and duplicate rows
- Alpha Vantage adjusted-daily success, `Error Message`, `Note`, `Information`, malformed schema,
  compact coverage, and action fields

Assert yfinance calls `download()` with `actions=True`, `auto_adjust=False`, and end date plus one
day. Assert provider-native frames are retained in `ProviderBatch`, credentials are absent from
metadata, and capabilities declare daily/action support.

Assert Alpha Vantage rejects non-daily requests before HTTP, requires key plus entitlement for
eligibility, does not mutate caller parameter dictionaries, maps response envelopes to typed
errors, and records output-size capability. Assert capability planning skips an Alpha configuration
whose compact/full entitlement cannot cover the requested historical range before making HTTP.

Test `RetryExecutor` with fake clock/sleeper/random source:

- two transient failures then success produces exact attempt and delay evidence
- terminal errors invoke once
- quota/entitlement invoke once and remain fallbackable
- excessive `Retry-After` does not sleep beyond the configured bound
- API keys never appear in exceptions, attempts, or string representations

### Implement

Move concrete behavior into provider modules. Keep `src.data.fetcher.DataFetcher`,
`YFinanceFetcher`, and `AlphaVantageFetcher` compatibility imports during migration. Provider
adapters do one network operation and response parsing; retry is an external decorator/executor.

Use explicit failure classification instead of string matching in the acquisition service. Alpha
Vantage uses configurable daily budget and process-local pacing; document that it is not globally
coordinated.

Run focused and full tests. Commit:

```text
feat(data): add capability-aware provider adapters
```

## Task 3: Canonical Normalization and Quality Evaluation

### Failing tests

Create exact-shape cases for every provider mapping. Assert the canonical columns and dtypes match
the approved order. Test timestamps with naive dates, timezone-aware provider timestamps, duplicate
normalization, stable sorting, and out-of-range filtering.

Add table-driven quality cases for:

- missing, nonnumeric, NaN, and infinite fields
- high below open/close, low above open/close, nonpositive prices, and negative volume
- missing actions versus provider-declared action coverage
- identical and conflicting timestamp duplicates
- an identical duplicate group with invalid OHLC that becomes one rejected unique row, plus a
  separate invalid unique row, proving the deduplicate-then-validate equations
- holiday/weekend rows, missing expected sessions, acceptable warnings, insufficient coverage,
  and excessive consecutive gaps
- finite/invalid adjustment factors, deterministic action signatures, and cached revisions
- exact row-count and expected-session reconciliation formulas
- missing, unparsable, and invalid-timezone timestamps in a pre-range unclassifiable bucket
- one-session and two-session partial ranges that are structurally usable even though range-local
  coverage would be below 98%; coverage applies only after the complete request merge

Assert warning candidates return a normalized accepted frame and findings. Assert any fatal result
does not expose a consumable frame.

### Implement

Make normalization a pure transformation returning normalized candidates and diagnostic counters.
Use stable provider row numbers solely for duplicate diagnostics; omit them from canonical output.
Do not use broad `errors="coerce"` without recording every rejected source row.

Make quality evaluation pure and policy-driven. Parse/classify timestamps, map canonical values,
resolve duplicate groups, then validate remaining unique rows. Exact duplicate rows retain the
first; an invalid identical group counts removed duplicates plus one rejected unique row.
Conflicting groups count every member and produce a fatal finding. Assert the four approved
reconciliation equations. Range candidates apply structural checks only. Calculate coverage and
consecutive gaps once over the final merged expected sessions.

V1 corporate-action checks require finite positive adjustment factors and hash sorted canonical
action tuples. Report every cached-versus-refreshed change, treating values as equal only under
`math.isclose(rel_tol=1e-9, abs_tol=1e-12)`. Do not classify price moves, invoke existing adjustment
functions, or recompute adjusted prices.

Run focused and full tests. Commit:

```text
feat(data): validate canonical market data quality
```

## Task 4: Immutable Cache Generations and Manifest Repository

### Failing tests

Use `tmp_path` and injected locks/IDs/clocks. Assert:

- validated symbols map to contract/calendar/interval/symbol namespaces
- an empty cache reports a miss without creating files
- publication stages Parquet, metadata, and final success manifest, hashes each, fsyncs files and
  directories, and atomically updates `CURRENT.json`
- a reader verifies pointer schema, referenced files, every hash, canonical contract, and calendar
  version
- corrupt pointer, missing generation, hash mismatch, and incompatible versions report
  invalidation without returning data
- failures before pointer replacement preserve the prior generation
- readers never select an unreferenced or partially written generation
- cleanup retains the active and immediately prior valid generation, never removes an unarchived
  pinned generation manifest, and treats cleanup failure as a warning
- a stale-base concurrent writer rebases onto the new `CURRENT`, revalidates, and preserves both
  writers' nonconflicting ranges; three repeated conflicts fail without publication

Manifest tests assign IDs only after syntactic request admission and assert every admitted success,
full hit, and failure serializes to versioned JSON with deterministic key
order, ISO UTC times, range-level lineage, policy values, attempts, counts, findings, hashes, and
status. Recursively scan output to prove known secret values and `apikey` query parameters are
absent. Test full-hit report persistence, indefinite request-report lookup after generation cleanup,
archive failure with embedded-manifest fallback/pinning, and successful maintenance archival/unpin.

**Platform note (2026-08-20):** POSIX fsyncs files and leaf publication directories, providing
stronger crash ordering when the ancestor hierarchy is already durable; recursively created
ancestors are not individually fsynced. Windows rejects directory descriptors through Python's
`os.open`; there publication is namespace-atomic during normal operation, file contents are fsynced,
and sudden-power-loss durability of directory metadata is best-effort.

### Implement

Refactor `DataStore` behind a canonical generation-store interface while preserving legacy
`get/save/fetch_or_cache` until route migration is complete. Write temporary artifacts inside the
target filesystem, flush them and their directories, and use `os.replace` for the pointer. Use a
cross-process `filelock` only around compare/rebase/validate/publish, not network fetch. Pointer
hashes cover Parquet, metadata, and success manifest. Lineage segments contain inclusive range,
provider, acquired-at time, action coverage, content hash, and action signature.

The final success publication manifest is transactionally part of the generation. A separate
immutable request-report archive retains every admitted request, including full hits and failures,
independently and indefinitely in V1. After a cache commit, archive the same report atomically. If
that archive fails, pin the generation against cleanup, let lookup fall back to the embedded
manifest, return a secondary warning, and retry archival during maintenance. For a full hit or
failure, archive failure is a typed artifact error. Invalid input rejected before admission receives
no acquisition ID. A caller-selected copy remains an optional post-commit artifact.

The platform note above also qualifies the archive's indefinite-retention guarantee across sudden
power loss; normal-operation lookup, pinning, and maintenance semantics are unchanged.

Run tests and commit:

```text
feat(data): publish versioned parquet cache generations
```

## Task 5: Acquisition Orchestration, Partial Hits, and Fallback

### Failing integration tests

Build fake providers, calendar, cache, retry executor, and manifest repository. Cover:

- full fresh hit makes zero provider calls
- miss uses yfinance, validates, publishes, and reports success
- partial hit groups only missing exchange sessions and preserves cached rows
- one-session and two-session partial fetches pass structural range validation before final merged
  coverage evaluation
- recent and historical TTL classification uses the injected clock
- latest completed session uses XNYS scheduled close plus the configured 30-minute availability lag
- stale refresh overlaps five expected sessions; accepted refreshed rows win overlap
- recent overlap action change and stale historical action comparison reacquire requested history
- warning-level primary is accepted without fallback
- fatal primary is discarded and eligible Alpha Vantage succeeds
- unavailable Alpha entitlement is recorded as skipped rather than attempted
- retry exhaustion falls back; terminal request errors do not
- all-source failure publishes no cache generation but writes a failed manifest
- a failed range in a multi-range request produces no partially published merged generation
- merged row-level sources and range lineage are exact
- final merged validation occurs before publication
- two writers based on the same generation rebase under the publication lock without losing either
  valid nonoverlapping update

### Implement

Add `AcquisitionService.acquire(request) -> AcquisitionResult`. Use a closed provider registry and
explicit eligibility reasons. Determine cache coverage from calendar sessions, not filename dates.
Fetch and structurally validate all needed ranges into memory before publishing one new generation.
Record the base generation; under the publication lock, rebase on a changed current generation and
rerun final merged validation. After three conflicting rebases, return a typed concurrency failure.

Use all-or-nothing publication for a request. `partial_success` means usable data with warnings, not
an incompletely acquired range below the approved quality threshold.

Run tests and commit:

```text
feat(data): orchestrate fallback and partial acquisition
```

## Task 6: Strict Candle Boundary, API, and CLI

### Failing tests

Assert `df_to_candles()` converts every validated canonical row in order and raises a typed contract
error for missing or invalid fields; it must never continue silently.

API tests assert old `DataFetchRequest` bodies remain valid, new defaults are stable,
`use_cache=false` forces refresh, mixed cached lineage returns `sources_used` plus optional
`selected_source`, the compact summary has exact fields, and report lookup returns a
redacted manifest. Test 400, 422, 429, 502, and cache-publication error mappings. Verify the backtest
route invokes the acquisition service while retaining its current response shape.

CLI tests invoke `main(argv)` without subprocesses and assert:

- `acquire` writes canonical/report artifacts and prints acquisition ID
- `inspect` loads an existing report
- bad arguments, provider exhaustion, fatal quality, and artifact errors use exit codes 2–5
- JSON output has stable schema and no secrets

### Implement

Use dependency providers/factories in FastAPI rather than constructing yfinance inside routes.
Keep the full frame out of API JSON. Add optional/defaulted request fields so this is additive.

Implement the CLI with `argparse`; keep orchestration in services. Artifact paths are explicit and
published atomically. Add Make targets for deterministic demos.

Run tests and commit:

```text
feat(data): expose acquisition reports through api and cli
```

## Task 7: Deterministic Benchmark and Documentation

### Failing tests

Test benchmark configuration validation, deterministic payload hashes, scenario ordering, measured
sample count, percentile calculation, environment capture, exact scenario counters, and separation
of optional live smoke results. Patch the timer with deterministic values for result tests.

The fixed matrix uses XNYS/1d, SPY/AAPL/MSFT, 2020-01-01 through 2024-12-31, seed 42, three
warmups, and fifteen measured runs for the ten approved scenarios. Compute actual session/row counts
at runtime and assert internal reconciliation, not invented constants. Construct a fresh isolated
cache/provider fixture before every warmup and measured repetition, exclude setup from timing, and
assert scenario preconditions plus expected provider-call/cache-status trace for every sample.

### Implement

Generate provider-shaped payloads from expected sessions without copying proprietary market data.
Capture Python/platform and exact dependency versions, calls, counts, Parquet bytes, individual
timings, median/p95 summaries, findings, and hashes. Never compare network and deterministic timing.

Document the analytical question, provider limitations, contract, rules, manifests, demo commands,
benchmark method, and claim discipline in `docs/data-acquisition-quality.md` and README.

Run the deterministic benchmark to create a local artifact only; transcribe observed values into
documentation only when the artifact is retained and reproducible. Commit:

```text
docs: add market data quality verification guide
```

## Task 8: Packaging, Compatibility, and Final Verification

Build a wheel and install it into a temporary environment. From outside the repository, import the
provider compatibility paths, load calendar support, execute fixture acquisition, write/read a
Parquet generation, and run CLI help. This catches missing fixtures/resources and accidental source
tree dependence.

Run:

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check src tests
uv run --extra dev mypy src
uv build
```

Also run the CLI demo twice to demonstrate cold then warm-cache behavior, validate every emitted
JSON file against its contract, and scan artifacts for configured secret values.

Confirm:

- no live request is required by tests or benchmark
- all tracked fixtures are redacted and license-safe
- old import paths and request bodies still work
- no generated cache, manifest, benchmark output, or credential is tracked
- documentation contains observed measurements only when backed by retained commands/artifacts
- PostgreSQL, real-time, distributed rate limiting, and performance improvement are not claimed

Commit final compatibility corrections with an accurately scoped message.

## Rollback and Migration Notes

The cache namespace includes its contract version, so the new implementation need not mutate old
exact-range Parquet files. Legacy reads may remain available during transition, but new canonical
writes use generations only. Rolling back code leaves legacy files untouched and versioned new
caches safely ignorable. API additions are defaulted; removal of legacy store methods and request
fields is explicitly deferred.

# Robust Multi-Source Market Data Acquisition and Quality Layer

**Status:** Approved
**Date:** 2026-08-04
**Repository:** `algo-backtesting`
**Branch:** `blueprint/data-acquisition-quality`

## 1. Problem and Users

The repository can download daily OHLCV data from yfinance and Alpha Vantage and cache exact
date-keyed Parquet files. It does not yet expose an explicit source-selection policy, reconcile
provider schemas through a common boundary, explain silently discarded rows, distinguish market
closures from missing bars, support partial cache hits, or persist acquisition lineage.

The primary users are:

- A strategy developer who needs trustworthy, reproducible daily bars.
- A maintainer diagnosing why a backtest used a particular source or rejected data.
- A recruiter or interviewer verifying direct evidence of API integration, validation, caching,
  fallback, lineage, and performance measurement.

The feature is daily US-equity acquisition for portfolio demonstration. Intraday streaming,
Kafka, cloud storage, a warehouse, and a general market-data platform are deferred.

## 2. Implemented Baseline

The implemented repository is authoritative:

- `DataFetcher` returns pandas frames with six nominal canonical columns.
- `YFinanceFetcher` calls `yfinance.download(auto_adjust=False)` and handles a single-ticker
  MultiIndex, but does not request actions, sort, deduplicate, validate, retry, or record lineage.
- `AlphaVantageFetcher` calls `TIME_SERIES_DAILY_ADJUSTED`, sleeps to approximate five calls per
  minute, filters the full response locally, and drops dividend and split fields.
- `DataStore` uses exact `(symbol, start, end)` Parquet filenames. It has no partial hits,
  freshness, atomic bundle publication, schema version, or manifest.
- `df_to_candles()` catches `KeyError` and `ValueError` and silently skips invalid rows.
- The data and backtest API routes instantiate yfinance directly, so Alpha Vantage is not a real
  fallback.
- Adjustment helpers are standalone and are not part of acquisition.
- No `data/raw` cache exists in the inspected repository checkout.
- The Python 3.12 baseline is 245 passing tests with 18 deprecation warnings.

## 3. Provider Research and Constraints

### 3.1 yfinance

- `download()` documents `start` as inclusive and `end` as exclusive.
- It supports daily and intraday intervals, but intraday history cannot extend beyond the last 60
  days.
- `actions=True` exposes dividend and stock-split information; `auto_adjust=False` preserves raw
  OHLC and adjusted close.
- Multi-level columns remain an expected return shape.
- yfinance is an unofficial wrapper around Yahoo's public interfaces and is intended for research
  and personal use; it is not an exchange-grade SLA.

Sources:

- <https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html>
- <https://ranaroussi.github.io/yfinance/advanced/price_repair.html>
- <https://ranaroussi.github.io/yfinance/index.html>

### 3.2 Alpha Vantage

- `TIME_SERIES_DAILY_ADJUSTED` returns raw OHLCV, adjusted close, dividend amount, and split
  coefficient, but is currently documented as a premium endpoint.
- The general free service is documented as up to 25 requests per day. Premium entitlements have
  plan-specific limits, so the repository's hard-coded five-per-minute assumption is not a current
  universal contract.
- Daily responses use provider-specific numbered field names and may encode errors, throttling, or
  entitlement information in a successful JSON response.
- `outputsize=compact` and `full` have different coverage and entitlement behavior.

Sources:

- <https://www.alphavantage.co/documentation/>
- <https://www.alphavantage.co/support/>

### 3.3 Trading calendars

`pandas_market_calendars` supplies exchange schedules and `valid_days()` for distinguishing
weekends and holidays from missing sessions. Calendar data comes from the installed library, not a
live exchange feed, so its version is part of lineage.

Sources:

- <https://pandas-market-calendars.readthedocs.io/en/latest/usage.html>
- <https://www.nyse.com/markets/hours-calendars>

## 4. Approaches Considered

### 4.1 Expand `DataFetcher` and `DataStore`

Smallest file count, but fetching, retry, normalization, validation, fallback, lineage, and cache
policy would become coupled. Rejected because the boundaries would be difficult to test or explain.

### 4.2 Separate acquisition and quality pipeline — selected

Provider adapters return typed batches. A service performs capability-aware selection, retry,
normalization, validation, fallback, merging, and reporting. Storage publishes canonical Parquet
and metadata. This is the smallest option with strong observable boundaries.

### 4.3 Permanent raw-to-canonical data lake

Maximum replayability, but retaining every raw payload creates storage lifecycle, redaction, and
multi-zone management beyond portfolio scope. Rejected; small redacted golden fixtures provide
adapter reproducibility instead.

## 5. Architecture and Data Flow

```text
AcquisitionRequest
  -> validate request and provider capabilities
  -> inspect cache coverage and freshness
  -> derive missing/stale exchange-session ranges
  -> select eligible provider
  -> retry provider adapter when failure is transient
  -> normalize ProviderBatch
  -> deduplicate and validate rows
  -> evaluate coverage and corporate-action consistency
  -> accept warnings or reject fatal candidate and fall back
  -> merge with validated cached rows
  -> validate the complete result
  -> publish immutable Parquet generation and metadata
  -> persist redacted AcquisitionManifest
  -> return AcquisitionResult to CLI, API, or Candle conversion
```

Responsibilities:

- Provider adapters translate transport and provider response shapes into `ProviderBatch`.
- The normalizer owns names, dtypes, timestamps, and action-field mapping.
- The quality evaluator owns rules and severity; it never performs I/O.
- The calendar adapter owns expected-session calculation.
- The acquisition service owns orchestration, retry, fallback, and merge precedence.
- The store owns cache locking, generation verification, and atomic publication.
- The manifest writer owns versioned JSON and credential redaction.
- API and CLI handlers remain thin consumers.

## 6. Public Contracts

### 6.1 Acquisition request

`AcquisitionRequest` contains normalized symbol, inclusive start and end session dates, interval,
calendar ID, source preference, cache preference, and refresh flag.

- V1 accepts interval `1d` only.
- V1 defaults to calendar `XNYS`.
- Source preference is `auto`, `yfinance`, or `alpha_vantage`.
- Symbols must match a closed safe grammar before being used in paths or providers.

### 6.2 Provider batch

`ProviderBatch` contains provider enum, request, provider-shaped frame, received timestamp, native
timezone, raw row count, response metadata, and declared action coverage. It never contains API
keys or credential-bearing URLs.

### 6.3 Canonical frame

There is one row per symbol and exchange session in exact order:

| Field | Type | Constraint |
|---|---|---|
| `timestamp` | `datetime64[ns]` | timezone-naive normalized session label; unique |
| `symbol` | string | normalized; matches request |
| `open` | `float64` | finite and greater than zero |
| `high` | `float64` | finite and greater than zero |
| `low` | `float64` | finite and greater than zero |
| `close` | `float64` | finite and greater than zero |
| `volume` | `float64` | finite and nonnegative |
| `adj_close` | `float64` | finite and greater than zero |
| `dividend_amount` | `float64` | finite and nonnegative |
| `split_coefficient` | `float64` | finite and greater than zero |
| `source` | provider enum string | required row lineage |

Daily timestamps are session labels rather than precise instants. Provider and exchange timezones
are recorded in the manifest. Intraday timezone behavior requires a future contract version.

### 6.4 Result and manifest

`AcquisitionResult` contains the validated canonical frame and `AcquisitionManifest`. The manifest
has a schema version, acquisition ID, normalized request, policy snapshot, environment versions,
cache evidence, attempts, retry delays, transformations, findings, reconciliation counters,
coverage intervals, range-level source lineage, output hash, duration, and final status. Each
lineage segment records inclusive range, provider, acquisition time, action-coverage status, frame
content hash, and action signature.

Closed final statuses are `success`, `partial_success`, and `failed`. Cache statuses are `miss`,
`full_hit`, `partial_hit`, `stale_refresh`, `forced_refresh`, and `invalidated`.

## 7. Source Selection, Retry, and Fallback

1. Validate the request before cache or network access.
2. Return a full fresh cache hit without constructing providers.
3. Build candidates from source preference, credentials, entitlement, interval, and range.
4. In `auto`, prefer yfinance and consider Alpha Vantage second.
5. Alpha Vantage is eligible only with an API key and explicit adjusted-daily entitlement flag.
6. Fetch one contiguous missing/stale range from one provider at a time.
7. Accept a warning-level candidate; discard an entire fatal candidate and try the next eligible
   provider.
8. If every candidate fails, return a typed failure and complete attempt manifest without
   publishing a valid-looking cache generation.

Retry connection failures, timeouts, bounded HTTP 429 responses, HTTP 5xx, and temporary provider
throttle responses. Defaults are three attempts, exponential full jitter, 0.5-second base, and
8-second cap. Clock, sleep, and randomness are injected.

Do not retry invalid requests, authentication, entitlement, long-lived quota exhaustion, permanent
response schemas, empty normalized data, or fatal quality findings. They may still permit fallback.
Honor `Retry-After` only within a configured maximum wait.

Alpha Vantage defaults to a conservative local 25-call daily ceiling and configurable process-local
pacing. Provider responses remain authoritative. This is not a distributed quota system.

## 8. Normalization and Quality Rules

Normalization preserves provider row order and an internal source-row number, maps fields, parses
timestamps, filters the inclusive range, maps canonical values, stably sorts, resolves duplicate
groups, and then validates each remaining unique row. Missing, unparsable, or invalid-timezone timestamps increment a
disjoint `timestamp_unclassifiable_rows` counter before range classification.

- Identical canonical duplicates retain the first row and increment `duplicates_removed`.
- Conflicting duplicates are fatal; the system does not guess which price is correct.
- Missing or nonnumeric OHLCV/action fields reject the row.
- Defaults of zero dividend and one split are allowed only when the adapter confirms actions are
  represented by the provider response.
- Invalid OHLC relationships, nonpositive prices, negative volume, NaN, and infinity reject rows.
- An accepted timestamp outside the chosen exchange calendar is a fatal calendar/source mismatch.
- Final frames must be ordered, unique, exact-schema, single-symbol, and single-interval.

Provider-range evaluation applies structural rules only: usable schema, parseable rows, no
conflicting duplicates, and at least one accepted row. Expected-session coverage is evaluated only
after all ranges are merged over the complete requested session set. Defaults are 98% minimum
coverage and no more than two consecutive missing sessions. Smaller final gaps are warnings;
violating either final threshold is fatal. Both values are configurable and recorded.

V1 corporate-action validation deliberately avoids subjective price-move thresholds. It requires
finite positive `adj_close / close`, finite nonnegative dividends, and finite positive split
coefficients. The action signature is SHA-256 over canonical JSON of sorted
`(timestamp, dividend_amount, split_coefficient)` tuples. Refresh comparison reports every changed
OHLCV/action value, using `math.isclose(rel_tol=1e-9, abs_tol=1e-12)` only to suppress serialization
noise. It does not classify market moves or recompute provider adjustments.

Severity is:

- `INFO`: expected mapping, sorting, filtering, cache assembly, and transformations.
- `WARNING`: exact duplicate removal, isolated rejected rows with sufficient coverage, limited gaps,
  and observed cached-versus-refreshed revisions.
- `FATAL`: unusable schema, conflicts, wrong calendar/symbol, insufficient coverage, no accepted
  rows, or failed canonical contract.

Reconciliation invariants are:

```text
provider_rows = timestamp_unclassifiable_rows + out_of_range_rows + in_range_rows
in_range_rows = dedupe_input_rows
dedupe_input_rows = exact_duplicate_rows_removed + conflicting_duplicate_rows + nonconflicting_unique_rows
nonconflicting_unique_rows = accepted_unique_rows + rejected_unique_rows
expected_sessions = accepted_expected_sessions + missing_sessions
```

`conflicting_duplicate_rows` counts every member of conflicting timestamp groups and makes the
candidate fatal. Exact duplicates are collapsed before row validation, so an identical invalid
group contributes removed duplicate members plus one `rejected_unique_rows` member.

## 9. Cache and Freshness

Cache keys are contract version, calendar, interval, and validated symbol. Each entry uses immutable
generation directories containing `bars.parquet`, `cache-metadata.json`, and the final success
`acquisition-manifest.json`; an atomically replaced `CURRENT.json` points to the active generation
and contains hashes for all three artifacts.

A writer records the base generation, prepares network results without a lock, then acquires a
cross-process `filelock` publication lock. If `CURRENT` changed, it rebases the accepted ranges onto
the latest generation and revalidates; after three conflicting rebases it fails without publishing.
It stages and fsyncs data, metadata, and the success manifest before replacing the pointer. On
POSIX systems it also fsyncs the leaf publication directories, providing stronger crash ordering
when the ancestor hierarchy is already durable. Recursively created ancestors are not individually
fsynced. On Windows, Python does not expose a supported directory-fsync descriptor, so replacement
remains namespace-atomic during normal operation but directory metadata durability across sudden
power loss is best-effort. A reader follows only the pointer and verifies schema version, file
existence, and every hash, failing closed if a persisted pointer is incomplete.

For partial hits, derive missing sessions, group them into contiguous ranges, acquire and validate
each, let validated refreshed rows replace overlap, merge unchanged cached rows, and validate the
complete result before publication.

The latest completed session is the last XNYS session whose scheduled close plus a configurable
30-minute availability lag is not later than the injected UTC clock. Freshness is evaluated per
lineage segment, not from one generation timestamp. Defaults are one hour for segments touching the
latest completed session and seven days for historical segments. Recent refresh overlaps five
expected sessions; appearance or change of an action tuple triggers full requested-range refresh.
A stale historical segment is reacquired for its complete requested historical range, allowing a
full action-signature comparison. Explicit refresh bypasses TTL. Contract/calendar mismatch
invalidates compatibility.

Assign an acquisition ID only after syntactic request validation admits the request. The success
publication manifest is part of an atomic cache generation, while a separate immutable request-
report archive records every admitted request during normal operation, including full hits and
failures. For cache writes, publish the generation first and then namespace-atomically archive the
identical report. If archival fails, the committed generation is pinned against cleanup, lookup
falls back to its embedded manifest, and maintenance retries archival; the acquisition returns a
secondary artifact warning rather than pretending cache publication failed. Full-hit/failure
report-write failure is a typed artifact error because no new cache mutation needs protection.
Report retention is independent of generation cleanup and indefinite in V1, subject to the platform
durability limitations above. Caller-selected copies are optional post-commit artifacts.

## 10. Interfaces and Error Handling

The existing `DataFetchRequest` gains defaulted `source`, `calendar`, and `refresh` fields.
`use_cache=false` maps to forced refresh. `DataFetchOut` gains a nested summary with acquisition ID,
status, `sources_used`, optional `selected_source` for newly fetched rows, cache status, requested
and accepted counts, rejected and missing counts, duplicate
count, coverage, and warning count. A report lookup returns the redacted manifest.

The backtest route uses the same acquisition service without changing its response schema.

The standard-library CLI provides `acquire`, `inspect`, and `benchmark`. Exit codes are 0 success or
warning, 2 request/configuration, 3 providers exhausted, 4 fatal quality, and 5 cache/artifact
publication failure.

API errors map typed failures to stable responses: invalid request 400, fatal/no usable data 422,
local quota 429 when attributable to the requested provider, provider exhaustion 502, and cache
publication 500. Error bodies include acquisition ID but never secrets.

## 11. Deterministic Testing

Tests use redacted golden yfinance and Alpha Vantage fixtures and injected clocks, sleepers, jitter,
download functions, HTTP clients, IDs, and calendars. They cover:

- Provider column/action mappings, exclusive-end translation, response errors, and capabilities.
- Dtypes, timezone normalization, ordering, duplicate variants, nulls, infinities, OHLC rules,
  volumes, action fields, holidays, gaps, coverage, and reconciliation.
- Retry success/exhaustion, quota short-circuit, entitlement, fallback, warnings, and all-source
  failure.
- Full/partial/stale/forced cache behavior, merge precedence, version mismatch, corruption, failed
  generation writes, and failed pointer publication.
- Strict Candle conversion, API compatibility, CLI schemas and exit codes, report redaction, and
  wheel/install resource behavior.
- Existing tests remain green and no live network is required in CI.

## 12. Benchmark and Measurement

The repeatable benchmark uses seed 42, XNYS daily sessions, symbols SPY/AAPL/MSFT, 2020-01-01
through 2024-12-31, three warmups, and fifteen measured runs. Actual session and row counts are
calculated and reported, never assumed.

Provider-shaped deterministic payloads measure cold cache, full hit, partial hit, stale refresh,
duplicate removal, limited gaps, retry success, yfinance-to-Alpha fallback, corporate-action
invalidation, and fatal rejection. The JSON artifact records configuration, dependency/platform
versions, observed counts, Parquet bytes, provider calls, all timings and summaries, hashes, and
quality state. Optional live smoke results are separate and do not gate CI or support deterministic
performance comparisons. Every warmup and measured repetition receives a fresh isolated scenario
fixture with asserted preconditions; setup is outside the timed region, and each sample asserts its
expected cache-status and provider-call trace.

## 13. Files in Scope

Create focused modules under `src/data/` for contracts, acquisition orchestration, normalization,
quality/calendar evaluation, retry policy, manifests, CLI, benchmark, and provider adapters or
compatibility exports. Create deterministic provider fixtures and focused unit/integration tests.

Modify the existing data store, Candle conversion boundary, data/backtest routes, API schemas,
package configuration, README, and Makefile. Add `pandas_market_calendars` and `filelock`. Do not add databases,
streaming infrastructure, cloud storage, or frontend work.

## 14. Acceptance Criteria

- Both adapters satisfy the explicit `ProviderBatch` contract and map action fields.
- Auto selection is capability-aware, yfinance-first, and uses Alpha Vantage only when enabled.
- Every retry and fallback outcome is deterministic, typed, tested, and manifested.
- Canonical results have exact columns, dtypes, ordering, uniqueness, and valid daily sessions.
- Duplicate, rejection, missing-session, coverage, and reconciliation counters are exact.
- Partial and stale cache requests fetch only planned ranges and publish atomically.
- Failed acquisition or publication never replaces the last valid cache generation.
- Every request produces a redacted machine-readable report, including failures.
- CLI commands and the API summary make the feature verifiable without reading source code.
- Deterministic fixtures validate both result values and shapes without live providers.
- The benchmark reports observed counts and timings without manufactured claims.
- Existing and new tests pass on Python 3.12.

## 15. Principal Risks

1. Provider contracts change: isolate adapters, fail closed on schemas, retain golden fixtures, and
   record provider/library versions.
2. Cache merging creates stale or mixed lineage: use exchange-session arithmetic, row source,
   immutable generations, hashes, locking, and full post-merge validation.
3. Quality policy mishandles real market behavior: separate structural fatal rules from recorded,
   configurable coverage and price-move warnings; preserve diagnostics.

## 16. Resume Evidence Produced After Implementation

Only reproducible artifacts may support claims. Eligible facts include symbols and ranges tested,
actual expected/received/accepted rows, duplicates removed, rejected rows, gaps, coverage, cache
status, provider attempts, fallback outcomes, Parquet bytes, measured runtime, test count, and
artifact hashes. This design claims none of those values before implementation and measurement.

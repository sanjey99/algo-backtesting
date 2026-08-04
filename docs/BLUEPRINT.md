# Blueprint: Robust Multi-Source Market Data Acquisition and Quality Layer

**Status:** Approved architecture; implementation not started
**Date:** 2026-08-04
**Branch:** `blueprint/data-acquisition-quality`

Detailed artifacts:

- [Approved design specification](superpowers/specs/2026-08-04-data-acquisition-quality-design.md)
- [Dependency-ordered implementation plan](superpowers/plans/2026-08-04-data-acquisition-quality.md)
- [Architecture decision records](adr/README.md)

## Problem and Intended Users

The repository downloads OHLCV data from yfinance and Alpha Vantage, performs limited column
normalization, and caches exact date-keyed Parquet files. It cannot currently explain provider
selection, rejected rows, missing sessions, fallback, cache freshness, or lineage. `df_to_candles()`
silently skips malformed records, and both API acquisition paths instantiate yfinance directly.

This feature gives strategy developers trustworthy daily data, maintainers diagnostic evidence,
and interviewers a repeatable demonstration of provider integration, schema reconciliation,
quality validation, partial caching, lineage, and measured behavior.

## Stack and Framework Decisions

- Python 3.12 and existing pandas/pyarrow/yfinance/requests stack
- `pandas_market_calendars` for XNYS expected sessions and holiday distinction
- Dataclasses, enums, protocols, and typed exceptions for explicit contracts
- Canonical Parquet plus versioned JSON manifests
- Standard-library `argparse` CLI and additive FastAPI summary fields
- Deterministic pytest fixtures; live provider access is optional and never gates CI

No PostgreSQL, warehouse, Spark, Kafka, real-time streaming, cloud storage, or frontend work is
added. Intraday and global calendar inference are deferred.

## Provider Limitations Discovered

- yfinance documents an inclusive `start` and exclusive `end`; the adapter must translate the
  public inclusive range. Intraday history is limited to the last 60 days, but V1 supports daily
  bars only. Actions require explicit request options.
- Alpha Vantage documents `TIME_SERIES_DAILY_ADJUSTED` as premium. The general free limit is
  currently documented as up to 25 requests daily, while premium limits depend on entitlement.
  The existing five-calls-per-minute constant is therefore not presented as universal truth.
- Calendar packages are versioned local rule sets, not live exchange feeds; the installed calendar
  version becomes lineage evidence.

Primary sources are the current yfinance API/price-repair documentation, Alpha Vantage API/support
documentation, pandas-market-calendars usage documentation, and NYSE calendar documentation. Full
links and findings are in the approved specification.

## Recommended Approach and Rejected Alternatives

Select a separate normalization and quality pipeline between providers and storage.

- Expanding `DataFetcher` and `DataStore` directly was rejected because it couples transport,
  retry, normalization, validation, fallback, lineage, and storage policy.
- A permanent raw-to-canonical data lake was rejected because payload retention and lifecycle
  management exceed portfolio scope.
- Small redacted golden fixtures retain provider-shape evidence without creating a raw data zone.

## Architecture

```text
Request validation
  -> cache coverage/freshness
  -> provider eligibility and source policy
  -> retrying provider adapter
  -> normalization
  -> deduplication and quality evaluation
  -> accept warnings or fall back on fatal candidate
  -> merge cached and acquired sessions
  -> final contract validation
  -> immutable Parquet generation + atomic pointer
  -> redacted acquisition manifest
  -> CLI / API summary / strict Candle conversion
```

Module boundaries:

- Contracts define requests, policies, batches, results, attempts, findings, and typed failures.
- Provider adapters translate transport/provider behavior only.
- Normalization maps columns, dtypes, timestamps, actions, ordering, and range filtering.
- Quality evaluation is pure and owns row rules, coverage, reconciliation, and severity.
- Calendar adapters own expected-session calculations.
- Acquisition owns cache-first orchestration, retry, fallback, range planning, and merge precedence.
- Storage owns locking, generation hashes, pointer verification, and atomic publication.
- Manifest storage owns versioned JSON and recursive credential redaction.
- CLI and API remain thin consumers.

## Source-Selection Policy

1. Validate symbol, inclusive range, daily interval, XNYS calendar, and source enum.
2. Return fresh full-cache coverage without constructing providers.
3. Plan missing or stale ranges from expected exchange sessions.
4. In `auto`, prefer yfinance.
5. Consider Alpha Vantage only with API key and explicit adjusted-daily entitlement configuration.
6. Record ineligible providers as skipped with a reason, not as API failures.
7. Retry transient connection, timeout, bounded 429, 5xx, or temporary throttle failures.
8. Do not retry request, authentication, entitlement, long quota, permanent schema, empty-data, or
   fatal-quality failures; attempt the next eligible provider when appropriate.
9. Accept a warning-level candidate. Reject the complete fatal candidate before fallback.
10. If all sources fail, write a failed manifest and publish no cache generation.

Retry defaults are three attempts, exponential full jitter, 0.5-second base, and 8-second cap.
Clock, sleeper, randomness, download function, and HTTP client are injected for deterministic tests.
Alpha Vantage uses a configurable process-local budget with a conservative default ceiling; this
is explicitly not a distributed rate limiter.

## Canonical Data Contract

One row represents one symbol and XNYS daily session. Exact ordered fields are:

| Field | Type | Rule |
|---|---|---|
| `timestamp` | `datetime64[ns]` | unique timezone-naive normalized session label |
| `symbol` | string | normalized and equal to request |
| `open` | `float64` | finite, positive |
| `high` | `float64` | finite, positive |
| `low` | `float64` | finite, positive |
| `close` | `float64` | finite, positive |
| `volume` | `float64` | finite, nonnegative |
| `adj_close` | `float64` | finite, positive |
| `dividend_amount` | `float64` | finite, nonnegative |
| `split_coefficient` | `float64` | finite, positive |
| `source` | provider enum | required row lineage |

Daily timestamps are session labels, not precise instants. Provider and exchange time zones are
manifest fields. Zero dividend and unit split defaults are legal only when a provider batch declares
that its action fields are represented.

`ProviderBatch` additionally records provider, normalized request, received time, native timezone,
raw row count, safe response metadata, and action coverage. It never contains secrets.

## Normalization and Quality Rules

Normalization preserves original provider order for diagnostics, maps source fields, parses without
unreported coercion, normalizes daily timestamps, removes out-of-range rows, stably sorts, resolves
duplicates, and validates.

- Identical canonical duplicates retain the first and increment the removal counter.
- Conflicting duplicates are fatal; no source row is guessed to be correct.
- Null/nonnumeric essentials, invalid OHLC relationships, nonpositive prices, negative volume,
  invalid actions, NaN, and infinity reject rows with diagnostics.
- Rows on a non-session date after normalization are a fatal calendar/source mismatch.
- The complete frame must have exact columns/dtypes, strict monotonicity, uniqueness, one symbol,
  and accepted timestamps within expected sessions.
- Missing bars are calculated from XNYS sessions, so weekends and holidays are excluded.
- Default minimum coverage is 98%; default maximum consecutive missing sessions is two.
- V1 adjustment factors must be finite and positive. Action signatures hash sorted canonical action
  tuples; refresh comparison reports changed values using fixed numeric-noise tolerances. V1 does
  not invent a threshold for classifying genuine market price moves.
- Acquisition consumes provider-adjusted values and does not reapply current adjustment helpers.

Severity:

- `INFO`: expected mappings, sorting, filtering, and cache assembly.
- `WARNING`: exact duplicates, isolated rejected rows with sufficient coverage, limited gaps, and
  observed cached-versus-refreshed revisions.
- `FATAL`: unusable schema, conflicting duplicates, wrong symbol/calendar, insufficient coverage,
  excessive gaps, no accepted rows, or failed canonical contract.

Required reconciliation:

```text
provider_rows = timestamp_unclassifiable_rows + out_of_range_rows + in_range_rows
in_range_rows = dedupe_input_rows
dedupe_input_rows = exact_duplicate_rows_removed + conflicting_duplicate_rows + nonconflicting_unique_rows
nonconflicting_unique_rows = accepted_unique_rows + rejected_unique_rows
expected_sessions = accepted_expected_sessions + missing_sessions
```

Missing, unparsable, and invalid-timezone timestamps are unclassifiable before range filtering.
Duplicate groups are resolved before unique-row validation; an identical invalid group contributes
removed duplicate members plus one rejected unique row. Conflicting groups count every member and make the candidate fatal. Provider ranges are checked for
structural usability; coverage and consecutive-gap severity apply only to the final merged request.

## Cache, Freshness, and Lineage

Cache identity is contract version/calendar/interval/validated symbol. Each entry contains immutable
generation directories with `bars.parquet`, `cache-metadata.json`, and the final success manifest;
an atomically replaced `CURRENT.json` identifies the active generation and hashes all artifacts.

Writers record their base generation, prepare provider results, then take a cross-process `filelock`
publication lock. A stale writer rebases and revalidates on the latest generation, retrying at most
three conflicts. Artifacts and directories are fsynced before pointer replacement. Readers verify
every hash, schema, and calendar version. Failed publication preserves the previous generation.

Partial hits derive missing sessions, group contiguous exchange ranges, validate every acquired
range, replace overlap only with accepted new rows, merge retained cache, and validate the complete
result before one all-or-nothing publication.

Freshness defaults:

- one-hour per-lineage-segment TTL when the range touches the latest completed session
- seven-day per-lineage-segment TTL for fully historical ranges
- five-session overlap during stale refresh
- full history refresh when overlap reveals an action change; stale historical segments refresh in
  full so older action revisions are observable
- explicit refresh bypasses TTL
- contract/calendar version mismatch invalidates compatibility

The latest completed session is the last XNYS session whose close plus a configurable 30-minute
availability lag is not later than the injected clock. Each lineage segment records range,
provider, acquired-at time, action coverage, content hash, and action signature.

Every request writes a versioned manifest containing acquisition ID, request, applied policies,
environment versions, expected sessions, initial cache state, candidates/skips, attempts and retry
delays, safe provider parameters, transformations, findings, all counters, missing intervals,
range-level source lineage, coverage, final cache generation/hash, duration, and final status.
Credentials and credential-bearing URLs are recursively excluded. An acquisition ID is assigned
after syntactic validation. Success publication manifests commit inside cache generations, while an
independent immutable request archive retains every admitted full hit, success, and failure.
Generation cleanup is forbidden until its report is archived; lookup can fall back to a pinned
embedded manifest and maintenance retries archival. Report retention is indefinite in V1. Optional
caller-selected copy failure is a warning, not a cache rollback.

## Public API and CLI Surface

`DataFetchRequest` gains defaulted `source="auto"`, `calendar="XNYS"`, and `refresh=false`.
`use_cache=false` maps to forced refresh. Existing request bodies remain valid.

`DataFetchOut` adds a compact nested summary containing acquisition ID, status, `sources_used`,
optional `selected_source`, cache status, requested sessions, accepted/rejected/missing rows,
duplicates removed, coverage, and
warning count. A lookup endpoint returns the redacted full manifest. Backtest acquisition uses the
same service while its response contract remains unchanged.

CLI demonstrations:

```bash
python -m src.data.cli acquire --symbol SPY --start 2020-01-01 --end 2024-12-31 \
  --calendar XNYS --source auto --report artifacts/spy-acquisition.json
python -m src.data.cli inspect --acquisition-id ACQUISITION_ID
python -m src.data.cli benchmark --output artifacts/data-quality-benchmark.json
```

Exit codes are 0 success/warning, 2 request/configuration, 3 providers exhausted, 4 fatal quality,
and 5 cache/artifact publication failure.

## Testing and Fixture Strategy

Use redacted golden provider fixtures and injected external effects. Tests cover provider column and
action mappings, end-date translation, capabilities, response envelopes, retries, quota/entitlement,
normalization, every row rule, duplicate variants, calendars, gaps, coverage, corporate actions,
reconciliation, full/partial/stale/forced cache paths, corruption, interrupted publication,
fallback, all-source failure, mixed lineage, strict Candle conversion, API compatibility, CLI exit
codes, report redaction, and installed-wheel behavior.

No live network is required. The existing 245-test Python 3.12 baseline must remain green.

## Benchmark and Measurement Plan

The deterministic configuration is seed 42, XNYS daily, SPY/AAPL/MSFT, 2020-01-01 through
2024-12-31, three warmups, and fifteen measured repetitions. Actual sessions and rows are computed,
not preclaimed.

Scenarios are cold cache, full hit, partial hit, stale tail, duplicate removal, warning gaps,
transient retry, yfinance-to-Alpha fixture fallback, action invalidation, and fatal rejection.

Each warmup and repetition starts from a fresh isolated scenario fixture with setup excluded from
timing and asserted cache/provider preconditions. The JSON report records exact inputs,
dependency/platform versions, observed requested/received/
accepted/rejected/duplicate/missing counts, calls, Parquet bytes, all timings and median/percentile
summaries, hashes, and quality state. Optional live smoke evidence is separate and never mixed with
deterministic performance comparisons.

## Files to Create or Modify

Create focused contracts, providers, normalization, quality, calendar, retry, acquisition,
manifest, CLI, and benchmark modules under `src/data/`; deterministic fixtures/tests; and an
operator guide. Add `pandas_market_calendars` and `filelock`. Modify current fetcher compatibility
exports, `DataStore`, strict Candle conversion,
data/backtest routes, API schemas, package dependencies, README, and Makefile.

The exact file map and method-level TDD sequence are in the linked implementation plan.

## Ordered Implementation Plan

1. Define request/result/error contracts and the exchange-calendar boundary.
2. Implement provider capabilities, adapters, error classification, and injected retry.
3. Implement canonical normalization, duplicate resolution, quality rules, and reconciliation.
4. Implement immutable cache generations, atomic pointer publication, and manifest repository.
5. Implement acquisition orchestration, partial range planning, merge validation, and fallback.
6. Make Candle conversion strict and expose the shared service through API and CLI.
7. Add deterministic benchmark, operator documentation, and demo commands.
8. Verify packaging, compatibility, tests, lint, types, secret redaction, and claim discipline.

Each task begins with failing tests, proceeds to the minimum implementation, runs focused and full
verification, and commits independently. Dependencies and exact cases are in the linked plan.

## Measurable Acceptance Criteria

- Both adapters satisfy an explicit typed batch contract and preserve action information.
- Auto selection is yfinance-first and never attempts ineligible Alpha Vantage access.
- Retry/fallback decisions are typed, deterministic, tested, and manifested.
- Canonical frames meet exact schema, dtype, ordering, uniqueness, and session rules.
- Duplicate, rejected, missing, coverage, and reconciliation values are exact.
- Partial/stale requests fetch only planned ranges and publish one validated generation.
- Failure never replaces the last valid cache generation.
- Every request, including failure, produces a redacted machine-readable report.
- CLI/API demonstrations expose quality without requiring source inspection.
- Deterministic fixtures validate result values and shapes without live providers.
- Benchmark artifacts contain observed counts, timings, versions, calls, and hashes.
- Existing and new tests pass on Python 3.12.

## Principal Technical Risks

1. **Provider contracts drift.** Isolate schemas, fail closed, retain golden fixtures, and record
   provider/library versions.
2. **Cache merge corrupts freshness or lineage.** Use exchange sessions, row sources, hashes,
   immutable generations, locks, all-or-nothing publication, and final validation.
3. **Quality policy misclassifies legitimate behavior.** Separate structural fatal rules from
   configurable coverage and price warnings; record applied policy and preserve diagnostics.

## Resume Evidence Produced

Only post-implementation reproducible facts may be quoted: symbols/ranges tested, actual requested
and accepted rows, duplicates removed, rejected rows, gaps, coverage, cache status, provider calls,
fallback outcomes, Parquet bytes, measured runtime, test count, and artifact hashes. No dataset
size, quality improvement, provider availability, or performance result is claimed by this design.

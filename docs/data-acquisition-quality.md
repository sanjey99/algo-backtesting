# Market Data Acquisition Quality Guide

## Purpose and scope

This guide explains how the daily US-equity acquisition boundary produces evidence that a
backtest input is usable. The analytical question is deliberately narrow: *for a requested
symbol and inclusive XNYS daily-session range, can the system produce a canonical, traceable
frame or clearly explain why it cannot?*

The feature is a research and demonstration boundary. It is not an exchange feed, a market-data
SLA, investment advice, or a claim that a provider's data is complete or suitable for trading.

## Provider limits

`yfinance` is an unofficial research-oriented interface; provider responses and availability can
change. Alpha Vantage adjusted daily data requires a configured API key and entitlement, and has
provider-controlled quota and coverage limits. The default deterministic benchmark never sends a
network request and does not use credentials. A live smoke check, when an operator chooses to run
one separately, is evidence only of that invocation; it must not be compared with deterministic
benchmark timings.

## Contract and quality rules

V1 accepts only `XNYS`, `1d`, safe normalized symbols, and inclusive start/end dates. Providers
return native `ProviderBatch` values; normalization then produces one canonical row per expected
exchange session with timestamp, symbol, OHLCV, adjusted close, corporate-action fields, and row
source.

The service records and verifies these rules:

- XNYS sessions distinguish market closures from missing observations.
- Canonical rows have exact dtypes, sorted unique timestamps, one symbol, finite prices, valid
  high/low relationships, non-negative volume/dividends, and positive split coefficients.
- Exact duplicates are removed with a warning; conflicting duplicates, unusable provider shapes,
  wrong-calendar rows, and an invalid complete frame are fatal.
- Final coverage and maximum consecutive missing-session rules are evaluated only after cached
  and newly accepted ranges are assembled. Isolated permitted gaps remain warnings.
- A stale or forced refresh is checked for changed dividend/split tuples. A detected action change
  causes a complete requested-range refresh rather than silently mixing action history.

Every completed or admitted failed request has a redacted JSON manifest. It includes request and
policy snapshots, dependency/calendar evidence, cache state, provider attempts, skipped providers,
findings, rejection and reconciliation counters, coverage, lineage hashes, output hash, and timing.
Credentials and credential-bearing values are redacted at the boundary.

## Publication and platform durability

Cache generations, pointers, report archives, and caller-selected artifacts are published through
same-filesystem namespace operations, so readers do not observe partially written file contents
during normal operation. Every file is flushed before publication. POSIX systems additionally
flush leaf publication directories, providing stronger crash ordering when the ancestor hierarchy
is already durable. Recursively created ancestor directories are not individually flushed, so the
system does not claim end-to-end power-loss durability for a first publication into a fresh tree.

Windows does not expose a supported directory descriptor through Python's `os.open`, so directory
metadata persistence across sudden power loss is best-effort even though file contents are flushed
and normal-operation publication remains namespace-atomic. After restart, readers verify the
pointer, referenced files, schemas, and hashes and fail closed rather than returning incomplete or
unverified market data. Prefer a pre-created POSIX hierarchy when stronger crash-ordering guarantees
are required, and use external transactional storage when end-to-end power-loss durability is a hard
requirement.

## Operator commands

Acquire canonical data and write caller-owned copies explicitly:

```bash
python -m src.data.cli acquire \
  --symbol SPY --start 2020-01-01 --end 2024-12-31 \
  --canonical artifacts/spy.parquet --report artifacts/spy-report.json
```

Inspect a redacted archived report:

```bash
python -m src.data.cli inspect --acquisition-id <acquisition-id>
```

Run the offline verification benchmark:

```bash
python -m src.data.cli benchmark --output artifacts/data-quality-benchmark.json
```

`artifacts/`, cache generations, reports, benchmark output, `.env` files, and credentials are
local operational evidence. Do not commit them. Copy or retain an artifact with the exact command,
environment, and dependency versions before citing values from it.

## Deterministic benchmark method

The benchmark has a fixed matrix: XNYS/1d, SPY/AAPL/MSFT, 2020-01-01 through 2024-12-31, seed 42,
three warmups, and fifteen measured repetitions. It computes sessions and rows from the installed
calendar at runtime; it does not hard-code observed counts.

Generated provider-shaped payloads contain deterministic synthetic values, never copied market
data. The ten ordered scenarios are cold cache, full hit, partial hit, stale refresh, duplicate
removal, limited gaps, retry success, yfinance-to-Alpha fallback, corporate-action invalidation,
and fatal rejection.

Before each warmup and measured sample, the runner creates a new isolated provider/cache/report
fixture and asserts the scenario precondition. Setup and teardown are outside the timed interval.
Every sample asserts its cache status, provider-call trace, and counters. The artifact records
configuration, platform and exact dependency versions, generated-payload hashes, observed counts,
Parquet bytes, individual timings, nearest-rank median/p95 summaries, findings, output hashes, and
the separate `live_smoke` field (normally `null`).

## Claim discipline

Do not infer live-provider latency, throughput, coverage, or reliability from this benchmark. Do
not compare a live smoke result with deterministic timing. State an observed count, byte size, hash,
or duration only when a retained, reproducible artifact supports it, along with its command and
environment. The implementation and this guide intentionally make no unbacked performance claims.

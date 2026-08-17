# ADR-0001: Separate Acquisition and Quality Pipeline

**Date:** 2026-08-04
**Status:** accepted

## Context

The current provider classes partially normalize their own frames, `DataStore` combines fetching
with exact-range caching, and Candle conversion silently discards invalid rows. Adding validation,
fallback, lineage, and partial hits directly to those classes would couple unrelated policies.

## Decision

Provider adapters return typed `ProviderBatch` values. Separate normalization, quality, calendar,
acquisition, storage, and manifest components transform them into a validated canonical result.

## Alternatives

- Expand `DataFetcher` and `DataStore`: fewer files, but poor isolation and observability.
- Persist a complete raw-to-canonical lake: stronger replay, but excessive lifecycle scope.

## Consequences

- Quality rules and orchestration become deterministic and independently testable.
- Adapters remain provider-specific without becoming policy owners.
- More explicit types and modules are required.
- Production implementation must preserve compatibility exports while callers migrate.

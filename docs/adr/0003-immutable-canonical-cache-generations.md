# ADR-0003: Immutable Canonical Cache Generations

**Date:** 2026-08-04
**Status:** accepted

## Context

Exact date-keyed Parquet files cannot support partial hits or freshness. Replacing a Parquet file
and JSON sidecar independently can expose mismatched data after interruption.

## Decision

Store canonical data, cache metadata, and the final success manifest in immutable generation
directories. Publish by atomically replacing `CURRENT.json`, whose hashes cover every artifact.
Use a cross-process `filelock` publication lock and optimistic generation comparison; stale writers
rebase and revalidate against the latest generation before publishing. An independent immutable
request-report archive retains every admitted request; a generation remains pinned until its
embedded publication manifest has been archived.

## Alternatives

- Overwrite one Parquet file in place: simplest, but unsafe on failed writes.
- Rename Parquet and metadata separately: leaves a crash window between two renames.
- Add a cache database: transactional, but an unnecessary platform expansion.

## Consequences

- Readers observe a complete old or new generation, never a half-published pair.
- Failed refreshes preserve the last valid data.
- Locking, rebasing, hashing, pointer validation, and old-generation cleanup require focused tests.
- A bounded number of prior generations may consume temporary extra disk space.

# ADR-0003: Harden and Measure SQLite

**Date**: 2026-08-04
**Status**: accepted
**Deciders**: Project owner and architecture session

## Context

SQLite is the only database exercised by the repository. The implemented schema declares foreign
keys but does not enable their enforcement, lacks supported natural-key constraints and analytical
indexes, and has an ineffective initial Alembic migration. PostgreSQL support is aspirational.

## Decision

We repair the SQLite migration path, enable foreign keys on every SQLite connection, add supported
natural-key constraints, and add indexes justified by the approved query catalogue. We evaluate
plans with SQLite `EXPLAIN QUERY PLAN` and report measured results without claiming PostgreSQL
readiness.

## Alternatives Considered

### Read-only detection without hardening

- **Pros**: No migration risk and smaller implementation surface
- **Cons**: Detects preventable defects without stopping recurrence
- **Why not**: The project owner approved focused prevention as part of the portfolio evidence

### Add PostgreSQL infrastructure

- **Pros**: A second database could demonstrate broader portability
- **Cons**: Adds deployment, dialect, CI, and migration scope not exercised by the repository
- **Why not**: It would turn a focused SQLite enhancement into a database-platform project

### Add indexes from intuition alone

- **Pros**: Fast to implement
- **Cons**: Can add write cost and redundant indexes without proving planner value
- **Why not**: Indexes must be tied to query predicates and measured plans

## Consequences

### Positive

- Supported duplicates and orphans are prevented after migration.
- Fresh and legacy database paths become explicit and testable.
- Query-plan and latency artifacts provide defensible performance evidence.

### Negative

- SQLite constraint changes may require table reconstruction through Alembic batch operations.
- Legacy databases require a preflight audit and explicit stamping workflow.

### Risks

- A legacy database may contain violations; migration refuses to proceed and preserves the
  database until the operator resolves the reported records.
- Query-plan text can vary by SQLite version; artifacts record versions and raw plan rows while
  tests avoid brittle whole-plan snapshots.

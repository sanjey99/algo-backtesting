# ADR-0001: Package Reviewed SQL Queries

**Date**: 2026-08-04
**Status**: accepted
**Deciders**: Project owner and architecture session

## Context

The repository persists backtest results through SQLAlchemy ORM but needs direct, inspectable SQL
evidence. The analytical queries require joins, CTEs, and window functions while retaining safe
parameterization and the existing database configuration.

## Decision

We store analytical queries as packaged `.sql` resources in a closed catalogue and execute them
through SQLAlchemy `text()` with named bind parameters. SQLAlchemy Core remains available for
schema and migration declarations.

## Alternatives Considered

### SQLAlchemy Core expression trees

- **Pros**: Composable, typed Python expressions and automatic value binding
- **Cons**: Complex analytical SQL is less visible and harder to review in an interview
- **Why not**: It weakens the direct-SQL evidence that motivates this feature

### Inline SQL or direct `sqlite3`

- **Pros**: Minimal setup for a single query
- **Cons**: Mixes SQL with orchestration or duplicates connection configuration
- **Why not**: It creates weaker review boundaries and a second database-access path

## Consequences

### Positive

- Queries are independently reviewable, hashable, testable, and explainable.
- Existing SQLAlchemy connections and pandas extraction remain reusable.
- Named binds establish a clear boundary for untrusted values.

### Negative

- Non-Python resources require explicit packaging verification.
- Dynamic identifiers require closed mappings because identifiers cannot be bind parameters.

### Risks

- A caller could attempt arbitrary resource loading; a catalogue enum and packaged-resource loader
  prevent arbitrary file paths and SQL identifiers.

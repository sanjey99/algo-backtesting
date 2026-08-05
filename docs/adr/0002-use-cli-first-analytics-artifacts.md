# ADR-0002: Use CLI-First Analytics Artifacts

**Date**: 2026-08-04
**Status**: accepted
**Deciders**: Project owner and architecture session

## Context

The feature must be easy for an interviewer to verify while staying focused on SQL analytics.
Building an API or dashboard would add presentation work without improving the evidence for joins,
CTEs, validation, and query-plan reasoning.

## Decision

We expose `compare`, `validate`, and `benchmark` through a standard-library `argparse` CLI. The
primary outputs are deterministic CSV and versioned JSON artifacts; API and UI adapters are
deferred.

## Alternatives Considered

### FastAPI analytics endpoint

- **Pros**: Remote access, OpenAPI documentation, and consistency with the existing backend
- **Cons**: Adds request/response design and server startup to the verification path
- **Why not**: It expands scope without strengthening the initial SQL evidence

### Streamlit analytics view

- **Pros**: Visual and approachable demonstration
- **Cons**: Couples SQL verification to frontend state and rendering
- **Why not**: The current enhancement is a data extraction layer, not a UI feature

### Typer or Click CLI

- **Pros**: Rich command ergonomics and dedicated test helpers
- **Cons**: Adds a runtime dependency for three simple subcommands
- **Why not**: `argparse` satisfies the approved interface with less dependency surface

## Consequences

### Positive

- Interview verification requires only Python and a SQLite database.
- CSV supports direct inspection and pandas reuse; JSON preserves validation and measurement
  evidence.
- The application service remains reusable by a future API adapter.

### Negative

- The first release has no browser-based analytics view.
- CLI exit codes and artifact schemas become public contracts that require tests.

### Risks

- CLI orchestration could absorb business logic; thin command handlers delegate all behavior to
  services, validators, and artifact writers.

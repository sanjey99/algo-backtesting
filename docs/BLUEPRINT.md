# Blueprint: SQL Analytics and Data Extraction Layer

**Status:** Approved architecture; implementation not started
**Date:** 2026-08-04
**Repository:** algo-backtesting
**Architecture branch:** `blueprint/sql-analytics-layer`

## Problem & Users

The existing backtester persists runs, trades, equity points, and metrics through SQLAlchemy ORM,
but it does not yet demonstrate direct analytical SQL, validated dataset extraction, integrity
reconciliation, or measured query-plan reasoning. The primary user is a candidate demonstrating
those skills to technical recruiters and interviewers; the secondary user is a developer or analyst
comparing persisted backtest results.

## Stack & Framework Decisions

- Python 3.12 and the repository's existing SQLAlchemy 2.0, pandas, Alembic, pytest, and Hatch stack
- SQLite as the only implemented, tested, and measured database
- Reviewed packaged `.sql` resources executed through SQLAlchemy `text()` with named binds
- Standard-library `argparse` CLI with deterministic CSV and JSON artifacts
- SQLite `EXPLAIN QUERY PLAN` and `time.perf_counter_ns()` for recorded evidence
- No new runtime dependency, PostgreSQL infrastructure, API endpoint, dashboard, or frontend work

The accepted decisions are recorded in `docs/adr/0001-0003`.

## Technical Risks

1. One-to-many joins can multiply rows and corrupt aggregates. Queries filter parents first,
   pre-aggregate each child table independently, and reconcile one-row-per-run output.
2. SQLite hardening can damage unknown legacy schemas. An exact read-only schema classifier,
   integrity preflight, explicit stamp/upgrade state machine, and refusal on unknown state protect
   existing databases.
3. Timing noise and SQLite plan variation can produce misleading claims. Identical backed-up
   datasets, warm-ups, repeated raw samples, version capture, SQL hashes, and no universal latency
   threshold keep evidence defensible.

## Architecture

A thin CLI delegates to analytics and integrity services. Services execute only catalogue-owned SQL
through a read-only repository, normalize results against declared contracts, and pass validated
frames to an artifact writer. A separate benchmark runner creates deterministic baseline and
hardened SQLite variants, verifies identical analytical results, and records actual counts, plans,
and timings. Alembic owns fresh schema creation and focused integrity hardening.

## API Surface

No new HTTP API is part of this blueprint. The service interfaces remain reusable by a future thin
FastAPI adapter.

## UI/UX Decisions

No UI or dashboard work is part of this blueprint. The recruiter-verification experience is the
documented `compare`, `validate`, and `benchmark` command sequence and its CSV/JSON artifacts.

## Detailed Approved Design

**Status:** Approved design
**Date:** 2026-08-04
**Branch:** `blueprint/sql-analytics-layer`
**Scope:** Architecture and implementation design only

## 1. Problem and Users

The backtester persists runs, trades, equity points, and metrics through SQLAlchemy ORM,
but it does not provide direct, reviewable evidence of analytical SQL. Its current strategy
comparison is computed in Streamlit memory rather than from persisted results. The repository
therefore demonstrates persistence without demonstrating SQL-based extraction, reconciliation,
window analytics, query-plan reasoning, or reproducible database benchmarking.

The primary user is a candidate demonstrating practical SQL competence to technical recruiters
and interviewers. A secondary user is a developer or analyst who needs reproducible comparisons
across persisted backtest runs.

The feature answers this top-level question:

> Given persisted runs for selected symbols, strategies, and dates, which performed best, how did
> performance evolve, and can the extracted results be trusted?

The primary verification surface is a command-line interface that produces a validated CSV
dataset, JSON metadata, a JSON integrity report, and a JSON benchmark/query-plan report. API and
dashboard work are deferred.

## 2. Repository Evidence

The design is based on the implemented repository rather than earlier planning documents.

- `src/db/tables.py` defines four tables: `backtest_runs`, `trades`, `equity_curve`, and
  `metrics`.
- `src/db/crud.py` uses ORM operations for persistence and retrieval; it contains no direct
  analytical SQL.
- `src/api/routes/backtest.py` persists completed runs and reconstructs API responses through
  multiple CRUD calls.
- `src/dashboard/app.py` compares strategies in memory and does not compare persisted runs.
- `alembic/versions/455406e2c7ac_initial_schema.py` is a no-op migration.
- `Base.metadata.create_all()` currently creates the actual schema.
- The checked-in `tests/data/backtester.db` has the four-table schema and zero rows.
- SQLite foreign-key declarations exist, but enforcement is disabled on the inspected
  connection.
- Only the `backtest_runs` primary-key index exists; child join, natural-key, and analytical
  filter indexes are absent.
- Existing database tests cover ORM round trips, not analytical values, result shapes,
  duplicates, orphans, or query plans.
- The Python 3.12 baseline passes 245 tests.

The current SQLite database is authoritative evidence of schema shape, not evidence of data
volume or performance. No row-count or latency claim can be made from it.

## 3. Goals and Non-Goals

### Goals

- Make joins, CTEs, and window functions directly inspectable in reviewed SQL files.
- Safely parameterize every external value.
- Extract a reusable, typed, one-row-per-run comparison dataset to pandas and CSV.
- Reconcile stored results against SQL-derived values.
- Detect duplicates, orphans, invalid records, and inconsistent analytical values.
- Prevent supported duplicate and orphan conditions through a focused schema migration.
- Record reproducible row counts, latency samples, environment metadata, and SQLite query plans.
- Provide deterministic tests and interview-friendly commands.

### Non-Goals

- PostgreSQL infrastructure or a claim of PostgreSQL readiness
- A warehouse, Spark, Kafka, cloud storage, or database-platform rewrite
- Real-time analytics
- A new API endpoint, Streamlit view, or unrelated frontend work
- Arbitrary user-authored SQL execution
- A universal query-latency service-level objective

## 4. Architectural Approaches

### 4.1 Packaged SQL catalogue with SQLAlchemy `text()` — selected

Reviewed `.sql` resources are loaded from a closed catalogue, wrapped in SQLAlchemy `text()`,
and executed with named bind parameters. This approach exposes the SQL directly while retaining
the repository's existing connection and transaction infrastructure. pandas can consume the
same SQLAlchemy connection and statement for DataFrame extraction.

Costs are limited to a resource loader, packaging configuration, and a wheel smoke test.

### 4.2 SQLAlchemy Core expression trees — rejected as the query representation

Core provides composability and safe literal binding, but complex CTE and window expressions are
less legible to an interviewer and obscure the direct-SQL evidence this feature exists to create.
Core remains appropriate for schema and index declarations.

### 4.3 Inline SQL strings or direct `sqlite3` — rejected

Inline strings mix SQL with orchestration and make queries harder to discover and review. Direct
`sqlite3` duplicates SQLAlchemy connection behavior and creates a second database-access path.

## 5. Architecture and Component Boundaries

```text
CLI
 |-- compare ----> AnalyticsService --> QueryCatalogue --> SQLite
 |                       |                    |
 |                       v                    `-- packaged .sql resources
 |                 ContractValidator
 |                       |
 |                       v
 |                 CSV + JSON metadata
 |
 |-- validate ---> IntegrityService --> validation query suite
 |                                      `-- JSON quality report
 |
 `-- benchmark --> BenchmarkRunner --> fixture generator + query catalogue
                                        |-- observed timings
                                        |-- actual row counts
                                        `-- EXPLAIN QUERY PLAN
```

### 5.1 `QueryCatalogue`

Exposes a closed set of query identifiers and loads packaged `.sql` resources with
`importlib.resources`. It validates that required named binds are present. The CLI cannot accept
an arbitrary SQL path, SQL fragment, or query identifier outside the catalogue.

### 5.2 `AnalyticsRepository`

Owns read-only statement execution through a public SQLAlchemy engine or connection factory. It
does not extend `src/db/crud.py`, whose responsibility remains transactional ORM persistence.

### 5.3 `AnalyticsService`

Validates filters, executes catalogue queries, returns typed tabular results, and coordinates
contract validation before export.

### 5.4 `ContractValidator`

Checks exact ordered columns, row count, uniqueness keys, required-value nullability, numeric
bounds, and coercible numeric/date values. It returns a normalized pandas frame with declared
nullable dtypes. A contract failure blocks export.

### 5.5 `IntegrityService`

Runs row-count reconciliation, duplicate and orphan queries, stored-versus-derived checks,
`PRAGMA integrity_check`, and `PRAGMA foreign_key_check`. It produces a structured report with
severity, affected table, observed count, capped sample identifiers, and remediation guidance.

### 5.6 `ArtifactWriter`

Writes stable UTF-8 CSV and JSON artifacts with deterministic column ordering and explicit null
representation. Comparison CSV and metadata are staged and published as an all-or-nothing bundle,
with rollback of prior files if publication fails. Existing files are not overwritten without
`--force`.

### 5.7 `BenchmarkRunner`

Builds deterministic temporary databases, runs an unmeasured warm-up and measured repetitions,
captures environment and query metadata, and records raw query-plan rows and latency samples.

### 5.8 `sql_cli`

Provides thin standard-library `argparse` adapters for `compare`, `validate`, and `benchmark`.
Business logic remains directly callable by tests and a future API adapter.

### 5.9 Database connection boundary

`src/db/database.py` will expose a supported engine or connection factory rather than requiring
analytics code to import private `_engine`. SQLite foreign-key enforcement will be configured for
every connection.

## 6. Query Catalogue

### 6.1 `strategy_run_comparison.sql`

**Question:** Which persisted strategy runs performed best within a comparable cohort?

A cohort contains runs with identical symbol, start timestamp, end timestamp, initial capital,
commission percentage, and slippage percentage. Rankings never compare runs across these
dimensions.

**Inputs:** `symbol`, `start_date`, `end_date`, and optional `strategy_name`.

**Tables:** all four implemented tables.

**CTEs:**

- `selected_runs` filters the parent table before child aggregation.
- `metric_pivot` converts metric rows into named columns with conditional aggregation.
- `trade_stats` aggregates closed trades to one row per run.
- `latest_equity` uses `ROW_NUMBER()` to select the final equity point per run.
- `run_facts` joins the one-row-per-run datasets.

**Windows:** cohort-partitioned `RANK()` by derived total return and Sharpe ratio.

**Reconciliation:** derives total return from final equity, compares it with the stored
`total_return`, and reports their difference. Summed `trades.pnl` is treated as commission-net
because that is the implemented `Trade.pnl` contract; commission is reported separately and is
not subtracted twice.

**Output grain:** one row per run.

| Column | Contract |
|---|---|
| `run_id` | unique non-null string |
| `strategy_name`, `symbol` | non-null strings |
| `start_date`, `end_date` | non-null datetimes |
| `initial_capital` | positive float |
| `commission_pct`, `slippage_pct` | nonnegative floats |
| `final_equity` | float; nullable only for incomplete history |
| `closed_trade_count`, `winning_trade_count` | nonnegative integers |
| `cumulative_trade_pnl` | float |
| `stored_total_return`, `derived_total_return`, `total_return_delta` | nullable floats |
| `sharpe_ratio`, `max_drawdown` | nullable floats |
| `return_rank`, `sharpe_rank` | nullable positive integers |

This result is the canonical CSV dataset.

### 6.2 `trade_sequence.sql`

**Question:** How did a run's realized trade performance evolve?

**Input:** `run_id`.

**Tables:** `backtest_runs` joined to `trades`.

**CTE:** `closed_trades` selects completed records while the report separately counts open
records.

**Windows:**

- `ROW_NUMBER()` ordered by `exit_date, id`
- cumulative `SUM(pnl)`
- cumulative winning-trade count
- five-trade rolling average P&L using a row frame

**Output grain:** one row per closed trade. A known run with no closed trades returns a valid
empty dataset; an unknown run is an error.

### 6.3 `equity_drawdown_audit.sql`

**Question:** Does stored equity history agree with SQL-derived returns and drawdowns?

**Input:** `run_id`.

**Tables:** `backtest_runs` joined to `equity_curve`.

**CTEs:**

- `ordered_equity` derives prior equity with `LAG()`.
- `running_peak` derives peak-to-date with a running `MAX()`.
- `calculated` computes point returns and drawdown from the running peak.

**Windows:** `LAG()`, running `MAX()`, and deterministic `ROW_NUMBER()`.

**Output:** stored drawdown, derived drawdown, absolute delta, point return, running peak, and a
mismatch flag. The numeric tolerance is a validated input and is recorded in metadata.

### 6.4 `strategy_cohort_summary.sql`

**Question:** How do strategies rank across multiple explicitly labeled comparable cohorts?

**Inputs:** optional symbol, lower and upper run dates, and minimum completed-run count.

**CTEs:** pre-aggregate metrics, trades, and final equity before strategy-level aggregation.

**Windows:** `DENSE_RANK()` within each cohort.

**Output:** strategy, cohort dimensions, run count, average derived return, average Sharpe, worst
drawdown, aggregate trade count, and ranks. No cross-cohort best-strategy claim is produced.

### 6.5 Integrity query suite

The suite covers:

- Parent and child row counts
- Per-run child counts
- Metrics rows versus distinct metric-name counts
- Duplicate `(backtest_id, metric_name)` groups
- Duplicate `(backtest_id, date)` equity groups
- Orphaned trades, equity points, and metrics through `LEFT JOIN`
- SQLite integrity and foreign-key checks
- Stored versus derived total-return differences
- Stored versus derived drawdown differences
- Invalid run dates, nonpositive capital, negative fees, invalid trade quantities, and
  inconsistent closed-trade null fields

## 7. Safety and Parameterization

All external values use named binds. No values are interpolated into SQL.

- Query identifiers are catalogue enums.
- Sort keys map from a closed enum to hard-coded SQL expressions.
- Sort direction is a closed choice.
- Limits are range-validated integers and bound where supported.
- Output paths are validated separately from database inputs.
- The CLI cannot load arbitrary SQL.

Injection tests pass hostile strings through every input and verify that database schema and rows
remain unchanged.

## 8. Validation and Error Handling

### 8.1 Severity

`PASS` means an invariant holds. `WARN` marks analytically suspicious but usable data. `FAIL`
means the affected dataset is untrustworthy.

Fatal findings include SQLite integrity failure, orphans, duplicate natural keys, nonpositive
capital or quantity, closed-trade null inconsistencies, reconciliation outside the declared
tolerance, and missing required equity history for a run presented as complete.

Warnings include no closed trades, missing optional metrics, empty cohorts, sparse equity history
without a bar-frequency contract, and open trades excluded from realized-P&L analytics.

`compare` blocks export when a fatal finding affects selected rows unless the user supplies an
explicit diagnostic override. The override is recorded in metadata.

### 8.2 CLI outcomes

| Outcome | Exit code |
|---|---:|
| Database or SQL failure | 1 |
| Invalid CLI input | 2 |
| Unknown run identifier | 3 |
| Result-contract failure | 4 |
| Integrity failure | 5 |
| Successful result, including a valid empty result | 0 |

Errors shown to the user are sanitized. Detailed exceptions are available through logging.

## 9. Schema Hardening and Migration Lifecycle

The current no-op Alembic revision is treated as a schema-management defect.

1. Rewrite the currently ineffective initial revision to create the existing four-table baseline.
2. Add a second revision for analytical hardening.
3. Audit a legacy `create_all()` database before stamping or upgrading it.
4. Classify the schema by exact tables, ordered columns, SQLite-affinity types, nullability,
   primary keys, foreign keys, constraints, indexes, and Alembic revision. Refuse partial or unknown
   schemas without stamping or DDL.
5. If the expected baseline exists without an Alembic version, stamp the corrected initial
   revision and apply the hardening revision.
6. If a database is already stamped at the initial revision, verify that the baseline tables
   actually exist before applying hardening. A stamped database with missing baseline tables is a
   fatal schema-state error and is not repaired silently.
7. Stop without modifying the database when duplicates, orphans, or unexpected schema state would
   make hardening unsafe.
8. Ensure Alembic honors an explicitly injected connection or database URL before environment
   defaults so tests and migration commands cannot target an unintended database.
9. Do not migrate silently during API startup. Startup verifies schema compatibility and reports
   the required migration command.

The hardening revision adds:

- `UNIQUE(backtest_id, metric_name)` on `metrics`
- `UNIQUE(backtest_id, date)` on `equity_curve`
- `INDEX trades(backtest_id, exit_date, id)`
- `INDEX backtest_runs(symbol, start_date, end_date)`
- Foreign-key enforcement on every SQLite connection
- Explicit downgrade operations for the new constraints and indexes

Alembic batch operations are used where SQLite requires table reconstruction. Migrations are
tested on copied temporary databases.

## 10. Extraction and Artifact Contracts

CSV output uses UTF-8, a stable line terminator, no DataFrame index, exact ordered columns, and an
explicit null representation. Date parsing and serialization are declared rather than inferred.

Comparison metadata records:

- Query identifier and SQL hash
- Bound parameters
- Generation timestamp
- Database path identifier without embedded credentials
- Output row and column counts
- Contract version and validation status
- Integrity-report reference
- Diagnostic override status

Validation and benchmark reports are versioned JSON objects so later implementations can evolve
without silently changing consumers.

## 11. Deterministic Testing Strategy

Tests use temporary file-backed SQLite databases.

- Query-unit tests cover catalogue lookup, resource loading, and required binds.
- Contract tests assert exact ordered columns, types, nullability, row count, and uniqueness.
- Analytical integration tests verify final-equity selection, cumulative and rolling P&L, win
  rates, running peaks, derived drawdowns, reconciliation deltas, and cohort ranks using
  hand-calculable data.
- Integrity tests create controlled pre-hardening duplicates, orphans, malformed records,
  incomplete records, and metric mismatches.
- Migration tests cover fresh, valid legacy, duplicate-blocked, orphan-blocked, upgrade, and
  downgrade paths.
- Connection tests verify `PRAGMA foreign_keys = 1` for every normal SQLite connection.
- CLI tests verify artifacts, exit codes, overwrite protection, empty results, and sanitized
  errors.
- Injection tests verify that hostile inputs cannot alter database state.
- Packaging tests build and install the wheel, then load and execute every SQL resource.
- Benchmark smoke tests verify artifact shape, not latency.
- The existing 245-test baseline remains green.

No PostgreSQL test or readiness claim is included.

## 12. Benchmark and Query-Plan Methodology

The benchmark creates a deterministic synthetic database from recorded generator inputs. It never
labels synthetic results as live market results.

Two database copies contain identical rows: one uses the baseline schema and one uses the
hardened/indexed schema. All connections are closed before using SQLite's backup API to create the
second copy. Both run `ANALYZE` before measurement.

Each report records:

- Generator seed and configuration
- Actual row counts for every table
- Query identifier and SQL SHA-256
- Bound parameters
- Python, SQLite, SQLAlchemy, pandas, and operating-system metadata
- Present index names
- Raw `EXPLAIN QUERY PLAN` rows
- Warm-up and measured repetition counts
- Every latency sample from `time.perf_counter_ns()`
- Derived minimum, median, maximum, and percentile summaries
- Result row count and contract status

There is no universal latency threshold or predetermined speedup. Acceptance requires a
reproducible artifact and an explainable plan difference. Exact plan text is not snapshotted
across SQLite versions.

## 13. Interview Demo

```bash
python -m src.analytics.sql_cli benchmark \
  --database-out artifacts/sql-demo.db \
  --out artifacts/benchmark.json

python -m src.analytics.sql_cli validate \
  --database artifacts/sql-demo.db \
  --out artifacts/validation.json

python -m src.analytics.sql_cli compare \
  --database artifacts/sql-demo.db \
  --symbol SPY \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --csv artifacts/strategy-comparison.csv \
  --metadata artifacts/strategy-comparison.json
```

The benchmark command prints the actual generated cohort values. Documentation must use those
observed values rather than assume the illustrative values above exist.

## 14. Files in Scope

### Create

- `src/analytics/sql/__init__.py`
- `src/analytics/sql/strategy_run_comparison.sql`
- `src/analytics/sql/trade_sequence.sql`
- `src/analytics/sql/equity_drawdown_audit.sql`
- `src/analytics/sql/strategy_cohort_summary.sql`
- Integrity resources under `src/analytics/sql/integrity/`
- `src/analytics/sql_catalog.py`
- `src/analytics/sql_service.py`
- `src/analytics/sql_contracts.py`
- `src/analytics/sql_artifacts.py`
- `src/analytics/sql_benchmark.py`
- `src/analytics/sql_cli.py`
- One hardening Alembic revision
- Focused SQL analytics, integrity, migration, CLI, packaging, and benchmark tests
- `docs/sql-analytics.md`

### Modify

- `alembic/versions/455406e2c7ac_initial_schema.py`
- `src/db/database.py`
- `src/db/tables.py`
- `pyproject.toml`
- `README.md`
- `Makefile`

## 15. Acceptance Criteria

- Every catalogue query executes through SQLAlchemy `text()` with named parameters.
- Every external value is bound or mapped through a closed enum; no arbitrary SQL is accepted.
- The catalogue contains documented multi-table joins, CTEs, and window functions.
- Comparison CSV contains exactly one validated row per run.
- Deterministic fixtures produce exact expected analytical values and result shapes.
- Duplicate and orphan fixtures are detected before migration.
- New constraints reject duplicate metric and equity natural keys.
- Normal SQLite connections reject foreign-key violations.
- Fresh and valid-legacy migration paths pass; unsafe legacy state fails without modification.
- Injection probes cannot change database state.
- Benchmark JSON contains observed counts, timings, SQL hashes, versions, and query plans.
- Index use is demonstrated in the pinned benchmark environment without brittle cross-version
  snapshots.
- Existing and new tests pass under Python 3.12.
- Documentation explains the question, SQL techniques, validation, observed results, and demo
  commands.
- PostgreSQL and performance claims appear only when backed by execution evidence; neither is a
  requirement of this feature.

## 16. Principal Technical Risks

### 16.1 Join fan-out corrupts aggregates

Child tables are aggregated separately after filtering selected runs, then joined at one row per
run. Tests reconcile output grain and totals.

### 16.2 Hardening damages a legacy SQLite database

A read-only preflight audit, copied-database migration tests, explicit stamping workflow, atomic
migration, and refusal on violations protect existing data.

### 16.3 Benchmark results are noisy or query plans vary

Identical data copies, warm-ups, repeated samples, environment capture, SQL hashes, raw
observations, and the absence of a universal threshold keep conclusions defensible.

## 17. Resume Evidence Produced After Implementation

Only reproducible artifacts may support resume claims. Eligible measurements include:

- Number of reviewed analytical and validation SQL queries
- Tables joined and rows extracted
- Persisted runs, trades, equity points, and metrics processed
- Duplicate and orphan records detected
- Reconciliation mismatches found
- Exact deterministic test count
- Measured latency distributions
- Named indexes selected by the measured SQLite planner
- Measured before-and-after plan or timing changes
- CSV row and column counts

This design claims none of those values before implementation and measurement.

## 18. Research Basis

- SQLAlchemy textual SQL and named parameters:
  <https://docs.sqlalchemy.org/en/20/tutorial/dbapi_transactions.html>
- SQLAlchemy SQLite foreign-key configuration:
  <https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#foreign-key-support>
- pandas SQL extraction:
  <https://pandas.pydata.org/docs/reference/api/pandas.read_sql_query.html>
- SQLite common table expressions:
  <https://www.sqlite.org/lang_with.html>
- SQLite window functions:
  <https://www.sqlite.org/windowfunctions.html>
- SQLite query planning and indexes:
  <https://www.sqlite.org/queryplanner.html>
- SQLite `EXPLAIN QUERY PLAN` stability caveat:
  <https://sqlite.org/eqp.html>
- SQLite foreign-key enforcement:
  <https://sqlite.org/foreignkeys.html>
- Alembic batch migrations for SQLite:
  <https://alembic.sqlalchemy.org/en/latest/batch.html>
- Python `argparse` subcommands:
  <https://docs.python.org/3.12/library/argparse.html>
- Python high-resolution timing:
  <https://docs.python.org/3.12/library/time.html#time.perf_counter_ns>


## Implementation Plan

# SQL Analytics and Data Extraction Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a recruiter-verifiable, CLI-first direct-SQL analytics layer that extracts validated
strategy-comparison datasets, audits persisted data, hardens SQLite integrity, and records
repeatable query-plan and latency evidence.

**Architecture:** Reviewed `.sql` resources are loaded from a closed catalogue and executed with
SQLAlchemy `text()` and named binds. A service validates pandas result contracts before atomic
CSV/JSON export, while separate integrity and benchmark services report reconciliation findings,
SQLite plans, and observed timings. Alembic owns schema creation and hardening; API and dashboard
work remain out of scope.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, SQLite, Alembic, pandas, pytest, Hatch, and standard
library `argparse`, `dataclasses`, `enum`, `hashlib`, `importlib.resources`, `json`, `pathlib`,
`statistics`, and `time`.

## Global Constraints

- This plan implements production code only after the architecture-only session has ended.
- SQLite is the only supported and measured database for this feature.
- Do not add PostgreSQL services, drivers, CI jobs, or readiness claims.
- Add no runtime dependency; use existing SQLAlchemy and pandas plus the Python standard library.
- Do not add an API endpoint, dashboard component, warehouse, streaming system, or frontend work.
- Every external query value uses a named bind or a closed hard-coded identifier mapping.
- The CLI never accepts raw SQL, a SQL filename, or an unrestricted sort expression.
- Preserve exact one-row-per-run grain in comparison outputs.
- Do not claim row counts, latency, speedup, or quality improvement until a generated artifact
  records the observed value.
- Keep existing ORM CRUD writes separate from analytical reads.
- Use deterministic file-backed SQLite fixtures; live providers are not part of tests.
- Run Python commands with `uv run --python 3.12 --extra dev`.
- Before each commit, restore any tracked `__pycache__/*.pyc` files changed by test execution and
  exclude generated `.venv`, `uv.lock`, databases, CSV, and JSON artifacts.

---

## File Map

| File | Responsibility |
|---|---|
| `src/db/database.py` | Public engine factory, SQLite connection pragmas, schema verification |
| `src/db/migrate.py` | Exact schema classification and explicit safe upgrade workflow |
| `src/db/tables.py` | ORM declarations matching hardened schema |
| `src/analytics/sql_contracts.py` | Query identifiers, filters, column contracts, typed reports |
| `src/analytics/sql_catalog.py` | Closed SQL resource lookup, hashing, named-bind verification |
| `src/analytics/sql_service.py` | Read-only execution, contract validation, analytics orchestration |
| `src/analytics/sql_artifacts.py` | Atomic deterministic CSV and JSON serialization |
| `src/analytics/sql_benchmark.py` | Deterministic data generation, plan capture, latency measurement |
| `src/analytics/sql_cli.py` | Thin `argparse` command handlers and stable exit codes |
| `src/analytics/sql/*.sql` | Reviewable analytical SQL statements |
| `src/analytics/sql/integrity/*.sql` | Reviewable reconciliation and integrity statements |
| `alembic/env.py` | Deterministic URL/connection selection for migration commands and tests |
| `alembic/versions/*.py` | Baseline schema and hardening migration |
| `tests/sql_analytics/` | File-backed fixtures plus query, migration, CLI, and benchmark tests |
| `docs/sql-analytics.md` | Analytical questions, techniques, validation, measured evidence, demo |

## Dependency Graph

```text
Task 1 -> Task 2
Task 2 -> Task 3
Task 2 -> Task 4
Task 4 -> Task 5
Task 2 + Task 4 -> Task 6
Task 5 + Task 6 -> Task 7
Task 3 + Task 7 -> Task 8
```

Tasks 3 and 4 may be implemented in parallel after Task 2 because they create separate SQL
resources and test modules. Task 6 depends on Tasks 2 and 4. Task 7 depends on Tasks 5 and 6. All
other tasks are serial at their dependency boundaries.

---

### Task 1: Establish the analytics connection, types, and closed SQL catalogue

**Context brief:** The current module exposes a private `_engine` and ORM `Session` factory. Direct
analytics must reuse SQLAlchemy without importing private state. SQL resources must be loadable
from an installed wheel and must not be user-selectable by path.

**Files:**
- Modify: `src/db/database.py:13-48`
- Create: `src/analytics/sql_contracts.py`
- Create: `src/analytics/sql_catalog.py`
- Create: `src/analytics/sql/__init__.py`
- Create: `tests/sql_analytics/__init__.py`
- Create: `tests/sql_analytics/sql_resources/__init__.py`
- Create: `tests/sql_analytics/sql_resources/smoke_select.sql`
- Create: `tests/sql_analytics/test_catalog.py`
- Create: `tests/sql_analytics/test_database_connection.py`

**Interfaces:**
- Produces: `create_db_engine(database_url: str) -> Engine`
- Produces: `get_engine() -> Engine`
- Produces: `QueryId(StrEnum)`, `ColumnKind(StrEnum)`, `ColumnSpec`, `ResultContract`, and `QuerySpec`
- Produces: `Scalar = str | int | float | datetime | None`
- Produces: `QueryCatalogue.load(query_id: QueryId) -> LoadedQuery`
- Produces: `LoadedQuery(statement: TextClause, sha256: str, required_params: frozenset[str])`

- [ ] **Step 1: Write failing connection-factory tests**

```python
def test_sqlite_engine_enables_foreign_keys(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_non_sqlite_engine_has_no_sqlite_connect_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    def fake_create_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_engine
    monkeypatch.setattr(database, "create_engine", fake_create_engine)
    database.create_db_engine("postgresql://example.invalid/db")
    assert "connect_args" not in captured
```

- [ ] **Step 2: Run the connection tests and confirm the missing public factory failure**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_database_connection.py -v`

Expected: collection fails because `create_db_engine` is not defined.

- [ ] **Step 3: Implement the engine factory and per-connection SQLite pragma**

```python
def create_db_engine(database_url: str) -> Engine:
    kwargs: dict[str, Any] = {"echo": False}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def get_engine() -> Engine:
    return _engine
```

Keep `_SessionLocal` bound to the engine returned by `create_db_engine(DATABASE_URL)`.

- [ ] **Step 4: Write failing catalogue tests**

```python
def test_catalogue_loads_packaged_statement() -> None:
    spec = QuerySpec(
        resource="smoke_select.sql",
        required_params=frozenset({"value"}),
        contract=ResultContract(columns=(ColumnSpec("value", ColumnKind.INTEGER, False),)),
    )
    catalogue = QueryCatalogue(
        specs={QueryId.STRATEGY_RUN_COMPARISON: spec},
        package="tests.sql_analytics.sql_resources",
    )
    loaded = catalogue.load(QueryId.STRATEGY_RUN_COMPARISON)
    assert str(loaded.statement).strip() == "SELECT :value AS value"
    assert loaded.required_params == frozenset({"value"})
    assert len(loaded.sha256) == 64


def test_query_id_rejects_arbitrary_resource_path() -> None:
    with pytest.raises(ValueError):
        QueryId("../../secrets.sql")
```

- [ ] **Step 5: Run catalogue tests and confirm missing-type failures**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_catalog.py -v`

Expected: collection fails because catalogue types are not defined.

- [ ] **Step 6: Implement the query identifiers, specifications, loader, and smoke SQL**

```python
class QueryId(StrEnum):
    STRATEGY_RUN_COMPARISON = "strategy_run_comparison"
    TRADE_SEQUENCE = "trade_sequence"
    EQUITY_DRAWDOWN_AUDIT = "equity_drawdown_audit"
    STRATEGY_COHORT_SUMMARY = "strategy_cohort_summary"


class ColumnKind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    kind: ColumnKind
    nullable: bool
    minimum: float | None = None
    exclusive_minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class ResultContract:
    columns: tuple[ColumnSpec, ...]
    unique_by: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class LoadedQuery:
    statement: TextClause
    sha256: str
    required_params: frozenset[str]


@dataclass(frozen=True)
class QuerySpec:
    resource: str
    required_params: frozenset[str]
    contract: ResultContract
    expanding_params: frozenset[str] = frozenset()
```

Map each enum to a fixed package-relative resource. The constructor accepts an explicit specs map
and package name for isolated resource-loader tests; production defaults use the immutable
catalogue and `src.analytics.sql`. Load through
`resources.files(package).joinpath(resource_name).read_text(encoding="utf-8")`, compare compiled
bind names with `QuerySpec.required_params`, apply
`statement.bindparams(bindparam(name, expanding=True))` for each declared expanding parameter, and
hash the UTF-8 bytes. Reject an expanding name that is not also required. The test-only smoke
resource contains exactly `SELECT :value AS value`.

- [ ] **Step 7: Run focused tests and static checks**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_catalog.py tests/sql_analytics/test_database_connection.py -v`

Run: `uv run --python 3.12 --extra dev ruff check src/db/database.py src/analytics/sql_contracts.py src/analytics/sql_catalog.py tests/sql_analytics`

Expected: all focused tests and lint checks pass.

- [ ] **Step 8: Commit the connection and catalogue boundary**

```bash
git add src/db/database.py src/analytics/sql_contracts.py src/analytics/sql_catalog.py \
  src/analytics/sql tests/sql_analytics
git commit -m "feat: add SQL analytics query catalogue"
```

**Exit criteria:** A public engine factory enables SQLite foreign keys, and the closed catalogue
loads a hashed named-bind statement without accepting paths.

**Rollback:** Revert this task's commit; no persistent schema has changed.

---

### Task 2: Build deterministic fixtures and the one-row-per-run comparison dataset

**Context brief:** Directly joining all three child tables would multiply rows. The comparison SQL
must filter runs, aggregate each child independently, then join one-row-per-run facts. Rankings are
partitioned by exact cohort dimensions.

**Files:**
- Create: `tests/sql_analytics/conftest.py`
- Create: `tests/sql_analytics/fixture_data.py`
- Create: `src/analytics/sql/strategy_run_comparison.sql`
- Modify: `src/analytics/sql_contracts.py`
- Modify: `src/analytics/sql_catalog.py`
- Create: `src/analytics/sql_service.py`
- Create: `tests/sql_analytics/test_comparison_query.py`

**Interfaces:**
- Consumes: `create_db_engine`, `QueryCatalogue`, `QueryId`
- Produces: `ComparisonFilters(symbol: str, start_date: datetime, end_date: datetime, strategy_name: str | None)`
- Consumes: `ColumnSpec(name: str, kind: ColumnKind, nullable: bool)`
- Consumes: `ResultContract(columns: tuple[ColumnSpec, ...], unique_by: tuple[str, ...])`
- Produces: `AnalyticsRepository.execute(query_id: QueryId, params: Mapping[str, Scalar]) -> pd.DataFrame`
- Produces: `AnalyticsService.compare_runs(filters: ComparisonFilters) -> pd.DataFrame`
- Produces: `validate_frame(frame: pd.DataFrame, contract: ResultContract) -> None`
- Produces: `RunNotFoundError` and `ContractValidationError`

- [ ] **Step 1: Create a deterministic file-backed fixture factory**

Define three fixed run IDs in `fixture_data.py`: two strategies in the same cohort and one run in
a different capital cohort. Insert hand-calculable trades, equity points, and all ten implemented
metric names through `save_backtest_run()`. Use fixed UTC-naive datetimes because the current
SQLAlchemy SQLite mapping stores naive ISO-formatted values.

```python
@pytest.fixture()
def analytics_db(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_comparison_runs(session)
    return engine
```

- [ ] **Step 2: Write the failing comparison value-and-shape test**

```python
def test_comparison_returns_one_row_per_run_with_correct_ranks(analytics_db: Engine) -> None:
    frame = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)
    assert tuple(frame.columns) == COMPARISON_CONTRACT.names
    assert frame["run_id"].tolist() == ["run-ma", "run-rsi"]
    assert frame["return_rank"].tolist() == [1, 2]
    assert frame.loc[0, "cumulative_trade_pnl"] == pytest.approx(150.0)
    assert frame.loc[0, "derived_total_return"] == pytest.approx(0.02)
    assert frame.loc[0, "total_return_delta"] == pytest.approx(0.0)
```

- [ ] **Step 3: Run the comparison test and confirm the missing resource/service failure**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_comparison_query.py -v`

Expected: failure because `strategy_run_comparison.sql` and `AnalyticsService` are incomplete.

- [ ] **Step 4: Write the comparison SQL with pre-aggregation and windows**

The statement must use these CTE grains:

```sql
WITH selected_runs AS (... one row per run ...),
metric_pivot AS (... GROUP BY m.backtest_id ...),
trade_stats AS (... GROUP BY t.backtest_id ...),
ranked_equity AS (
  SELECT e.*,
         ROW_NUMBER() OVER (PARTITION BY e.backtest_id ORDER BY e.date DESC, e.id DESC) AS rn
  FROM equity_curve AS e
  JOIN selected_runs AS r ON r.id = e.backtest_id
),
run_facts AS (... one row per selected run ...)
SELECT ...,
       RANK() OVER (
         PARTITION BY symbol, start_date, end_date, initial_capital,
                      commission_pct, slippage_pct
         ORDER BY derived_total_return DESC
       ) AS return_rank
FROM run_facts
ORDER BY return_rank, strategy_name, run_id
```

Wrap each rank expression in `CASE WHEN` so a null derived return or Sharpe produces a null rank.
Use `run_id` only in the outer `ORDER BY` for deterministic presentation; do not include it in the
window ordering because equal metrics must receive equal ranks.

Use `CASE WHEN :strategy_name IS NULL OR strategy_name = :strategy_name` for the optional bound
filter. Count only rows with non-null exit date, exit price, and P&L as closed trades. Do not
subtract `commission` from `pnl`.

- [ ] **Step 5: Implement contract validation and comparison execution**

Implement `AnalyticsRepository` with the engine and catalogue injected in its constructor. It
loads and executes catalogue queries and returns raw DataFrames. `AnalyticsService` owns input and
result-contract validation and delegates execution to the repository. `validate_frame` returns a
normalized copy using pandas `StringDtype`, nullable `Int64`, nullable `Float64`, nullable boolean,
and `pd.to_datetime(errors="raise")` according to `ColumnKind`. It rejects missing, extra, or
reordered columns; duplicate `unique_by` keys; nulls in non-nullable columns; non-coercible values;
and values outside `minimum`, `exclusive_minimum`, or `maximum`. Set positive capital and rank
constraints and nonnegative count/fee constraints directly in their `ColumnSpec` definitions.
Execute with `pd.read_sql_query(loaded.statement, connection, params=params)` inside
`engine.connect()`; generic contract normalization handles every date column in every query.

- [ ] **Step 6: Add fan-out, tie, empty, and optional-filter tests**

Assert that adding extra metric and equity rows does not change trade aggregates, tied returns use
the same `RANK`, missing metrics produce nullable `Int64` ranks, a nonexistent symbol returns an
empty normalized frame with the exact contract columns and pandas dtypes, positive/nonnegative
constraints reject bad values, and the optional strategy filter excludes other strategies.

- [ ] **Step 7: Run focused tests and type/lint checks**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_comparison_query.py -v`

Run: `uv run --python 3.12 --extra dev ruff check src/analytics tests/sql_analytics`

Run: `uv run --python 3.12 --extra dev mypy src/analytics/sql_contracts.py src/analytics/sql_catalog.py src/analytics/sql_service.py --strict`

Expected: exact value, shape, fan-out, tie, and empty-result tests pass with no lint/type errors.

- [ ] **Step 8: Commit the comparison dataset**

```bash
git add src/analytics/sql_contracts.py src/analytics/sql_catalog.py \
  src/analytics/sql_service.py src/analytics/sql/strategy_run_comparison.sql \
  tests/sql_analytics
git commit -m "feat: add persisted run comparison query"
```

**Exit criteria:** The service returns a validated, exact-grain comparison DataFrame whose values
and ranks match hand-calculated fixtures.

**Rollback:** Revert the task commit; schema and existing CRUD remain unchanged.

---

### Task 3: Add trade sequencing, drawdown audit, and cohort summary queries

**Context brief:** These queries demonstrate cumulative, rolling, lagged, and ranking analytics
against persisted data. They must preserve deterministic ordering when timestamps tie.

**Files:**
- Create: `src/analytics/sql/trade_sequence.sql`
- Create: `src/analytics/sql/equity_drawdown_audit.sql`
- Create: `src/analytics/sql/strategy_cohort_summary.sql`
- Modify: `src/analytics/sql_contracts.py`
- Modify: `src/analytics/sql_catalog.py`
- Modify: `src/analytics/sql_service.py`
- Create: `tests/sql_analytics/test_trade_sequence_query.py`
- Create: `tests/sql_analytics/test_equity_audit_query.py`
- Create: `tests/sql_analytics/test_cohort_summary_query.py`

**Interfaces:**
- Consumes: `AnalyticsService`, `QueryCatalogue`, deterministic fixture engine
- Produces: `trade_sequence(run_id: str) -> pd.DataFrame`
- Produces: `equity_drawdown_audit(run_id: str, tolerance: float) -> pd.DataFrame`
- Produces: `CohortFilters(symbol: str | None, start_date: datetime | None, end_date: datetime | None, minimum_run_count: int)`
- Produces: `cohort_summary(filters: CohortFilters) -> pd.DataFrame`

- [ ] **Step 1: Write failing trade-sequence tests**

Assert exact sequence numbers, cumulative P&L, cumulative wins, cumulative win rate, and five-row
rolling mean. Include two trades sharing an exit timestamp and verify `id` breaks the tie. Verify a
known run without closed trades returns a valid empty frame and an unknown run raises
`RunNotFoundError`.

- [ ] **Step 2: Implement `trade_sequence.sql` and its service method**

Use a `closed_trades` CTE and these windows:

```sql
ROW_NUMBER() OVER (ORDER BY exit_date, id) AS trade_sequence,
SUM(pnl) OVER (ORDER BY exit_date, id ROWS UNBOUNDED PRECEDING) AS cumulative_pnl,
AVG(pnl) OVER (ORDER BY exit_date, id ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
  AS rolling_5_trade_avg_pnl
```

Derive cumulative win rate from cumulative wins divided by `ROW_NUMBER()` with floating-point
division.

- [ ] **Step 3: Write failing equity-audit tests**

Use a known equity path with a new peak and drawdown. Assert prior equity, point return, running
peak, derived drawdown, absolute delta, and mismatch flag at exact rows. Test tolerance `0`, a small
positive tolerance, negative tolerance rejection, deterministic ordering by `date, id`, and unknown
run behavior.

- [ ] **Step 4: Implement `equity_drawdown_audit.sql` and its contract**

Use `LAG(equity)`, running `MAX(equity)`, and `ROW_NUMBER()` in separate CTE layers so aliases are
not illegally reused in the same select list. Bind `:tolerance`; do not interpolate it.

- [ ] **Step 5: Write failing cohort-summary tests**

Assert grouping by symbol, exact date range, capital, commission, and slippage; minimum-run-count
filtering; average derived return; average Sharpe; worst drawdown; aggregate trade count; and
cohort-partitioned `DENSE_RANK()`.

- [ ] **Step 6: Implement `strategy_cohort_summary.sql` and its service method**

Filter selected runs before aggregating children. Bind optional symbol and date bounds plus
`minimum_run_count`. Do not produce an unlabeled overall winner across cohorts.

- [ ] **Step 7: Run the three focused suites**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_trade_sequence_query.py tests/sql_analytics/test_equity_audit_query.py tests/sql_analytics/test_cohort_summary_query.py -v`

Expected: all exact-value, ordering, empty, tolerance, and cohort tests pass.

- [ ] **Step 8: Commit the analytical query catalogue**

```bash
git add src/analytics/sql src/analytics/sql_contracts.py src/analytics/sql_catalog.py \
  src/analytics/sql_service.py tests/sql_analytics
git commit -m "feat: add SQL window analytics queries"
```

**Exit criteria:** All four approved analytical questions have static SQL resources and exact
value-and-shape tests.

**Rollback:** Revert the task commit; the comparison query from Task 2 remains independently usable.

---

### Task 4: Implement integrity, duplicate, orphan, and reconciliation reporting

**Context brief:** Integrity checks must run against legacy databases before constraints are added
and must remain useful after hardening. The report distinguishes fatal failures from warnings and
caps sample identifiers.

**Files:**
- Create: `src/analytics/sql/integrity/__init__.py`
- Create: `src/analytics/sql/integrity/table_counts.sql`
- Create: `src/analytics/sql/integrity/per_run_reconciliation.sql`
- Create: `src/analytics/sql/integrity/duplicate_metrics.sql`
- Create: `src/analytics/sql/integrity/duplicate_equity.sql`
- Create: `src/analytics/sql/integrity/orphan_children.sql`
- Create: `src/analytics/sql/integrity/invalid_records.sql`
- Create: `src/analytics/sql/integrity/metric_reconciliation.sql`
- Modify: `src/analytics/sql_contracts.py`
- Modify: `src/analytics/sql_catalog.py`
- Modify: `src/analytics/sql_service.py`
- Create: `tests/sql_analytics/test_integrity_service.py`

**Interfaces:**
- Produces: `Severity(StrEnum)` with `PASS`, `WARN`, and `FAIL`
- Produces: `ValidationFinding(code: str, severity: Severity, table: str | None, observed_count: int, sample_ids: tuple[str, ...], message: str)`
- Produces: `ValidationReport(schema_version: str, generated_at: datetime, database: str, tolerance: float, findings: tuple[ValidationFinding, ...])`
- Produces: `IntegrityService.validate(tolerance: float, sample_limit: int = 20) -> ValidationReport`
- Produces: `IntegrityService.failures_for_run_ids(run_ids: Collection[str], tolerance: float) -> tuple[ValidationFinding, ...]`
- Produces: `ValidationReport.has_failures -> bool`
- Produces: `IntegrityFailureError(report: ValidationReport)`

- [ ] **Step 1: Write failing clean-database and row-count tests**

Assert exact counts for all four tables, per-run child counts, `PASS` findings for duplicate and
orphan checks, and a versioned report structure. Assert `sample_limit` rejects values outside
`1..100`.

- [ ] **Step 2: Write failing defect-fixture tests**

Create separate pre-hardening databases with foreign keys temporarily disabled and inject one
duplicate metric key, one duplicate equity timestamp, one orphan for each child table, an invalid
quantity, inconsistent closed-trade nulls, an invalid run date range, and stored/derived return and
drawdown mismatches. Assert the exact finding code, severity, count, and capped identifiers.

- [ ] **Step 3: Implement the static integrity SQL resources**

Use `GROUP BY ... HAVING COUNT(*) > 1` for duplicates and `LEFT JOIN ... WHERE parent.id IS NULL`
for orphans. Use a pre-aggregated final-equity CTE for return reconciliation and the same running
peak method as the approved equity audit for drawdown reconciliation.

- [ ] **Step 4: Implement SQLite pragma checks without treating them as arbitrary catalogue SQL**

Execute fixed constants `PRAGMA integrity_check` and `PRAGMA foreign_key_check` through
`Connection.exec_driver_sql`. Convert every non-`ok` integrity row and every foreign-key row to a
fatal finding. Never accept a pragma name from the CLI.

- [ ] **Step 5: Implement severity assembly and selected-row relevance**

Classify duplicates, orphans, invalid invariants, and out-of-tolerance reconciliation as `FAIL`.
Classify no closed trades, missing optional metrics, and sparse uncontracted equity as `WARN`.
`sample_ids` is presentation-only. Implement `failures_for_run_ids` with scoped catalogue queries
using `bindparam("run_ids", expanding=True)` declared through `QuerySpec.expanding_params`; never
infer relevance from capped samples. Empty input returns an empty tuple without executing invalid
`IN ()` SQL.

- [ ] **Step 6: Test scoped relevance beyond the presentation cap**

Create more than 20 defective runs, select a defective run outside the first 20 sample identifiers,
and assert `failures_for_run_ids` still blocks it. Assert a clean selected run is not blocked by a
warning and that an empty selection performs no scoped query.

- [ ] **Step 7: Run integrity tests**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_integrity_service.py -v`

Expected: every controlled defect produces its exact documented finding without modifying the
database.

- [ ] **Step 8: Commit the integrity suite**

```bash
git add src/analytics/sql/integrity src/analytics/sql_contracts.py \
  src/analytics/sql_catalog.py src/analytics/sql_service.py \
  tests/sql_analytics/test_integrity_service.py
git commit -m "feat: add SQL integrity validation suite"
```

**Exit criteria:** Clean fixtures pass, every supported defect is detected deterministically, and
validation performs no writes.

**Rollback:** Revert this task's commit; analytical extraction remains available without the
validation gate.

---

### Task 5: Repair Alembic and apply focused SQLite schema hardening

**Context brief:** The existing migration is empty, while tables are created by ORM metadata.
Migrations must become authoritative without silently damaging legacy databases. The integrity
suite from Task 4 provides operator diagnostics; the migration also performs fixed preflight
counts before any DDL.

**Files:**
- Modify: `alembic/env.py:53-72`
- Modify: `alembic/versions/455406e2c7ac_initial_schema.py:21-31`
- Create: `alembic/versions/20260804_01_harden_analytics_integrity.py`
- Modify: `src/db/tables.py:14-77`
- Modify: `src/db/database.py:24-48`
- Create: `src/db/migrate.py`
- Modify: `src/api/main.py:15-18`
- Modify: `tests/test_api.py:77-80`
- Create: `tests/sql_analytics/test_migrations.py`
- Create: `tests/sql_analytics/test_legacy_migration.py`
- Create: `tests/sql_analytics/test_schema_verification.py`

**Interfaces:**
- Consumes: fixed integrity invariants from Task 4
- Produces: `verify_schema_current(engine: Engine) -> None`
- Produces: `SchemaNotCurrentError(current_revision: str | None, expected_revision: str)`
- Produces: `SchemaState(StrEnum)` with `EMPTY`, `BASELINE_UNVERSIONED`, `BASELINE_VERSIONED`,
  `CURRENT`, and `UNKNOWN`
- Produces: `SchemaAssessment(state: SchemaState, current_revision: str | None, differences: tuple[str, ...])`
- Produces: `assess_schema(engine: Engine) -> SchemaAssessment`
- Produces: `upgrade_database(database_url: str) -> MigrationOutcome`
- Produces: `MigrationOutcome(initial_state: SchemaState, previous_revision: str | None, current_revision: str, actions: tuple[str, ...])`

- [ ] **Step 1: Write failing fresh-migration tests**

Create an Alembic `Config`, set `config.attributes["database_url"]` to a temporary target SQLite
file, run `command.upgrade(config, "head")`, and assert the four tables, `alembic_version`, foreign
keys, unique constraints, and named indexes through SQLAlchemy inspection and SQLite pragmas. Run
the migration in an isolated subprocess whose `DATABASE_URL` points to a second temporary decoy
database containing a sentinel table. Assert the decoy remains unchanged, proving the explicit
target wins and no repository-default database is touched.

- [ ] **Step 2: Make Alembic honor configured URLs and injected connections**

In `run_migrations_online`, first use `config.attributes.get("connection")` when provided. Otherwise
resolve the URL in this order: `config.attributes.get("database_url")`, the `DATABASE_URL`
environment variable, then `config.get_main_option("sqlalchemy.url")`. Copy the Alembic section,
set only that copy's `sqlalchemy.url`, and pass it to `engine_from_config`. Use the same resolver for
offline migrations. `upgrade_database` always supplies the explicit `database_url` attribute.

- [ ] **Step 3: Implement the actual initial revision**

Translate the current `src/db/tables.py` baseline exactly into `op.create_table` calls. The initial
revision must not contain hardening-only unique constraints or analytical indexes; those belong to
the second revision so the benchmark can compare identical baseline and hardened databases.

- [ ] **Step 4: Generate and name the hardening revision**

Run: `uv run --python 3.12 --extra dev alembic revision --rev-id 20260804_01 -m "harden analytics integrity"`

Set `revision = "20260804_01"` and `down_revision = "455406e2c7ac"`.

- [ ] **Step 5: Write failing schema-classification and legacy-workflow tests**

Copy the tracked `tests/data/backtester.db` to a temporary path and assert it classifies as
`BASELINE_UNVERSIONED`. Also construct: an empty database, a valid baseline stamped at
`455406e2c7ac`, a current database, a stamped database missing a baseline table, a partially
matching schema, a wrong column type, an extra unexpected column, and an unknown revision. Assert
the exact state and sorted human-readable differences without changing any schema.

- [ ] **Step 6: Implement exact baseline assessment and the explicit upgrade workflow**

Define the corrected-baseline contract from table names, ordered columns, SQLite-affinity types,
nullability, primary keys, foreign keys, and absence of hardening constraints/indexes. Ignore only
SQLite internal tables and `alembic_version`. `upgrade_database` follows this closed state machine:

```text
EMPTY                  -> alembic upgrade head
BASELINE_UNVERSIONED   -> integrity preflight -> stamp 455406e2c7ac -> upgrade head
BASELINE_VERSIONED     -> integrity preflight -> upgrade head
CURRENT                -> no-op success
UNKNOWN                -> fail without stamp or DDL
```

Run assessment and integrity preflight before any stamp or upgrade. Expose
`python -m src.db.migrate --database PATH`, print the classified state and applied revisions, and
return nonzero without mutation for `UNKNOWN` or failed integrity. Never infer safety only from
table names.

- [ ] **Step 7: Write failing unsafe-legacy migration tests**

Build pre-hardening databases containing duplicate metrics, duplicate equity points, and orphaned
children. Stamp each at the initial revision, attempt `upgrade head`, and assert failure occurs
before schema/index changes. Compare `sqlite_master` and row counts before and after.

- [ ] **Step 8: Implement migration preflight and batch hardening**

At the beginning of `upgrade()`, execute fixed duplicate and orphan count statements through
`op.get_bind()`. Raise `RuntimeError` containing only invariant names and counts when any count is
nonzero. Then use `batch_alter_table` to add named unique constraints and `op.create_index` for:

```text
uq_metrics_backtest_metric(backtest_id, metric_name)
uq_equity_curve_backtest_date(backtest_id, date)
ix_trades_backtest_exit_id(backtest_id, exit_date, id)
ix_backtest_runs_symbol_dates(symbol, start_date, end_date)
```

Implement downgrade in reverse order.

- [ ] **Step 9: Align ORM metadata and add schema verification**

Add named `UniqueConstraint` and `Index` declarations matching the migration. Replace
`init_db()` table creation with a call to `verify_schema_current(get_engine())`. Determine current
and head revisions through Alembic's `MigrationContext` and `ScriptDirectory`; raise a message
containing `alembic upgrade head` when absent or stale.

- [ ] **Step 10: Isolate API tests from the default database**

Patch `src.api.main.init_db` in the `client` fixture before entering `TestClient`, because API tests
already create and inject their own `StaticPool` database. Add a focused lifespan test that an
unmigrated real engine raises `SchemaNotCurrentError`.

- [ ] **Step 11: Test real legacy upgrade, stamping, downgrade, and foreign-key rejection**

Run `upgrade_database` against a temporary copy of the tracked unversioned database and a generated
valid `create_all()` baseline; assert it performs preflight, stamp, and upgrade. Cover an already
stamped baseline, downgrade to the baseline, and upgrade again. Assert malformed and
stamped-but-missing-table databases remain byte-for-byte or schema-and-row-count identical after
refusal. Insert a child with a nonexistent parent on a normal connection and assert
`IntegrityError`.

- [ ] **Step 12: Run migration and regression tests**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_migrations.py tests/sql_analytics/test_legacy_migration.py tests/sql_analytics/test_schema_verification.py tests/test_db.py tests/test_api.py -v`

Expected: all fresh, legacy, refusal, downgrade, foreign-key, CRUD, and API tests pass.

- [ ] **Step 13: Commit schema management and hardening**

```bash
git add alembic/env.py alembic/versions src/db/database.py src/db/migrate.py \
  src/db/tables.py src/api/main.py tests/test_api.py tests/sql_analytics/test_migrations.py \
  tests/sql_analytics/test_legacy_migration.py \
  tests/sql_analytics/test_schema_verification.py
git commit -m "feat: harden SQLite analytics schema"
```

**Exit criteria:** Alembic builds a fresh database, safely upgrades a valid legacy baseline,
refuses invalid legacy state before DDL, and normal connections enforce foreign keys and natural
keys.

**Rollback:** Downgrade only test or backed-up databases to the initial revision. Revert the commit
for source rollback; never run downgrade against an unbacked user database.

---

### Task 6: Add deterministic artifact writing and the `compare` and `validate` CLI commands

**Context brief:** Services need a recruiter-friendly boundary that produces stable artifacts and
clear exit codes without moving logic into command handlers.

**Files:**
- Create: `src/analytics/sql_artifacts.py`
- Create: `src/analytics/sql_cli.py`
- Modify: `src/analytics/sql_contracts.py`
- Create: `tests/sql_analytics/test_artifacts.py`
- Create: `tests/sql_analytics/test_sql_cli.py`

**Interfaces:**
- Consumes: `AnalyticsService`, `IntegrityService`, `ComparisonFilters`, `ValidationReport`, and
  catalogue SQL hashes from Tasks 1, 2, and 4
- Produces: `write_comparison_bundle(frame: pd.DataFrame, metadata: ComparisonMetadata, csv_path: Path, metadata_path: Path, force: bool) -> tuple[ArtifactInfo, ArtifactInfo]`
- Produces: `main(argv: Sequence[str] | None = None) -> int`
- Produces: `ArtifactInfo(path: Path, byte_count: int, sha256: str)` and `ArtifactExistsError`
- Produces: `ComparisonMetadata(schema_version: str, generated_at: datetime, query_id: QueryId, sql_sha256: str, bound_params: Mapping[str, Scalar], database_identifier: str, row_count: int, ordered_columns: tuple[str, ...], contract_version: str, contract_valid: bool, validation_report_path: str, diagnostic_override: bool)`

- [ ] **Step 1: Write failing artifact tests**

Assert UTF-8 bytes, `\n` line endings, exact header order, no pandas index, explicit empty null
field, refusal to overwrite, `--force` overwrite, parent creation, cleanup of temporary files after
serialization failure, and all-or-nothing publication of the CSV/metadata pair. Inject failure on
the second publish and assert neither a new half-pair nor overwritten originals remain.

- [ ] **Step 2: Implement atomic CSV and JSON writers**

Preflight both destinations before writing. Stage both siblings with
`tempfile.NamedTemporaryFile(dir=path.parent, delete=False)`, call
`DataFrame.to_csv(index=False, encoding="utf-8", lineterminator="\n", na_rep="")` and
`json.dump(..., sort_keys=True, indent=2, default=serialize_supported_type)`, then flush and close
both. When `force` replaces existing files, move originals to uniquely named sibling backups;
publish both staged files with `Path.replace`; on any failure remove newly published files and
restore backups. Delete backups only after both publishes succeed.

- [ ] **Step 3: Write failing parser and exit-code tests**

Test required subcommands, ISO date validation, start-before-end, database path conversion to a
SQLite URL, `compare` outputs, `validate` report output, existing-file errors, empty cohorts,
integrity-blocked comparison, diagnostic override metadata, and exit codes `0` through `5`.
Assert exact `ComparisonMetadata` fields: query ID and hash, serialized bound parameters, UTC
timestamp, credential-free database identifier, row/column counts, contract version/status,
validation-report reference, and override status.

- [ ] **Step 4: Implement thin `argparse` command handlers**

Create required `compare`, `validate`, and `benchmark` subparsers; implement only `compare` and
`validate` in this task. `compare` accepts `--database`, `--symbol`, `--start`, `--end`, optional
`--strategy`, `--csv`, `--metadata`, `--diagnostic-override`, and `--force`. `validate` accepts
`--database`, `--out`, `--tolerance`, `--sample-limit`, and `--force`. Map domain exceptions to the
approved exit codes. Build the engine through `create_db_engine`, call services, then write
artifacts. Do not pass exception tracebacks to normal stdout/stderr unless `--verbose` is set.

- [ ] **Step 5: Add injection and no-write tests**

Pass SQL metacharacters through symbol and strategy values, snapshot table names and counts before
and after, and assert they are identical. Reject arbitrary sort keys and query IDs at argument
parsing.

- [ ] **Step 6: Run CLI and artifact tests**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_artifacts.py tests/sql_analytics/test_sql_cli.py -v`

Expected: deterministic bytes, safety probes, overwrite behavior, and all documented exits pass.

- [ ] **Step 7: Commit the CLI extraction boundary**

```bash
git add src/analytics/sql_artifacts.py src/analytics/sql_cli.py \
  src/analytics/sql_contracts.py tests/sql_analytics/test_artifacts.py \
  tests/sql_analytics/test_sql_cli.py
git commit -m "feat: add SQL analytics CLI exports"
```

**Exit criteria:** `compare` and `validate` generate deterministic validated artifacts and expose
stable, tested error behavior.

**Rollback:** Revert the task commit; services and migrations remain usable directly.

---

### Task 7: Add deterministic benchmark and SQLite query-plan evidence

**Context brief:** Performance evidence must be measured, reproducible, and honest. The benchmark
compares identical baseline and hardened databases created through Alembic, not two independently
generated datasets.

**Files:**
- Create: `src/analytics/sql_benchmark.py`
- Modify: `src/analytics/sql_cli.py`
- Create: `tests/sql_analytics/test_benchmark.py`
- Modify: `tests/sql_analytics/test_sql_cli.py`

**Interfaces:**
- Produces: `BenchmarkConfig(seed: int, run_count: int, equity_points_per_run: int, trades_per_run: int, warmups: int, repetitions: int)`
- Produces: `BenchmarkRunner.run(config: BenchmarkConfig) -> BenchmarkReport`
- Produces: `PlanRow(node_id: int, parent_id: int, auxiliary: int, detail: str)`
- Produces: `TimingSummary(samples_ns: tuple[int, ...], minimum_ns: int, median_ns: float, maximum_ns: int, p95_ns: int)`
- Produces: `QueryMeasurement(schema_variant: str, query_id: QueryId, sql_sha256: str, params: Mapping[str, Scalar], result_row_count: int, contract_valid: bool, plan_rows: tuple[PlanRow, ...], timing: TimingSummary)`
- Produces: `SchemaVariantEvidence(name: str, alembic_revision: str, table_row_counts: Mapping[str, int], indexes: Mapping[str, tuple[str, ...]])`
- Produces: `BenchmarkReport(schema_version: str, generated_at: datetime, config: BenchmarkConfig, environment: Mapping[str, str], variants: tuple[SchemaVariantEvidence, ...], measurements: tuple[QueryMeasurement, ...])`

- [ ] **Step 1: Write failing deterministic-generator tests**

Run the generator twice with the same configuration and assert identical per-table row counts,
primary identifiers, comparison result hashes, and fixture metadata. Run with a different seed and
assert identifiers or values differ while contracts still pass. Validate every positive count and
enforce bounded configuration values to prevent accidental resource exhaustion.

- [ ] **Step 2: Implement deterministic data generation in batches**

Use `random.Random(seed)` only through an injected local generator. Generate fixed-date cohorts,
strategies, metrics, closed trades, and equity paths. Insert with SQLAlchemy Core executemany in
bounded batches. Record actual inserted counts from the database after commit rather than deriving
them only from requested configuration.

- [ ] **Step 3: Write failing baseline-versus-hardened plan tests**

Upgrade a temporary database to `455406e2c7ac`, seed it, close every connection and dispose its
engine, then duplicate it with the standard-library `sqlite3.Connection.backup` API. Upgrade the
copy to head and run `ANALYZE` on both. Assert plan rows have four fields; each variant records its
Alembic revision, actual table counts, and index inventory; expected named indexes exist only in
the hardened copy; and every query in the fixed measurement matrix returns identical ordered-row
hashes and row counts from both copies.

- [ ] **Step 4: Implement safe plan capture**

Prefix only catalogue-owned SQL text with the fixed string `EXPLAIN QUERY PLAN ` and execute it
with the same named parameters. Convert the returned four columns to `PlanRow`. Record full detail
strings but expose derived `SCAN`, `SEARCH`, and `USE TEMP B-TREE` labels only for reporting. Never
assert raw node IDs or full strings across SQLite versions.

- [ ] **Step 5: Write failing timing-report tests with an injected clock**

Inject a monotonic nanosecond callable returning a fixed sequence and assert raw samples plus
minimum, median, maximum, and nearest-rank p95 calculations exactly. Define p95 as
`sorted_samples[math.ceil(0.95 * len(sorted_samples)) - 1]`. Assert warm-ups are executed but
excluded from recorded samples. Require `repetitions` in `1..100`; no test asserts that elapsed
time is below a threshold.

- [ ] **Step 6: Implement repeated timing and metadata capture**

Use `time.perf_counter_ns` by default. Define a fixed matrix containing the comparison, trade
sequence, equity audit, and cohort summary query IDs with parameters taken from the generated
fixture manifest. For each query/database pair, execute configured warm-ups,
then measured repetitions, fully materializing results inside the timed interval. Record query ID,
SQL hash, bound parameters, result rows, contract status, table counts, index names, plan rows,
Python/SQLite/SQLAlchemy/pandas versions, platform metadata, and UTC generation time.

- [ ] **Step 7: Wire and test the `benchmark` CLI command**

Add validated `--seed`, `--runs`, `--equity-points-per-run`, `--trades-per-run`, `--warmups`,
`--repetitions`, `--database-out`, and `--out` options. Defaults define the portfolio workload as
seed `42`, 150 runs, 2,520 equity points per run, 40 trades per run, three warm-ups, and 15 measured
repetitions. Preserve the hardened database at `--database-out`, write the report atomically, and
print the actual cohort filters required by the follow-up `compare` command.

- [ ] **Step 8: Run benchmark tests and a local smoke benchmark**

Run: `uv run --python 3.12 --extra dev pytest tests/sql_analytics/test_benchmark.py tests/sql_analytics/test_sql_cli.py -v`

Run: `uv run --python 3.12 --extra dev python -m src.analytics.sql_cli benchmark --runs 6 --equity-points-per-run 20 --trades-per-run 4 --warmups 1 --repetitions 3 --database-out /private/tmp/algo-sql-smoke.db --out /private/tmp/algo-sql-smoke.json`

Expected: tests pass and the smoke report contains observed counts, three timing samples per
query/database pair, hashes, versions, and plan rows. The small smoke configuration is verification,
not a resume benchmark.

- [ ] **Step 9: Commit repeatable performance evidence**

```bash
git add src/analytics/sql_benchmark.py src/analytics/sql_cli.py \
  tests/sql_analytics/test_benchmark.py tests/sql_analytics/test_sql_cli.py
git commit -m "feat: add SQLite analytics benchmark"
```

**Exit criteria:** The benchmark proves identical result values across baseline and hardened
schemas and emits complete observed timing and planner evidence without a fabricated target.

**Rollback:** Revert the task commit and remove only explicitly generated benchmark artifacts.

---

### Task 8: Package resources, document measured results, and run the full verification gate

**Context brief:** The feature is not recruiter-verifiable until installed wheels retain SQL
resources, commands are documented, and all claims trace to artifacts generated by the completed
implementation.

**Files:**
- Modify: `pyproject.toml:1-42`
- Modify: `Makefile:1-28`
- Modify: `README.md`
- Create: `docs/sql-analytics.md`
- Create: `tests/sql_analytics/test_packaging.py`

**Interfaces:**
- Consumes: all services and CLI commands from Tasks 1–7
- Produces: installed-wheel resource availability and documented demo commands

- [ ] **Step 1: Write a failing wheel resource smoke test**

Build a wheel to a temporary directory, create an isolated virtual environment, install that exact
wheel, and run a subprocess with no source checkout on `PYTHONPATH`. In the subprocess, migrate a
temporary database, insert the deterministic small fixture, load every analytical and integrity
`QueryId`, execute each with its valid fixed parameter set, and assert its declared result contract
or integrity result shape. This verifies both loading and execution from the installed wheel.

- [ ] **Step 2: Configure Hatch to include SQL resources**

Add the narrowest Hatch wheel include configuration that packages `src/analytics/sql/**/*.sql`
alongside the `src` package. Build the wheel and inspect its archive names before rerunning the
smoke test.

- [ ] **Step 3: Add stable Make targets**

Add `sql-validate`, `sql-compare`, and `sql-benchmark-smoke` targets that invoke
`$(PYTHON) -m src.analytics.sql_cli`. Parameters use Make variables with documented defaults only
for file paths; symbol and date cohort values are supplied explicitly by the caller.

- [ ] **Step 4: Generate the implementation evidence artifacts**

Run the portfolio profile with 150 synthetic runs, 2,520 equity points per run, 40 closed trades
per run, three warm-ups, and 15 measured repetitions. These are declared workload inputs, not
claims about a live dataset. Save the command and report path; the report records actual inserted
row counts. Do not commit the generated databases. Commit a compact JSON report only when it has
no machine-sensitive path and is at most 250 KiB; otherwise document the exact regeneration
command and summarize only values read from the generated report.

- [ ] **Step 5: Write `docs/sql-analytics.md` from observed evidence**

Document each analytical question, involved tables, CTEs and windows, input/output grain,
validation performed, migration behavior, exact demo commands, and values read from the generated
benchmark and validation reports. Label the dataset deterministic/synthetic. Include SQLite and
library versions next to any latency or plan observation.

- [ ] **Step 6: Update README without overstating capabilities**

Add a concise direct-SQL analytics feature description and link to `docs/sql-analytics.md`. Mention
SQLite measurement only. Do not state PostgreSQL readiness, performance improvement, or data
quality improvement unless the linked artifact directly supports that exact statement.

- [ ] **Step 7: Run the complete verification matrix**

Run: `uv run --python 3.12 --extra dev pytest -q`

Run: `uv run --python 3.12 --extra dev ruff check src tests`

Run: `uv run --python 3.12 --extra dev mypy src --strict`

Run: `uv build`

Run the documented `benchmark`, `validate`, and `compare` commands against a temporary artifact
directory and inspect the CSV header, JSON schema versions, observed row counts, and plan rows.

Expected: all tests, lint, type checking, wheel build, resource smoke test, and three demo commands
pass. Any baseline warning already present before implementation is reported separately and is not
silently attributed to this feature.

- [ ] **Step 8: Perform the resume-evidence audit**

For every number in README or `docs/sql-analytics.md`, identify the exact JSON field or test command
that supports it. Remove any number without reproducible evidence. Confirm the phrase “PostgreSQL
ready” does not appear.

- [ ] **Step 9: Commit documentation and packaging**

```bash
git add pyproject.toml Makefile README.md docs/sql-analytics.md \
  tests/sql_analytics/test_packaging.py
git commit -m "docs: add SQL analytics verification guide"
```

**Exit criteria:** Installed wheels contain every query, the full quality gate passes, and every
published claim is traceable to a reproducible command or generated artifact.

**Rollback:** Revert the documentation/packaging commit. Preserve generated evidence outside the
repository or remove only its explicit artifact directory.

---

## Cross-Task Verification Invariants

After every task:

1. Run the task's focused tests.
2. Run `git diff --check`.
3. Confirm `git status --short` contains only intended source and documentation paths.
4. Confirm no database, CSV, JSON, `.venv`, `uv.lock`, or regenerated tracked bytecode is staged.
5. Confirm SQL files contain named binds for external values and no f-string/template syntax.
6. Confirm comparison result grain remains one row per run.

Before final merge:

1. Run the entire verification matrix from Task 8.
2. Review all migrations against both fresh and copied legacy databases.
3. Inspect generated CSV and JSON artifacts manually.
4. Compare every resume/documentation claim to its supporting artifact.

## Plan Mutation Protocol

- Split a task only when each resulting task retains an independent red-green-refactor cycle and
  commit.
- Insert a task by assigning a decimal number, documenting its dependency edges, and updating the
  dependency graph before implementation.
- Reorder tasks only when all consumed interfaces already exist and no shared-file parallel edit is
  introduced.
- Skip a task only with project-owner approval and an explicit record of which acceptance criteria
  are removed.
- When repository behavior contradicts this plan, stop, record the observed evidence, update the
  design or ADR if the architecture changes, and obtain approval before continuing.

## Final Handoff

Implementation should use `superpowers:subagent-driven-development` for fresh-agent execution and
two-stage review, or `superpowers:executing-plans` for inline batches with checkpoints. This plan
does not authorize implementation during the architecture session.

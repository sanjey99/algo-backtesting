# Design: SQL Analytics and Data Extraction Layer

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

Checks exact ordered columns, row count, uniqueness keys, required-value nullability, and
coercible numeric/date values. A contract failure blocks export.

### 5.5 `IntegrityService`

Runs row-count reconciliation, duplicate and orphan queries, stored-versus-derived checks,
`PRAGMA integrity_check`, and `PRAGMA foreign_key_check`. It produces a structured report with
severity, affected table, observed count, capped sample identifiers, and remediation guidance.

### 5.6 `ArtifactWriter`

Writes stable UTF-8 CSV and JSON artifacts with deterministic column ordering and explicit null
representation. It writes to a temporary sibling file and atomically renames only after
validation succeeds. Existing files are not overwritten without `--force`.

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
4. If the expected baseline exists without an Alembic version, stamp the corrected initial
   revision and apply the hardening revision.
5. If a database is already stamped at the initial revision, verify that the baseline tables
   actually exist before applying hardening. A stamped database with missing baseline tables is a
   fatal schema-state error and is not repaired silently.
6. Stop without modifying the database when duplicates, orphans, or unexpected schema state would
   make hardening unsafe.
7. Do not migrate silently during API startup. Startup verifies schema compatibility and reports
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
hardened/indexed schema. Both run `ANALYZE` before measurement.

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

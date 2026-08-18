# SQL analytics verification guide

This layer answers portfolio-analysis questions with reviewed, packaged SQL rather than generated
query text. It is implemented and measured for SQLite. The evidence below uses a deterministic,
synthetic fixture; it is not live trading data and is not a production latency claim.

## Analytical questions and result grain

| Query ID | Question and inputs | Tables and SQL structure | Output grain |
|---|---|---|---|
| `strategy_run_comparison` | How do runs for one exact symbol/start/end cohort compare, optionally within one strategy? | `selected_runs` filters `backtest_runs`; `metric_pivot`, `trade_stats`, `ranked_equity`, and `run_facts` pre-aggregate `metrics`, closed `trades`, and the latest `equity_curve` point. `ROW_NUMBER` selects latest equity; `RANK` orders derived return and Sharpe within matching capital and cost assumptions. | One row per run (`run_id` is unique). |
| `trade_sequence` | How did realized P&L, wins, win rate, and the rolling five-trade mean evolve for one run? | `closed_trades` excludes open/incomplete trades. `ROW_NUMBER`, cumulative `SUM`, and a five-row `AVG` window use `(exit_date, trade_id)` for deterministic order. | One row per closed trade. |
| `equity_drawdown_audit` | Does each stored drawdown agree with the equity path for one run within a bound tolerance? | `ordered_equity`, `running_peak`, and `calculated` read `equity_curve`. `LAG`, `ROW_NUMBER`, and cumulative `MAX` derive point return, high-water mark, drawdown, absolute delta, and mismatch status. | One row per equity point. |
| `strategy_cohort_summary` | How do strategies aggregate and rank inside comparable cohorts, subject to optional bounds and a minimum run count? | `selected_runs`, `metric_pivot`, `closed_trade_counts`, `ranked_equity`, `run_facts`, `cohort_facts`, and `ranked_cohorts` read all four application tables. `ROW_NUMBER` chooses final equity and `DENSE_RANK` ranks average derived return within identical symbol/date/capital/cost dimensions. | One row per strategy and exact cohort dimension set. |

The integrity catalogue adds these result grains:

| Query ID | Validation performed | Output grain |
|---|---|---|
| `integrity_table_counts` | Counts the four application tables. | One row per table. |
| `integrity_per_run_reconciliation` | Reconciles total, open, and closed trades plus equity and metric counts. | One row per run. |
| `integrity_duplicate_metrics` | Finds repeated `(run_id, metric_name)` keys. | One row per duplicated key. |
| `integrity_duplicate_equity` | Finds repeated `(run_id, timestamp)` keys. | One row per duplicated key. |
| `integrity_orphan_children` | Finds trade, equity, or metric rows without a parent run. | One row per defect record. |
| `integrity_invalid_records` | Finds inverted run dates, invalid capital or fees, nonpositive quantities, and inconsistent trade-close fields. | One row per defect record. |
| `integrity_metric_reconciliation` | Recomputes total return and drawdown from equity and compares them with stored values using a bound tolerance. | One row per mismatch. |

All external values are named binds. `QueryCatalogue` accepts only its closed `QueryId` set,
checks the SQL bind names exactly, and fingerprints the loaded SQL. `AnalyticsService` then
normalizes columns to a declared contract and checks order, types, nullability, numeric bounds, and
unique grain. Comparison export also runs selected-run integrity checks and blocks fatal findings
unless the caller explicitly requests diagnostic output. JSON and CSV publication is atomic and
does not overwrite existing destinations unless the relevant command supports and receives
`--force`.

## Migration behavior

Run migrations explicitly before application startup:

```bash
.venv/bin/python -m src.db.migrate --database /private/tmp/algo-analytics.db
```

The migration workflow classifies an empty database, the exact unversioned baseline, the stamped
baseline, the current schema, or an unknown schema. Empty databases upgrade to head. Exact legacy
baselines pass duplicate/orphan preflight checks before stamping or upgrading. Current databases
are unchanged. Unknown or structurally inconsistent databases are refused without stamping. The
current head is `20260804_01`, following baseline revision `455406e2c7ac`; the hardened revision
adds unique keys for metric names and equity timestamps and indexes run-cohort selection and
closed-trade ordering. The installed-wheel test proves that the wheel contains both migrations,
the Alembic configuration, and every catalogue SQL resource.

## Reproducible commands

Use fresh destination paths because benchmark artifacts are never overwritten implicitly. The
portfolio command below is the exact workload used for the observations in this guide:

```bash
mkdir -p /private/tmp/algo-sql-portfolio-seed42
.venv/bin/python -m src.analytics.sql_cli benchmark \
  --seed 42 \
  --runs 150 \
  --equity-points-per-run 2520 \
  --trades-per-run 40 \
  --warmups 3 \
  --repetitions 15 \
  --database-out /private/tmp/algo-sql-portfolio-seed42/portfolio.db \
  --out /private/tmp/algo-sql-portfolio-seed42/portfolio.json

.venv/bin/python -m src.analytics.sql_cli validate \
  --database /private/tmp/algo-sql-portfolio-seed42/portfolio.db \
  --out /private/tmp/algo-sql-portfolio-seed42/validation.json

.venv/bin/python -m src.analytics.sql_cli compare \
  --database /private/tmp/algo-sql-portfolio-seed42/portfolio.db \
  --symbol SPY --start 2022-01-01 --end 2024-12-31 \
  --csv /private/tmp/algo-sql-portfolio-seed42/comparison.csv \
  --metadata /private/tmp/algo-sql-portfolio-seed42/comparison-metadata.json
```

The Make targets select `.venv/bin` on Unix-like systems and `.venv/Scripts` on Windows. Override
`VENV` to use another virtual-environment directory and `TEMP_DIR` to choose where smoke artifacts
are written. `SQL_SYMBOL`, `SQL_START`, and `SQL_END` are required rather than defaulted:

```bash
make sql-benchmark-smoke

make sql-validate

make sql-compare SQL_SYMBOL=SPY SQL_START=2022-01-01 SQL_END=2024-12-31
```

The smoke target deliberately uses the smaller checked-in verification profile from the
implementation plan. It is not the portfolio evidence workload.

## Observed deterministic portfolio evidence

The measured report was generated at
`/private/tmp/algo-sql-task8-portfolio-20260804-a/portfolio.json`. At `6,081,019` bytes it is kept
outside the repository. The regeneration command above writes the same report shape without
depending on that machine-local path.

The report's `config` records seed `42`, `150` runs, `2520` equity points per run, `40` closed
trades per run, `3` warm-ups, and `15` recorded samples. Its observed fixture counts—not merely
requested counts—are:

| Table | Observed rows |
|---|---:|
| `backtest_runs` | 150 |
| `equity_curve` | 378000 |
| `trades` | 6000 |
| `metrics` | 1500 |

The environment recorded in that JSON is CPython `3.12.13`, SQLite `3.53.1`, SQLAlchemy `2.0.51`,
and pandas `3.0.5` on `macOS-26.5.2-arm64-arm-64bit`. Timings use `perf_counter_ns`, fully
materialize the validated frame, exclude warm-ups, and have no pass/fail threshold.

| Schema | Query | Result rows | Median ms | p95 ms | Plan rows |
|---|---|---:|---:|---:|---:|
| baseline | `strategy_run_comparison` | 150 | 508.039458 | 571.953834 | 32 |
| baseline | `trade_sequence` | 40 | 2.739584 | 3.029750 | 6 |
| baseline | `equity_drawdown_audit` | 2520 | 21.856708 | 28.699000 | 12 |
| baseline | `strategy_cohort_summary` | 3 | 420.680583 | 488.363500 | 31 |
| hardened | `strategy_run_comparison` | 150 | 490.980792 | 554.671083 | 29 |
| hardened | `trade_sequence` | 40 | 2.532000 | 2.669333 | 5 |
| hardened | `equity_drawdown_audit` | 2520 | 9.652917 | 28.667333 | 11 |
| hardened | `strategy_cohort_summary` | 3 | 396.328417 | 529.777042 | 28 |

Every measurement reports `contract_valid: true`. Baseline and hardened result row counts and
result hashes match for each query. The baseline revision has no named application indexes; the
hardened revision reports `ix_backtest_runs_symbol_dates` and `ix_trades_backtest_exit_id`.
Observed plan details include a baseline `SCAN t` versus a hardened indexed `SEARCH t` for trade
sequence, and a baseline `SCAN r` versus a hardened cohort-index `SEARCH r` for run comparison.
Temporary B-trees remain in windowed ordering and cohort aggregation plans, so these observations
do not support a blanket performance claim.

Validation of the preserved hardened database wrote schema `1.0` with `26` findings, all `PASS`,
and independently observed the same four table counts. Comparison metadata wrote schema `1.0`,
contract `1.0`, `contract_valid: true`, and `row_count: 150`; the CSV header is the declared
27-column comparison contract and the data grain remains one row per run.

## Evidence audit

These commands locate every measured value above in the generated artifacts:

```bash
stat -f '%z bytes' /private/tmp/algo-sql-task8-portfolio-20260804-a/portfolio.json

jq '{schema_version, config, environment, counts: .fixture.table_row_counts,
     variants: [.variants[] | {name, alembic_revision, indexes, table_row_counts}]}' \
  /private/tmp/algo-sql-task8-portfolio-20260804-a/portfolio.json

jq -r '.measurements[] |
  [.schema_variant, .query_id, .result_row_count,
   (.timing.median_ns / 1000000), (.timing.p95_ns / 1000000),
   (.plan_rows | length), .contract_valid, .result_sha256] | @tsv' \
  /private/tmp/algo-sql-task8-portfolio-20260804-a/portfolio.json

jq -r '.measurements[] | .schema_variant, .query_id, .plan_rows[].detail' \
  /private/tmp/algo-sql-task8-portfolio-20260804-a/portfolio.json

jq '{schema_version, tolerance, finding_count: (.findings | length),
     severity_counts: (.findings | group_by(.severity) |
       map({(.[0].severity): length}) | add),
     counts: [.findings[] | select(.code | startswith("TABLE_COUNT_")) |
       {code, observed_count}]}' \
  /private/tmp/algo-sql-task8-portfolio-20260804-a/validation.json

head -n 1 /private/tmp/algo-sql-task8-portfolio-20260804-a/comparison.csv
jq '{schema_version, contract_version, contract_valid, row_count, ordered_columns,
     bound_params}' \
  /private/tmp/algo-sql-task8-portfolio-20260804-a/comparison-metadata.json
```

Migration state behavior is exercised by
`pytest -q tests/sql_analytics/test_legacy_migration.py tests/sql_analytics/test_migrations.py`.
Installed migration and query execution is exercised by
`pytest -q tests/sql_analytics/test_packaging.py`. The feature-scoped static checks are:

```bash
.venv/bin/ruff check \
  alembic/env.py alembic/versions \
  src/analytics/sql_artifacts.py src/analytics/sql_benchmark.py \
  src/analytics/sql_catalog.py src/analytics/sql_cli.py \
  src/analytics/sql_contracts.py src/analytics/sql_service.py \
  src/api/main.py src/db/database.py src/db/migrate.py src/db/tables.py \
  tests/sql_analytics tests/test_api.py

.venv/bin/mypy \
  src/analytics/sql_artifacts.py src/analytics/sql_benchmark.py \
  src/analytics/sql_catalog.py src/analytics/sql_cli.py \
  src/analytics/sql_contracts.py src/analytics/sql_service.py \
  src/api/main.py src/db/database.py src/db/migrate.py src/db/tables.py \
  --strict --python-version 3.12
```

Both scoped commands are clean. The repository-wide `make lint` gate is also clean: Ruff passes
across `src` and `tests`, and strict mypy passes across all source modules. Warning-promotion gates
cover the API, database, and SQL analytics surfaces; current release evidence should be regenerated
with the commands above rather than inferred from historical counts.

## Limitations

- The dataset is deterministic and synthetic. Passing integrity checks establishes consistency
  with the encoded invariants, not correctness against an external broker or market-data source.
- The timings describe one local SQLite/library/platform combination. They are not portable
  service-level objectives, cross-database results, or evidence of a universal speedup.
- Query plans are SQLite planner observations. Raw node identifiers and full plan strings can vary
  with SQLite versions and statistics.
- The benchmark compares identical baseline and hardened copies and checks result equality, but it
  does not simulate concurrent writers, remote storage, or production workload skew.
- Comparison metadata currently leaves `validation_report_path` empty; validation is executed as a
  preflight rather than linked to a separately persisted report.
